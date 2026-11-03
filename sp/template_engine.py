# -*- coding: utf-8 -*-
"""模板匹配引擎（形状识别族·模板通道）：OpenCV matchTemplate 治固定图标漏识。

ScreenPilot。基准报告 W-04/W-07/W-10 实锤：窗口三键、后退/刷新、
标签页 × 一类「UIA 不暴露 + OCR 无文本」的固定图标整片漏识，X 专项只认
交叉结构救不了普通图标。本通道取 MAA（MaaAssistantArknights）匹配器分层
思想中的「模板层」——模板/特征/哈希按场景分层，固定像素结构的图标正是
模板匹配的主场（借思想不搬代码）。

模板库 `sp/shape_templates/`（manifest.json + *.png），支持增量添加：
  add_template(名, BGR裁块, …) → 落盘并热生效（manifest mtime 变化自动重载）
同名可多模板共存（暗/亮主题各一副模样，任一命中即认）。

匹配要点（防误报第一，宁可少认不可乱认）：
  · 全库共用一次灰度化；TM_CCOEFF_NORMED 零均值归一化互相关，对线性亮度
    变化免疫（明暗整体偏移不炸），但结构性差异靠多模板各管各的主题；
  · 每模板独立阈值（MAA templ_thres 思想）：默认 0.93——真实命中自匹配
    1.0、背景巧合实测 ≤0.94，高阈值压误报（explorer 导航横线假冒最小化
    0.838、edge 信息圆圈假冒刷新 0.822、游戏内方框假冒最大化 0.933 全挡）；
  · 多尺度 0.8~1.2 抗 DPI 差异——尺度 1.0 先行，命中即跳过其余尺度；未命中
    才补跑 0.8/0.9/1.1/1.2（阈值放宽 0.07：缩放匹配分数天然走低，防漏）；
  · 两段式提速（1560p 全库裸跑实测 2~2.5s 太重）：1/2 降采样粗筛（阈值放宽
    0.10）出峰点 → 峰点邻域原尺度精匹配出真分数——耗时降约 4 倍且 conf 不失真；
  · 全局 NMS 去重：多尺度/多模板对同一处的重复命中按分数留一。
产出 Element：type 按清单（默认 button）、text=语义模板名、
source="visual"、conf=精匹配分数（封顶 0.95）——天然高于融合保护线 0.5，
不被无名大容器吞并（sp/fuse.py 二十棒闸门）；已被 UIA/OCR 认出的目标
（如 Edge 返回键）融合层自然吞并，不产生重复元素。
"""
import os
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

from .model import Element

# 全图精匹配并行池：OpenCV matchTemplate 释放 GIL，行切分块真并行
# （2560p 全图单模板单尺度 ~55ms，4 块并行 ~15ms；16 模板多尺度的大头）
_匹配池 = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tpl-match")

# 模板库目录：本文件同级的 shape_templates/
库目录 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shape_templates")

# 尺度档：1.0 优先（同机同 DPI 直接命中）；其余仅在 1.0 未命中时补跑
_主尺度 = 1.0
_备选尺度 = (0.8, 0.9, 1.1, 1.2)
_备选阈值放宽 = 0.07   # 缩放匹配分数天然走低，备选尺度阈值放宽保召回

_默认阈值 = 0.93
_小模板边 = 20         # min(宽,高) ≤ 此值=小模板：粗筛失真（缩半后仅 7~10px），
                       # 直接全图精匹配（小模板 matchTemplate 本身就快）
_粗筛降采样 = 2        # 大模板主尺度粗筛降采样倍数（1/2 面积，提速 ~4x）
_粗筛放宽 = 0.10       # 粗筛阈值放宽量（分数以精匹配为准，粗筛宁滥勿缺）
_备选阈值放宽 = 0.04   # 备选尺度只在主尺度无命中时跑，放宽 0.04（实测同机
                       # 背景巧合在缩放尺度 0.87~0.90 成片，再宽即误报洪水）
