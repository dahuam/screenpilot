# -*- coding: utf-8 -*-
"""扫描管线：截图 → 三引擎并行（UIA / OCR / 视觉）→ 融合 → 统一编号。

CLI 与 MCP server 共用本管线，保证两端行为一致。

并行设计（提速关键，此前串行 4.5~8s）：
- 线程 A（UIA）：独立 COM 线程——线程内 CoInitialize，窗口查找/置前/取矩形/
  遍历控件树全部在同一线程完成（STA 的 COM 接口指针跨线程直接用会
  RPC_E_WRONG_THREAD，故绝不把主线程创建的控件对象传进来用）；
- 主线程：截图（窗口模式等线程 A 交出矩形后截）；
- 线程 B（OCR + 视觉）：拿到截图即启动，与 UIA 遍历并行；
  onnxruntime / OpenCV 推理均释放 GIL，线程并行真实有效。

窗口矩形通过 Event + dict 从线程 A 交给主线程（纯 Python 数据，安全）。

后台模式（background=True）：窗口定位后不置前，截图走 Win32 PrintWindow
（窗口被遮挡也能抓到自身画面）；黑帧/失败回退屏幕拷贝并在 meta 标注。

编号稳定性：传入 tracker_state（上一帧快照）时用 IdTracker 做帧间匹配保号，
新元素追加编号；返回值携带新快照供调用方落盘/续用。
"""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import booklib, capture, fuse, ocr_engine, privacy, stacking, template_engine, tracker, uia_engine, visual_engine

# 形状通道（☐☑○◉滑块）的 UIA 密度闸：它的价值场是 UIA 盲区（游戏设置页/
# 自绘界面），而 UIA 富集窗口（explorer/Edge 等 ≥60 个 UIA 元素）里小方框/
# 小圆环图标与表单控件形态学同构、纯像素分不开，形状候选在那里纯误报源
# （bench 实测 explorer 一屏 18 个）。富集窗口静音形状语义元素；模板通道
# （像素级证据、实测零误报）不受限。scan_image 无 UIA → 永远活跃。
_形状通道UIA上限 = 60

# 形状通道语义名全集（密度闸/标签伴生闸共用的处置对象）
_形状语义集 = ("☐", "☑", "○", "◉", "滑块")

# ---- 增量扫描（2026-10-29 施工单施工二）----
# 上一帧缓存落盘（CLI 每次是独立进程，进程内缓存不跨调用；MCP 进程常驻也
# 统一走落盘，两端行为一致）。仅 incremental=True 的 scan 读写（普通 scan
# 不背 PNG 编码开销），首帧 incremental 无上帧自动退全量并存基线。
_数据目录 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp_data")
_增量状态路径 = os.path.join(_数据目录, "incremental_last.json")
_增量图路径 = os.path.join(_数据目录, "incremental_last.png")
# 差分参数（实测定档）：降采样宽 480 对全屏约 5 倍降采样，阈值 25 过抖动，
# 膨胀核 9 合邻碎变化，矩形外扩 16px 缓解裁块边界截断
_差分降采样宽 = 480
_差分阈值 = 25
_差分膨胀核 = 9
_差分外扩 = 16
_变化占比全量线 = 0.6   # 变化面积占比超此值直接全量（增量已无意义）

# 平移检测（2026-10-31 第三单·滚动复用）：滚动=内容整体平移，朴素差分会把
# 整窗判成变化（滚动靶实测 93~100% 超 60% 全量线被退全量）——增量恰在最
# 高频的滚动场景形同虚设。先搜最佳垂直平移量，平移成立则按平移对齐后差分，
# 变化区域只剩「新露出的条带 + 对齐后残余差异」，OCR 只扫这些小块。
_平移搜索范围 = 480     # 原像素，垂直滚动步长搜索上限（超出=大幅翻页，退全量合理）
_平移噪声 = 14          # 平移对齐后灰度均值差低于此 = 对齐贴合
_平移判定比 = 0.55      # 最佳 dy 均值差 < 零平移均值差 × 此值 = 明显平移关系
_平移最小位移 = 4       # 小图上 <4 行（约 10px）的位移交给普通差分（抖动场景）
_平移静止线 = 3.0       # 零平移均值差低于此 = 画面静止（截图噪声级），不搜平移
# 内容位移保守线（stale 兜底）：平移未检出但内容变化占比过此值 → 内容大概率
# 移动/翻动过，书库旧坐标存疑，同样标 stale（宁保守勿错坐标）
_内容位移线 = 0.25

# 智能增量窗口（2026-11-01 A2）：incremental=None 时，同桶（同窗口/同全屏）
# 且距上帧 ≤ 此秒数自动走增量。agent 最常见用法=连续扫同一窗口。
_智能增量窗口秒 = 60


def _小图灰(图):
    """降采样灰度小图（差分/平移共用预处理）：INTER_AREA 区域平均抗文字
    笔画混叠（INTER_LINEAR 对 1px 笔画会产生斑马纹假变化），3x3 高斯再平
    掉滚动亚像素重排（ClearType 对非整数滚动位重光栅化的边缘差）。"""
    import cv2
    h, w = 图.shape[:2]
    小w = min(_差分降采样宽, w)
    小h = max(1, int(h * (小w / float(w))))
    小 = cv2.resize(图, (小w, 小h), interpolation=cv2.INTER_AREA)
    灰 = cv2.cvtColor(小, cv2.COLOR_BGR2GRAY)
    灰 = cv2.GaussianBlur(灰, (3, 3), 0)
    return 灰


