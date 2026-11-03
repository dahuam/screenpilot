# -*- coding: utf-8 -*-
"""开发集实验：裁图策略对照（施工单 F 定型证据，ScreenPilot）。

目的：为语义匹配器选定裁图/prompt 策略（一次定型后冻结，全量考试零改动）。
开发集：SS-v2 icon 类抽 40 题 + text 类抽 20 题（random seed=7，取 oracle=True 池——
oracle=False 语义通道无能为力，不进对照），题号落 dev_qids.json 留档。

策略维度（组合对照）：
  crop=bare    裸图标裁图（现状基线）
  crop=join    图标∪伴生文字联合裁图（伴生几何=识屏融合层标签伴生闸同款口径：
               右/左侧 10~250px 垂直带内，或下方 10~100px 水平带内，取并集框）
  invert=True  裁图边缘均值亮度<80 时 uint8 域反相（深色主题通用预处理）
  prompt=single query 原句单模板
  prompt=ens3  三模板 ensemble（CLIP 通用提示工程，模板均值向量后再归一）

评价：语义通道 top1 命中率（选中元素中心落 GT bbox，官方口径）——即假设
短路/采纳阈值放行时的判分命中。
"""
import sys, os, json, time, random

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bench"))

import numpy as np
import cv2
from sp import pipeline
from public_sspot import load_v2, _norm, _停用词
from semantic_matcher import SemanticMatcher, _l2, _NORM_MEAN, _NORM_STD

OUT_DIR = os.path.join(ROOT, "bench", "results", "ssv2_r2_dev")
os.makedirs(OUT_DIR, exist_ok=True)


def prompts_for(instruction, mode):
    if mode == "single":
        return [instruction]
    core = " ".join(w for w in _norm(instruction).split()
                    if w not in _停用词 and len(w) >= 2)
    return [instruction,
            f"a screenshot of the {core} button in a user interface",
            f"an icon of {core}"]


def 联合框(bbox, ocr_boxes, w, h):
    """图标∪伴生文字联合框（几何口径与 sp/pipeline 标签伴生闸同款、窗加宽到 250/100）。"""
    l, t, r, b = bbox
    jl, jt, jr, jb = l, t, r, b
    for ol, ot, orr, ob in ocr_boxes:
        ox, oy = (ol + orr) / 2, (ot + ob) / 2
        if r + 10 <= ox <= r + 250 and t - 40 <= oy <= b + 40:       # 右侧伴生
            jr, jt, jb = max(jr, orr), min(jt, ot), max(jb, ob)
        elif l - 250 <= ox <= l - 10 and t - 40 <= oy <= b + 40:     # 左侧伴生
            jl, jt, jb = min(jl, ol), min(jt, ot), max(jb, ob)
        elif b + 10 <= oy <= b + 100 and l - 40 <= ox <= r + 40:     # 下方伴生
            jb, jl, jr = max(jb, ob), min(jl, ol), max(jr, orr)
    jl, jt = max(0, int(jl)), max(0, int(jt))
    jr, jb = min(w, int(jr)), min(h, int(jb))
    return jl, jt, jr, jb


def 裁图(img, el, crop_mode, invert, ocr_boxes):
    h, w = img.shape[:2]
    l, t, r, b = el.bbox
    l, t = max(0, int(l)), max(0, int(t))
    r, b = min(w, int(r)), min(h, int(b))
    if r - l < 3 or b - t < 3:
        r, b = min(w, l + 3), min(h, t + 3)
    crop = img[t:b, l:r]
    if crop_mode == "join":
        jl, jt, jr, jb = 联合框(el.bbox, ocr_boxes, w, h)
        if jr - jl >= 5 and jb - jt >= 5:
            crop = img[jt:jb, jl:jr]
    if invert and float(crop.mean()) < 80:
        crop = 255 - crop
    # 等比缩放 + 白底 pad 正方形 + bicubic 224 + CLIP 归一（与 semantic_matcher 同款）
    ch, cw = crop.shape[:2]
    scale = 224 / float(max(ch, cw))
    nw, nh = max(1, int(round(cw * scale))), max(1, int(round(ch * scale)))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    resized = cv2.resize(crop, (nw, nh), interpolation=interp)
    canvas = np.full((224, 224, 3), 255, np.uint8)
    ox, oy = (224 - nw) // 2, (224 - nh) // 2
    canvas[oy:oy + nh, ox:ox + nw] = resized
    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    rgb = (rgb - _NORM_MEAN) / _NORM_STD
    return rgb.transpose(2, 0, 1)


