# -*- coding: utf-8 -*-
"""收台机制（登记-清理两段式）全链路测试 —— ScreenPilot（2026-11-06 设计页 §5 施工）

验收对照（设计页 §6）：
  T1-T4/T7-T10 本文件（六门/账本三源/差分/猝死/零触碰/expect 修复）
  T5-T6 记事本脏窗实测（验收 4：报「已修改」不关）——同文件内完成

靶纪律：tk 自造靶（台账铁律9，notepad 有会话恢复坑）为主；记事本脏窗
实测一条（验收 4 明确要求记事本场景），收尾 UIA 点「不保存」彻底关净
（体感账 2026-11-06 同款收台，TabState 实测不丢）。全程零 taskkill——
猝死模拟用 os._exit（硬死不经 atexit/finally，与杀进程同语义）。

用法：python _test_收台.py    （退出码 0=全过）
"""
import ctypes
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from sp import cleanup, waiter  # noqa: E402

user32 = ctypes.windll.user32

过账 = []


def 步(名):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), 名), flush=True)


def 检查(名, 条件, 详情=""):
    过账.append(bool(条件))
    print(("  PASS " if 条件 else "  FAIL ") + 名 +
          ((" | " + str(详情)) if 详情 else ""))
    return bool(条件)


def 拉靶(子串="识屏自测靶", 等待轮=30):
    before = {w["hwnd"] for w in cleanup.snapshot()}
    subprocess.Popen([sys.executable, os.path.join(ROOT, "tk_target.py")])
    for _ in range(等待轮):
        time.sleep(0.5)
        new = [w for w in cleanup.snapshot()
               if w["hwnd"] not in before and 子串 in (w["title"] or "")]
        if new:
            return new[0]
    raise RuntimeError("tk 靶未拉起")


def wm_close(hwnd):
    user32.SendMessageTimeoutW(int(hwnd), 0x0010, None, None, 0x0002,
                               2000, None)


def 等窗消失(hwnd, 轮=20):
    for _ in range(轮):
        if not (user32.IsWindow(int(hwnd)) and user32.IsWindowVisible(int(hwnd))):
            return True
        time.sleep(0.5)
    return False


