# -*- coding: utf-8 -*-
"""语义匹配器（ScreenPilot·施工单 F）：CLIP/SigLIP int8 ONNX 纯 CPU。

定位（架构铁律）：本模块只装在**考卷侧**（bench/），给统考匹配器补语义理解——
识屏 sp/ 核心零改动，识屏本体保持零模型纯三引擎。产品侧「图标语义命名回写
元素表」是第二阶段，不在本单。

模型（走 hf-mirror 下载，落 SCREENPILOT_MODELS_DIR，缺省 ~/screenpilot_models/）：
- clip   = Xenova/clip-vit-base-patch32 int8（150MB，512 维）
- siglip = Xenova/siglip-base-patch16-224 int8（211MB，768 维）
开发集对照实验（bench/results/ssv2_r2_dev/）定型后 MODEL_KIND 冻结。

匹配逻辑（与首考启发式匹配器并联，启发式优先——任务书口径「高分直接用、
低分回落语义」）：
  1. 启发式 match_query 先跑（规则首考已冻结，零改动）；
  2. 启发式强证据（总分 >= 短路阈值）→ 直接采用，不跑语义（省时，且保护
     text 类强证据区不被语义倒扣——首考该区命中率 64% 高于语义水位）；
  3. 弱证据/弃权 → 语义通道：query 编码 ↔ 全部候选元素裁图编码，L2 归一后
     余弦相似度 top1；top1 余弦 >= 采纳阈值 → 采用语义结果；否则回落启发式
     结果（宁缺勿滥交归因）。

裁图策略（开发集 5 策略对照后冻结）：**裸裁图 + 原句 query**——联合伴生文字框、
深色反相、prompt ensemble 实测全部无益甚至有害（icon 目标旁的无关文字混入
联合框反而干扰，见 crop_strategy_experiment.json）。等比缩放 + 白底 pad
成方 → bicubic 224（SigLIP 按官方口径直接 resize 拉伸）→ 各自标准归一。

参数定型纪律（与首考 D 组同构）：{短路阈值, 采纳阈值, 模型, 裁图策略} 只在
开发集（固定 seed 抽样 60 题，题号落 dev_qids.json）上定型，一次冻结，全量
考试零改动；定型数据全部落 bench/results/ssv2_r2_dev/ 留档。

性能：同图视觉特征缓存（SS-v2 同图多题共享，scan_image 确定性输出）；
视觉编码 batch 推理；query 文本特征进程内缓存；单裁图 ~18-25ms（16 核 CPU）。
"""
import os
import threading

import numpy as np

# 模型落位：HF 重资产不进 repo；环境变量 SCREENPILOT_MODELS_DIR 优先，
# 缺省 ~/screenpilot_models（下载脚本见 README「语义匹配模型」节）。
_MODELS_ROOT = (os.environ.get("SCREENPILOT_MODELS_DIR")
                or os.path.expanduser("~/screenpilot_models"))
CLIP_DIR = os.path.join(_MODELS_ROOT, "clip-vit-base-patch32-onnx")
SIGLIP_DIR = os.path.join(_MODELS_ROOT, "siglip-base-patch16-224-onnx")
SIGLIP2_DIR = os.path.join(_MODELS_ROOT, "siglip2-base-patch16-224-onnx")

_MODEL_TABLE = {
    "clip": {
        "path": os.path.join(CLIP_DIR, "onnx", "model_quantized.onnx"),
        "tok": os.path.join(CLIP_DIR, "tokenizer.json"),
        "ctx": 77, "bos": 49406, "eos": 49407, "pad": 0,
        "mean": np.array([0.48145466, 0.4578275, 0.40821073], np.float32),
        "std": np.array([0.26862954, 0.26130258, 0.27577711], np.float32),
        "pad_square": True,
    },
    "siglip": {
        "path": os.path.join(SIGLIP_DIR, "onnx", "model_quantized.onnx"),
        "tok": os.path.join(SIGLIP_DIR, "tokenizer.json"),
        "ctx": 64, "bos": None, "eos": 1, "pad": 1,
        "mean": np.array([0.5, 0.5, 0.5], np.float32),
        "std": np.array([0.5, 0.5, 0.5], np.float32),
        "pad_square": False,        # SigLIP 官方=直接 resize 拉伸，无 center crop
        "resize_interp": "cubic",   # 落档 preprocessor_config.json resample=3=PIL BICUBIC
    },
    # 施工单「模型替换」2026-10-25：SigLIP2 int8 ONNX 顶替 SigLIP1 打分位（替换不并列）。
    # onnx-community/siglip2-base-patch16-224-ONNX int8（378MB，hf-mirror 下载落档）。
    # 节点实测（sess.get_inputs/get_outputs）：输入 input_ids+pixel_values（无
    # attention_mask），输出 text_embeds/image_embeds 各 768 维——与 SigLIP1 完全同构。
    # token 实测：<bos>=2 <eos>=1 <pad>=0（Gemma 风格）；预处理实测（preprocessor_config
    # .json）：mean/std=0.5、224×224 直接拉伸、resample=2=PIL BILINEAR（与 SigLIP1 的
    # BICUBIC 不同，插值已参数化——施工时以落档配置为准，未照抄调研推算值）。
    "siglip2": {
        "path": os.path.join(SIGLIP2_DIR, "onnx", "model_int8.onnx"),
        "tok": os.path.join(SIGLIP2_DIR, "tokenizer.json"),
        "ctx": 64, "bos": 2, "eos": 1, "pad": 0,
        "mean": np.array([0.5, 0.5, 0.5], np.float32),
        "std": np.array([0.5, 0.5, 0.5], np.float32),
        "pad_square": False,         # 官方=直接 resize 拉伸，无 center crop
        "resize_interp": "linear",   # 落档 preprocessor_config.json resample=2=PIL BILINEAR
    },
}

