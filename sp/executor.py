# -*- coding: utf-8 -*-
"""动作执行模块：click / type / key / scroll。

原则：UIA 控件级调用优先（更稳），失败或非 UIA 来源一律物理坐标兜底；
中文输入必须走剪贴板粘贴（pyautogui.write 不支持中文）。

漂移点击：物理点击默认不做像素级精准落点——落点在元素中心附近做高斯随机
偏移（限制在 bbox 内缩安全区内，绝不点出元素），且移动带随机时长拖尾，
模拟人手不精准的操作轨迹；precise=True（或环境变量 SP_NO_JITTER=1）恢复
像素级精准。UIA 控件级调用不经鼠标，不受影响。
"""
import os
import random
import time

import pyautogui
import pyperclip
import uiautomation as auto

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.05  # 每个基础动作后的短暂间隔

# 漂移开关与幅度倍率：SP_NO_JITTER=1 全局关闭；SP_JITTER 调幅度（默认 1.0）
_JITTER_OFF = os.environ.get("SP_NO_JITTER", "") == "1"
_JITTER_SCALE = float(os.environ.get("SP_JITTER", "1.0") or 1.0)

# 中文键名 -> pyautogui 键名映射
KEY_MAP = {
    "回车": "enter", "换行": "enter", "确认": "enter", "enter": "enter",
    "esc": "esc", "escape": "esc", "退出": "esc", "取消": "esc",
    "tab": "tab", "制表": "tab",
    "空格": "space", "space": "space",
    "退格": "backspace", "backspace": "backspace",
    "删除": "delete", "delete": "delete",
    "上": "up", "上箭头": "up", "↑": "up", "up": "up",
    "下": "down", "下箭头": "down", "↓": "down", "down": "down",
    "左": "left", "左箭头": "left", "←": "left", "left": "left",
    "右": "right", "右箭头": "right", "→": "right", "right": "right",
    "home": "home", "end": "end",
    "上翻页": "pageup", "pageup": "pageup",
    "下翻页": "pagedown", "pagedown": "pagedown",
    "ctrl": "ctrl", "alt": "alt", "shift": "shift", "win": "win",
}


def _map_key(名):
    """中文/别名键名映射；未知键名原样透传给 pyautogui。"""
    return KEY_MAP.get(名.strip().lower(), 名.strip())


def _control_at(x, y):
    """取屏幕物理坐标点上的 UIA 控件，失败返回 None。"""
    try:
        return auto.ControlFromPoint(int(x), int(y))
    except Exception:
        return None


def _bbox近似(a, b, iou阈值=0.6):
    """两个 bbox 的 IoU 是否超阈值（后台重定位控件时的同位判定）。"""
    al, at, ar, ab = a
    bl, bt, br, bb = b
    il, it = max(al, bl), max(at, bt)
    ir, ib = min(ar, br), min(ab, bb)
    if ir <= il or ib <= it:
        return False
    交 = (ir - il) * (ib - it)
    并 = (ar - al) * (ab - at) + (br - bl) * (bb - bt) - 交
    return 并 > 0 and 交 / float(并) > iou阈值


def _控件重定位(元素):
    """按元素所属窗口句柄在控件树内按 bbox 重找控件。

    被遮挡窗口的 ControlFromPoint 会拿到遮挡物的控件（物理坐标落在最顶层
    窗口上），此路线走 UIA 树后台遍历（UIA 读树不受遮挡影响），供后台操作。
    """
    hwnd = int(getattr(元素, "hwnd", 0) or 0) & 0xFFFFFFFF
    if not hwnd:
        return None
    try:
        root = auto.ControlFromHandle(hwnd)
    except Exception:
        return None
    if root is None:
        return None
    from collections import deque
    队列 = deque([(root, 0)])
    while 队列:
        c, d = 队列.popleft()
        if d > 14:
            break
        try:
            r = c.BoundingRectangle
            bbox = [int(r.left), int(r.top), int(r.right), int(r.bottom)]
        except Exception:
            bbox = None
        if bbox and bbox[2] - bbox[0] > 1 and bbox[3] - bbox[1] > 1 \
                and _bbox近似(bbox, list(元素.bbox)):
            return c
        try:
            child = c.GetFirstChildControl()
            while child:
                队列.append((child, d + 1))
                child = child.GetNextSiblingControl()
        except Exception:
            pass
    return None


