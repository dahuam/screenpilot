# -*- coding: utf-8 -*-
"""时间维度四件套 验收 —— ScreenPilot（2026-11-01 B1 转正）

按 POC 原达标线复验（上轮交接 next_steps 三）：
  T1 抠底分层：UI 文本保留率≥95%；稳定框误收中位数≤10% 且超标框≤2；
     稳定移动轨迹≥1；产物三件齐（POC 判据①②③；真靶成绩 98.4%/0%/9 条）
  T2 连拍 toast：10 轮捕获 ≥9（POC v2 达标线，真靶 10/10）
  T3 一隐一显：50 次（40 真打+10 干扰）准确率 ≥95%（POC v3 达标线外推，
     真靶 20/20）
  T4 ESC 卡死：5 假死+5 健康，误差 ≤1（POC v4 达标线，真靶 0 误差）
  T5 三端：CLI watch/verify/hang/split 四子命令通道可用（MCP 工具的入口与
     参数穿透由 _test_capability_matrix.py 静态校验；MCP 真通道机制已有
     _test_mcp.py 覆盖，本脚本不重复起 MCP 子进程）

纪律：前台干扰型用例串行跑；tk 靶 place 绝对定位；
靶子 taskkill 即清、收尾删状态文件。

用法：python _test_watch.py [--skip-long]
  --skip-long 跳过 T3 的 50 轮打字（调试用，正式验收必须全跑）
"""
import json
import os
import random
import string
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from sp import enable_dpi_awareness  # noqa: E402

enable_dpi_awareness()

from sp import executor, watch  # noqa: E402

靶路径 = os.path.join(BASE, "_tk_watch_target.py")
状态文件 = os.path.join(BASE, "_tk_watch_state.json")
验收目录 = os.path.join(BASE, "验收_20261101_时间维度")
靶标题 = "识屏watch靶"

结果账 = []


def 判(名, ok, 证据=""):
    结果账.append({"case": 名, "pass": bool(ok), "evidence": str(证据)})
    print(("[%s] %s %s" % ("✅过" if ok else "❌败", 名, 证据)), flush=True)