def _增量读(桶key, 区域):
    """读上一帧增量缓存：桶匹配+区域完全一致才有效，返回 (上帧图, 上帧元素快照) 或 None。"""
    if not os.path.isfile(_增量状态路径) or not os.path.isfile(_增量图路径):
        return None
    try:
        with open(_增量状态路径, "r", encoding="utf-8") as f:
            状态 = json.load(f)
        if 状态.get("key") != 桶key or 状态.get("region") != list(区域):
            return None
        img = capture.load_image(_增量图路径)
        if img.shape != (区域[3] - 区域[1], 区域[2] - 区域[0], 3):
            return None
        return img, 状态.get("elements") or []
    except Exception:
        return None


def _上帧时刻(桶key, 区域=None):
    """读上帧缓存时间戳（epoch 秒）：key 匹配才有效，否则 None。

    区域给定（全屏模式）时连 region 一起匹配；区域 None（窗口模式智能预判）
    只看 key+时间——精确 region 匹配由截图后的 _增量读 把关，不匹配自动退
    全量，预判宽松不会错走增量。
    """
    if not os.path.isfile(_增量状态路径):
        return None
    try:
        with open(_增量状态路径, "r", encoding="utf-8") as f:
            状态 = json.load(f)
        if 状态.get("key") != 桶key:
            return None
        if 区域 is not None and 状态.get("region") != list(区域):
            return None
        return time.mktime(time.strptime(状态.get("time", ""),
                                         "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


def _增量存(桶key, 区域, img, 元素):
    """落盘本帧为下一帧的增量基线（原图 PNG + 元素快照 JSON）。失败不碍主流程。"""
    try:
        os.makedirs(_数据目录, exist_ok=True)
        capture.save_image(img, _增量图路径)
        状态 = {
            "key": 桶key,
            "region": list(区域),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elements": [{"id": e.id, "type": e.type, "text": e.text,
                          "bbox": list(e.bbox), "source": e.source,
                          "conf": float(e.conf), "hwnd": int(e.hwnd or 0),
                          "clickable": bool(e.clickable)} for e in 元素],
        }
        with open(_增量状态路径, "w", encoding="utf-8") as f:
            json.dump(状态, f, ensure_ascii=False)
    except Exception:
        pass


def _差分矩形(上帧图, 当前图, 平移dy=0):
    """两帧图降采样灰度差分 → 膨胀 → 变化矩形列表（图内坐标）+ 面积占比。

    平移dy≠0（滚动帧）：先在降采样灰度图上按 dy 垂直切片对齐（上帧滚出区
    与当前帧新露出区不参与对齐差分），变化矩形只剩「对齐后残余差异」；
    新露出条带由调用方按返回的 新露出 区间一并处理（本函数追加为矩形）。
    「元素位置不变但内容变了的区域必须被重扫」由差分天然保证（内容变=像素变）。
    膨胀把相邻碎变化连成块，减少 OCR 小块调用次数。
    """
    import cv2
    import numpy as np
    h, w = 当前图.shape[:2]
    if 上帧图.shape != 当前图.shape:
        return [], 1.0
    a = _小图灰(上帧图)
    b = _小图灰(当前图)
    小h, 小w = a.shape[:2]
    新露出 = None
    if 平移dy:
        d = max(1, min(小h - 1, abs(int(round(平移dy * 小w / float(w))))))
        if d >= 2:   # 对齐后重叠区 <2 行的极端滚动没有增量意义，走普通差分
            if 平移dy > 0:   # 内容上移（视口下滚）：a[d:] 对 b[:小h-d]
                a2, b2 = a[d:, :], b[:小h - d, :]
                新露出 = [0, h - int(round(平移dy)), w, h]
            else:            # 内容下移（视口上滚）：a[:小h-d] 对 b[d:]
                a2, b2 = a[:小h - d, :], b[d:, :]
                新露出 = [0, 0, w, -int(平移dy)]
            a, b = a2, b2
    diff = cv2.absdiff(a, b)
    _, bw = cv2.threshold(diff, _差分阈值, 255, cv2.THRESH_BINARY)
    bw = cv2.dilate(bw, np.ones((_差分膨胀核, _差分膨胀核), np.uint8))
    cnts, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    缩回 = w / float(小w)
    # 平移下移（dy<0）时重叠区从 b 的 y=|d| 行开始，contour 坐标要加偏移
    y偏移 = 0
    if 平移dy and 平移dy < 0 and 新露出:
        y偏移 = -int(平移dy)
    rects = []
    for c in cnts:
        x, y, rw, rh = cv2.boundingRect(c)
        if (rw * rh) * 缩回 * 缩回 < 100:   # <100 原像素面积当噪声忽略
            continue
        rects.append([x, y + y偏移, x + rw, y + y偏移 + rh])
    # 迭代合并相交/邻近（间距 ≤2 格）矩形，直到不动点
    间距 = 3   # 降采样坐标系下的格
    合并了 = True
    while 合并了:
        合并了 = False
        出 = []
        while rects:
            r = rects.pop()
            for i, o in enumerate(出):
                if not (r[2] + 间距 < o[0] or o[2] + 间距 < r[0] or
                        r[3] + 间距 < o[1] or o[3] + 间距 < r[1]):
                    o[0], o[1] = min(r[0], o[0]), min(r[1], o[1])
                    o[2], o[3] = max(r[2], o[2]), max(r[3], o[3])
                    合并了 = True
                    break
            else:
                出.append(r)
        rects = 出
    out = []
    for x1, y1, x2, y2 in rects:
        ox1 = max(0, int(x1 * 缩回) - _差分外扩)
        oy1 = max(0, int(y1 * 缩回) - _差分外扩)
        ox2 = min(w, int(x2 * 缩回) + _差分外扩)
        oy2 = min(h, int(y2 * 缩回) + _差分外扩)
        if ox2 - ox1 >= 2 and oy2 - oy1 >= 2:
            out.append([ox1, oy1, ox2, oy2])
    if 新露出:
        # 新露出条带（滚动卷绕进视口的内容）必须整带重扫——上帧没有它的像素
        ox1, oy1, ox2, oy2 = 新露出
        if ox2 > ox1 and oy2 > oy1 and not any(
                r[0] <= ox1 and r[1] <= oy1 and r[2] >= ox2 and r[3] >= oy2
                for r in out):
            out.append([ox1, oy1, ox2, oy2])
    占比 = sum((r[2] - r[0]) * (r[3] - r[1]) for r in out) / float(w * h)
    return out, 占比


