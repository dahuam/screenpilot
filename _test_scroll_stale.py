# -*- coding: utf-8 -*-
"""滚动复用 + stale 盲区修复 验收脚本（2026-10-31「识屏第三单」）。

修复对象（交接单 f1945899 依据段暴露的三条未修项）：
  1 滚动场景增量形同虚设：朴素差分把滚动判成整窗变化（实测 93~100% 超全量
     线 60%）直接退全量——平移检测修复后滚动帧走增量（新露出条带重扫）。
  2 stale 盲区：窗口尺寸没变但页面滚动，find 仍返回 stale=false 错坐标
     （独立复验发现 3 例）——滚动帧入库把域内旧条目标 stale_scroll，查找
     如实报 stale=true；亲眼重扫到的条目自动清标记；另附 age_s 陈旧度。
  3 OCR 固定开销顶死提速（det 'min' 模式把小裁块放大到短边 736，300x120
     实测 297ms）——快速实例（'max'+关 cls）同块 56ms 文本逐字一致。

用法：python _test_scroll_stale.py
靶纪律：本地 HTML（Edge 开、用完 WM_CLOSE 优雅关）；计算器不用（本套
不依赖）；进程纪律=单进程串行，收尾进程清零。
"""
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

根 = os.path.dirname(os.path.abspath(__file__))
结果 = []


def 记(用例, ok, 说明):
    结果.append((用例, bool(ok)))
    print(("PASS" if ok else "FAIL") + " | " + 用例 + " | " + 说明)


def 关窗按标题(标题子串):
    """WM_CLOSE 优雅关闭标题含子串的可见顶层窗口（Edge 进程树杀不得）。"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    窗口s = []
    枚举 = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(h, l):
        n = user32.GetWindowTextLengthW(h)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            窗口s.append((h, buf.value))
        return True
    user32.EnumWindows(枚举(cb), 0)
    n = 0
    for h, t in 窗口s:
        if 标题子串 in t and user32.IsWindowVisible(h):
            user32.PostMessageW(h, 0x0010, 0, 0)   # WM_CLOSE
            n += 1
    return n


长页HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>滚动复用靶</title></head><body style="margin:0;font-size:30px;font-family:微软雅黑">
<script>
for (var i = 0; i < 40; i++) {
  document.write('<p style="margin:34px">段落第' + i + '行 滚动复用靶文本内容' + i + '</p>');
}
document.write('<p id="bottom" style="color:red;font-size:44px;font-weight:bold;margin:40px">页底元素滚动复用靶底</p>');
</script></body></html>"""


