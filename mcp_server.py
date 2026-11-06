# -*- coding: utf-8 -*-
"""识屏系统 MCP Server（xuanlan-screenpilot）—— ScreenPilot 出品

把 CLI 十一命令包成 MCP 工具，让 ZCode 等
agent 直接挂载使用。stdio 传输；直 import sp 引擎包（进程常驻，OCR 模型
只加载一次，比 subprocess 调 CLI 快）。

工具与 CLI 对应：
  scan_screen    <-> scan    扫描屏幕/窗口 → 元素表 + 标注图（三引擎并行；连续 scan 编号自动保号；meta 自动入库摘要）
  activate_window <-> activate 按标题子串激活（置前）窗口
  window_stack   <-> stack   窗口 Z 序全景（堆叠状态感知）
  click_element  <-> click    按编号点击（默认漂移点击，precise 恢复精准）
  click_xy       <-> clickxy  按屏幕物理坐标直接点击（hit_window 反馈+top_must 拦截）
  type_text      <-> type     按编号输入文本（中文走剪贴板）
  press_key      <-> key      按键（支持中文键名）
  scroll_screen  <-> scroll   滚动
  snap_screen    <-> snap     截屏保存（--bg/后台模式 PrintWindow 抓窗）
  crop_screen    <-> crop     按编号裁元素原分辨率局部图（两级视觉·看清通道）
  crop_xy        <-> cropxy   按物理坐标区域裁片（看清通道兜底）
  book_find      <-> find     检索元素书库（两级视觉·文本通道：按名查 bbox 直达）

注意：stdout 是 MCP 协议通道，本文件任何代码禁止 print 到 stdout；
数据（元素缓存/标注图/截图）固定写到 mcp_data\\ 目录，不依赖启动 cwd。
"""
import sys

# 标准流先重配置为 UTF-8：stdout 承载 JSON-RPC（含中文内容），stderr 承载日志；
# Windows 管道默认 GBK 会炸/乱码。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import os

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

MCP_DATA = os.path.join(SERVER_DIR, "mcp_data")
os.makedirs(MCP_DATA, exist_ok=True)
元素缓存路径 = os.path.join(MCP_DATA, "elements.json")
追踪状态路径 = os.path.join(MCP_DATA, "track_state.json")

# DPI 感知必须尽早在主线程设置（截屏/UIA/点击三方同一物理像素坐标系）
from sp import enable_dpi_awareness

enable_dpi_awareness()

import json
import time
import functools
from typing import Union

from mcp.server.fastmcp import FastMCP

from sp import annotate, booklib, capture, cleanup, executor, lastshot, \
    pipeline, stacking, uia_engine, waiter, watch
from sp.model import element_from_dict
from sp.ocr_engine import preload as _ocr预热

# F5：MCP 常驻进程启动即后台预热 OCR 模型（加载 4~6s），会话首扫只付推理账
_ocr预热()

# 收台机制（登记-清理两段式）：启动即开会话账本 + 扫上窗残留（设计页 §5）
cleanup.init_session("mcp")
cleanup.startup_scan()

mcp = FastMCP(
    "xuanlan-screenpilot",
    instructions=(
        "ScreenPilot（screenpilot）：把 Windows 真实屏幕变成结构化元素表，"
        "按编号闭环操作，不看图猜坐标。协议：① 先 scan_screen（可指定窗口标题子串）"
        "拿到带编号元素表与标注图路径；② 按编号 click_element / type_text 操作，"
        "press_key / scroll_screen 做按键与滚动；③ 连续 scan_screen 元素编号自动保持"
        "稳定（同元素保号，新元素追加新号，meta.tracker 可查 kept/new/lost 统计）；"
        "④ 元素消失后其编号不再存在，操作后界面已变的仍需重扫核对。窗口被遮挡时用"
        " background=True 后台抓窗（PrintWindow，不置前）；click/type 默认漂移点击"
        "（人手感落点），precise=True 恢复像素级精准。元素表漏识的目标（如"
        "游戏自绘「×」关闭按钮，visual 引擎已带专项识别）可从 snap/标注图读"
        "坐标后用 click_xy 直点。点击/输入可带 expect={find,mode,timeout,"
        "interval} 做业务生效判定：操作后轮询扫描等待文本出现/消失，超时如实"
        "返回未命中。"
        "堆叠协议：屏幕画面是多窗口堆叠合成，物理点击永远打在最顶层窗口——"
        "分不清前台/遮挡时先调 window_stack 看窗口 Z 序全景；click_element "
        "默认落点归窗核验（被遮挡即拒绝并报 top_window）；click_xy 结果附 "
        "hit_window（点到了谁），可加 top_must 事前拦截。后台抓窗"
        "（background=True）看到的是窗口自身渲染而非前台堆叠真相，其 meta."
        "occlusion 给出该窗当前可见率，物理点击前须先置前。标注图与截图返回"
        "绝对路径，标注图上/左边缘印有屏幕物理"
        "坐标刻度轴（每 100px 主刻度+数字、50px 半格短刻度），读图即可得 "
        "click_xy 直点坐标。"
        "两级视觉协议：定位走文本（地图），看清走裁片（风景），互不污染。"
        "① 看不清元素上的文字/图标时用 crop_screen(element_id) 裁该元素"
        "原分辨率局部图（整屏图在视觉编码端被摊薄，裁片原生逐字清），"
        "漏识目标用 crop_xy(x1,y1,x2,y2)；② scan_screen 会自动把带名元素"
        "入库（manual/<应用域>/，应用域×类型两级分类，同名元素按空间桶"
        "并存多候选），book_find(query) 按名查 bbox 直达目标位置，不整屏"
        "重扫——stale=false 的 bbox 已按当前窗口位置换算（窗口移动坐标"
        "随动）；stale=true 须重扫（reason 说明原因）；同名多候选看 "
        "pos_hint 分辨。"
        "按键支持中文键名（回车/esc/上/下/删除、ctrl+s 组合等）。"
        "时间维度协议（静态扫描抓不到瞬时状态）：① 点了按钮闪过的提示/"
        "飘字用 watch_burst（可带前置 action，动作后逐帧差分报 hit）；"
        "② 输入是否生效用 verify_typing（动作前基线，观察回显区变化，"
        "自绘输入框/无文字属性控件适用）；③ 窗口疑似卡死用 probe_hang"
        "（屏幕通道发键看变化；PrintWindow 对挂起窗会永久阻塞，别用 scan/"
        "snap 后台模式碰挂死窗）；④ 游戏画面抠 UI 底图/分实体与动画层用 "
        "split_static_dynamic（多帧抠底+净位移分层）。"
    ),
)


