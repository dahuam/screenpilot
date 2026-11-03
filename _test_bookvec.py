# -*- coding: utf-8 -*-
"""书库向量层验收脚本（2026-11-02 F3·B3「书库接 SigLIP 向量」）。

对应一条线 F3 验收线：
  1 开关缺省零行为：BOOKVEC 未设 → visual icon 不入库、向量库不动（零回归根基）
  2 BOOKVEC=1 icon 入库：无文字 visual icon 进库（name=""、crop=None）+ 向量落库
  3 文字通道不污染：有文字条目照旧入库可查；query 非空不命中空名 icon 条目
  4 以图查库盲查：同图标的另一裁剪框查询 → top1 命中正确条目（F3 验收线主用例）
  5 domain 过滤与 domain_key 回读：跨域查询 domain 参数生效
  6 开关关闭后向量库零增长
（全库回填 booksweep --embed 不入本脚本——6000+ 裁片×20ms 分钟级，随盲查
  实测单独跑账）

靶纪律：合成图自造靶（无活屏依赖）+「书库向量靶」独立域，测完清场；向量库
测试前备份、测后还原（库文件是全库共享派生层，不许污染）。
用法：python _test_bookvec.py   （首次跑约 30~60s：SigLIP 211MB 惰性加载）
"""
import json
import os
import shutil
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

根 = os.path.dirname(os.path.abspath(__file__))
结果 = []


def 记(用例, ok, 说明):
    结果.append((用例, ok, 说明))
    print(("PASS" if ok else "FAIL") + " | " + 用例 + " | " + 说明)


def _合成靶图():
    """深灰底三枚高区分度色块图标（红方/绿圆/黄方）+ 语义位置分散。"""
    import cv2
    img = np.full((300, 400, 3), 40, np.uint8)
    cv2.rectangle(img, (50, 50), (90, 90), (0, 0, 255), -1)       # 图标A 红方
    cv2.circle(img, (200, 150), 25, (0, 255, 0), -1)              # 图标B 绿圆
    cv2.rectangle(img, (300, 60), (360, 100), (0, 255, 255), -1)  # 图标C 黄方
    return img


def _靶元素表():
    from sp.model import Element
    return [
        Element(type="icon", text="", bbox=[50, 50, 90, 90],
                source="visual", conf=0.3, clickable=True),
        Element(type="icon", text="", bbox=[175, 125, 225, 175],
                source="visual", conf=0.3, clickable=True),
        Element(type="icon", text="", bbox=[300, 60, 360, 100],
                source="visual", conf=0.3, clickable=True),
        Element(type="button", text="确定", bbox=[40, 220, 120, 252],
                source="uia", conf=1.0, clickable=True),
        Element(type="button", text="取消", bbox=[140, 220, 220, 252],
                source="uia", conf=1.0, clickable=True),
    ]


def _入库(img, meta_extra=None):
    from sp import booklib
    meta = {"window": "书库向量靶", "hwnd": 0, "offset": [0, 0],
            "screen": [400, 300]}
    if meta_extra:
        meta.update(meta_extra)
    return booklib.入库(_靶元素表(), img, meta)