def _try_invoke链(ctrl):
    """对控件及其向上 2 层父级尝试控件级动作，返回 (成功, 方法名)。"""
    node = ctrl
    尝试层数 = 0
    while node is not None and 尝试层数 < 3:
        成功, 方法 = _尝试控件动作(node)
        if 成功:
            return True, 方法
        try:
            node = node.GetParentControl()
        except Exception:
            break
        尝试层数 += 1
    return False, None


def _尝试控件动作(ctrl):
    """对控件尝试 Invoke -> Toggle -> LegacyIAccessible 默认动作。

    返回 (是否成功, 方法名)。
    """
    try:
        inv = ctrl.GetPattern(auto.PatternId.InvokePattern)
        if inv:
            inv.Invoke()
            return True, "uia_invoke"
    except Exception:
        pass
    try:
        tog = ctrl.GetPattern(auto.PatternId.TogglePattern)
        if tog:
            tog.Toggle()
            return True, "uia_toggle"
    except Exception:
        pass
    try:
        lap = ctrl.GetPattern(auto.PatternId.LegacyIAccessiblePattern)
        if lap is not None:
            try:
                if lap.CurrentDefaultAction:
                    lap.DoDefaultAction()
                    return True, "uia_default_action"
            except Exception:
                pass
    except Exception:
        pass
    return False, None


def _漂移点(bbox):
    """在 bbox 内取「人手感」落点：中心附近高斯偏移，内缩安全区防出界。

    小元素（宽或高 <12px）不漂移（漂了也容易点出界）。
    """
    l, t, r, b = bbox
    w, h = r - l, b - t
    cx, cy = (l + r) // 2, (t + b) // 2
    if w < 12 or h < 12 or _JITTER_OFF or _JITTER_SCALE <= 0:
        return int(cx), int(cy)
    # σ 与元素尺寸挂钩：大按钮漂得多、小按钮漂得少；上限受内缩 25% 的安全区约束
    sigma = min(w, h) * 0.15 * _JITTER_SCALE
    mx = max(1, int(w * 0.25))
    my = max(1, int(h * 0.25))
    dx = max(-mx, min(mx, random.gauss(0, sigma)))
    dy = max(-my, min(my, random.gauss(0, sigma)))
    return int(cx + dx), int(cy + dy)


def _物理点击(x, y, 拖尾=True):
    """物理点击：默认带随机时长移动拖尾（拟人轨迹），失败抛异常由调用方处理。"""
    if 拖尾 and not _JITTER_OFF:
        pyautogui.moveTo(x, y, duration=random.uniform(0.06, 0.20))
        pyautogui.click()
    else:
        pyautogui.click(x, y)


