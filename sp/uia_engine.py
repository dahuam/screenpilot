# -*- coding: utf-8 -*-
"""UIA 控件树引擎：遍历 Windows UI Automation 控件树，产出结构化元素表。"""
import ctypes
import threading
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

# ============ P0 三件套（2026-11-06 借鉴 vercel-labs/agent-desktop） ============
# uiautomation 库用老 CLSID {ff48dba4-…} 建单例，该实例不支持 IUIAutomation2、
# 无超时保护——对挂死窗口的 ElementFromHandle 等跨进程调用可挂 30s+。
# 实测：老 CLSID QI IUIAutomation2 报 E_NOINTERFACE，CUIAutomation8 QI 成功
# （探针脚本 _debug/_probe_p0三件套.py）。引导=monkeypatch 库单例换对象+设超时。

_引导锁 = threading.Lock()
_已引导 = False
_引导降级说明 = None  # 换对象成功但超时设置失败时的说明；None=完整生效


def ensure_timeout_engine():
    """把 uiautomation 库单例换成 CUIAutomation8 对象并设双超时（幂等）。

    库内部一律经 _AutomationClient.instance().IUIAutomation 现取字段，
    替换 IUIAutomation + ViewWalker 两个字段即全量生效。必须在业务控件
    创建前调用（scan_uia / control_from_hwnd / find_window_control 入口
    已埋点）。失败返回 False 回退库原生行为，绝不打断扫描主流程；
    QI 指针交给 comtypes GC，勿手动 Release（探针实测手动 Release 会
    access violation）。
    """
    global _已引导, _引导降级说明
    if _已引导:
        return True
    with _引导锁:
        if _已引导:
            return True
        try:
            import comtypes.client
            from uiautomation import uiautomation as uu

            m = comtypes.client.GetModule("UIAutomationCore.dll")
            inst = uu._AutomationClient.instance()
            obj8 = comtypes.client.CreateObject(m.CUIAutomation8, interface=m.IUIAutomation)
            p2 = obj8.QueryInterface(m.IUIAutomation2)
            try:
                p2.SetConnectionTimeout(2000)     # 连接超时：挂死 provider 2s 拒答
                p2.SetTransactionTimeout(20000)   # 事务超时：单次跨进程调用 20s 上限
            except Exception:
                try:
                    p2.ConnectionTimeout = 2000   # comtypes 版本差异的 property 兜底
                    p2.TransactionTimeout = 20000
                except Exception as e:
                    _引导降级说明 = "CUIAutomation8 已生效但超时设置失败: %r" % (e,)
            inst.IUIAutomation = obj8
            inst.ViewWalker = obj8.RawViewWalker
            _已引导 = True
            return True
        except Exception:
            return False


_WM_NULL = 0x0000
_SMTO_ABORTIFHUNG = 0x0002


def 窗口泵探测(hwnd, timeout_ms=2000):
    """WM_NULL 泵探测：目标窗口 2s 内不泵消息即抛结构化 ValueError。

    挂死窗口的 ControlFromHandle 无超时保护时可挂 30s+，探测先行让
    调用方快速失败。活窗立即返回 True。注意探针实测极少数活窗（无
    消息循环的自绘窗）也会超时——超时只能证明「不能证明活着」，
    语义调用对它大概率同样冻结，物理点击不受影响。
    """
    hwnd = int(hwnd) & 0xFFFFFFFF
    if not hwnd:
        return True
    u = ctypes.windll.user32
    if not u.IsWindow(hwnd):
        raise ValueError("窗口句柄已失效（IsWindow=False）: hwnd=0x%X（窗口可能已关闭）" % hwnd)
    out = ctypes.c_uint32(0)
    t0 = time.perf_counter()
    ret = u.SendMessageTimeoutW(hwnd, _WM_NULL, 0, 0, _SMTO_ABORTIFHUNG, int(timeout_ms), ctypes.byref(out))
    if ret != 0:
        return True
    耗时 = (time.perf_counter() - t0) * 1000
    raise ValueError(
        "窗口无响应（%dms 未泵消息，挂死概率高）: hwnd=0x%X（探测耗时 %.0fms）。"
        "UIA 语义调用对该窗口可能长时间冻结，建议改物理坐标操作或先排查窗口进程。"
        % (int(timeout_ms), hwnd, 耗时))


# COMError hresult 分类单表（agent-desktop hresult_record 的 Python 精简版）。
# comtypes 的 COMError.hresult 是有符号 int，统一 & 0xFFFFFFFF 后比对。
_HR_ELEMENTNOTAVAILABLE = 0x80040201  # UIA_E_ELEMENTNOTAVAILABLE：元素已消失
_HR_INCOMPLETE = frozenset((
    0x80010001,  # RPC_E_CALL_REJECTED：provider 忙拒收
    0x8001000A,  # RPC_E_CALL_IN_PROGRESS：provider 调用进行中
    0x800705B4,  # ERROR_TIMEOUT：超时
    0x800706BE,  # RPC_S_CALL_FAILED：远程调用失败
    0x800706BF,  # RPC_S_CALL_FAILED_DNE：远程调用失败且未执行
))


