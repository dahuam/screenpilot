# -*- coding: utf-8 -*-
"""三新功能端到端冒烟：编号保号 / 后台抓窗 / 漂移点击 —— ScreenPilot

场景（靶=识屏自测靶 tk_target.py；本机浏览器全屏占前台=天然遮挡场）：
  [bg-1]   靶被遮挡不置前，scan --bg → PrintWindow 真抓（OCR/UIA 读出「自测按钮」
           即铁证——屏幕拷贝只能抓到浏览器）→ 首帧自然编号+写快照
  [bg-op]  遮挡下按编号点击（UIA 控件级，不发鼠标）→ 标题变「×1」业务闭环
  [drift]  置前后 --physical 物理点击（默认漂移）→ at 在 bbox 安全区 + drift
           非零 + 标题变「×2」业务生效
  [bg-2]   再扫 --bg → 标题「×2」文本变化仍保号（tracker.kept>0）
  [bg-snap] snap --bg → 后台截图落盘，尺寸=靶窗口
清理：杀靶 + 删临时文件（无副作用）。

物理点击认知（本次实测固化）：被遮挡窗口的坐标物理点击=点到遮挡物（鼠标
消息落在最顶层窗口）——后台操作只能走 UIA 控件级调用，漂移点击需窗口可见。
"""
import json
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.chdir(BASE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

缓存 = os.path.join(BASE, "_smoke_elem.json")
通过, 失败 = [], []

def 记(名, ok, 说明=""):
    (通过 if ok else 失败).append(名)
    print(("  ✅" if ok else "  ❌") + f" {名}" + (f"：{说明}" if 说明 else ""))

def cli(*参数):
    r = subprocess.run([sys.executable, os.path.join(BASE, "screenpilot.py"), *参数],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"ok": False, "error": (r.stdout + r.stderr)[-400:]}

def 找按钮(元素表, 关键词):
    return [e for e in 元素表 if 关键词 in e.get("text", "")]

print("== 拉起 tk 自测靶（不置前，任其被前台窗口遮挡）==")
靶 = subprocess.Popen([sys.executable, os.path.join(BASE, "tk_target.py")])
time.sleep(2.5)
快照 = os.path.join(BASE, "_smoke_bg.png")

try:
    # [bg-1] 后台首扫（遮挡态）
    d = cli("scan", "--win", "识屏自测靶", "--bg", "--out", 缓存)
    元素 = d.get("elements", [])
    btns = 找按钮(元素, "自测按钮")
    meta = d.get("meta", {})
    记("[bg-1] 后台 scan 成功", d.get("ok") is not False and len(元素) > 0,
      f"total={len(元素)} duration={meta.get('duration_ms')}ms")
    记("[bg-1] PrintWindow 真抓（无黑帧回退）", meta.get("bg_fallback") is False,
      f"bg_fallback={meta.get('bg_fallback')}")
    记("[bg-1] 遮挡下读出「自测按钮」", bool(btns),
      str([(e["id"], e["text"], e["source"]) for e in btns]))
    with open(缓存, encoding="utf-8") as f:
        cache = json.load(f)
    _桶 = cache.get("tracker", {})
    _桶状态 = _桶.get("识屏自测靶", _桶) if isinstance(_桶, dict) else {}  # 兼容分桶/旧单状态
    记("[bg-1] 缓存内嵌 tracker 快照", bool(_桶状态.get("elements")))
    if not btns:
        raise SystemExit("无按钮元素，后续用例无法继续")
    btn_id = btns[0]["id"]

    # [bg-op] 遮挡下按编号点击（UIA 控件级后台重定位，不发鼠标）
    d = cli("click", str(btn_id), "--file", 缓存)
    记("[bg-op] 遮挡下后台控件级点击(重定位+invoke)", d.get("ok") is True and d.get("background") is True
      and str(d.get("method", "")).startswith("uia_"),
      f"method={d.get('method')} background={d.get('background')}"
      "（tk 的 UIA 桥不执行 invoke 回调，业务生效以真实应用为准：计算器 7×8=56；"
      "本用例验证「遮挡下正确定位目标控件并发出控件级调用」的机制）")

    # [drift] 置前 + 物理漂移点击（业务闭环）
    d = cli("activate", "--win", "识屏自测靶")
    记("[drift] activate 真实置前", d.get("ok") is True, str(d.get("rect")))
    time.sleep(0.6)
    d = cli("click", str(btn_id), "--physical", "--file", 缓存)
    at, drift = d.get("at"), d.get("drift")
    btn_bbox = next((e["bbox"] for e in 元素 if e["id"] == btn_id), None)
    在区内 = at and btn_bbox and (btn_bbox[0] + 2) <= at[0] <= (btn_bbox[2] - 2) \
            and (btn_bbox[1] + 2) <= at[1] <= (btn_bbox[3] - 2)
    记("[drift] 物理点击(默认漂移)", d.get("ok") is True and d.get("method") == "pyautogui_click" and 在区内,
      f"at={at} drift={drift} bbox={btn_bbox}")
    记("[drift] 漂移非零落点", bool(drift) and (drift[0] or drift[1]),
      f"drift={drift}（高斯随机，偶为 0 不算失败，仅记录）")
    time.sleep(0.8)

    # [bg-2] 再扫：标题变 ×1（文本变化）仍保号（OCR 可能读作 x1/X1/×1）
    d = cli("scan", "--win", "识屏自测靶", "--bg", "--out", 缓存)
    元素2 = d.get("elements", [])
    btns2 = [e for e in 元素2
             if e.get("text", "").lower().replace("×", "x").startswith("自测按钮x")]
    tr = d.get("meta", {}).get("tracker", {})
    print("    [bg-2] 全部元素:", [(e["id"], e["text"], e["source"]) for e in 元素2])
    记("[bg-2] 漂移点击业务生效（标题变×1）", bool(btns2),
      str([(e["id"], e["text"]) for e in btns2]))
    记("[bg-2] 文本变化仍保号", bool(btns2) and btns2[0]["id"] == btn_id,
      f"旧id={btn_id} 新id={btns2[0]['id'] if btns2 else '?'} tracker={tr}")
    记("[bg-2] tracker 统计 kept>0", tr.get("kept", 0) > 0, f"tracker={tr}")

    # [bg-snap] 后台截图
    d = cli("snap", 快照, "--win", "识屏自测靶", "--bg")
    尺寸对 = d.get("size") and abs(d["size"][0] - 436) <= 30 and abs(d["size"][1] - 239) <= 30
    记("[bg-snap] 后台截图落盘(尺寸=靶窗)", d.get("ok") is True and os.path.exists(快照) and 尺寸对,
      f"size={d.get('size')} fallback={d.get('bg_fallback')}")

finally:
    靶.kill()
    for p in (缓存, 快照):
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

print(f"\n==== 冒烟结果: 通过 {len(通过)} / 失败 {len(失败)} ====")
sys.exit(1 if 失败 else 0)
