# -*- coding: utf-8 -*-
"""F1 书库生命周期批验收脚本（2026-11-01：backup-gc + TTL 消费）。

纯单元（monkeypatch 临时目录，不动真库不开 GUI），与 _test_book_clean U1 同法：

  GC-U1 backup-gc 干跑清单正确（应删=超龄份+每域第 3 份起；无时间份/配额内不动）
  GC-U2 backup-gc 真跑：每域≤2 份、超龄份清、无时间份保留、空份目录消失
  TTL-U1 _条目过期 判定：None/未来/坏格式=不过期，过去=过期
  TTL-U2 find 索引路径过滤到期条目（expired_excluded 如实计数）
  TTL-U3 find 全扫路径（BOOKLIB_INDEX=0）同样过滤
  TTL-U4 清扫过期模式：干跑出账；真跑条目清、备份在、活条目无损
  TTL-U5 库概况每域 expired 计数

用法：python _test_book_gc.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

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


def _造份(项目根, 标记, 域条目们):
    """造一份备份：manual_backup_<标记>/<域>/entries.json。返回份路径。"""
    份 = os.path.join(项目根, "manual_backup_%s" % 标记)
    for 域, 条目数 in 域条目们.items():
        d = os.path.join(份, 域)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "entries.json"), "w", encoding="utf-8") as f:
            json.dump({"domain": 域, "entries": [{"n": i} for i in range(条目数)]},
                      f, ensure_ascii=False)
    return 份


def _造域(库根, 域名, 条目们):
    """造主库域：条目们=[{name, expires_at}]（v2 简形，带 rel_bbox 免 legacy 分支）。"""
    d = os.path.join(库根, 域名)
    os.makedirs(d, exist_ok=True)
    es = []
    for it in 条目们:
        e = {"name": it["name"], "type": "button", "bbox": [10, 10, 40, 30],
             "rel_bbox": [10, 10, 40, 30], "win_size": [800, 600],
             "win_rect": [0, 0, 800, 600], "bucket": [0, 0], "hwnd": 0,
             "crop": None, "time": "2026-11-01 00:00:00"}
        if "expires_at" in it:
            e["expires_at"] = it["expires_at"]
        es.append(e)
    with open(os.path.join(d, "entries.json"), "w", encoding="utf-8") as f:
        json.dump({"domain": 域名, "schema": 2, "updated": "", "entries": es},
                  f, ensure_ascii=False)


def main():
    from sp import booklib, bookindex

    # ══ TTL-U1：_条目过期 判定 ══
    ok = (not booklib._条目过期({"expires_at": None})
          and not booklib._条目过期({"expires_at": ""})
          and not booklib._条目过期({"expires_at": "2099-01-01 00:00:00"})
          and not booklib._条目过期({"expires_at": "不是时间"})
          and not booklib._条目过期({})
          and booklib._条目过期({"expires_at": "2026-01-01 00:00:00"})
          and booklib._条目过期({"expires_at": "2020-12-31 23:59:59"}))
    记("TTL-U1 _条目过期判定", ok, "None/空/未来/坏格式/缺键=不过期；过去时间=过期")

    tmp = tempfile.mkdtemp(prefix="_bk_gc_")
    真库根, 真归档根, 真项目根 = booklib.库根, booklib.归档根, booklib.项目根
    真索引根 = bookindex.索引根
    try:
        项目根 = os.path.join(tmp, "proj")
        os.makedirs(项目根)
        booklib.项目根 = 项目根
        booklib.库根 = os.path.join(项目根, "manual")
        booklib.归档根 = os.path.join(项目根, "manual_archive")
        bookindex.索引根 = os.path.join(booklib.库根, "_index")
        os.makedirs(booklib.库根)

        # 造备份份：超龄份（61 天前）+ 近龄 4 份（每域>2 场景）+ 无时间份
        _造份(项目根, "20260901_000000", {"旧域": 3, "计算器": 2})          # 超龄，整份删
        _造份(项目根, "20261025_080000", {"计算器": 2, "样例": 1})          # 计算器第 3 份 → 域目录删
        _造份(项目根, "20261030_090000", {"计算器": 2, "样例": 1})          # 计算器第 2 份 → 留
        _造份(项目根, "20261031_100000", {"计算器": 2, "样例": 1})          # 计算器最新 → 留
        _造份(项目根, "乱七八糟名", {"怪域": 1})                             # 解析不出 → 不动
        份数前 = len([d for d in os.listdir(项目根) if d.startswith("manual_backup_")])

        # ══ GC-U1：干跑清单正确 ══
        r = booklib.备份回收(干跑=True)
        删路径 = {os.path.relpath(x["path"], 项目根) for x in r["would_remove"]}
        应删 = {"manual_backup_20260901_000000",                        # 超龄整份
               os.path.join("manual_backup_20261025_080000", "计算器"),  # 计算器第 3 份
               os.path.join("manual_backup_20261025_080000", "样例")}    # 样例第 3 份
        无时间份在 = os.path.isdir(os.path.join(项目根, "manual_backup_乱七八糟名"))
        主库没动 = os.path.isdir(booklib.库根)
        ok = (r["dry_run"] and 删路径 == 应删 and 无时间份在
              and r["would_free_bytes"] > 0 and 主库没动
              and all(x.get("age_days") is not None for x in r["would_remove"]))
        记("GC-U1 干跑清单正确", ok,
           "应删%s 实删%s；无时间份保留=%s；dry_run=%s 释放=%dB"
           % (sorted(应删), sorted(删路径), 无时间份在, r["dry_run"],
              r["would_free_bytes"]))

        # ══ GC-U2：真跑 ══
        r2 = booklib.备份回收(干跑=False)
        剩份 = sorted(d for d in os.listdir(项目根) if d.startswith("manual_backup_"))
        每域份数 = {}
        for 份 in 剩份:
            for 域 in os.listdir(os.path.join(项目根, 份)):
                每域份数[域] = 每域份数.get(域, 0) + 1
        ok = ("manual_backup_20260901_000000" not in 剩份            # 超龄没了
              and not os.path.isdir(os.path.join(项目根, "manual_backup_20261025_080000",
                                                 "计算器"))        # 第 3 份没了
              and 每域份数.get("计算器") == 2                        # 每域≤2
              and 每域份数.get("怪域") == 1                          # 无时间份保留
              and r2["freed_bytes"] > 0)
        记("GC-U2 真跑每域≤2+超龄清+无时间份留", ok,
           "剩份=%s 每域=%s 释放=%dB" % (剩份, 每域份数, r2["freed_bytes"]))

        # ══ TTL-U2~U5：临时库造「1 活 + 1 到期」域 ══
        _造域(booklib.库根, "样例域", [
            {"name": "活按钮"},
            {"name": "废按钮", "expires_at": "2026-01-01 00:00:00"},
        ])
        bookindex._索引缓存, bookindex._索引缓存指纹 = None, ""
        bookindex._域缓存, bookindex._域缓存指纹 = {}, ""

        命中, 排除, 过期排除 = booklib.查找明细(query="按钮")
        ok = (len(命中) == 1 and 命中[0]["name"] == "活按钮"
              and 排除 == 0 and 过期排除 == 1)
        记("TTL-U2 find 索引路径过滤到期", ok,
           "命中=%d（只活按钮）legacy排除=%d 过期排除=%d"
           % (len(命中), 排除, 过期排除))

        os.environ["BOOKLIB_INDEX"] = "0"    # 强制全扫旧路径
        try:
            命中3, 排除3, 过期3 = booklib.查找明细(query="按钮")
        finally:
            os.environ.pop("BOOKLIB_INDEX", None)
        ok = (len(命中3) == 1 and 命中3[0]["name"] == "活按钮"
              and 排除3 == 0 and 过期3 == 1)
        记("TTL-U3 find 全扫路径同样过滤", ok,
           "命中=%d 过期排除=%d（索引路径与全扫路径行为一致）"
           % (len(命中3), 过期3))

        # TTL-U4：清扫过期模式（先干跑后真跑）
        rd = booklib.清扫(过期=True, 干跑=True)
        干跑删 = next((d["removed"] for d in rd["domains"] if d["domain"] == "样例域"), -1)
        库前 = len(booklib._读域("样例域")["entries"])
        rr = booklib.清扫(过期=True, 干跑=False)
        库后条目 = booklib._读域("样例域")["entries"]
        ok = (干跑删 == 1 and 库前 == 2 and len(库后条目) == 1
              and 库后条目[0]["name"] == "活按钮"
              and rr.get("backup") and os.path.isdir(rr["backup"]))
        记("TTL-U4 清扫过期模式干跑+真跑", ok,
           "干跑 removed=%d；库 %d→%d（剩活按钮）；备份在=%s"
           % (干跑删, 库前, len(库后条目), bool(rr.get("backup"))))

        # TTL-U5：库概况 expired 计数（再造 1 到期条目验计数）
        _造域(booklib.库根, "计数域", [
            {"name": "甲"},
            {"name": "乙", "expires_at": "2026-01-01 00:00:00"},
        ])
        概况 = booklib.库概况()
        行 = next((r for r in 概况["domains"] if r["domain"] == "计数域"), {})
        ok = 行.get("expired") == 1 and 概况.get("total_expired", 0) >= 1
        记("TTL-U5 库概况 expired 计数", ok,
           "计数域 expired=%s 全库 total_expired=%s"
           % (行.get("expired"), 概况.get("total_expired")))
    finally:
        booklib.库根, booklib.归档根, booklib.项目根 = 真库根, 真归档根, 真项目根
        bookindex.索引根 = 真索引根
        bookindex._索引缓存, bookindex._索引缓存指纹 = None, ""
        bookindex._域缓存, bookindex._域缓存指纹 = {}, ""
        shutil.rmtree(tmp, ignore_errors=True)

    过 = sum(1 for _, ok, _ in 结果 if ok)
    print("\n===== F1 生命周期批验收 %d/%d =====" % (过, len(结果)))
    return 0 if 过 == len(结果) else 1


if __name__ == "__main__":
    sys.exit(main())
