# -*- coding: utf-8 -*-
"""时间维度四件套 —— ScreenPilot（2026-11-01 B1 转正）

来源（POC 目录保留作历史证据，勿删）：
  - poc_time_anchor/diff_sampler.py（142 行 · 2026-10-27 时间锚点四合一 POC）
  - poc_pixui/pixui_sampler.py（154 行 · 2026-10-27 像素抠 UI POC）
POC 原成绩（真靶）：v2 连拍抓 toast 10/10；v3 一隐一显打字 20/20；v4 ESC
卡死判定 10/10 误差 0；抠底 UI 文本保留率 98.4%（≥95%）+ 实体层误收 0% +
稳定移动轨迹 9 条（雷电模拟器霸业 idle 真场景）。
转正后自造 tk 靶复验（2026-11-01）：19/19 全过——连拍 10/10、一隐一显
50/50、ESC 10/10 误差 0、抠底 UI 保留 100%+误收 0+移动轨迹 1（`_test_watch.py`）。
两份 POC 共用同一套「后台抓帧 + 灰度差分」底座，本模块合成一个实现
（不重复实现差分采样），对外四能力：

  watch_burst            连拍窗：动作后 0~N 秒逐帧差分，抓 toast/飘字等瞬时反馈
  verify_typing          一隐一显：动作前抓基线，动作后短窗看指定区域是否变化
  probe_hang             ESC 卡死探测：发键后短窗看内容变化（屏幕通道——挂起
                         窗口不处理 WM_PRINT，PrintWindow 会永久阻塞，见 POC
                         实测大坑；屏幕拷贝是挂死观察唯一安全通道）
  split_static_dynamic   抠底动静态分层：多帧逐像素中位数抠 UI 底图，时间轴
                         标准差抠动态掩码，跨帧中心跟踪 + 净位移分实体/动画层

红线（上轮交接第 5 条）：
  - sp/ 现有九件能力行为零改动：本模块只读复用 capture / executor /
    ocr_engine / stacking，不改它们；
  - 重活（多帧采样/抠底）执行期间本进程降 BELOW_NORMAL 让路霸业挂机，
    finally 恢复原优先级（MCP 常驻进程不能永久降档）；
  - 单进程串行；前台干扰型用例串行跑（踩坑教训）。

坐标约定：截图/元素 bbox 一律物理像素；region 默认给屏幕坐标
[x1,y1,x2,y2]（与元素表同源），region_ref="window" 时按窗口图坐标解释。
"""
import ctypes
import ctypes.wintypes as wt
import os
import time

import cv2
import numpy as np

from . import capture, executor, ocr_engine

_USER32 = ctypes.windll.user32
_BELOW_NORMAL = 0x4000

# 产物/命中帧默认落在项目 mcp_data/ 下（与 pipeline 增量缓存同目录）
_数据目录 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp_data")


# ---------------- 基础件：找窗 / 抓帧 / 差分 ----------------

def find_window(title_sub):
    """按标题子串找第一个可见顶层窗口 hwnd；找不到返回 None。

    纯 EnumWindows，不碰 UIA：时间维度工具要的是像素通道，UIA 初始化
    在挂起窗上还有 COM 阻塞风险（POC 坑六：UIA 禁碰挂起窗）。
    """
    hits = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def _回调(h, _l):
        if _USER32.IsWindowVisible(h):
            n = _USER32.GetWindowTextLengthW(h)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                _USER32.GetWindowTextW(h, buf, n + 1)
                if title_sub in buf.value:
                    hits.append(int(h))
        return True

    _USER32.EnumWindows(_回调, 0)
    return hits[0] if hits else None


def _解析窗(hwnd=None, window=None):
    if hwnd:
        h = int(hwnd) & 0xFFFFFFFF
        if not _USER32.IsWindow(h):
            raise ValueError("无效窗口句柄: %s" % hwnd)
        return h
    if window:
        h = find_window(window)
        if not h:
            raise ValueError("找不到标题含「%s」的可见窗口" % window)
        return h
    raise ValueError("必须提供 hwnd 或 window")


def _挂着(hwnd):
    """IsHungAppWindow 旁证（恢复后可能滞后，POC 里只作辅助不作准据）。"""
    try:
        return bool(_USER32.IsHungAppWindow(int(hwnd) & 0xFFFFFFFF))
    except Exception:
        return False


