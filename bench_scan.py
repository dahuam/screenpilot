# -*- coding: utf-8 -*-
"""scan 串行 vs 并行 同进程公平基准（ScreenPilot）。

同一窗口、同进程、热模型下各跑 N 轮：
- 串行版：按旧 do_scan 逻辑（截图 → UIA → OCR → 视觉 顺序执行）
- 并行版：sp.pipeline.scan（UIA 线程 A / OCR+视觉 线程 B 与主线程截图并行）

用法：python bench_scan.py <窗口标题子串> [轮数=3]
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import time
import threading

from sp import enable_dpi_awareness, capture, fuse, ocr_engine, uia_engine, visual_engine, pipeline

enable_dpi_awareness()


def 串行scan(win, max_nodes=800):
    """旧版串行逻辑（与改造前 do_scan 等价），返回耗时 ms 与元素数。"""
    开始 = time.perf_counter()
    sw, sh = capture.screen_size()
    if win:
        wc = uia_engine.find_window_control(win)
        wc.SetActive()
        time.sleep(0.35)
        rect = wc.BoundingRectangle
        l, t = max(0, int(rect.left)), max(0, int(rect.top))
        r, b = min(sw, int(rect.right)), min(sh, int(rect.bottom))
        img = capture.shot_region(l, t, r, b)
        uia列表 = uia_engine.scan_uia(root=wc, screen_wh=(sw, sh), max_nodes=max_nodes)
    else:
        img = capture.shot_full()
        uia列表 = uia_engine.scan_uia(root=None, screen_wh=(sw, sh), max_nodes=max_nodes)
    偏移 = (l, t) if win else (0, 0)
    ocr列表 = ocr_engine.scan_ocr(img)
    for o in ocr列表:
        o.bbox = [o.bbox[0] + 偏移[0], o.bbox[1] + 偏移[1], o.bbox[2] + 偏移[0], o.bbox[3] + 偏移[1]]
    visual列表 = visual_engine.scan_visual(img)
    for v in visual列表:
        v.bbox = [v.bbox[0] + 偏移[0], v.bbox[1] + 偏移[1], v.bbox[2] + 偏移[0], v.bbox[3] + 偏移[1]]
    元素 = fuse.fuse(uia列表, ocr列表, visual列表)
    fuse.assign_ids(元素, screen_wh=(sw, sh))
    return int((time.perf_counter() - 开始) * 1000), len(元素)


def 并行scan(win, max_nodes=800):
    开始 = time.perf_counter()
    结果 = pipeline.scan(win=win, max_nodes=max_nodes)
    return 结果["meta"]["duration_ms"], 结果["meta"]["counts"]["total"]


def main():
    win = sys.argv[1] if len(sys.argv) > 1 else "计算器"
    if win in ("全屏", "full", "-"):
        win = None  # 全屏模式
    轮数 = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    # 预热：OCR 模型加载 + UIA COM 初始化，排除首轮噪声
    ocr_engine.get_ocr()
    串行scan(win)

    串行ms, 并行ms = [], []
    n串 = n并 = 0
    for i in range(轮数):
        ms, n = 串行scan(win)
        串行ms.append(ms); n串 = n
        print("串行 #%d: %dms (%d 元素)" % (i + 1, ms, n))
    for i in range(轮数):
        ms, n = 并行scan(win)
        并行ms.append(ms); n并 = n
        print("并行 #%d: %dms (%d 元素)" % (i + 1, ms, n))
    均 = lambda xs: sum(xs) / len(xs)
    print("====")
    print("窗口: %s | 轮数: %d" % (win or "全屏", 轮数))
    print("串行均值: %.0fms (%d 元素) | 并行均值: %.0fms (%d 元素) | 提速: %.0f%%（耗时降至 %.0f%%）"
          % (均(串行ms), n串, 均(并行ms), n并, (1 - 均(并行ms) / 均(串行ms)) * 100, 均(并行ms) / 均(串行ms) * 100))


if __name__ == "__main__":
    main()