_备选粗筛放宽 = 0.12   # 备选尺度粗筛放宽更大：缩放+降采样双重掉分，宁滥勿缺
_备选大模板边 = 24     # 备选尺度仅对大模板跑——小图标缩放后 7~10px 无区分度
_conf上限 = 0.95

# 模块级缓存：manifest 内容 + 模板灰度图（含各尺度），mtime 变化即重载
_manifest缓存 = {"mtime": None, "条目": []}
_图缓存 = {}  # 文件名 -> (mtime, {scale: 灰度ndarray})


def _读manifest():
    """读模板清单（带 mtime 缓存）；库目录不存在返回空表。"""
    路径 = os.path.join(库目录, "manifest.json")
    try:
        mtime = os.path.getmtime(路径)
    except OSError:
        _manifest缓存["mtime"] = None
        _manifest缓存["条目"] = []
        return []
    if _manifest缓存["mtime"] == mtime:
        return _manifest缓存["条目"]
    import json
    try:
        with open(路径, encoding="utf-8") as f:
            数据 = json.load(f)
        条目 = 数据.get("templates", [])
    except Exception:
        条目 = []
    _manifest缓存["mtime"] = mtime
    _manifest缓存["条目"] = 条目
    return 条目


def _模板灰度(文件名):
    """按文件名取模板灰度图与各尺度版本（mtime 缓存；支持 3/4 通道 png）。"""
    路径 = os.path.join(库目录, 文件名)
    try:
        mtime = os.path.getmtime(路径)
    except OSError:
        return {}
    命中 = _图缓存.get(文件名)
    if 命中 and 命中[0] == mtime:
        return 命中[1]
    img = cv2.imdecode(np.fromfile(路径, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return {}
    灰 = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    尺度表 = {}
    for s in (_主尺度,) + _备选尺度:
        if abs(s - 1.0) < 1e-6:
            t = 灰
        else:
            w, h = max(4, int(round(灰.shape[1] * s))), max(4, int(round(灰.shape[0] * s)))
            t = cv2.resize(灰, (w, h), interpolation=cv2.INTER_AREA)
        尺度表[s] = t
    _图缓存[文件名] = (mtime, 尺度表)
    return 尺度表


def _峰值收集(响应图, 阈值, 邻域=3):
    """响应图上的局部极大峰点（邻域最大=自身）→ [(score, x, y)]。

    先 minMaxLoc 快筛：落榜模板（峰低于阈值）一次 O(n) 即短路，省掉
    dilate+where 的全套开销（2560p 响应图 ~15ms/次 × 每模板多尺度）。
    局部极大过滤省掉密集达标点的后置 NMS 压力。
    """
    if 响应图.size == 0:
        return []
    _mn, mx, _mnl, _mxl = cv2.minMaxLoc(响应图)
    if mx < 阈值:
        return []
    核 = np.ones((邻域, 邻域), np.uint8)
    峰掩码 = (响应图 >= 阈值) & (响应图 >= cv2.dilate(响应图, 核))
    ys, xs = np.where(峰掩码)
    return [(float(响应图[y, x]), int(x), int(y)) for y, x in zip(ys, xs)]


def _邻域精匹配(灰度, t灰, cx, cy, 阈值):
    """在粗筛峰点 (cx,cy)（原图坐标）邻域内做原尺度精匹配，返回 (score, l, t) 或 None。"""
    th, tw = t灰.shape[:2]
    l, t = max(0, cx - 8), max(0, cy - 8)
    r, b = min(灰度.shape[1], cx + tw + 8), min(灰度.shape[0], cy + th + 8)
    roi = 灰度[t:b, l:r]
    if roi.shape[0] < th or roi.shape[1] < tw:
        return None
    响应 = cv2.matchTemplate(roi, t灰, cv2.TM_CCOEFF_NORMED)
    _, mx, _, ml = cv2.minMaxLoc(响应)
    if mx < 阈值:
        return None
    return (float(mx), l + ml[0], t + ml[1])


def _nms(命中, 间距系数=0.6):
    """全局 NMS：按 (score 降序, manifest 顺序) 留一——同位冲突时同分保留
    更早建立的模板（explorer 通用版先于 ldplayer 专版入库，「关闭」优先于
    后来在别的宿主上长得几乎一样的「标签页关闭」误占其位）。"""
    保留 = []
    for 命 in sorted(命中, key=lambda x: (-x[0], x[7] if len(x) > 7 else 0)):
        score, l, t, w, h, 名, 类型 = 命[:7]
        cx, cy = l + w / 2.0, t + h / 2.0
        重复 = False
        for r in 保留:
            rw = max(w, r[3])
            rh = max(h, r[4])
            if abs(cx - (r[1] + r[3] / 2.0)) < 间距系数 * rw and \
               abs(cy - (r[2] + r[4] / 2.0)) < 间距系数 * rh:
                重复 = True
                break
        if not 重复:
            保留.append(命)
    return 保留


def scan_templates(img_bgr, max_count=48):
    """模板匹配通道：返回语义图标元素列表。bbox 相对输入图像左上角。

    分层匹配（MAA 按场景分层思想）：
    · 主尺度 1.0：小模板全图精匹配；大模板 1/2 粗筛出峰 → 邻域精匹配；
    · 备选尺度 0.8/0.9/1.1/1.2：仅大模板、仅主尺度无命中时跑（抗 DPI/缩放），
      1/4 粗筛 + 邻域精匹配，阈值放宽 0.04；
    · 命中点全局 NMS 去重（同分保 manifest 序，防跨名误占）。
    """
    条目 = _读manifest()
    if not 条目 or img_bgr is None or img_bgr.size == 0:
        return []
    灰度 = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    H, W = 灰度.shape[:2]
    粗图 = {2: cv2.resize(灰度, (W // 2, H // 2), interpolation=cv2.INTER_AREA),
            4: cv2.resize(灰度, (W // 4, H // 4), interpolation=cv2.INTER_AREA)}

    def _全图匹配(t灰, 阈值):
        """小模板直配：全图一次精匹配（行切 4 块并行）→ [(score, l, t, tw, th)]。

        行切带重叠（重叠量=模板高），跨缝目标在相邻两块中都会完整出现，
        重复峰由 NMS 收敛为一条。单块 matchTemplate 返回全图相对坐标再平移。
        """
        th, tw = t灰.shape[:2]
        if th >= H or tw >= W:
            return []
        块数 = max(1, min(4, H // max(4, th * 3)))
        if 块数 == 1:
            响应 = cv2.matchTemplate(灰度, t灰, cv2.TM_CCOEFF_NORMED)
            return [(s, x, y, tw, th) for s, x, y in _峰值收集(响应, 阈值)]
        步 = (H - th) // 块数

        def _块(y0):
            y1 = min(H, y0 + (H - th) // 块数 + th + 1)
            响应 = cv2.matchTemplate(灰度[y0:y1], t灰, cv2.TM_CCOEFF_NORMED)
            return [(s, x, y + y0, tw, th) for s, x, y in _峰值收集(响应, 阈值)]

        收 = []
        for r in _匹配池.map(_块, [i * 步 for i in range(块数)]):
            收 += r
        return 收

    def _粗筛匹配(t灰, 真阈值, 缩, 放宽):
        """大模板两段式：粗筛出峰（阈值放宽）→ 邻域精匹配（真阈值把关）。"""
        th, tw = t灰.shape[:2]
        ct = cv2.resize(t灰, (max(3, tw // 缩), max(3, th // 缩)),
                        interpolation=cv2.INTER_AREA)
        图 = 粗图[缩]
        if ct.shape[0] >= 图.shape[0] or ct.shape[1] >= 图.shape[1]:
            return []
        响应 = cv2.matchTemplate(图, ct, cv2.TM_CCOEFF_NORMED)
        out = []
        for _s, cx, cy in _峰值收集(响应, 真阈值 - 放宽):
            r = _邻域精匹配(灰度, t灰, cx * 缩, cy * 缩, 真阈值)
            if r is not None:
                out.append((r[0], r[1], r[2], tw, th))
        return out

    命中 = []
    名已中 = set()  # 同名任一变体已命中 → 其余变体跳过备选尺度（同族形状
                    # 几乎同像，主尺度精匹配自会拒掉；备选 4 尺度纯耗时段）
    for 序, 条 in enumerate(条目):
        名 = 条.get("name") or ""
        文件 = 条.get("file") or ""
        阈值 = float(条.get("threshold", _默认阈值))
        类型 = 条.get("type", "button")
        尺度表 = _模板灰度(文件)
        if not 尺度表:
            continue
        t主 = 尺度表[_主尺度]
        小 = min(t主.shape[:2]) <= _小模板边

        收齐 = (_全图匹配(t主, 阈值) if 小
                else _粗筛匹配(t主, 阈值, _粗筛降采样, _粗筛放宽))
        if not 收齐 and 名 not in 名已中:
            # 原尺度无望：补跑备选尺度（DPI/缩放差异场景）。一律走 1/2 粗筛
            # （全图直配 4 尺度实测 1.9s 太重；粗筛放宽 0.12 保峰，真分数
            # 仍由邻域精匹配把关）
            for 尺度 in _备选尺度:
                t灰 = 尺度表.get(尺度)
                if t灰 is None:
                    continue
                th, tw = t灰.shape[:2]
                if th >= H or tw >= W or min(th, tw) < 4:
                    continue
                收齐 += _粗筛匹配(t灰, 阈值 - _备选阈值放宽,
                                  _粗筛降采样, _备选粗筛放宽)
        for s, x, y, tw, th in 收齐:
            命中.append((min(s, _conf上限), x, y, tw, th, 名, 类型, 序))
        if 收齐:
            名已中.add(名)

    元素 = []
    for score, x, y, tw, th, 名, 类型, _序 in _nms(命中)[:max_count]:
        元素.append(Element(
            type=类型,
            text=名,
            bbox=[int(x), int(y), int(x + tw), int(y + th)],
            source="visual",
            conf=round(score, 3),
            clickable=True,
        ))
    return 元素


def add_template(名, img_bgr, threshold=_默认阈值, 类型="button", 说明="", tag=""):
    """增量添加模板：裁块落盘 + 更新 manifest，立即热生效。

    名：语义名（会成为元素的 text，如「后退」「关闭」）；
    tag：变体标记（主题/宿主来源，如 explorer/ldplayer/light），同名多模板共存。
    返回落盘文件名。
    """
    import json
    if img_bgr is None or img_bgr.size == 0:
        raise ValueError("模板裁块为空")
    os.makedirs(库目录, exist_ok=True)
    文件 = "%s%s.png" % (名, ("_" + tag) if tag else "")
    # 中文/特殊路径安全写盘（cv2.imencode 不吃 unicode 路径的场景）
    cv2.imencode(".png", img_bgr)[1].tofile(os.path.join(库目录, 文件))

    条目 = [t for t in _读manifest() if not (t.get("name") == 名 and t.get("file") == 文件)]
    条目.append({"name": 名, "file": 文件, "threshold": float(threshold),
                 "type": 类型, "note": 说明})
    _manifest缓存["mtime"] = None  # 强制下轮重载
    with open(os.path.join(库目录, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "templates": 条目}, f, ensure_ascii=False, indent=1)
    return 文件


def list_templates():
    """当前模板清单（诊断用）。"""
    return list(_读manifest())
