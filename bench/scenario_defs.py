# -*- coding: utf-8 -*-
"""ScreenPilot Bench 场景定义（ScreenPilot·基准评测施工单 B）。

只消费现有 sp 能力（pipeline/uia_engine），不改 sp/ 核心代码。
每个场景给出：扫描模式、窗口标题子串、启动/收尾动作（供 gt_collect 与 run_bench 共用）。

坐标系约定：
- window 模式：GT bbox 一律为【窗口相对坐标】（运行时加 meta.offset 转屏幕坐标），
  与窗口位置解耦，保证可复现；
- screen 模式（开始菜单）：GT bbox 为【屏幕绝对坐标】。
"""
import os
import subprocess
import time

# 工程根目录（screenpilot/）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(ROOT, "bench")
SCENE_DIR = os.path.join(BENCH, "scenes", "文件夹靶")
BENCH_PAGE = os.path.join(BENCH, "assets", "bench_page.html")


def _popen(cmd):
    subprocess.Popen(cmd, cwd=ROOT)


def launch_calc():
    # UWP 计算器标准启动路径（单实例；另一施工组若在测则仅聚焦不重开，禁互杀）
    _popen(["explorer.exe", r"shell:AppsFolder\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"])


def find_hwnd_by_class(class_name):
    """用 Win32 EnumWindows 按窗口类名找可见顶层窗口句柄。

    背景（本轮实测弱点多处引用）：Win11 新版任务管理器（WinUI，类 TaskManagerWindow）
    不出现在 UIA 桌面子列表里，sp 的按标题找窗对它失明；Win32 枚举真实可靠。
    """
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []
    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(h, l):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, buf, 256)
        if buf.value == class_name and user32.IsWindowVisible(h):
            # 带标题优先（排除隐藏辅助窗）
            tbuf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(h, tbuf, 256)
            found.append((h, tbuf.value))
        return True
    user32.EnumWindows(_cb, 0)
    return found


def launch_taskmgr():
    # 实测：Popen('taskmgr.exe') 会静默提权且常卡成无窗后台实例；
    # ctrl+shift+esc 用户热键路径可靠（窗口类 TaskManagerWindow）
    import pyautogui
    pyautogui.hotkey("ctrl", "shift", "esc")
    time.sleep(4.0)


def launch_explorer():
    _popen(["explorer.exe", SCENE_DIR])


def launch_edge():
    # 本地固定 HTML 页（file:// 协议，内容不变可复现）
    url = "file:///" + BENCH_PAGE.replace("\\", "/")
    _popen(["cmd", "/c", "start", "", "msedge", url])


def launch_none():
    pass


SCENARIOS = {
    # 全部场景：key -> 定义。GT 构建与跑分共用。
    "calculator": {
        "name": "计算器",
        "mode": "window",          # scan --win
        "win": "计算器",
        "launch": launch_calc,
        "launch_wait": 3.5,
        "teardown": "close_win",   # 收尾温和关窗（WM_CLOSE），不 taskkill
    },
    "taskmgr": {
        "name": "任务管理器",
        "mode": "window",
        "win": "任务管理器",
        "find": "hwnd_class",      # UIA 桌面树看不到它，须按类名 EnumWindows 找句柄
        "win_class": "TaskManagerWindow",
        "launch": launch_taskmgr,
        "launch_wait": 1.0,
        "teardown": "alt_f4",      # 提权窗口 WM_CLOSE 被 UIPI 拦，用 Alt+F4
    },
    "explorer": {
        "name": "文件资源管理器",
        "mode": "window",
        "win": "文件夹靶",
        "launch": launch_explorer,
        "launch_wait": 2.5,
        "teardown": "close_win",
    },
    "edge_bench": {
        "name": "Edge本地基准页",
        "mode": "window",
        "win": "ScreenPilot 基准页",
        "launch": launch_edge,
        "launch_wait": 5.0,
        "teardown": "close_win",
    },
    "startmenu": {
        "name": "开始菜单应用列表",
        "mode": "screen",          # 全屏扫描，GT 带 roi 限定菜单区域
        "win": None,
        "launch": launch_none,
        "launch_wait": 0,
        "prepare": "startmenu",    # Win 键展开 → 点「所有应用」进入应用列表
        "teardown": "esc",
    },
    "ldplayer": {
        "name": "雷电模拟器",
        "mode": "window",
        "win": "雷电",             # 运行时探测：找不到窗口则整场景 SKIP 并如实记录
        "launch": launch_none,     # 禁擅开：模拟器没跑就跳过，绝不代启
        "launch_wait": 0,
        "teardown": None,          # 原本就在跑，保持原状
        "optional": True,
    },
}


