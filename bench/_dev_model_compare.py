# -*- coding: utf-8 -*-
"""开发集实验：编码模型对照 CLIP vs SigLIP（施工单 F 定型证据）。

口径：开发集 60 题（icon40+text20，dev_qids.json 同题）+ 赢家裁图策略
（bare 裁图 + 原句 prompt，见 crop_strategy_experiment.json 结论）。
两模型各按**官方预处理**编码：CLIP=等比 pad 白底 224 + CLIP 归一；
SigLIP=直接 resize 224×224（拉伸）+ 0.5/0.5 归一。
落盘每题 top1 cos 与是否命中 → model_compare.json（采纳阈值定型数据）。
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
OUT_DIR = os.path.join(ROOT, "bench", "results", "ssv2_r2_dev")

MODELS = {
    "clip": {
        "path": os.path.join(_MODELS_ROOT, "clip-vit-base-patch32-onnx", "onnx", "model_quantized.onnx"),
        "tok": os.path.join(_MODELS_ROOT, "clip-vit-base-patch32-onnx", "tokenizer.json"),
        "ctx": 77, "bos": 49406, "eos": 49407, "pad": 0,
        "mean": np.array([0.48145466, 0.4578275, 0.40821073], np.float32),
        "std": np.array([0.26862954, 0.26130258, 0.27577711], np.float32),
        "pad_square": True,
    },
    "siglip": {
        "path": os.path.join(_MODELS_ROOT, "siglip-base-patch16-224-onnx", "onnx", "model_quantized.onnx"),
        "tok": os.path.join(_MODELS_ROOT, "siglip-base-patch16-224-onnx", "tokenizer.json"),
        "ctx": 64, "bos": None, "eos": 1, "pad": 1,
        "mean": np.array([0.5, 0.5, 0.5], np.float32),
        "std": np.array([0.5, 0.5, 0.5], np.float32),
        "pad_square": False,   # SigLIP 官方=直接 resize 拉伸，无 center crop
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
    inames = {i.name for i in sess.get_inputs()}   # SigLIP 无 attention_mask（内置 causal）
    _sess_cache[kind] = (sess, tok, cfg, inames)
    return _sess_cache[kind]


def encode_text(sess, tok, cfg, inames, text):
    ids = [i for i in tok.encode(text).ids if i not in (cfg["bos"], cfg["eos"])]
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
    ch, cw = crop.shape[:2]
    if cfg["pad_square"]:                       # CLIP：等比 + 白底 pad 正方形
        scale = 224 / float(max(ch, cw))
        nw, nh = max(1, int(round(cw * scale))), max(1, int(round(ch * scale)))
        interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
        resized = cv2.resize(crop, (nw, nh), interpolation=interp)
        canvas = np.full((224, 224, 3), 255, np.uint8)
        ox, oy = (224 - nw) // 2, (224 - nh) // 2
        canvas[oy:oy + nh, ox:ox + nw] = resized
    else:                                        # SigLIP：直接 resize 拉伸
        canvas = cv2.resize(crop, (224, 224), interpolation=cv2.INTER_CUBIC)
    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    rgb = (rgb - cfg["mean"]) / cfg["std"]
    return rgb.transpose(2, 0, 1)


def main():
    dev_qids = set(json.load(open(os.path.join(OUT_DIR, "dev_qids.json"))))
    题s = [t for t in load_v2() if t["qid"] in dev_qids]
    print(f"开发集 {len(题s)} 题")

    记录 = {k: [] for k in MODELS}
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
            qv = _l2(encode_text(sess, tok, cfg, inames, 题["instruction"]).reshape(1, -1))[0]
            cos = vis @ qv
            top = int(np.argmax(cos))
            el = 元素s[top]
            hit = gt[0] <= el.center()[0] <= gt[2] and gt[1] <= el.center()[1] <= gt[3]
            记录[kind].append({"qid": 题["qid"], "ui_type": 题["ui_type"],
                              "top_cos": round(float(cos[top]), 4), "top1_hit": bool(hit)})
        if (k + 1) % 10 == 0:
            print(f"  .. {k+1}/{len(题s)}  {int(time.perf_counter()-t0)}s")

    汇总 = {}
    for kind, rows in 记录.items():
        icon = [r for r in rows if r["ui_type"] == "icon"]
        text = [r for r in rows if r["ui_type"] == "text"]
        汇总[kind] = {
            "n": len(rows),
            "acc": round(sum(r["top1_hit"] for r in rows) / max(len(rows), 1), 4),
            "icon_acc": round(sum(r["top1_hit"] for r in icon) / max(len(icon), 1), 4),
            "text_acc": round(sum(r["top1_hit"] for r in text) / max(len(text), 1), 4),
        }
        曲线 = {}
        for th in [0.15, 0.20, 0.22, 0.25, 0.28, 0.30, 0.35]:
            采 = [r for r in rows if r["top_cos"] >= th]
            曲线[str(th)] = {
                "采纳率": round(len(采) / max(len(rows), 1), 3),
                "采纳精度": round(sum(r["top1_hit"] for r in 采) / max(len(采), 1), 3),
            }
        汇总[kind]["adopt_curve"] = 曲线

    print(json.dumps(汇总, ensure_ascii=False, indent=1))
    json.dump({"summary": 汇总, "per_q": 记录},
              open(os.path.join(OUT_DIR, "model_compare.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("落档: model_compare.json, 总耗时", int(time.perf_counter() - t0), "s")


def _l2(x):
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return x / n


if __name__ == "__main__":
    main()
