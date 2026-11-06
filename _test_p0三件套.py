# -*- coding: utf-8 -*-
"""P0 三件套 实靶验收 —— ScreenPilot（2026-11-06，借鉴 vercel-labs/agent-desktop）

场景：tk 子进程起窗后不泵消息 = 挂死窗口（探针 _debug/_probe_p0三件套.py 同款）。

判据：
  T1 引导：ensure_timeout_engine() 成功，库单例可 QI IUIAutomation2（CUIAutomation8 生效）
  T2 泵探测活窗：泵消息的 tk 窗立即通过（True）
  T3 泵探测挂死窗：<3.5s 抛 ValueError 且含「窗口无响应」（结构化）
  T4 control_from_hwnd 挂死窗：<3.5s 结构化报错不冻结（老代码此处可挂 30s+）
  T5 executor._控件重定位 挂死窗元素：返回 None 且 <3.5s（后台路线快速放弃，物理兜底可达）
  T6 scan_uia error_stats：正常窗口扫描返回分类计数字典（无错为空）
  T7 COMError 分类真弹：死进程控件读属性，抛 COMError 则 hresult 可归类
"""
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

结果账 = []


def 判(名, ok, 证据=""):
    结果账.append((名, bool(ok)))
    print(("[%s] %s %s" % ("✅过" if ok else "❌败", 名, 证据)))