def _文本(obj) -> str:
    """工具结果统一为单行 JSON 文本；首调捎带残窗提醒（收台机制，设计页 §2）。"""
    try:
        通知 = cleanup.take_notice()
        if 通知 and isinstance(obj, dict):
            obj["residual_notice"] = 通知
    except Exception:
        pass
    return json.dumps(obj, ensure_ascii=False)


def _读缓存():
    if not os.path.exists(元素缓存路径):
        raise FileNotFoundError("元素缓存不存在（请先 scan_screen）")
    with open(元素缓存路径, "r", encoding="utf-8") as f:
        return json.load(f).get("elements", [])


def _查元素(元素id):
    for d in _读缓存():
        if d.get("id") == 元素id:
            return element_from_dict(d)
    raise ValueError("缓存中无编号 %s 的元素（请重新 scan_screen 刷新编号）" % 元素id)


def _前置窗口(wc):
    """窗口置前（截图与控件树同源；SetActive 内部处理前台锁）。"""
    uia_engine.activate_control(wc)


# 帧间编号追踪：状态按窗口分桶（同窗口反复扫描保号；窗口切换互不干扰），
# 进程内存持有 + 落盘双保险（server 重启也能续上保号链）。
追踪状态 = {}
if os.path.exists(追踪状态路径):
    try:
        with open(追踪状态路径, "r", encoding="utf-8") as f:
            追踪状态 = json.load(f)
    except Exception:
        追踪状态 = {}


def _存追踪状态(桶key, 状态):
    追踪状态[桶key] = 状态
    try:
        with open(追踪状态路径, "w", encoding="utf-8") as f:
            json.dump(追踪状态, f, ensure_ascii=False)
    except Exception:
        pass


def _扫描核心(win=None, no_focus=False, max_nodes=800, no_uia=False, no_ocr=False,
              no_visual=False, background=False, hwnd=None, incremental=None,
              book=None, compact=None):
    """scan 核心流程：调 sp.pipeline 并行管线（自动编号追踪）→ 标注图 + 缓存。

    incremental/book/compact 透传三态（None=智能默认）：book 三态映射到
    no_book 时不能用 `not book`（None 取反会错成强制不入库），须显式映射。
    """
    桶key = str(hwnd) if hwnd else (win if win else "screen")
    旧状态 = 追踪状态.get(桶key)
    结果 = pipeline.scan(win=win, no_focus=no_focus, max_nodes=max_nodes,
                       no_uia=no_uia, no_ocr=no_ocr, no_visual=no_visual,
                       background=background, hwnd=hwnd, incremental=incremental,
                       no_book=(True if book is False
                                else (False if book else None)),
                       compact=compact,
                       # None→{}：首帧也要建追踪器写快照，下一帧才能保号
                       tracker_state=旧状态 if 旧状态 is not None else {})
    meta, 元素, img = 结果["meta"], 结果["elements"], 结果["img"]
    if 结果["tracker"]:
        meta["tracker"] = 结果["tracker"]["stats"]
        _存追踪状态(桶key, 结果["tracker"]["state"])

    标注路径 = os.path.join(MCP_DATA, "annotated_scan.png")
    try:
        annotate.annotate(img, 元素, 标注路径, offset=meta["offset"])
    except Exception:
        标注路径 = None

    缓存 = {
        "meta": meta,
        "elements": [e.to_dict() for e in 元素],
    }
    with open(元素缓存路径, "w", encoding="utf-8") as f:
        json.dump(缓存, f, ensure_ascii=False, indent=1)

    # 两级视觉·裁片通道地基：登记最近抓图（原图+状态），crop_screen/crop_xy
    # 从这里取同源图与同源元素表裁片（2026-10-28 设计方案施工单①）
    try:
        lastshot.register(img, meta, elements_json=元素缓存路径)
    except Exception as e:
        meta["errors"]["lastshot"] = str(e)

    for d in 缓存["elements"]:
        if len(d["text"]) > 40:
            d["text"] = d["text"][:40] + "…"
    缓存["meta"]["annotated_png"] = 标注路径
    return 缓存


@mcp.tool()
def scan_screen(window: str = None, no_focus: bool = False, max_nodes: int = 800,
                no_uia: bool = False, no_ocr: bool = False, no_visual: bool = False,
                background: bool = False, hwnd: int = None,
                incremental: bool = None, book: bool = None,
                compact: bool = None) -> str:
    """扫描屏幕或指定窗口，返回带编号的结构化元素表（agent 操作屏幕的第一步）。

    连续扫描元素编号自动保持稳定（同元素保号，新元素追加新号；meta.tracker
    报告 kept/new/lost 统计）。

    Args:
        window: 窗口标题子串（如 "记事本"）；缺省扫全屏。窗口模式默认自动置前。
        no_focus: 窗口扫描时不自动置前。
        max_nodes: UIA 遍历节点上限（默认 800）。
        no_uia / no_ocr / no_visual: 分别跳过 UIA / OCR / 视觉引擎（调试用）。
        background: 后台模式——不置前窗口，PrintWindow 抓窗（窗口被遮挡也能抓
            到真实画面）；黑帧自动回退屏幕拷贝并以 meta.bg_fallback 提示。
        hwnd: 窗口句柄直定位（优先于 window 参数；配合 background 天然非前台）。
        incremental: 增量模式三态。None（默认）=智能：同一窗口/同一全屏 60 秒内
            连续扫自动走增量（meta 报 incremental=true + incremental_auto=true），
            首帧/换窗口/超时自动退全量——连续观察同一窗口不必手动开它；
            true=强制增量；false=强制全量。增量=与上一帧差分只重扫变化区域，
            滚动场景自动平移对齐复用旧条目（滚动过的旧条目入库标 stale，
            book_find 如实报 stale=true + reason），meta 带 incremental/
            refreshed_rects/scroll_shift/content_shifted 可查。
        book: 是否把本次扫描写入元素书库，三态。None（默认）=智能：窗口扫描
            入库、全屏扫描不入库（桌面/任务栏噪声无检索价值；要全屏入库显式
            传 true）；敏感窗口（记事本/聊天/密码登录类标题，名单 sp/privacy.py）
            命中时智能默认改为不入库并报 meta.privacy_skip=true；true=强制
            入库（不被隐私名单拦）；false=强制不入库——临时扫描或屏幕上有
            敏感内容时传 false，本次不落库、不建裁片。
        compact: 扫描档三态。None（默认）=compact 档：滤空文本非交互壳+同文本
            同位去重（meta 报 compact_removed 统计），元素表更短更省上下文；
            false=关（全量元素表，基准对照/怀疑滤多了用）；true=强制开（同默认）。
    """
    try:
        结果 = _扫描核心(win=window, no_focus=no_focus, max_nodes=max_nodes,
                       no_uia=no_uia, no_ocr=no_ocr, no_visual=no_visual,
                       background=background, hwnd=hwnd, incremental=incremental,
                       book=book, compact=compact)
        结果["hint"] = ("按 id 调 click_element / type_text 操作元素；连续 scan 编号"
                      "自动保号，元素消失的编号不复用；界面已变的操作仍需重扫核对。"
                      "annotated_png 为标注图路径，可读图核对。")
        return _文本(结果)
    except Exception as e:
        return _文本({"ok": False, "error": "scan 失败: %s" % e})