def close_window_gently(title_substr, timeout=6.0):
    """温和关窗：纯 ctypes 枚举按标题找窗 → WM_CLOSE（不 taskkill，尊重禁互杀纪律）。

    背景：曾用 uiautomation 枚举，长跑分进程里会随机抛 COM 事件错误
    （-2147220991 事件无法调用任何订户），改 EnumWindows 后稳定。
    返回 dict(ok, window[, error])。窗口不存在视为已关。
    """
    import ctypes
    from ctypes import wintypes
    time.sleep(1.0)  # 等待界面稳定
    user32 = ctypes.windll.user32
    user32.SendMessageTimeoutW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                           ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_uint, ctypes.c_uint,
                                           ctypes.POINTER(ctypes.c_ulong)]
    user32.SendMessageTimeoutW.restype = ctypes.c_ulong

    def _find():
        hits = []
        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def cb(h, l):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(h, buf, 256)
            if buf.value and title_substr in buf.value and user32.IsWindowVisible(h):
                hits.append((h, buf.value))
            return True
        user32.EnumWindows(cb, 0)
        return hits

    hits = _find()
    if not hits:
        return {"ok": True, "window": title_substr, "note": "窗口未找到，视为已关"}
    names = [n for _, n in hits]
    WM_CLOSE = 0x0010  # 坑16 固化：句柄参数显式 64 位类型（argtypes 已在上方设置）
    still = list(hits)
    for _ in range(3):  # 暂态竞态重试（跑分后立即关窗曾遇一次不生效）
        for h, _n in still:
            user32.SendMessageTimeoutW(h, WM_CLOSE, None, None, 0x0002,
                                       int(timeout * 1000), None)
            time.sleep(0.8)
        time.sleep(1.2)
        still = _find()
        if not still:
            return {"ok": True, "window": names[0], "closed": len(names)}
    return {"ok": False, "window": names[0],
            "error": f"WM_CLOSE×3 后仍有 {len(still)} 窗"}


def prepare_startmenu(sp_module=None):
    """展开开始菜单并进入「所有应用」列表状态。

    sp_module：可选注入 sp 包（gt_collect/run_bench 均直接 import sp，无需注入时省略）。
    返回 dict(ok, error可选)。
    """
    import pyautogui
    # 前台可能是提权窗（UIPI 吞合成键，正跑实测踩中：esc 串进游戏还弹了退出框）
    # → 全程只用鼠标：点任务栏开始按钮（点击落在任务栏上，不经前台窗口）。
    # 注意开始按钮是 toggle：菜单开着时点它=关闭，故点后核验、没开再点。
    try:
        from sp import pipeline as _pl
    except Exception:
        _pl = None
    if _pl is None:
        return {"ok": True, "note": "无 pipeline 可核验，盲开"}
    sw, sh = pyautogui.size()
    for attempt in range(3):
        pyautogui.click(24, sh - 20)
        time.sleep(1.8)
        res = _pl.scan(win=None, tracker_state=None)
        if any("搜索应用" in (e.text or "") for e in res["elements"]):
            return {"ok": True, "note": f"菜单在位（第 {attempt + 1} 次点击）"}
    print("警告：三次点击均未核验到开始菜单在位，继续按现状扫（成绩可能异常）")
    # 进入应用列表：全屏扫描找「所有应用」按钮点一下（开始菜单已在前台，
    # 不赌 CoreWindow 标题；用现有 sp 能力，不改核心）
    try:
        from sp import pipeline as _pl
        from sp import executor as _ex
        res = _pl.scan(win=None, tracker_state=None)
        btn = None
        for e in res["elements"]:
            if e.text and "所有应用" in e.text:
                btn = e
                break
        if btn is None:
            # 已在应用列表状态（无「所有应用」按钮）或没扫到——两种都算就位，由截图核对
            return {"ok": True, "note": "未找到所有应用按钮（可能已在列表态或扫描未命中）"}
        _ex.click(btn, precise=True)
        time.sleep(1.2)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _登记靶窗(key):
    """launch 后登记靶窗进会话账本（收台机制·launch 源，设计页 §5）。

    bench 进程首次调用时开 bench 会话账本；找不到靶窗（如本就开着）返回 0，
    teardown 走按标题兜底。
    """
    try:
        from sp import cleanup
        if cleanup.账本 is None:
            cleanup.init_session("bench")
        sc = SCENARIOS[key]
        if sc.get("find") == "hwnd_class":
            hits = find_hwnd_by_class(sc["win_class"])
            n = 0
            for h, _t in hits:
                if cleanup.register_hwnd(int(h), source="launch"):
                    n += 1
            return n
        return len(cleanup.register_by_title(sc["win"], source="launch"))
    except Exception:
        return 0