def grab(hwnd, mode="printwindow"):
    """抓一帧。返回 (img_bgr, rect[l,t,r,b], fallback)。

    mode="printwindow"：后台 PrintWindow 抓窗，被遮挡也真实；黑帧自动回退
      屏幕拷贝（fallback=True）。**目标窗挂起时该通道会永久阻塞**——调用前
      须 `_挂着` 检查（本模块四个能力和下游 CLI/MCP 均已检查/规避）。
    mode="screen"：屏幕区域拷贝，不向目标窗发任何消息，挂死观察的安全通道；
      要求窗口在前台且无遮挡。
    """
    hwnd = int(hwnd) & 0xFFFFFFFF
    if mode == "screen":
        rect = capture.window_rect_hwnd(hwnd)
        return capture.shot_region(rect[0], rect[1], rect[2], rect[3]), rect, False
    img, rect, 回退 = capture.shot_window_hwnd(hwnd, fallback_screen=True)
    return img, rect, 回退


def frame_diff(a, b, thresh=25):
    """两帧灰度阈值差分。返回 (变化像素占比, [变化连通域框(x,y,w,h)...])。"""
    if a is None or b is None or a.shape != b.shape:
        return 1.0, []
    ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
    d = cv2.absdiff(ga, gb)
    mask = (d > thresh).astype(np.uint8)
    ratio = float(mask.mean())
    boxes = []
    if ratio > 0.0005:
        mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=2)
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        for i in range(1, n):
            x, y, w, h, area = stats[i]
            if area >= 30:
                boxes.append((int(x), int(y), int(w), int(h)))
    return ratio, boxes


def _裁(图, rect, region, region_ref):
    """把 region 换算进帧图坐标并裁剪。返回 (区域图, 区域内偏移[x,y])。"""
    h, w = 图.shape[:2]
    if region is None:
        return 图, (0, 0)
    x1, y1, x2, y2 = [int(round(float(v))) for v in region]
    if region_ref != "window":
        x1 -= rect[0]
        y1 -= rect[1]
        x2 -= rect[0]
        y2 -= rect[1]
    x1 = max(0, min(x1, w - 1))
    y1 = max(0, min(y1, h - 1))
    x2 = max(x1 + 1, min(x2, w))
    y2 = max(y1 + 1, min(y2, h))
    return 图[y1:y2, x1:x2], (x1, y1)


def _置前(hwnd, 重试=3, 等待=0.25):
    """Win32 置前，返回是否成功（前台锁/最小化可能失败，如实返回）。

    已在前台直接早退：probe_hang 对挂起窗口探测时，SetForegroundWindow
    对不处理消息的窗口可能失败，而窗口本来就该在前台——不白折腾。
    """
    hwnd = int(hwnd) & 0xFFFFFFFF
    for _ in range(重试):
        try:
            if (int(_USER32.GetForegroundWindow()) & 0xFFFFFFFF) == hwnd:
                return True
            _USER32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(等待)
    try:
        return (int(_USER32.GetForegroundWindow()) & 0xFFFFFFFF) == hwnd
    except Exception:
        return False


def _遮挡摘要(hwnd):
    try:
        from . import stacking
        occ = stacking.occlusion(hwnd)
        return {"visible_ratio": occ.get("visible_ratio"),
                "foreground": occ.get("foreground"),
                "top_blocker": occ["blockers"][0] if occ.get("blockers") else None}
    except Exception:
        return None


def _让路(开启=True):
    """进程优先级降 BELOW_NORMAL 让路（总案 §六：霸业挂机期有效）。

    返回原优先级供 finally 恢复——MCP 进程常驻，不能永久降档。
    """
    try:
        k32 = ctypes.windll.kernel32
        h = k32.GetCurrentProcess()
        原 = k32.GetPriorityClass(h)
        if 开启:
            k32.SetPriorityClass(h, _BELOW_NORMAL)
        return 原
    except Exception:
        return None


def _恢复优先级(原):
    if 原 is None:
        return
    try:
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), 原)
    except Exception:
        pass


# ---------------- 动作执行（连拍/一隐一显的前置动作）----------------

