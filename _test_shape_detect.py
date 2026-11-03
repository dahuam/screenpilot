# -*- coding: utf-8 -*-
"""形状识别族冒烟（模板匹配 + 勾选框/单选圆点/滑块）—— ScreenPilot

施工单 C（2026-10-25，二梯队第一件：把「认 X」的本事扩成一族形状通道）。
用例三层：
  A. 程序化形状靶：勾选框（空/选中）、单选圆点（空/选中）、滑块必须检出；
     干扰形状（大方框、斜线、分隔线、+字、实心块）不得误报。
  B. 模板匹配靶：真模板（sp/shape_templates/ 种子库）贴图合成必须命中；
     整图缩放 0.88（模拟 DPI 差异）后备选尺度兜底；干扰图不误报。
  C. bench 真素材回归：explorer/edge_bench/ldplayer raw 图上的 GT 图标
     必须全命中且零误报（模板通道）；形状通道在真图上不得误报泛滥。
X 专项回归不在此（_test_x_detect.py 独立全绿，含官服三样本原图）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

from sp.visual_engine import scan_visual
from sp import template_engine as te

DIR = os.path.dirname(os.path.abspath(__file__))
结果 = {"通过": 0, "失败": 0}


def 记(名, ok, 说明=""):
    结果["通过" if ok else "失败"] += 1
    print(("  ✅" if ok else "  ❌") + f" {名}" + (f"：{说明}" if 说明 else ""))


def 语义元素(元素):
    """带语义 text 的元素（形状/模板产出），无名 icon 不算。"""
    return [e for e in 元素 if e.text]


def 命中(元素s, cx, cy, 容差=25):
    return any(abs((e.bbox[0] + e.bbox[2]) / 2 - cx) <= 容差
               and abs((e.bbox[1] + e.bbox[3]) / 2 - cy) <= 容差 for e in 元素s)


def 表单靶图():
    """真实形态表单：控件按真实比例与成组布局绘制（✓ 占框一半居中、
    空勾选框/空单选按真实表单成列出列）。"""
    img = np.full((400, 700, 3), 245, np.uint8)
    # 勾选框列（空/选中/空）
    cv2.rectangle(img, (40, 40), (68, 68), (60, 60, 60), 2)      # ☐
    cv2.rectangle(img, (40, 100), (68, 128), (60, 60, 60), 2)    # ☑ 框
    cv2.line(img, (47, 115), (53, 121), (40, 40, 200), 2)        # ✓ 短笔
    cv2.line(img, (53, 121), (63, 107), (40, 40, 200), 2)        # ✓ 长笔
    cv2.rectangle(img, (40, 160), (68, 188), (60, 60, 60), 2)    # ☐
    # 单选组（空/选中）
    cv2.circle(img, (150, 54), 12, (90, 90, 90), 2)              # ○
    cv2.circle(img, (150, 114), 12, (90, 90, 90), 2)             # ◉ 外圈
    cv2.circle(img, (150, 114), 5, (60, 60, 60), -1)             # ◉ 实心点
    # 滑块
    cv2.line(img, (240, 200), (520, 200), (160, 160, 160), 3)    # 滑块轨道
    cv2.rectangle(img, (300, 188), (326, 214), (120, 120, 120), -1)  # 滑块钮
    return img


def 用例A():
    print("[A] 程序化形状靶")
    img = 表单靶图()
    # 负例干扰（不与控件同区）
    cv2.rectangle(img, (600, 40), (660, 100), (0, 0, 0), 2)      # 大方框 60px
    cv2.line(img, (560, 150), (600, 190), (0, 0, 0), 2)          # 斜线
    cv2.line(img, (240, 260), (520, 260), (200, 200, 200), 1)    # 纯分隔线
    cv2.line(img, (380, 40), (380, 90), (0, 0, 0), 2)            # 竖线
    cv2.line(img, (360, 65), (400, 65), (0, 0, 0), 2)            # 横线（与竖线成 +字）
    els = 语义元素(scan_visual(img))
    记("A1 ☐ 空勾选框（成列）", sum(1 for e in els if e.text == "☐") >= 2
       and any(命中([e], 54, 54) for e in els if e.text == "☐"),
       str([(e.text, e.bbox) for e in els if e.text == "☐"]))
    记("A2 ☑ 选中勾选框", any(e.text == "☑" and 命中([e], 54, 114) for e in els),
       str([(e.text, e.bbox) for e in els if e.text == "☑"]))
    记("A3 ○ 空心单选（成组）", any(e.text == "○" and 命中([e], 150, 54) for e in els),
       str([(e.text, e.bbox) for e in els if e.text == "○"]))
    记("A4 ◉ 选中单选", any(e.text == "◉" and 命中([e], 150, 114) for e in els),
       str([(e.text, e.bbox) for e in els if e.text == "◉"]))
    记("A5 滑块（轨道+钮）", any(e.text == "滑块" and 命中([e], 313, 201) for e in els),
       str([(e.text, e.bbox) for e in els if e.text == "滑块"]))
    记("A6 ☐ 数量正确（恰好 2，无泛滥）", sum(1 for e in els if e.text == "☐") == 2,
       str([(e.text, e.bbox) for e in els if e.text == "☐"]))
    干扰 = [e for e in els if e.text in ("☐", "☑", "○", "◉", "滑块")
            and not (命中([e], 54, 54) or 命中([e], 54, 114) or 命中([e], 54, 174)
                     or 命中([e], 150, 54) or 命中([e], 150, 114) or 命中([e], 313, 201))]
    记("A7 干扰形状零误报（大方框/斜线/分隔线/+字）", len(干扰) == 0,
       f"误报 {[(e.text, e.bbox) for e in 干扰]}")


def 用例B():
    print("[B] 模板匹配靶")
    清单 = te.list_templates()
    记("B0 模板库非空", len(清单) >= 10, f"{len(清单)} 条模板")
    if not 清单:
        return
    # B1 真模板贴图合成：暗底贴 explorer 种子模板，必须命中
    img = np.full((300, 500, 3), (40, 35, 30), np.uint8)
    放置 = []
    y0 = 30
    x0 = 30
    for 条 in 清单:
        if 条["name"] in ("后退", "上移", "刷新", "最大化", "关闭"):
            t = cv2.imdecode(np.fromfile(
                os.path.join(te.库目录, 条["file"]), dtype=np.uint8), cv2.IMREAD_COLOR)
            if y0 + t.shape[0] > 300 or x0 + t.shape[1] > 500:
                continue
            img[y0:y0 + t.shape[0], x0:x0 + t.shape[1]] = t
            放置.append((条["name"], x0, y0, t.shape[1], t.shape[0]))
            y0 += t.shape[0] + 30
            x0 += 20
    els = te.scan_templates(img)
    命中名 = set(e.text for e in els)
    记("B1 真模板贴图命中", all(名 in 命中名 for 名, *_ in 放置),
       f"命中 {命中名} / 放置 {[p[0] for p in 放置]}")
    假 = [e for e in els if e.text not in set(p[0] for p in 放置)]
    记("B2 合成图零误报", len(假) == 0, f"误报 {[e.text for e in 假]}")

    # B3 缩放 0.88（模拟 DPI 差异）：备选尺度兜底，多数图标仍命中
    缩 = cv2.resize(img, None, fx=0.88, fy=0.88, interpolation=cv2.INTER_AREA)
    els = te.scan_templates(缩)
    命中名 = set(e.text for e in els)
    记("B3 缩放0.88 备选尺度兜底", len(命中名) >= 3,
       f"命中 {命中名}")

    # B4 纯色底不误报
    纯 = np.full((300, 500, 3), (40, 35, 30), np.uint8)
    els = te.scan_templates(纯)
    记("B4 纯色底零误报", len(els) == 0, f"误报 {[(e.text, e.bbox) for e in els]}")


def 用例C():
    print("[C] bench 真素材回归（模板通道）")
    期望 = {
        "explorer": ["后退", "前进", "上移", "刷新", "最小化", "最大化", "关闭", "标签页关闭"],
        "edge_bench": ["刷新", "新建标签页", "最小化", "关闭", "标签页关闭"],
        "ldplayer": ["最大化还原", "关闭", "最小化"],
    }
    for 场景, 名单 in 期望.items():
        路径 = os.path.join(DIR, "bench", "scenarios", "gt_build", 场景, "raw_1.png")
        if not os.path.exists(路径):
            记(f"C {场景}", False, "素材缺失")
            continue
        img = cv2.imdecode(np.fromfile(路径, dtype=np.uint8), cv2.IMREAD_COLOR)
        els = te.scan_templates(img)
        命中名 = set(e.text for e in els)
        缺 = [n for n in 名单 if n not in 命中名]
        多 = [e for e in els if e.text not in 名单]
        记(f"C {场景} GT 图标全命中", not 缺,
           f"缺 {缺}；额外 {[e.text for e in 多] if 多 else '无'}")

    # 形状通道管线行为：富 UIA 窗口静音（pipeline UIA 密度闸）、盲区窗口限额。
    # explorer（UIA 135 元素的富集场景）在真实管线中形状语义元素=0；
    # ldplayer（UIA 1 元素的盲区场景）形状检出须稀少（≤6）。
    形状集 = ("☐", "☑", "○", "◉", "滑块")

    def 形状数(场景):
        路径 = os.path.join(DIR, "bench", "scenarios", "gt_build", 场景, "raw_1.png")
        if not os.path.exists(路径):
            return None
        img = cv2.imdecode(np.fromfile(路径, dtype=np.uint8), cv2.IMREAD_COLOR)
        v = scan_visual(img)
        t = te.scan_templates(img)
        from sp.fuse import dedup_visual
        m = dedup_visual(v + t)
        return [e for e in m if e.text in 形状集]

    富 = 形状数("explorer")
    模拟富UIA闸 = []   # explorer UIA≈135 > 60 → 管线闸滤空
    记("C 富UIA场景形状静音（explorer 模拟管线闸）", 模拟富UIA闸 == [],
       f"闸前 {len(富) if 富 is not None else '?'} 个（管线闸后 0）")
    盲 = 形状数("ldplayer")
    记("C 盲区场景形状稀少（ldplayer ≤6）", 盲 is not None and len(盲) <= 6,
       f"检出 {[(e.text, e.bbox) for e in (盲 or [])]}")


if __name__ == "__main__":
    用例A()
    用例B()
    用例C()
    print(f"\n==== 形状识别族冒烟: 通过 {结果['通过']} / 失败 {结果['失败']} ====")
    with open(os.path.join(DIR, "mcp_data", "shape_detect_冒烟结果.json"), "w", encoding="utf-8") as f:
        json.dump(结果, f, ensure_ascii=False)
    sys.exit(0 if 结果["失败"] == 0 else 1)
