# -*- coding: utf-8 -*-
"""书库向量层（2026-11-02 F3 施工·B3「书库接 SigLIP 向量」）：以图查库。

B3 定义（盘点清单/一条线 F3）：find 只能按文字查，纯图标/无文字元素
（游戏自绘 UI、专业软件工具栏）查不到；接上 SigLIP 向量才通「以图查库」
——图标裁片 → 向量 → book_find 命中书库条目。

开关（沿 BOOKLIB_INDEX 环境变量灰度先例，方向按资源尺子反置）：
- BOOKVEC=1 显式开启：SigLIP ONNX（211MB，E 盘模型库）惰性加载、入库
  算向量、无文字 icon 元素（visual conf≥0.3）入库、book_find 支持以图查库；
- 缺省/其他值=关：零行为变化——模型不加载、条目正本零改动、文字检索
  路径不经过本模块（零回归由 _test_bookvec 守）。

向量是派生数据：100% 可从裁片重算 → 落 manual/_index/ 派生层（沿
bookindex 哲学「派生坏了回填即愈」），entries.json 正本零改动——条目
不写 768 维向量（存量 6000+ 条 ×15KB 会让正本膨胀拖慢读域，违背速度
尺子；盘点页「vector 字段位已留」考古实核为未落码，本棒按派生层架构
落位，条目正本不动）。

行键一致性：向量行键=(domain,name,type,bbox) 与 booklib._条目键 同源；
查询命中后按键回读域账本正本取全条（bookindex 同款宁缺勿错兜底），
正本已被清扫/归档的向量行自然失活；回填() 全量重建清死行与同键重复行。

样本防注入纪律同书库：向量层只做编码与余弦，不解释任何文本内容。
"""
import json
import os
import threading

import numpy as np

from . import booklib

# 模型配置与 bench/semantic_matcher.py _MODEL_TABLE["siglip"] 同源（现役
# SigLIP1 int8；SigLIP2 判定门不达标禁用，见 bench 档案 results/
# ssv2_r3_siglip2_dev/）。sp/ 不 import bench——配置复制，改动须两处同步。
_MODELS_ROOT = (os.environ.get("SCREENPILOT_MODELS_DIR")
                or os.path.expanduser("~/screenpilot_models"))
SIGLIP_DIR = os.path.join(_MODELS_ROOT, "siglip-base-patch16-224-onnx")
_模型表 = {
    "path": os.path.join(SIGLIP_DIR, "onnx", "model_quantized.onnx"),
    "tok": os.path.join(SIGLIP_DIR, "tokenizer.json"),
    "mean": np.array([0.5, 0.5, 0.5], np.float32),
    "std": np.array([0.5, 0.5, 0.5], np.float32),
    "resize_interp": "cubic",   # 落档 preprocessor_config.json resample=3=BICUBIC
}
_批大小 = 32          # 视觉编码批大小（bench 同款）
_会话超时线程数 = 12

索引根 = os.path.join(booklib.库根, "_index")
向量文件 = os.path.join(索引根, "vectors.npz")
元数据文件 = os.path.join(索引根, "vectors_meta.json")

# 无文字 icon 入库门槛：visual icon 候选现役 conf=0.3（visual_engine 产出
# 口径），BOOKVEC 开时放行入库——有文字条目仍守 0.5 原线零变化。
_icon最低置信 = 0.3

_锁 = threading.Lock()
_会话 = None


def 开启():
    """BOOKVEC=1 才开（缺省关=零行为变化）。"""
    return os.environ.get("BOOKVEC") == "1"


def _取会话():
    """SigLIP ONNX 会话惰性单例（双检锁；模型缺失抛错由调用方兜）。"""
    global _会话
    if _会话 is None:
        with _锁:
            if _会话 is None:
                import onnxruntime as ort
                so = ort.SessionOptions()
                so.intra_op_num_threads = _会话超时线程数
                so.inter_op_num_threads = 1
                so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                _会话 = ort.InferenceSession(
                    _模型表["path"], so, providers=["CPUExecutionProvider"])
    return _会话


