# -*- coding: utf-8 -*-
"""UIA 控件树引擎：遍历 Windows UI Automation 控件树，产出结构化元素表。"""
import time
from collections import deque

import uiautomation as auto

from .model import Element

# UIA ControlTypeName -> 统一类型名（任务规定的映射表）
TYPE_MAP = {
    "ButtonControl": "button",
    "EditControl": "edit",
    "DocumentControl": "edit",
    "TextControl": "text",
    "CheckBoxControl": "checkbox",
    "ComboBoxControl": "combobox",
    "ListItemControl": "item",
    "HyperlinkControl": "link",
    "MenuItemControl": "menu_item",
    "TabItemControl": "tab",
    "TreeItemControl": "tree_item",
    "WindowControl": "window",
    "RadioButtonControl": "radio",
    "SliderControl": "slider",
    "ImageControl": "image",
    "TableControl": "table",
    "DataItemControl": "item",
    "GroupControl": "group",
    "PaneControl": "pane",
    "CustomControl": "generic",
}

# 天然可点击的控件类型
CLICKABLE_TYPES = {
    "button", "checkbox", "combobox", "item", "link",
    "menu_item", "tab", "tree_item", "edit", "radio", "slider",
}


def control_from_hwnd(hwnd):
    """按窗口句柄直取 UIA 控件（比标题匹配更精准无歧义）；找不到抛 ValueError。"""
    wc = auto.ControlFromHandle(int(hwnd))
    if wc is None:
        raise ValueError("句柄 %s 未对应到 UIA 控件（窗口可能已关闭）" % hwnd)
    return wc


def find_window_control(title):
    """按标题子串查找顶层窗口控件，返回 uiautomation 控件；找不到抛 ValueError。"""
    root = auto.GetRootControl()

    def _归一(s):
        # 剥零宽字符（Edge 标题「Microsoft\u200b Edge」实测带 U+200B，裸子串必失配）
        for ch in ("\u200b", "\u200c", "\u200d", "\ufeff"):
            s = s.replace(ch, "")
        return s.lower()

    key = _归一(title)
    候选 = []
    顶层名单 = []
    for w in root.GetChildren():
        try:
            name = w.Name or ""
        except Exception:
            continue
        顶层名单.append(f"{w.ControlTypeName}:{name}")
        if key in _归一(name):
            候选.append(w)
    if not 候选:
        raise ValueError(
            "未找到标题含「%s」的窗口；当前顶层窗口: %s" % (title, " | ".join(顶层名单))
        )
    return 候选[0]


def _前台hwnd():
    import ctypes
    return ctypes.windll.user32.GetForegroundWindow() & 0xFFFFFFFF


def activate_control(wc):
    """把控件对应窗口激活置前，返回是否真实到前台。

    分层兜底（每层后核验 GetForegroundWindow，杜绝「假成功」）：
    1. uiautomation 的 SetActive（内部处理 Windows 前台锁，多数场景够用）；
    2. 最小化→还原→SetForegroundWindow：还原动作系统会强制激活，可穿透
       「前台进程拒绝让位」的锁（2026-10-23 实测：用户正用全屏浏览器时
       SetActive 静默失效，此手法有效）；
    3. 裸 SetForegroundWindow（聊胜于无，可能仍被拦）。
    失败静默放过由调用方决定是否影响后续流程。
    """
    import ctypes
    import time
    u = ctypes.windll.user32
    try:
        hwnd = int(wc.NativeWindowHandle or 0) & 0xFFFFFFFF
    except Exception:
        hwnd = 0

    def 到前台():
        return hwnd != 0 and _前台hwnd() == hwnd

    try:
        wc.SetActive()
        if 到前台() or hwnd == 0:
            return True
    except Exception:
        pass
    if hwnd:
        try:
            u.ShowWindow(hwnd, 6)  # SW_MINIMIZE
            time.sleep(0.15)
            u.ShowWindow(hwnd, 9)  # SW_RESTORE：还原强制激活，穿透前台锁
            time.sleep(0.3)
            u.SetForegroundWindow(hwnd)
            time.sleep(0.15)
            if 到前台():
                return True
        except Exception:
            pass
        try:
            u.ShowWindow(hwnd, 9)
            u.SetForegroundWindow(hwnd)
            time.sleep(0.1)
            return 到前台()
        except Exception:
            pass
    return False


def activate_window(title, wait=0.4):
    """按标题子串查找并激活窗口，返回 (控件, 矩形)；找不到抛 ValueError。

    wait 为置前后等待渲染稳定的秒数（截图/点击前调大些更稳）。
    """
    wc = find_window_control(title)
    activate_control(wc)
    time.sleep(wait)
    return wc, wc.BoundingRectangle


def _check_pattern(ctrl):
    """通过 InvokePattern / TogglePattern / LegacyIAccessible 默认动作判断可点性。"""
    try:
        if ctrl.GetPattern(auto.PatternId.InvokePattern):
            return True
    except Exception:
        pass
    try:
        if ctrl.GetPattern(auto.PatternId.TogglePattern):
            return True
    except Exception:
        pass
    try:
        lap = ctrl.GetPattern(auto.PatternId.LegacyIAccessiblePattern)
        if lap is not None:
            action = lap.CurrentDefaultAction
            if action:
                return True
    except Exception:
        pass
    return False