def 分类_com_error(e):
    """COMError 按 hresult 归三类：element_gone / incomplete / other。

    element_gone=元素消失（跳过合理）；incomplete=超时/传输类失败
    （树可能不完整，meta 里报出来）；other=其余。非 COMError 归 other。
    """
    hr = getattr(e, "hresult", None)
    if not isinstance(hr, int):
        return "other"
    hr &= 0xFFFFFFFF
    if hr == _HR_ELEMENTNOTAVAILABLE:
        return "element_gone"
    if hr in _HR_INCOMPLETE:
        return "incomplete"
    return "other"


def _计错(error_stats, e):
    """scan_uia 遍历中的 COM 失败分类计数（error_stats 为 None 时不计）。"""
    if error_stats is None:
        return
    类 = 分类_com_error(e)
    error_stats[类] = error_stats.get(类, 0) + 1


def control_from_hwnd(hwnd):
    """按窗口句柄直取 UIA 控件（比标题匹配更精准无歧义）；找不到抛 ValueError。

    取控件前先泵探测（2s 上限）：挂死窗口的 ControlFromHandle 在无超时
    保护时可挂 30s+，探测先行快速结构化失败（窗口无响应/句柄失效分开报）。
    """
    ensure_timeout_engine()
    窗口泵探测(hwnd)
    wc = auto.ControlFromHandle(int(hwnd))
    if wc is None:
        raise ValueError("句柄 %s 未对应到 UIA 控件（窗口可能已关闭）" % hwnd)
    return wc


def find_window_control(title):
    """按标题子串查找顶层窗口控件，返回 uiautomation 控件；找不到抛 ValueError。"""
    ensure_timeout_engine()
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


def _check_pattern(ctrl, error_stats=None):
    """通过 InvokePattern / TogglePattern / LegacyIAccessible 默认动作判断可点性。"""
    try:
        if ctrl.GetPattern(auto.PatternId.InvokePattern):
            return True
    except Exception as e:
        _计错(error_stats, e)
    try:
        if ctrl.GetPattern(auto.PatternId.TogglePattern):
            return True
    except Exception as e:
        _计错(error_stats, e)
    try:
        lap = ctrl.GetPattern(auto.PatternId.LegacyIAccessiblePattern)
        if lap is not None:
            # 库包装属性名是 DefaultAction（COM 侧叫 CurrentDefaultAction；
            # 旧代码误用 COM 名一直 AttributeError 被吞，2026-11-06 分类计数暴露）
            action = lap.DefaultAction
            if action:
                return True
    except Exception as e:
        _计错(error_stats, e)
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


def scan_uia(root=None, screen_wh=None, max_nodes=1200, max_depth=24, root_hwnd=0,
             error_stats=None):
    """广度优先遍历控件树，返回元素列表（屏幕物理像素坐标）。

    root 为 None 时从桌面根开始（全屏扫描）；root_hwnd 为 root 所属顶层窗口
    句柄，记录到每个元素的 hwnd 字段（后台点击时按窗口树内重定位控件）。
    过滤规则：0/1 尺寸、IsOffscreen、完全出屏；无名的未映射类型（如 Pane）
    自身丢弃但子树照常展开——Chromium 系浏览器的内容树挂在无名
    AXVirtualView/Pane 容器链下（深度可达 20+ 层），剪掉容器子树=内容区黑盒；
    部分出屏的元素裁剪到屏幕范围内。

    error_stats 传 dict 时就地填 COM 失败分类计数（element_gone/incomplete/
    other）：库内部大量吞错（失效控件读属性常不抛异常），抛出来的按
    hresult 分类——element_gone=元素消失跳过合理，incomplete=超时/传输类
    失败（树可能不完整），供调用方接进 scan meta.errors.uia_stats。
    """
    if root is None:
        ensure_timeout_engine()
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
        except Exception as e:
            _计错(error_stats, e)
            continue
        try:
            名称 = (ctrl.Name or "").strip()
        except Exception as e:
            _计错(error_stats, e)
            名称 = ""

        映射类型 = TYPE_MAP.get(控件类型)
        无名未映射 = 映射类型 is None and not 名称
        if not 无名未映射:
            try:
                rect = ctrl.BoundingRectangle
                l, t, r, b = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
            except Exception as e:
                _计错(error_stats, e)
                rect = None
            if rect is not None:
                if r - l <= 1 or b - t <= 1:
                    rect = None  # 0 尺寸过滤
            if rect is not None:
                try:
                    if ctrl.IsOffscreen:
                        rect = None
                except Exception as e:
                    _计错(error_stats, e)

                # 裁剪到屏幕范围，完全出屏丢弃
                # （l/t/r/b 已在 BoundingRectangle 取值处赋值；Rect 对象不可迭代，禁止裸解包）
                cl, ct = max(l, 0), max(t, 0)
                cr, cb = min(r, sw), min(b, sh)
                if cr - cl <= 1 or cb - ct <= 1:
                    rect = None
            if rect is not None:
                可点 = 映射类型 in CLICKABLE_TYPES or _check_pattern(ctrl, error_stats)
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
            except Exception as e:
                _计错(error_stats, e)
    return 元素