def _平移检测(上帧图, 当前图):
    """垂直平移搜索：上帧/当前帧降采样灰度图上搜最佳 dy（内容平移量）。

    返回 (dy原像素, 最佳均值差, 零平移均值差)；非平移关系返回 None。
    判定：|dy|≥6 小图行 且 最佳差 < 平移噪声 且 最佳差 < 零平移差×判定比
    （两者同时成立才认平移——纯内容替换对任何 dy 都对不齐，不会误判）。
    粗搜步长 3 行 + 峰值 ±3 行细搜，全流程 numpy 切片向量化（~40ms 量级）。
    """
    import numpy as np
    h, w = 当前图.shape[:2]
    if 上帧图.shape != 当前图.shape:
        return None
    a = _小图灰(上帧图).astype(np.int16)
    b = _小图灰(当前图).astype(np.int16)
    小h, 小w = a.shape[:2]
    # 抹平亮度漂移（滚动时页面可能有 hover 高亮/抗锯齿差）：各自去均值
    a = a - int(np.mean(a))
    b = b - int(np.mean(b))
    限 = min(小h // 2, max(1, int(_平移搜索范围 * 小w / float(w))))

    def 差值(dy):
        if dy > 0:
            aa, bb = a[dy:, :], b[:小h - dy, :]
        elif dy < 0:
            aa, bb = a[:小h + dy, :], b[-dy:, :]
        else:
            aa, bb = a, b
        return float(np.mean(np.abs(aa - bb)))

    d0 = 差值(0)
    if d0 <= _平移静止线:
        return None   # 画面几乎没变（截图噪声级）：无「平移」可言，朴素差分自然零重扫
    粗 = [(差值(dy), dy) for dy in range(-限, 限 + 1, 3)]
    最佳差, 最佳dy = min(粗)
    for dy in range(max(-限, 最佳dy - 3), min(限, 最佳dy + 3) + 1):
        差 = 差值(dy)
        if 差 < 最佳差:
            最佳差, 最佳dy = 差, dy
    if abs(最佳dy) < _平移最小位移:
        return None
    if 最佳差 >= _平移噪声 or 最佳差 >= d0 * _平移判定比:
        return None
    dy原 = int(round(最佳dy * w / float(小w)))
    return dy原, round(最佳差, 2), round(d0, 2)


def _有标签伴生(e, ocr列表):
    """表单布局铁律：控件必有文本标签伴生（右侧 10~180px 或下方 10~80px）。

    这是形状语义名（☐☑○◉滑块）唯一的场景证据——小图标与表单控件在
    15~20px 尺寸下形态学彻底同构（铁律21③），纯像素分不开。
    """
    l, t, r, b = e.bbox
    for o in ocr列表:
        ox, oy = o.center()
        if (r + 10 <= ox <= r + 180 and t - 40 <= oy <= b + 40) or \
           (l - 40 <= ox <= r + 40 and b + 10 <= oy <= b + 80):
            return True
    return False


def _标签伴生闸(visual列表, ocr列表):
    """形状通道标签伴生闸（场景先验闸，铁律21③）。

    小重构（2026-10-25 施工单·任务A）：处置从「丢弃」改「降级」。
    旧处置：无伴生标签的形状语义元素直接丢弃——但形状通道在产出时已把
    同位无名 icon 候选挤掉（visual_engine「形状同位 icon 剔除」），闸门再杀
    形状=该位置候选彻底消失（双输）。统考实锤：SS-Pro 4K 专业软件工具栏
    图标（纯图标域、无文字标签可伴）被大量误读为 ○/◉ 后整候选消失，
    icon oracle 46.05%→28.95%。
    新处置：无伴生 → 形状**语义断言**不成立，但几何候选本身（此处有东西）
    依然成立 → 降级回中性无名 icon 候选（text=""、type="icon"、conf=0.3），
    位置证据保留，不给无证据的语义名（游戏装饰环照样不会被当 ○ 单选钮，
    防误报初衷完整保持）。conf=0.3 走通用 icon 融合路径（可被 OCR/UIA
    吞并），与「不可信证据」的定位一致。
    域自适应豁免判据=伴生信号本身（OCR 文字伴生）：有标签域（表单）保持
    形状语义，无标签域（纯图标工具栏）退回中性候选——同一信号两种处置，
    零新参数、零新闸门。
    """
    if not visual列表 or not ocr列表:
        return visual列表
    for e in visual列表:
        if e.text in _形状语义集 and not _有标签伴生(e, ocr列表):
            e.text = ""
            e.type = "icon"
            e.conf = 0.3
    return visual列表


# ---- compact 扫描档（2026-11-04 实操体感刀1）----
# 病根（评测实证）：一次扫描 59 元素里约 18 个空文本壳（空 pane+空图标），
# 菜单项 button+menu_item+text 三份同文本，单扫约 10KB 烧 agent 上下文。
# 滤选必须在编号分配（assign_ids / IdTracker.apply）之前做，否则滤掉元素
# 会造成 id 断号；滤选后的集合进 tracker 与增量基线，帧间保号口径前后一致。
_COMPACT可交互类型 = frozenset((
    "button", "edit", "checkbox", "combobox", "item", "link",
    "menu_item", "tab", "tree_item", "radio", "slider"))
# 同文本去重的保留优先序（序号小者优先）：可交互 > 非交互；同级保 UIA 源
# （有 hwnd 可走控件级调用）再保 conf 高者
_COMPACT交互序 = {"button": 0, "menu_item": 1, "edit": 2, "combobox": 3,
                  "item": 4, "tab": 5, "tree_item": 6, "checkbox": 7,
                  "radio": 8, "link": 9, "slider": 10}
_COMPACT源序 = {"uia": 0, "ocr": 1, "visual": 2}


def _compact归一文本(s):
    """去重用的文本归一化：去全部空白 + 小写（中文原样）。"""
    return "".join(str(s or "").split()).lower()


def _compact同位(a, b):
    """compact 去重的同位判定：bbox 互相包含或 IoU>0.3（须配合同文本才合并）。"""
    al, at, ar, ab = a.bbox
    bl, bt, br, bb = b.bbox
    互相包含 = ((al <= bl and at <= bt and ar >= br and ab >= bb) or
               (bl <= al and bt <= at and br >= ar and bb >= ab))
    return 互相包含 or fuse._iou(a.bbox, b.bbox) > 0.3


def _compact滤选(元素):
    """compact 档两刀（2026-11-04 实操体感刀1），返回 (保留列表, 滤空数, 去重数)。

    ① 滤空壳：空文本且非可交互的元素剔除（空 pane/group/无名字 icon——
       只占编号烧上下文，agent 对它形不成任何操作意图）；可交互类型
       （button/edit/menu_item 等）与 clickable 元素空文本也保留——点得到
       比读得到重要（无文字图标按钮仍可按编号点击）。
    ② 同文本去重：归一化文本相同且 bbox 包含/重叠的合并为一个（同一菜单项
       button+menu_item+text 三份同文本实测），保留序见 _COMPACT交互序。
       空文本元素不参与去重（两个无名控件同位≠同一元素，合并会误杀）。
    """
    原数 = len(元素)
    保留 = [e for e in 元素
            if e.text or e.type in _COMPACT可交互类型 or e.clickable]
    滤空数 = 原数 - len(保留)

    def 保序权重(e):
        return (_COMPACT交互序.get(e.type, 99),
                _COMPACT源序.get(e.source, 99),
                -e.conf)

    出 = []
    for e in sorted(保留, key=保序权重):
        归一 = _compact归一文本(e.text)
        if not 归一:
            出.append(e)   # 空文本不去重（防无名可交互控件被同位合并误杀）
        elif not any(_compact归一文本(k.text) == 归一 and _compact同位(e, k)
                     for k in 出):
            出.append(e)
    return 出, 滤空数, len(保留) - len(出)


def scan(win=None, no_focus=False, max_nodes=800, no_uia=False, no_ocr=False,
         no_visual=False, background=False, hwnd=None, tracker_state=None,
         incremental=None, no_book=None, compact=None):
    """三引擎并行扫描核心。

    hwnd：窗口句柄直定位（优先于 win 标题；配合 background 天然非前台）。
    background：后台模式——不置前窗口，PrintWindow 抓窗（被遮挡也能抓）。
    tracker_state：上一帧编号追踪快照（None=不追踪，走纯空间编号）。
    incremental 三态（2026-11-01 A2 前为 False）：
      None（默认）= 智能：同桶（同窗口/同全屏）且距上帧 ≤60s 自动走增量
        （meta 报 incremental_auto=True）；首帧/跨窗口/超时退全量——agent
        最常见用法就是连续扫同一窗口，最常见用法就该是快的；
      True = 强制增量（无上帧自动退全量并存基线）；
      False = 强制全量（行为与 A2 前的默认一致）。
      增量模式与上一帧缓存图差分，只对变化矩形裁块跑 OCR/视觉，未变化区域
      的 OCR/视觉元素沿上一帧快照保留（UIA 照常全量，控件树读取本就快）；
      上帧缺失/区域不一致/变化面积过大时自动退回全量。
    no_book 三态（2026-11-01 A2 前为 False）：
      None（默认）= 智能：窗口扫描入库，全屏扫描不入库（桌面/任务栏噪声
        无检索价值，隐私敏感——screen 域 657 条存量即此成因，已清）；
        智能默认下窗口标题命中隐私敏感名单（sp/privacy.py：记事本/聊天/
        密码登录类）一律不入库并在 meta 报 privacy_skip（2026-11-04 刀2
        红线）；True = 强制不入库；False = 强制入库（显式值不被隐私名单拦）。
    compact 三态（2026-11-04 实操体感刀1）：
      None（默认）= 开：融合后编号分配前滤空壳+同文本同位去重（meta 报
        compact_removed 统计）；False = 关：全量元素表（行为与从前一致）；
      True = 强制开（与默认同）。

    返回 dict：meta（time/duration_ms/screen/window/offset/counts/errors/
    background/bg_fallback，增量成功时另有 incremental/refreshed_rects，
    智能增量生效另有 incremental_auto，未入库时 book.skipped+reason）、
    elements（Element 列表，已编号）、img（BGR ndarray）、
    tracker（{"state": 新快照, "stats": 统计} 或 None）。
    抛异常时向上传播（窗口找不到等致命错误由调用方决定话术）。
    """
    开始 = time.perf_counter()
    sw, sh = capture.screen_size()

    # 增量三态的智能判定必须在 UIA 线程启动前定值（线程体读 incremental 是
    # 一次性判定，启动后改值有竞态）。桶key 此处已知：全屏抓图区域恒全屏，
    # 窗口模式的精确 region 匹配交给截图后的 _增量读 把关（不匹配自动退全量）。
    桶key = str(hwnd) if hwnd else (win if win else "screen")
    增量自动 = False
    if incremental is None:
        # 智能增量（2026-11-01 A2）：同桶且距上帧 ≤60s 自动走增量——agent
        # 最常见用法是连续扫同一窗口，最常见用法就该是快的；首帧/跨窗口/
        # 超时退全量。显式 True/False 不经此分支，行为与从前一致。
        try:
            预区域 = [0, 0, int(sw), int(sh)] if not (win or hwnd) else None
            上帧t = _上帧时刻(桶key, 预区域)
        except Exception:
            上帧t = None
        if 上帧t is not None and (time.time() - 上帧t) <= _智能增量窗口秒:
            incremental = True
            增量自动 = True
        else:
            incremental = False

    盒子 = {"uia列表": [], "uia错误": None, "窗口名": None, "区域": None,
           "hwnd": None, "url_host": None, "rect就绪": threading.Event(),
           # 增量模式：主线程截图差分后决定是否跳过 UIA 遍历（UIA 元素复用
           # 上帧快照——遍历是窗口扫描的大头，雷电挂机负载下占 2s+）
           "增量决定": threading.Event(), "跳uia遍历": False}
    偏移 = (0, 0)

    def uia线程体():
        # UIA 是 STA COM：本线程独立初始化，且所有控件对象都只在本线程内创建与使用
        try:
            import ctypes
            ctypes.windll.ole32.CoInitialize(None)
        except Exception:
            pass
        try:
            uia_root = None
            if hwnd or win:
                if hwnd:
                    wc = uia_engine.control_from_hwnd(hwnd)
                else:
                    wc = uia_engine.find_window_control(win)
                # UIA 读控件树本身不需要前台；仅普通（非后台）模式才置前
                if not background and not no_focus:
                    uia_engine.activate_control(wc)  # SetActive 处理前台锁
                    time.sleep(0.35)  # 等前置渲染稳定再交矩形
                rect = wc.BoundingRectangle
                l, t = max(0, int(rect.left)), max(0, int(rect.top))
                r, b = min(sw, int(rect.right)), min(sh, int(rect.bottom))
                盒子["区域"] = (l, t, r, b)
                盒子["hwnd"] = int(wc.NativeWindowHandle or 0) or None
                uia_root = wc
                盒子["窗口名"] = wc.Name
                # 书库 v2 域解析：浏览器窗口读地址栏 host（同线程 STA 安全；
                # 读不到退化 web_ 前缀，绝不打断主流程）
                if not no_uia and booklib.是浏览器标题(盒子["窗口名"]):
                    try:
                        盒子["url_host"] = uia_engine.读地址栏主机(wc)
                    except Exception:
                        盒子["url_host"] = None
            盒子["rect就绪"].set()
            if not no_uia:
                if incremental:
                    # 增量模式：等主线程差分决定（默认 false 时最多等 8s 后照跑）
                    盒子["增量决定"].wait(timeout=8)
                if not (incremental and 盒子["跳uia遍历"]):
                    盒子["uia列表"] = uia_engine.scan_uia(
                        root=uia_root, screen_wh=(sw, sh), max_nodes=max_nodes,
                        root_hwnd=int(getattr(uia_root, "NativeWindowHandle", 0) or 0))
        except Exception as e:
            盒子["uia错误"] = str(e)
        finally:
            盒子["rect就绪"].set()
            盒子["增量决定"].set()

    线程A = threading.Thread(target=uia线程体, name="uia-scan", daemon=True)
    线程A.start()

    bg_fallback = False
    抓图区域 = [0, 0, int(sw), int(sh)]
    if win or hwnd:
        # 窗口模式：截图区域由线程 A 产出
        if not 盒子["rect就绪"].wait(timeout=5.0):
            raise RuntimeError("等待窗口矩形超时（5s）")
        if 盒子["区域"] is None:
            raise RuntimeError("窗口定位失败: %s" % (盒子["uia错误"] or "未知"))
        l, t, r, b = 盒子["区域"]
        偏移 = (l, t)
        if background:
            # 后台抓窗：窗口自身绘制到内存位图，被遮挡也真实；黑帧回退屏幕拷贝
            目标句柄 = hwnd or 盒子["hwnd"]
            if not 目标句柄:
                raise RuntimeError("后台模式未取到窗口句柄（无 HWND 的窗口请用普通模式）")
            img, rect抓, bg_fallback = capture.shot_window_hwnd(目标句柄)
            抓图区域 = [int(rect抓[0]), int(rect抓[1]), int(rect抓[2]), int(rect抓[3])]
        else:
            img = capture.shot_region(l, t, r, b)
            抓图区域 = [l, t, r, b]
    else:
        if background:
            raise RuntimeError("后台模式需指定窗口（--win / hwnd），全屏无「非前台」概念")
        img = capture.shot_full()

    # 增量准备（施工二）：读上帧缓存 → 主线程差分出变化矩形 → 放行 UIA 线程
    # 的遍历决定（走增量=跳过遍历，UIA 元素复用上帧快照；退全量=照常遍历）。
    # 增量三态的智能判定已在线程启动前完成（防 UIA 线程读值竞态）。
    上帧 = None
    增量退因 = None
    走增量 = False
    滚动位移 = None     # (dx, dy) 原像素，dy>0=内容上移（视口下滚）；非滚动帧 None
    变化矩形s = []      # 图内坐标（增量模式：需要重扫 OCR/视觉的区域）
    差分占比 = 0.0
    保留ocr = []
    保留visual = []
    if incremental:
        if no_ocr and no_visual:
            增量退因 = "no_ocr+no_visual：无 OCR/视觉开销可省，退全量"
        else:
            上帧 = _增量读(桶key, 抓图区域)
            if 上帧 is None:
                增量退因 = "无有效上帧缓存（首帧或抓图区域已变），退全量"
            else:
                # 平移检测一律先跑（~40ms，numpy 切片向量化）：滚动=内容整体
                # 平移，朴素差分把滚动判成整窗变化（实测 93~100% 超 60% 全量线
                # 退全量）；小幅滚动占比虽不过线、坐标却已失真，同样要对齐差分
                # 并把位移透传书库标 stale——盲区堵在检测在先，不看占比脸色
                平移 = _平移检测(上帧[0], img)
                if 平移 is not None:
                    dy = 平移[0]
                    滚动位移 = (0, dy)
                    rects, 占比 = _差分矩形(上帧[0], img, 平移dy=dy)
                else:
                    rects, 占比 = _差分矩形(上帧[0], img)
                差分占比 = 占比
                if 占比 <= _变化占比全量线:
                    变化矩形s = rects
                    走增量 = True   # rects 可为空=画面无变化，零重扫全复用（合法增量帧）
                else:
                    增量退因 = ("变化面积占比 %d%% 超全量线 %d%%，退全量"
                                % (round(占比 * 100), round(_变化占比全量线 * 100)))
    # 滚动帧 UIA 照常真遍历（遍历本就便宜且滚动后控件位置全变，快照复用会
    # 全体错位）；非滚动增量帧才跳过遍历复用快照
    盒子["跳uia遍历"] = 走增量 and 滚动位移 is None
    盒子["增量决定"].set()

    def ocr视觉体():
        ocr列表, ocr错误 = [], None
        visual列表 = []
        模板列表 = []
        if 上帧 is not None and 走增量:
            # ---- 增量分支：OCR/视觉只重扫变化矩形（内容变化由差分保证重扫，
            # 如计算器显示区 0→7；滚动帧=平移对齐后的新露出条带+残余块）。
            # UIA：非滚动帧复用上帧快照（控件树本帧不重扫）；滚动帧线程 A
            # 照常真遍历（滚动后控件位置全变，快照全体错位不可复用）----
            from .model import Element
            _上帧图, 上帧快照 = 上帧
            复用uia = []
            dy滚动 = 滚动位移[1] if 滚动位移 else 0
            dx滚动 = 滚动位移[0] if 滚动位移 else 0
            h图, w图 = img.shape[:2]
            for d in 上帧快照:
                el = Element(type=d.get("type", "generic"),
                             text=d.get("text", ""),
                             bbox=list(d["bbox"]),
                             source=d.get("source", "uia"),
                             conf=float(d.get("conf", 0) or 0),
                             clickable=bool(d.get("clickable", False)),
                             hwnd=int(d.get("hwnd", 0) or 0))
                if el.source == "uia":
                    if 滚动位移 is None:
                        复用uia.append(el)   # UIA 全复用（控件树本帧不重扫）
                    continue
                if 滚动位移 is not None:
                    # 滚动帧：旧 OCR/视觉条目按平移量搬移到本帧位置复用
                    # （OCR 层=视口实拍，纯平移近似对滚动内容成立）；搬移后
                    # 完全滚出视口的条目丢弃
                    el.bbox = [el.bbox[0] + dx滚动, el.bbox[1] + dy滚动,
                               el.bbox[2] + dx滚动, el.bbox[3] + dy滚动]
                    if (el.bbox[2] < 0 or el.bbox[0] > w图 or
                            el.bbox[3] < 0 or el.bbox[1] > h图):
                        continue
                elif 变化矩形s and any(
                        not (el.bbox[2] < x1 or x2 < el.bbox[0] or
                             el.bbox[3] < y1 or y2 < el.bbox[1])
                        for x1, y1, x2, y2 in 变化矩形s):
                    continue   # 变化区域内的旧 OCR/视觉条目丢弃，等新扫补
                if el.source == "ocr":
                    保留ocr.append(el)
                else:
                    保留visual.append(el)
            for x1, y1, x2, y2 in 变化矩形s:
                块 = img[y1:y2, x1:x2]
                if not no_ocr:
                    try:
                        # 快速实例：det 不再把小块放大到短边 736（300x120 实测
                        # 297→56ms，文本逐字一致）；全量路径不受影响
                        for o in ocr_engine.scan_ocr(块, 快速=True):
                            o.bbox = [o.bbox[0] + x1, o.bbox[1] + y1,
                                      o.bbox[2] + x1, o.bbox[3] + y1]
                            ocr列表.append(o)
                    except Exception as e:
                        ocr错误 = ocr错误 or str(e)
                if not no_visual:
                    块v = []
                    块t = []
                    try:
                        块v = visual_engine.scan_visual(块)
                    except Exception:
                        pass
                    try:
                        块t = template_engine.scan_templates(块)
                    except Exception:
                        pass
                    # visual 家族内部去重（与全量路径同规则）
                    visual列表 = fuse.dedup_visual(visual列表 + 块v + 块t)
                    模板列表.extend(块t)
            # 新扫元素 bbox 从截图相对坐标平移到屏幕坐标（保留/复用元素已是屏幕坐标）
            for o in ocr列表 + visual列表:
                o.bbox = [o.bbox[0] + 偏移[0], o.bbox[1] + 偏移[1],
                          o.bbox[2] + 偏移[0], o.bbox[3] + 偏移[1]]
            if 滚动位移 is None:
                盒子["uia列表"] = 复用uia   # 线程 A 遍历已跳过，UIA 以快照为准
            # 滚动帧：不覆盖 uia列表——线程 A 真遍历结果为准
            return (保留ocr + ocr列表), ocr错误, (保留visual + visual列表), len(模板列表)

        # ---- 全量路径（默认；行为零变化）----
        if not no_ocr:
            try:
                ocr列表 = ocr_engine.scan_ocr(img)
            except Exception as e:
                ocr错误 = str(e)
        if not no_visual:
            try:
                visual列表 = visual_engine.scan_visual(img)
            except Exception:
                pass
            try:
                # 模板匹配通道（施工单 C）：固定语义图标（窗口三键/后退/刷新等）
                模板列表 = template_engine.scan_templates(img)
            except Exception:
                pass
            # visual 家族内部去重：同位候选保语义名/conf 高者（防无名形状
            # 解读挤掉模板的语义名，如刷新键被 ◉ 占名）
            visual列表 = fuse.dedup_visual(visual列表 + 模板列表)
        # bbox 从截图相对坐标平移到屏幕坐标
        for o in ocr列表 + visual列表:
            o.bbox = [o.bbox[0] + 偏移[0], o.bbox[1] + 偏移[1],
                      o.bbox[2] + 偏移[0], o.bbox[3] + 偏移[1]]
        return ocr列表, ocr错误, visual列表, len(模板列表)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr-visual") as 池:
        未来 = 池.submit(ocr视觉体)  # 与线程 A 的 UIA 遍历并行
        线程A.join()
        ocr列表, ocr错误, visual列表, 模板数 = 未来.result()

    # 形状通道 UIA 密度闸（富 UIA 窗口静音形状语义元素，见常量处说明）
    if len(盒子["uia列表"]) > _形状通道UIA上限:
        visual列表 = [e for e in visual列表
                      if e.text not in ("☐", "☑", "○", "◉", "滑块")]

    # 形状通道标签伴生闸：无伴生文本标签的形状语义元素降级回中性 icon
    # 候选（处置=降级非丢弃，详见 _标签伴生闸 docstring 的误杀归因）
    visual列表 = _标签伴生闸(visual列表, ocr列表)

    元素 = fuse.fuse(盒子["uia列表"], ocr列表, visual列表)

    # compact 滤选（实操体感刀1）：融合后、编号分配前——滤空壳+同文本同位
    # 去重；滤选后的集合进 tracker/自然编号（id 不断号，帧间保号口径一致）
    compact滤空 = compact去重 = 0
    if compact is not False:
        元素, compact滤空, compact去重 = _compact滤选(元素)

    追踪结果 = None
    if tracker_state is not None:
        追踪器 = tracker.IdTracker(tracker_state)
        统计 = 追踪器.apply(元素, screen_wh=(sw, sh))
        追踪结果 = {"state": 追踪器.snapshot(), "stats": 统计}
    else:
        fuse.assign_ids(元素, screen_wh=(sw, sh))

    meta = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_ms": int((time.perf_counter() - 开始) * 1000),
        "screen": [sw, sh],
        "window": 盒子["窗口名"],
        "hwnd": 盒子["hwnd"] or 0,
        "offset": list(偏移),
        "background": bool(background),
        "bg_fallback": bool(bg_fallback),
        "counts": {"uia": len(盒子["uia列表"]), "ocr": len(ocr列表),
                   "visual": len(visual列表), "template": 模板数,
                   "total": len(元素)},
        "errors": {"uia": 盒子["uia错误"], "ocr": ocr错误},
    }
    meta["url_host"] = 盒子.get("url_host")
    # compact 档统计（实操体感刀1）：滤了多少如实可见；显式关时也如实标注
    if compact is False:
        meta["compact"] = False
        meta["compact_removed"] = 0
    else:
        meta["compact"] = True
        meta["compact_removed"] = compact滤空 + compact去重
        meta["compact_detail"] = {"empty_dropped": compact滤空,
                                  "same_text_merged": compact去重}
    # 增量扫描 meta（施工二）：增量成功报 incremental/refreshed_rects（屏幕坐标）
    # 与复用统计；退全量如实报 fallback 原因，禁止假增量
    if incremental:
        if 走增量:
            meta["incremental"] = True
            if 增量自动:
                meta["incremental_auto"] = True   # A2 智能判定生效（非显式传参）
            meta["refreshed_rects"] = [
                [r[0] + 偏移[0], r[1] + 偏移[1], r[2] + 偏移[0], r[3] + 偏移[1]]
                for r in 变化矩形s]
            meta["incremental_stats"] = {
                "uia_reused": len(盒子["uia列表"]),
                "kept_ocr_visual": len(保留ocr) + len(保留visual),
                "rescanned_rects": len(变化矩形s),
                "diff_ratio": round(差分占比, 3),
            }
            if 滚动位移 is not None:
                # 滚动复用帧：dy>0=内容上移（视口下滚）。书库联动靠同名字段：
                # 入库按它把域内旧条目标 stale_scroll（坐标已随滚动失效）
                meta["scroll_shift"] = [滚动位移[0], 滚动位移[1]]
                meta["incremental_note"] = (
                    "滚动复用帧（平移 dy=%+d px）：UIA 控件树已重扫，OCR/视觉"
                    "上帧条目按平移搬移+新露出条带重扫；入库已把该域滚动前"
                    "旧条目标 stale，查找会如实报 stale=true" % 滚动位移[1])
            else:
                if 差分占比 >= _内容位移线:
                    # 平移未检出但内容变化过大（周期性内容/亚像素渲染下平移
                    # 检出有边界）——保守兜底：内容大概率移动/翻动过，书库
                    # 旧坐标存疑，入库同样标 stale（宁保守勿错坐标）
                    meta["content_shifted"] = True
                # UIA 复用语义如实告知：变化区域的 UIA 文本可能过期（本帧未重扫
                # 控件树），内容核对走裁片通道（定位走文本、看清走裁片）
                meta["incremental_note"] = ("UIA 元素复用上帧快照（控件树未重扫）："
                                            "变化区域元素文本可能过期，内容核对请用 "
                                            "crop_screen/crop_xy 裁片读图")
        else:
            meta["incremental"] = False
            if 增量退因:
                meta["incremental_fallback"] = 增量退因
    # A2 前基线只在增量帧落（普通 scan 不背 PNG 编码开销）；智能增量上线后
    # 任何一帧都必须落基线，否则下一帧永远等不到上帧、智能判定成死功能。
    # 全屏 PNG 编码 ~300ms 级，换 60s 内下一帧提速 ~70%。
    _增量存(桶key, 抓图区域, img, 元素)
    # 堆叠感知（2026-10-28）：窗口模式一律附遮挡摘要——尤其后台模式，抓到的
    # 是窗口自身渲染（后台真相），物理点击只打得动前台可见部分，agent 必须
    # 一眼知道「这张图对应的窗口现在被遮多少、谁挡着」。
    if 盒子["hwnd"]:
        try:
            occ = stacking.occlusion(盒子["hwnd"])
            meta["occlusion"] = {
                "visible_ratio": occ["visible_ratio"],
                "foreground": occ["foreground"],
                "minimized": occ.get("minimized", False),
                "top_blocker": (occ["blockers"][0]
                                if occ["blockers"] else None),
            }
        except Exception as e:
            # 防御段禁静默：留 error 字段防「字段不存在」伪装（Q-040 对策③）
            meta["errors"]["occlusion"] = str(e)
    # 两级视觉·文本通道（2026-10-28 设计方案施工单②）：scan 元素自动入库。
    # 静态图 scan_image 不入库（无窗口域概念）。入库失败不影响扫描主流程。
    # A2 三态：None=智能（窗口扫描入库，全屏不入库——桌面/任务栏噪声无检索
    # 价值，screen 域 657 条存量即此成因）；True/False=强制。
    入库 = (no_book is False) or (no_book is None and bool(win or hwnd))
    # 隐私默认（实操体感刀2·P0 红线）：智能默认（no_book=None）下窗口标题
    # 命中敏感名单（sp/privacy.py：记事本/写字板/密码登录/聊天类）→ 一律
    # 不入库，meta.privacy_skip 如实告知；显式 book=True（no_book=False）/
    # book=False 是调用方明示意志，不被名单拦。全屏模式维持原「不入库」
    # 默认，不受名单影响。判定在 pipeline 一处做，CLI 与 MCP 两端同生效。
    命中词 = None
    if 入库 and no_book is None and (win or hwnd):
        命中词 = (privacy.敏感命中(盒子["窗口名"])
                  or privacy.敏感命中(win))
        if 命中词:
            入库 = False
    if not 入库:
        meta["book"] = {"skipped": True,
                        "reason": ("privacy" if 命中词
                                   else "no_book" if no_book
                                   else "fullscreen_default")}
        if 命中词:
            meta["privacy_skip"] = True
            meta["privacy_note"] = ("窗口标题命中隐私敏感名单（命中「%s」）："
                                    "本窗口扫描默认未写入元素书库；确实需要"
                                    "入库请显式传 book=true 重扫" % 命中词)
    else:
        try:
            meta["book"] = booklib.入库(元素, img, meta)
        except Exception as e:
            meta["errors"]["book"] = str(e)
    return {"meta": meta, "elements": 元素, "img": img, "tracker": 追踪结果}