def 起_tk窗(泵消息):
    """tk 子进程建窗，泵消息=True 循环 update（活窗）/False 建-打印- sleep（挂死窗）。"""
    if 泵消息:
        循环 = (
            "import tkinter,time;"
            "r=tkinter.Tk();r.title('P0活窗');r.geometry('160x80+80+80');"
            "r.update();print(r.winfo_id(),flush=True);"
            "t0=time.time();\n"
            "while time.time()-t0<60:\n"
            "    r.update();time.sleep(0.05)\n"
        )
    else:
        循环 = (
            "import tkinter,time;"
            "r=tkinter.Tk();r.title('P0挂死探针窗');r.geometry('200x100+100+100');"
            "r.update();print(r.winfo_id(),flush=True);time.sleep(120)"
        )
    p = subprocess.Popen([sys.executable, "-c", 循环], stdout=subprocess.PIPE,
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
    hwnd = None
    t0 = time.time()
    while time.time() - t0 < 15:
        line = p.stdout.readline().decode(errors="replace").strip()
        if line.isdigit():
            hwnd = int(line)
            break
    return p, hwnd


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from sp import enable_dpi_awareness
    enable_dpi_awareness()
    from sp import uia_engine, executor
    from sp.model import Element

    # T1 引导
    ok = uia_engine.ensure_timeout_engine()
    import comtypes.client
    m = comtypes.client.GetModule("UIAutomationCore.dll")
    from uiautomation import uiautomation as uu
    inst = uu._AutomationClient.instance()
    try:
        inst.IUIAutomation.QueryInterface(m.IUIAutomation2)
        qi_ok = True
        qi证据 = "单例 QI IUIAutomation2 成功"
    except Exception as e:
        qi_ok = False
        qi证据 = repr(e)[:80]
    判("T1 CUIAutomation8 引导", ok and qi_ok and not uia_engine._引导降级说明,
       "%s 降级=%s" % (qi证据, uia_engine._引导降级说明))
    判("T1b 分类函数", uia_engine.分类_com_error(Exception("无hresult")) == "other"
       and uia_engine.分类_com_error(type("E", (Exception,), {})()) == "other",
       "非 COMError 归 other")

    # 活窗/挂死窗靶
    活窗进程, 活窗hwnd = 起_tk窗(True)
    挂窗进程, 挂窗hwnd = 起_tk窗(False)
    记事本 = None
    try:
        判("靶窗就绪", bool(活窗hwnd) and bool(挂窗hwnd),
           "活窗=0x%X 挂窗=0x%X" % (活窗hwnd or 0, 挂窗hwnd or 0))

        # T2 泵探测活窗
        t0 = time.perf_counter()
        r2 = False
        try:
            r2 = uia_engine.窗口泵探测(活窗hwnd)
        except ValueError as e:
            r2 = False
            print("   活窗探测异常:", str(e)[:80])
        dt2 = (time.perf_counter() - t0) * 1000
        判("T2 泵探测活窗", r2 and dt2 < 1500, "ret=%s 耗时=%.0fms" % (r2, dt2))

        # T3 泵探测挂死窗
        t0 = time.perf_counter()
        抛错 = None
        try:
            uia_engine.窗口泵探测(挂窗hwnd)
        except ValueError as e:
            抛错 = str(e)
        dt3 = (time.perf_counter() - t0) * 1000
        判("T3 泵探测挂死窗", 抛错 is not None and "无响应" in 抛错 and dt3 < 3500,
           "%.0fms 内抛: %s" % (dt3, (抛错 or "未抛")[:60]))

        # T4 control_from_hwnd 挂死窗（老代码此处 ControlFromHandle 可挂 30s+）
        t0 = time.perf_counter()
        抛错4 = None
        try:
            uia_engine.control_from_hwnd(挂窗hwnd)
        except ValueError as e:
            抛错4 = str(e)
        except Exception as e:
            抛错4 = "非ValueError: %r" % (e,)
        dt4 = (time.perf_counter() - t0) * 1000
        判("T4 control_from_hwnd 挂死窗结构化报错",
           抛错4 is not None and "无响应" in 抛错4 and dt4 < 3500,
           "%.0fms 内: %s" % (dt4, (抛错4 or "未抛")[:60]))

        # T5 后台重定位路线快速放弃
        挂元素 = Element(type="button", text="靶", bbox=[100, 100, 140, 120],
                        source="uia", conf=1.0, clickable=True, hwnd=挂窗hwnd)
        t0 = time.perf_counter()
        重定位 = executor._控件重定位(挂元素)
        dt5 = (time.perf_counter() - t0) * 1000
        判("T5 _控件重定位 挂死窗快速放弃", 重定位 is None and dt5 < 3500,
           "None=%s %.0fms（物理兜底可达，不冻结）" % (重定位 is None, dt5))

        # T6 正常窗口扫描 + stats 字典
        t0 = time.perf_counter()
        wc = uia_engine.control_from_hwnd(活窗hwnd)
        stats = {}
        元素 = uia_engine.scan_uia(root=wc, root_hwnd=活窗hwnd, error_stats=stats)
        dt6 = (time.perf_counter() - t0) * 1000
        判("T6 scan_uia 活窗+stats", isinstance(stats, dict) and len(元素) >= 1,
           "元素=%d stats=%s %.0fms" % (len(元素), stats, dt6))

        # T7 死进程控件读属性真弹观测
        记事本 = subprocess.Popen(["notepad.exe"])
        time.sleep(1.5)
        wc7 = None
        import uiautomation as auto
        for _ in range(20):
            try:
                wc7 = auto.WindowControl(searchDepth=1, ClassName="Notepad")
                if wc7.Exists(0.5, 1):
                    break
            except Exception:
                pass
            time.sleep(0.3)
        观测 = "未取得记事本控件"
        if wc7 and wc7.Exists(0.2, 1):
            子 = wc7.GetFirstChildControl()
            记事本.kill()
            time.sleep(1.0)
            try:
                _ = 子.Name
                观测 = "读 Name 未抛（库吞错，探针已实锤此形态）"
            except Exception as e:
                类 = uia_engine.分类_com_error(e)
                hr = getattr(e, "hresult", None)
                观测 = "抛 %s hresult=%s → %s" % (
                    type(e).__name__, hex(hr & 0xFFFFFFFF) if isinstance(hr, int) else hr, 类)
        else:
            记事本.kill()
        判("T7 死进程控件观测", True, 观测 + "（观测项不判败）")
    finally:
        for 进程 in (挂窗进程, 活窗进程, 记事本):
            if 进程:
                try:
                    进程.kill()
                except Exception:
                    pass

    过 = sum(1 for _, ok in 结果账 if ok)
    print("\n===== P0 三件套验收：%d/%d 过 =====" % (过, len(结果账)))
    return 0 if 过 == len(结果账) else 1


if __name__ == "__main__":
    sys.exit(main())