def _执行动作(actions):
    """按顺序执行动作列表，返回逐条结果。动作失败即停。

    支持：click（坐标物理点击，走 executor.click_xy 的落点归窗核验）、
    key（键序列）、paste（剪贴板粘贴，中文安全）、wait（等待秒数）。
    元素级点击（按编号）不走这里——agent 自己 click_element 后再调观察，
    或先 scan 取中心坐标，把坐标给 click 动作。
    """
    账 = []
    for 动作 in actions or []:
        if not isinstance(动作, dict):
            账.append({"ok": False, "error": "动作必须是 dict：%r" % (动作,)})
            break
        kind = str(动作.get("kind") or "").lower()
        try:
            if kind == "click":
                r = executor.click_xy(int(动作["x"]), int(动作["y"]),
                                      precise=bool(动作.get("precise")),
                                      top_must=动作.get("top_must"))
            elif kind == "key":
                executor.press_key(str(动作["key"]))
                r = {"ok": True, "method": "key", "key": str(动作["key"])}
            elif kind == "paste":
                import pyautogui
                import pyperclip
                text = str(动作.get("text", ""))
                pyperclip.copy(text)
                pyautogui.hotkey("ctrl", "v")
                r = {"ok": True, "method": "paste", "chars": len(text)}
            elif kind == "wait":
                time.sleep(float(动作.get("seconds", 0.3)))
                r = {"ok": True, "method": "wait"}
            else:
                r = {"ok": False, "error": "未知动作 kind=%r（支持 click/key/paste/wait）" % kind}
        except Exception as e:
            r = {"ok": False, "error": "%s 动作失败：%s" % (kind or "未知", e)}
        账.append(r)
        if not r.get("ok"):
            break
    return 账


# ---------------- 连拍底座 ----------------

def _连拍(hwnd, 基线, 基线rect, region, region_ref, seconds, interval,
          thresh=25, mode="printwindow", on_frame=None, keep_img=False):
    """从基线出发连拍 seconds 秒，返回帧账。

    每帧同时记两种变化率：ratio=相对上一帧（瞬时变化，POC v2 连拍口径），
    ratio_base=相对基线（持续变化，一隐一显口径）。区域形状变了（窗口
    尺寸变化）按 1.0 如实记。
    """
    基区, 区偏移 = _裁(基线, 基线rect, region, region_ref)
    账 = []
    上一帧 = None
    t0 = time.time()
    while True:
        已过 = time.time() - t0
        if 已过 >= seconds:
            break
        time.sleep(max(0.0, min(interval, seconds - 已过)))
        img, rect, 回退 = grab(hwnd, mode)
        帧, _ = _裁(img, rect, region, region_ref)
        if 帧.shape != 基区.shape:
            r_prev, r_base, boxes = 1.0, 1.0, []
        else:
            r_base, boxes = frame_diff(基区, 帧, thresh)
            r_prev = frame_diff(上一帧, 帧, thresh)[0] if 上一帧 is not None else r_base
        条目 = {"t": round(time.time() - t0, 3),
                "ratio": round(float(r_prev), 5),
                "ratio_base": round(float(r_base), 5),
                "boxes": boxes, "fallback": bool(回退)}
        if keep_img:
            条目["img"] = img
            条目["rect"] = rect
        账.append(条目)
        上一帧 = 帧
        if on_frame:
            on_frame(条目, 账)
    return 账, 区偏移


def _对外账(账):
    return [{k: v for k, v in f.items() if k not in ("img", "rect")} for f in 账]


def _保存帧(帧账, 目录, 前缀):
    """保存带 img 的帧（仅命中帧），返回 artifact 路径列表。"""
    os.makedirs(目录, exist_ok=True)
    路径 = []
    for i, f in enumerate(帧账):
        if "img" not in f:
            continue
        输出 = os.path.join(目录, "%s_%02d_t%.2f.png" % (前缀, i + 1, f["t"]))
        capture.save_image(f["img"], 输出)
        路径.append(输出)
    return 路径


# ---------------- 能力一：连拍窗（toast / 飘字）----------------