def _裁片预处理(img_bgr, bbox):
    """bbox 裁片 → 224×224 直接拉伸 → 归一 → CHW float32（SigLIP 官方口径，
    与 bench/semantic_matcher._crop_prep siglip 分支同构）。"""
    import cv2
    h, w = img_bgr.shape[:2]
    l, t = max(0, int(bbox[0])), max(0, int(bbox[1]))
    r, b = min(w, int(bbox[2])), min(h, int(bbox[3]))
    if r - l < 3 or b - t < 3:      # 退化裁片：给最小可用块，交给相似度淘汰
        r, b = min(w, l + 3), min(h, t + 3)
    crop = img_bgr[t:b, l:r]
    canvas = cv2.resize(crop, (224, 224), interpolation=cv2.INTER_CUBIC)
    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    rgb = (rgb - _模型表["mean"]) / _模型表["std"]
    return rgb.transpose(2, 0, 1)


def _l2(x):
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return x / n


def 编码裁片图(img_bgr, bboxes):
    """同图多 bbox 裁片批量编码 → [n,768] L2 归一。"""
    crops = [_裁片预处理(img_bgr, b) for b in bboxes]
    sess = _取会话()
    dummy = np.zeros((1, 64), np.int64)   # SigLIP1 图支路仍吃 text 输入占位
    vecs = []
    with _锁:
        for i in range(0, len(crops), _批大小):
            px = np.stack(crops[i:i + _批大小]).astype(np.float32)
            emb = sess.run(["image_embeds"], {"pixel_values": px,
                                              "input_ids": dummy})[0]
            vecs.append(emb)
    return _l2(np.concatenate(vecs))


def 编码图片(图源):
    """单图编码 → [768] L2 归一。图源=BGR ndarray 或图片文件路径（盲查用例
    主通道：crop_xy/crop_screen 的裁片 png 直接喂）。中文路径安全走
    capture.load_image。"""
    if isinstance(图源, str):
        from . import capture
        img = capture.load_image(图源)
        if img is None:
            raise FileNotFoundError("图片读不到：%s" % 图源)
    else:
        img = 图源
    h, w = img.shape[:2]
    return 编码裁片图(img, [[0, 0, w, h]])[0]  # 全图为一个「裁片」


# ── 向量库（派生层，manual/_index/vectors.npz + vectors_meta.json）────────

def _载库():
    """读向量库。返回 (矩阵[n,768] or None, 行键列表)。坏档返回 (None, [])。"""
    if not (os.path.isfile(向量文件) and os.path.isfile(元数据文件)):
        return None, []
    try:
        z = np.load(向量文件)
        矩阵 = z["vecs"]
        with open(元数据文件, "r", encoding="utf-8") as f:
            键s = json.load(f)
        if 矩阵.ndim != 2 or len(键s) != 矩阵.shape[0]:
            return None, []
        return 矩阵, 键s
    except Exception:
        return None, []


def _存库(矩阵, 键s):
    """原子写向量库（临时文件 + 替换，防写一半断电烂档）。savez 传文件句柄
    ——传路径会被自动补 .npz 后缀，os.replace 就找不到源文件了。"""
    os.makedirs(索引根, exist_ok=True)
    tmp_v, tmp_m = 向量文件 + ".tmp", 元数据文件 + ".tmp"
    with open(tmp_v, "wb") as f:
        np.savez_compressed(f, vecs=矩阵.astype(np.float32))
    with open(tmp_m, "w", encoding="utf-8") as f:
        json.dump(键s, f, ensure_ascii=False)
    os.replace(tmp_v, 向量文件)
    os.replace(tmp_m, 元数据文件)


def 追加向量(域名, 记录s):
    """入库侧接线：本轮写入条目的向量追加进库。记录s=[(键, 向量), ...]，
    键=(name, type, bbox 绝对坐标元组) 与 booklib._条目键 同源。"""
    if not 记录s:
        return 0
    矩阵, 键s = _载库()
    新v = np.stack([v for _k, v in 记录s])
    新k = [[域名, k[0], str(k[1]), list(k[2])] for k, _v in 记录s]
    if 矩阵 is not None:
        矩阵 = np.concatenate([矩阵, 新v])
        键s = 键s + 新k
    else:
        矩阵, 键s = 新v, 新k
    _存库(矩阵, 键s)
    return len(记录s)


