# -*- coding: utf-8 -*-
"""识屏系统（screenpilot）CLI 入口。

把 Windows 真实屏幕变成 AI agent 可操作的结构化元素表。

子命令：
  scan      扫描屏幕/窗口，打印 JSON 元素表，生成 annotated_scan.png 并缓存 elements.json（三引擎并行）
  activate  按标题子串激活（置前）窗口
  click     按编号点击元素（坐标从 elements.json 缓存查询）
  clickxy   按屏幕物理坐标直接点击（元素表漏识目标兜底，坐标从截图/标注图读取）
  type      按编号向元素输入文本（中文走剪贴板粘贴）
  key       按键（支持中文键名，如：回车）
  scroll    滚动 up|down 像素
  snap      截屏保存（全屏或指定窗口）
  crop      按编号裁元素原分辨率局部图（两级视觉·看清通道）
  cropxy    按屏幕物理坐标区域裁片（看清通道，漏识目标兜底）
  find      检索元素书库（两级视觉·文本通道：按名查 bbox 直达）
  watch     连拍窗抓瞬时反馈（时间维度：动作后逐帧差分）
  verify    一隐一显：动作后短窗看指定区域是否出现反馈
  hang      发键短窗探测窗口卡死（屏幕通道）
  split     多帧抠底动静态分层（UI 底图/动态掩码/实体与动画层）
"""
import sys

# 本机控制台可能是 GBK 系编码：先把标准流重配置为 UTF-8，避免中文输出崩溃/乱码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import os

# 保证 sp 包可被导入（无论从哪个工作目录启动）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse
import json
import time

# DPI 感知必须尽早在主线程设置（导入 sp 包时也会自动设置，双保险）
from sp import enable_dpi_awareness

enable_dpi_awareness()

from sp import annotate, booklib, capture, executor, lastshot, pipeline, stacking, uia_engine, waiter, watch
from sp.model import element_from_dict

默认缓存 = "elements.json"
默认标注图 = "annotated_scan.png"


def _打印(obj):
    """单行 JSON 输出，便于 agent 解析。"""
    print(json.dumps(obj, ensure_ascii=False))


