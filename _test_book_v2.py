# -*- coding: utf-8 -*-
"""书库数据模型 v2 验收脚本（2026-10-29「识屏三整改」施工单·施工一）。

对应任务书 3.3 五项验收（第 5 项回归另跑 _test_twovision/_test_stack）：
  1  同名消歧真靶：双「确定」tk 靶 → find 返回 2 候选各 bbox 正确、各点一次命中
  2  域漂移：Edge 同站点两个搜索词 → 该站点域仍只有 1 个（条目数增加）
  3  窗口移动：find「七」→ 移窗 200px → bbox 随动且点击业务生效；改尺寸 → stale=true
  4  旧库不丢：迁移后域条目数与 manual_backup_20261029 完全一致（565 条）
  5  域解析单元用例（url主机/解析域 纯逻辑，快速失败前置）

靶纪律：计算器 + tk 自造靶（铁律15）；Edge 用完即关（自开的进程树 taskkill /T）。
用法：python _test_book_v2.py
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse

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


def main():
    import ctypes
    from ctypes import wintypes

    from sp import enable_dpi_awareness, uia_engine
    enable_dpi_awareness()

    user32 = ctypes.windll.user32

    def 窗口rect(hwnd):
        rc = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rc))
        return [rc.left, rc.top, rc.right, rc.bottom]

    def 移窗(hwnd, l, t, w, h):
        user32.MoveWindow(hwnd, l, t, w, h, True)
        time.sleep(0.6)

    # ══ 用例5（前置快检）：域解析单元逻辑
    from sp import booklib
    ok5 = (booklib.url主机("https://www.baidu.com/s?wd=x") == "baidu.com"
           and booklib.url主机("https://www.youtube.com/watch?v=1") == "youtube.com"
           and booklib.解析域("天气_百度搜索 - Microsoft Edge",
                          "https://www.baidu.com/s?wd=x") == "baidu.com"
           and booklib.解析域("随便逛逛 - Microsoft Edge") == "web_随便逛逛"
           and booklib.解析域("文档 - 记事本") == "记事本"
           and booklib.解析域("无标签标题") == "无标签标题")
    记("5 域解析单元逻辑", ok5,
       "url主机/解析域 六断言（浏览器host优先/web_退化/非浏览器末段/唯一段）")

    # ══ 用例4：旧库不丢（迁移后 vs 备份）
    # 备份份是可回收资源（2026-11-01 F1 backup-gc：每域留 2 份+超龄清）——
    # 本用例不硬依赖它：份在=照常校验「备份各域只增不丢」；份被 GC=该断言
    # 退化跳过，「恰 565」守恒断言（主库+归档口径，不读备份）仍全程在岗。
    before备份 = {}
    备份根 = os.path.join(根, "manual_backup_20261029")
    备份在 = os.path.isdir(备份根)
    if 备份在:
        for d in os.listdir(备份根):
            p = os.path.join(备份根, d, "entries.json")
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    before备份[d] = len(json.load(f)["entries"])
    现库 = {d["domain"]: d["total"] for d in booklib.域清单()}
    # 备份各域只增不丢；全库 legacy 条目数必须恰 565。2026-10-29 第二单
    # 「书库收尾」归档后口径更新：legacy 移出主库进 manual_archive/（移动
    # 不是删除），「不丢」与守恒都按 主库+归档 合计口径（全程守恒）
    归档根 = os.path.join(根, "manual_archive")
    归档域数 = {}
    if os.path.isdir(归档根):
        for d in os.listdir(归档根):
            p = os.path.join(归档根, d, "entries.json")
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    归档域数[d] = len(json.load(f)["entries"])
    现库合计 = dict(现库)
    for k, v in 归档域数.items():
        现库合计[k] = 现库合计.get(k, 0) + v
    不丢 = (not 备份在) or all(现库合计.get(k, 0) >= v for k, v in before备份.items())
    legacy数 = 0
    for d in os.listdir(os.path.join(根, "manual")):
        p = os.path.join(根, "manual", d, "entries.json")
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                legacy数 += sum(1 for e in json.load(f)["entries"]
                                if not e.get("rel_bbox"))
    归档数 = sum(归档域数.values())
    记("4 旧库不丢", 不丢 and legacy数 + 归档数 == 565,
       "备份 565 条全部在库=%s（主库+归档合计口径，备份在=%s），主库 legacy=%d + 归档=%d = %d（应恰 565）"
       % (不丢, 备份在, legacy数, 归档数, legacy数 + 归档数))

    # ══ 用例3：窗口移动（计算器）
    # 复用已开的计算器；没有才拉起并等「七」就位。坑（本轮实测）：taskkill
    # 强杀 UWP 后立即 startfile，本机雷电挂机负载下会起成空壳（标题栏在而
    # 数字键盘区不挂 UIA 树）甚至起不来——必须循环等待真实就位。
    def _找七(扫描结果):
        # CLI scan 输出的元素表键是 elements（find 的输出键才是 entries）
        return next((e for e in (扫描结果.get("elements") or [])
                     if e.get("text") == "七" and e.get("type") == "button"), None)
    r1 = cli("scan", "--win", "计算器")
    f1 = _找七(r1)
    if f1 is None:
        subprocess.run(["taskkill", "/f", "/im", "CalculatorApp.exe"],
                       capture_output=True)
        time.sleep(2)
        os.startfile("calculator:")
        for _ in range(10):   # 最多再等 ~60s 到数字键盘区挂上 UIA 树
            time.sleep(6)
            r1 = cli("scan", "--win", "计算器")
            f1 = _找七(r1)
            if f1:
                break
    wc = uia_engine.find_window_control("计算器")
    hwnd = int(wc.NativeWindowHandle or 0)
    if f1 and hwnd:
        rect0 = 窗口rect(hwnd)
        # 移窗 +200px 横向
        移窗(hwnd, rect0[0] + 200, rect0[1],
             rect0[2] - rect0[0], rect0[3] - rect0[1])
        q = cli("find", "七", "--domain", "计算器")
        v2条目 = [e for e in (q.get("entries") or [])
                  if e.get("stale") is False and not e.get("legacy")
                  and e.get("type") == "button"]
        随动 = bool(v2条目) and abs(v2条目[0]["bbox"][0] - f1["bbox"][0] - 200) <= 2 \
            and abs(v2条目[0]["bbox"][1] - f1["bbox"][1]) <= 2
        # 按随动坐标点击 → 显示区 0→7 业务生效
        点了 = 命中 = False
        if v2条目:
            l, t, rr, bb = v2条目[0]["bbox"]
            rc = cli("clickxy", str((l + rr) // 2), str((t + bb) // 2),
                     "--top-must", "计算器")
            点了 = rc.get("ok") is True
            time.sleep(1)
            r复扫 = cli("scan", "--win", "计算器")
            命中 = any("7" in (e.get("text") or "")
                       and "显示" in (e.get("text") or "")
                       for e in (r复扫.get("elements") or []))
        # 改窗口尺寸 → stale=true
        移窗(hwnd, rect0[0] + 200, rect0[1],
             rect0[2] - rect0[0] + 160, rect0[3] - rect0[1])
        q2 = cli("find", "七", "--domain", "计算器")
        变宽 = [e for e in (q2.get("entries") or [])
               if not e.get("legacy") and e.get("type") == "button"]
        staleOK = bool(变宽) and 变宽[0].get("stale") is True \
            and "尺寸" in (变宽[0].get("reason") or "")
        # 还原窗口
        移窗(hwnd, rect0[0], rect0[1], rect0[2] - rect0[0], rect0[3] - rect0[1])
        记("3 窗口移动随动+尺寸stale", 随动 and 点了 and 命中 and staleOK,
           "移窗bbox随动=%s 点击ok=%s 显示区7=%s 尺寸变化stale=%s（%s）"
           % (随动, 点了, 命中, staleOK, (变宽[0].get("reason") if 变宽 else "无条目")[:50]))
    else:
        记("3 窗口移动随动+尺寸stale", False, "计算器/「七」按钮不可得")

    # ══ 用例2：域漂移（Edge 同站点两个搜索词）
    edge路径s = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    edge = next((p for p in edge路径s if os.path.isfile(p)), None)
    if edge:
        dom清单1 = None
        procs = []
        try:
            u1 = "https://www.baidu.com/s?wd=" + urllib.parse.quote("天气")
            procs.append(subprocess.Popen([edge, "--new-window", u1]))
            time.sleep(8)
            # url_host 就绪重试（2026-11-01）：Edge 新窗 8s 固定等待在本机
            # 雷电挂机负载下可能页面未加载完，UIA 拿不到地址栏 host →
            # url_host=None 假失败；与例1 条件就绪同律。
            s1 = cli("scan", "--win", "百度搜索")
            m1 = s1.get("meta") or {}
            for _ in range(4):
                if m1.get("url_host") == "baidu.com":
                    break
                time.sleep(4)
                s1 = cli("scan", "--win", "百度搜索", "--full")
                m1 = s1.get("meta") or {}
            b1 = m1.get("book") or {}
            u2 = "https://www.baidu.com/s?wd=" + urllib.parse.quote("股票")
            procs.append(subprocess.Popen([edge, u2]))
            time.sleep(7)
            s2 = cli("scan", "--win", "百度搜索")
            m2 = s2.get("meta") or {}
            b2 = m2.get("book") or {}
            同域 = (m1.get("url_host") == "baidu.com"
                   and m2.get("url_host") == "baidu.com"
                   and b1.get("domain") == "baidu.com"
                   and b2.get("domain") == "baidu.com")
            唯一 = [d for d in booklib.域清单() if "baidu" in d["domain"].lower()]
            条目增 = (b2.get("total") or 0) >= (b1.get("total") or 0) and (b1.get("total") or 0) > 0
            记("2 域漂移同站点一域", 同域 and len(唯一) == 1 and 条目增,
               "url_host=%s→%s 域=%s→%s baidu域数=%d 条目 %s→%s"
               % (m1.get("url_host"), m2.get("url_host"),
                  b1.get("domain"), b2.get("domain"), len(唯一),
                  b1.get("total"), b2.get("total")))
        finally:
            for p in procs:
                try:
                    subprocess.run(["taskkill", "/f", "/t", "/pid", str(p.pid)],
                                   capture_output=True)
                except Exception:
                    pass
    else:
        记("2 域漂移同站点一域", False, "未找到 Edge 可执行文件")

    # ══ 用例1：同名消歧真靶（双「确定」tk 靶）
    # 前置卫生（2026-11-01 实测）：上一个进程的窗口句柄失效后，库里旧同名
    # 条目会以 stale=true「窗口已关闭」占位——find 只看 stale=false 候选，
    # 候选数会虚减（左「确定」旧 hwnd 失效，只剩新扫描的右候选 → 用例假失败）。
    # 靶域无产品价值，直接清目录（不走 booksweep 备份，免备份堆）。
    for _d in (os.path.join(根, "manual", "书库v2消歧靶"),
               os.path.join(根, "manual_archive", "书库v2消歧靶")):
        if os.path.isdir(_d):
            shutil.rmtree(_d, ignore_errors=True)
    靶 = subprocess.Popen([sys.executable, os.path.join(根, "_tk_dual_target.py")])
    try:
        # 条件就绪替代固定等待（2026-11-01）：开靶固定 sleep 3.5s 在串行回归
        # （前例刚杀 Edge/计算器，负载抖动）下靶渲染慢半拍，OCR 时刻漏检第二
        # 个同名按钮 → 候选=1 假失败；单靶手动验证识别 2/2 稳定。重扫至双候选
        # 就绪（find 候选随重扫 update 补名），最多 5 轮 ~10s。
        s, book, q = {}, {}, {}
        for _ in range(5):
            time.sleep(2)
            s = cli("scan", "--win", "消歧靶")
            book = (s.get("meta") or {}).get("book") or {}
            q = cli("find", "确定", "--domain", "书库v2消歧靶")
            if len([e for e in (q.get("entries") or [])
                    if e.get("stale") is False and not e.get("legacy")]) >= 2:
                break
        候选 = [e for e in (q.get("entries") or [])
               if e.get("stale") is False and not e.get("legacy")]
        两候选 = len(候选) >= 2
        不同位 = 两候选 and (候选[0]["bbox"] != 候选[1]["bbox"])
        # 各点一次：按 x 排序，先左后右；重扫看「左已点」「右已点」文本出现
        全中 = True
        细节 = []
        for 标记 in ("左", "右"):
            组 = sorted(候选, key=lambda e: e["bbox"][0])
            目标 = 组[0] if 标记 == "左" else 组[-1]
            l, t, rr, bb = 目标["bbox"]
            rc = cli("clickxy", str((l + rr) // 2), str((t + bb) // 2),
                     "--top-must", "消歧靶")
            time.sleep(1.2)
            # 复扫强制全量（2026-11-01）：点击后紧接的 scan 落在 60s 同窗增量
            # 窗口内，UIA 元素复用上帧快照 → 按钮文本仍显示旧「确定」，
            # 「左已点/右已点」判假（本轮实测复现）；点击业务本身 ok=true。
            复扫 = cli("scan", "--win", "消歧靶", "--full")
            命中文本 = any(标记 in (e.get("text") or "")
                          for e in (复扫.get("elements") or []))
            全中 = 全中 and rc.get("ok") is True and 命中文本
            细节.append("%s=%s" % (标记, 命中文本))
        记("1 同名消歧双候选", 两候选 and 不同位 and 全中,
           "候选=%d 不同位=%s 各点命中[%s] book=%s"
           % (len(候选), 不同位, " ".join(细节), json.dumps(book, ensure_ascii=False)))
    finally:
        靶.terminate()
        time.sleep(0.5)

    # 收尾：计算器关闭
    subprocess.run(["taskkill", "/f", "/im", "CalculatorApp.exe"],
                   capture_output=True)

    过 = sum(1 for _, ok, _ in 结果 if ok)
    print("\n===== 书库v2验收 %d/%d =====" % (过, len(结果)))
    return 0 if 过 == len(结果) else 1


if __name__ == "__main__":
    sys.exit(main())
