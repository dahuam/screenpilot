# -*- coding: utf-8 -*-
"""两级视觉与文本通道 验收脚本（2026-10-28 设计方案施工单④）。

靶=计算器（铁律15 固定自测靶；Win11 用 os.startfile('calculator:') 拉起，
铁律30 交接单坑）。CLI 子进程端到端（与 MCP 共用 sp 管线，同套逻辑）。

用例清单：
  1  scan 自动入库：meta.book 摘要 + manual/<计算器>/entries.json 有 button 条目
  2  find 文本检索（跨进程）：按名 "7" 命中且带 bbox
  3  查库直达点击：find bbox 中心 clickxy 物理 → 重扫显示区 0→7 业务生效
  4  crop_screen：裁显示区元素 → 原分辨率图逐字可读（图落验收目录人工复核）
  5  crop_xy：坐标区域裁片成功
  6  超龄自动重抓：epoch 回拨 120s → crop used=refreshed
  7  snap 后 cropxy（无元素表场景）：snap 登记 + cropxy 走缓存
  8  MCP 工具面函数级自测：crop_screen/crop_xy/book_find 直调（MCP server
     新工具面下个会话生效，函数级先验逻辑）

用法：python _test_twovision.py
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

根 = os.path.dirname(os.path.abspath(__file__))
验收目录 = os.path.join(根, "验收_20261028_两级视觉")
os.makedirs(验收目录, exist_ok=True)
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


def 找显示区(元素表):
    """计算器显示区：text 含数字的最大面积元素（或 UIA「显示是」文本）。"""
    最佳 = None
    for e in 元素表:
        t = e.get("text") or ""
        if ("显示" in t) or any(ch.isdigit() for ch in t):
            l, tp, r, b = e["bbox"]
            面积 = (r - l) * (b - tp)
            if 最佳 is None or 面积 > 最佳[0]:
                最佳 = (面积, e)
    return 最佳[1] if 最佳 else None


def main():
    import ctypes
    # ── 搭台：拉计算器（Win11：explorer calc: 无效，os.startfile 有效）
    subprocess.run(["taskkill", "/f", "/im", "CalculatorApp.exe"],
                   capture_output=True)
    time.sleep(1)
    os.startfile("calculator:")
    time.sleep(5)

    from sp import uia_engine
    from sp import enable_dpi_awareness
    enable_dpi_awareness()
    wc = uia_engine.find_window_control("计算器")
    hwnd = int(wc.NativeWindowHandle or 0)
    记("搭台", bool(hwnd), "计算器 hwnd=%s 标题=%s" % (hwnd, wc.Name))

    # ── 用例1：scan 自动入库
    r = cli("scan", "--win", "计算器")
    book = (r.get("meta") or {}).get("book") or {}
    库文件 = os.path.join(根, "manual", book.get("domain", "_"), "entries.json")
    有按钮 = False
    if os.path.isfile(库文件):
        with open(库文件, "r", encoding="utf-8") as f:
            条目 = json.load(f).get("entries", [])
        有按钮 = any(e.get("type") == "button" for e in 条目)
    记("1 scan自动入库", bool(book) and os.path.isfile(库文件) and 有按钮,
       "meta.book=%s 库文件=%s 含button=%s" % (book, os.path.isfile(库文件), 有按钮))

    # ── 用例2：find 文本检索（CLI 新进程=跨进程命中）
    # Win11 中文计算器数字按钮 UIA 名=「七」（首跑实锤），阿拉伯「7」是屏幕
    # 文本条目——两路都验：语义按钮名 + 文本条目
    r = cli("find", "七", "--domain", book.get("domain", "计算器"))
    条目s = r.get("entries") or []
    # stale 过滤（2026-11-01）：书库 v2 空间桶下 find('七') 返回多条候选，
    # 窗口改过尺寸/位置后旧条目 stale=true 坐标不可信（点中计算器空白处，
    # hit 窗对而业务零变化）——查库直达点击只认 stale=false 条目
    btn7 = next((e for e in 条目s
                 if e.get("type") == "button" and e.get("stale") is False), None)
    r文本 = cli("find", "7", "--domain", book.get("domain", "计算器"))
    文本条目 = r文本.get("entries") or []
    记("2 find跨进程检索", bool(btn7) and bool(文本条目),
       "find('七')命中%d条 button七=%s；find('7')命中%d条（text 层）"
       % (len(条目s), (btn7 or {}).get("bbox"), len(文本条目)))

    # ── 用例3：查库直达点击（button「七」bbox 中心物理点击 → 显示 0→7）
    if btn7:
        l, t, r2, b2 = btn7["bbox"]
        cx, cy = (l + r2) // 2, (t + b2) // 2
        r点击 = cli("clickxy", str(cx), str(cy), "--top-must", "计算器")
        time.sleep(1)
        # 复扫强制全量（2026-11-01）：点击后紧接的 scan 走 60s 同窗增量，
        # 显示区 OCR 复用上帧 → 仍显 0 判假（与 book_v2 例1/book_clean N1
        # 同根因同修法：动作后验证必须全量帧）。
        r复扫 = cli("scan", "--win", "计算器", "--full")
        显示 = 找显示区(r复扫.get("elements") or [])
        显示文本 = (显示 or {}).get("text", "")
        记("3 查库直达点击", (r点击.get("ok") is True) and "7" in 显示文本,
           "clickxy ok=%s hit=%s 显示区=%r" % (r点击.get("ok"),
                                              (r点击.get("hit_window") or {}).get("title"), 显示文本))
    else:
        记("3 查库直达点击", False, "无 button七 条目可点")

    # ── 用例4：crop_screen 显示区元素（原分辨率逐字可读）
    r = cli("scan", "--win", "计算器")
    显示 = 找显示区(r.get("elements") or [])
    if 显示:
        出图 = os.path.join(验收目录, "验收4_crop显示区.png")
        rc = cli("crop", str(显示["id"]), "--pad", "6", "--out", 出图)
        ok = rc.get("ok") is True and os.path.isfile(出图)
        尺寸 = rc.get("crop_size")
        记("4 crop_screen裁片", ok, "元素id=%s text=%r 尺寸=%s used=%s" %
           (显示["id"], (显示.get("text") or "")[:20], 尺寸, rc.get("used")))
    else:
        记("4 crop_screen裁片", False, "未找到显示区元素")

    # ── 用例5：crop_xy 坐标区域裁片
    if 显示:
        l, t, r2, b2 = 显示["bbox"]
        出图 = os.path.join(验收目录, "验收5_cropxy显示区.png")
        rc = cli("cropxy", str(l), str(t), str(r2), str(b2), "--out", 出图)
        记("5 crop_xy区域裁片", rc.get("ok") is True and os.path.isfile(出图),
           "尺寸=%s used=%s" % (rc.get("crop_size"), rc.get("used")))
    else:
        记("5 crop_xy区域裁片", False, "无显示区坐标")

    # ── 用例6：超龄自动重抓（epoch 回拨 120s → refreshed）
    from sp import lastshot
    状态 = lastshot.status()
    if 状态:
        状态["epoch"] = float(状态.get("epoch", time.time())) - 120
        with open(lastshot.状态路径, "w", encoding="utf-8") as f:
            json.dump(状态, f, ensure_ascii=False)
        出图 = os.path.join(验收目录, "验收6_超龄重抓.png")
        rc = cli("cropxy", "0", "0", "200", "200", "--out", 出图)
        记("6 超龄自动重抓", rc.get("ok") is True and rc.get("used") == "refreshed",
           "used=%s（期望 refreshed）" % rc.get("used"))
    else:
        记("6 超龄自动重抓", False, "无 lastshot 状态")

    # ── 用例7：snap 后 cropxy（snap 也登记缓存，无元素表场景）
    rs = cli("snap", os.path.join(验收目录, "验收7_snap.png"), "--win", "计算器", "--bg")
    rc = cli("cropxy", "0", "0", "150", "80",
             "--out", os.path.join(验收目录, "验收7_snap后cropxy.png"))
    记("7 snap后cropxy", rs.get("ok") is True and rc.get("ok") is True,
       "snap bg_fallback=%s crop used=%s" % (rs.get("bg_fallback"), rc.get("used")))

    # ── 用例8：MCP 工具面函数级自测（直调 server 函数逻辑）
    try:
        from sp import booklib as bl
        域 = bl.域清单()
        命中 = bl.查找(query="7")
        记("8 书库接口函数级", bool(域) and bool(命中),
           "域清单=%s find('7')=%d 条" % ([d["domain"] for d in 域], len(命中)))
    except Exception as e:
        记("8 书库接口函数级", False, str(e))

    # ── 收尾
    subprocess.run(["taskkill", "/f", "/im", "CalculatorApp.exe"], capture_output=True)

    过 = sum(1 for _, ok, _ in 结果 if ok)
    print("\n===== 验收 %d/%d =====" % (过, len(结果)))
    return 0 if 过 == len(结果) else 1


if __name__ == "__main__":
    sys.exit(main())