@mcp.tool()
def scan_image(image_path: str, out_path: str = None, no_ocr: bool = False,
               no_visual: bool = False) -> str:
    """静态图片识别：对图片文件跑 OCR+视觉双引擎，输出与活屏扫描同构的元素表。

    用途：公开统考（ScreenSpot 系静态截图）/ 离线图片识别。与活屏 scan 的
    差异（meta.source="image"）：无 UIA 控件树；bbox 为图像像素坐标
    （offset=(0,0)）；不写 tracker；不写元素书库（静态图不入库）。

    Args:
        image_path: 图片文件路径（中文路径安全）。
        out_path: 标注图输出路径（缺省存 mcp_data\\scan_image_<时间戳>.png）。
        no_ocr / no_visual: 分别跳过 OCR / 视觉引擎（调试用）。
    """
    try:
        结果 = pipeline.scan_image(image_path, no_ocr=no_ocr, no_visual=no_visual)
        meta, 元素, img = 结果["meta"], 结果["elements"], 结果["img"]
        if not out_path:
            out_path = os.path.join(
                MCP_DATA, "scan_image_%s.png" % time.strftime("%H%M%S"))
        out_path = os.path.abspath(out_path)
        try:
            annotate.annotate(img, 元素, out_path, offset=(0, 0))
            meta["annotated_png"] = out_path
        except Exception as e:
            meta["errors"]["annotate"] = str(e)
        缓存元素 = [e.to_dict() for e in 元素]
        for d in 缓存元素:
            if len(d["text"]) > 40:
                d["text"] = d["text"][:40] + "…"
        return _文本({"meta": meta, "elements": 缓存元素})
    except Exception as e:
        return _文本({"ok": False, "error": "scan_image 失败: %s" % e})


@mcp.tool()
def activate_window(window: str, wait: float = 0.4) -> str:
    """按标题子串激活（置前）窗口，返回窗口名与屏幕矩形。

    Args:
        window: 窗口标题子串（如 "计算器"）。
        wait: 置前后等待秒数（默认 0.4，截图/点击前可调大）。
    """
    try:
        wc, rect = uia_engine.activate_window(window, wait=wait)
        return _文本({"ok": True, "action": "activate", "window": wc.Name,
                     "rect": [int(rect.left), int(rect.top),
                              int(rect.right), int(rect.bottom)]})
    except Exception as e:
        return _文本({"ok": False, "action": "activate", "window": window,
                     "error": str(e)})


def _动作结果(动作, 元素id=None, 结果=None, 元素=None):
    return _文本({
        "ok": bool(结果.get("ok")),
        "action": 动作,
        **({"id": 元素id} if 元素id is not None else {}),
        **({"method": 结果.get("method")} if 结果.get("method") else {}),
        **({"element_type": 元素.type, "text": 元素.text[:50], "source": 元素.source} if 元素 else {}),
        **({"error": 结果["error"]} if 结果.get("error") else {}),
    })