def click(元素, force_physical=False, precise=False, check_top=True):
    """点击元素。UIA 来源先试控件级调用（含向上最多 2 层父级），物理点击兜底。

    force_physical=True 时跳过 UIA 控件级调用直接物理点击
    （适用于 XAML 下拉按钮等 Invoke 不展开菜单的场景）。
    precise=False（默认）时物理点击带漂移落点与移动拖尾（不过度精准）。

    遮挡保护：ControlFromPoint 拿到的控件若不属于元素所属窗口（说明该坐标
    被别的窗口盖着，控件是遮挡物的），不用它，改走窗口树内 bbox 重定位
    （UIA 后台遍历）——防止误触发遮挡物。

    落点归窗核验（2026-10-28 堆叠感知层，根治「点了也不知道点没点到」）：
    物理点击永远打在最顶层窗口（铁律19）。check_top=True（默认）时，
    元素带 hwnd 的物理点击在发点前先 WindowFromPoint 归根比对——归窗不是
    目标窗就拒绝点击（ok=False + top_window 说明谁挡着），把「点完才发现
    点错」变成「点前拦截」。check_top=False 是明确知情时的强点逃生口。
    """
    cx, cy = 元素.center()
    if 元素.source == "uia" and not force_physical:
        ctrl = _control_at(cx, cy)
        if ctrl is not None:
            try:
                顶 = ctrl.GetTopLevelControl()
                顶句柄 = int(getattr(顶, "NativeWindowHandle", 0) or 0) & 0xFFFFFFFF
                if 元素.hwnd and 顶句柄 != (int(元素.hwnd) & 0xFFFFFFFF):
                    ctrl = None  # 坐标被遮挡：拿到的控件属于别的窗口，弃用
            except Exception:
                pass
        if ctrl is not None:
            成功, 方法 = _try_invoke链(ctrl)
            if 成功:
                return {"ok": True, "method": 方法, "at": [cx, cy]}
        # 后台路线：窗口树内按 bbox 重定位控件再试（被遮挡也能操作）
        后台控件 = _控件重定位(元素)
        if 后台控件 is not None:
            成功, 方法 = _try_invoke链(后台控件)
            if 成功:
                return {"ok": True, "method": 方法, "at": [cx, cy], "background": True}
    # 物理点击兜底（pyautogui，物理像素坐标）；默认漂移落点，precise 恢复精准
    落点 = (cx, cy) if precise else _漂移点(元素.bbox)
    # 事前拦截：落点最顶层窗口不是目标窗 = 点下去命中的是遮挡物
    if check_top and getattr(元素, "hwnd", 0):
        from . import stacking
        try:
            遮挡者 = stacking.ensure_top_at(落点[0], 落点[1], 元素.hwnd)
        except Exception:
            遮挡者 = None
        if 遮挡者 is not None:
            return {"ok": False, "method": "blocked", "at": [落点[0], 落点[1]],
                    "top_window": 遮挡者,
                    "error": ("落点最顶层窗口是「%s」，目标窗被遮挡——物理点击会"
                              "点到遮挡物。先 activate_window 置前目标窗再点，"
                              "或确需强点用 check_top=False。"
                              % (遮挡者.get("title") or 遮挡者.get("hwnd", "?")))}
    try:
        _物理点击(落点[0], 落点[1], 拖尾=not precise)
        结果 = {"ok": True, "method": "pyautogui_click", "at": [落点[0], 落点[1]],
                "drift": [落点[0] - cx, 落点[1] - cy]}
        if not getattr(元素, "hwnd", 0):
            结果["hit_window"] = _落点归窗(落点)  # 无 hwnd 元素：事后反馈落在谁身上
        return 结果
    except Exception as e:
        return {"ok": False, "method": "none", "at": [落点[0], 落点[1]], "error": str(e)}


def _落点归窗(落点):
    """点击后反馈：落点最顶层窗口是谁（OCR/视觉来源元素无 hwnd，靠它事后判）。"""
    try:
        from . import stacking
        顶 = stacking.who_at(落点[0], 落点[1])
        if 顶:
            return {"hwnd": 顶["hwnd"], "title": 顶["title"]}
    except Exception:
        pass
    return None


def click_xy(x, y, precise=False, top_must=None):
    """按屏幕物理坐标直接点击——元素表兜底通道。

    元素表漏识的目标（如游戏自绘 UI 的 X 关闭按钮）：agent 从 snap 截图或
    标注图读出目标像素坐标后直点。坐标系与元素表一致（物理像素，DPI 感知）。
    默认漂移落点（以坐标为中心的 20px 虚拟框，手感与元素点击一致）；
    precise=True 恢复像素级精准。

    堆叠感知（2026-10-28）：坐标点击命中「最顶层窗口」——
    - 结果一律附 hit_window（落点归根窗），点完即知打到谁；
    - top_must="标题子串" 事前拦截：落点最顶层窗标题不含该子串就拒绝点击
      （防拿后台抓窗图的坐标点到前台遮挡物上）。
    """
    x, y = int(x), int(y)
    if precise or _JITTER_OFF:
        落点 = (x, y)
    else:
        半 = 10
        落点 = _漂移点([x - 半, y - 半, x + 半, y + 半])
    命中窗 = _落点归窗(落点)
    if top_must:
        标题 = (命中窗 or {}).get("title", "")
        if top_must not in 标题:
            return {"ok": False, "method": "blocked", "at": [落点[0], 落点[1]],
                    "hit_window": 命中窗,
                    "error": ("落点最顶层窗口是「%s」，不含要求子串「%s」——已拦截，"
                              "未点击。窗口遮挡时先 activate 目标窗。"
                              % (标题 or "无窗口", top_must))}
    try:
        _物理点击(落点[0], 落点[1], 拖尾=not precise)
        return {"ok": True, "method": "pyautogui_click_xy",
                "at": [落点[0], 落点[1]],
                "drift": [落点[0] - x, 落点[1] - y],
                "hit_window": 命中窗}
    except Exception as e:
        return {"ok": False, "method": "none", "at": [落点[0], 落点[1]], "error": str(e)}