def 库况():
    """向量库概况（booksweep --embed 摘要用）。"""
    矩阵, 键s = _载库()
    if 矩阵 is None:
        return {"exists": False, "rows": 0}
    域 = {}
    for k in 键s:
        域[k[0]] = 域.get(k[0], 0) + 1
    return {"exists": True, "rows": 矩阵.shape[0], "dim": 矩阵.shape[1],
            "domains": 域}


def 回填():
    """全量重建向量库（booksweep --embed）：遍历全库条目，从裁片 png 重编码。

    无裁片条目（BOOKVEC 开时入库的无文字 icon，crop=None）与裁片丢失条目
    跳过并计数（其向量等下次重扫入库时刷新）。幂等可重跑；返回摘要。
    """
    矩阵s, 键s = [], []
    跳过 = 0
    域数 = 0
    if os.path.isdir(booklib.库根):
        for 域名 in booklib._选域(None, booklib.库根):
            数据 = booklib._读域(域名)
            if not 数据["entries"]:
                continue
            域数 += 1
            for e in 数据["entries"]:
                if not isinstance(e, dict):
                    continue
                裁片 = e.get("crop")
                路径 = os.path.join(booklib._域路径(域名), 裁片) if 裁片 else None
                if not 裁片 or not os.path.isfile(路径 or ""):
                    跳过 += 1
                    continue
                try:
                    v = 编码图片(路径)
                except Exception:
                    跳过 += 1
                    continue
                键 = (e.get("name") or "", e.get("type") or "",
                      tuple(e.get("bbox") or ()))
                矩阵s.append(v)
                键s.append([域名, 键[0], 键[1], list(键[2])])
    if 矩阵s:
        _存库(np.stack(矩阵s), 键s)
    else:
        _存库(np.zeros((0, 768), np.float32), [])
    return {"domains": 域数, "rows": len(矩阵s), "skipped_no_crop": 跳过}


def 以图查库(图源, domain=None, 类型=None, limit=10):
    """以图查库（B3 主通道）：查询图编码 ↔ 向量库余弦 → top-K 条目。

    行键回读正本取全条（键=(name,type,bbox) 同键多行取最大 cos——同键重扫
    幂等更新产生多向量行，正本只有一条）；正本已被清扫/归档的行自然失活。
    返回条目列表（复用 booklib._换算条目：bbox/stale/pos_hint 语义不变，
    另附 cos 与 vec_domain 字段）。向量库为空/模型缺失抛错，调用方如实报。
    """
    qv = 编码图片(图源)
    矩阵, 键s = _载库()
    if 矩阵 is None or 矩阵.shape[0] == 0:
        raise RuntimeError("向量库为空——先 BOOKVEC=1 扫描入库或 booksweep --embed 回填")
    # domain/type 过滤在向量行上先做（省全库余弦也可，量小直接全算再滤——
    # 6000×768 矩阵乘毫秒级，先算后滤更简单）
    cos = 矩阵 @ qv
    序 = np.argsort(-cos)
    命中 = {}
    for i in 序:
        k = 键s[int(i)]
        域名, 名字, 类型s = k[0], str(k[1] or ""), str(k[2] or "")
        bbox = tuple(k[3] or [])
        if domain and domain.lower() not in str(域名).lower():
            continue
        if 类型 and 类型s != 类型:
            continue
        键 = (名字, 类型s, bbox)
        # 同键多行取最大 cos（首次出现即该键最高分，argsort 降序保证）
        if 键 in 命中:
            continue
        命中[键] = (域名, float(cos[int(i)]))
        if len(命中) >= limit * 3:   # 回读正本有失活折损，候选池放宽 3 倍
            break
    结果 = []
    for 键, (域名, 分) in 命中.items():
        全条 = None
        for e in booklib._读域(域名)["entries"]:
            if not isinstance(e, dict):
                continue
            if ((e.get("name") or "", e.get("type") or "",
                 tuple(e.get("bbox") or ())) == 键):
                全条 = e
                break
        if 全条 is None:
            continue   # 向量行悬空（正本已清扫/归档）：宁缺勿错
        d = booklib._换算条目(全条, 域名)
        d["cos"] = round(分, 4)
        d["vec_query"] = "image"
        结果.append(d)
        if len(结果) >= limit:
            break
    return 结果