def watch_burst(hwnd=None, window=None, seconds=3.0, interval=0.2, region=None,
                region_ref="screen", actions=None, rebase=True, thresh=25,
                hit_ratio=0.004, mode="printwindow", save_dir=None):
    """连拍窗：动作后 0~seconds 秒逐帧差分，报告瞬时反馈（toast/飘字）。

    rebase=True（默认）：动作执行后重抓基线，把点击浮雕等动作自身视觉反馈
    吸收进基线，只报动作之后的新变化（POC v2 口径）。
    actions：见 _执行动作；为空=纯观察（基线=调用时刻）。
    save_dir：给定则把命中帧 PNG 存到该目录并返回 artifacts。
    """
    try:
        hwnd = _解析窗(hwnd, window)
        if mode == "printwindow" and _挂着(hwnd):
            return {"ok": False, "action": "watch_burst",
                    "error": "目标窗口当前无响应（IsHungAppWindow=True）：PrintWindow "
                             "对挂起窗会永久阻塞。卡死场景请用 probe_hang；"
                             "非卡死误报可改 mode='screen'（窗口须在前台无遮挡）。"}
        基线, rect, 回退 = grab(hwnd, mode)
        动作账 = _执行动作(actions)
        if 动作账 and not 动作账[-1].get("ok"):
            return {"ok": False, "action": "watch_burst",
                    "error": "前置动作失败：" + str(动作账[-1].get("error")),
                    "actions": 动作账}
        if rebase and actions:
            基线, rect, 回退 = grab(hwnd, mode)
        账, 区偏移 = _连拍(hwnd, 基线, rect, region, region_ref, seconds,
                          interval, thresh, mode, keep_img=bool(save_dir))
        # fallback 帧（PrintWindow 黑帧退屏幕）的内容可信度低，只计数不判命中
        命中 = [f for f in 账 if f["ratio"] > hit_ratio and not f["fallback"]]
        首 = 命中[0]["t"] if 命中 else None
        末 = 命中[-1]["t"] if 命中 else None
        最大 = max([f["ratio"] for f in 账], default=0.0)
        结果 = {"ok": True, "action": "watch_burst", "window_hwnd": hwnd,
                "seconds": seconds, "interval": interval,
                "frames": len(账), "hit_frames": len(命中),
                "first_hit_t": 首, "last_hit_t": 末,
                "max_ratio": round(float(最大), 5),
                "fallback_frames": sum(1 for f in 账 if f["fallback"]),
                "region_offset": list(区偏移),
                "ledger": _对外账(账),
                "hint": ("命中 %d 帧，首个变化 @%ss；boxes 为变化连通域（region 内坐标）。"
                         % (len(命中), 首) if 命中 else
                         "观察窗内未检测到变化（ratio 均≤%.4f）。" % hit_ratio)}
        if save_dir and 命中:
            结果["artifacts"] = _保存帧([f for f in 账 if f["ratio"] > hit_ratio],
                                        save_dir, "watch_hit")
        return 结果
    except Exception as e:
        return {"ok": False, "action": "watch_burst", "error": str(e)}


# ---------------- 能力二：一隐一显打字验证 ----------------

def verify_typing(hwnd=None, window=None, region=None, region_ref="screen",
                  actions=None, wait=1.5, interval=0.25, thresh=25,
                  change_ratio=0.01, mode="printwindow", ocr_check=False):
    """一隐一显：动作前抓基线，动作后 wait 秒观察 region 是否出现变化。

    与 watch_burst 的区别（POC v3 口径）：基线在动作**前**——动作本身
    （点输入框/粘贴）不该被算作反馈，只有区域内容真的变了才算「显示」。
    回显区常与输入框同窗：region 建议给回显区 bbox（屏幕坐标），别给全窗。

    ocr_check=True 时对基线与末帧的 region 做一次 OCR，返回变化前后文本，
    给 agent 一个人能读的证据（慢 1~3 秒，默认关）。
    """
    try:
        hwnd = _解析窗(hwnd, window)
        if mode == "printwindow" and _挂着(hwnd):
            return {"ok": False, "action": "verify_typing",
                    "error": "目标窗口当前无响应：PrintWindow 会永久阻塞；"
                             "请改用 probe_hang 或 mode='screen'。"}
        基线, rect, 回退 = grab(hwnd, mode)
        基区, 区偏移 = _裁(基线, rect, region, region_ref)
        if actions:
            动作账 = _执行动作(actions)
            if not 动作账[-1].get("ok"):
                return {"ok": False, "action": "verify_typing",
                        "error": "前置动作失败：" + str(动作账[-1].get("error")),
                        "actions": 动作账}
        else:
            动作账 = []
        账, _ = _连拍(hwnd, 基线, rect, region, region_ref, wait, interval,
                     thresh, mode, keep_img=bool(ocr_check))
        变化 = [f for f in 账 if f["ratio_base"] > change_ratio]
        首 = 变化[0]["t"] if 变化 else None
        最大 = max([f["ratio_base"] for f in 账], default=0.0)
        结果 = {"ok": True, "action": "verify_typing", "window_hwnd": hwnd,
                "changed": bool(变化), "first_change_t": 首,
                "max_ratio": round(float(最大), 5), "frames": len(账),
                "region_offset": list(区偏移),
                "actions": 动作账,
                "ledger": _对外账(账),
                "hint": ("区域内出现变化（首个 @%ss，最大变化占比 %.4f）→ 反馈成立。"
                         % (首, 最大) if 变化 else
                         "区域内零变化（最大变化占比 %.4f ≤ %.4f）→ 未见反馈。"
                         % (最大, change_ratio))}
        if ocr_check:
            def 文本(img):
                try:
                    return [e.text for e in ocr_engine.scan_ocr(img, min_conf=0.5)][:30]
                except Exception as e:
                    return ["<OCR失败: %s>" % e]
            结果["ocr"] = {"before": 文本(基区),
                          "after": 文本(账[-1]["img"]) if 账 and "img" in 账[-1] else []}
        return 结果
    except Exception as e:
        return {"ok": False, "action": "verify_typing", "error": str(e)}


