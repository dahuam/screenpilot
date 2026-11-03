# -*- coding: utf-8 -*-
"""视觉引擎（轻量兜底）：OpenCV 连通域找疑似图标/按钮候选 + X 关闭按钮专项检测。

通用通道流程：灰度 -> Canny 边缘 -> 膨胀 -> 连通域 -> 按面积与长宽比过滤。
产出的 icon 元素置信度低，可被融合层吞并（有 UIA/OCR 元素落在其上时丢弃）。

X 专项通道（2026-10-25 二十棒，Q-018 漏识根治）：游戏自绘 UI 的「×」关闭按钮
既非文字（OCR 不出）又各有一副难啃的模样，官服两例实测全漏——
  · 暗背景公告红X：红块紧贴弹窗金框，笔画边缘与框线黏成团，全图 Canny+Hough
    拟不出它的笔画（漏识主因）；
  · 白底黑细线X：本形状可检，但同屏二维码的高密度边缘拟出海量假对角交叉
    （误报洪水）。
对策为双通道，只认「两条对角笔画交叉」的结构证据，与颜色无关：
  · 通道一（色块+反色笔画）：高饱和色块内提亮/压暗出笔画掩码后局部验证——
    色块自身天然与邻界隔离，免疫黏连；
  · 通道二（形状+环带）：全图 Hough 配对后加 bbox 外环带边缘密度验证——
    二维码等纹理区环带密度爆表，整片灭掉。
配对闸门四道：两笔方向一正一反、近似垂直（90°±20°，汉字撇捺夹角偏平偏陡）、
等长（按钮 × 对称）、交点在两笔中段；配对后再过「四臂齐全」验证（X 自交点向
四个对角方向都有笔画延伸，汉字交叉四臂常缺）。
检出后 text 标「×」、conf=0.72（高于通用 icon，低于 OCR 文本——OCR 若已在
同位出字则融合层自然吞并，不产生重复元素）。

形状通道族（2026-10-25 施工单 C，同思路扩展）：勾选框/单选圆点/滑块三类
常见控件形状——表单/设置页高频控件，UIA 盲区场景（自绘/游戏设置面板）
与纯图标同理整片漏识。沿用「形状闸门 + 验证条件」防误报范本：
  · 勾选框 checkbox：小方形连通域过「四边带」验证（四条边带内边缘密度
    齐=框线完整，+字/斜框/实心图标不过）+ 环带干净度（贴文字标签的表单
    环境比 X 的 0.35 宽松）；框内勾笔画/填充判选中态。实心填充方块一律
    不认（与图标色块无法区分，宁可少认）。
  · 单选圆点 radio：小圆形连通域（膨胀闭合后近实心盘）+ 圆周完整 + 环带
    干净度——字母 O/数字 0 处在文字笔画区，环带密度爆表被灭（X 专项同款
    思路）；中心实点判选中态。
  · 滑块 slider：细长水平轨道线 + 轨道条带内的小方/圆钮配对——两者同现
    才认（纯横线=分隔线/进度条不报，钮单独=普通图标不报）。
形状候选 conf 0.60~0.68，高于通用 icon 的 0.3、过融合保护线 0.5（不被
无名大容器吞并），低于 OCR 文本（同位有字时 OCR 优先）。
"""
import math

import cv2
import numpy as np

from .model import Element

# X 通道公共量程：按常见关闭按钮 12~60px 设定
_X尺寸下限 = 10
_X尺寸上限 = 70
_对角斜率范围 = (0.6, 1.7)   # 与水平夹角约 ±31°~60°，水平/垂直笔画不算
_环带密度上限 = 0.35         # bbox 外环带边缘占比超过它=处于纹理区，弃


def _框重叠(a, b):
    """两个 bbox [l,t,r,b] 是否算同一处：IoU>0.3 或一方中心落入另一方。"""
    al, at, ar, ab = a
    bl, bt, br, bb = b
    il, it, ir, ib = max(al, bl), max(at, bt), min(ar, br), min(ab, bb)
    if ir > il and ib > it:
        交 = (ir - il) * (ib - it)
        并 = (ar - al) * (ab - at) + (br - bl) * (bb - bt) - 交
        if 并 > 0 and 交 / float(并) > 0.3:
            return True
    acx, acy = (al + ar) / 2.0, (at + ab) / 2.0
    bcx, bcy = (bl + br) / 2.0, (bt + bb) / 2.0
    return (bl <= acx <= br and bt <= acy <= bb) or (al <= bcx <= ar and at <= bcy <= ab)


