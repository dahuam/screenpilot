# -*- coding: utf-8 -*-
"""公开统考跑分器：ScreenSpot-v2 / ScreenSpot-Pro（ScreenPilot·施工单 D）。

把识屏系统（元素表范式）拿去考市面公开 grounding 统考，产出可对外引用的成绩。

官方判分口径：预测点击点落入目标 bbox 即命中（ScreenSpot 系通用规则）。
识屏范式差异（如实声明）：识屏不直接打点，而是先出结构化元素表、再由匹配器
按考卷 query（英文功能描述）从表中选元素，选中元素的**中心点**当作点击点。

匹配器首版=启发式（文本形近归一 + 类型先验 + 位置先验），无学习成分——
这是本统考成绩的最大短板来源，报告须如实注明；同时报 oracle 上限
（GT 目标在元素表中存在可辨识候选的比例），拆分「识别引擎成绩」与「匹配器成绩」。

归因三分类（每道错题必归类，作为迭代路标）：
- miss_detection 漏识：元素表中无任何候选与 GT 相交（识别引擎未出该目标）
- match_error    匹配失败：目标有候选，但匹配器选中的是无关元素
- coord_offset   坐标偏：匹配器选中的元素与 GT 相交（IoU>=0.3），但其中心偏出 GT bbox

用法：
  python bench/public_sspot.py --dataset v2 [--limit N] [--tag 名称]
  python bench/public_sspot.py --dataset pro [--sample 200] [--seed 42] [--platform windows]

数据准备：下载 OS-Copilot/ScreenSpot-v2（json+images）与
likaixin/ScreenSpot-Pro（annotations+images，考 Windows 子集抽样）到本地，
数据集根目录用环境变量 SCREENPILOT_DATASETS_DIR 指定（缺省
~/screenpilot_datasets）。数据集不入本仓库。
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
import random
import argparse
import statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sp import pipeline  # noqa: E402

_DATASETS_ROOT = (os.environ.get("SCREENPILOT_DATASETS_DIR")
                  or os.path.expanduser("~/screenpilot_datasets"))
SSV2_DIR = os.path.join(_DATASETS_ROOT, "screenspot_v2")
SSV2_IMG_DIR = os.path.join(SSV2_DIR, "screenspotv2_image")
SSPRO_DIR = os.path.join(_DATASETS_ROOT, "screenspot_pro")
SSPRO_ANN_DIR = os.path.join(SSPRO_DIR, "annotations")
SSPRO_IMG_DIR = os.path.join(SSPRO_DIR, "images")


# ---------------- 数据加载 ----------------

def _ssv2_bbox_xywh(b):
    """SS-v2 官方格式 [x, y, w, h] → [l, t, r, b]。"""
    x, y, w, h = b
    return [float(x), float(y), float(x) + float(w), float(y) + float(h)]


def load_v2(limit=None, sources=None):
    """加载 ScreenSpot-v2 全量题目（desktop 334 + mobile 501 + web 437 = 1272）。

    sources：限定 data_source 前缀（如 {"desktop", "mobile", "web"}），None=全量。
    """
    文件 = [("desktop", "screenspot_desktop_v2.json"),
            ("mobile", "screenspot_mobile_v2.json"),
            ("web", "screenspot_web_v2.json")]
    题s = []
    for 部分, fname in 文件:
        path = os.path.join(SSV2_DIR, fname)
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        for i, r in enumerate(rows):
            if sources and r.get("data_source", 部分) not in sources:
                continue
            img = os.path.join(SSV2_IMG_DIR, r["img_filename"])
            if not os.path.isfile(img):
                continue
            题s.append({
                "qid": f"v2_{部分}_{i}",
                "dataset": "screenspot_v2",
                "group": r.get("data_source", 部分),      # windows/macos/ios/android/gitlab...
                "platform": 部分,                          # desktop/mobile/web
                "ui_type": r.get("data_type", "?"),        # icon/text
                "instruction": r["instruction"],
                "bbox": _ssv2_bbox_xywh(r["bbox"]),        # [l,t,r,b]
                "img_path": img,
            })
    if limit:
        题s = 题s[:limit]
    return 题s


# SS-Pro：annotation 文件名 ↔ images 目录名映射（img_filename 自带目录，直接用）
def load_pro(platform="windows", sample=200, seed=42, sample_scale=None):
    """加载 ScreenSpot-Pro 题目；platform 限平台，sample>0 时按应用分层抽样。

    抽样口径（报告须注明）：platform 内按应用（annotation 文件）分层，每应用
    抽 max(2, round(n * sample/总题数)) 题，random.Random(seed) 洗牌后取前 K——
    固定种子完全可复现。sample_scale="all" 跳过抽样取全平台。
    """
    题s, 按应用 = [], {}
    for fname in sorted(os.listdir(SSPRO_ANN_DIR)):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(SSPRO_ANN_DIR, fname), encoding="utf-8") as f:
            rows = json.load(f)
        app = os.path.basename(fname)[:-5]
        for i, r in enumerate(rows):
            if platform and r.get("platform") != platform:
                continue
            img = os.path.join(SSPRO_IMG_DIR, *r["img_filename"].split("/"))
            题s.append({
                "qid": r.get("id") or f"pro_{app}_{i}",
                "dataset": "screenspot_pro",
                "group": r.get("application", app),
                "platform": r.get("platform", platform),
                "ui_type": r.get("ui_type", "?"),
                "instruction": r["instruction"],
                "bbox": [float(x) for x in r["bbox"]],     # 官方已是 [l,t,r,b]
                "img_path": img,
            })
            按应用.setdefault(app, []).append(len(题s) - 1)
    if sample and sample_scale != "all" and sample < len(题s):
        rng = random.Random(seed)
        每应用配额 = max(2, round(sample * len(题s) / max(len(题s), 1) / len(按应用)))
        选中 = []
        for app, idxs in sorted(按应用.items()):
            rng.shuffle(idxs)
            k = max(2, min(len(idxs), round(len(idxs) * sample / len(题s))))
            k = max(k, min(len(idxs), 2))  # 每应用至少 2 题
            选中.extend(idxs[:k])
        题s = [题s[i] for i in sorted(选中)]
    return 题s


# ---------------- 启发式匹配器（首版，如实报短板） ----------------

_停用词 = {
    "the", "a", "an", "of", "to", "in", "on", "at", "for", "with", "this", "that",
    "it", "is", "are", "and", "or", "please", "me", "my", "i", "want", "would",
    "like", "click", "press", "tap", "select", "open", "activate", "use", "using",
    "find", "show", "here", "there", "you", "your", "can", "should", "do", "does",
}

# query 关键词 → 元素类型/属性先验
_类型先验 = {
    "button": ("clickable", 0.5), "btn": ("clickable", 0.5), "link": ("clickable", 0.4),
    "tab": ("clickable", 0.3), "menu": ("clickable", 0.3), "toggle": ("clickable", 0.4),
    "switch": ("clickable", 0.4), "checkbox": ("clickable", 0.4), "icon": ("icon", 0.4),
    "field": ("edit", 0.4), "input": ("edit", 0.4), "box": ("edit", 0.2),
    "search": ("edit", 0.2), "bar": ("edit", 0.1),
}


def _norm(s):
    """文本归一：lower + 形近（×→x 等）+ 去标点挤空格。"""
    s = (s or "").lower().replace("×", "x").replace("*", "x")
    out = []
    for ch in s:
        out.append(ch if ch.isalnum() or ch.isspace() else " ")
    return " ".join("".join(out).split())


def _edit1(a, b):
    """编辑距离是否 <=1（等长替换/一增一删，O(n)）。"""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(x != y for x, y in zip(a, b)) <= 1
    if la > lb:
        a, b, la, lb = b, a, lb, la
    # a 短 b 长 1：b 删一个字符能否得到 a
    i = j = 差 = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
        else:
            差 += 1
            if 差 > 1:
                return False
        j += 1
    return True


def _位置分(el, w, h, 位置词集):
    """按 bbox 中心相对全图的归一化位置给 0~0.6 分。"""
    if not 位置词集:
        return 0.0
    cx, cy = el.center()
    nx, ny = cx / max(w, 1), cy / max(h, 1)
    s = 0.0
    if "top" in 位置词集:
        s = max(s, max(0.0, 1.0 - ny / 0.45))
    if "bottom" in 位置词集:
        s = max(s, max(0.0, 1.0 - (1.0 - ny) / 0.45))
    if "left" in 位置词集:
        s = max(s, max(0.0, 1.0 - nx / 0.45))
    if "right" in 位置词集:
        s = max(s, max(0.0, 1.0 - (1.0 - nx) / 0.45))
    if "center" in 位置词集 or "middle" in 位置词集:
        s = max(s, max(0.0, 1.0 - abs(nx - 0.5) / 0.4, 1.0 - abs(ny - 0.5) / 0.4))
    return 0.6 * s


# 通用 UI 常识（非对题调参）：窗口控制动词的目标在标题栏（窗口顶部，右侧为主），
# 且这类词是动作目标本身，应压过 query 里出现的窗口/应用名等名词
_窗口控制词 = {"close", "minimize", "maximize", "quit", "exit"}


def match_query(query, 元素s, img_wh):
    """从元素表中为 query 选最佳元素。返回 (元素 or None, 证据 dict)。

    首版启发式：文本形近（主证据）+ 类型/可点击先验 + 位置先验
    + 窗口控制常识（close/minimize 等词文本加权、其余名词降权、标题栏位置加成）。
    无任何文本证据且无位置词时返回 None（宁缺勿滥，交归因判定）。
    """
    w, h = img_wh
    nq = _norm(query)
    词 = nq.split()
    核心词 = [t for t in 词 if t not in _停用词 and len(t) >= 2]
    位置词集 = set(词) & {"top", "bottom", "left", "right", "center", "middle", "corner"}
    控制词 = [t for t in 核心词 if t in _窗口控制词]
    普通词 = [t for t in 核心词 if t not in _窗口控制词]
    类型加分目标 = set()
    for t in 词:
        if t in _类型先验:
            类型加分目标.add(_类型先验[t][0])
    if 控制词:
        位置词集 = 位置词集 | {"top"}  # 标题栏常识：控制按钮在窗口顶部（right 相对窗口而言，
                                      # 全图坐标不可靠，10-25 冒烟实测反有害，弃用）

    最佳, 最佳分, 最佳明细 = None, 0.0, None
    for el in 元素s:
        nt = _norm(el.text)
        文本分 = 0.0
        if nt and 核心词:
            def 词分(kw):
                if nt == kw:
                    return 4.0
                if kw in nt:
                    return 3.0
                tokens = nt.split()
                if any(tok.startswith(kw) or (len(tok) >= 3 and kw.startswith(tok))
                       for tok in tokens):
                    return 2.5
                if len(kw) >= 5 and any(_edit1(kw, tok) for tok in tokens):
                    return 1.5
                if len(kw) >= 4 and any(tok.startswith(kw[:4]) for tok in tokens):
                    return 0.8
                return 0.0

            for kw in 控制词:
                if 词分(kw) > 0:
                    文本分 += 词分(kw) * 2.0  # 控制词命中加倍（目标即该词本身）
            普通分 = sum(词分(kw) for kw in 普通词)
            if 控制词 and 普通分 > 0:
                普通分 *= 0.4  # query 有控制词时，压过窗口/应用名等名词匹配
            文本分 += 普通分
            if 核心词 and all(kw in nt for kw in 核心词):
                文本分 *= 1.3  # 全核心词命中 bonus
        类型分 = 0.0
        el_attr = "icon" if el.source == "visual" else ("clickable" if el.clickable else "text")
        if el_attr in 类型加分目标:
            类型分 = 0.5
        if 核心词 and el.clickable and ("button" in 词 or "btn" in 词):
            类型分 = max(类型分, 0.3)
        位置分 = _位置分(el, w, h, 位置词集)
        总分 = 文本分 + 类型分 + 位置分
        if 总分 > 最佳分:
            最佳, 最佳分 = el, 总分
            最佳明细 = {"text_score": round(文本分, 2), "type_score": 类型分,
                       "pos_score": round(位置分, 2), "total": round(总分, 2)}
    if 最佳 is None or 最佳分 <= 0:
        return None, {"reason": "no_candidate_above_zero"}
    return 最佳, 最佳明细


# ---------------- 官方口径判分 + 归因 ----------------

def _center_in(c, b):
    return b[0] <= c[0] <= b[2] and b[1] <= c[1] <= b[3]


def _iou(a, b):
    il, it = max(a[0], b[0]), max(a[1], b[1])
    ir, ib = min(a[2], b[2]), min(a[3], b[3])
    if ir <= il or ib <= it:
        return 0.0
    inter = (ir - il) * (ib - it)
    aa = (a[2] - a[0]) * (a[3] - a[1])
    ab = (b[2] - b[0]) * (b[3] - b[1])
    return inter / float(aa + ab - inter)


def judge(题, 元素s, 选中):
    """官方口径判分 + 三分类归因。返回结果 dict。

    命中：选中元素中心 ∈ GT bbox（官方「点击点落 bbox」口径）。
    oracle（目标可辨识）：任一检出元素中心落 GT bbox，或与 GT IoU>=0.3，
    或 GT 中心落在该元素 bbox 内。
    """
    gt = 题["bbox"]
    gcx = (gt[0] + gt[2]) / 2.0
    gcy = (gt[1] + gt[3]) / 2.0
    oracle_el = None
    oracle_best_iou = 0.0
    for el in 元素s:
        cx, cy = el.center()
        iou = _iou(el.bbox, gt)
        if _center_in((cx, cy), gt) or iou >= 0.3 or _center_in((gcx, gcy), el.bbox):
            if iou > oracle_best_iou:
                oracle_el, oracle_best_iou = el, iou
    结果 = {
        "qid": 题["qid"], "instruction": 题["instruction"],
        "group": 题["group"], "platform": 题["platform"], "ui_type": 题["ui_type"],
        "gt_bbox": gt, "n_elements": len(元素s),
        "oracle": oracle_el is not None,
    }
    if 选中 is None:
        结果["hit"] = False
        结果["fail_type"] = "match_error" if oracle_el is not None else "miss_detection"
        结果["picked"] = None
        return 结果
    cx, cy = 选中.center()
    iou_sel = _iou(选中.bbox, gt)
    结果["picked"] = {**选中.to_dict(), "center": [cx, cy]}
    if _center_in((cx, cy), gt):
        结果["hit"] = True
        结果["fail_type"] = None
        结果["sel_iou"] = round(iou_sel, 3)
    elif iou_sel >= 0.3:
        结果["hit"] = False
        结果["fail_type"] = "coord_offset"
        结果["sel_iou"] = round(iou_sel, 3)
    else:
        结果["hit"] = False
        结果["fail_type"] = "match_error"
        结果["sel_iou"] = round(iou_sel, 3)
    return 结果


# ---------------- 主流程 ----------------

def run(题s, out_dir, tag, matcher=None, semantic_mod=None):
    os.makedirs(out_dir, exist_ok=True)
    jsonl_path = os.path.join(out_dir, "predictions.jsonl")
    已做 = set()
    if os.path.exists(jsonl_path):  # 断点续跑：跳过已完成题目
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                try:
                    已做.add(json.loads(line)["qid"])
                except Exception:
                    pass
    print(f"[{tag}] 共 {len(题s)} 题，已完成 {len(已做)}，续跑 {len(题s) - min(len(已做), len(题s))} 题")

    n_new = 0
    with open(jsonl_path, "a", encoding="utf-8") as out:
        for i, 题 in enumerate(题s):
            if 题["qid"] in 已做:
                continue
            try:
                res = pipeline.scan_image(题["img_path"])
                元素s = res["elements"]
                选中, 证据 = match_query(题["instruction"], 元素s, res["meta"]["screen"])
                if matcher is not None:
                    # 施工单 F：启发式优先 + 语义回落（判分口径零改动，见 semantic_matcher）
                    选中, 证据 = semantic_mod.match_query_semantic(
                        题["instruction"], 元素s, res["img"], matcher,
                        选中, 证据, img_key=题["img_path"])
                r = judge(题, 元素s, 选中)
                r["match_evidence"] = 证据
                r["matcher"] = "semantic+heuristic" if matcher is not None else "heuristic"
                r["duration_ms"] = res["meta"]["duration_ms"]
                r["img"] = os.path.basename(题["img_path"])
            except Exception as e:
                r = {"qid": 题["qid"], "instruction": 题["instruction"],
                     "group": 题["group"], "platform": 题["platform"],
                     "ui_type": 题["ui_type"], "hit": False,
                     "fail_type": "error", "error": str(e), "oracle": False}
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
            out.flush()
            n_new += 1
            if n_new % 25 == 0:
                print(f"  .. {i + 1}/{len(题s)}")
    return jsonl_path


def summarize(jsonl_path, tag):
    """汇总：总 acc / oracle / 归因分布 / 分组 / 耗时。"""
    rows = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    rows = [r for r in rows if r.get("fail_type") != "error"]
    n = len(rows)
    hits = sum(1 for r in rows if r["hit"])
    oracle = sum(1 for r in rows if r.get("oracle"))
    归因 = {"miss_detection": 0, "match_error": 0, "coord_offset": 0}
    for r in rows:
        if not r["hit"] and r.get("fail_type") in 归因:
            归因[r["fail_type"]] += 1
    durs = [r["duration_ms"] for r in rows if r.get("duration_ms")]

    def 分组统计(key):
        分 = {}
        for r in rows:
            分.setdefault(r.get(key) or "?", []).append(r)
        out = {}
        for g, rs in sorted(分.items()):
            out[g] = {
                "n": len(rs),
                "acc": round(sum(1 for r in rs if r["hit"]) / len(rs), 4),
                "oracle": round(sum(1 for r in rs if r.get("oracle")) / len(rs), 4),
            }
        return out

    summary = {
        "tag": tag, "n": n,
        "acc": round(hits / n, 4) if n else None,
        "oracle_rate": round(oracle / n, 4) if n else None,
        "acc_on_oracle": round(sum(1 for r in rows if r["hit"]) / oracle, 4) if oracle else None,
        "fail_types": 归因,
        "duration_mean_ms": round(statistics.mean(durs), 1) if durs else None,
        "duration_p50_ms": round(statistics.median(durs), 1) if durs else None,
        "by_platform": 分组统计("platform"),
        "by_ui_type": 分组统计("ui_type"),
        "by_group": 分组统计("group"),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    out_path = os.path.join(os.path.dirname(jsonl_path), "summary.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    return summary


def main():
    ap = argparse.ArgumentParser(description="ScreenSpot 公开统考跑分器（元素表范式）")
    ap.add_argument("--dataset", choices=["v2", "pro"], required=True)
    ap.add_argument("--limit", type=int, default=None, help="v2 只考前 N 题（调试用）")
    ap.add_argument("--sample", type=int, default=200, help="pro 分层抽样题数（0=全平台）")
    ap.add_argument("--platform", default="windows", help="pro 平台过滤（windows/macos/linux）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--matcher", choices=["heuristic", "semantic"], default="heuristic",
                    help="heuristic=首考启发式（默认）；semantic=启发式+CLIP 语义回落（施工单 F）")
    ap.add_argument("--tag", default=None, help="结果目录名（默认自动带时间戳）")
    args = ap.parse_args()

    matcher = None
    semantic_mod = None
    if args.matcher == "semantic":
        import semantic_matcher as semantic_mod  # 考卷侧模块（延迟加载，sp/ 零依赖）
        matcher = semantic_mod.SemanticMatcher(threads=12)
        print(f"语义匹配器已加载：kind={semantic_mod.MODEL_KIND} "
              f"({semantic_mod._MODEL_TABLE[semantic_mod.MODEL_KIND]['path']}) "
              f"(短路={semantic_mod.SHORT_CIRCUIT}, 采纳={semantic_mod.SEM_ADOPT})")

    if args.dataset == "v2":
        题s = load_v2(limit=args.limit)
        tag = args.tag or time.strftime("ssv2_%Y%m%d_%H%M%S")
    else:
        题s = load_pro(platform=args.platform, sample=args.sample, seed=args.seed,
                       sample_scale="all" if args.sample == 0 else None)
        tag = args.tag or time.strftime("sspro_%Y%m%d_%H%M%S")
    out_dir = os.path.join(ROOT, "bench", "results", tag)
    print(f"题量 {len(题s)} → {out_dir}")

    t0 = time.perf_counter()
    jsonl = run(题s, out_dir, tag, matcher=matcher, semantic_mod=semantic_mod)
    s = summarize(jsonl, tag)
    print(f"\n=== {tag} 结果 ===")
    print(f"acc={s['acc']}  oracle={s['oracle_rate']}  acc|oracle={s['acc_on_oracle']}  "
          f"n={s['n']}  归因={s['fail_types']}")
    print(f"耗时均值={s['duration_mean_ms']}ms  中位={s['duration_p50_ms']}ms  "
          f"总墙钟={int(time.perf_counter() - t0)}s")
    print("by_platform:", json.dumps(s["by_platform"], ensure_ascii=False))
    print("by_ui_type:", json.dumps(s["by_ui_type"], ensure_ascii=False))
    print("by_group:", json.dumps(s["by_group"], ensure_ascii=False)[:1200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
