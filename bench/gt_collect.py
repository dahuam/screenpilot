# -*- coding: utf-8 -*-
"""GT 半自动构建·采集器（ScreenPilot·基准评测施工单 B）。

对指定场景：搭台 → pipeline.scan 连跑 3 次（与 GT 规程一致）→ 落盘
  bench/scenarios/gt_build/<key>/
    raw_<i>.png        原始截图（人工核对漏识用）
    annotated_<i>.png  标注图（红=UIA 蓝=OCR 绿=视觉，编号可对元素）
    scan_<i>.json      meta + elements（屏幕坐标）

用法：python bench/gt_collect.py <scenario_key> [场景key2 ...]
不自动收尾——收尾由 bench_final.py 统一执行（GT 构建期间要反复扫）。
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import os
import json
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sp import enable_dpi_awareness  # noqa: E402
enable_dpi_awareness()

from sp import annotate, capture, pipeline  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenario_defs as sd  # noqa: E402


def collect(key, rounds=3):
    sc = sd.SCENARIOS[key]
    out_dir = os.path.join(ROOT, "bench", "scenarios", "gt_build", key)
    os.makedirs(out_dir, exist_ok=True)

    setup = sd.setup_scenario(key)
    print("setup:", json.dumps(setup, ensure_ascii=False))
    if not setup.get("ok"):
        print("搭台失败，中止场景", key)
        return 1

    for i in range(1, rounds + 1):
        if sc["mode"] == "window":
            if sc.get("find") == "hwnd_class":
                tgt = sd.resolve_target(key)
                if not tgt.get("ok"):
                    print("目标窗口丢失:", tgt.get("error"))
                    return 1
                res = pipeline.scan(hwnd=tgt["hwnd"], tracker_state=None)
            else:
                res = pipeline.scan(win=sc["win"], tracker_state=None)
        else:
            res = pipeline.scan(win=None, tracker_state=None)
        meta, els, img = res["meta"], res["elements"], res["img"]
        capture.save_image(img, os.path.join(out_dir, f"raw_{i}.png"))
        annotate.annotate(img, els, os.path.join(out_dir, f"annotated_{i}.png"),
                          offset=meta["offset"])
        data = {"meta": meta, "elements": [e.to_dict() for e in els]}
        with open(os.path.join(out_dir, f"scan_{i}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        print(f"[{key}] scan{i}: {meta['duration_ms']}ms, {len(els)} 元素, "
              f"counts={meta['counts']}, errors={meta['errors']}")
        time.sleep(0.6)
    return 0


def main(argv):
    keys = argv[1:]
    if not keys:
        print("用法：python bench/gt_collect.py <scenario_key> [...]; 可选：",
              ",".join(sd.SCENARIOS))
        return 2
    rc = 0
    for k in keys:
        if k not in sd.SCENARIOS:
            print("未知场景:", k)
            rc = 2
            continue
        try:
            rc |= collect(k)
        except Exception as e:
            print(f"[{k}] 采集异常: {e}")
            rc |= 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