def main():
    # ---- 抽开发集（固定 seed，题号留档）----
    题s = load_v2()
    首考 = {}
    p = os.path.join(ROOT, "bench", "results", "ssv2_full_r1", "predictions.jsonl")
    for line in open(p, encoding="utf-8"):
        r = json.loads(line)
        首考[r["qid"]] = r
    icon池 = [t for t in 题s if t["ui_type"] == "icon" and 首考.get(t["qid"], {}).get("oracle")]
    text池 = [t for t in 题s if t["ui_type"] == "text" and 首考.get(t["qid"], {}).get("oracle")]
    rng = random.Random(7)
    dev = rng.sample(icon池, 40) + rng.sample(text池, 20)
    json.dump([t["qid"] for t in dev],
              open(os.path.join(OUT_DIR, "dev_qids.json"), "w"))
    print(f"开发集 {len(dev)} 题（icon 40 + text 20）→ dev_qids.json")

    m = SemanticMatcher(threads=12)

    策略s = [("bare", False, "single"), ("bare", False, "ens3"),
             ("join", False, "ens3"), ("join", True, "ens3"),
             ("join", True, "single")]
    战绩 = {s: {"hit": 0, "n": 0, "icon_hit": 0, "icon_n": 0,
                "text_hit": 0, "text_n": 0} for s in 策略s}
    t0 = time.perf_counter()
    for k, 题 in enumerate(dev):
        try:
            res = pipeline.scan_image(题["img_path"])
        except Exception as e:
            print("skip", 题["qid"], e)
            continue
        元素s, img = res["elements"], res["img"]
        ocr_boxes = [el.bbox for el in 元素s if el.source == "ocr"]
        gt = 题["bbox"]
        for 策 in 策略s:
            crop_mode, invert, prompt_mode = 策
            crops = [裁图(img, el, crop_mode, invert, ocr_boxes) for el in 元素s]
            vecs = []
            with m._lock:
                for i in range(0, len(crops), 32):
                    px = np.stack(crops[i:i + 32]).astype(np.float32)
                    emb = m.sess.run(
                        ["image_embeds"],
                        {"pixel_values": px,
                         "input_ids": np.zeros((1, 77), np.int64),
                         "attention_mask": np.zeros((1, 77), np.int64)})[0]
                    vecs.append(emb)
            vis = _l2(np.concatenate(vecs))
            ptx = _l2(m.encode_text(prompts_for(题["instruction"], prompt_mode))
                      .mean(axis=0, keepdims=True))
            cos = vis @ ptx[0]
            el = 元素s[int(np.argmax(cos))]
            hit = gt[0] <= el.center()[0] <= gt[2] and gt[1] <= el.center()[1] <= gt[3]
            战绩[策]["n"] += 1
            战绩[策]["hit"] += hit
            if 题["ui_type"] == "icon":
                战绩[策]["icon_n"] += 1
                战绩[策]["icon_hit"] += hit
            else:
                战绩[策]["text_n"] += 1
                战绩[策]["text_hit"] += hit
        if (k + 1) % 10 == 0:
            print(f"  .. {k+1}/{len(dev)}  {int(time.perf_counter()-t0)}s")

    print("\n=== 开发集 top1 命中率（60 题：icon40+text20） ===")
    for 策, v in 战绩.items():
        print(f"{str(策):34s} 总 {v['hit']:2d}/{v['n']} = {v['hit']/max(v['n'],1):.2%} | "
              f"icon {v['icon_hit']:2d}/{v['icon_n']} = {v['icon_hit']/max(v['icon_n'],1):.2%} | "
              f"text {v['text_hit']}/{v['text_n']} = {v['text_hit']/max(v['text_n'],1):.2%}")
    json.dump({str(k): v for k, v in 战绩.items()},
              open(os.path.join(OUT_DIR, "crop_strategy_experiment.json"), "w",
                   encoding="utf-8"), ensure_ascii=False, indent=1)
    print("落档: crop_strategy_experiment.json")


if __name__ == "__main__":
    main()