# ---------------- 能力三：ESC 卡死探测 ----------------

def probe_hang(hwnd=None, window=None, key="esc", wait=2.0, interval=0.25,
               resend_after=0.7, settle=0.4, change_ratio=0.004, focus=True):
    """发键短窗观察：内容有变化=窗口活着，零变化=判卡死。

    通道纪律（POC 实锤）：必须走屏幕拷贝——挂起窗口不处理 WM_PRINT，
    PrintWindow 调用会永久阻塞。因此本函数要求窗口在前台且无遮挡：
    focus=True 先置前；置前失败或可见率 <0.95 时如实拒绝判定（判定不可信
    比不判定更糟）。按键是全局键盘事件，只发到前台窗口。

    resend_after：首轮无变化后补发一发（防单发丢失，POC 健康轮行为）；
    0=不补发。is_hung_api 是 IsHungAppWindow 旁证（恢复后可滞后，不作准据）。
    """
    try:
        hwnd = _解析窗(hwnd, window)
        前台 = _置前(hwnd) if focus else (
            (int(_USER32.GetForegroundWindow()) & 0xFFFFFFFF) == hwnd)
        if not 前台:
            return {"ok": False, "action": "probe_hang",
                    "error": "目标窗口不在前台且置前失败：按键会发到别的窗口，"
                             "卡死判定不可信。请先 activate 并清障再探测。",
                    "window_hwnd": hwnd}
        遮挡 = _遮挡摘要(hwnd)
        if 遮挡 and (遮挡.get("visible_ratio") or 1.0) < 0.95:
            return {"ok": False, "action": "probe_hang",
                    "error": "窗口可见率 %s（<0.95）：屏幕拷贝会混入遮挡内容，"
                             "先置前/挪开遮挡再探测。" % 遮挡.get("visible_ratio"),
                    "occlusion": 遮挡}
        time.sleep(max(0.0, float(settle)))
        rect = capture.window_rect_hwnd(hwnd)
        基线 = capture.shot_region(rect[0], rect[1], rect[2], rect[3])
        executor.press_key(key)
        补发 = [False]
        账 = []
        t0 = time.time()
        while time.time() - t0 < wait:
            time.sleep(max(0.0, min(interval, wait - (time.time() - t0))))
            cur = capture.shot_region(rect[0], rect[1], rect[2], rect[3])
            ratio, boxes = frame_diff(基线, cur)
            条目 = {"t": round(time.time() - t0, 3), "ratio": round(float(ratio), 5),
                    "boxes": boxes}
            账.append(条目)
            if ratio > change_ratio:
                break
            if (not 补发[0]) and resend_after and 条目["t"] >= resend_after:
                executor.press_key(key)
                补发[0] = True
        最大 = max([f["ratio"] for f in 账], default=0.0)
        有变化 = any(f["ratio"] > change_ratio for f in 账)
        卡死API = _挂着(hwnd)
        结果 = {"ok": True, "action": "probe_hang", "window_hwnd": hwnd,
                "hang": not 有变化, "changed": 有变化,
                "max_ratio": round(float(最大), 5), "frames": len(账),
                "key": key, "resent": 补发[0],
                "is_hung_api": 卡死API, "occlusion": 遮挡,
                "ledger": 账,
                "hint": ("发键后内容有变化（最大 %.4f）→ 窗口有响应，未卡死。"
                         % 最大 if 有变化 else
                         "发键后 %.1fs 内容零变化（最大 %.4f）→ 判卡死；"
                         "IsHungAppWindow=%s（旁证，恢复后可滞后）。"
                         % (wait, 最大, 卡死API))}
        return 结果
    except Exception as e:
        return {"ok": False, "action": "probe_hang", "error": str(e)}