def main():
    import ctypes
    from ctypes import wintypes

    import numpy as np
    import pyautogui
    import cv2

    from sp import enable_dpi_awareness
    from sp.pipeline import _平移检测, _差分矩形
    import sp.booklib as B

    enable_dpi_awareness()
    user32 = ctypes.windll.user32

    # ── S1 平移检测单元（合成图）──
    rng = np.random.RandomState(7)
    base = np.full((941, 1216, 3), 245, dtype=np.uint8)
    for _ in range(60):
        x, y = rng.randint(0, 1150), rng.randint(0, 860)
        cv2.putText(base, "txt", (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (30, 30, 30), 2)
    dy真 = 100
    cur = np.full_like(base, 245)
    cur[:941 - dy真] = base[dy真:]
    cur[941 - dy真:] = rng.randint(200, 255, (dy真, 1216, 3), dtype=np.uint8)
    r = _平移检测(base, cur)
    记("S1a 平移检测检出 dy", r is not None and abs(r[0] - dy真) <= 12,
       "检出=%s（真值 %d）" % (r[0] if r else None, dy真))
    记("S1b 静止帧不误判", _平移检测(base, base.copy()) is None,
       "静止帧返回 None")
    cur2 = np.full_like(base, 245)
    for _ in range(60):
        x, y = rng.randint(0, 1150), rng.randint(0, 860)
        cv2.putText(cur2, "xyz", (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (30, 30, 30), 2)
    记("S1c 纯内容替换不误判平移", _平移检测(base, cur2) is None,
       "非平移大变化返回 None（照旧走 fallback 全量）")

    # ── S1d 对齐差分：变化面积从近全窗降到条带+残余 ──
    _r0, 占比朴素 = _差分矩形(base, cur)
    _r1, 占比对齐 = _差分矩形(base, cur, 平移dy=(r[0] if r else 100))
    含条带 = any(abs(x[3] - 941) < 40 for x in _r1)
    记("S1d 对齐差分变化面积骤降", 占比对齐 < 占比朴素 * 0.7 and 含条带,
       "朴素 %.0f%% → 对齐 %.0f%%（降 %.0f%%），含新露出条带=%s"
       % (占比朴素 * 100, 占比对齐 * 100, (1 - 占比对齐 / max(占比朴素, 1e-9)) * 100, 含条带))

    # ── S2/S3 滚动真靶：增量生效 + scroll_shift + find stale 生命周期 ──
    # 靶载体=tk 自建 Canvas 滚动靶（同进程 yview_moveto 即时跳滚，无平滑
    # 动画、无前台竞争）。Edge 靶弃用根因：本机 pyautogui 合成滚轮对 Edge
    # 无效（clicks 语义仅滚个位 px）、后台进程 SetForegroundWindow 被前台
    # 锁拒绝致 ↓ 键被地址栏吃掉——滚动输入不可控（环境坑，报告已记）。
    try:
        import tkinter as tk

        靶窗 = tk.Tk()
        靶窗.title("滚动复用靶tk")
        靶窗.geometry("900x1100+40+90")
        画布 = tk.Canvas(靶窗, width=880, height=1060,
                         scrollregion=(0, 0, 880, 4000), bg="white")
        画布.pack(side="left", fill="both", expand=True)
        滚条 = tk.Scrollbar(靶窗, command=画布.yview)
        滚条.pack(side="right", fill="y")
        画布.config(yscrollcommand=滚条.set)
        for i in range(40):
            画布.create_text(24, 50 + i * 95,
                             text="段落第%d行 滚动复用靶文本内容%d" % (i, i),
                             anchor="nw", font=("微软雅黑", 16, "bold"), fill="black")
        靶窗.update()
        靶窗.attributes("-topmost", True)
        靶窗.lift()
        靶窗.focus_force()
        靶窗.update()
        time.sleep(0.5)

        def 直扫(增量=True):
            # 走 CLI 子进程（跨进程 UIA），主线程只负责给 tk 窗 pump 消息。
            # 同进程直调 scan 曾两次死锁：scan 的 UIA 线程取 tk 窗矩形依赖
            # 窗口所属线程 pump（WM_GETOBJECT），全量帧/增量帧路径不同、超时
            # 表现不稳定——子进程彻底隔离 COM 情绪（最小复现已验跨进程可取）
            盒 = {}
            参数 = ["scan", "--win", "滚动复用靶tk", "--no-focus"] + (
                ["--incremental"] if 增量 else [])

            def 跑():
                r = subprocess.run([sys.executable, os.path.join(根, "screenpilot.py"), *参数],
                                   capture_output=True, timeout=120)
                import json as _json
                out = r.stdout.decode("utf-8", "replace").strip()
                try:
                    盒["r"] = _json.loads(out.splitlines()[-1])
                except Exception:
                    盒["r"] = {"meta": {}, "elements": [],
                               "_err": r.stderr.decode("utf-8", "replace")[-200:]}

            th = threading.Thread(target=跑, daemon=True)
            th.start()
            while th.is_alive():
                靶窗.update()
                time.sleep(0.02)
            return 盒["r"]

        # 首帧（退全量存基线）——等 OCR 能读到段落
        首帧OCR = set()
        p = None
        for _ in range(8):
            p = 直扫()
            首帧OCR = set(e.get("text") for e in p.get("elements") or []
                          if e.get("source") == "ocr" and e.get("text"))
            if any("段落第0行" in t for t in 首帧OCR):
                break
            time.sleep(1)
            靶窗.update()
        滚前域 = (p["meta"].get("book") or {}).get("domain") if p else None
        滚前命中 = B.查找("段落第0行", domain=滚前域)
        滚前stale = [h for h in 滚前命中 if h.get("stale") is False]

        # 滚动：yview_moveto 即时跳滚 0.12×(4000-1060)=353px——一次滚轮的
        # 典型幅度，落在搜索范围 ±480px 内（且不超 窗高/2 的对齐重叠下限）；
        # 跳滚后 tk 重绘一次到位（无动画中间态）
        画布.yview_moveto(0.12)
        靶窗.update()
        time.sleep(0.6)
        q = 直扫()
        m3 = q.get("meta") or {}
        # 滚动帧走增量=修复主目标（修复前滚动即 93~101% 超 60% 全量线被退
        # 全量）；平移检出（scroll_shift）与内容位移兜底（content_shifted，
        # 周期性内容/亚像素渲染下平移检出的保守兜底）至少一路报出
        位移报出 = (isinstance(m3.get("scroll_shift"), list)
                    and abs(m3["scroll_shift"][1]) >= 16) or \
                   m3.get("content_shifted") is True
        滚动生效 = m3.get("incremental") is True and 位移报出
        新OCR = set(e.get("text") for e in q.get("elements") or []
                    if e.get("source") == "ocr" and e.get("text")) - 首帧OCR
        记("S2 滚动帧走增量（不再 fallback 全量）", 滚动生效,
           "incremental=%s scroll_shift=%s content_shifted=%s diff=%s rects=%d 新进OCR=%s 退因=%r"
           % (m3.get("incremental"), m3.get("scroll_shift"),
              m3.get("content_shifted"),
              (m3.get("incremental_stats") or {}).get("diff_ratio"),
              len(m3.get("refreshed_rects") or []), sorted(新OCR)[:4],
              m3.get("incremental_fallback")))

        # S3 滚动后 find：滚前条目 stale=true（reason 含滚动/内容变化）；age_s
        滚后命中 = B.查找("段落第0行", domain=滚前域)
        staled = [h for h in 滚后命中
                  if h.get("stale") and ("滚动" in (h.get("reason") or "")
                                         or "大幅变化" in (h.get("reason") or ""))]
        带age = all(isinstance(h.get("age_s"), int) for h in 滚后命中) if 滚后命中 else False
        记("S3a 滚动后 find 如实报 stale", bool(staled) and bool(滚前stale),
           "滚前 stale=false %d 条 → 滚后 stale=true(含滚动 reason) %d 条；age_s 全带=%s"
           % (len(滚前stale), len(staled), 带age))

        # 复扫（视口内元素被亲眼重扫）：同键条目清标记 stale=false——
        # 用滚动后视口内可见的段落（第6行；第0行已滚出视口，保持 stale 才对）
        直扫(增量=False)   # 全量帧：视口内全部元素重入库
        复扫命中 = B.查找("段落第6行", domain=滚前域)
        可信 = [h for h in 复扫命中 if h.get("stale") is False]
        记("S3b 重扫后同键条目清标记", bool(可信),
           "全量重扫后视口内「段落第6行」stale=false %d 条（亲眼重扫清标记）" % len(可信))
        靶窗.destroy()
    except Exception as e:
        记("S2 滚动帧走增量（不再 fallback 全量）", False, "异常: %s" % e)
        记("S3a 滚动后 find 如实报 stale", False, "异常: %s" % e)
        记("S3b 重扫后同键条目清标记", False, "异常: %s" % e)

    # ── S4 OCR 快速实例：小块耗时与文本一致性 ──
    from sp.ocr_engine import scan_ocr, get_ocr
    块 = np.full((120, 300, 3), 255, dtype=np.uint8)
    cv2.putText(块, "7894561", (30, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 0), 3)
    get_ocr(False)   # 双实例初始化在计时外（首初始化 1~2s 是加载不是推理）
    get_ocr(True)
    scan_ocr(块.copy(), 快速=True)   # 预热一跑（模型首推理也慢）
    t0 = time.perf_counter()
    默认 = scan_ocr(块.copy())
    t默认 = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    快速 = scan_ocr(块.copy(), 快速=True)
    t快速 = (time.perf_counter() - t0) * 1000
    文本同 = ([e.text for e in 快速] == [e.text for e in 默认])
    记("S4 OCR 快速实例提速且文本一致", t快速 < t默认 * 0.5 and 文本同 and 快速,
       "默认 %.0fms → 快速 %.0fms（%.1f 倍）文本一致=%s 结果=%s"
       % (t默认, t快速, t默认 / max(1, t快速), 文本同,
          [e.text for e in 快速]))

    过 = sum(1 for _名, ok in 结果 if ok)
    print("\n===== 滚动复用+stale盲区验收 %d/%d =====" % (过, len(结果)))
    return 0 if 过 == len(结果) else 1


if __name__ == "__main__":
    sys.exit(main())