# ---- 匹配策略参数（2026-10-25 开发集定型后冻结，定型记录见 results/ssv2_r2_dev/）----
# 定型证据三件：crop_strategy_experiment.json（裁图策略）/ model_compare.json
# （CLIP vs SigLIP + 阈值曲线）/ 首考 predictions total×hit 交叉分析（短路阈值）。
MODEL_KIND = "siglip"  # 施工单「模型替换」2026-10-25 判定门结论：SigLIP2 不达标，不冻结切换，
                       # 回滚 SigLIP1（零样本 icon top1 35% vs SigLIP2 17.5%，-17.5pp；
                       # 总 48.33% vs 25%，text 75% vs 40%；档案 results/ssv2_r3_siglip2_dev/）。
                       # siglip2 条目保留仅作档案与后续微调基座候选，默认禁用——其采纳线
                       # 也未定型（余弦分布 0.07~0.17 各桶精度 0.14~0.29 无可分区间）。
SHORT_CIRCUIT = 4.0    # 启发式总分 >= 此值直接采用。定型依据（首考 1272 题交叉分析）：
                       # total>=4 区命中率 64%（text 强证据区，须保护不被语义倒扣）；
                       # total<4 的 icon 介入池 373 题首考仅命中 8 题（误抢重灾区，
                       # 语义 35% 水位远超之）；4.5 时 text 介入池首考命中 72% 超语义水位。
SEM_ADOPT = 0.07       # SigLIP 尺度采纳线。定型依据（开发集 60 题 top_cos 分布
                       # 0.019~0.182 中位 0.096）：0.05~0.13 区间采纳精度统计不可分
                       # （0.47~0.57），取中值 0.07；低于此回落启发式（保原对）。
VIS_BATCH = 32         # 视觉编码批大小


def _l2(x):
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return x / n


def _crop_prep(img_bgr, bbox, cfg):
    """裸裁图 → 缩放/pad → 224 → 标准归一。RGB/CHW 输出。"""
    import cv2
    h, w = img_bgr.shape[:2]
    l, t = max(0, int(bbox[0])), max(0, int(bbox[1]))
    r, b = min(w, int(bbox[2])), min(h, int(bbox[3]))
    if r - l < 3 or b - t < 3:      # 退化候选：给最小可用块，交给相似度淘汰
        r, b = min(w, l + 3), min(h, t + 3)
    crop = img_bgr[t:b, l:r]
    ch, cw = crop.shape[:2]
    if cfg["pad_square"]:            # CLIP：等比缩放 + 白底 pad 正方形
        scale = 224 / float(max(ch, cw))
        nw, nh = max(1, int(round(cw * scale))), max(1, int(round(ch * scale)))
        interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
        resized = cv2.resize(crop, (nw, nh), interpolation=interp)
        canvas = np.full((224, 224, 3), 255, np.uint8)
        ox, oy = (224 - nw) // 2, (224 - nh) // 2
        canvas[oy:oy + nh, ox:ox + nw] = resized
    else:                            # SigLIP/SigLIP2：直接 resize 拉伸（官方口径）
        interp = cv2.INTER_LINEAR if cfg.get("resize_interp") == "linear" else cv2.INTER_CUBIC
        canvas = cv2.resize(crop, (224, 224), interpolation=interp)
    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    rgb = (rgb - cfg["mean"]) / cfg["std"]
    return rgb.transpose(2, 0, 1)    # HWC → CHW