def scan_image(path, no_ocr=False, no_visual=False):
    """静态图片识别：对图片文件跑 OCR + 视觉双引擎，输出与活屏扫描同构的元素表。

    用途：公开统考（ScreenSpot 系静态截图）/ 离线图片识别。
    与活屏 scan 的差异（meta.source="image" 标识）：
    - UIA 引擎不参与——静态截图没有可读的控件树；
    - bbox 为图像像素坐标（offset=(0,0)），无屏幕坐标概念；
    - 不写 tracker（无帧间追踪意义），逐图独立编号。

    path：图片文件路径（中文路径安全，见 capture.load_image）。
    """
    开始 = time.perf_counter()
    img = capture.load_image(path)
    h, w = img.shape[:2]

    ocr列表, ocr错误 = [], None
    if not no_ocr:
        try:
            ocr列表 = ocr_engine.scan_ocr(img)
        except Exception as e:
            ocr错误 = str(e)
    visual列表 = []
    模板列表 = []
    if not no_visual:
        try:
            visual列表 = visual_engine.scan_visual(img)
        except Exception:
            pass
        try:
            # 模板匹配通道（施工单 C）：与活屏 scan 同构
            模板列表 = template_engine.scan_templates(img)
        except Exception:
            pass
        visual列表 = fuse.dedup_visual(visual列表 + 模板列表)

    元素 = fuse.fuse([], ocr列表, visual列表)

    # 形状通道标签伴生闸（与活屏 scan 同款；scan_image 无 UIA，密度闸不适用）
    # 处置=降级非丢弃（统考 SS-Pro 误杀修法，详见 _标签伴生闸）
    元素 = _标签伴生闸(元素, ocr列表)
    fuse.assign_ids(元素, screen_wh=(w, h))

    meta = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_ms": int((time.perf_counter() - 开始) * 1000),
        "screen": [w, h],
        "window": None,
        "offset": [0, 0],
        "source": "image",
        "image_path": os.path.abspath(path),
        "background": False,
        "bg_fallback": False,
        "counts": {"uia": 0, "ocr": len(ocr列表),
                   "visual": len(visual列表), "template": len(模板列表),
                   "total": len(元素)},
        "errors": {"uia": None, "ocr": ocr错误},
    }
    return {"meta": meta, "elements": 元素, "img": img, "tracker": None}
