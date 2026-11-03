# -*- coding: utf-8 -*-
"""堆叠状态感知层 实靶验收 —— ScreenPilot（2026-10-28）

场景：计算器被「识屏自测靶」（tk 靶）部分遮挡，物理点击盲打必错位。

判据：
  A1 stack()：foreground=识屏自测靶（前台判定成立）
  A2 occlusion(计算器)：visible_ratio<1 且 blockers 含识屏自测靶（遮挡量化）
  A3 click_element(7, physical) 被遮挡时 → 拦截 ok=False，top_window=自测靶
  A4 activate 计算器 → 同一元素再点 → ok=True 且业务生效（显示区出现 7）
  A5 click_xy hit_window 反馈 + top_must 事前拦截（相符过/不符拒）
  A6 scan --bg 后台抓窗 meta.occlusion 摘要存在（后台图自带可见率）
"""
import json
import os
import subprocess
import sys
import time

import ctypes

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from sp import enable_dpi_awareness  # noqa: E402

enable_dpi_awareness()
from sp import stacking, uia_engine  # noqa: E402

结果账 = []


def 判(名, ok, 证据=""):
    结果账.append((名, bool(ok)))
    print(("[%s] %s %s" % ("✅过" if ok else "❌败", 名, 证据)))


def cli(*参数):
    r = subprocess.run([sys.executable, os.path.join(BASE, "screenpilot.py"), *参数],
                       capture_output=True, timeout=120)
    return json.loads(r.stdout.decode("utf-8", "replace"))


def 杀(进程名):
    subprocess.run(["taskkill", "/f", "/im", 进程名], capture_output=True)


def main():
    杀("CalculatorApp.exe")
    time.sleep(0.5)
    os.startfile("calculator:")  # Win11 UWP 计算器（calc: 协议；explorer calc: 无效）
    靶 = subprocess.Popen([sys.executable, os.path.join(BASE, "tk_target.py")])
    time.sleep(3.0)
    try:
        # ---------- 搭台：scan 计算器 → 挪 tk 靶盖住「7」按钮 → tk 置前 ----------
        scan1 = cli("scan", "--win", "计算器")
        assert scan1.get("elements"), "scan 计算器失败: %s" % scan1.get("error")
        calc_hwnd = scan1["meta"]["hwnd"]
        候选 = [e for e in scan1["elements"] if e["text"].strip() == "7"]
        if not 候选:
            print("[排查] 计算器元素表：")
            for e in scan1["elements"][:60]:
                print("  ", e["id"], e["type"], repr(e["text"][:20]),
                      "click" if e.get("clickable") else "-", e["source"])
            raise AssertionError("找不到「7」按钮")
        七 = 候选[0]
        cx7, cy7 = (七["bbox"][0] + 七["bbox"][2]) // 2, (七["bbox"][1] + 七["bbox"][3]) // 2

        st = stacking.stack(limit=60)
        tk窗 = next(w for w in st["windows"] if "识屏自测靶" in w["title"])
        # 挪 tk 靶：使其中心盖住「7」按钮（SWP_NOSIZE=0x1，HWND_TOP 置顶）
        user32 = ctypes.windll.user32
        user32.SetWindowPos(tk窗["hwnd"], 0, cx7 - 210, cy7 - 130, 0, 0, 0x1)
        # SetWindowPos 只升 Z 序不改前台焦点——显式激活 tk 靶当「当前窗」
        uia_engine.activate_window("识屏自测靶")
        time.sleep(0.8)
        st = stacking.stack(limit=60)
        tk窗 = next(w for w in st["windows"] if "识屏自测靶" in w["title"])

        # ---------- A1 前台判定 ----------
        判("A1 stack 前台=识屏自测靶",
           st["foreground"] and "识屏自测靶" in st["foreground"]["title"],
           "前台=%s" % (st["foreground"] or {}).get("title"))

        # ---------- A2 遮挡量化 + A6 后台 scan 摘要 ----------
        occ = stacking.occlusion(calc_hwnd)
        判("A2 occlusion 可见率<1 且遮挡者=自测靶",
           occ["visible_ratio"] < 1.0
           and any("识屏自测靶" in b["title"] for b in occ["blockers"]),
           "ratio=%s blockers=%s" % (occ["visible_ratio"],
                                     [b["title"][:12] for b in occ["blockers"][:2]]))
        scanbg = cli("scan", "--win", "计算器", "--bg")
        mo = scanbg.get("meta", {}).get("occlusion")
        判("A6 scan --bg meta.occlusion 摘要",
           isinstance(mo, dict) and mo.get("visible_ratio", 1) < 1.0,
           "meta.occlusion=%s" % json.dumps(mo, ensure_ascii=False))

        # ---------- A3 被遮挡物理点击 → 事前拦截 ----------
        os.chdir(BASE)
        r3 = cli("click", str(七["id"]), "--physical")
        top = r3.get("top_window") or {}
        判("A3 遮挡态点击被拦截",
           r3.get("ok") is False and r3.get("method") == "blocked"
           and "识屏自测靶" in (top.get("title") or ""),
           "error=%s top=%s" % ((r3.get("error") or "")[:40], top.get("title")))

        # ---------- A5 click_xy hit_window 反馈 + top_must 拦截 ----------
        tk按钮x, tk按钮y = tk窗["rect"][0] + 210, tk窗["rect"][1] + 130
        r5a = cli("clickxy", str(tk按钮x), str(tk按钮y), "--top-must", "识屏自测靶")
        hw = r5a.get("hit_window") or {}
        判("A5a click_xy 相符放行且反馈命中窗",
           r5a.get("ok") is True and "识屏自测靶" in (hw.get("title") or ""),
           "hit_window=%s" % hw.get("title"))
        r5b = cli("clickxy", str(tk按钮x), str(tk按钮y), "--top-must", "计算器")
        判("A5b top_must 不符拦截未点击",
           r5b.get("ok") is False and r5b.get("method") == "blocked",
           "error=%s" % (r5b.get("error") or "")[:40])

        # ---------- A4 置前后同一元素物理点击生效 ----------
        cli("activate", "--win", "计算器")
        time.sleep(0.6)
        r4 = cli("click", str(七["id"]), "--physical")
        判("A4a 置前拦截解除点击发出",
           r4.get("ok") is True and r4.get("method") == "pyautogui_click",
           "method=%s at=%s" % (r4.get("method"), r4.get("at")))
        time.sleep(0.8)
        scan2 = cli("scan", "--win", "计算器")
        文本们 = " ".join(e.get("text", "") for e in scan2.get("elements", []))
        判("A4b 业务生效（显示区出现 7）", "7" in 文本们,
           "显示区元素含7=%s" % ("7" in 文本们))

    finally:
        try:
            靶.kill()
        except Exception:
            pass
        杀("CalculatorApp.exe")
    过 = sum(1 for _, ok in 结果账 if ok)
    print("\n===== 堆叠感知验收：%d/%d 过 =====" % (过, len(结果账)))
    return 0 if 过 == len(结果账) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