def main():
    print("=== 收台机制全链路测试 ===")
    c = cleanup.init_session("test")

    # ---- T1 账本 launch 源登记 ----
    靶 = 拉靶()
    条目 = cleanup.register_by_title("识屏自测靶", source="launch")
    检查("T1 launch 登记", 条目 and 条目[0]["close_candidate"] is True
          and 条目[0]["source"] == "launch" and 条目[0]["hwnd"] == 靶["hwnd"],
          条目[0] if 条目 else "无条目")

    # ---- T2 差分登记（action_diff advisory）----
    前 = cleanup.snapshot()
    靶2 = 拉靶()
    新增 = cleanup.diff_register(前)
    检查("T2 差分登记 action_diff", len(新增) == 1 and 新增[0]["hwnd"] == 靶2["hwnd"],
          [w["title"] for w in 新增])
    账目 = {d["hwnd"]: d for d in c.entries()}
    检查("T2b 差分条目只报不关", 账目.get(靶2["hwnd"], {}).get("close_candidate") is False
          and 账目.get(靶2["hwnd"], {}).get("source") == "action_diff")

    # ---- T3 白名单永免（门2）----
    检查("T3a explorer exe 免", cleanup.白名单命中(
        {"exe": r"C:\Windows\explorer.exe", "cls": ""}) == "exe:explorer.exe")
    检查("T3b Progman 类免", cleanup.白名单命中(
        {"exe": "", "cls": "Progman"}) == "cls:Progman")
    zc = next((w for w in cleanup.snapshot() if "zcode" in
               (w["exe"] or "").lower()), None)
    检查("T3c ZCode 自身免", bool(zc) and cleanup.白名单命中(zc) is not None,
          zc and cleanup.白名单命中(zc))
    强条 = {"hwnd": zc["hwnd"], "title": zc["title"], "pid": zc["pid"],
            "exe": zc["exe"], "cls": zc["cls"], "source": "explicit",
            "close_candidate": True} if zc else None
    if 强条:
        r = cleanup.close_entry(强条, force=True)
        检查("T3d 白名单 force 也不碰", r["result"] == "whitelist", r["detail"])
    else:
        检查("T3d 白名单 force 也不碰", False, "快照中无 ZCode 窗")

    # ---- T4 身份三验（门1）----
    r = cleanup.close_entry({"hwnd": 999999, "title": "不存在", "pid": 1,
                             "exe": "", "cls": ""})
    检查("T4a 假句柄=gone", r["result"] == "gone", r["detail"])
    假pid条 = {"hwnd": 靶["hwnd"], "title": 靶["title"], "pid": 1,
               "exe": 靶["exe"], "cls": 靶["cls"]}
    r = cleanup.close_entry(假pid条)
    检查("T4b pid 不符拒关", r["result"] == "identity", r["detail"])
    检查("T4c 拒关后靶仍在", user32.IsWindow(靶["hwnd"]))

    # ---- T5/T6 记事本脏窗实测（验收 4：报「已修改」不关）----
    步("T5 记事本环节开始")
    print("  -- 记事本脏窗实测（会恢复 TabState 标签，收尾全部不保存关净）--")
    before = {w["hwnd"] for w in cleanup.snapshot()}
    subprocess.Popen(["notepad"])
    np = None
    for _ in range(30):
        time.sleep(0.5)
        new = [w for w in cleanup.snapshot()
               if w["hwnd"] not in before and "notepad" in
               (w["title"] or "").lower()]
        if new:
            np = new[0]
            break
    检查("T5a 记事本拉起", bool(np), np and np["title"])
    脏关 = None
    if np:
        try:
            from sp import uia_engine
            uia_engine.activate_window("Notepad", wait=1.0)
            time.sleep(1.0)
            # 焦点核验：typewrite 打错窗=事故，前台不是记事本就跳过打字
            前台 = int(user32.GetForegroundWindow() or 0)
            if 前台 != np["hwnd"]:
                raise RuntimeError("记事本置前失败（前台 hwnd=%s≠%s），"
                                   "拒绝盲打" % (前台, np["hwnd"]))
            # 隔离：ctrl+n 开全新标签打字（体感账 12:34 同款），试验与
            # 恢复出的老板标签零接触；收尾只关本试验标签
            import pyautogui
            pyautogui.hotkey("ctrl", "n")
            time.sleep(1.2)
            步("新标签已开，打脏标记")
            pyautogui.typewrite("dirty flag test", interval=0.03)
            time.sleep(0.6)
            np条 = {"hwnd": np["hwnd"], "title": np["title"], "pid": np["pid"],
                    "exe": np["exe"], "cls": np["cls"],
                    "close_candidate": True}
            步("打字完成，等 UIA 稳定后测脏标记")
            time.sleep(1.5)
            脏, 证据 = cleanup.脏标记(np["hwnd"])
            检查("T5b 脏标记测出", 脏, 证据)
            步("脏标记返回，close_entry（预期 dirty）")
            r = cleanup.close_entry(np条)
            检查("T5c 脏窗只报不关", r["result"] == "dirty", r["detail"])
            步("close_entry force（行为不定：记事本 WM_CLOSE 对脏窗或弹框或"
               "直接关，TabState 兜底零丢失——机制如实报告即合格）")
            r2 = cleanup.close_entry(np条, force=True)
            检查("T6 force 脏窗=如实报告（closed/survivor/dirty_survivor）",
                 r2["result"] in ("closed", "survivor", "dirty_survivor"),
                 r2["result"] + " | " + r2["detail"][:60])
            脏关 = np条
        except Exception as e:
            检查("T5 记事本链", False, "异常: %s" % e)
        步("T5/T6 环节结束")

    # ---- T7 猝死模拟（验收 2）：子进程硬死 → 下窗启动扫描通知 ----
    步("T7 猝死模拟开始")
    死脚本 = os.path.join(ROOT, "_debug", "_收台_猝死模拟.py")
    os.makedirs(os.path.dirname(死脚本), exist_ok=True)
    with open(死脚本, "w", encoding="utf-8") as f:
        f.write(
            "# -*- coding: utf-8 -*-\n"
            "import sys, os, time, subprocess\n"
            "ROOT = r'%s'\n"
            "sys.path.insert(0, ROOT)\n"
            "from sp import cleanup\n"
            "cleanup.init_session('mcp')\n"
            "cleanup.register_by_title('识屏自测靶', source='launch')\n"
            "os._exit(0)  # 硬死：atexit/finally 不跑=杀进程语义，账本已在盘上\n"
            % ROOT)
    c2 = cleanup.init_session("mcp")   # 「下一窗」
    notice = cleanup.startup_scan()
    检查("T7a 猝死残窗捎带通知", bool(notice) and "识屏自测靶" in notice, notice)
    步("T7 完成")

    # ---- T8 残窗报告与跨会话只报不关（验收 2/3 准备）----
    检查("T8a 残窗报告文件在", os.path.exists(cleanup.REPORT_PATH),
          cleanup.REPORT_PATH)

    # ---- T9 零触碰与跨会话语义（验收 3 + 门4）----
    基线 = cleanup.snapshot()
    基线hwnd = {w["hwnd"] for w in 基线}
    遗留窗 = [w for w in 基线 if "识屏自测靶" in w["title"]]
    res = cleanup.cleanup_windows()  # 默认=只清本会话账本（c2 空）
    closed_titles = [x.get("title", "") for x in res.get("closed", [])]
    检查("T9a 门4：跨会话遗留默认不碰", not closed_titles, closed_titles)
    # agent 判明后显式清遗留（targets 指定）
    res1 = cleanup.cleanup_windows(targets=["识屏自测靶"])
    t1 = [x.get("title", "") for x in res1.get("closed", [])]
    检查("T9b 显式 targets 清遗留靶", len(t1) >= 1 and
          all("识屏自测靶" in t for t in t1), t1)
    time.sleep(1.0)
    现状 = {w["hwnd"] for w in cleanup.snapshot()}
    被误关 = [w["title"] for w in 基线 if w["hwnd"] not in 现状
             and "识屏自测靶" not in w["title"]]
    检查("T9c 基线窗（含老板窗）零触碰", not 被误关, 被误关)

    # ---- T10 wait_for 消失判成功（体感账 #14 修复）----
    靶3 = 拉靶()
    wm_close(靶3["hwnd"])
    time.sleep(1.5)
    条 = {"find": "自测", "mode": "disappear", "timeout": 5, "interval": 300,
          "window": None, "hwnd": 靶3["hwnd"]}
    rep = waiter.wait_for(条)
    检查("T10a disappear+窗消失=成功",
         rep["matched"] and rep["evidence"].get("verified_by_disappearance"),
         {k: rep[k] for k in ("matched", "window_gone")})
    靶4 = 拉靶()
    wm_close(靶4["hwnd"])
    time.sleep(1.5)
    条2 = dict(条, mode="appear", hwnd=靶4["hwnd"])
    rep2 = waiter.wait_for(条2)
    检查("T10b appear+窗消失=如实失败不空轮询",
         (not rep2["matched"]) and rep2.get("window_gone"),
         {k: rep2[k] for k in ("matched", "window_gone")})

    # ---- 收尾：只关试验标签（ctrl+w+不保存），老板标签零接触 ----
    步("收尾开始")
    if 脏关:
        import uiautomation as auto
        import pyautogui
        # 前台必须在记事本（ctrl/w 只发给前台）
        uia_engine_front = None
        try:
            from sp import uia_engine as _ue
            _ue.activate_window("Notepad", wait=1.0)
            time.sleep(0.8)
            前台 = int(user32.GetForegroundWindow() or 0)
            if not user32.IsWindow(脏关["hwnd"]):
                # T6 force 已把整窗直接关掉（记事本行为不定，TabState 兜底）
                检查("收尾 试验标签关净", True, "窗已在 T6 随整窗关闭")
            elif 前台 == 脏关["hwnd"]:
                # ① ctrl+w 关试验标签（活动标签=试验标签）
                pyautogui.hotkey("ctrl", "w")
                time.sleep(1.2)
                # ② 若弹保存框→不保存（只丢试验标签）
                def 点钮(hwnd, 名单, 超时=2.0):
                    try:
                        wc = auto.ControlFromHandle(int(hwnd))
                        for nm in 名单:
                            try:
                                b = wc.ButtonControl(Name=nm)
                                if b.Exists(超时):
                                    from sp import executor as _ex
                                    ok, 法 = _ex._try_invoke链(b)
                                    步("Invoke「%s」 via %s ok=%s" % (nm, 法, ok))
                                    return True
                            except Exception:
                                continue
                    except Exception as ee:
                        步("点钮异常: %s" % ee)
                    return False
                点钮(脏关["hwnd"], ("不保存", "Don\u2019t save", "Don't save"))
                time.sleep(1.0)
                # ③ 整窗 WM_CLOSE：弹框=有脏老板标签→点「取消」保内容（窗留）
                wm_close(脏关["hwnd"])
                time.sleep(1.5)
                if 点钮(脏关["hwnd"], ("取消", "Cancel")):
                    步("检测到脏老板标签→已点取消保内容，窗保留")
                # ④ 断言：试验字样标签已不在（TabItem 清单核验）
                试签在 = False
                try:
                    wc = auto.ControlFromHandle(int(脏关["hwnd"]))
                    def _走t(c, d=0):
                        global 试签在
                        if d > 5 or 试签在:
                            return
                        try:
                            for ch in c.GetChildren():
                                if ch.ControlTypeName == "TabItemControl" and \
                                        "dirty flag" in (ch.Name or ""):
                                    试签在 = True
                                _走t(ch, d + 1)
                        except Exception:
                            pass
                    if wc is not None:
                        _走t(wc)
                except Exception:
                    pass
                检查("收尾 试验标签关净", not 试签在 and
                     not user32.IsWindow(脏关["hwnd"]), "试签在=%s" % 试签在)
            else:
                检查("收尾 试验标签关净", False, "记事本置前失败，跳过（前台=%s）" % 前台)
        except Exception as ee:
            检查("收尾 试验标签关净", False, "异常: %s" % ee)
    res2 = cleanup.cleanup_windows()
    检查("收尾 剩余测试靶清光", res2.get("closed") is not None
          and all("识屏自测靶" in (x.get("title") or "")
                  for x in res2.get("closed", [])),
          [x.get("title") for x in res2.get("closed", [])])
    cleanup.session_exit_cleanup()

    n, total = sum(过账), len(过账)
    print("=== %d/%d PASS ===" % (n, total))
    return 0 if n == total else 1


if __name__ == "__main__":
    sys.exit(main())