def main():
    from sp import booklib, bookvec
    import cv2

    靶域 = "书库向量靶"
    域目录 = os.path.join(booklib.库根, 靶域)
    npz备份, meta备份 = bookvec.向量文件 + ".testbak", bookvec.元数据文件 + ".testbak"
    有原库 = os.path.isfile(bookvec.向量文件)
    if 有原库:   # 全库共享向量库：先备份，测后还原
        shutil.copy2(bookvec.向量文件, npz备份)
        shutil.copy2(bookvec.元数据文件, meta备份)
    原库行数 = bookvec.库况().get("rows", 0)
    try:
        img = _合成靶图()

        # ── 用例1 开关缺省零行为 ──
        os.environ.pop("BOOKVEC", None)
        摘要 = _入库(img)
        数据 = booklib._读域(靶域)
        icon条目 = [e for e in 数据["entries"] if e.get("type") == "icon"]
        # 有回填库时断言「行数零写入」，空环境断言「文件未创建」——缺省态
        # 根本不该碰向量层，两种环境下语义一致
        if 有原库:
            零写入 = bookvec.库况().get("rows", -1) == 原库行数
        else:
            零写入 = not os.path.isfile(bookvec.向量文件)
        记("1 开关缺省零行为",
           (not icon条目 and 摘要["scanned_in"] == 2 and 摘要["vector"] is None
            and 零写入),
           "缺省 BOOKVEC：icon 全不入库（scanned_in=%d 只收有文字）、vector 账=None、"
           "%s" % (摘要["scanned_in"],
                   "向量库 %d 行零写入" % 原库行数 if 有原库 else "向量库文件未创建"))

        # ── 用例2 BOOKVEC=1 icon 入库+向量落库 ──
        os.environ["BOOKVEC"] = "1"
        摘要 = _入库(img)
        数据 = booklib._读域(靶域)
        icon条目 = [e for e in 数据["entries"] if e.get("type") == "icon"]
        库况 = bookvec.库况()
        ok = (len(icon条目) == 3
              and all(e.get("name") == "" and e.get("crop") is None for e in icon条目)
              and (摘要["vector"] or {}).get("encoded") == 5
              and 库况.get("rows") == 原库行数 + 5)
        记("2 BOOKVEC=1 icon 入库", ok,
           "3 枚 icon 进库（name=''/crop=None）+2 文字条目，vector.encoded=5，"
           "向量库 %d→%d 行" % (原库行数, 库况.get("rows", -1)))

        # ── 用例3 文字通道不污染 ──
        文字命中 = booklib.查找(query="确定", domain=靶域)
        全量命中 = booklib.查找(domain=靶域)
        ok = (len(文字命中) == 1 and 文字命中[0]["name"] == "确定"
              and all(e.get("name") != "" or not e.get("name")
                      for e in [])  # 占位
              and sum(1 for e in 全量命中 if e.get("type") == "icon") == 3)
        记("3 文字通道不污染", ok,
           "query='确定' 恰 1 命中（空名 icon 不占文字候选）；无 query 浏览=5 条含 3 icon"
           "（浏览全库语义，icon 可见）")

        # ── 用例4 以图查库盲查（F3 验收线主用例）──
        # 查询裁片=红方块区域外扩 4px（模拟 crop_xy 实拍：坐标略偏+留白不同），
        # top1 必须命中红方块条目（bbox≈[50,50,90,90]）
        crop = img[46:94, 46:94]
        命中 = bookvec.以图查库(crop, limit=5)
        top = 命中[0] if 命中 else {}
        ok = (len(命中) == 5 and top.get("type") == "icon"
              and top.get("bbox") == [50, 50, 90, 90] and top.get("cos", 0) > 0.7)
        记("4 以图查库盲查", ok,
           "红方块外扩裁片 → top1=icon bbox%s cos=%.3f（>%s 线）；top5 齐返回"
           % (top.get("bbox"), top.get("cos", -1), 0.7))

        # ── 用例5 domain 过滤 ──
        # 不存在域须环境无关（原用"计算器"——真库回填后恰有该域，负向断言失效）
        命中_他域 = bookvec.以图查库(crop, domain="不存在的域XYZ", limit=5)
        ok = len(命中_他域) == 0
        记("5 domain 过滤", ok,
           "domain='不存在的域XYZ' → 0 命中（域子串过滤在向量行生效）")

        # ── 用例6 开关关闭后向量库零增长 ──
        os.environ.pop("BOOKVEC", None)
        行前 = bookvec.库况()["rows"]
        _入库(img)
        行后 = bookvec.库况()["rows"]
        记("6 开关关闭零增长", 行前 == 行后,
           "BOOKVEC 撤销后再入库：向量库 %d→%d 行（零增长）" % (行前, 行后))
    finally:
        # 清场：靶域整删 + 向量库还原（纪律22/23）
        shutil.rmtree(域目录, ignore_errors=True)
        if 有原库:
            os.replace(npz备份, bookvec.向量文件)
            os.replace(meta备份, bookvec.元数据文件)
        else:
            for f in (bookvec.向量文件, bookvec.元数据文件):
                try:
                    os.remove(f)
                except OSError:
                    pass
        os.environ.pop("BOOKVEC", None)
        booklib.清扫(域名=靶域, 干跑=False, 标记="bookvec_test") if os.path.isdir(
            os.path.join(booklib.库根, 靶域)) else None

    n_ok = sum(1 for _u, ok, _s in 结果 if ok)
    print("----\n%d/%d PASS" % (n_ok, len(结果)))
    return 0 if n_ok == len(结果) else 1


if __name__ == "__main__":
    sys.exit(main())