# ---------------- 能力四：抠底动静态分层 ----------------

def _动态块(dyn, min_area=60, dilate_iter=2):
    """动态掩码 → 实体块 [(x,y,w,h,area)]，按面积降序。"""
    m = cv2.dilate(dyn, np.ones((5, 5), np.uint8), iterations=dilate_iter)
    n, _, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    blocks = [tuple(int(v) for v in stats[i]) for i in range(1, n) if stats[i][4] >= min_area]
    blocks.sort(key=lambda b: -b[4])
    return blocks


def _块中心(b):
    x, y, w, h = b[:4]
    return x + w / 2.0, y + h / 2.0


def _跟踪块(blocks_per_frame, max_gap=45.0, min_hits=3):
    """跨帧中心近邻跟踪（POC 原实现）。返回 hits≥min_hits 的轨迹。

    轨迹 = {hits, positions: [(fx,cx,cy)], boxes: [(x,y,w,h)], dist,
            net_dist}。net_dist=首末直线距离——净位移分实体/动画层的关键轴。
    """
    tracks = []
    for fx, blocks in blocks_per_frame:
        used = set()
        for tr in tracks:
            if tr["last_f"] != fx - 1:
                continue
            px, py = tr["positions"][-1][1], tr["positions"][-1][2]
            best, best_d = None, max_gap
            for i, b in enumerate(blocks):
                if i in used:
                    continue
                cx, cy = _块中心(b)
                d = ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5
                if d <= best_d:
                    best, best_d = i, d
            if best is not None:
                used.add(best)
                cx, cy = _块中心(blocks[best])
                tr["positions"].append((fx, cx, cy))
                tr["boxes"].append(tuple(int(v) for v in blocks[best][:4]))
                tr["last_f"] = fx
                tr["hits"] += 1
        for i, b in enumerate(blocks):
            if i in used:
                continue
            cx, cy = _块中心(b)
            tracks.append({"hits": 1, "last_f": fx,
                           "positions": [(fx, cx, cy)],
                           "boxes": [tuple(int(v) for v in b[:4])], "dist": 0.0})
    for tr in tracks:
        p = tr["positions"]
        tr["dist"] = sum(((p[k + 1][1] - p[k][1]) ** 2 + (p[k + 1][2] - p[k][2]) ** 2) ** 0.5
                         for k in range(len(p) - 1))
        tr["net_dist"] = ((p[-1][1] - p[0][1]) ** 2 + (p[-1][2] - p[0][2]) ** 2) ** 0.5
        tr.pop("last_f")
    return [t for t in tracks if t["hits"] >= min_hits]


def _实体掩码(shape, mover_tracks):
    m = np.zeros(shape[:2], np.uint8)
    for tr in mover_tracks:
        for (x, y, w, h) in tr["boxes"]:
            cv2.rectangle(m, (x, y), (x + w, y + h), 1, -1)
    return m


def _框重叠率(mask, box):
    x, y, w, h = [int(v) for v in box]
    hh, ww = mask.shape
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(ww, x + w), min(hh, y + h)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return float(mask[y0:y1, x0:x1].mean())


def _ocr框(img, min_conf=0.5):
    try:
        return [(e.bbox[0], e.bbox[1], e.bbox[2] - e.bbox[0], e.bbox[3] - e.bbox[1],
                 e.text, float(e.conf))
                for e in ocr_engine.scan_ocr(img, min_conf=min_conf)]
    except Exception:
        return []