def type_text(元素, text, precise=False):
    """向元素输入文本。

    - UIA edit/combobox 且纯 ASCII：优先 ValuePattern.SetValue（会覆盖原内容）；
    - 含中文或上述失败：点击聚焦 + 剪贴板 Ctrl+V 粘贴（中文唯一可靠路线）。
    precise 透传给聚焦点击（False=默认漂移点击聚焦）。
    """
    结果 = {"ok": False, "method": None, "text_len": len(text)}

    # 路线一：ValuePattern 直接设值（仅 ASCII，中文必须走剪贴板）
    if 元素 is not None and 元素.source == "uia" and 元素.type in ("edit", "combobox") and text.isascii():
        cx, cy = 元素.center()
        ctrl = _control_at(cx, cy)
        if ctrl is not None:
            try:
                顶 = ctrl.GetTopLevelControl()
                顶句柄 = int(getattr(顶, "NativeWindowHandle", 0) or 0) & 0xFFFFFFFF
                if 元素.hwnd and 顶句柄 != (int(元素.hwnd) & 0xFFFFFFFF):
                    ctrl = None  # 坐标被遮挡：控件属于别的窗口，弃用防误写
            except Exception:
                pass
        if ctrl is None:
            ctrl = _控件重定位(元素)  # 被遮挡：窗口树内后台重定位
        if ctrl is not None:
            node = ctrl
            尝试层数 = 0
            while node is not None and 尝试层数 < 3:
                try:
                    vp = node.GetPattern(auto.PatternId.ValuePattern)
                    if vp:
                        vp.SetValue(text)
                        结果.update(ok=True, method="uia_setvalue", at=[cx, cy])
                        return 结果
                except Exception:
                    pass
                try:
                    node = node.GetParentControl()
                except Exception:
                    break
                尝试层数 += 1

    # 路线二：点击聚焦 + 剪贴板粘贴
    if 元素 is not None:
        点击结果 = click(元素, precise=precise)
        if not 点击结果.get("ok"):
            结果.update(error="聚焦点击失败: %s" % 点击结果.get("error", "未知"))
            return 结果
        time.sleep(0.35)  # 等输入焦点稳定
    try:
        pyperclip.copy(text)
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.15)
        结果.update(ok=True, method="clipboard_paste")
        return 结果
    except Exception as e:
        结果.update(ok=False, error=str(e))
        return 结果


def press_key(键序列):
    """按键。支持「+」组合（如 ctrl+s）与中文键名（如 回车）。"""
    parts = [p for p in 键序列.split("+") if p.strip()]
    keys = [_map_key(p) for p in parts]
    try:
        if len(keys) == 1:
            pyautogui.press(keys[0])
        else:
            pyautogui.hotkey(*keys)
        return {"ok": True, "keys": keys}
    except Exception as e:
        return {"ok": False, "keys": keys, "error": str(e)}


def scroll(方向, 像素):
    """滚动。pyautogui 滚动单位为齿格，按 1 齿 ≈ 100 物理像素折算。"""
    clicks = max(1, int(round(像素 / 100.0)))
    try:
        if 方向 in ("up", "上"):
            pyautogui.scroll(clicks)
        else:
            pyautogui.scroll(-clicks)
        return {"ok": True, "direction": 方向, "pixels": 像素, "clicks": clicks}
    except Exception as e:
        return {"ok": False, "direction": 方向, "pixels": 像素, "error": str(e)}
