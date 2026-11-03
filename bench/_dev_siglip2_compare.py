# -*- coding: utf-8 -*-
"""开发集判定门：SigLIP1 基线复刻 vs SigLIP2 对照（施工单「模型替换」2026-10-25）。

口径：与 F 组定型实验 _dev_model_compare.py 完全同构——dev60（icon40+text20，
dev_qids.json 固定题集）+ 裸裁图 + 原句 query；每题 pipeline.scan_image 一次，
两模型共用元素表（任务A改后引擎=基线复刻口径，成绩为 A+B 合并账）。差异仅在：
SigLIP2 预处理按落档 preprocessor_config.json（BILINEAR 拉伸，非 SigLIP1 BICUBIC）。

产出（落 results/ssv2_r3_siglip2_dev/model_compare_r3.json）：
- summary：两模型 acc / icon_acc / text_acc + 采纳阈值曲线（SigLIP 尺度网格）；
- per_q：每题 top_cos 与 top1_hit（SEM_ADOPT 重定型原始数据）；
- top_cos 分位数（余弦分布法定型线用）。

判定门（任务书 §三.3）：SigLIP2 icon top1 较 SigLIP1 基线提升 >=+3pp 方为显著；
不达标立即停（不跑全量），交无效证明+微调路线施工单草案。
"""
import sys, os, json, time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bench"))

import numpy as np
import cv2
import onnxruntime as ort
from tokenizers import Tokenizer
from sp import pipeline
from public_sspot import load_v2

_MODELS_ROOT = (os.environ.get("SCREENPILOT_MODELS_DIR")
                or os.path.expanduser("~/screenpilot_models"))
OUT_DIR = os.path.join(ROOT, "bench", "results", "ssv2_r3_siglip2_dev")

MODELS = {
    "siglip": {   # 基线（现役 SigLIP1，条目与 semantic_matcher._MODEL_TABLE 逐字段一致）
        "path": os.path.join(_MODELS_ROOT, "siglip-base-patch16-224-onnx", "onnx", "model_quantized.onnx"),
        "tok": os.path.join(_MODELS_ROOT, "siglip-base-patch16-224-onnx", "tokenizer.json"),
        "ctx": 64, "bos": None, "eos": 1, "pad": 1,
        "mean": np.array([0.5, 0.5, 0.5], np.float32),
        "std": np.array([0.5, 0.5, 0.5], np.float32),
        "pad_square": False,
        "resize_interp": "cubic",
    },
    "siglip2": {  # 候选（SigLIP2 int8，条目与 semantic_matcher._MODEL_TABLE 逐字段一致）
        "path": os.path.join(_MODELS_ROOT, "siglip2-base-patch16-224-onnx", "onnx", "model_int8.onnx"),
        "tok": os.path.join(_MODELS_ROOT, "siglip2-base-patch16-224-onnx", "tokenizer.json"),
        "ctx": 64, "bos": 2, "eos": 1, "pad": 0,
        "mean": np.array([0.5, 0.5, 0.5], np.float32),
        "std": np.array([0.5, 0.5, 0.5], np.float32),
        "pad_square": False,
        "resize_interp": "linear",   # 落档 preprocessor_config.json resample=2=BILINEAR
    },
}

_sess_cache = {}


def get_sess(kind):
    if kind in _sess_cache:
        return _sess_cache[kind]
    cfg = MODELS[kind]
    so = ort.SessionOptions()
    so.intra_op_num_threads = 12
    so.inter_op_num_threads = 1
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    sess = ort.InferenceSession(cfg["path"], so, providers=["CPUExecutionProvider"])
    tok = Tokenizer.from_file(cfg["tok"])
    inames = {i.name for i in sess.get_inputs()}
    _sess_cache[kind] = (sess, tok, cfg, inames)
    return _sess_cache[kind]


def encode_text(sess, tok, cfg, inames, text):
    # 与 semantic_matcher._tokenize 同构（滤 bos/eos/pad 再手动构 [bos]+ids+[eos]）
    ids = [i for i in tok.encode(text).ids if i not in (cfg["bos"], cfg["eos"], cfg["pad"])]
    ids = ([cfg["bos"]] if cfg["bos"] else []) + ids[: cfg["ctx"] - 2] + [cfg["eos"]]
    mask = [1] * len(ids) + [0] * (cfg["ctx"] - len(ids))
    ids = ids + [cfg["pad"]] * (cfg["ctx"] - len(ids))
    emb = sess.run(["text_embeds"], {
        "input_ids": np.array([ids], np.int64),
        **({"attention_mask": np.array([mask], np.int64)} if "attention_mask" in inames else {}),
        "pixel_values": np.zeros((1, 3, 224, 224), np.float32),
    })[0]
    return emb[0]


def prep(img_bgr, bbox, cfg):
    h, w = img_bgr.shape[:2]
    l, t = max(0, int(bbox[0])), max(0, int(bbox[1]))
    r, b = min(w, int(bbox[2])), min(h, int(bbox[3]))
    if r - l < 3 or b - t < 3:
        r, b = min(w, l + 3), min(h, t + 3)
    crop = img_bgr[t:b, l:r]
    interp = cv2.INTER_LINEAR if cfg.get("resize_interp") == "linear" else cv2.INTER_CUBIC
    canvas = cv2.resize(crop, (224, 224), interpolation=interp)   # 两模型均直接拉伸
    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    rgb = (rgb - cfg["mean"]) / cfg["std"]
    return rgb.transpose(2, 0, 1)