def _动作差分(fn):
    """六动作工具的差分登记装饰器（收台机制·登记段，设计页 §3）。

    动作前 EnumWindows 快照 → 动作 → 差分，新增窗自动登记 action_diff
    （advisory：只报不自动关），返回体捎带 windows_opened。代价=每动作
    一次 EnumWindows（毫秒级）。
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        前 = cleanup.snapshot()
        r = fn(*args, **kwargs)
        try:
            新增 = cleanup.diff_register(前)
            if 新增 and isinstance(r, str):
                d = json.loads(r)
                d["windows_opened"] = [{"hwnd": w["hwnd"],
                                        "title": (w["title"] or "")[:60],
                                        "pid": w["pid"]} for w in 新增]
                r = json.dumps(d, ensure_ascii=False)
        except Exception:
            pass
        return r
    return wrapper


@mcp.tool()
def window_stack(limit: int = 40, occlude_hwnd: int = None) -> str:
    """窗口 Z 序全景（堆叠状态感知）——哪个窗在前台、目标窗被谁遮挡。

    agent 看到的截图是堆叠合成画面，分辨不出前台窗与遮挡关系；本工具把
    Z 序变成结构化数据。物理点击永远打在最顶层窗口：点被遮挡窗口的坐标
    =点到遮挡物，先查本工具再动手。

    Args:
        limit: 最多返回窗口数（默认 40，按 Z 序从顶层往下）。
        occlude_hwnd: 可选，指定窗口句柄做遮挡量化（窗口内网格采样，
            返回 visible_ratio 可见率 + blockers 遮挡者清单）。
    """
    try:
        结果 = stacking.stack(limit=limit)
        if occlude_hwnd:
            结果["occlusion"] = stacking.occlusion(occlude_hwnd)
        结果["hint"] = ("foreground=当前焦点窗；windows 按 Z 序（首个最顶层）。"
                       "对 foreground=False 且被遮挡的窗：物理点击会点到遮挡物，"
                       "先 activate_window 置前，或用 UIA 控件级/background 通道。")
        return _文本(结果)
    except Exception as e:
        return _文本({"ok": False, "action": "stack", "error": str(e)})


@mcp.tool()
def crop_screen(element_id: int, pad: int = 0, out_path: str = None,
                force_refresh: bool = False) -> str:
    """按元素表编号裁片出图（两级视觉·看清通道）——原分辨率局部图，逐字可读。

    整屏图在模型视觉编码端被摊薄（糊），裁片是小图原生清晰：看不清元素上的
    文字/图标时，用本工具裁该元素的局部原分辨率图再读。裁片从最近一次
    scan_screen 的同源原图裁出（编号与元素表严格同源）；缓存超龄（>60s，
    画面可能已变）自动重抓新图再裁，返回 used="refreshed" 提示。

    Args:
        element_id: scan_screen 返回的元素 id。
        pad: 裁片四周留白像素（默认 0=纯 bbox）。
        out_path: 裁片输出路径（缺省存 mcp_data\\crop_<时间戳>.png）。
        force_refresh: 无视缓存直接重抓当前画面再裁（默认 false）。60s 内的
            缓存裁片可能拍于动作之前——裁片用来核对「动作后画面」时务必传
            true；注意 bbox 仍取同源元素表的坐标，画面大变请先重扫。
    """
    try:
        状态 = lastshot.status()
        if not 状态:
            raise RuntimeError("无最近抓图缓存（请先 scan_screen）")
        表路径 = 状态.get("elements_json")
        if not 表路径 or not os.path.isfile(表路径):
            raise RuntimeError("最近抓图无同源元素表（snap 不带元素表）——请先 scan_screen 再 crop_screen")
        目标 = None
        with open(表路径, "r", encoding="utf-8") as f:
            for d in json.load(f).get("elements", []):
                if d.get("id") == element_id:
                    目标 = d
                    break
        if 目标 is None:
            raise ValueError("同源元素表中无编号 %s（请重新 scan_screen 刷新编号）" % element_id)
        路径, 信息 = lastshot.crop_element(目标, pad=pad, out_path=out_path,
                                         force_refresh=force_refresh)
        return _文本({"ok": True, "action": "crop", "id": element_id,
                     "text": (目标.get("text") or "")[:50],
                     "element_type": 目标.get("type"),
                     "path": 路径, **信息,
                     "hint": "用读图工具打开 path 查看——原分辨率裁片，逐字可读。"})
    except Exception as e:
        return _文本({"ok": False, "action": "crop", "id": element_id, "error": str(e)})


@mcp.tool()
def crop_xy(x1: int, y1: int, x2: int, y2: int, pad: int = 0,
            out_path: str = None, force_refresh: bool = False) -> str:
    """按屏幕物理坐标区域裁片出图（看清通道）——元素表漏识目标的兜底。

    坐标系与元素表/click_xy 完全同源（物理像素，DPI 感知）；从最近一次
    scan/snap 的原图裁出原分辨率局部图。缓存超龄自动重抓新图（返回
    used="refreshed"），防「拿旧图裁新画面」错位。

    Args:
        x1: 区域左上角 X。y1: 区域左上角 Y。
        x2: 区域右下角 X。y2: 区域右下角 Y。
        pad: 裁片四周留白像素（默认 0）。
        out_path: 裁片输出路径（缺省存 mcp_data\\crop_xy_<时间戳>.png）。
        force_refresh: 无视缓存直接重抓当前画面再裁（默认 false）。60s 内的
            缓存裁片可能拍于动作之前——核对动作后的画面时务必传 true；
            返回的 shot_time 是图的实际拍摄时间，可据此核对。
    """
    try:
        路径, 信息 = lastshot.crop_xy(x1, y1, x2, y2, pad=pad, out_path=out_path,
                                    force_refresh=force_refresh)
        return _文本({"ok": True, "action": "cropxy", "path": 路径, **信息,
                     "hint": "用读图工具打开 path 查看——原分辨率裁片，逐字可读。"})
    except Exception as e:
        return _文本({"ok": False, "action": "cropxy", "error": str(e)})


@mcp.tool()
def book_find(query: str = None, domain: str = None, element_type: str = None,
              limit: int = 30, include_legacy: bool = False,
              image: str = None) -> str:
    """检索元素书库（两级视觉·文本通道）——按名查 bbox，直达目标不重扫。

    书库由 scan_screen 自动入库（应用域×元素类型两级分类，manual/<域>/）。
    文本查询命中 → 直接拿 bbox（屏幕坐标）→ click_xy 直点或 crop_xy 裁片。
    bbox 是「最后入库时」的屏幕坐标：窗口若已挪动/关闭，先 activate_window
    置前并重扫核对。query/domain 均子串匹配。
    以图查库（F3·BOOKVEC=1）：image=裁片 png 路径 → SigLIP 向量余弦 top-K，
    纯图标/无文字元素（游戏自绘 UI、专业软件工具栏）查库的主通道；命中条目
    附 cos 相似度（降序），bbox 语义同文本通道。

    Args:
        query: 元素名子串（如 "设置"、"7"）；缺省返回全部（受 limit 限）。
        domain: 应用域子串（如 "计算器"）。
        element_type: 元素类型精确匹配（button/edit/document/pane…）。
        limit: 最多返回条数（默认 30）。
        include_legacy: 是否含 v1 旧条目（默认 False——legacy 坐标不可信，
            重扫即归档出主库；True=旧行为。被排除数见 legacy_excluded）。
        image: 图片路径（如 crop_xy 产物）——给了就走以图查库（需向量库
            非空：BOOKVEC=1 扫描入库或 booksweep --embed 回填）。
    """
    try:
        if image:
            from sp import bookvec
            条目 = bookvec.以图查库(image, domain=domain, 类型=element_type,
                                  limit=limit)
            return _文本({"ok": True, "action": "book_find", "mode": "image",
                         "image": image, "count": len(条目), "entries": 条目,
                         "hint": "余弦降序 top-K，cos=查询图↔条目裁片相似度；"
                                 "bbox=最后入库时的屏幕坐标，可 click_xy/crop_xy 直达。"})
        if not query and not domain and not element_type:
            域 = booklib.域清单()
            return _文本({"ok": True, "action": "book_find", "domains": 域,
                         "hint": "未指定 query——返回域清单。带 query/domain 查具体条目，"
                                 "命中条目附 bbox（屏幕坐标）与 crop 裁片路径。"})
        条目, 排除, 过期排除 = booklib.查找明细(query=query, domain=domain,
                                           类型=element_type, limit=limit,
                                           include_legacy=include_legacy)
        return _文本({"ok": True, "action": "book_find", "count": len(条目),
                     "legacy_excluded": 排除,
                     "expired_excluded": 过期排除,
                     "entries": 条目,
                     "hint": "bbox=最后入库时的屏幕坐标，可 click_xy/crop_xy 直达；"
                             "窗口已挪动或目标时效要求高时仍以重扫为准。"
                             "legacy 旧条目默认排除（include_legacy=true 查旧账）；"
                             "TTL 到期条目一律排除（booksweep --expired 可清）。"})
    except Exception as e:
        return _文本({"ok": False, "action": "book_find", "error": str(e)})


@mcp.tool()
@_动作差分
def click_element(element_id: int, physical: bool = False, precise: bool = False,
                  expect: Union[dict, str, None] = None,
                  check_top: bool = True) -> str:
    """按 scan_screen 元素表中的编号点击元素。

    物理点击默认漂移落点（高斯偏移+移动拖尾，不过度精准），precise=True 恢复
    像素级精准；UIA 控件级调用不经鼠标，不受漂移影响。

    Args:
        element_id: scan_screen 返回的元素 id。
        physical: 跳过 UIA 控件级调用，直接物理点击坐标（控件级调用无效时用）。
        precise: 像素级精准落点（默认 False=漂移点击）。
        expect: 可选业务生效判定 {find: "文本子串", mode: "appear"|"disappear",
            timeout: 秒(默认10), interval: 毫秒(默认800)}（或其 JSON 字符串）。
            点击发出后轮询扫描该元素所属窗口，条件成立才算成功返回；超时如实
            返回未命中并附最后一次扫描证据（expect.evidence）。OCR/视觉来源
            元素无 hwnd 时需在 expect 里指定 window 或 hwnd。默认不等待。
        check_top: 落点归窗核验（默认开）——物理点击前核验落点最顶层窗口
            是不是目标窗，被遮挡就拒绝点击并返回 top_window（谁挡着），防
            「点了也不知道点到谁」。确需强点设 False。
    """
    try:
        el = _查元素(element_id)
        结果 = executor.click(el, force_physical=physical, precise=precise,
                              check_top=check_top)
        d = json.loads(_动作结果("click", element_id, 结果, el))
        if isinstance(结果.get("top_window"), dict):
            d["top_window"] = 结果["top_window"]
        if 结果.get("hit_window"):
            d["hit_window"] = 结果["hit_window"]
        d = waiter.apply_expect(d, expect, 默认hwnd=int(getattr(el, "hwnd", 0) or 0))
        return _文本(d)
    except Exception as e:
        return _文本({"ok": False, "action": "click", "id": element_id, "error": str(e)})


@mcp.tool()
@_动作差分
def click_safe(element_id: int, expect: Union[dict, str, None] = None,
               physical: bool = False, precise: bool = False) -> str:
    """click_element 的安全版：业务生效判定（expect）必填 + 落点归窗核验恒开。

    点完必须用业务证据（文本出现/消失）判定成功与否，超时如实失败——
    「点击执行成功 ≠ 业务生效」的强约束通道；被遮挡一律拒绝点击
    （check_top 恒为 true，无逃生口）。不需要业务判定的常规点击用 click_element。

    Args:
        element_id: scan_screen 返回的元素 id。
        expect: 必填业务生效判定 {find: "文本子串", mode: "appear"|"disappear",
            timeout: 秒(默认10), interval: 毫秒(默认800)}（或其 JSON 字符串）。
            点击发出后轮询扫描该元素所属窗口，条件成立才算成功返回；超时如实
            返回未命中并附最后一次扫描证据（expect.evidence）。OCR/视觉来源
            元素无 hwnd 时需在 expect 里指定 window 或 hwnd。
        physical: 跳过 UIA 控件级调用，直接物理点击坐标。
        precise: 像素级精准落点（默认 False=漂移点击）。
    """
    if expect is None or (isinstance(expect, str) and not expect.strip()):
        return _文本({"ok": False, "action": "click_safe", "id": element_id,
                     "error": "click_safe 要求必填 expect 业务生效判定（点完必须用"
                              "业务证据判定成败）；不需要业务判定请改用 click_element"})
    try:
        el = _查元素(element_id)
        结果 = executor.click(el, force_physical=physical, precise=precise,
                              check_top=True)
        d = json.loads(_动作结果("click_safe", element_id, 结果, el))
        if isinstance(结果.get("top_window"), dict):
            d["top_window"] = 结果["top_window"]
        if 结果.get("hit_window"):
            d["hit_window"] = 结果["hit_window"]
        d = waiter.apply_expect(d, expect, 默认hwnd=int(getattr(el, "hwnd", 0) or 0))
        return _文本(d)
    except Exception as e:
        return _文本({"ok": False, "action": "click_safe", "id": element_id, "error": str(e)})


@mcp.tool()
@_动作差分
def click_xy(x: int, y: int, precise: bool = False,
             expect: Union[dict, str, None] = None,
             top_must: str = None) -> str:
    """按屏幕物理坐标直接点击——元素表兜底通道。

    用于元素表漏识的目标（如游戏自绘 UI 的「×」关闭按钮）：从 snap_screen
    截图或 scan 返回的标注图（annotated_png）上读出目标像素坐标后直点。
    坐标系与元素表完全一致（物理像素，DPI 感知；窗口扫描返回的 bbox 已含
    窗口偏移，同坐标系可直接用——标注图上/左边缘已印屏幕坐标刻度轴，可直接
    读数）。默认漂移落点，precise=True 恢复像素级精准。

    堆叠感知：坐标点击命中的是「最顶层窗口」——结果一律附 hit_window
    （落点归根窗：hwnd+标题），点完即知打到了谁；top_must 指定标题子串时
    事前核验，不符则拒绝点击（ok=False + hit_window 说明落点处是谁），
    防止拿后台抓窗图的坐标点到前台遮挡物上。

    Args:
        x: 目标物理像素 X 坐标。
        y: 目标物理像素 Y 坐标。
        precise: 像素级精准落点（默认 False=漂移点击）。
        expect: 可选业务生效判定 {find: "文本子串", mode: "appear"|"disappear",
            timeout: 秒(默认10), interval: 毫秒(默认800)}（或其 JSON 字符串）。
            点击发出后轮询扫描等待，条件成立才算成功返回；超时如实返回未命中
            并附证据（expect.evidence）。坐标点击无所属窗口，expect 必须指定
            window（标题子串）或 hwnd，否则拒判。默认不等待。
        top_must: 落点最顶层窗口标题必须含此子串才点击（事前拦截错窗点击）。
    """
    try:
        结果 = executor.click_xy(x, y, precise=precise, top_must=top_must)
        d = {"action": "clickxy", **结果}
        d = waiter.apply_expect(d, expect)
        return _文本(d)
    except Exception as e:
        return _文本({"ok": False, "action": "clickxy", "error": str(e)})


@mcp.tool()
@_动作差分
def type_text(element_id: int, text: str, precise: bool = False,
              expect: Union[dict, str, None] = None) -> str:
    """按编号向元素输入文本（UIA 编辑框走 SetValue，中文走剪贴板粘贴，均安全）。

    Args:
        element_id: 目标输入框元素的 id（scan 结果里 type 为 edit/document 的元素）。
        text: 要输入的文本，支持中文与换行。
        precise: 聚焦点击用像素级精准落点（默认 False=漂移点击聚焦）。
        expect: 可选业务生效判定 {find: "文本子串", mode: "appear"|"disappear",
            timeout: 秒(默认10), interval: 毫秒(默认800)}（或其 JSON 字符串）。
            输入发出后轮询扫描该元素所属窗口，条件成立才算成功返回；超时如实
            返回未命中并附证据（expect.evidence）。OCR/视觉来源元素无 hwnd 时
            需在 expect 里指定 window 或 hwnd。默认不等待。
    """
    try:
        el = _查元素(element_id)
        结果 = executor.type_text(el, text, precise=precise)
        d = json.loads(_动作结果("type", element_id, 结果, el))
        d = waiter.apply_expect(d, expect, 默认hwnd=int(getattr(el, "hwnd", 0) or 0))
        return _文本(d)
    except Exception as e:
        return _文本({"ok": False, "action": "type", "id": element_id, "error": str(e)})


@mcp.tool()
@_动作差分
def press_key(key: str) -> str:
    """按键或组合键。支持中文键名：回车/esc/tab/空格/退格/删除/上下左右/上翻页/下翻页，
    组合用 + 连接（如 ctrl+s、ctrl+shift+esc）。

    Args:
        key: 键名，如 "回车"、"esc"、"ctrl+s"。
    """
    try:
        结果 = executor.press_key(key)
        return _动作结果("key", 结果=结果)
    except Exception as e:
        return _文本({"ok": False, "action": "key", "error": str(e)})


@mcp.tool()
@_动作差分
def scroll_screen(direction: str, pixels: int = 600) -> str:
    """在当前鼠标位置滚动页面。

    Args:
        direction: "up"/"down"（或 "上"/"下"）。
        pixels: 滚动像素量（默认 600）。
    """
    try:
        结果 = executor.scroll(direction, pixels)
        return _动作结果("scroll", 结果=结果)
    except Exception as e:
        return _文本({"ok": False, "action": "scroll", "error": str(e)})


@mcp.tool()
def snap_screen(window: str = None, out_path: str = None, background: bool = False,
                hwnd: int = None) -> str:
    """截屏保存，返回图片绝对路径（可直接读图查看屏幕现状）。

    Args:
        window: 窗口标题子串（缺省全屏；窗口模式自动置前）。
        out_path: 输出路径（缺省存 mcp_data\\snap_<时间戳>.png）。
        background: 后台模式——不置前窗口，PrintWindow 抓窗（被遮挡也真实）。
        hwnd: 窗口句柄直定位（优先于 window 参数）。
    """
    try:
        sw, sh = capture.screen_size()
        win_name = None
        抓图偏移 = [0, 0]
        抓图句柄 = 0
        if hwnd or window:
            if hwnd:
                wc = uia_engine.control_from_hwnd(hwnd)
            else:
                wc = uia_engine.find_window_control(window)
            win_name = wc.Name
            抓图句柄 = int(hwnd or getattr(wc, "NativeWindowHandle", 0) or 0)
            if background:
                h2 = 抓图句柄
                if not h2:
                    raise RuntimeError("后台模式未取到窗口句柄（无 HWND 窗口请用普通模式）")
                img, rect, 回退 = capture.shot_window_hwnd(h2)
                抓图偏移 = [int(rect[0]), int(rect[1])]
            else:
                _前置窗口(wc)
                rect = wc.BoundingRectangle
                l, t = max(0, int(rect.left)), max(0, int(rect.top))
                r, b = min(sw, int(rect.right)), min(sh, int(rect.bottom))
                img = capture.shot_region(l, t, r, b)
                抓图偏移 = [l, t]
                回退 = False
        else:
            img = capture.shot_full()
            回退 = False
        if out_path:
            输出 = os.path.abspath(out_path)
        else:
            输出 = os.path.join(MCP_DATA, "snap_%s.png" % time.strftime("%H%M%S"))
        输出目录 = os.path.dirname(输出)
        if 输出目录 and not os.path.isdir(输出目录):
            os.makedirs(输出目录, exist_ok=True)
        capture.save_image(img, 输出)
        # 两级视觉·裁片通道地基：snap 也登记最近抓图（无元素表，crop_screen
        # 不可用但 crop_xy 可用）
        try:
            lastshot.register(img, {"source": "snap", "window": win_name,
                                    "hwnd": 抓图句柄, "offset": 抓图偏移},
                              elements_json=None, shot_path=输出)
        except Exception:
            pass
        h, w = img.shape[:2]
        return _文本({"ok": True, "action": "snap", "path": 输出, "size": [w, h],
                     "window": win_name,
                     **({"bg_fallback": True} if 回退 else {}),
                     "hint": "用读图工具直接打开 path 查看屏幕现状。"})
    except Exception as e:
        return _文本({"ok": False, "action": "snap", "error": str(e)})


# ---------------- 时间维度四件（2026-11-01 B1 转正）----------------

def _区域(region):
    """region 参数：'x1,y1,x2,y2' 字符串或 4 元数组（屏幕物理坐标）；None=全窗。"""
    if not region:
        return None
    if isinstance(region, str):
        parts = [p for p in region.replace("，", ",").split(",") if p.strip()]
        if len(parts) != 4:
            raise ValueError("region 需为 x1,y1,x2,y2 四个数字")
        return [float(p) for p in parts]
    return [float(v) for v in region]


def _工具动作(action):
    """action 参数：JSON 字符串或 dict（单动作），或动作数组。"""
    if action is None:
        return None
    if isinstance(action, str):
        try:
            项 = json.loads(action)
        except Exception as e:
            raise ValueError("action 不是合法 JSON：%s" % e)
    else:
        项 = action
    if isinstance(项, dict):
        return [项]
    if isinstance(项, list):
        return 项
    raise ValueError("action 须为 JSON 对象或对象数组")


@mcp.tool()
def watch_burst(window: str = None, hwnd: int = None, seconds: float = 3.0,
                interval: float = 0.2, region: str = None,
                action: Union[dict, str, None] = None, rebase: bool = True,
                mode: str = "printwindow", save_dir: str = None) -> str:
    """连拍窗：动作后 0~seconds 秒逐帧差分，抓 toast/飘字等瞬时反馈。

    时间维度四件之一（POC v2 口径，真靶 10/10）。适合「点了一下，闪过的
    提示说了什么/到底有没有闪」这类瞬态判断——scan 抓不到瞬时状态，它专治。

    Args:
        window: 窗口标题子串（与 hwnd 二选一）。
        hwnd: 窗口句柄直定位（优先于 window）。
        seconds: 观察窗秒数（默认 3）。
        interval: 帧间隔秒（默认 0.2）。
        region: 观察区域 'x1,y1,x2,y2'（屏幕物理坐标，与元素表同源；缺省全窗）。
        action: 前置动作，JSON 或对象：{"kind":"click","x":..,"y":..} /
            {"kind":"key","key":"enter"} / {"kind":"paste","text":".."} /
            {"kind":"wait","seconds":0.3}；也接受动作数组（顺序执行）。
        rebase: 动作后重抓基线（默认 True：吸收点击浮雕等动作自身视觉反馈）。
        mode: 抓帧通道，printwindow（默认，后台抓窗）| screen（挂死/前台场景）。
        save_dir: 命中帧 PNG 保存目录（缺省不存）。

    返回：帧账 ledger（每帧 t/ratio/ratio_base/boxes）、hit_frames、first_hit_t、
    max_ratio。ratio=相对上一帧的变化占比，ratio_base=相对基线。
    """
    try:
        return _文本(watch.watch_burst(
            hwnd=hwnd, window=window, seconds=seconds, interval=interval,
            region=_区域(region), actions=_工具动作(action), rebase=rebase,
            mode=mode, save_dir=save_dir))
    except Exception as e:
        return _文本({"ok": False, "action": "watch_burst", "error": str(e)})


@mcp.tool()
def verify_typing(window: str = None, hwnd: int = None, wait: float = 1.5,
                  interval: float = 0.25, region: str = None,
                  action: Union[dict, str, None] = None, ocr_check: bool = False,
                  mode: str = "printwindow") -> str:
    """一隐一显：动作前抓基线，动作后短窗看 region 是否出现变化（判定输入生效）。

    时间维度四件之二（POC v3 口径，真靶 20/20）。与 watch_burst 的区别：
    基线在动作**前**——动作本身不算反馈，只有指定区域内容真变了才算「显示」。
    典型用法：action 里点输入框→Ctrl+A→粘贴一串，region 给回显区 bbox，
    changed=true 即输入回显成立（自绘输入框/无文字属性控件OCR也靠它）。

    Args:
        window/hwnd: 目标窗口。
        wait: 观察秒数（默认 1.5）。
        interval: 帧间隔秒（默认 0.25）。
        region: 回显区域 'x1,y1,x2,y2'（屏幕坐标，建议必给，别给全窗）。
        action: 前置动作（格式同 watch_burst），也接受动作数组。
        ocr_check: 附基线与末帧的 region OCR 文本（慢 1~3 秒；默认关）。
        mode: printwindow | screen。

    返回：changed（区域是否出现变化）、first_change_t、max_ratio、ledger，
    ocr_check=true 时附 ocr.before/after 文本。
    """
    try:
        return _文本(watch.verify_typing(
            hwnd=hwnd, window=window, wait=wait, interval=interval,
            region=_区域(region), actions=_工具动作(action),
            ocr_check=ocr_check, mode=mode))
    except Exception as e:
        return _文本({"ok": False, "action": "verify_typing", "error": str(e)})


@mcp.tool()
def probe_hang(window: str = None, hwnd: int = None, key: str = "esc",
               wait: float = 2.0, resend_after: float = 0.7,
               focus: bool = True) -> str:
    """发键短窗探测窗口卡死：内容有变化=活着，零变化=判卡死（屏幕通道）。

    时间维度四件之三（POC v4 口径，真靶 10/10 误差 0）。**必须走屏幕拷贝**：
    挂起窗口不处理 WM_PRINT，PrintWindow 会永久阻塞。因此要求窗口在前台
    无遮挡——focus=True 先置前；置前失败或可见率<0.95 会如实拒绝判定。
    按键是全局键盘事件，只发到前台窗口。

    Args:
        window/hwnd: 目标窗口。
        key: 探测按键（默认 esc）。
        wait: 观察秒数（默认 2.0）。
        resend_after: 首轮无变化后补发一发的时刻秒（0=不补发，默认 0.7）。
        focus: 是否先置前（默认 True；False 时要求窗口已在前台）。

    返回：hang（是否判卡死）、changed、max_ratio、is_hung_api（旁证，恢复后
    可滞后）、occlusion、ledger。
    """
    try:
        return _文本(watch.probe_hang(
            hwnd=hwnd, window=window, key=key, wait=wait,
            resend_after=resend_after, focus=focus))
    except Exception as e:
        return _文本({"ok": False, "action": "probe_hang", "error": str(e)})


@mcp.tool()
def split_static_dynamic(window: str = None, hwnd: int = None, frames: int = 8,
                         interval: float = 0.5, std_thresh: float = 12.0,
                         moving_min_dist: float = 15.0, verify_ocr: bool = False,
                         out_dir: str = None, mode: str = "printwindow") -> str:
    """多帧抠底动静态分层：UI 底图 + 动态掩码 + 实体/动画层轨迹。

    时间维度四件之四（像素抠UI POC 口径，真靶 UI 保留 98.4%、实体误收 0%、
    稳定移动轨迹 9 条）。静止=UI / 变化=实体：多帧逐像素中位数抠出 UI 底图
    （偶发动态物被滤掉、静止 UI 全保留），时间轴标准差抠动态掩码，跨帧跟踪
    后按净位移分层——净位移大=实体移动，净位移≈0=原位动画层（呼吸灯/跳秒）。

    Args:
        window/hwnd: 目标窗口。
        frames: 采样帧数（默认 8，≥2）。
        interval: 帧间隔秒（默认 0.5）。
        std_thresh: 动态掩码灰度标准差阈值（默认 12）。
        moving_min_dist: 净位移≥此值判实体（默认 15px）。
        verify_ocr: 附 UI 文本保留率/稳定框误收量化（POC 判据①，慢 2~4 秒）。
        out_dir: 产物目录（默认 mcp_data/watch_<时间>）。
        mode: printwindow（默认后台抓窗）| screen。

    返回：artifacts（ui_base/dyn_mask/annotated 绝对路径）、movers（实体轨迹，
    net_dist 降序）、anims 数、dyn_ratio；verify_ocr=true 时附 ocr 量化账。
    """
    try:
        return _文本(watch.split_static_dynamic(
            hwnd=hwnd, window=window, frames=frames, interval=interval,
            std_thresh=std_thresh, moving_min_dist=moving_min_dist,
            verify_ocr=verify_ocr, out_dir=out_dir, mode=mode))
    except Exception as e:
        return _文本({"ok": False, "action": "split_static_dynamic", "error": str(e)})



# ---------------- 收台机制两件（2026-11-06 登记-清理两段式）----------------

@mcp.tool()
def register_window(window: str, close_candidate: bool = True) -> str:
    """登记窗口进会话账本（收台机制·显式登记）——收尾 cleanup_windows 会温和关它。

    用于自己拉起的应用窗（subprocess/双击图标开的）：登记后任务收尾或会话
    结束（stdio 断开）时自动 WM_CLOSE 温和关闭；不登记的窗永远不碰。
    按标题子串匹配，命中多窗全登。

    Args:
        window: 窗口标题子串（如 "记事本"）。
        close_candidate: true（默认）=自动关候选；false=只入账不自动关。
    """
    try:
        条目 = cleanup.register_by_title(window, source="explicit",
                                         close_candidate=close_candidate)
        return _文本({"ok": True, "action": "register_window",
                     "query": window, "registered": 条目,
                     "count": len(条目),
                     "hint": ("登记 %d 窗：会话收尾将 WM_CLOSE 温和关闭"
                              "（白名单/脏窗保护恒生效）" % len(条目))
                     if 条目 else "未命中任何可见窗（先 scan_screen 核对标题）"})
    except Exception as e:
        return _文本({"ok": False, "action": "register_window",
                     "error": str(e)})


@mcp.tool()
def cleanup_windows(targets: list = None, force: bool = False) -> str:
    """收台清理（任务收尾调用）——按会话账本温和关窗，全程 WM_CLOSE 无 taskkill。

    清理顺序六门（防误伤，详=sp/cleanup.py）：身份三验（hwnd+pid）→ 白名单
    永免（explorer/任务管理器/ZCode 自身等）→ 只认本会话登记的窗 → 脏内容
    保护（「已修改/未保存」只报不关）→ WM_CLOSE×3 → 清不掉写残窗报告不强杀。

    Args:
        targets: 缺省 None=清本会话账本内全部自动关候选窗（launch/explicit
            登记的）；也可传清单指定窗（int hwnd 或 str 标题子串混合）——
            指定窗不受「只认本会话」限制，但白名单与身份验证恒生效。
        force: true=跳过脏内容保护（对「已修改」窗也关；agent 判明后显式
            决定用）。白名单窗 force 也不碰。

    返回 closed（已关）/survivors（WM_CLOSE×3 仍在，已写残窗报告）/
    dirty_skipped（脏窗只报不关）/skipped_whitelist（白名单永免）。
    """
    try:
        结果 = cleanup.cleanup_windows(targets=targets, force=force)
        结果["action"] = "cleanup_windows"
        if 结果.get("ok"):
            结果["hint"] = ("任务收尾建议：登记过的窗已温和关闭；survivors 已写"
                           "残窗报告不强杀；dirty_skipped 是有未保存内容的窗，"
                           "决定权在你（可 force=true 或自行保存后重清）。")
        return _文本(结果)
    except Exception as e:
        return _文本({"ok": False, "action": "cleanup_windows", "error": str(e)})


if __name__ == "__main__":
    # 会话收口双保险（设计页 §2：stdio 断=会话收口，残窗清理靠机制）：
    # finally 接 stdio 断开的正常退出；atexit 兜 interpretor 退出路径；幂等。
    import atexit
    atexit.register(cleanup.session_exit_cleanup)
    try:
        mcp.run(transport="stdio")
    finally:
        cleanup.session_exit_cleanup()