def _近邻保留率(ref_boxes, cur_boxes, tol=15):
    """参照框在当前 OCR 中的中心近邻保留率（POC 判据①口径）。

    返回 (保留率, 丢失框文本列表)——丢失明细给诊断用，别只报个率。
    2026-11-01 实测补丁：同一行文字参照帧被 OCR 识别成 1 框、底图识别成
    2 框时，中心近邻会误判「丢失」（tk 靶「攻击 123 防御 456」实测复现）。
    加空间包含兜底——底图框中心落在参照框内即该位置有文字，算保留；
    这是对 OCR 切分差异的测量鲁棒化，不是放宽内容要求。
    """
    if not ref_boxes:
        return 1.0, []
    hit = 0
    missing = []
    for (rx, ry, rw, rh, 文本, _c) in ref_boxes:
        rcx, rcy = rx + rw / 2.0, ry + rh / 2.0
        命中 = False
        for (cx, cy, cw, ch, _t2, _c2) in cur_boxes:
            if abs(cx + cw / 2.0 - rcx) <= tol and abs(cy + ch / 2.0 - rcy) <= tol:
                命中 = True
                break
        if not 命中:
            for (cx, cy, cw, ch, _t2, _c2) in cur_boxes:
                中心x, 中心y = cx + cw / 2.0, cy + ch / 2.0
                if rx + 2 <= 中心x <= rx + rw - 2 and ry + 2 <= 中心y <= ry + rh - 2:
                    命中 = True
                    break
        if not 命中:
            # 反向切分（参照帧 2 框、底图合并成 1 框）：文本归一后互含且框相交
            归一 = lambda s: "".join(str(s).split())
            rt = 归一(文本)
            for (cx, cy, cw, ch, t2, _c2) in cur_boxes:
                ct = 归一(t2)
                if not rt or not ct:
                    continue
                if (rt in ct or ct in rt) and not (
                        cx + cw <= rx or rx + rw <= cx or
                        cy + ch <= ry or ry + rh <= cy):
                    命中 = True
                    break
        if 命中:
            hit += 1
        else:
            missing.append(文本)
    return hit / len(ref_boxes), missing