def _l2(x):
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return x / n


def main():
    dev_qids = set(json.load(open(os.path.join(ROOT, "bench", "results", "ssv2_r2_dev", "dev_qids.json"))))
    题s = [t for t in load_v2() if t["qid"] in dev_qids]
    print(f"开发集 {len(题s)} 题（判定门：SigLIP1 基线复刻 vs SigLIP2）")

    记录 = {k: [] for k in MODELS}
    vis_ms = {k: 0.0 for k in MODELS}
    vis_n = {k: 0 for k in MODELS}
    t0 = time.perf_counter()
    for k, 题 in enumerate(题s):
        try:
            res = pipeline.scan_image(题["img_path"])
        except Exception as e:
            print("skip", 题["qid"], e)
            continue
        元素s, img = res["elements"], res["img"]
        gt = 题["bbox"]

        for kind in MODELS:
            sess, tok, cfg, inames = get_sess(kind)
            tc = time.perf_counter()
            crops = [prep(img, el.bbox, cfg) for el in 元素s]
            vecs = []
            dummy = np.zeros((1, cfg["ctx"]), np.int64)
            feed_extra = {"input_ids": dummy,
                          **({"attention_mask": dummy} if "attention_mask" in inames else {})}
            for i in range(0, len(crops), 32):
                px = np.stack(crops[i:i + 32]).astype(np.float32)
                emb = sess.run(["image_embeds"], {"pixel_values": px, **feed_extra})[0]
                vecs.append(emb)
            vis = _l2(np.concatenate(vecs))
            vis_ms[kind] += (time.perf_counter() - tc) * 1000
            vis_n[kind] += len(crops)
            qv = _l2(encode_text(sess, tok, cfg, inames, 题["instruction"]).reshape(1, -1))[0]
            cos = vis @ qv
            top = int(np.argmax(cos))
            el = 元素s[top]
            hit = gt[0] <= el.center()[0] <= gt[2] and gt[1] <= el.center()[1] <= gt[3]
            记录[kind].append({"qid": 题["qid"], "ui_type": 题["ui_type"],
                              "top_cos": round(float(cos[top]), 4), "top1_hit": bool(hit)})
        if (k + 1) % 10 == 0:
            print(f"  .. {k+1}/{len(题s)}  {int(time.perf_counter()-t0)}s", flush=True)

    汇总 = {}
    for kind, rows in 记录.items():
        icon = [r for r in rows if r["ui_type"] == "icon"]
        text = [r for r in rows if r["ui_type"] == "text"]
        汇总[kind] = {
            "n": len(rows),
            "acc": round(sum(r["top1_hit"] for r in rows) / max(len(rows), 1), 4),
            "icon_acc": round(sum(r["top1_hit"] for r in icon) / max(len(icon), 1), 4),
            "text_acc": round(sum(r["top1_hit"] for r in text) / max(len(text), 1), 4),
            "vis_ms_per_crop": round(vis_ms[kind] / max(vis_n[kind], 1), 1),
        }
        # 采纳阈值曲线（SigLIP 尺度网格，两模型同网格便于对照）
        曲线 = {}
        for th in [0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.12, 0.15, 0.18]:
            采 = [r for r in rows if r["top_cos"] >= th]
            曲线[str(th)] = {
                "采纳率": round(len(采) / max(len(rows), 1), 3),
                "采纳精度": round(sum(r["top1_hit"] for r in 采) / max(len(采), 1), 3),
            }
        汇总[kind]["adopt_curve"] = 曲线
        # top_cos 分位数（余弦分布法定型 SEM_ADOPT）
        tcs = sorted(r["top_cos"] for r in rows)
        tcs_icon = sorted(r["top_cos"] for r in rows if r["ui_type"] == "icon")
        汇总[kind]["top_cos_q"] = {
            "all_p10_p50_p90": [round(np.percentile(tcs, p), 4) for p in (10, 50, 90)],
            "icon_p10_p50_p90": [round(np.percentile(tcs_icon, p), 4) for p in (10, 50, 90)],
        }
        # 分桶精度（0.01 步进，看可分区间——与二轮 §9.4 方法同构）
        桶 = {}
        for lo in [i / 100 for i in range(2, 20)]:
            hi = lo + 0.01
            bs = [r for r in rows if lo <= r["top_cos"] < hi]
            if bs:
                桶[f"{lo:.2f}~{hi:.2f}"] = {"n": len(bs), "精度": round(sum(r["top1_hit"] for r in bs) / len(bs), 3)}
        汇总[kind]["cos_bucket"] = 桶

    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk not in ("cos_bucket",)}
                      for k, v in 汇总.items()}, ensure_ascii=False, indent=1))
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump({"summary": 汇总, "per_q": 记录},
              open(os.path.join(OUT_DIR, "model_compare_r3.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("落档: ssv2_r3_siglip2_dev/model_compare_r3.json, 总耗时", int(time.perf_counter() - t0), "s")


if __name__ == "__main__":
    main()