def _交点(a, b):
    """两条线段的交点；不相交或交点不在两笔中段（非真交叉）返回 None。"""
    x1, y1, x2, y2 = a
    x3, y3, x4, y4 = b
    分母 = float((x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3))
    if abs(分母) < 1e-9:
        return None  # 平行
    t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / 分母
    u = ((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) / 分母
    if not (0.12 <= t <= 0.88 and 0.12 <= u <= 0.88):
        return None
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))


def _对角线段(笔画图):
    """从二值笔画/边缘图提对角线段列表：[(坐标四元组, 长度, 方向±1), …]。"""
    线段集 = cv2.HoughLinesP(笔画图, 1, np.pi / 180, threshold=12,
                            minLineLength=7, maxLineGap=3)
    if 线段集 is None:
        return []
    segs = []
    for l in 线段集:
        # OpenCV 4.x 返回 (N,1,4)、5.x 返回 (N,4)：ravel 后统一取四元组
        x1, y1, x2, y2 = map(int, np.ravel(l))
        dx, dy = x2 - x1, y2 - y1
        长度 = math.hypot(dx, dy)
        if 长度 < 7 or 长度 > _X尺寸上限 or dx == 0:
            continue
        斜率 = abs(dy) / float(abs(dx))
        if 斜率 < _对角斜率范围[0] or 斜率 > _对角斜率范围[1]:
            continue
        segs.append(((x1, y1, x2, y2), 长度, 1 if dx * dy > 0 else -1))
    return segs


def _配对成框(segs, 尺寸下限=_X尺寸下限, 尺寸上限=_X尺寸上限):
    """对角线段两两配对（方向一正一反/近似垂直/等长/真交叉）
    → [(bbox, 交点, 贯穿度)]。

    「近似垂直 + 等长」是按钮 × 与汉字撇捺交叉（「乂」「人」类，夹角≠90°、
    两笔悬殊）的关键分界，官服公告正文实测靠它压掉文字误报。贯穿度=
    平均笔长/bbox 对角：按钮 × 的笔画贯穿整个域（0.7~1.0），勾选框内的
    ✓ 笔画只占小半（<0.45）——与形状通道同位冲突时靠它裁决。
    """
    框s = []
    已配 = set()
    for i in range(len(segs)):
        if i in 已配:
            continue
        pa, la, sa = segs[i]
        cax, cay = (pa[0] + pa[2]) / 2.0, (pa[1] + pa[3]) / 2.0
        ka = (pa[3] - pa[1]) / float(pa[2] - pa[0]) if pa[2] != pa[0] else 99.0
        for j in range(i + 1, len(segs)):
            if j in 已配:
                continue
            pb, lb, sb = segs[j]
            if sa == sb:
                continue
            kb = (pb[3] - pb[1]) / float(pb[2] - pb[0]) if pb[2] != pb[0] else 99.0
            # 两笔近似垂直（90°±20°）：按钮 × 正交，汉字撇捺夹角偏平/偏陡
            if not (0.6 <= abs(ka * kb) <= 1.67):
                continue
            if not (0.6 < la / lb < 1.67):  # 两笔等长（按钮 × 对称）
                continue
            if abs(cax - (pb[0] + pb[2]) / 2.0) > 18 or abs(cay - (pb[1] + pb[3]) / 2.0) > 18:
                continue
            交 = _交点(pa, pb)
            if 交 is None:
                continue
            l = min(pa[0], pa[2], pb[0], pb[2]) - 2
            t = min(pa[1], pa[3], pb[1], pb[3]) - 2
            r = max(pa[0], pa[2], pb[0], pb[2]) + 2
            b = max(pa[1], pa[3], pb[1], pb[3]) + 2
            w, h = r - l, b - t
            if not (尺寸下限 <= w <= 尺寸上限 and 尺寸下限 <= h <= 尺寸上限):
                continue
            if not (0.5 <= w / float(h) <= 2.0):  # X 的外接框应近方形
                continue
            对角 = math.hypot(w, h)
            贯穿 = (la + lb) / 2.0 / 对角
            框s.append(([l, t, r, b], 交, 贯穿))
            已配.update((i, j))
            break
    return 框s