def split_static_dynamic(hwnd=None, window=None, frames=8, interval=0.5,
                         std_thresh=12.0, min_area=60, min_hits=3,
                         moving_min_dist=15.0, verify_ocr=False, out_dir=None,
                         mode="printwindow"):
    """多帧抠底 + 动静态分层。

    静止=UI / 变化=实体（设计原则 5）：逐像素时间轴中位数得 UI 底图（偶发
    动态物被中位数滤掉、静止 UI 全保留），时间轴标准差得动态掩码；跨帧跟踪
    后按**净位移**分层——净位移大=实体移动（怪/玩家），净位移≈0=原位动画层
    （呼吸灯/跳秒/聊天滚动，POC 首轮误收根源即只看累计位移）。

    POC 复现口径：frames=10, interval=1.0, std_thresh=12, min_hits=3,
    moving_min_dist=15（雷电真靶成绩见模块头）。
    verify_ocr=True 时附 POC 判据①的量化值（UI 文本保留率/稳定框误收），
    多花 2~4 秒（两帧 OCR），默认关。
    """
    原优先级 = None
    try:
        hwnd = _解析窗(hwnd, window)
        if mode == "printwindow" and _挂着(hwnd):
            return {"ok": False, "action": "split_static_dynamic",
                    "error": "目标窗口当前无响应：PrintWindow 会永久阻塞；"
                             "请等窗口恢复，或改 mode='screen'（前台无遮挡）。"}
        frames = max(2, int(frames))
        原优先级 = _让路(True)
        t0 = time.time()
        帧序 = []
        for i in range(frames):
            img, rect, 回退 = grab(hwnd, mode)
            帧序.append({"t": round(time.time() - t0, 3), "img": img,
                        "rect": rect, "fallback": 回退})
            if i < frames - 1:
                time.sleep(max(0.0, float(interval)))
        形状 = 帧序[0]["img"].shape
        if any(f["img"].shape != 形状 for f in 帧序):
            return {"ok": False, "action": "split_static_dynamic",
                    "error": "采样期间窗口尺寸变化，多帧不可叠（先固定窗口再采样）"}
        imgs = [f["img"] for f in 帧序]
        stack = np.stack(imgs).astype(np.float32)
        ui_base = np.median(stack, axis=0).astype(np.uint8)          # UI 底图
        灰度栈 = np.stack([cv2.cvtColor(i, cv2.COLOR_BGR2GRAY) for i in imgs]).astype(np.float32)
        std = 灰度栈.std(axis=0)
        dyn = (std > float(std_thresh)).astype(np.uint8)             # 动态掩码
        灰度底 = cv2.cvtColor(ui_base, cv2.COLOR_BGR2GRAY)
        blocks_per_frame = []
        for fx, img in enumerate(imgs):
            m = (cv2.absdiff(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 灰度底)
                 > float(std_thresh)).astype(np.uint8)
            blocks_per_frame.append((fx, _动态块(m, min_area)))
        tracks = _跟踪块(blocks_per_frame, max_gap=45.0, min_hits=int(min_hits))
        movers = [t for t in tracks if t["net_dist"] >= float(moving_min_dist)]
        anims = [t for t in tracks if t["net_dist"] < float(moving_min_dist)]
        movers.sort(key=lambda x: -x["net_dist"])

        输出目录 = out_dir or os.path.join(_数据目录, "watch_%s" % time.strftime("%H%M%S"))
        os.makedirs(输出目录, exist_ok=True)
        artifacts = {"ui_base": os.path.join(输出目录, "ui_base.png"),
                     "dyn_mask": os.path.join(输出目录, "dyn_mask.png"),
                     "annotated": os.path.join(输出目录, "annotated.png")}
        capture.save_image(ui_base, artifacts["ui_base"])
        capture.save_image(dyn * 255, artifacts["dyn_mask"])
        ui_boxes = []
        ocr账 = None
        if verify_ocr:
            ref_ocr = _ocr框(帧序[0]["img"])
            ui_ocr = _ocr框(ui_base)
            keep, missing = _近邻保留率(ref_ocr, ui_ocr)
            last_ocr = _ocr框(帧序[-1]["img"])
            稳定 = [b for b in ref_ocr
                    if b[5] >= 0.85 and any(c[4] == b[4]
                                           and abs(c[0] - b[0]) <= 5
                                           and abs(c[1] - b[1]) <= 5 for c in last_ocr)]
            emask = _实体掩码(形状, movers)
            rates = [(b[4][:16], float(_框重叠率(emask, b[:4]))) for b in 稳定]
            rates.sort(key=lambda x: -x[1])
            med_rate = float(np.median([r for _t, r in rates])) if rates else 0.0
            bad = [(t, round(r, 3)) for t, r in rates if r > 0.30]
            ocr账 = {"ui_keep_rate": round(float(keep), 4),
                     "ref_boxes": len(ref_ocr),
                     "missing": missing[:8],
                     "stable_boxes": len(稳定),
                     "median_entity_ratio": round(med_rate, 4),
                     "bad_boxes": bad[:8],
                     "bad_count": len(bad)}
            ui_boxes = [(b[0], b[1], b[2], b[3]) for b in ref_ocr]
        动块 = _动态块(dyn, min_area)
        ann = ui_base.copy()
        for (x, y, w, h) in ui_boxes:
            cv2.rectangle(ann, (x, y), (x + w, y + h), (255, 160, 0), 1)
        for b in 动块:
            x, y, w, h = b[:4]
            cv2.rectangle(ann, (x, y), (x + w, y + h), (0, 0, 255), 2)
        for tr in tracks:
            p = tr["positions"]
            for k in range(len(p) - 1):
                cv2.line(ann, (int(p[k][1]), int(p[k][2])),
                         (int(p[k + 1][1]), int(p[k + 1][2])), (0, 255, 255), 1)
        capture.save_image(ann, artifacts["annotated"])
        h, w = 形状[:2]
        return {"ok": True, "action": "split_static_dynamic", "window_hwnd": hwnd,
                "frames": frames, "interval": interval, "frame_size": [w, h],
                "duration_s": round(time.time() - t0, 2),
                "dyn_ratio": round(float(dyn.mean()), 5),
                "blocks": len(动块),
                "tracks": len(tracks), "stable_movers": len(movers),
                "anims": len(anims),
                "movers": [{"hits": t["hits"], "net_dist": round(t["net_dist"], 1),
                            "dist": round(t["dist"], 1), "boxes": t["boxes"]}
                           for t in movers[:8]],
                "fallback_frames": sum(1 for f in 帧序 if f["fallback"]),
                "artifacts": artifacts,
                "ocr": ocr账,
                "occlusion": _遮挡摘要(hwnd),
                "hint": ("UI 底图=%s（静止层）；movers 为净位移≥%.0fpx 的实体轨迹；"
                         "anims=%d 条原位动画层。verify_ocr 口径见 POC 判据①。"
                         % (artifacts["ui_base"], moving_min_dist, len(anims)))}
    except Exception as e:
        return {"ok": False, "action": "split_static_dynamic", "error": str(e)}
    finally:
        _恢复优先级(原优先级)
