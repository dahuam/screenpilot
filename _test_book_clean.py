# -*- coding: utf-8 -*-
"""书库收尾验收脚本（2026-10-29 第二单「识屏书库收尾」）。

对应任务书 3.3（legacy 归档）与 4.3（空间桶细化）全部验收条目
（第 3.3.5/4.3.4 回归三套另跑 _test_book_v2/_test_twovision/_test_stack）：

  U1 归档/恢复单元闭环（monkeypatch 临时库，不动真库）：归档→恢复→归档守恒
  N1 近距同名真靶（4.3.1）：双「确定」垂直 ~110px 同桶 → find 2 候选、
     pos_hint 互异、各点各中
  N2 幂等（4.3.2）：同界面连扫三次靶域条目数不涨
  N3 库容（4.3.3）：靶域条目数增幅 < 2 倍
  A1 计算器重扫即归档（3.3.1）：scan 一轮 → find 无 legacy、域 legacy=0
  A2 归档恢复闭环真库版（3.3.4）：恢复→legacy 回来→再归档→守恒
  A3 全库清理（3.3.2+3.3.3）：bookarchive 全库 → 全库 legacy=0、
     归档目录可查、主库+归档=归档前总数

靶纪律：计算器 + tk 自造靶（铁律15）；全程单进程串行（坑22）；计算器用完即关。
用法：python _test_book_clean.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

根 = os.path.dirname(os.path.abspath(__file__))
CLI = [sys.executable, os.path.join(根, "screenpilot.py")]
结果 = []


def 记(用例, ok, 说明):
    结果.append((用例, ok, 说明))
    print(("PASS" if ok else "FAIL") + " | " + 用例 + " | " + 说明)


def cli(*参数):
    p = subprocess.run(CLI + list(参数), capture_output=True, timeout=180)
    out = p.stdout.decode("utf-8", "replace").strip()
    try:
        return json.loads(out.splitlines()[-1])
    except Exception:
        return {"_raw": out, "_err": p.stderr.decode("utf-8", "replace")[-300:]}


def 全库盘点():
    """主库各域 {legacy, v2, total} 汇总。"""
    from sp import booklib
    legacy = v2 = 0
    for d in booklib.域清单():
        p = os.path.join(booklib.库根, d["domain"], "entries.json")
        with open(p, "r", encoding="utf-8") as f:
            es = json.load(f)["entries"]
        legacy += sum(1 for e in es if not e.get("rel_bbox"))
        v2 += sum(1 for e in es if e.get("rel_bbox"))
    return {"legacy": legacy, "v2": v2, "total": legacy + v2}


def 归档盘点():
    from sp import booklib
    return sum(d["archived"] for d in booklib.归档清单())


def main():
    from sp import booklib

    # ══ U1：归档/恢复单元闭环（临时库，monkeypatch 根路径）
    tmp = tempfile.mkdtemp(prefix="_bk_clean_")
    真库根, 真归档根 = booklib.库根, booklib.归档根
    try:
        booklib.库根 = os.path.join(tmp, "manual")
        booklib.归档根 = os.path.join(tmp, "manual_archive")
        os.makedirs(booklib.库根)
        # 造 mini 域：2 条 legacy（无 rel_bbox）+ 1 条 v2
        域数据 = {"domain": "样例域", "schema": 2, "updated": "", "entries": [
            {"name": "旧按钮", "type": "button", "bbox": [10, 20, 60, 50]},
            {"name": "旧二", "type": "edit", "bbox": [5, 5, 40, 30]},
            {"name": "新按钮", "type": "button", "bbox": [100, 100, 150, 130],
             "rel_bbox": [100, 100, 150, 130], "win_size": [800, 600],
             "win_rect": [0, 0, 800, 600], "bucket": [1, 1], "hwnd": 0},
        ]}
        os.makedirs(os.path.join(booklib.库根, "样例域"))
        with open(os.path.join(booklib.库根, "样例域", "entries.json"),
                  "w", encoding="utf-8") as f:
            json.dump(域数据, f, ensure_ascii=False)
        s1 = booklib.归档域("样例")
        主1 = len(booklib._读域("样例域")["entries"])
        归1 = len(booklib._读归档("样例域")["entries"])
        r1 = booklib.恢复归档("样例")
        主2 = len(booklib._读域("样例域")["entries"])
        归2 = len(booklib._读归档("样例域")["entries"])
        s2 = booklib.归档域("样例")
        主3 = len(booklib._读域("样例域")["entries"])
        归3 = len(booklib._读归档("样例域")["entries"])
        闭环 = (s1["样例域"]["archived"] == 2 and 主1 == 1 and 归1 == 2
               and r1["样例域"]["restored"] == 2 and 主2 == 3 and 归2 == 0
               and s2["样例域"]["archived"] == 2 and 主3 == 1 and 归3 == 2)
        记("U1 归档恢复单元闭环", 闭环,
           "归档%s 主%d/归%d → 恢复%s 主%d/归%d → 再归档 主%d/归%d（守恒=3）"
           % (s1["样例域"]["archived"], 主1, 归1, r1["样例域"]["restored"],
              主2, 归2, 主3, 归3))
    finally:
        booklib.库根, booklib.归档根 = 真库根, 真归档根
        shutil.rmtree(tmp, ignore_errors=True)

    # 基线（真库）
    基线 = 全库盘点()
    print("基线：主库 total=%d legacy=%d v2=%d" % (
        基线["total"], 基线["legacy"], 基线["v2"]))

    # ══ N1/N2/N3：近距同名真靶（tk 双「确定」垂直 ~110px）
    靶 = subprocess.Popen([sys.executable, os.path.join(根, "_tk_near_dual.py")])
    try:
        # 条件就绪替代固定等待（2026-11-01，同 _test_book_v2 例 1）：靶渲染
        # 慢半拍时 OCR 时刻漏检第二个同名按钮 → 候选<2 假失败；重扫至双候选。
        s, book, q = {}, {}, {}
        for _ in range(5):
            time.sleep(2)
            s = cli("scan", "--win", "近距消歧靶")
            book = (s.get("meta") or {}).get("book") or {}
            q = cli("find", "确定", "--domain", "近距消歧靶")
            if len([e for e in (q.get("entries") or [])
                    if e.get("stale") is False]) >= 2:
                break
        候选 = [e for e in (q.get("entries") or []) if e.get("stale") is False]
        两候选 = len(候选) == 2
        同桶 = 两候选 and 候选[0].get("bucket") == 候选[1].get("bucket")
        互异 = 两候选 and 候选[0].get("pos_hint") != 候选[1].get("pos_hint")
        垂距 = (abs(候选[1]["bbox"][1] - 候选[0]["bbox"][1])
               if 两候选 else 9999)
        近距 = 两候选 and 垂距 < 150
        # 各点一次：按 y 排序先上后下；重扫看「上已点」「下已点」文本
        全中 = True
        细节 = []
        if 两候选:
            for 标记 in ("上", "下"):
                组 = sorted(候选, key=lambda e: e["bbox"][1])
                目标 = 组[0] if 标记 == "上" else 组[-1]
                l, t, rr, bb = 目标["bbox"]
                rc = cli("clickxy", str((l + rr) // 2), str((t + bb) // 2),
                         "--top-must", "近距消歧靶")
                time.sleep(1.2)
                # 复扫强制全量（2026-11-01）：点击后紧接的 scan 落在 60s 同窗
                # 增量窗口内，UIA 复用上帧快照 → 按钮文本仍显旧「确定」，
                # 「上已点/下已点」判假（与 _test_book_v2 例1 同根因同修法）。
                复扫 = cli("scan", "--win", "近距消歧靶", "--full")
                命中文本 = any(标记 in (e.get("text") or "")
                              for e in (复扫.get("elements") or []))
                全中 = 全中 and rc.get("ok") is True and 命中文本
                细节.append("%s=%s" % (标记, 命中文本))
        记("N1 近距同名双候选各点各中", 两候选 and 同桶 and 互异 and 近距 and 全中,
           "候选=%d 同桶=%s pos_hint互异=%s 垂直距=%dpx 各点[%s]"
           % (len(候选), 同桶, 互异, 垂距, " ".join(细节)))

        # N2 幂等：界面已变（按钮文本被点过）→ 先以当前界面为基准连扫三次
        s_a = cli("scan", "--win", "近距消歧靶")
        n_a = ((s_a.get("meta") or {}).get("book") or {}).get("total") or 0
        稳 = [n_a]
        for _ in range(2):
            time.sleep(0.8)
            s_b = cli("scan", "--win", "近距消歧靶")
            稳.append(((s_b.get("meta") or {}).get("book") or {}).get("total") or -1)
        记("N2 同界面连扫三次幂等", 稳[0] == 稳[1] == 稳[2] and 稳[0] > 0,
           "三次域条目数=%s（不涨即幂等）" % 稳)

        # N3 库容：靶域条目数 vs 最近一次 scan 入库元素数，增幅 < 2 倍
        元素数 = ((s_b.get("meta") or {}).get("counts") or {}).get("total") or 0
        库容OK = 稳[-1] > 0 and 元素数 > 0 and 稳[-1] <= 元素数 * 2
        记("N3 单域库容增幅<2x", 库容OK,
           "域条目=%d，单次 scan 元素=%d（倍率 %.2f）"
           % (稳[-1], 元素数, 稳[-1] / max(1, 元素数)))
    finally:
        靶.terminate()
        time.sleep(0.5)

    # ══ A1：计算器重扫即归档（3.3.1）
    # 复用已开的计算器；没有才拉起并循环等「七」就位（空壳坑，见 _test_book_v2）
    def _找七(扫描结果):
        return next((e for e in (扫描结果.get("elements") or [])
                     if e.get("text") == "七" and e.get("type") == "button"), None)

    def _域legacy(域名):
        return sum(1 for e in booklib._读域(域名)["entries"]
                   if not e.get("rel_bbox"))
    # 幂等前置（10-29 拍板）：legacy 已清零的库上重跑时，先从归档恢复造出前置，
    # 测试自给自足可重跑；A2 归档收尾，终态仍是 legacy=0。
    if _域legacy("计算器") == 0:
        cli("bookarchive", "--restore", "--domain", "计算器")
    计器前 = next((d for d in booklib.域清单() if d["domain"] == "计算器"), {})
    计器前legacy = _域legacy("计算器")
    r1 = cli("scan", "--win", "计算器")
    f1 = _找七(r1)
    if f1 is None:
        subprocess.run(["taskkill", "/f", "/im", "CalculatorApp.exe"],
                       capture_output=True)
        time.sleep(2)
        os.startfile("calculator:")
        for _ in range(10):
            time.sleep(6)
            r1 = cli("scan", "--win", "计算器")
            f1 = _找七(r1)
            if f1:
                break
    if f1:
        b = (r1.get("meta") or {}).get("book") or {}
        归档数 = b.get("archived") or 0
        q = cli("find", "七", "--domain", "计算器")
        无legacy = all(e.get("rel_bbox") for e in (q.get("entries") or []))
        计器后 = next((d for d in booklib.域清单() if d["domain"] == "计算器"), {})
        记("A1 计算器重扫即归档",
           归档数 == 计器前legacy and 计器前legacy > 0
           and 无legacy and q.get("legacy_excluded") == 0
           and _域legacy("计算器") == 0,
           "域内原 legacy=%d → 本轮归档=%d → 域 legacy=0；find(七) "
           "legacy_excluded=%s 全v2=%s 域条目 %s→%s"
           % (计器前legacy, 归档数, q.get("legacy_excluded"), 无legacy,
              计器前.get("total"), 计器后.get("total")))
    else:
        记("A1 计算器重扫即归档", False, "计算器/「七」按钮不可得")

    # ══ A2：归档恢复闭环真库版（3.3.4，计算器域）
    # 注意 --domain 只动计算器域，其他域 legacy 不受影响（全库清零是 A3 的事）；
    # 守恒式=「主库+归档」总和（恢复是往主库 +119、归档侧 -119，主库单边必变）。
    # 无需再造前置：A1 造的前置被 A1 归档回归档侧，此处 restore 正好有账可还。
    主前 = 全库盘点()
    总前 = 主前["total"] + 归档盘点()
    rc1 = cli("bookarchive", "--restore", "--domain", "计算器")
    恢后legacy = _域legacy("计算器")
    恢后 = 全库盘点()
    总恢 = 恢后["total"] + 归档盘点()
    rc2 = cli("bookarchive", "--domain", "计算器")
    归后legacy = _域legacy("计算器")
    归后 = 全库盘点()
    总归 = 归后["total"] + 归档盘点()
    守恒 = (总前 == 总恢 == 总归)
    记("A2 归档→恢复→再归档闭环（计算器域）",
       rc1.get("ok") and rc2.get("ok")
       and 计器前legacy > 0 and 恢后legacy == 计器前legacy
       and 归后legacy == 0 and 主前["total"] == 归后["total"] and 守恒,
       "计算器 legacy %d→恢复%d→归档%d；主库 %d→%d→%d；主库+归档和 %d 恒=%s"
       % (计器前legacy, 恢后legacy, 归后legacy, 主前["total"],
          恢后["total"], 归后["total"], 总前, 守恒))

    # ══ A3：全库清理（3.3.2 + 3.3.3）
    # 守恒口径（10-29 拍板改写）：bookarchive 只是移动，断言「归档动作前后主库+归档总量不变」；
    # 原式以「归档前主库含 legacy」为前提，在已清零库上二跑必口径失配。
    归档前总 = 全库盘点()["total"] + 归档盘点()
    rc3 = cli("bookarchive")
    终态 = 全库盘点()
    归档总 = 归档盘点()
    五域 = {d["domain"] for d in booklib.归档清单()}
    期望域 = {"计算器", "雷电模拟器", "YouTube", "百度一下，你就知道", "Microsoft​ Edge"}
    全零 = 终态["legacy"] == 0
    守恒全 = 归档前总 == (终态["total"] + 归档总)
    记("A3 全库归档终态", rc3.get("ok") and 全零 and 期望域 <= 五域 and 守恒全,
       "全库 legacy=%d 归档目录=%d 条/域=%s；归档前后总量 %d→%d 守恒=%s"
       % (终态["legacy"], 归档总, sorted(五域), 归档前总,
          终态["total"] + 归档总, 守恒全))

    # 收尾：计算器关闭
    subprocess.run(["taskkill", "/f", "/im", "CalculatorApp.exe"],
                   capture_output=True)

    过 = sum(1 for _, ok, _ in 结果 if ok)
    print("\n===== 书库收尾验收 %d/%d =====" % (过, len(结果)))
    return 0 if 过 == len(结果) else 1


if __name__ == "__main__":
    sys.exit(main())
