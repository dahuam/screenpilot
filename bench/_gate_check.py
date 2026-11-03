# -*- coding: utf-8 -*-
"""标签伴生闸降级处置自检（任务A重构功能验证，非跑分）。

三个分支：
  1. 无伴生标签的形状语义（☐/○）→ 降级为 text=""/type="icon"/conf=0.3；
  2. 有伴生标签的形状语义（右侧带文字）→ 保持形状语义名；
  3. 降级后的中性候选位置不消失（bbox 仍在元素表）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

from sp import pipeline

结果 = {"通过": 0, "失败": 0}


def 记(名, ok, 说明=""):
    结果["通过" if ok else "失败"] += 1
    print(("  ✅" if ok else "  ❌") + f" {名}" + (f"：{说明}" if 说明 else ""))


def 命中(元素s, cx, cy, 容差=25):
    return any(abs((e.bbox[0] + e.bbox[2]) / 2 - cx) <= 容差
               and abs((e.bbox[1] + e.bbox[3]) / 2 - cy) <= 容差 for e in 元素s)


def 主():
    img = np.full((400, 700, 3), 245, np.uint8)
    # 无标签形状（左列）
    cv2.rectangle(img, (40, 40), (68, 68), (60, 60, 60), 2)      # ☐ 无标签
    cv2.circle(img, (150, 54), 12, (90, 90, 90), 2)              # ○ 无标签
    cv2.circle(img, (150, 114), 12, (90, 90, 90), 2)             # ◉ 无标签
    cv2.circle(img, (150, 114), 5, (60, 60, 60), -1)
    # 有标签形状（右列，右侧带文字标签）：标准「框内 ✓」选中勾选框
    cv2.rectangle(img, (420, 40), (448, 68), (60, 60, 60), 2)    # ☑ 框
    cv2.line(img, (427, 55), (433, 61), (40, 40, 200), 2)        # ✓ 短笔
    cv2.line(img, (433, 61), (443, 47), (40, 40, 200), 2)        # ✓ 长笔
    cv2.putText(img, "Enable", (470, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (30, 30, 30), 2)

    落盘 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_gate_check.png")
    # 中文路径安全写盘（cv2.imwrite 对 unicode 路径静默失败，台账坑22）
    cv2.imencode(".png", img)[1].tofile(落盘)
    res = pipeline.scan_image(落盘)
    els = res["elements"]

    # 1) 无标签 ☐（40,54 处）应降级：同位存在元素且其 text 不是 ☐
    同位 = [e for e in els if 命中([e], 54, 54)]
    记("1a 无标签☐同位候选保留（不消失）", len(同位) >= 1,
       str([(e.text, e.type, e.conf, e.bbox) for e in 同位]))
    记("1b 无标签☐已降级（无形状语义名）", all(e.text != "☐" for e in 同位),
       str([(e.text, e.type) for e in 同位]))

    # 2) 无标签 ○/◉（150,54 / 150,114 处）同样降级
    同位圆 = [e for e in els if 命中([e], 150, 54) or 命中([e], 150, 114)]
    记("2 无标签○◉降级", 同位圆 and all(e.text not in ("○", "◉") for e in 同位圆),
       str([(e.text, e.type, e.conf) for e in 同位圆]))

    # 3) 有标签 ☐（434,54 处，右侧 Enable）保持形状语义
    有标签 = [e for e in els if e.text in ("☐", "☑") and 命中([e], 434, 54)]
    记("3 有标签☐保持形状语义", len(有标签) >= 1,
       str([(e.text, e.type, e.conf) for e in 有标签]))

    os.remove(落盘)


if __name__ == "__main__":
    主()
    print(f"\n==== 伴生闸降级自检: 通过 {结果['通过']} / 失败 {结果['失败']} ====")
    sys.exit(0 if 结果["失败"] == 0 else 1)
