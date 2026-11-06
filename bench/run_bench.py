# -*- coding: utf-8 -*-
"""ScreenPilot Bench 跑分器（ScreenPilot·基准评测施工单 B）。

一键重跑：python bench/run_bench.py [--rounds 3] [--scenarios a,b,...] [--tag 名称]
                                       [--no-setup] [--no-teardown]

流程（每场景）：搭台 → 定位窗口 → 1 次预热扫（不计分，Edge a11y 懒激活实测必须）
→ N 次计分扫 → 指标计算 → 落盘 bench/results/<tag>/。

指标（沿用融合层判同口径：IoU≥0.3 或中心互含）：
- recall        GT 命中率（match=bbox 元素）；slot 槽位另报（预测中心落格即命中）
- precision     ROI 内预测的命中占比；FPI = 每屏平均假元素数
- 定位误差      bbox 命中对的中心点 px 距离 均值/中位数（槽位对不参与）
- scan 耗时     meta.duration_ms 均值 + P95（进程内热模型口径）
- 分引擎        命中预测按 source 拆（uia/ocr/visual）+ 原始引擎候选数均值
                （融合合并的来源归属最终输出未携带，记 待改进）

只消费现有 sp 能力（sp.pipeline / sp.uia_engine），不改 sp/ 核心代码。
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
import argparse
import statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sp import enable_dpi_awareness  # noqa: E402
enable_dpi_awareness()

from sp import annotate, pipeline  # noqa: E402
import scenario_defs as sd  # noqa: E402

SCENARIO_ORDER = ["calculator", "taskmgr", "explorer", "edge_bench", "startmenu", "ldplayer"]


# ---------------- 匹配几何 ----------------

def iou(a, b):
    il, it = max(a[0], b[0]), max(a[1], b[1])
    ir, ib = min(a[2], b[2]), min(a[3], b[3])
    if ir <= il or ib <= it:
        return 0.0
    inter = (ir - il) * (ib - it)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / float(area_a + area_b - inter)


def center(b):
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


def center_in(c, b):
    return b[0] <= c[0] <= b[2] and b[1] <= c[1] <= b[3]


def matchable(g, p):
    """融合层判同口径：IoU>=0.3 或中心互含。"""
    if iou(g, p) >= 0.3:
        return True
    return center_in(center(g), p) and center_in(center(p), g)


def pair_score(g, p):
    """贪心配对优先级：bbox 对用 IoU（中心互含兜底给小分），槽位对用负中心距。"""
    if g.get("match") == "slot":
        if not center_in(center(p["bbox"]), g["bbox"]):
            return None
        gx, gy = center(g["bbox"])
        px, py = center(p["bbox"])
        return 100.0 - ((gx - px) ** 2 + (gy - py) ** 2) ** 0.5  # 越近越优先
    v = iou(g["bbox"], p["bbox"])
    if v >= 0.3:
        return v
    if matchable(g["bbox"], p["bbox"]):
        return 0.01
    return None


def greedy_match(gts, preds):
    """全局贪心 1:1 配对。返回 (pairs, matched_gt_ids, matched_pred_idx)。"""
    cands = []
    for gi, g in enumerate(gts):
        for pi, p in enumerate(preds):
            s = pair_score(g, p)
            if s is not None:
                cands.append((s, gi, pi))
    cands.sort(key=lambda x: -x[0])
    used_g, used_p, pairs = set(), set(), []
    for s, gi, pi in cands:
        if gi in used_g or pi in used_p:
            continue
        used_g.add(gi)
        used_p.add(pi)
        pairs.append((gi, pi, s))
    return pairs, used_g, used_p


# ---------------- 评测核心 ----------------

def score_scan(gt_elements, pred_elements, roi):
    """单次扫描打分。pred bbox 与 gt_elements bbox 均为屏幕坐标（调用方先转换）。"""
    gts = gt_elements
    in_roi, out_roi = [], []
    if roi:
        for p in pred_elements:
            (in_roi if center_in(center(p["bbox"]), roi) else out_roi).append(p)
    else:
        in_roi = list(pred_elements)
    pairs, used_g, used_p = greedy_match(gts, in_roi)

    n_gt = len(gts)
    n_slot = sum(1 for g in gts if g.get("match") == "slot")
    n_bbox = n_gt - n_slot
    hit_slot = sum(1 for gi, _, _ in pairs if gts[gi].get("match") == "slot")
    hit_bbox = len(pairs) - hit_slot

    errs = []
    eng_hit = {"uia": 0, "ocr": 0, "visual": 0, "other": 0}
    for gi, pi, _ in pairs:
        g, p = gts[gi], in_roi[pi]
        eng_hit[p["source"] if p["source"] in eng_hit else "other"] += 1
        if g.get("match") != "slot":
            gx, gy = center(g["bbox"])
            px, py = center(p["bbox"])
            errs.append(((gx - px) ** 2 + (gy - py) ** 2) ** 0.5)

    fp = [in_roi[pi] for pi in range(len(in_roi)) if pi not in used_p]
    return {
        "n_gt": n_gt, "n_gt_bbox": n_bbox, "n_gt_slot": n_slot,
        "n_pred_roi": len(in_roi), "n_pred_out_roi": len(out_roi),
        "hit_bbox": hit_bbox, "hit_slot": hit_slot,
        "recall_bbox": hit_bbox / n_bbox if n_bbox else None,
        "recall_slot": hit_slot / n_slot if n_slot else None,
        "precision": len(pairs) / len(in_roi) if in_roi else None,
        "fpi": float(len(fp)),
        "loc_mean": statistics.mean(errs) if errs else None,
        "loc_median": statistics.median(errs) if errs else None,
        "engine_hits": eng_hit,
        "fp_samples": [{"text": p["text"][:24], "type": p["type"],
                        "bbox": p["bbox"], "source": p["source"]} for p in fp[:20]],
        "n_fp": len(fp),
    }


def to_screen_gt(gt, offset):
    """GT bbox 转屏幕坐标（window 模式加 offset；screen 模式原样）。"""
    els = []
    for g in gt["elements"]:
        b = list(g["bbox"])
        if gt["mode"] == "window":
            b = [b[0] + offset[0], b[1] + offset[1], b[2] + offset[0], b[3] + offset[1]]
        els.append({**g, "bbox": b})
    return els


def run_scenario(key, rounds, out_dir, do_setup=True):
    sc = sd.SCENARIOS[key]
    gt_path = os.path.join(ROOT, "bench", "scenarios", f"{key}_gt.json")
    with open(gt_path, encoding="utf-8") as f:
        gt = json.load(f)
    rec = {"scenario": key, "name": gt["name"], "mode": gt["mode"],
           "n_gt": len(gt["elements"]), "scans": [], "skipped": None}

    if do_setup:
        setup = sd.setup_scenario(key)
        rec["setup"] = setup
        if not setup.get("ok"):
            rec["skipped"] = f"搭台失败: {setup.get('error')}"
            return rec
    if sc.get("optional"):
        tgt = sd.resolve_target(key)
        if not tgt.get("ok"):
            rec["skipped"] = f"目标不存在（可选场景跳过）: {tgt.get('error')}"
            return rec

    # 预热扫（不计分）：热模型 + Edge a11y 懒激活 + 计算器等首扫抖动
    if gt["mode"] == "window":
        if sc.get("find") == "hwnd_class":
            tgt = sd.resolve_target(key)
            if not tgt.get("ok"):
                rec["skipped"] = f"目标丢失: {tgt.get('error')}"
                return rec
            pipeline.scan(hwnd=tgt["hwnd"], tracker_state=None)
        else:
            try:
                pipeline.scan(win=sc["win"], tracker_state=None)
            except Exception as e:
                rec["skipped"] = f"预热扫失败: {e}"
                return rec
    else:
        pipeline.scan(win=None, tracker_state=None)

    for i in range(1, rounds + 1):
        t0 = time.perf_counter()
        if gt["mode"] == "window":
            if sc.get("find") == "hwnd_class":
                tgt = sd.resolve_target(key)
                if not tgt.get("ok"):
                    rec["skipped"] = f"第{i}扫前目标丢失: {tgt.get('error')}"
                    break
                res = pipeline.scan(hwnd=tgt["hwnd"], tracker_state=None)
            else:
                res = pipeline.scan(win=sc["win"], tracker_state=None)
        else:
            res = pipeline.scan(win=None, tracker_state=None)
        wall_ms = int((time.perf_counter() - t0) * 1000)
        meta, els = res["meta"], res["elements"]
        pred = [e.to_dict() for e in els]
        gts_screen = to_screen_gt(gt, meta["offset"])
        s = score_scan(gts_screen, pred, gt.get("roi"))
        s["scan_i"] = i
        s["duration_ms"] = meta["duration_ms"]
        s["wall_ms"] = wall_ms
        s["counts"] = meta["counts"]
        s["offset"] = meta["offset"]
        rec["scans"].append(s)
        with open(os.path.join(out_dir, f"{key}_scan{i}.json"), "w", encoding="utf-8") as f:
            json.dump({"meta": meta, "elements": pred}, f, ensure_ascii=False, indent=1)
        annotate.annotate(res["img"], els, os.path.join(out_dir, f"{key}_annotated{i}.png"),
                          offset=meta["offset"])
        time.sleep(0.4)
    return rec


def aggregate(rec):
    """多扫聚合：均值 + P95。"""
    scans = rec.get("scans") or []
    if not scans:
        return None

    def vals(k):
        return [s[k] for s in scans if s.get(k) is not None]

    def p95(xs):
        xs = sorted(xs)
        if not xs:
            return None
        return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]

    durs = vals("duration_ms")
    agg = {
        "rounds": len(scans),
        "recall_bbox_mean": statistics.mean(vals("recall_bbox")) if vals("recall_bbox") else None,
        "recall_slot_mean": statistics.mean(vals("recall_slot")) if vals("recall_slot") else None,
        "precision_mean": statistics.mean(vals("precision")) if vals("precision") else None,
        "fpi_mean": statistics.mean(vals("fpi")),
        "loc_mean": statistics.mean(vals("loc_mean")) if vals("loc_mean") else None,
        "loc_median_of_medians": statistics.median(vals("loc_median")) if vals("loc_median") else None,
        "duration_mean_ms": statistics.mean(durs) if durs else None,
        "duration_p95_ms": p95(durs),
        "engine_hits_mean": {},
        "counts_mean": {},
    }
    for src in ("uia", "ocr", "visual", "other"):
        xs = [s["engine_hits"].get(src, 0) for s in scans]
        agg["engine_hits_mean"][src] = statistics.mean(xs)
    for k in ("uia", "ocr", "visual", "total"):
        xs = [s["counts"].get(k, 0) for s in scans]
        agg["counts_mean"][k] = round(statistics.mean(xs), 1)
    return agg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--scenarios", default=",".join(SCENARIO_ORDER))
    ap.add_argument("--tag", default=time.strftime("run_%Y%m%d_%H%M%S"))
    ap.add_argument("--no-setup", action="store_true")
    ap.add_argument("--no-teardown", action="store_true")
    args = ap.parse_args()

    out_dir = os.path.join(ROOT, "bench", "results", args.tag)
    os.makedirs(out_dir, exist_ok=True)
    keys = [k for k in args.scenarios.split(",") if k]

    # 收台机制：bench 中途崩（Ctrl+C/异常）时 atexit 兜底清本进程账本靶窗
    try:
        import atexit
        from sp import cleanup as _cleanup
        atexit.register(_cleanup.session_exit_cleanup)
    except Exception:
        pass

    print(f"=== ScreenPilot Bench · {args.tag} · rounds={args.rounds} ===")
    results = []
    for key in keys:
        print(f"\n--- [{key}] ---")
        try:
            rec = run_scenario(key, args.rounds, out_dir, do_setup=not args.no_setup)
        except Exception as e:
            rec = {"scenario": key, "skipped": f"异常: {e}", "scans": []}
        rec["agg"] = aggregate(rec)
        results.append(rec)
        if rec.get("skipped"):
            print("  SKIP:", rec["skipped"])
        elif rec["agg"]:
            a = rec["agg"]
            rb = f"{a['recall_bbox_mean']*100:.1f}%" if a["recall_bbox_mean"] is not None else "-"
            rs = f"{a['recall_slot_mean']*100:.1f}%" if a["recall_slot_mean"] is not None else "-"
            pr = f"{a['precision_mean']*100:.1f}%" if a["precision_mean"] is not None else "-"
            lm = f"{a['loc_mean']:.1f}px" if a["loc_mean"] is not None else "-"
            print(f"  recall(bbox)={rb} recall(slot)={rs} precision={pr} "
                  f"FPI={a['fpi_mean']:.1f} 误差均值={lm} "
                  f"耗时均值={a['duration_mean_ms']:.0f}ms P95={a['duration_p95_ms']}ms")
        # 单场景收尾（ldplayer 无收尾=保持原状）
        if not args.no_teardown:
            td = sd.teardown_scenario(key)
            print("  teardown:", json.dumps(td, ensure_ascii=False))

    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump({"tag": args.tag, "rounds": args.rounds,
                   "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "host": "Win11 宿主 2560x1440 纯CPU",
                   "results": results}, f, ensure_ascii=False, indent=1)
    print(f"\n结果已落盘: {out_dir}\\results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
