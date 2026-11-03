# -*- coding: utf-8 -*-
"""OCR 文字引擎：RapidOCR（onnxruntime CPU 推理）。

输入 BGR ndarray，输出 text 类型元素；
短文本（<=8 字符）视为可点候选（clickable=True）。
"""
import cv2
import numpy as np

from .model import Element

_ocr实例 = None
_ocr快速实例 = None
_实例锁 = None   # 懒建：预热后台线程与首扫并发加载防双实例（F5）
_预热线程 = None

# F2（2026-11-01 深夜）：增量裁块小字粘连根治——短边 < 该值的块放大 2 倍再进
# det（真实按钮行 275x45 实证：原样进 'max' 把 'max min' 粘成一框、'SearchV'
# 跨按钮粘连截断（Volume/Home/Tools/User 全丢）；2x CUBIC 放大后逐词独立
# 检出且最快（145ms vs 'min'400 实例 837ms）。bbox 坐标按系数还原，对外零感。
_快速放大短边 = 200
_快速放大倍数 = 2

# F4（2026-11-02 凌晨）：单字符 CC 兜底路。病根实锤不是「深色低对比」而是
# det(DBNet) 对孤立单字符全字号盲——训练分布外（合成单字 12~110px 白底黑字
# 全灭、同图多字符全检出；深底密集小字四档对比度全 5/5 命中，「深底整片漏识」
# 为 det=736 时代旧账，det=400 后不复现）。真实场景：计算器显示区深底大"0"、
# 雷电侧栏计数角标"7"均 det 0 框 → expect 判文本必假失败。
# 治法：det 跑完后双态 OTSU 连通域提候选→det 覆盖排除→紧贴裁片（pad3+小字
# 放大）走 text_recognizer 单独识别。收录双档 conf：≥max(min_conf,0.5) 全字符
# 集；0.42 档只收 ASCII 短 token（≤4 字符数字/字母/常用符号）——单字符 rec
# conf 天花板实测 ~0.49（'0'0.498/'7'0.487），而图标笔画垃圾（'由''古''龍'）
# 全 <0.47 且为非 ASCII，白名单+双档把收益与误报切开（雷电大厅 12 候选实测
# 零误报）。
_兜底白名单线 = 0.42
_兜底全字符线 = 0.5
_兜底白名单字符 = set(
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.%:/+-")

# F5（2026-11-02 凌晨）rec 提速实验结论（负结果，防重蹈）：为消 rec 批内
# 「框 pad 到批内最大宽高比」的浪费试过 batch 加大（32/64，反而慢 2.5~4.4 倍）、
# batch=1（踩线条目 conf 0.558→0.487 掉过 0.5 线，真丢框）、长条 ratio>8 逐框
# 分桶（固定屏受控对照 494ms vs 原版 416ms，无收益反慢）——三案全砍。根因：
# rec 总耗时由当屏框的 ratio 分布决定，收益是屏型赌博不普适，且任何打乱批内
# 组合的方案都有文本/score 抖动回归面（89 框实测 2 条文本变体）。全屏 rec 热
# 态真实账 ~0.4~2s（屏型依赖），模型已是 v3 mobile 最小规格（det 2.3MB+rec
# 10.2MB），det 限边 min=400 再降伤小字（F1 先例）——F5 主刀改落冷启动预热
# （见 preload）：进程启动后台加载模型，首扫不吃 4~6 秒加载。


def preload(后台=True):
    """预热 OCR 实例（模型加载 4~6s 是冷首扫大头），MCP/CLI 入口启动时调用。

    后台=True 起守护线程立即返回；False 同步加载（测试用）。
    重复调用幂等（已有预热线程在跑或加载完则跳过）。
    """
    global _预热线程
    if _预热线程 is not None and _预热线程.is_alive():
        return
    if 后台:
        import threading
        _预热线程 = threading.Thread(target=_预热加载, daemon=True,
                                     name="ocr-preload")
        _预热线程.start()
    else:
        _预热加载()


def _预热加载():
    get_ocr(False)
    get_ocr(True)


def get_ocr(快速=False):
    """懒加载 RapidOCR 单例（首次初始化约 1~2 秒）。

    快速=True：det 预处理 limit_type='max' + 关 cls。默认配置 det 用 'min'
    ——增量裁出的小块会被放大到短边 limit 再推理；'max'（长边超 736 才缩）
    下小块原尺寸进 det，同块 56ms 且识别文本逐字一致（600x400 对照 97ms
    vs 363ms）。det_model_path=None 是 1.2.3 kwargs 更新器的必带键（缺它
    KeyError，None=用默认模型文件）。

    全量实例 det_limit_side_len=400（2026-11-01）：默认 736 会把小窗口
    （短边 <736）过度上采样（536x239 靶窗被放大约 3 倍），det 对 12 号
    中文稳定漏检（tk 按钮同名双「确定」只检出 1 个，3 轮复跑稳定复现）；
    降到 400 后 2/2 检出且错字修正，7 张真实截图对照零退化（含 2560x1400
    全屏 89 条——短边超限不缩放，大图行为零变化），det 输入面积降至 30%
    小窗全量 OCR 顺带提速。
    """
    global _ocr实例, _ocr快速实例, _实例锁
    if _实例锁 is None:
        _实例锁 = __import__("threading").Lock()
    if 快速:
        if _ocr快速实例 is None:
            with _实例锁:
                if _ocr快速实例 is None:
                    from rapidocr_onnxruntime import RapidOCR
                    _ocr快速实例 = RapidOCR(det_limit_side_len=736, det_limit_type='max',
                                            det_model_path=None, use_cls=False)
        return _ocr快速实例
    if _ocr实例 is None:
        with _实例锁:
            if _ocr实例 is None:
                from rapidocr_onnxruntime import RapidOCR
                _ocr实例 = RapidOCR(det_limit_side_len=400, det_limit_type='min',
                                    det_model_path=None)
    return _ocr实例


def _单字符兜底(img_bgr, 已有框, min_conf, 快速=False):
    """det 后 CC 连通域兜底：补孤立单字符（见文件头 F4 注）。bbox 相对输入图。

    **分块双态 OTSU**：整窗多峰分布（如计算器浅色按钮块+深底读数区）会把全局
    OTSU 阈值拉偏吞掉白字（整窗实测 67 候选全碎片零读数），按 320px 块 160px
    步长（50% 重叠，保证 <160px 字符至少完整落在一个块内）分块后块内分布简单
    （双态 OTSU 在块内完美）；亮字/暗字两态各提一遍。中心点 12px 网格去重
    （重叠块同字符收敛同格）；中心点落入 det 已检出框（±4px）的候选丢弃。
    候选裁片 pad3，高度<48px CUBIC 3x 放大进 rec（单字符 rec conf 随小字放大
    升，pad2x3 实测最高）。rec 用当前实例的 text_recognizer 成员（两实例同
    rec 模型）。rec 数量封顶 24（area 降序，防图标密集窗裁片爆炸）。
    """
    灰 = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    h图, w图 = 灰.shape
    块, 步 = 320, 160
    候选 = []
    seen = set()
    for y0 in range(0, h图, 步):
        for x0 in range(0, w图, 步):
            y1, x1 = min(h图, y0 + 块), min(w图, x0 + 块)
            g = 灰[y0:y1, x0:x1]
            if g.shape[0] < 16 or g.shape[1] < 16:
                continue
            for 反相 in (False, True):
                gg = 255 - g if 反相 else g
                _, bw = cv2.threshold(gg, 0, 255,
                                      cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                if bw.mean() > 127:
                    bw = 255 - bw
                n, _, stats, _ = cv2.connectedComponentsWithStats(bw, 8)
                for i in range(1, n):
                    x, y, cw, ch, area = stats[i]
                    if ch < 8 or ch > 300 or cw < 2 or cw > 500 or area < 15:
                        continue
                    gx, gy = x0 + int(x), y0 + int(y)
                    key = ((gx + cw // 2) // 12, (gy + ch // 2) // 12)
                    if key in seen:
                        continue
                    seen.add(key)
                    候选.append((gx, gy, gx + int(cw), gy + int(ch),
                                 int(area)))
    盒子 = [(b[0] - 4, b[1] - 4, b[2] + 4, b[3] + 4) for b in 已有框]
    def _已覆盖(b):
        cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
        return any(x1 <= cx <= x2 and y1 <= cy <= y2 for x1, y1, x2, y2 in 盒子)
    候选 = [b for b in 候选 if not _已覆盖(b)]
    if not 候选:
        return []
    if len(候选) > 24:   # rec 数量封顶：图标密集窗保大头，防裁片爆炸
        候选.sort(key=lambda b: -b[4])
        候选 = 候选[:24]
    裁片 = []
    for x1, y1, x2, y2, _a in 候选:
        c = img_bgr[max(0, y1 - 3):min(h图, y2 + 3),
                    max(0, x1 - 3):min(w图, x2 + 3)]
        if c.shape[0] < 48:
            c = cv2.resize(c, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        裁片.append(c)
    try:
        res, _elapse = get_ocr(快速).text_recognizer(裁片)
    except Exception:
        return []
    元素 = []
    已收 = []   # 收录级去重：重叠块同字符中心点量化跨格拦不住（差 4px 跨线）
    for (x1, y1, x2, y2, _a), item in zip(候选, res):
        text, score = item
        text = (text or "").strip()
        score = float(score)
        if not text:
            continue
        紧凑 = text.replace(" ", "")
        is白 = len(紧凑) <= 4 and all(c in _兜底白名单字符 for c in 紧凑)
        # 白名单档固定 0.42（字符集强先验，与调用方 min_conf 解耦——默认
        # min_conf=0.5 会把该档抬回 0.5 形同虚设；'0'0.497/'7'0.487 是单字符
        # rec conf 天花板区）；全字符集档听更严一方
        if score < max(min_conf, _兜底全字符线) and not (
                is白 and score >= _兜底白名单线):
            continue
        dup = False
        for (a1, b1, c1, d1), t1 in 已收:
            ix = max(0, min(x2, c1) - max(x1, a1))
            iy = max(0, min(y2, d1) - max(y1, b1))
            if text == t1 and ix * iy > 0.4 * ((x2 - x1) * (y2 - y1)):
                dup = True
                break
        if dup:
            continue
        已收.append(((x1, y1, x2, y2), text))
        元素.append(Element(type="text", text=text, bbox=[x1, y1, x2, y2],
                           source="ocr", conf=score,
                           clickable=len(text) <= 8))
    return 元素


def scan_ocr(img_bgr, min_conf=0.5, 快速=False):
    """对图像做全量 OCR，返回元素列表。bbox 相对输入图像左上角。

    快速：增量裁块通道用（小图不放大，见 get_ocr）；默认 False，
    现有调用方行为零变化。快速通道下短边 < _快速放大短边 的块先放大
    _快速放大倍数 倍（CUBIC）再进 det——根治小字词间距近的 det 粘连
    （'max min' 一框 / 'SearchV' 截断），bbox 按系数还原。
    """
    倍数 = 1
    原图 = img_bgr   # 快速通道会放大 img_bgr；兜底路用原尺寸图（bbox 同坐标系）
    if 快速:
        h0, w0 = img_bgr.shape[:2]
        if min(h0, w0) < _快速放大短边:
            # 放大后长边不得超 736：超了 'max' 会缩回，DBNet 对缩回的长条
            # 全灭（实测宽 400 块 2x 后 0 条，宽 350 的 2x=700 正常 6 条）
            倍数 = min(_快速放大倍数, max(1, 736 // max(h0, w0)))
            if 倍数 > 1:
                img_bgr = cv2.resize(img_bgr, (w0 * 倍数, h0 * 倍数),
                                     interpolation=cv2.INTER_CUBIC)
    result, _elapse = get_ocr(快速)(img_bgr)
    元素 = []
    # det 空结果不走 early return——孤立单字符整图 det 0 框恰是兜底路的主场，
    # 提前 return 会把兜底一起跳过（F4 实测：雷电角标图因此零产出）
    for box, text, score in (result or []):
        text = (text or "").strip()
        score = float(score)
        if not text or score < min_conf:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        l, t = int(min(xs)), int(min(ys))
        r, b = int(max(xs)), int(max(ys))
        if r - l <= 1 or b - t <= 1:
            continue
        if 倍数 > 1:
            l, t, r, b = l // 倍数, t // 倍数, r // 倍数, b // 倍数
        # 短文本独立小框视为可点候选（按钮/链接多为此形态）
        可点 = len(text) <= 8
        元素.append(Element(
            type="text",
            text=text,
            bbox=[l, t, r, b],
            source="ocr",
            conf=score,
            clickable=可点,
        ))
    # F4 单字符兜底：det 对孤立单字符全字号盲（计算器显示区/雷电角标漏检
    # 根因），det 路出框后再补一路 CC+rec；失败不碍 det 主流程
    try:
        元素.extend(_单字符兜底(原图, [e.bbox for e in 元素], min_conf, 快速))
    except Exception:
        pass
    return 元素
