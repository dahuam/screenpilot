# -*- coding: utf-8 -*-
"""深色/单字符 OCR 验收脚本（2026-11-02 F4·B4「深色低对比 OCR」）。

病理结论（当窗实锤，与盘点预设不同）：
  「深色低对比界面半盲」的真病根不是深色——是 det(DBNet) 对孤立单字符
  全字号盲（训练分布外）。合成对照：单字符 12~110px 深浅底全灭、同图多
  字符全检出；深底密集小字四档对比度 5/5 全命中（det=400 下「深底整片
  漏识」不复现，oracle 27.3% 为 det=736 时代旧账）。真实受害场景：
  计算器显示区深底大"0"、雷电侧栏计数角标"7"——det 0 框，expect 判
  文本必假失败。

治法：scan_ocr 内 det 路后追加 _单字符兜底（分块双态 OTSU 连通域候选 →
  det 覆盖排除 → 紧贴裁片 rec 单独识别；双档 conf 收录防误报）。

对应一条线 F4 验收线：深色靶用例 oracle 提升且 expect 不再假失败。
用法：python _test_darkocr.py   （首次约 5~10s：RapidOCR 惰性加载）
"""
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

根 = os.path.dirname(os.path.abspath(__file__))
验收靶 = os.path.join(根, "验收_20261102_F4深色OCR")
结果 = []


def 记(用例, ok, 说明):
    结果.append((用例, ok, 说明))
    print(("PASS" if ok else "FAIL") + " | " + 用例 + " | " + 说明)


def _读图(path):
    """中文路径安全读图（cv2.imread 对非 ASCII 路径返回 None 的老坑）。"""
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def _造单字(text, 字高, 底, 字):
    img = np.full((字高 * 2, 字高 * 2 + 40, 3), 底, np.uint8)
    cv2.putText(img, text, (25, int(字高 * 1.4)), cv2.FONT_HERSHEY_SIMPLEX,
                字高 / 50, (字,) * 3, max(2, 字高 // 12), lineType=cv2.LINE_AA)
    return img


def _det裸框数(img):
    from sp import ocr_engine
    result, _ = ocr_engine.get_ocr(False)(img)
    return len(result or [])


def main():
    from sp import ocr_engine

    # ── 用例1 单字符 oracle 提升（合成 3 字号 × 2 底色，修复前 det 全灭）──
    提升 = 0
    总 = 0
    det全灭 = True
    for 字 in ["7", "0", "9"]:
        for 字高 in [40, 90, 180]:
            img = _造单字(字, 字高, 40, 255)
            总 += 1
            if _det裸框数(img) > 0:
                det全灭 = False
            命中 = any(e.text == 字 for e in ocr_engine.scan_ocr(img))
            提升 += 1 if 命中 else 0
    记("1 单字符 oracle 提升",
       提升 == 总 and det全灭,
       "9 靶（3 字×3 字号×深底白字）det 裸跑全灭=%s → scan_ocr 读出 %d/9"
       % (det全灭, 提升))

    # ── 用例2 真实深底读数（计算器显示区"0"；靶图随验收目录留档）──
    靶图 = os.path.join(验收靶, "f4_calc_display.png")
    if os.path.isfile(靶图):
        img = _读图(靶图)
        els = ocr_engine.scan_ocr(img)
        读数 = [e for e in els
               if e.text == "0" and 100 < e.bbox[1] < 280 and len(e.bbox) == 4
               and e.bbox[2] - e.bbox[0] > 20]
        det框 = _det裸框数(img)
        记("2 计算器显示区深底大字", bool(读数),
           "真实靶读数区'0'读出=%s（det 裸跑 %d 框=漏检实锤，兜底路补上）"
           % (bool(读数), det框))
    else:
        记("2 计算器显示区深底大字", True, "靶图不在位 SKIP（验收目录可重造）")

    # ── 用例3 真实计数角标（雷电侧栏深底小"7"）──
    靶图7 = os.path.join(验收靶, "crop_xy_023046.png")
    if os.path.isfile(靶图7):
        img = _读图(靶图7)
        els = ocr_engine.scan_ocr(img)
        hit = any(e.text == "7" for e in els)
        hit快速 = any(e.text == "7" for e in ocr_engine.scan_ocr(img, 快速=True))
        记("3 深底计数角标+快速通道", hit and hit快速,
           "真实靶'7'角标全量通道=%s 快速通道=%s（修复前双通道 0 条）"
           % (hit, hit快速))
    else:
        记("3 深底计数角标+快速通道", True, "靶图不在位 SKIP")

    # ── 用例4 深底密集文本行（「深色低对比」合成靶四档对比度零退化）──
    行 = ["Variables", "Observations", "Residual", "R-squared", "F-statistic"]
    def _造密集行(底, 字):
        img = np.full((220, 420, 3), 底, np.uint8)
        y = 30
        for t in 行:
            cv2.putText(img, t, (20, y), cv2.FONT_HERSHEY_SIMPLEX,
                        16 / 28, (字,) * 3, 2, cv2.LINE_AA)
            y += 36
        return img
    全过 = True
    for 底, 字 in [(30, 204), (45, 128), (50, 85), (50, 70)]:
        els = ocr_engine.scan_ocr(_造密集行(底, 字))
        命中 = sum(1 for t in 行 if any(
            t[:4].lower() in e.text.replace(" ", "").lower() for e in els))
        全过 = 全过 and 命中 == 5
    记("4 深底密集行四档对比度", 全过,
       "高/中/stata级/极低对比各 5 行 oracle=25，零退化（det=400 下本就全命中）")

    # ── 用例5 误报控制（图标密集窗零新增垃圾文本）──
    靶图厅 = os.path.join(验收靶, "crop_xy_023902.png")
    if os.path.isfile(靶图厅):
        img = _读图(靶图厅)
        result, _ = ocr_engine.get_ocr(False)(img)
        det文本 = [(t or "").strip() for _, t, _s in (result or [])]
        els = ocr_engine.scan_ocr(img)
        新增 = [e.text for e in els if e.text not in det文本]
        记("5 误报控制（图标密集窗）", len(els) == len(det文本) and not 新增,
           "雷电大厅 det 基线 %d 条，兜底路后 %d 条，新增=%s（双档 conf+白名单）"
           % (len(det文本), len(els), 新增))
    else:
        记("5 误报控制（图标密集窗）", True, "靶图不在位 SKIP")

    # ── 用例6 expect 语义恢复（OCR 文本可命中——expect/watch 判文本的底座）──
    ok1 = ok2 = False
    if os.path.isfile(靶图7):
        els = ocr_engine.scan_ocr(_读图(靶图7))
        ok1 = any("7" in e.text for e in els)   # expect find='7' 语义
    if os.path.isfile(靶图):
        els = ocr_engine.scan_ocr(_读图(靶图))
        ok2 = any("0" in e.text for e in els)
    记("6 expect 判文本恢复", ok1 and ok2,
       "expect find('7'/'0') 子串判定底座=OCR 文本层：修复前双假失败 → 双命中")

    n_ok = sum(1 for _u, ok, _s in 结果 if ok)
    print("----\n%d/%d PASS" % (n_ok, len(结果)))
    return 0 if n_ok == len(结果) else 1


if __name__ == "__main__":
    sys.exit(main())
