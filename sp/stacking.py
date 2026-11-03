# -*- coding: utf-8 -*-
"""堆叠状态感知层：让 agent 看见窗口的 Z 序真相（2026-10-28 立项根因）。

问题：agent 看到的是堆叠合成画面，分辨不出——
① 哪个窗口是当前前台（焦点）窗；
② 目标窗口此刻被谁遮挡、遮了多少（后台抓窗能看到它自身的渲染，但那是
   「后台真相」，不是「前台堆叠真相」——拿后台真相定坐标物理点击，
   点的是前台遮挡物）；
③ 一个坐标点击下去命中的是哪个窗口——物理点击永远打在最顶层窗口
   （铁律19），此前点完都不知道点没点到。

本模块把 Z 序变成结构化数据 + 点击前置拦截：
- stack():        可见顶层窗口 Z 序全景（谁在前台、谁被谁盖）
- occlusion():    目标窗口遮挡量化（窗口内网格采样 WindowFromPoint 归根比对）
- who_at():       某坐标最顶层窗口是谁（点击前先问，点击后反馈）
- ensure_top_at():落点归窗核验——归窗不是目标窗就拒绝，供物理点击前置调用

坑29 实锤沿用：WindowFromPoint 的 argtypes 必须显式（x64 缺失=乱坐标假
截胡），返回的往往是子窗口——必须 GetAncestor(GA_ROOT) 归根再比对。
"""
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

GW_HWNDNEXT = 2
GA_ROOT = 2
DWMWA_CLOAKED = 14


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# x64 显式签名（坑29：缺 argtypes = 假截胡/乱坐标）
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetTopWindow.argtypes = [wintypes.HWND]
user32.GetTopWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetWindow.restype = wintypes.HWND
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
# 注意：GetWindowRect 不能设 argtypes——capture.py 用自定义 RECT 结构体
# byref 传参，ctypes 同一 DLL 的 argtypes 全局生效，设 POINTER(wintypes.RECT)
# 会把 capture.window_rect_hwnd 炸出 ArgumentError（2026-10-28 实测踩中）。
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wintypes.HWND
ctypes.windll.dwmapi.DwmGetWindowAttribute.argtypes = [
    wintypes.HWND, ctypes.c_uint, ctypes.POINTER(ctypes.c_int), ctypes.c_uint]
ctypes.windll.dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long


def _窗口标题(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _类名(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _rect(hwnd):
    rc = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rc)):
        return None
    return [rc.left, rc.top, rc.right, rc.bottom]


def _cloaked(hwnd):
    """UWP 幽灵窗判定：IsWindowVisible=True 但被 DWM 斗篷遮蔽（不可见）。"""
    v = ctypes.c_int(0)
    ctypes.windll.dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v))
    return v.value != 0


def foreground_hwnd():
    """当前前台（焦点）顶层窗口句柄；无前台返回 0。"""
    return int(user32.GetForegroundWindow() or 0)


def _窗口信息(hwnd, fg):
    return {
        "hwnd": int(hwnd),
        "title": _窗口标题(hwnd),
        "cls": _类名(hwnd),
        "rect": _rect(hwnd),
        "foreground": int(hwnd) == fg,
        "minimized": bool(user32.IsIconic(hwnd)),
    }


def stack(limit=40):
    """可见顶层窗口 Z 序全景（从最顶层往下）。

    返回 {"foreground": {窗信息}, "windows": [窗信息...按 Z 序]}。
    过滤：不可见窗、DWM 斗篷幽灵窗（UWP 后台挂起态）、零面积窗。
    """
    fg = foreground_hwnd()
    清单 = []
    h = user32.GetTopWindow(None)
    # 顶层窗链上隐藏窗很多，必须走够深（500 步硬上限防环；2026-10-31 实测
    # 深夜系统窗多时新窗会排到链 250+ 位，200 步截断真窗），否则真窗被截断
    for _ in range(500):
        if not h:
            break
        if user32.IsWindowVisible(h) and not _cloaked(h):
            info = _窗口信息(h, fg)
            r = info["rect"]
            if r and r[2] - r[0] > 0 and r[3] - r[1] > 0:
                清单.append(info)
                if len(清单) >= limit:
                    break
        h = user32.GetWindow(h, GW_HWNDNEXT)
    前台 = next((w for w in 清单 if w["foreground"]), None)
    return {"foreground": 前台, "windows": 清单}


