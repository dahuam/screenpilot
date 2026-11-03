# -*- coding: utf-8 -*-
"""三源融合层：UIA / OCR / 视觉候选去重合并 + 统一编号排序。

规则：
- 中心点互含或 IoU>0.3 判定为同一元素，优先级 uia > ocr > visual；
- UIA 元素无名而 OCR 命中时，把 OCR 文本并入其 text；
- visual icon 被 UIA/OCR 元素覆盖（中心落入或 IoU>0.3）时吞并丢弃；
- 最终按「从上到下、从左到右」（行容差分行）输出从 1 开始的统一编号。
"""
from .model import Element


def _iou(a, b):
    """计算两个 bbox [l,t,r,b] 的交并比。"""
    al, at, ar, ab = a
    bl, bt, br, bb = b
    il = max(al, bl)
    it = max(at, bt)
    ir = min(ar, br)
    ib = min(ab, bb)
    if ir <= il or ib <= it:
        return 0.0
    交 = (ir - il) * (ib - it)
    面a = (ar - al) * (ab - at)
    面b = (br - bl) * (bb - bt)
    return 交 / float(面a + 面b - 交)


def _center_in(inner, outer):
    """inner 的中心点是否落在 outer 矩形内。"""
    cx, cy = inner.center()
    l, t, r, b = outer.bbox
    return l <= cx <= r and t <= cy <= b


def _same(a, b):
    """判定两个元素是否为同一元素：中心点互含或 IoU>0.3。"""
    return _iou(a.bbox, b.bbox) > 0.3 or (_center_in(a, b) and _center_in(b, a))


def dedup_visual(visual列表):
    """visual 家族内部去重（融合前）：同位（IoU>0.3 或中心互含）候选保一。

    语义名优先：带语义 text 的候选（模板通道的「关闭/刷新」、形状通道的
    「☑/滑块」）压过无语义的（通用 icon 候选、无名连通域解读）——否则
    刷新键位置的形状解读 ◉ 会把模板候选「刷新」吞掉，语义劣化；
    同为语义名/同为无名时保 conf 高者。
    """
    def 权重(e):
        return (1 if e.text else 0, e.conf)

    保留 = []
    for v in sorted(visual列表, key=权重, reverse=True):
        if not any(_iou(v.bbox, r.bbox) > 0.3 or
                   (_center_in(v, r) and _center_in(r, v)) for r in 保留):
            保留.append(v)
    return 保留


def fuse(uia列表, ocr列表, visual列表):
    """三源融合，返回去重后的元素列表（未编号）。"""
    合并 = list(uia列表)

    # 1) OCR 融入：与 UIA 同元素的丢弃并把文本并入；否则保留
    for o in ocr列表:
        最佳 = None
        最佳分 = 0.0
        for m in 合并:
            if m.source != "uia":
                continue
            if _same(o, m):
                分 = _iou(o.bbox, m.bbox)
                if 最佳 is None or 分 > 最佳分:
                    最佳 = m
                    最佳分 = 分
        if 最佳 is not None:
            if not 最佳.text and o.text:
                最佳.text = o.text          # UIA 无名，OCR 文本并入
                最佳.conf = max(最佳.conf, o.conf)
            elif (o.text and 最佳.text and len(o.text) > len(最佳.text)
                  and 最佳.text in o.text and o.conf >= 0.7):
                # UIA 无障碍桥常缓存旧名（按钮改名后 Name 不刷新，tk 等框架实测），
                # OCR 读的是当前画面：新文本更长且完整包含旧名时，以画面为准
                最佳.text = o.text
                最佳.conf = max(最佳.conf, o.conf)
        else:
            合并.append(o)

    # 2) 视觉图标：被 UIA/OCR 元素覆盖的吞并丢弃，孤立的保留。
    #    例外（2026-10-25 二十棒，Q-018 同源坑）：模拟器/自绘宿主的 UIA 树会
    #    暴露盖满整个画面的无名容器（雷电实测 pane TheRender/sub），按中心
    #    互含规则会把画面上全部视觉候选吞没——高置信视觉（X 专项 conf≥0.5）
    #    不被容器类元素吞并；低置信 icon（0.3 噪声候选）维持原吞并防噪声涌入。
    _容器类型 = ("pane", "window", "group", "custom", "document")
    for v in visual列表:
        被吞 = False
        for m in 合并:
            if v.conf >= 0.5 and m.source == "uia" and m.type in _容器类型:
                continue
            if _center_in(v, m) or _iou(v.bbox, m.bbox) > 0.3:
                被吞 = True
                break
        if not 被吞:
            合并.append(v)

    return 合并


def assign_ids(元素, row_tol=24, screen_wh=None):
    """按从上到下、从左到右（带行容差分行）编号，id 从 1 开始。

    面积占屏幕 >=80% 的容器元素（桌面/根 pane 等无操作价值）后置编号，
    让实际可操作内容获得靠前的小编号。
    """
    if screen_wh:
        屏面积 = screen_wh[0] * screen_wh[1]
        容器id集 = set()
        for e in 元素:
            if (e.bbox[2] - e.bbox[0]) * (e.bbox[3] - e.bbox[1]) >= 0.8 * 屏面积:
                容器id集.add(id(e))
        普通 = [e for e in 元素 if id(e) not in 容器id集]
        容器 = [e for e in 元素 if id(e) in 容器id集]
    else:
        容器 = []
        普通 = list(元素)
    元素s = 普通 + 容器
    元素s.sort(key=lambda e: (e.bbox[1], e.bbox[0]))
    # 贪心分行：中心纵向距离接近的归同一行
    行s = []  # 每行: [元素列表, 行中心y]
    for e in 元素s:
        cx, cy = e.center()
        高 = e.bbox[3] - e.bbox[1]
        放置 = False
        for i, (行, 中心y) in enumerate(行s):
            if abs(cy - 中心y) <= max(row_tol, 高 / 2.0):
                行.append(e)
                # 更新行中心为行内元素纵向中心均值
                新y = sum(x.center()[1] for x in 行) / len(行)
                行s[i] = (行, 新y)
                放置 = True
                break
        if not 放置:
            行s.append(([e], float(cy)))
    # 行按行中心排序，行内按左边界排序
    行s.sort(key=lambda rc: rc[1])
    编号 = 0
    for 行, _ in 行s:
        行.sort(key=lambda e: e.bbox[0])
        for e in 行:
            编号 += 1
            e.id = 编号
    return 元素s