def _中心去重(候选, 间距=12):
    """中心相近（同一 X 的多对线段/双通道重复）只留一个。候选=[(框, 交, …), …]"""
    结果 = []
    for 条目 in 候选:
        框 = 条目[0]
        cx, cy = (框[0] + 框[2]) / 2.0, (框[1] + 框[3]) / 2.0
        if all(abs(cx - (e[0][0] + e[0][2]) / 2.0) > 间距
               or abs(cy - (e[0][1] + e[0][3]) / 2.0) > 间距 for e in 结果):
            结果.append(条目)
    return 结果


def _积分密度(积分, l, t, r, b):
    """边缘二值积分图上矩形区域的边缘像素占比（越界自动夹取）。"""
    l, t = max(0, int(l)), max(0, int(t))
    r, b = min(积分.shape[1] - 1, int(r)), min(积分.shape[0] - 1, int(b))
    面积 = float((r - l) * (b - t))
    if 面积 <= 0:
        return 1.0
    return (积分[b, r] - 积分[b, l] - 积分[t, r] + 积分[t, l]) / 面积


def _四臂齐(积分, 框, 交):
    """X 的四条臂自交点向四个对角方向延伸——汉字交叉四臂常缺，据此再筛。"""
    cx, cy = 交
    s = min(框[2] - 框[0], 框[3] - 框[1]) * 0.20
    for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
        ax, ay = cx + dx * s * 0.7, cy + dy * s * 0.7
        bx, by = cx + dx * s * 1.7, cy + dy * s * 1.7
        if _积分密度(积分, min(ax, bx) - 1, min(ay, by) - 1,
                     max(ax, bx) + 1, max(ay, by) + 1) < 0.10:
            return False
    return True