def who_at(x, y):
    """某屏幕物理坐标最顶层窗口是谁（归根后的顶层窗）。

    返回 {hwnd, title, cls, rect}；坐标无窗口返回 None。
    这就是「这个坐标点击下去会打到谁」的事前答案。
    """
    h = int(user32.WindowFromPoint(POINT(int(x), int(y))) or 0)
    if not h:
        return None
    root = int(user32.GetAncestor(h, GA_ROOT) or h)
    return _窗口信息(root, foreground_hwnd())


def ensure_top_at(x, y, 目标hwnd):
    """落点归窗核验：该坐标最顶层窗口必须是目标窗口，否则返回遮挡者。

    返回 None = 归窗匹配（可以放心点）；
    返回 dict = 归窗不符（who_at 结果），物理点击打下去命中的是它。
    """
    顶 = who_at(x, y)
    if 顶 is None:
        return {"hwnd": 0, "title": "", "cls": "", "rect": None,
                "reason": "该坐标无窗口"}
    if int(顶["hwnd"]) == (int(目标hwnd) & 0xFFFFFFFF):
        return None
    return 顶


def occlusion(hwnd, step=48, max_samples=400):
    """目标窗口遮挡量化：窗口内网格采样 WindowFromPoint 归根比对。

    返回 {窗口信息, samples, visible, occluded, visible_ratio,
          blockers: [遮挡者窗信息（按遮挡采样点数降序）]}。
    可见率 = 采样点归根仍属本窗的比例——这是「前台堆叠真相」：
    后台抓窗（PrintWindow）永远是 100% 自身渲染，但物理点击只打得动
    visible_ratio 对应的那部分屏幕。
    """
    hwnd = int(hwnd) & 0xFFFFFFFF
    if not user32.IsWindow(hwnd):
        raise RuntimeError("无效窗口句柄: %s" % hwnd)
    info = _窗口信息(hwnd, foreground_hwnd())
    r = info["rect"]
    if info["minimized"] or not r:
        return {**info, "samples": 0, "visible": 0, "occluded": 0,
                "visible_ratio": 0.0, "blockers": [], "minimized": True}
    sw_w = user32.GetSystemMetrics(0)
    sw_h = user32.GetSystemMetrics(1)
    # 采样区 = 窗口与屏幕交集（屏幕外区域无「堆叠」意义）
    l, t = max(r[0], 0), max(r[1], 0)
    rr, bb = min(r[2], sw_w), min(r[3], sw_h)
    w, h = rr - l, bb - t
    if w <= 0 or h <= 0:
        return {**info, "samples": 0, "visible": 0, "occluded": 0,
                "visible_ratio": 0.0, "blockers": [], "offscreen": True}
    # 步长自适应：采样点数不超过 max_samples
    步 = max(step, int(((w / step) * (h / step) / max_samples) ** 0.5 * step))
    遮挡计数 = {}
    采样 = 0
    可见 = 0
    y = t + 步 // 2
    while y < bb:
        x = l + 步 // 2
        while x < rr:
            采样 += 1
            顶 = who_at(x, y)
            if 顶 and 顶["hwnd"] == hwnd:
                可见 += 1
            elif 顶:
                k = 顶["hwnd"]
                if k not in 遮挡计数:
                    遮挡计数[k] = {**顶, "hits": 0}
                遮挡计数[k]["hits"] += 1
            x += 步
        y += 步
    blockers = sorted(遮挡计数.values(), key=lambda d: -d["hits"])
    return {**info, "samples": 采样, "visible": 可见, "occluded": 采样 - 可见,
            "visible_ratio": round(可见 / 采样, 3) if 采样 else 1.0,
            "blockers": [{"hwnd": b["hwnd"], "title": b["title"],
                          "hits": b["hits"]} for b in blockers]}