class SemanticMatcher:
    """CLIP/SigLIP ONNX 编码器 + 同图特征缓存。线程安全（推理加锁）。"""

    def __init__(self, kind=None, threads=12):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.kind = kind or MODEL_KIND
        self.cfg = _MODEL_TABLE[self.kind]
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.sess = ort.InferenceSession(self.cfg["path"], so,
                                         providers=["CPUExecutionProvider"])
        self.inames = {i.name for i in self.sess.get_inputs()}  # SigLIP 无 attention_mask
        self.tok = Tokenizer.from_file(self.cfg["tok"])
        self._img_cache = {}      # img_path -> (签名, vecs[n,dim] 已 L2 归一)
        self._txt_cache = {}      # 文本 -> vec[dim] 已 L2 归一
        self._lock = threading.Lock()
        self.vis_ms = 0.0         # 累计视觉编码耗时（报告用）
        self.vis_n = 0

    # ---------- 编码 ----------

    def _tokenize(self, text):
        cfg = self.cfg
        # 滤集含 pad：SigLIP1 pad=eos=1 本就在滤集（行为不变）；SigLIP2 tokenizer
        # 自带 post-processor 会 pad 到 64（pad=0），不滤会残留错位（Gemma 风格）。
        ids = [i for i in self.tok.encode(text).ids
               if i not in (cfg["bos"], cfg["eos"], cfg["pad"])]
        ids = ([cfg["bos"]] if cfg["bos"] else []) + ids[: cfg["ctx"] - 2] + [cfg["eos"]]
        mask = [1] * len(ids) + [0] * (cfg["ctx"] - len(ids))
        ids = ids + [cfg["pad"]] * (cfg["ctx"] - len(ids))
        return (np.array([ids], dtype=np.int64),
                np.array([mask], dtype=np.int64))

    def encode_text(self, texts):
        """批量文本编码 → [n,dim] L2 归一（进程内缓存）。"""
        out = []
        miss = [(i, t) for i, t in enumerate(texts) if t not in self._txt_cache]
        if miss:
            pairs = [self._tokenize(t) for _, t in miss]
            dummy = np.zeros((1, 3, 224, 224), np.float32)
            feed = {"input_ids": np.concatenate([p[0] for p in pairs]),
                    "pixel_values": dummy}
            if "attention_mask" in self.inames:
                feed["attention_mask"] = np.concatenate([p[1] for p in pairs])
            with self._lock:
                emb = self.sess.run(["text_embeds"], feed)[0]
            for (i, t), row in zip(miss, emb):
                self._txt_cache[t] = row
        for t in texts:
            out.append(self._txt_cache[t])
        return _l2(np.stack(out))

    def encode_crops(self, img_bgr, bboxes, cache_key=None):
        """从 BGR 原图按 bbox 列表裁图编码 → [n,dim] L2 归一。

        cache_key：图片路径。同图同候选集直接复用缓存（SS-v2 同图多题省 40%+）。
        """
        if cache_key is not None:
            签名 = tuple(tuple(b) for b in bboxes)
            hit = self._img_cache.get(cache_key)
            if hit is not None and hit[0] == 签名:
                return hit[1]
        import time
        t0 = time.perf_counter()
        crops = [_crop_prep(img_bgr, b, self.cfg) for b in bboxes]
        vecs = []
        dummy = np.zeros((1, self.cfg["ctx"]), np.int64)
        feed_extra = {"input_ids": dummy}
        if "attention_mask" in self.inames:
            feed_extra["attention_mask"] = dummy
        with self._lock:
            for i in range(0, len(crops), VIS_BATCH):
                px = np.stack(crops[i:i + VIS_BATCH]).astype(np.float32)
                emb = self.sess.run(["image_embeds"],
                                    {"pixel_values": px, **feed_extra})[0]
                vecs.append(emb)
        vecs = _l2(np.concatenate(vecs))
        self.vis_ms += (time.perf_counter() - t0) * 1000
        self.vis_n += len(bboxes)
        if cache_key is not None:
            self._img_cache[cache_key] = (签名, vecs)
        return vecs


# ---------- 与启发式匹配器的并联裁决 ----------

def match_query_semantic(query, 元素s, img_bgr, matcher, 启发式元素, 启发式证据,
                         img_key=None):
    """启发式优先 + 语义回落裁决。返回 (选中元素 or None, 证据 dict)。

    - 启发式强证据（total >= SHORT_CIRCUIT）：直接采用，语义跳过（省时+保 text）；
    - 弱证据/弃权：query 编码 ↔ 全候选裁图编码余弦，top1 >= SEM_ADOPT 采纳，
      否则回落启发式结果。
    """
    证据 = dict(启发式证据) if isinstance(启发式证据, dict) else {}
    总分 = float(证据.get("total", 0.0)) if "total" in 证据 else 0.0
    if 启发式元素 is not None and 总分 >= SHORT_CIRCUIT:
        证据["semantic"] = "skipped_strong_heuristic"
        return 启发式元素, 证据
    if not 元素s:
        证据["semantic"] = "no_elements"
        return 启发式元素, 证据

    qvec = matcher.encode_text([query])[0]           # 原句编码（开发集定型：ens3 无益）
    vecs = matcher.encode_crops(img_bgr, [el.bbox for el in 元素s],
                                cache_key=img_key)   # 裸裁图（开发集定型：join/反相无益）
    cos = vecs @ qvec
    top = int(np.argmax(cos))
    证据["semantic"] = {
        "model": matcher.kind,
        "top_cos": round(float(cos[top]), 4),
        "adopt_thresh": SEM_ADOPT,
        "short_circuit": SHORT_CIRCUIT,
    }
    if cos[top] >= SEM_ADOPT:
        证据["semantic"]["adopted"] = True
        return 元素s[top], 证据
    证据["semantic"]["adopted"] = False   # 低置信：回落启发式（可能为弃权 None）
    return 启发式元素, 证据