def 读状态():
    try:
        with open(状态文件, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def 等布局(超时=15.0):
    t0 = time.time()
    while time.time() - t0 < 超时:
        st = 读状态()
        lay = st.get("layout") or {}
        if lay.get("save") and lay.get("echo"):
            return st
        time.sleep(0.2)
    raise RuntimeError("靶布局未就绪（状态文件=%s）" % 状态文件)


def 中心(box):
    return ((int(box[0]) + int(box[2])) // 2, (int(box[1]) + int(box[3])) // 2)


def 扩(box, pad=4):
    return [int(box[0]) - pad, int(box[1]) - pad,
            int(box[2]) + pad, int(box[3]) + pad]


def 区域串(box):
    return "%d,%d,%d,%d" % (int(box[0]), int(box[1]), int(box[2]), int(box[3]))


def cli(*参数, 超时=120):
    r = subprocess.run([sys.executable, os.path.join(BASE, "screenpilot.py"), *参数],
                       capture_output=True, timeout=超时)
    out = r.stdout.decode("utf-8", "replace").strip().splitlines()
    if out:
        try:
            return json.loads(out[-1])
        except Exception:
            pass
    return {"ok": False, "error": r.stderr.decode("utf-8", "replace")[-300:]}


def T0_底层():
    import numpy as np
    a = np.zeros((60, 80, 3), np.uint8)
    b = a.copy()
    b[10:30, 20:50] = 255
    r0, b0 = watch.frame_diff(a, a)
    r1, b1 = watch.frame_diff(a, b)
    判("T0a frame_diff 同图零变化", r0 == 0.0 and not b0, "ratio=%.5f" % r0)
    判("T0b frame_diff 变化检出+连通域", r1 > 0.01 and len(b1) >= 1,
       "ratio=%.4f boxes=%d" % (r1, len(b1)))
    r2, _ = watch.frame_diff(a, np.zeros((30, 40, 3), np.uint8))
    判("T0c 尺寸不一致按 1.0 如实报", r2 == 1.0, "ratio=%.2f" % r2)


def T1_抠底分层(hwnd):
    print("\n=== T1 抠底动静态分层（POC 判据①②③） ===", flush=True)
    time.sleep(1.0)  # 动画进入稳定状态
    out = os.path.join(验收目录, "split")
    r = watch.split_static_dynamic(hwnd=hwnd, frames=8, interval=0.5,
                                   verify_ocr=True, out_dir=out)
    ocr = r.get("ocr") or {}
    判("T1a split 执行成功", r.get("ok"), r.get("error", ""))
    if not r.get("ok"):
        return r
    判("T1b OCR 参照框≥5（判据前提）", ocr.get("ref_boxes", 0) >= 5,
       "ref_boxes=%s" % ocr.get("ref_boxes"))
    判("T1c UI 文本保留率≥95%", ocr.get("ui_keep_rate", 0) >= 0.95,
       "keep=%s" % ocr.get("ui_keep_rate"))
    判("T1d 稳定框误收中位数≤10%", ocr.get("median_entity_ratio", 1) <= 0.10,
       "median=%s stable_boxes=%s" % (ocr.get("median_entity_ratio"),
                                     ocr.get("stable_boxes")))
    判("T1e 超标框（>30%）≤2", ocr.get("bad_count", 99) <= 2,
       "bad=%s %s" % (ocr.get("bad_count"), ocr.get("bad_boxes")))
    判("T1f 稳定移动轨迹≥1", r.get("stable_movers", 0) >= 1,
       "movers=%s tracks=%s anims=%s" % (r.get("stable_movers"),
                                         r.get("tracks"), r.get("anims")))
    arts = r.get("artifacts") or {}
    判("T1g 产物三件齐", all(os.path.isfile(p) for p in arts.values()),
       ", ".join(os.path.basename(p) for p in arts.values()))
    return r


def T2_连拍(hwnd, 布局):
    print("\n=== T2 连拍窗抓 toast（10 轮，达标≥9） ===", flush=True)
    保存点 = 中心(布局["save"])
    捕获 = 0
    明细 = []
    for i in range(10):
        time.sleep(2.0)  # 等上一发 toast 彻底消失
        前 = 读状态().get("saved_n", 0)
        r = watch.watch_burst(
            hwnd=hwnd, seconds=3.0, interval=0.2,
            actions=[{"kind": "click", "x": 保存点[0], "y": 保存点[1],
                      "top_must": 靶标题}])
        后 = 读状态().get("saved_n", 0)
        ok = bool(r.get("ok")) and r.get("hit_frames", 0) >= 1
        捕获 += ok
        明细.append({"round": i + 1, "hit_frames": r.get("hit_frames"),
                    "first_hit_t": r.get("first_hit_t"),
                    "max_ratio": r.get("max_ratio"),
                    "saved_n": [前, 后], "ok": bool(r.get("ok")),
                    "error": r.get("error")})
        print("  第%d轮: hit_frames=%s 首个@%ss ratio=%s saved_n %s→%s"
              % (i + 1, r.get("hit_frames"), r.get("first_hit_t"),
                 r.get("max_ratio"), 前, 后), flush=True)
    判("T2 连拍 toast 捕获≥9/10", 捕获 >= 9, "捕获=%d/10" % 捕获)
    真值增 = [d for d in 明细 if d["saved_n"][1] > d["saved_n"][0]]
    判("T2b 真值核对 saved_n 10 轮全增", len(真值增) == 10,
       "增=%d/10" % len(真值增))
    return 明细


def T3_打字(hwnd, 布局, 跳过=False):
    print("\n=== T3 一隐一显打字（50 次：40 真打+10 干扰，达标≥95%%） ===", flush=True)
    if 跳过:
        判("T3 一隐一显（--skip-long 跳过）", False, "未跑，不计入正式验收")
        return []
    回显区 = 扩(布局["echo"], 4)
    输入点 = 中心(布局["entry"])
    标题点 = 中心(布局["title"])
    对 = 0
    明细 = []
    for i in range(50):
        干扰 = (i % 5 == 4)
        time.sleep(0.25)
        if 干扰:
            动作 = [{"kind": "click", "x": 标题点[0], "y": 标题点[1],
                     "top_must": 靶标题}]
            期望 = False
            串 = "(干扰)"
        else:
            串 = "".join(random.choice(string.ascii_lowercase + string.digits)
                        for _ in range(6))
            动作 = [{"kind": "click", "x": 输入点[0], "y": 输入点[1],
                     "top_must": 靶标题},
                    {"kind": "key", "key": "ctrl+a"},
                    {"kind": "paste", "text": 串}]
            期望 = True
        r = watch.verify_typing(hwnd=hwnd, region=回显区, actions=动作,
                                wait=1.2, interval=0.2)
        判对 = bool(r.get("ok")) and bool(r.get("changed")) == 期望
        对 += 判对
        明细.append({"round": i + 1, "type": "干扰" if 干扰 else "真打",
                    "expect": 期望, "changed": r.get("changed"),
                    "pass": 判对, "error": r.get("error")})
        if (i + 1) % 10 == 0:
            print("  进度 %d/50，当前判对 %d（%.1f%%）"
                  % (i + 1, 对, 对 / (i + 1) * 100), flush=True)
    准确率 = 对 / 50.0
    判("T3 一隐一显 50 次准确率≥95%", 准确率 >= 0.95,
       "判对=%d/50 (%.1f%%)" % (对, 准确率 * 100))
    return 明细


def T4_卡死(hwnd, 布局):
    print("\n=== T4 ESC 卡死探测（5 假死+5 健康，误差≤1） ===", flush=True)
    假死点 = 中心(布局["freeze"])
    标题点 = 中心(布局["title"])
    判对 = 0
    明细 = []
    for i in range(10):
        期望假死 = (i % 2 == 0)
        executor.click_xy(标题点[0], 标题点[1], top_must=靶标题)  # 激活并确认前台
        time.sleep(0.4)
        if 期望假死:
            executor.click_xy(假死点[0], 假死点[1], top_must=靶标题)
            time.sleep(0.4)  # 等冻结生效（点击回调 sleep 开始）
            r = watch.probe_hang(hwnd=hwnd, key="esc", wait=2.0, resend_after=0)
            t0 = time.time()
            while time.time() - t0 < 20.0:
                if 读状态().get("freeze_end"):
                    break
                time.sleep(0.3)
            time.sleep(2.0)  # 恢复后让 UI 静下来
        else:
            r = watch.probe_hang(hwnd=hwnd, key="esc", wait=2.0, resend_after=0.7)
            time.sleep(1.8)  # ESC 已收标签 1.5s 消失
        本次判对 = bool(r.get("ok")) and bool(r.get("hang")) == 期望假死
        判对 += 本次判对
        明细.append({"round": i + 1, "truth_hang": 期望假死,
                    "judged_hang": r.get("hang"), "pass": 本次判对,
                    "is_hung_api": r.get("is_hung_api"),
                    "max_ratio": r.get("max_ratio"), "error": r.get("error")})
        print("  第%d轮: 真值=%s 判定=%s（is_hung_api=%s max_ratio=%s）%s"
              % (i + 1, "假死" if 期望假死 else "健康",
                 "假死" if r.get("hang") else "健康", r.get("is_hung_api"),
                 r.get("max_ratio"), "✅" if 本次判对 else "❌"), flush=True)
    判("T4 ESC 卡死判定误差≤1/10", (10 - 判对) <= 1, "判对=%d/10" % 判对)
    return 明细


def T5_CLI通道(hwnd, 布局):
    print("\n=== T5 CLI 四子命令通道 ===", flush=True)
    回显区 = 扩(布局["echo"], 4)
    out_cli = os.path.join(验收目录, "cli_split")
    r1 = cli("watch", "--win", 靶标题, "--seconds", "0.6", "--interval", "0.2",
             "--region", 区域串(回显区))
    判("T5a CLI watch 通道", r1.get("ok") is True and r1.get("action") == "watch_burst",
       "ok=%s action=%s" % (r1.get("ok"), r1.get("action")))
    r2 = cli("verify", "--win", 靶标题, "--region", 区域串(回显区),
             "--wait", "0.8", "--action", json.dumps({"kind": "key", "key": "tab"}))
    判("T5b CLI verify 通道", r2.get("ok") is True and r2.get("action") == "verify_typing",
       "ok=%s action=%s" % (r2.get("ok"), r2.get("action")))
    r3 = cli("split", "--win", 靶标题, "--frames", "3", "--interval", "0.3",
             "--out-dir", out_cli)
    判("T5c CLI split 通道", r3.get("ok") is True and r3.get("action") == "split_static_dynamic",
       "ok=%s frames=%s" % (r3.get("ok"), r3.get("frames")))
    # hang 要求前台：新进程受 Windows 前台锁影响，允许「通道可用但拒绝判定」
    r4 = cli("hang", "--win", 靶标题, "--wait", "1.2", "--resend-after", "0")
    通道可用 = (r4.get("action") == "probe_hang"
              and (r4.get("ok") is True
                   or "置前" in str(r4.get("error", ""))
                   or "前台" in str(r4.get("error", ""))))
    判("T5d CLI hang 通道", 通道可用,
       "ok=%s hang=%s error=%s" % (r4.get("ok"), r4.get("hang"),
                                  str(r4.get("error", ""))[:60]))
    return {"watch": r1, "verify": r2, "split": r3, "hang": r4}


def main():
    if not os.path.isdir(验收目录):
        os.makedirs(验收目录, exist_ok=True)
    try:
        if os.path.isfile(状态文件):
            os.remove(状态文件)
    except Exception:
        pass
    靶 = subprocess.Popen([sys.executable, 靶路径],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        st = 等布局()
        布局 = st["layout"]
        hwnd = watch.find_window(靶标题)
        if not hwnd:
            raise RuntimeError("找不到靶窗口「%s」" % 靶标题)
        print("靶 hwnd=%s layout=%s" % (hwnd, json.dumps(布局, ensure_ascii=False)),
              flush=True)
        判("T0 前置：find_window 找到靶窗", True, "hwnd=%s" % hwnd)
        T0_底层()
        T1_抠底分层(hwnd)
        T2明细 = T2_连拍(hwnd, 布局)
        T3明细 = T3_打字(hwnd, 布局, 跳过="--skip-long" in sys.argv)
        T4明细 = T4_卡死(hwnd, 布局)
        T5账 = T5_CLI通道(hwnd, 布局)
    finally:
        try:
            靶.kill()
        except Exception:
            pass
        time.sleep(0.5)
        try:
            if os.path.isfile(状态文件):
                os.remove(状态文件)
        except Exception:
            pass
    过 = sum(1 for r in 结果账 if r["pass"])
    总 = len(结果账)
    总账 = {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "target": 靶标题, "pass": 过, "total": 总,
            "results": 结果账}
    try:
        with open(os.path.join(验收目录, "watch_验收_20261101.json"),
                  "w", encoding="utf-8") as f:
            json.dump(总账, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    print("\n===== 时间维度四件套验收：%d/%d 过 =====" % (过, 总), flush=True)
    for r in 结果账:
        if not r["pass"]:
            print("  未过：%s（%s）" % (r["case"], r["evidence"]), flush=True)
    return 0 if 过 == 总 else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