# 地址栏识别特征（Chromium 系中文/英文实测名与常见关键词，子串匹配）
_地址栏特征 = ("地址", "address", "omnibox", "search or type")


def 读地址栏主机(wc, 最大节点=300, 最大深度=10):
    """浏览器窗口控件树里找地址栏 Edit 控件并读 URL，返回站点 host（str）或 None。

    供书库 v2 域解析用（问题B：浏览器按站点分域，不按页面标题分域）。
    STA COM 纪律：必须与控件 wc 同线程调用（pipeline 的 UIA 线程内完成），
    禁止把 wc 传到别的线程。地址栏读不到/异常一律返回 None（调用方退化
    web_ 前缀规则），绝不抛异常打断扫描主流程。
    """
    try:
        队列 = deque([(wc, 0)])
        计数 = 0
        while 队列:
            ctrl, depth = 队列.popleft()
            计数 += 1
            if 计数 > 最大节点:
                break
            try:
                if ctrl.ControlTypeName == "EditControl":
                    名称 = ""
                    try:
                        名称 = ctrl.Name or ""
                    except Exception:
                        pass
                    aid = ""
                    try:
                        aid = ctrl.AutomationId or ""
                    except Exception:
                        pass
                    特征串 = ("%s %s" % (名称, aid)).lower()
                    if any(k in 特征串 for k in _地址栏特征):
                        url = None
                        try:
                            pat = ctrl.GetPattern(auto.PatternId.ValuePattern)
                            url = pat.Value if pat else None
                        except Exception:
                            url = None
                        if url:
                            from . import booklib
                            host = booklib.url主机(url)
                            if host:
                                return host
            except Exception:
                pass
            if depth < 最大深度:
                try:
                    child = ctrl.GetFirstChildControl()
                    while child:
                        队列.append((child, depth + 1))
                        child = child.GetNextSiblingControl()
                except Exception:
                    pass
    except Exception:
        return None
    return None


def scan_uia(root=None, screen_wh=None, max_nodes=1200, max_depth=24, root_hwnd=0):
    """广度优先遍历控件树，返回元素列表（屏幕物理像素坐标）。

    root 为 None 时从桌面根开始（全屏扫描）；root_hwnd 为 root 所属顶层窗口
    句柄，记录到每个元素的 hwnd 字段（后台点击时按窗口树内重定位控件）。
    过滤规则：0/1 尺寸、IsOffscreen、完全出屏；无名的未映射类型（如 Pane）
    自身丢弃但子树照常展开——Chromium 系浏览器的内容树挂在无名
    AXVirtualView/Pane 容器链下（深度可达 20+ 层），剪掉容器子树=内容区黑盒；
    部分出屏的元素裁剪到屏幕范围内。
    """
    if root is None:
        root = auto.GetRootControl()
    sw, sh = screen_wh if screen_wh else (99999, 99999)

    队列 = deque([(root, 0)])
    元素 = []
    计数 = 0
    while 队列:
        ctrl, depth = 队列.popleft()
        计数 += 1
        if 计数 > max_nodes:
            break
        try:
            控件类型 = ctrl.ControlTypeName
        except Exception:
            continue
        try:
            名称 = (ctrl.Name or "").strip()
        except Exception:
            名称 = ""

        映射类型 = TYPE_MAP.get(控件类型)
        无名未映射 = 映射类型 is None and not 名称
        if not 无名未映射:
            try:
                rect = ctrl.BoundingRectangle
                l, t, r, b = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
            except Exception:
                rect = None
            if rect is not None:
                if r - l <= 1 or b - t <= 1:
                    rect = None  # 0 尺寸过滤
            if rect is not None:
                try:
                    if ctrl.IsOffscreen:
                        rect = None
                except Exception:
                    pass

                # 裁剪到屏幕范围，完全出屏丢弃
                # （l/t/r/b 已在 BoundingRectangle 取值处赋值；Rect 对象不可迭代，禁止裸解包）
                cl, ct = max(l, 0), max(t, 0)
                cr, cb = min(r, sw), min(b, sh)
                if cr - cl <= 1 or cb - ct <= 1:
                    rect = None
            if rect is not None:
                可点 = 映射类型 in CLICKABLE_TYPES or _check_pattern(ctrl)
                元素.append(Element(
                    type=映射类型 or "generic",
                    text=名称,
                    bbox=[cl, ct, cr, cb],
                    source="uia",
                    conf=1.0 if 名称 else 0.6,
                    clickable=可点,
                    hwnd=root_hwnd,
                ))

        if depth < max_depth:
            try:
                child = ctrl.GetFirstChildControl()
                while child:
                    队列.append((child, depth + 1))
                    child = child.GetNextSiblingControl()
            except Exception:
                pass
    return 元素