def _通道色块(img_bgr):
    """通道一：高饱和色块 + 块内反色笔画 → [(bbox, 交点)]（红底白X一类，免疫黏连）。"""
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    掩码 = ((hsv[:, :, 1] > 110) & (hsv[:, :, 2] > 60)).astype(np.uint8)
    掩码 = cv2.morphologyEx(掩码, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    数量, _, 统计, _ = cv2.connectedComponentsWithStats(掩码)
    候选 = []
    for i in range(1, 数量):
        x, y, w, h, area = 统计[i]
        if not (_X尺寸下限 <= w <= _X尺寸上限 and _X尺寸下限 <= h <= _X尺寸上限):
            continue
        if not (0.55 <= w / float(h) <= 1.8):
            continue
        if area < 0.35 * w * h:  # 色块须实心（碎边角不算）
            continue
        roi灰 = cv2.cvtColor(img_bgr[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)
        中位 = float(np.median(roi灰))
        # 块内亮笔画（红底白X）或暗笔画（亮底深X）二选一，哪个出交叉算哪个
        for 笔画 in ((roi灰 - 中位 > 45).astype(np.uint8),
                     (中位 - roi灰 > 45).astype(np.uint8)):
            if 笔画.sum() < 0.05 * w * h:
                continue
            # 笔画须是块内部图案：按钮 × 居中不贴边；贴边的「笔画」实为
            # 背景缝隙（文字团块里尤其如此，官服金色字团实测误报源）
            if 笔画[1:-1, 1:-1].sum() < 笔画.sum() * 0.85:
                continue
            笔画d = cv2.dilate(笔画 * 255, np.ones((2, 2), np.uint8))
            # 局部线段配对出的框与交点平移回原图坐标（加色块偏移 x,y）
            候选 += [([l + x, t + y, r + x, b + y], (ix + x, iy + y), 比)
                     for (l, t, r, b), (ix, iy), 比 in _配对成框(_对角线段(笔画d))]
    return 候选


def _通道形状(边缘):
    """通道二：全图 Hough 配对 + 环带干净度 → [(bbox, 交点)]（白底细线X一类）。

    环带验证：关闭按钮四周是干净底；二维码/纹理区的假交叉环带全是边缘。
    """
    # 二次膨胀：1~2px 细笔画的 Canny 双边缘黏成单线，配对才齐
    笔画图 = cv2.dilate(边缘, np.ones((3, 3), np.uint8), iterations=1)
    框s = _配对成框(_对角线段(笔画图))
    边二值 = (笔画图 > 0).astype(np.uint8)
    积分 = cv2.integral(边二值)
    干净 = []
    for 框, 交, _比 in 框s:
        l, t, r, b = 框
        外 = max(4, (r - l) // 3)
        环外 = _积分密度(积分, l - 外, t - 外, r + 外, b + 外)
        内 = _积分密度(积分, l, t, r, b)
        if 环外 <= 0.5 and 环外 - max(内, 0.0001) <= _环带密度上限:
            干净.append((框, 交, _比))
    return 干净


# ---- 形状通道族：勾选框 / 单选圆点 / 滑块（施工单 C）----

_框尺寸下限 = 9      # 勾选框/圆点边长量程（常见控件 12~26px）
_框尺寸上限 = 36
_勾选框环带上限 = 0.55   # 环带边缘占比上限（贴标签文字的表单环境，比 X 宽松）
_圆点环带上限 = 0.42     # 单选圆点环带更严（O/0 在笔画区，必须灭）


def _四边带密度(积分, l, t, r, b):
    """候选框四条边带的边缘密度 → (上, 下, 左, 右)。带宽随框自适应（≈1/6 边长，≥2px）。"""
    w, h = r - l, b - t
    带 = max(2, min(w, h) // 6)
    return (_积分密度(积分, l, t, r, t + 带),
            _积分密度(积分, l, b - 带, r, b),
            _积分密度(积分, l, t, l + 带, b),
            _积分密度(积分, r - 带, t, r, b))


def _四角密度(积分, l, t, r, b, 角=None):
    """候选框四个角部小区（角=边长 1/5，≥3px）的边缘密度——方框角有线，
    圆/椭圆离角远：勾选框与单选圆点的决定性分界。"""
    w, h = r - l, b - t
    角 = 角 or max(3, min(w, h) // 5)
    return (_积分密度(积分, l, t, l + 角, t + 角),
            _积分密度(积分, r - 角, t, r, t + 角),
            _积分密度(积分, l, b - 角, l + 角, b),
            _积分密度(积分, r - 角, b - 角, r, b))


def _判圆点(积分, x, y, w, h, 填充率, 边积分=None):
    """小圆形连通域过单选圆点验证 → None / '○' / '◉'（中心实点=选中）。

    闸门：①尺寸 8~30；②宽高比近圆；③环带干净——字母 O/数字 0 处在文字
    笔画区环带密度爆表被灭（X 专项同款思路）；④圆周四正向触边（弧线连续）；
    ⑤环内空隙（膨胀前边缘图，内接 r/2 方形）：空心 radio 环内是空的、
    ◉ 环内只有中心点——实心/带细节的圆形图标（工具栏圆图标、复选圆钮）
    环内一概有内容，密度爆表被灭（真图误报泛滥的根治闸门）。
    填充率宽带 0.25~0.95：空心环膨胀后 ~0.3+、小实心圆 ~0.9，均收。
    """
    if w < 8 or h < 8 or w > 30 or h > 30:
        return None
    if not (0.78 <= w / float(h) <= 1.28):
        return None
    if not (0.25 <= 填充率 <= 0.95):
        return None
    r = (w + h) / 4.0
    外 = max(3, int(r * 0.8))
    cx, cy = x + w / 2.0, y + h / 2.0
    if _积分密度(积分, cx - r - 外, cy - r - 外, cx + r + 外, cy + r + 外) > _圆点环带上限 + 0.22:
        return None
    # 圆周四正向：外扩 1~2px 处的上下左右中点小区应有边缘（圆弧连续性）
    触 = 0
    对 = max(2, int(r * 0.5))
    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
        px = int(cx + dx * (w / 2.0 + 1.5))
        py = int(cy + dy * (h / 2.0 + 1.5))
        if _积分密度(积分, px - 对 // 2, py - 对 // 2, px + 对 // 2 + 1, py + 对 // 2 + 1) > 0.18:
            触 += 1
    if 触 < 3:
        return None
    # 环内空隙（膨胀前边缘）：内接 r/2 方形内，空心环应为空；有内容再分
    # 「仅中心点（◉）」与「带细节图标（拒）」
    内密度 = 0.0
    中心点密度 = 0.0
    if 边积分 is not None:
        半 = max(2, int(r * 0.55))
        内密度 = _积分密度(边积分, cx - 半, cy - 半, cx + 半, cy + 半)
        心 = max(2, int(r * 0.28))
        中心点密度 = _积分密度(边积分, cx - 心, cy - 心, cx + 心, cy + 心)
        if 内密度 > 0.10 and 中心点密度 > 0.10:
            return None   # 环内有中心之外的细节 = 图标
        if 内密度 > 0.28:
            return None   # 内部内容过密 = 图标
    中心密度 = _积分密度(积分, int(cx - r * 0.45), int(cy - r * 0.45),
                       int(cx + r * 0.45), int(cy + r * 0.45))
    if 中心密度 >= 0.30:
        return '◉'
    if 中心点密度 > 0.10:
        return None   # 环内有零星内容但不成实心点 = 图标
    return '○'


def _是勾(中心区二值):
    """勾形笔画验证：中心区检出「一短一长」的两段相交线 → True。

    勾选框选中态（✓）与 × 的结构分界在**短长悬殊**：✓ 短/长 0.2~0.55，
    × 等长（0.6~1.67，X 专项配对闸门口径）。夹角只排平行/共线（真勾两笔
    夹角 70°~110° 与 × 的 90° 分不开，不能当判据）。Hough 阈值取 7/5px：
    游戏装饰小图标内部常能凑出碎短段，提阈压凑对误报。暗底红粗 X 的色块
    会被形状通道误读成 ☑（2px 笔画把填充率拉进框线区间），靠这道验证打回。
    """
    线段集 = cv2.HoughLinesP(中心区二值, 1, np.pi / 180, threshold=7,
                            minLineLength=5, maxLineGap=2)
    if 线段集 is None:
        return False
    segs = []
    for l in 线段集:
        x1, y1, x2, y2 = map(int, np.ravel(l))
        长度 = math.hypot(x2 - x1, y2 - y1)
        if 长度 >= 5:
            segs.append(((x1, y1, x2, y2), 长度))
    for i in range(len(segs)):
        pa, la = segs[i]
        for j in range(i + 1, len(segs)):
            pb, lb = segs[j]
            短, 长 = (la, lb) if la <= lb else (lb, la)
            if not (0.15 <= 短 / float(长) <= 0.55):   # 勾两笔悬殊；等长是 ×
                continue
            # 相交（非平行/共线）：方向向量叉积非零 + 交点在两笔范围内
            v1 = (pa[2] - pa[0], pa[3] - pa[1])
            v2 = (pb[2] - pb[0], pb[3] - pb[1])
            叉 = v1[0] * v2[1] - v1[1] * v2[0]
            if abs(叉) < 1e-6:
                continue   # 平行/共线
            交 = _交点(pa, pb)
            if 交 is None:
                continue
            return True
    return False


def _判勾选框(积分, x, y, w, h, 填充率, 中心区二值=None):
    """方形连通域过勾选框验证 → None / '☐'（空心）/ '☑'（框内见勾笔画）。

    五道闸门：①近正方；②非实心块（实心=图标色块，无法与按钮区分，弃；
    Canny 轮廓膨胀后实心块填充率 0.95+，框线型 0.4~0.85 可分）；③四边带
    密度齐（框线完整——斜框/缺角框不过）；④四角有线（方框角有线、圆离角
    远——单选圆点的决定性分界，实测 2px 线圆环四边带也能凑过，靠它灭）；
    ⑤环带干净（贴着密集文字的不算）。
    """
    if w < _框尺寸下限 or h < _框尺寸下限 or w > _框尺寸上限 or h > _框尺寸上限:
        return None
    if not (0.70 <= w / float(h) <= 1.43):
        return None
    if 填充率 > 0.85:   # 实心块不认（宁少认不乱认）
        return None
    上, 下, 左, 右 = _四边带密度(积分, x, y, x + w, y + h)
    if min(上, 下, 左, 右) < 0.28 or sum((上, 下, 左, 右)) / 4.0 < 0.40:
        return None
    if min(_四角密度(积分, x, y, x + w, y + h)) < 0.22:
        return None
    # 中环带干净度（框线内圈到中心区之间，12%~28% 内缩环带）：真实勾选框
    # 框内除居中勾笔画外干净，环带密度低；图标（截图框/剪刀/头像/文字块）
    # 内部细节必然穿过该环带——雷电侧栏实战 4 个 ☑ 误报的根治闸
    内12x, 内12y = max(2, int(w * 0.12)), max(2, int(h * 0.12))
    内28x, 内28y = int(w * 0.28), int(h * 0.28)
    大区 = (x + 内12x, y + 内12y, x + w - 内12x, y + h - 内12y)
    小区 = (x + 内28x, y + 内28y, x + w - 内28x, y + h - 内28y)
    大边缘 = (_积分密度(积分, *大区) * (大区[2] - 大区[0]) * (大区[3] - 大区[1]))
    小边缘 = (_积分密度(积分, *小区) * (小区[2] - 小区[0]) * (小区[3] - 小区[1]))
    环带面积 = float((大区[2] - 大区[0]) * (大区[3] - 大区[1])
                 - (小区[2] - 小区[0]) * (小区[3] - 小区[1]))
    if 环带面积 > 4 and (大边缘 - 小边缘) / 环带面积 > 0.68:
        return None   # 框内环带细节密集 = 图标不是勾选框（实测：侧栏图标
                      # 0.69~1.0，真勾笔画 0.64——阈值 0.68 分档）
    外 = max(4, min(w, h) // 3)
    if _积分密度(积分, x - 外, y - 外, x + w + 外, y + h + 外) > _勾选框环带上限 + 0.30:
        return None
    # 选中态：中心区（内缩 28%）三段判定——空框 / 勾笔画（须过勾形验证，
    # 挡「框内等长垂直叉」的 ×）/ 实心填充（选中态）。实心判定看「最大
    # 连通域占区域比」：交叉笔画膨胀后会填满小中心区的密度、但 X 形四角
    # 是空的（占比 ~0.6），实心填充块 ≥0.85——密度单指标分不开这两者。
    内缩x, 内缩y = int(w * 0.28), int(h * 0.28)
    中心密度 = _积分密度(积分, x + 内缩x, y + 内缩y, x + w - 内缩x, y + h - 内缩y)
    if 中心密度 < 0.10:
        return '☐'
    if 中心区二值 is None or (中心区二值 > 0).sum() < 6:
        return '☑' if 中心密度 >= 0.5 else None
    数n, _, 统n, _ = cv2.connectedComponentsWithStats(
        (中心区二值 > 0).astype(np.uint8))
    if 数n > 1:
        最大 = max(统n[i, cv2.CC_STAT_AREA] for i in range(1, 数n))
        区面积 = 中心区二值.shape[0] * 中心区二值.shape[1]
        if 最大 / float(区面积) >= 0.85:
            return '☑'   # 实心填充（选中态），非笔画交叉
    return '☑' if _是勾(中心区二值) else None


def _形状去重(元素):
    """形状元素互撞去重：同一处多形状/多候选只留 conf 最高者（一票形态证据）。"""
    保留 = []
    for e in sorted(元素, key=lambda x: -x.conf):
        if not any(_框重叠(e.bbox, r.bbox) for r in 保留):
            保留.append(e)
    return 保留


def _detect_滑块(膨胀):
    """滑块 = 水平轨道 + 骑在轨道上的小钮，从「轨道+钮黏连」的连通域行密度
    剖面识别 → [bbox(钮)]。

    行密度法：对水平长域逐行统计边缘像素数——满宽行带=轨道，非满宽行=钮
    凸出部分（其列范围=钮位置）。纯分隔线/进度条（全行满宽）与孤立钮
    （无满宽行）天然不认，两者同现才成立。
    """
    数量, _, 统计, _ = cv2.connectedComponentsWithStats(膨胀)
    钮s = []
    for i in range(1, 数量):
        x, y, w, h, area = 统计[i]
        # 水平长域：轨道 60~600px 长；总高含钮凸出可到 ~40px
        if not (60 <= w <= 600 and 2 <= h <= 40):
            continue
        掩码 = 膨胀[y:y + h, x:x + w] > 0
        行宽 = 掩码.sum(axis=1)
        满行 = np.where(行宽 >= 0.55 * w)[0]
        if len(满行) < 2 or len(满行) > 0.7 * h:
            continue   # 无轨道 / 全满（进度条/分隔线）
        轨t, 轨b = int(满行[0]), int(满行[-1]) + 1
        # 钮 = 非满宽行的列范围（凸出轨道的部分；钮不凸出时退回满行带内的
        # 宽度突变列——此处只认凸出型，保守不误报）
        非满 = 掩码.copy()
        非满[轨t:轨b] = False
        列有 = np.where(非满.sum(axis=0) >= 2)[0]
        if len(列有) < _框尺寸下限:
            continue
        列min, 列max = int(列有[0]), int(列有[-1]) + 1
        钮宽 = 列max - 列min
        凸上 = 轨t - int(np.where(非满[:, 列min:列max].sum(axis=1) > 0)[0][0])
        凸下 = int(np.where(非满[:, 列min:列max].sum(axis=1) > 0)[0][-1]) - 轨b + 1
        钮高 = 凸上 + (轨b - 轨t) + 凸下
        if not (_框尺寸下限 <= 钮宽 <= 34 and 8 <= 钮高 <= 40):
            continue
        if not (0.45 <= 钮宽 / float(钮高) <= 2.2):
            continue
        钮s.append([int(x + 列min), int(y + 轨t - 凸上),
                    int(x + 列max), int(y + 轨b + 凸下)])
        if len(钮s) >= 12:
            break
    return 钮s


def scan_visual(img_bgr, min_size=16, max_size=300, min_area=40, max_count=80):
    """从图像提取疑似图标候选 + X 关闭按钮 + 形状通道族，返回元素列表。bbox 相对输入图像左上角。"""
    灰度 = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    边缘 = cv2.Canny(灰度, 50, 150)
    膨胀 = cv2.dilate(边缘, np.ones((3, 3), np.uint8), iterations=1)
    # X 双通道产出后，统一在全图边缘上做「四臂齐全」验证（汉字交叉四臂常缺）。
    # 候选带贯穿度（平均笔长/bbox 对角）：真 × 笔画贯穿全域（0.7~1.0），
    # 同位冲突裁决用（贯穿=真 ×；勾选框内 ✓ 只占小半，让位 ☑）。
    边二值 = (膨胀 > 0).astype(np.uint8)
    积分 = cv2.integral(边二值)
    色块候选 = [c for c in _中心去重(_通道色块(img_bgr)) if _四臂齐(积分, c[0], c[1])]
    细线候选 = [c for c in _中心去重(_通道形状(边缘)) if _四臂齐(积分, c[0], c[1])]
    x框s = [c[0] for c in 色块候选] + [c[0] for c in 细线候选]
    x比s = {tuple(c[0]): c[2] for c in 色块候选 + 细线候选}

    数量, _, 统计, _ = cv2.connectedComponentsWithStats(膨胀)

    # 环内空隙验证用膨胀前边缘的积分图（radio 三区结构判别）
    边积分 = cv2.integral((边缘 > 0).astype(np.uint8))

    def _撞x(x, y, w, h):
        框 = [x, y, x + w, y + h]
        return any(_框重叠(框, x框) for x框 in x框s)

    候选 = []
    形状元素 = []
    for i in range(1, 数量):
        x, y, w, h, area = 统计[i]
        # 形状通道族先判：自带尺寸闸门（8~36px）比 icon 的 min_size 更下探
        # ——勾选框/单选圆点 10~15px 是常态，不能被 icon 尺寸过滤拦截
        填充率 = area / float(w * h)
        if (8 <= w <= _框尺寸上限 and 8 <= h <= _框尺寸上限 and area >= 30):
            # 不做 _撞x 预排除：框内 ✓ 常诱发 X 专项误报（下方同位让位规则
            # 收敛），形状专项的证据链比通用线段配对更特异
            # 先圆后框（圆更特异；判中即互斥，圆环不该再算勾选框）
            形 = _判圆点(积分, x, y, w, h, 填充率, 边积分)
            if 形 is None:
                # 勾选框选中态验证需要中心区笔画图（膨胀图同坐标系裁剪）
                中心区 = None
                内缩x, 内缩y = int(w * 0.28), int(h * 0.28)
                if w - 2 * 内缩x >= 4 and h - 2 * 内缩y >= 4:
                    中心区 = 膨胀[y + 内缩y:y + h - 内缩y,
                                 x + 内缩x:x + w - 内缩x]
                形 = _判勾选框(积分, x, y, w, h, 填充率, 中心区)
            if 形 is not None:
                形状元素.append(Element(
                    type="checkbox" if 形 in ("☐", "☑") else "radio",
                    text=形,
                    bbox=[int(x), int(y), int(x + w), int(y + h)],
                    source="visual",
                    conf=0.68 if 形 in ("☑", "◉") else 0.60,
                    clickable=True,
                ))
        # icon 候选过滤：疑似图标按钮的大小范围
        if w < min_size or h < min_size or w > max_size or h > max_size:
            continue
        # 填充率过滤：剔除稀疏散点噪声
        if area < min_area:
            continue
        # 长宽比过滤：0.2~5 之间（排除细长线条与极端形状）
        ratio = w / float(h)
        if ratio < 0.2 or ratio > 5.0:
            continue
        if _撞x(x, y, w, h):
            continue
        候选.append((area, x, y, w, h))

    # 滑块：轨道+钮配对（行密度法，黏连域内直接分型）
    for 框 in _detect_滑块(膨胀):
        形状元素.append(Element(
            type="slider",
            text="滑块",
            bbox=框,
            source="visual",
            conf=0.60,
            clickable=True,
        ))
    形状元素 = _形状去重(形状元素)
    # 空形态成组约束：☐/○ 单发必是图标（任意空心小框/环都像），真实表单
    # 控件成组（复选列/单选组）——同列或同行存在尺寸相近（±30%）、间距
    # 10~130px（真实表单行高 48~120px 量程）的另一形状元素（含选中态，
    # 「1 空 + 1 选中」是单选组常态）才认。选中态（☑◉，有内部笔画/实心点
    # 证据）自身豁免。
    空形态 = [e for e in 形状元素 if e.text in ("☐", "○")]
    for e in 空形态:
        w = e.bbox[2] - e.bbox[0]
        h = e.bbox[3] - e.bbox[1]
        cx, cy = e.center()
        有伴 = False
        for o in 形状元素:
            if o is e or o.text not in ("☐", "☑", "○", "◉"):
                continue
            ow, oh = o.bbox[2] - o.bbox[0], o.bbox[3] - o.bbox[1]
            if not (0.7 <= w / float(max(1, ow)) <= 1.43
                    and 0.7 <= h / float(max(1, oh)) <= 1.43):
                continue
            ox, oy = o.center()
            if (abs(ox - cx) <= max(w, ow) * 0.6 and 10 <= abs(oy - cy) <= 130) or \
               (abs(oy - cy) <= max(h, oh) * 0.6 and 10 <= abs(ox - cx) <= 130):
                有伴 = True
                break
        if not 有伴:
            形状元素 = [x for x in 形状元素 if x is not e]
    # 同位冲突裁决（贯穿度定生死）：真 × 的笔画贯穿整个域（贯穿度 0.7+），
    # 勾选框内 ✓ 只占小半（<0.45）——被形状通道误读成 ☑ 的色块 ×（贯穿）
    # 判 × 赢；X 专项拿「框角伪段+✓ 笔」配出的假 ×（不贯穿）让位 ☑。
    裁后形状 = []
    for s in 形状元素:
        撞框s = [框 for 框 in x框s if _框重叠(s.bbox, list(框))]
        if not 撞框s:
            裁后形状.append(s)
            continue
        贯穿 = max(x比s.get(tuple(框), 0.0) for 框 in 撞框s)
        if 贯穿 < 0.55:
            裁后形状.append(s)     # 不贯穿的假 × 让位，形状（☑ 等）保留
            x框s = [框 for 框 in x框s if 框 not in 撞框s]
        # 贯穿的真 ×：形状元素丢弃（不入裁后形状）
    形状元素 = 裁后形状
    # 与形状元素同位的低置信 icon 候选剔除（形状证据更强，不重复占位）
    候选 = [(a, x, y, w, h) for a, x, y, w, h in 候选
            if not any(_框重叠([x, y, x + w, y + h], s.bbox) for s in 形状元素)]

    # 按面积降序取前 N 个，避免背景噪声淹没元素表。
    # 配额随图幅自适应（r4 加考·G12 截断闸修复）：统一面积降序前 80 对
    # 4K 图是系统性小图标歧视——origin_40（3840×2160）实锤：icon 候选池
    # 142 个、截掉 62 个，出局者全为 16×16 工具栏小图标量级（第 80/81 名
    # 面积同为 361px²），SS-Pro icon oracle 46→29% 的主伤源。信息量与像素
    # 面积成正比：配额 = 80 × 图幅/1080p 图幅（4K→320、1440p→142、
    # ≤1080p→80 不变，SS-v2 等 1K 级图零影响）；320 仍是防爆上限（防纹理
    # 图候选爆炸拖垮融合层与语义匹配器）。
    H全, W全 = img_bgr.shape[:2]
    配额 = max(max_count, H全 * W全 * max_count // (1920 * 1080))
    候选.sort(reverse=True)
    元素 = []
    for area, x, y, w, h in 候选[:配额]:
        元素.append(Element(
            type="icon",
            text="",
            bbox=[int(x), int(y), int(x + w), int(y + h)],
            source="visual",
            conf=0.3,
            clickable=True,
        ))
    元素 += 形状元素
    for 框 in x框s:
        元素.append(Element(
            type="icon",
            text="×",
            bbox=[int(框[0]), int(框[1]), int(框[2]), int(框[3])],
            source="visual",
            conf=0.72,
            clickable=True,
        ))
    return 元素
