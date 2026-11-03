# -*- coding: utf-8 -*-
"""X 关闭按钮专项识别冒烟 —— ScreenPilot（2026-10-25 二十棒）

用例两层：
  A. 程序化靶图：暗底红粗X / 白底黑细X / 纹理底白X 必须检出；
     干扰形状（+字、方框、横竖线）不得误报为「×」。
  B. 官服真实漏识样本回归（十九棒 Q-018 两例原图）：
     official_01_登录页公告.png（暗背景红X）、official_02/03_ESC后.png（白底黑细线X）
     ——漏识原图必须翻案。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

from sp.visual_engine import scan_visual

DIR = os.path.dirname(os.path.abspath(__file__))
结果 = {"通过": 0, "失败": 0}


def 记(名, ok, 说明=""):
    结果["通过" if ok else "失败"] += 1
    print(("  ✅" if ok else "  ❌") + f" {名}" + (f"：{说明}" if 说明 else ""))


def 找x(元素):
    return [e for e in 元素 if e.text == "×"]


def 画x(img, cx, cy, size, 颜色, 粗):
    cv2.line(img, (cx - size // 2, cy - size // 2), (cx + size // 2, cy + size // 2), 颜色, 粗)
    cv2.line(img, (cx - size // 2, cy + size // 2), (cx + size // 2, cy - size // 2), 颜色, 粗)


def 命中(x元素s, cx, cy, 容差=25):
    return any(abs((e.bbox[0] + e.bbox[2]) / 2 - cx) <= 容差
               and abs((e.bbox[1] + e.bbox[3]) / 2 - cy) <= 容差 for e in x元素s)


def 用例A():
    print("[A] 程序化靶图")

    # A1 暗底红粗X（官服公告弹窗形态）
    img = np.full((300, 400, 3), (40, 35, 30), np.uint8)
    cv2.rectangle(img, (100, 60), (300, 240), (60, 50, 45), -1)
    画x(img, 285, 75, 24, (60, 60, 230), 3)  # BGR 红
    xs = 找x(scan_visual(img))
    记("A1 暗底红粗X 检出", len(xs) == 1 and 命中(xs, 285, 75),
       f"检出 {[(e.bbox, e.conf) for e in xs]}")

    # A2 白底黑细线X（官服退出确认框形态）
    img = np.full((300, 400, 3), (245, 245, 245), np.uint8)
    cv2.rectangle(img, (120, 80), (280, 220), (255, 255, 255), -1)
    cv2.rectangle(img, (120, 80), (280, 220), (0, 0, 0), 1)
    画x(img, 268, 92, 18, (30, 30, 30), 1)
    xs = 找x(scan_visual(img))
    记("A2 白底黑细线X 检出", len(xs) == 1 and 命中(xs, 268, 92),
       f"检出 {[(e.bbox, e.conf) for e in xs]}")

    # A3 纹理噪声底白X（游戏面板纹理形态）
    img = np.random.randint(50, 90, (300, 400, 3), dtype=np.uint8)
    画x(img, 200, 150, 30, (250, 250, 250), 2)
    xs = 找x(scan_visual(img))
    记("A3 纹理底白X 检出", len(xs) >= 1 and 命中(xs, 200, 150),
       f"检出 {len(xs)} 处")

    # A4 干扰形状不误报：+字、方框、横竖线、单斜线（各画一个，均不得标×）
    img = np.full((400, 400, 3), (245, 245, 245), np.uint8)
    cv2.line(img, (80, 80), (120, 80), (0, 0, 0), 2)     # 横线
    cv2.line(img, (200, 60), (200, 120), (0, 0, 0), 2)   # 竖线
    cv2.line(img, (60, 200), (120, 160), (0, 0, 0), 2)   # 单斜线
    cv2.line(img, (180, 160), (180, 220), (0, 0, 0), 2)  # +字 竖笔
    cv2.line(img, (150, 190), (210, 190), (0, 0, 0), 2)  # +字 横笔
    cv2.rectangle(img, (260, 60), (330, 130), (0, 0, 0), 2)  # 方框
    cv2.line(img, (280, 200), (340, 260), (0, 0, 0), 2)  # 反向单斜线
    xs = 找x(scan_visual(img))
    记("A4 干扰形状零误报", len(xs) == 0, f"误报 {[(e.bbox) for e in xs]}")


def 用例B():
    print("[B] 官服真实漏识样本回归（十九棒原图）")
    样本 = [
        ("official_01_登录页公告.png", "暗背景公告红X"),
        ("official_02_ESC后.png", "退出确认框白底黑细线X"),
        ("official_03_ESC二次后.png", "退出确认框白底黑细线X（二次）"),
    ]
    for 文件名, 说明 in 样本:
        路径 = os.path.join(DIR, "mcp_data", 文件名)
        if not os.path.exists(路径):
            记(f"B {文件名}", False, "样本文件缺失")
            continue
        img = cv2.imdecode(np.fromfile(路径, dtype=np.uint8), cv2.IMREAD_COLOR)
        元素 = scan_visual(img)
        xs = 找x(元素)
        落点 = [(int((e.bbox[0] + e.bbox[2]) / 2), int((e.bbox[1] + e.bbox[3]) / 2)) for e in xs]
        # 输出落点供人工读图核对（X 按钮通常在弹窗右上角）
        记(f"B {文件名}（{说明}）", len(xs) >= 1,
           f"×检出 {len(xs)} 处 落点={落点}")
        # 存标注图供人工核对
        调试 = img.copy()
        for e in xs:
            cv2.rectangle(调试, (e.bbox[0], e.bbox[1]), (e.bbox[2], e.bbox[3]), (0, 255, 0), 2)
        出 = os.path.join(DIR, "mcp_data", "x_detect_%s" % 文件名)
        cv2.imencode(".png", 调试)[1].tofile(出)


if __name__ == "__main__":
    用例A()
    用例B()
    print(f"\n==== X 识别冒烟: 通过 {结果['通过']} / 失败 {结果['失败']} ====")
    with open(os.path.join(DIR, "mcp_data", "x_detect_冒烟结果.json"), "w", encoding="utf-8") as f:
        json.dump(结果, f, ensure_ascii=False)
    sys.exit(0 if 结果["失败"] == 0 else 1)