def _写json(path, obj):
    """UTF-8 写 JSON 文件（ensure_ascii=False，保留中文可读）。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def _读缓存(path):
    """读取 elements.json 缓存，返回元素字典列表。"""
    if not os.path.exists(path):
        raise FileNotFoundError("元素缓存不存在: %s（请先执行 scan）" % path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f).get("elements", [])


def do_scan(args):
    """scan 子命令：三引擎并行扫描 + 融合 + 编号 + 标注图 + 缓存。

    编号稳定性：缓存文件里内嵌上一帧追踪快照（tracker 字段），连续 scan 自动
    保号（同元素沿用旧编号，新元素追加）；换 --out 或删缓存则重新自然编号。
    """
    # 追踪状态按窗口分桶（key=win/句柄/screen）：同窗口反复扫描保号，换窗口互不干扰；
    # 旧格式（单状态 dict 含 elements）自动转为对应桶
    桶key = str(args.hwnd) if args.hwnd else (args.win if args.win else "screen")
    旧状态 = None
    if os.path.exists(args.out):
        try:
            with open(args.out, "r", encoding="utf-8") as f:
                桶 = json.load(f).get("tracker") or {}
            if isinstance(桶, dict) and "elements" in 桶:  # 旧单状态格式
                桶 = {桶key: 桶}
            旧状态 = 桶.get(桶key)
        except Exception:
            旧状态 = None
    try:
        结果 = pipeline.scan(win=args.win, no_focus=args.no_focus, max_nodes=args.max_nodes,
                           no_uia=args.no_uia, no_ocr=args.no_ocr, no_visual=args.no_visual,
                           background=args.bg, hwnd=args.hwnd,
                           # None→{}：首帧也要建追踪器写快照，下一帧才能保号
                           tracker_state=旧状态 if 旧状态 is not None else {},
                           # 三态默认（2026-11-01 A2）：--incremental→True 强制
                           # 增量，--full→False 强制全量，都未传→None 智能
                           # （同窗口 60s 内自动增量）；book 同理智能——窗口
                           # 扫描入库、全屏不入库，--book/--no-book 显式定
                           incremental=(True if getattr(args, "incremental", False)
                                        else (False if getattr(args, "full", False)
                                              else None)),
                           no_book=(True if getattr(args, "no_book", False)
                                    else (False if getattr(args, "book", False)
                                          else None)))
    except Exception as e:
        _打印({"ok": False, "error": "scan 失败: %s" % e})
        return 1

    meta, 元素, img = 结果["meta"], 结果["elements"], 结果["img"]
    if 结果["tracker"]:
        meta["tracker"] = 结果["tracker"]["stats"]

    # 缓存元素表 + 标注图 + 追踪快照（按窗口分桶）
    缓存 = {"meta": meta, "elements": [e.to_dict() for e in 元素]}
    if 结果["tracker"]:
        try:
            with open(args.out, "r", encoding="utf-8") as f:
                桶 = json.load(f).get("tracker") or {}
        except Exception:
            桶 = {}
        if isinstance(桶, dict) and "elements" in 桶:  # 旧单状态格式
            桶 = {桶key: 桶}
        桶[桶key] = 结果["tracker"]["state"]
        缓存["tracker"] = 桶
    _写json(args.out, 缓存)
    # 两级视觉·裁片通道地基：登记最近抓图（原图+状态+同源元素表路径），
    # crop/cropxy 从这里取图裁片（2026-10-28 设计方案施工单①）
    try:
        lastshot.register(img, meta, elements_json=os.path.abspath(args.out))
    except Exception as e:
        meta["lastshot_error"] = str(e)
    try:
        annotate.annotate(img, 元素, args.annotated, offset=meta["offset"])
        标注路径 = os.path.abspath(args.annotated)
    except Exception as e:
        标注路径 = None
        meta["annotate_error"] = str(e)

    输出 = {"meta": dict(meta), "elements": 缓存["elements"]}
    输出["meta"]["elements_json"] = os.path.abspath(args.out)
    输出["meta"]["annotated_png"] = 标注路径
    if meta.get("bg_fallback"):
        输出["hint"] = ("后台抓窗黑帧已回退屏幕拷贝：若窗口被遮挡，截图混入遮挡内容，"
                        "OCR/视觉结果可能不准（UIA 控件树不受影响）。")
    _打印(输出)
    return 0


def do_scanimg(args):
    """scanimg 子命令：静态图片识别（OCR+视觉双引擎，统考/离线图用）。

    独立于活屏 elements.json 缓存（不碰保号链），结果落 --out 指定文件。
    """
    try:
        结果 = pipeline.scan_image(args.image, no_ocr=args.no_ocr, no_visual=args.no_visual)
    except Exception as e:
        _打印({"ok": False, "error": "scanimg 失败: %s" % e})
        return 1
    meta, 元素, img = 结果["meta"], 结果["elements"], 结果["img"]
    缓存 = {"meta": meta, "elements": [e.to_dict() for e in 元素]}
    _写json(args.out, 缓存)
    try:
        annotate.annotate(img, 元素, args.annotated, offset=meta["offset"])
        标注路径 = os.path.abspath(args.annotated)
    except Exception as e:
        标注路径 = None
        meta["annotate_error"] = str(e)
    输出 = {"meta": dict(meta), "elements": 缓存["elements"]}
    输出["meta"]["elements_json"] = os.path.abspath(args.out)
    输出["meta"]["annotated_png"] = 标注路径
    _打印(输出)
    return 0


def do_stack(args):
    """stack 子命令：窗口 Z 序全景（堆叠状态感知）+ 可选遮挡量化。"""
    try:
        结果 = stacking.stack(limit=args.limit)
        if args.occlude:
            结果["occlusion"] = stacking.occlusion(args.occlude, step=args.step)
        结果["hint"] = ("foreground=当前焦点窗；windows 按 Z 序（首个最顶层）。"
                        "被遮挡窗口的物理点击会点到遮挡物——先 activate 置前。")
        _打印(结果)
        return 0
    except Exception as e:
        _打印({"ok": False, "action": "stack", "error": str(e)})
        return 1


def do_activate(args):
    """activate 子命令：按标题子串激活（置前）窗口。"""
    try:
        wc, rect = uia_engine.activate_window(args.win, wait=args.wait)
    except ValueError as e:
        _打印({"ok": False, "action": "activate", "window": args.win, "error": str(e)})
        return 1
    _打印({"ok": True, "action": "activate", "window": wc.Name,
           "rect": [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]})
    return 0


def _查元素(args, 元素id):
    """从缓存查元素并重建 Element。"""
    for d in _读缓存(args.file):
        if d.get("id") == 元素id:
            return element_from_dict(d)
    raise ValueError("缓存中无编号 %s 的元素（缓存共 %s 条）" % (元素id, len(_读缓存(args.file)) if os.path.exists(args.file) else 0))


def do_click(args):
    """click 子命令：按编号点击（默认漂移落点，--precise 恢复像素级精准）。

    --expect 传 JSON 条件做业务生效判定（如 '{"find":"保存成功","mode":"appear"}'）：
    点击发出后轮询扫描等待，超时如实返回未命中（ok=false + expect 报告）。
    物理点击默认落点归窗核验（--no-check-top 关）：落点最顶层窗不是目标窗
    （被遮挡）就拒绝点击并报 top_window，防「点了也不知道点到谁」。
    """
    try:
        el = _查元素(args, args.id)
    except (FileNotFoundError, ValueError) as e:
        _打印({"ok": False, "action": "click", "id": args.id, "error": str(e)})
        return 1
    结果 = executor.click(el, force_physical=getattr(args, "physical", False),
                          precise=getattr(args, "precise", False),
                          check_top=not getattr(args, "no_check_top", False))
    结果.update({"action": "click", "id": el.id, "type": el.type,
                 "text": el.text[:50], "source": el.source})
    结果 = waiter.apply_expect(结果, getattr(args, "expect", None),
                               默认hwnd=int(getattr(el, "hwnd", 0) or 0))
    _打印(结果)
    return 0 if 结果.get("ok") else 1


def do_clickxy(args):
    """clickxy 子命令：按屏幕物理坐标直接点击（元素表漏识目标兜底通道）。

    命中的是最顶层窗口：结果附 hit_window（落到谁身上）；--top-must 指定
    标题子串则事前核验，不符拒绝点击（防后台图坐标点到前台遮挡物）。
    """
    结果 = executor.click_xy(args.x, args.y, precise=getattr(args, "precise", False),
                             top_must=getattr(args, "top_must", None))
    结果.update({"action": "clickxy"})
    结果 = waiter.apply_expect(结果, getattr(args, "expect", None))
    _打印(结果)
    return 0 if 结果.get("ok") else 1


def do_type(args):
    """type 子命令：按编号输入文本（支持中文，走剪贴板）。"""
    text = " ".join(args.text)
    try:
        el = _查元素(args, args.id)
    except (FileNotFoundError, ValueError) as e:
        _打印({"ok": False, "action": "type", "id": args.id, "error": str(e)})
        return 1
    结果 = executor.type_text(el, text, precise=getattr(args, "precise", False))
    结果.update({"action": "type", "id": el.id, "type": el.type,
                 "text": el.text[:50], "source": el.source})
    结果 = waiter.apply_expect(结果, getattr(args, "expect", None),
                               默认hwnd=int(getattr(el, "hwnd", 0) or 0))
    _打印(结果)
    return 0 if 结果.get("ok") else 1


def do_key(args):
    """key 子命令：按键（中文键名映射）。"""
    结果 = executor.press_key(args.键名)
    结果.update({"action": "key"})
    _打印(结果)
    return 0 if 结果.get("ok") else 1


def do_scroll(args):
    """scroll 子命令：滚动。"""
    结果 = executor.scroll(args.direction, args.pixels)
    结果.update({"action": "scroll"})
    _打印(结果)
    return 0 if 结果.get("ok") else 1


def do_snap(args):
    """snap 子命令：截图保存（--bg 走 PrintWindow 后台抓窗，被遮挡也真实）。"""
    sw, sh = capture.screen_size()
    回退 = False
    抓图偏移 = [0, 0]
    抓图句柄 = 0
    窗口 = None
    if args.win:
        if args.bg:
            wc = uia_engine.find_window_control(args.win)
            hwnd = int(wc.NativeWindowHandle or 0)
            if not hwnd:
                _打印({"ok": False, "action": "snap",
                       "error": "后台模式未取到窗口句柄（无 HWND 窗口请用普通模式）"})
                return 1
            img, rect, 回退 = capture.shot_window_hwnd(hwnd)
            l, t, r, b = rect
            抓图偏移 = [int(l), int(t)]
            抓图句柄 = hwnd
            窗口 = wc.Name
        else:
            wc, rect = uia_engine.activate_window(args.win)
            l, t = max(0, int(rect.left)), max(0, int(rect.top))
            r, b = min(sw, int(rect.right)), min(sh, int(rect.bottom))
            img = capture.shot_region(l, t, r, b)
            抓图偏移 = [l, t]
            抓图句柄 = int(getattr(wc, "NativeWindowHandle", 0) or 0)
            窗口 = wc.Name
    else:
        img = capture.shot_full()
        回退 = False
    capture.save_image(img, args.out)
    # 两级视觉·裁片通道地基：snap 也登记最近抓图（无元素表，crop 不可用但 cropxy 可用）
    try:
        lastshot.register(img, {"source": "snap", "window": 窗口,
                                "hwnd": 抓图句柄, "offset": 抓图偏移},
                          elements_json=None, shot_path=os.path.abspath(args.out))
    except Exception:
        pass
    h, w = img.shape[:2]
    _打印({"ok": True, "action": "snap", "path": os.path.abspath(args.out),
           "size": [w, h], "window": 窗口, **({"bg_fallback": True} if 回退 else {})})
    return 0


def do_crop(args):
    """crop 子命令：按编号裁元素局部原分辨率图（两级视觉·看清通道）。"""
    try:
        el = _查元素(args, args.id)
    except (FileNotFoundError, ValueError) as e:
        _打印({"ok": False, "action": "crop", "id": args.id, "error": str(e)})
        return 1
    try:
        路径, 信息 = lastshot.crop_element(el.to_dict(), pad=args.pad, out_path=args.out)
    except Exception as e:
        _打印({"ok": False, "action": "crop", "id": args.id, "error": str(e)})
        return 1
    _打印({"ok": True, "action": "crop", "id": args.id, "text": el.text[:50],
           "element_type": el.type, "path": 路径, **信息,
           "hint": "读图工具打开 path 查看——原分辨率裁片，逐字可读。"})
    return 0


def do_cropxy(args):
    """cropxy 子命令：按屏幕物理坐标区域裁片（看清通道，元素表漏识目标兜底）。"""
    try:
        路径, 信息 = lastshot.crop_xy(args.x1, args.y1, args.x2, args.y2,
                                      pad=args.pad, out_path=args.out)
    except Exception as e:
        _打印({"ok": False, "action": "cropxy", "error": str(e)})
        return 1
    _打印({"ok": True, "action": "cropxy", "path": 路径, **信息,
           "hint": "读图工具打开 path 查看——原分辨率裁片，逐字可读。"})
    return 0


def do_find(args):
    """find 子命令：检索元素书库（文本通道：按名查 bbox 直达）。

    --image 图片路径：以图查库（F3·BOOKVEC=1）——裁片 png 编码与向量库
    余弦，top-K 命中书库条目（纯图标/无文字元素主通道）。
    """
    if args.image:
        try:
            from sp import bookvec
            条目 = bookvec.以图查库(args.image, domain=args.domain,
                                   类型=args.type, limit=args.limit)
        except Exception as e:
            _打印({"ok": False, "action": "find", "mode": "image",
                   "image": args.image, "error": str(e),
                   "hint": "以图查库需 BOOKVEC=1 且向量库非空（booksweep --embed 回填）。"})
            return 1
        _打印({"ok": True, "action": "find", "mode": "image", "image": args.image,
               "count": len(条目), "entries": 条目,
               "hint": "余弦降序 top-K，cos=查询图↔条目裁片相似度；bbox 语义同文本通道。"})
        return 0
    if not args.query and not args.domain and not args.type:
        _打印({"ok": True, "action": "find", "domains": booklib.域清单(),
               "hint": "未指定查询词——返回域清单；find <名> [--domain 域] 查具体条目。"})
        return 0
    条目, 排除, 过期排除 = booklib.查找明细(query=args.query, domain=args.domain,
                                        类型=args.type, limit=args.limit,
                                        include_legacy=args.include_legacy)
    _打印({"ok": True, "action": "find", "count": len(条目),
           "legacy_excluded": 排除, "expired_excluded": 过期排除, "entries": 条目,
           "hint": "bbox=最后入库时的屏幕坐标，可 clickxy/cropxy 直达；窗口挪动后以重扫为准。"
                   "legacy 旧条目默认排除（--include-legacy 查归档前旧账）；"
                   "TTL 到期条目一律排除（booksweep --expired 可清）。"})
    return 0


def do_bookarchive(args):
    """bookarchive 子命令：legacy 旧条目归档/恢复/查看（移动不是删除，可逆）。"""
    if args.list:
        _打印({"ok": True, "action": "bookarchive", "mode": "list",
               "archive_root": booklib.归档根, "domains": booklib.归档清单(),
               "hint": "manual_archive/<域>/entries.json；恢复=bookarchive --restore [--domain 域]。"})
        return 0
    if args.restore:
        摘要 = booklib.恢复归档(args.domain)
        _打印({"ok": True, "action": "bookarchive", "mode": "restore",
               "domains": 摘要,
               "hint": "归档条目已移回主库 manual/<域>/entries.json（裁片 png 一直在原域目录，无缝）。"})
        return 0
    摘要 = booklib.归档域(args.domain)
    _打印({"ok": True, "action": "bookarchive", "mode": "archive",
           "domains": 摘要,
           "hint": "legacy 条目移出主库→manual_archive/<域>/；归档前整域备份 manual_backup_*_prearchive/；"
                   "恢复=bookarchive --restore。"})
    return 0


def do_booksweep(args):
    """booksweep 子命令：书库清扫（列账/清空域/按龄清/清过期/备份回收），
    默认干跑，先备份再动。

    五种用法：
      booksweep --list                      # 全库体检：每域条目/裁片/体积/空域/到期数
      booksweep --empty --domain 靶 --yes    # 清空匹配域（整域移入备份，可恢复）
      booksweep --older-than 30 --yes        # 删 30 天前的条目（裁片一并删，备份 entries.json）
      booksweep --orphans --yes              # 清孤儿裁片（json 无引用的 png，移入备份）
      booksweep --expired --yes              # 清 TTL 到期条目（expires_at 已到；先备份）
      booksweep --backup-gc [--yes]          # 备份份回收：每域留最近 2 份+超 30 天清
      booksweep --embed                      # F3 向量回填：全库裁片重编码重建向量库（幂等）
    缺省（无任何模式旗标）等同 --list。
    """
    if args.embed:
        from sp import bookvec
        摘要 = bookvec.回填()
        摘要.update({"ok": True, "action": "booksweep", "mode": "embed",
                     "vector_db": bookvec.库况(),
                     "hint": "全库裁片重编码（无裁片条目跳过等重扫刷新）；以图查库=find --image。"})
        _打印(摘要)
        return 0
    if args.list or not (args.empty or args.older_than is not None or args.orphans
                         or args.expired or args.backup_gc):
        概况 = booklib.库概况()
        空域 = [r["domain"] for r in 概况["domains"] if r["empty"]]
        _打印({"ok": True, "action": "booksweep", "mode": "list",
               "total_entries": 概况["total_entries"],
               "total_bytes": 概况["total_bytes"],
               "total_expired": 概况.get("total_expired", 0),
               "empty_domains": 空域,
               "domains": 概况["domains"],
               "hint": "清空域=booksweep --empty --domain 子串 --yes；按龄清=booksweep "
                       "--older-than 天数 --yes；清孤儿=booksweep --orphans --yes；"
                       "清到期=booksweep --expired --yes；备份回收=booksweep --backup-gc；"
                       "默认干跑（不带 --yes 只看账）。"
                       "真干前自动整域备份到 manual_backup_<时间戳>/，可恢复清扫。"})
        return 0
    if args.empty and not args.domain:
        _打印({"ok": False, "error": "清空模式必须带 --domain（防误清全库）；"
                                     "全库体检用 booksweep --list"})
        return 2
    if args.backup_gc:
        报告 = booklib.备份回收(干跑=not args.yes)
        报告.update({"ok": True, "action": "booksweep", "mode": "backup_gc",
                     "hint": ("干跑清单即真删清单——核对 would_remove 后加 --yes 真清"
                              "（备份份真删不可恢复，删除前无再备份）。"
                              if not args.yes else
                              "已回收 %d 项，释放 %.1f MB。"
                              % (len(报告.get("removed") or []),
                                 (报告.get("freed_bytes") or 0) / 1048576))})
        _打印(报告)
        return 0
    if args.expired:
        报告 = booklib.清扫(域名=args.domain, 过期=True, 干跑=not args.yes)
        报告.update({"ok": True, "action": "booksweep", "mode": "expired",
                     "hint": ("干跑：加 --yes 才真清。" if not args.yes
                              else "到期条目已清，备份在 %s；恢复=booklib.恢复清扫(标记)。"
                                   % 报告.get("backup"))})
        _打印(报告)
        return 0
    if args.orphans:
        报告 = booklib.清孤儿(干跑=not args.yes)
        报告.update({"ok": True, "action": "booksweep", "mode": "orphans",
                     "hint": ("干跑：加 --yes 才真清（孤儿移入 manual_backup_<标记>/孤儿/）。"
                              if not args.yes else
                              "孤儿已移入 %s。" % 报告.get("backup"))})
        _打印(报告)
        return 0
    报告 = booklib.清扫(域名=args.domain, 空域=args.empty,
                       龄期天数=args.older_than, 干跑=not args.yes)
    报告.update({"ok": True, "action": "booksweep",
                 "mode": "empty" if args.empty else "older_than",
                 "hint": ("干跑：加 --yes 才真清。" if not args.yes
                          else "已备份到 %s；恢复=booklib.恢复清扫(标记)。"
                               % 报告.get("backup"))})
    _打印(报告)
    return 0


def _解析区域(文本):
    """--region "x1,y1,x2,y2" → [x1,y1,x2,y2]（物理像素；缺省 None=全窗）。"""
    if not 文本:
        return None
    parts = [p for p in str(文本).replace("，", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise SystemExit("--region 需要 4 个数字：x1,y1,x2,y2")
    return [float(p) for p in parts]


def _解析动作(文本列表):
    """--action JSON（可多次）→ 动作列表；单条 dict 自动包成列表。"""
    动作 = []
    for 文本 in 文本列表 or []:
        try:
            项 = json.loads(文本)
        except Exception as e:
            raise SystemExit("--action 不是合法 JSON：%s（%s）" % (文本, e))
        if isinstance(项, dict):
            动作.append(项)
        elif isinstance(项, list):
            动作.extend(项)
        else:
            raise SystemExit("--action 须为 JSON 对象或对象数组")
    return 动作 or None


def do_watch(args):
    """watch 子命令：连拍窗抓瞬时反馈（toast/飘字）。"""
    结果 = watch.watch_burst(
        hwnd=args.hwnd, window=args.win, seconds=args.seconds,
        interval=args.interval, region=_解析区域(args.region),
        region_ref=args.region_ref, actions=_解析动作(args.action),
        rebase=not args.no_rebase, thresh=args.thresh,
        hit_ratio=args.hit_ratio, mode=args.mode, save_dir=args.save_dir)
    _打印(结果)
    return 0 if 结果.get("ok") else 1


def do_verify(args):
    """verify 子命令：一隐一显——动作后短窗看回显区是否变化。"""
    结果 = watch.verify_typing(
        hwnd=args.hwnd, window=args.win, region=_解析区域(args.region),
        region_ref=args.region_ref, actions=_解析动作(args.action),
        wait=args.wait, interval=args.interval, thresh=args.thresh,
        change_ratio=args.change_ratio, mode=args.mode, ocr_check=args.ocr_check)
    _打印(结果)
    return 0 if 结果.get("ok") else 1


def do_hang(args):
    """hang 子命令：ESC 短窗探测窗口卡死（屏幕通道，判定要求前台无遮挡）。"""
    结果 = watch.probe_hang(
        hwnd=args.hwnd, window=args.win, key=args.key, wait=args.wait,
        interval=args.interval, resend_after=args.resend_after,
        settle=args.settle, change_ratio=args.change_ratio,
        focus=not args.no_focus)
    _打印(结果)
    return 0 if 结果.get("ok") else 1


def do_split(args):
    """split 子命令：多帧抠底 + 动静态分层（重活，执行期进程让路）。"""
    结果 = watch.split_static_dynamic(
        hwnd=args.hwnd, window=args.win, frames=args.frames,
        interval=args.interval, std_thresh=args.std, min_area=args.min_area,
        min_hits=args.min_hits, moving_min_dist=args.moving_min_dist,
        verify_ocr=args.verify_ocr, out_dir=args.out_dir, mode=args.mode)
    _打印(结果)
    return 0 if 结果.get("ok") else 1


def main(argv=None):
    # F5：起 CLI 即后台预热 OCR 模型（加载 4~6s 与参数解析/截图并行），冷首扫只付推理账
    from sp.ocr_engine import preload as _ocr预热
    _ocr预热()
    p = argparse.ArgumentParser(
        prog="screenpilot",
        description="识屏系统：把 Windows 真实屏幕变成 AI agent 可操作的结构化元素表")
    子 = p.add_subparsers(dest="cmd", required=True)

    # scan
    sp_scan = 子.add_parser("scan", help="扫描屏幕/窗口，产出元素表+标注图")
    sp_scan.add_argument("--win", default=None, help="窗口标题子串（缺省扫全屏）")
    sp_scan.add_argument("--hwnd", type=lambda s: int(s, 0), default=None,
                         help="窗口句柄直定位（优先于 --win，支持 0x 十六进制）")
    sp_scan.add_argument("--bg", action="store_true",
                         help="后台模式：不置前窗口，PrintWindow 抓窗（被遮挡也能抓）")
    sp_scan.add_argument("--no-focus", action="store_true", help="窗口扫描时不自动置前")
    sp_scan.add_argument("--out", default=默认缓存, help="元素表缓存路径（默认 elements.json）")
    sp_scan.add_argument("--annotated", default=默认标注图, help="标注图输出路径（默认 annotated_scan.png）")
    sp_scan.add_argument("--max-nodes", type=int, default=800, help="UIA 遍历节点上限（默认 800）")
    sp_scan.add_argument("--no-uia", action="store_true", help="跳过 UIA 引擎（测试 OCR 兜底）")
    sp_scan.add_argument("--no-ocr", action="store_true", help="跳过 OCR 引擎")
    sp_scan.add_argument("--no-visual", action="store_true", help="跳过视觉引擎")
    sp_scan.add_argument("--no-book", action="store_true",
                         help="本次扫描不写入元素书库（临时/敏感屏幕用）")
    sp_scan.add_argument("--book", action="store_true",
                         help="全屏扫描也写入书库（默认全屏不入库，仅窗口扫描入库；"
                              "敏感窗口——记事本/聊天/密码登录类标题——智能默认"
                              "同样不入库并报 meta.privacy_skip，--book 显式强制）")
    sp_scan.add_argument("--incremental", action="store_true",
                         help="强制增量模式：与上帧差分只重扫变化区域（首帧存基线退全量；"
                              "meta.incremental/refreshed_rects 可查）。"
                              "默认智能：同窗口 60s 内连续扫自动增量")
    sp_scan.add_argument("--full", action="store_true",
                         help="强制全量（默认智能模式下基准测量/怀疑增量失真时用）")
    sp_scan.set_defaults(func=do_scan)

    # scanimg（静态图片识别）
    sp_scanimg = 子.add_parser("scanimg", help="识别静态图片文件（OCR+视觉，统考/离线图用）")
    sp_scanimg.add_argument("image", help="图片文件路径")
    sp_scanimg.add_argument("--out", default="elements_img.json", help="元素表 JSON 输出路径")
    sp_scanimg.add_argument("--annotated", default="annotated_img.png", help="标注图输出路径")
    sp_scanimg.add_argument("--no-ocr", action="store_true", help="跳过 OCR 引擎")
    sp_scanimg.add_argument("--no-visual", action="store_true", help="跳过视觉引擎")
    sp_scanimg.set_defaults(func=do_scanimg)

    # stack（窗口 Z 序全景）
    sp_stack = 子.add_parser("stack", help="窗口 Z 序全景（谁在前台/被谁遮挡）")
    sp_stack.add_argument("--limit", type=int, default=40, help="最多返回窗口数（默认 40）")
    sp_stack.add_argument("--occlude", type=lambda s: int(s, 0), default=None,
                          help="指定窗口句柄做遮挡量化（支持 0x 十六进制）")
    sp_stack.add_argument("--step", type=int, default=48, help="遮挡采样步长（默认 48px）")
    sp_stack.set_defaults(func=do_stack)

    # activate
    sp_act = 子.add_parser("activate", help="按标题子串激活（置前）窗口")
    sp_act.add_argument("--win", required=True, help="窗口标题子串")
    sp_act.add_argument("--wait", type=float, default=0.4, help="置前后等待秒数（默认 0.4）")
    sp_act.set_defaults(func=do_activate)

    # click
    sp_click = 子.add_parser("click", help="按编号点击元素")
    sp_click.add_argument("id", type=int, help="元素编号（scan 输出的 id）")
    sp_click.add_argument("--file", default=默认缓存, help="元素缓存路径")
    sp_click.add_argument("--physical", action="store_true", help="跳过 UIA 控件级调用，直接物理点击")
    sp_click.add_argument("--precise", action="store_true",
                          help="像素级精准落点（默认漂移点击：高斯偏移+移动拖尾）")
    sp_click.add_argument("--no-check-top", action="store_true",
                          help="关闭落点归窗核验（默认开：被遮挡就拒绝物理点击）")
    sp_click.add_argument("--expect", default=None,
                          help='业务生效判定 JSON（如 \'{"find":"保存成功","mode":"appear",'
                               '"timeout":10,"interval":800}\'）：操作后轮询扫描等待条件'
                               "成立才算成功；mode 可 appear/disappear，超时如实返回未命中")
    sp_click.set_defaults(func=do_click)

    # clickxy
    sp_clickxy = 子.add_parser("clickxy", help="按屏幕物理坐标直接点击（元素表漏识兜底）")
    sp_clickxy.add_argument("x", type=int, help="目标物理像素 X 坐标")
    sp_clickxy.add_argument("y", type=int, help="目标物理像素 Y 坐标")
    sp_clickxy.add_argument("--precise", action="store_true",
                            help="像素级精准落点（默认漂移点击）")
    sp_clickxy.add_argument("--top-must", default=None,
                            help="落点最顶层窗口标题必须含此子串才点击（事前拦截错窗）")
    sp_clickxy.add_argument("--expect", default=None,
                            help='业务生效判定 JSON；坐标点击须带 window 或 hwnd '
                                 '（如 \'{"window":"计算器","find":"56","mode":"appear"}\'）')
    sp_clickxy.set_defaults(func=do_clickxy)

    # type
    sp_type = 子.add_parser("type", help="按编号输入文本（支持中文）")
    sp_type.add_argument("id", type=int, help="目标元素编号（edit/编辑区）")
    sp_type.add_argument("text", nargs="+", help="要输入的文本")
    sp_type.add_argument("--file", default=默认缓存, help="元素缓存路径")
    sp_type.add_argument("--precise", action="store_true",
                         help="聚焦点击用像素级精准落点（默认漂移）")
    sp_type.add_argument("--expect", default=None,
                         help='业务生效判定 JSON（同 click --expect）：输入后轮询扫描等待')
    sp_type.set_defaults(func=do_type)

    # key
    sp_key = 子.add_parser("key", help="按键（如：回车 / esc / ctrl+s）")
    sp_key.add_argument("键名", help="键名，支持 + 组合与中文（回车/esc/tab 等）")
    sp_key.set_defaults(func=do_key)

    # scroll
    sp_scroll = 子.add_parser("scroll", help="滚动")
    sp_scroll.add_argument("direction", choices=["up", "down", "上", "下"], help="滚动方向")
    sp_scroll.add_argument("pixels", type=int, help="滚动像素量")
    sp_scroll.set_defaults(func=do_scroll)

    # snap
    sp_snap = 子.add_parser("snap", help="截屏保存")
    sp_snap.add_argument("out", nargs="?", default="snap.png", help="输出路径（默认 snap.png）")
    sp_snap.add_argument("--win", default=None, help="窗口标题子串（缺省全屏）")
    sp_snap.add_argument("--bg", action="store_true",
                         help="后台模式：不置前窗口，PrintWindow 抓窗")
    sp_snap.set_defaults(func=do_snap)

    # crop（按编号裁元素局部图，看清通道）
    sp_crop = 子.add_parser("crop", help="按编号裁元素原分辨率局部图（看清通道）")
    sp_crop.add_argument("id", type=int, help="元素编号（scan 输出的 id）")
    sp_crop.add_argument("--file", default=默认缓存, help="元素缓存路径")
    sp_crop.add_argument("--pad", type=int, default=0, help="裁片四周留白像素（默认 0）")
    sp_crop.add_argument("--out", default=None, help="裁片输出路径（默认 mcp_data/crop_<时间>.png）")
    sp_crop.set_defaults(func=do_crop)

    # cropxy（按坐标区域裁片，看清通道兜底）
    sp_cropxy = 子.add_parser("cropxy", help="按屏幕物理坐标区域裁片（看清通道兜底）")
    sp_cropxy.add_argument("x1", type=int, help="区域左上角 X")
    sp_cropxy.add_argument("y1", type=int, help="区域左上角 Y")
    sp_cropxy.add_argument("x2", type=int, help="区域右下角 X")
    sp_cropxy.add_argument("y2", type=int, help="区域右下角 Y")
    sp_cropxy.add_argument("--pad", type=int, default=0, help="裁片四周留白像素（默认 0）")
    sp_cropxy.add_argument("--out", default=None, help="裁片输出路径（默认 mcp_data/crop_xy_<时间>.png）")
    sp_cropxy.set_defaults(func=do_cropxy)

    # find（书库检索，文本通道）
    sp_find = 子.add_parser("find", help="检索元素书库（按名查 bbox 直达，不重扫）")
    sp_find.add_argument("query", nargs="?", default=None, help="元素名子串（缺省列域清单）")
    sp_find.add_argument("--domain", default=None, help="应用域子串过滤（如 计算器）")
    sp_find.add_argument("--type", default=None, help="元素类型精确过滤（button/edit…）")
    sp_find.add_argument("--limit", type=int, default=30, help="最多返回条数（默认 30）")
    sp_find.add_argument("--include-legacy", action="store_true", default=False,
                         help="含 v1 旧条目（默认排除——坐标不可信还占候选位）")
    sp_find.add_argument("--image", default=None, metavar="图片路径",
                         help="以图查库（F3·BOOKVEC=1）：裁片 png 编码查向量库 top-K")
    sp_find.set_defaults(func=do_find)

    # bookarchive（legacy 归档/恢复，2026-10-29 第二单施工一）
    sp_bkarc = 子.add_parser("bookarchive", help="legacy 旧条目归档/恢复/查看（可逆，不删数据）")
    sp_bkarc.add_argument("--domain", default=None,
                          help="只处理域名子串匹配的域（如 --domain youtube；缺省全库）")
    sp_bkarc.add_argument("--restore", action="store_true", default=False,
                          help="恢复模式：归档条目移回主库（缺省=归档模式）")
    sp_bkarc.add_argument("--list", action="store_true", default=False,
                          help="只看归档目录现状，不动数据")
    sp_bkarc.set_defaults(func=do_bookarchive)

    # booksweep（书库清扫，2026-11-01 A1：只增不减的治理出口）
    sp_bk = 子.add_parser("booksweep", help="书库体检/清扫（默认干跑，先备份再动）")
    sp_bk.add_argument("--list", action="store_true", default=False,
                       help="只看全库体检账（每域条目/裁片/体积/空域标记）")
    sp_bk.add_argument("--domain", default=None,
                       help="域名子串过滤（如 --domain 靶；缺省全部域）")
    sp_bk.add_argument("--empty", action="store_true", default=False,
                       help="清空匹配域的全部条目与裁片（必须带 --domain 防误清全库）")
    sp_bk.add_argument("--older-than", type=float, default=None, metavar="天数",
                       help="删除入库时间早于 N 天的条目（裁片一并删）")
    sp_bk.add_argument("--orphans", action="store_true", default=False,
                       help="清孤儿裁片（域目录里 entries.json 无引用的 png）")
    sp_bk.add_argument("--expired", action="store_true", default=False,
                       help="清 TTL 到期条目（expires_at 已到；默认干跑，先备份）")
    sp_bk.add_argument("--backup-gc", action="store_true", default=False,
                       help="备份份回收：每域留最近 2 份+超 30 天清（干跑默认，"
                            "真删不可恢复，先看干跑清单再 --yes）")
    sp_bk.add_argument("--embed", action="store_true", default=False,
                       help="F3 向量回填：全库裁片重编码重建 manual/_index/vectors.npz（幂等）")
    sp_bk.add_argument("--yes", action="store_true", default=False,
                       help="真执行（缺省=干跑只看账）")
    sp_bk.set_defaults(func=do_booksweep)

    # watch（时间维度·连拍窗抓瞬时反馈，2026-11-01 B1 转正）
    sp_watch = 子.add_parser("watch", help="连拍窗：动作后 0~N 秒逐帧差分，抓 toast/飘字")
    sp_watch.add_argument("--win", default=None, help="窗口标题子串")
    sp_watch.add_argument("--hwnd", type=lambda s: int(s, 0), default=None,
                          help="窗口句柄直定位（优先于 --win）")
    sp_watch.add_argument("--seconds", type=float, default=3.0, help="观察窗秒数（默认 3）")
    sp_watch.add_argument("--interval", type=float, default=0.2, help="帧间隔秒（默认 0.2）")
    sp_watch.add_argument("--region", default=None,
                          help='观察区域 x1,y1,x2,y2（屏幕坐标；缺省全窗）')
    sp_watch.add_argument("--region-ref", choices=["screen", "window"], default="screen",
                          help="region 坐标系（默认 screen=元素表同源）")
    sp_watch.add_argument("--action", action="append", default=None,
                          help='前置动作 JSON，可多次：{"kind":"click","x":..,"y":..} / '
                               '{"kind":"key","key":"enter"} / {"kind":"paste","text":".."} / '
                               '{"kind":"wait","seconds":0.3}')
    sp_watch.add_argument("--no-rebase", action="store_true", default=False,
                          help="动作后不重抓基线（默认重抓：吸收点击浮雕等动作自身反馈）")
    sp_watch.add_argument("--thresh", type=int, default=25, help="差分灰度阈值（默认 25）")
    sp_watch.add_argument("--hit-ratio", type=float, default=0.004,
                          help="判定命中帧的变化占比阈值（默认 0.004）")
    sp_watch.add_argument("--mode", choices=["printwindow", "screen"], default="printwindow",
                          help="抓帧通道（默认后台 PrintWindow；挂死/前台场景用 screen）")
    sp_watch.add_argument("--save-dir", default=None, help="命中帧 PNG 保存目录（缺省不存）")
    sp_watch.set_defaults(func=do_watch)

    # verify（时间维度·一隐一显打字验证）
    sp_verify = 子.add_parser("verify", help="一隐一显：动作后短窗看指定区域是否出现反馈")
    sp_verify.add_argument("--win", default=None, help="窗口标题子串")
    sp_verify.add_argument("--hwnd", type=lambda s: int(s, 0), default=None,
                           help="窗口句柄直定位（优先于 --win）")
    sp_verify.add_argument("--region", default=None,
                           help='回显区域 x1,y1,x2,y2（屏幕坐标；建议必给）')
    sp_verify.add_argument("--region-ref", choices=["screen", "window"], default="screen",
                           help="region 坐标系（默认 screen）")
    sp_verify.add_argument("--action", action="append", default=None,
                           help="前置动作 JSON，可多次（同 watch --action）")
    sp_verify.add_argument("--wait", type=float, default=1.5, help="观察秒数（默认 1.5）")
    sp_verify.add_argument("--interval", type=float, default=0.25, help="帧间隔秒（默认 0.25）")
    sp_verify.add_argument("--thresh", type=int, default=25, help="差分灰度阈值（默认 25）")
    sp_verify.add_argument("--change-ratio", type=float, default=0.01,
                           help="判定出现变化的变化占比阈值（默认 0.01）")
    sp_verify.add_argument("--mode", choices=["printwindow", "screen"], default="printwindow",
                           help="抓帧通道（默认 PrintWindow）")
    sp_verify.add_argument("--ocr-check", action="store_true", default=False,
                           help="附变化前后区域 OCR 文本（慢 1~3 秒）")
    sp_verify.set_defaults(func=do_verify)

    # hang（时间维度·ESC 卡死探测）
    sp_hang = 子.add_parser("hang", help="发键短窗探测窗口卡死（屏幕通道；要求前台无遮挡）")
    sp_hang.add_argument("--win", default=None, help="窗口标题子串")
    sp_hang.add_argument("--hwnd", type=lambda s: int(s, 0), default=None,
                         help="窗口句柄直定位（优先于 --win）")
    sp_hang.add_argument("--key", default="esc", help="探测按键（默认 esc）")
    sp_hang.add_argument("--wait", type=float, default=2.0, help="观察秒数（默认 2.0）")
    sp_hang.add_argument("--interval", type=float, default=0.25, help="帧间隔秒（默认 0.25）")
    sp_hang.add_argument("--resend-after", type=float, default=0.7,
                         help="首轮无变化后补发一发的时刻秒（0=不补发；默认 0.7）")
    sp_hang.add_argument("--settle", type=float, default=0.4,
                         help="发键前等待渲染稳定秒数（默认 0.4）")
    sp_hang.add_argument("--change-ratio", type=float, default=0.004,
                         help="判定有响应的变化占比阈值（默认 0.004）")
    sp_hang.add_argument("--no-focus", action="store_true", default=False,
                         help="不主动置前（默认置前；窗口须已在前台才判定可信）")
    sp_hang.set_defaults(func=do_hang)

    # split（时间维度·抠底动静态分层）
    sp_split = 子.add_parser("split", help="多帧抠底：UI 底图 + 动态掩码 + 实体/动画层分层")
    sp_split.add_argument("--win", default=None, help="窗口标题子串")
    sp_split.add_argument("--hwnd", type=lambda s: int(s, 0), default=None,
                          help="窗口句柄直定位（优先于 --win）")
    sp_split.add_argument("--frames", type=int, default=8, help="采样帧数（默认 8，≥2）")
    sp_split.add_argument("--interval", type=float, default=0.5, help="帧间隔秒（默认 0.5）")
    sp_split.add_argument("--std", type=float, default=12.0,
                          help="动态掩码灰度时间轴标准差阈值（默认 12）")
    sp_split.add_argument("--min-area", type=int, default=60, help="动态块最小面积（默认 60）")
    sp_split.add_argument("--min-hits", type=int, default=3,
                          help="轨迹至少命中帧数（默认 3）")
    sp_split.add_argument("--moving-min-dist", type=float, default=15.0,
                          help="净位移≥此值判实体，否则动画层（默认 15px）")
    sp_split.add_argument("--verify-ocr", action="store_true", default=False,
                          help="附 UI 文本保留率/稳定框误收量化（POC 判据①，慢 2~4 秒）")
    sp_split.add_argument("--out-dir", default=None,
                          help="产物目录（默认 mcp_data/watch_<时间>）")
    sp_split.add_argument("--mode", choices=["printwindow", "screen"], default="printwindow",
                          help="抓帧通道（默认 PrintWindow 后台抓窗）")
    sp_split.set_defaults(func=do_split)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