def setup_scenario(key):
    """按场景定义搭台：启动 → 等待 → 靶窗登记。返回 dict(ok, error可选)。"""
    sc = SCENARIOS[key]
    try:
        if sc.get("prepare") == "startmenu":
            return prepare_startmenu()
        if sc["launch"]:
            sc["launch"]()
            if sc.get("launch_wait"):
                time.sleep(sc["launch_wait"])
        登记数 = _登记靶窗(key)
        return {"ok": True, "registered": 登记数}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def resolve_target(key):
    """定位场景目标窗口：普通场景走 sp 按标题找窗；hwnd_class 场景走 EnumWindows。

    返回 dict(ok, hwnd, name, error可选)。
    """
    sc = SCENARIOS[key]
    if sc.get("find") == "hwnd_class":
        hits = find_hwnd_by_class(sc["win_class"])
        if not hits:
            return {"ok": False, "error": f"EnumWindows 未找到类 {sc['win_class']} 的可见窗口"}
        hwnd, title = hits[0]
        return {"ok": True, "hwnd": int(hwnd), "name": title}
    try:
        from sp import uia_engine
        wc = uia_engine.find_window_control(sc["win"])
        return {"ok": True, "hwnd": int(wc.NativeWindowHandle or 0), "name": wc.Name}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def teardown_scenario(key):
    """收尾复原（收台机制：close_win 走 sp.cleanup 六门 WM_CLOSE，设计页 §5）。"""
    sc = SCENARIOS[key]
    how = sc.get("teardown")
    try:
        if how == "close_win":
            try:
                from sp import cleanup
                if cleanup.账本 is None:
                    cleanup.init_session("bench")
                res = cleanup.cleanup_windows()  # 账本自动关（含此前场景幸存者重试）
                if res.get("ok") and (res.get("closed") or res.get("survivors")
                                      or res.get("dirty_skipped")):
                    surv = res.get("survivors", [])
                    return {"ok": not surv, "window": sc["win"],
                            "mode": "sp.cleanup",
                            "closed": [c.get("title", "") for c in res.get("closed", [])],
                            "survivors": len(surv),
                            "dirty_skipped": [d.get("title", "")
                                              for d in res.get("dirty_skipped", [])],
                            **({"error": "WM_CLOSE×3 后仍在，已写残窗报告"} if surv else {})}
            except Exception:
                pass  # 收台通道异常兜底旧路
            return close_window_gently(sc["win"])
        if how == "alt_f4":
            import pyautogui
            tgt = resolve_target(key)
            if not tgt.get("ok"):
                return {"ok": True, "note": "窗口已不在，视为已关"}
            from sp import uia_engine
            wc = uia_engine.control_from_hwnd(tgt["hwnd"])
            uia_engine.activate_control(wc)
            time.sleep(0.8)
            pyautogui.hotkey("alt", "f4")
            time.sleep(1.2)
            gone = not resolve_target(key).get("ok")
            return {"ok": gone, "window": tgt.get("name"),
                    **({} if gone else {"error": "Alt+F4 后窗口仍在"})}
        if how == "esc":
            import pyautogui
            pyautogui.press("esc")
            time.sleep(0.5)
            return {"ok": True}
        return {"ok": True, "note": "无需收尾"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
