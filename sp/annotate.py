# -*- coding: utf-8 -*-
"""标注图渲染：在截图上画元素框与编号 + 坐标刻度轴，输出 annotated_scan.png。

坐标刻度轴（2026-10-25 二十棒坑22 闭环配套）：上边缘与左边缘每 100px 画主
刻度线+屏幕坐标数字、50px 半格短刻度——数字一律标**屏幕物理坐标**（窗口
模式图内原点=窗口原点 offset，台账坑13：图内位置 + offset 才是屏幕坐标），
读图者直接读出物理坐标配合 click_xy 直点元素表漏识目标，比凭空猜坐标快得多。
"""
import os

import cv2
from PIL import Image, ImageDraw, ImageFont

# 不同来源不同颜色（RGB）：uia 红、ocr 蓝、visual 绿
颜色表 = {
    "uia": (224, 49, 49),
    "ocr": (34, 98, 230),
    "visual": (33, 145, 80),
}

# 刻度轴：黄色与三源框色（红/蓝/绿）区分；主刻度 100px 带数字，半格 50px 短线
轴色 = (255, 190, 0)
刻度主长 = 16
刻度半长 = 8

_字体缓存 = {}


def _get_font(大小=15):
    """获取中文字体（微软雅黑优先），按字号缓存，失败退默认字体。"""
    if 大小 not in _字体缓存:
        for 路径 in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
            if os.path.exists(路径):
                try:
                    _字体缓存[大小] = ImageFont.truetype(路径, 大小)
                    break
                except Exception:
                    continue
        else:
            _字体缓存[大小] = ImageFont.load_default()
    return _字体缓存[大小]


def _标数字(draw, x, y, text, font, 垫底=True):
    """画带黑底小条的数字标签（保证任意底色上可读）。"""
    tb = draw.textbbox((0, 0), text, font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    if 垫底:
        draw.rectangle([x - 1, y - 1, x + tw + 3, y + th + 3], fill=(0, 0, 0))
    draw.text((x + 1, y + 1), text, fill=轴色, font=font)


def _画刻度轴(draw, w, h, offset):
    """上边缘与左边缘画屏幕物理坐标刻度轴。

    主刻度：屏幕坐标 100 的整数倍（细线 + 坐标数字）；半格：+50 处短刻度线。
    数字标屏幕坐标而非图内坐标（图内原点=窗口原点 offset，台账坑13）。
    """
    font = _get_font(12)
    l0, t0 = int(offset[0]), int(offset[1])
    # 上边缘竖刻度（x 轴）：从图内可见的第一个 100 倍数屏幕坐标开始
    x = ((l0 + 99) // 100) * 100
    while x - l0 <= w:
        图x = x - l0
        draw.line([(图x, 0), (图x, 刻度主长)], fill=轴色, width=1)
        _标数字(draw, 图x + 3, 1, str(x), font)
        if 图x + 50 < w:
            draw.line([(图x + 50, 0), (图x + 50, 刻度半长)], fill=轴色, width=1)
        x += 100
    # 左边缘横刻度（y 轴）同理
    y = ((t0 + 99) // 100) * 100
    while y - t0 <= h:
        图y = y - t0
        draw.line([(0, 图y), (刻度主长, 图y)], fill=轴色, width=1)
        _标数字(draw, 1, 图y + 3, str(y), font)
        if 图y + 50 < h:
            draw.line([(0, 图y + 50), (刻度半长, 图y + 50)], fill=轴色, width=1)
        y += 100


def annotate(img_bgr, 元素, out_path, offset=(0, 0), grid=True):
    """画刻度轴（grid=True 默认开）与元素框、编号，保存 PNG，返回输出路径。

    元素 bbox 为屏幕坐标；img 为全屏或窗口截图——窗口模式必须传该窗口的
    屏幕原点 offset=(l, t)，画框前平移到截图相对坐标，否则整体偏移窗口原点。
    """
    l0, t0 = int(offset[0]), int(offset[1])
    img = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img)
    if grid:
        _画刻度轴(draw, img.width, img.height, (l0, t0))
    font = _get_font(15)
    for e in 元素:
        l, t, r, b = e.bbox[0] - l0, e.bbox[1] - t0, e.bbox[2] - l0, e.bbox[3] - t0
        # 完全出截图范围的元素不画（窗口模式下的屏幕级元素）
        if r <= 0 or b <= 0:
            continue
        color = 颜色表.get(e.source, (120, 120, 120))
        draw.rectangle([l, t, r, b], outline=color, width=2)
        # 编号标签：画在框左上角外侧（贴顶时放框内）
        label = str(e.id) if e.id else "?"
        tb = draw.textbbox((0, 0), label, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        lx = max(0, l)
        ly = t - th - 5
        if ly < 0:
            ly = t  # 贴顶，放框内
        draw.rectangle([lx, ly, lx + tw + 6, ly + th + 4], fill=color)
        draw.text((lx + 3, ly + 2), label, fill=(255, 255, 255), font=font)
    目录 = os.path.dirname(os.path.abspath(out_path))
    if 目录:
        os.makedirs(目录, exist_ok=True)
    img.save(out_path)
    return out_path
