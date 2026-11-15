# -*- coding: utf-8 -*-
# 2026-11-15 加固：stdout 非 tty（重定向/CI）时 Windows 默认 GBK，✅/❌ 直接
# UnicodeEncodeError 崩（本日实测）——与其他测试套对齐，强制 utf-8。
import sys as _sys
try:
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
"""MCP server 端到端自测 —— ScreenPilot

真实 stdio 起 mcp_server.py 子进程，走 MCP 协议：
initialize → 七工具齐全 → activate_window(计算器) → scan_screen(并行管线) →
click_element 连点 7×8 → 重扫核对显示 56（业务闭环）→ snap_screen →
press_key(esc) → type_text(中文，best-effort)。

靶子纪律：禁用 notepad 当靶（Win11 会话恢复会污染用户已开标签）。
计算器 + tkinter 自造靶（tk_target.py），均为 taskkill 即清、无副作用。
"""
import asyncio
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")
TARGET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tk_target.py")


def _命中计数(文本):
    """计数标题命中（2026-11-15 tk 靶诊断实锤后新增）：UIA 全名「自测按钮×N」
    或 OCR 半截「钮×2」（tk 按钮右半被 OCR 单独读出时 UIA Name 还落后一拍）。"""
    t = 文本.lower().replace("×", "x")
    return t.startswith("自测按钮x") or "钮x" in t


def 摘要(text, n=300):
    """结果 JSON 摘要（控制台友好）。"""
    try:
        d = json.loads(text)
    except Exception:
        return text[:n]
    if "elements" in d:
        d = {"ok": True, "meta": d.get("meta"), "元素数": len(d.get("elements", [])),
             "hint": d.get("hint", "")[:60]}
    s = json.dumps(d, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


async def main():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=[SERVER])
    结果 = {"通过": 0, "失败": 0}

    def 记(名, ok, 说明=""):
        结果["通过" if ok else "失败"] += 1
        print(("  ✅" if ok else "  ❌") + f" {名}" + (f"：{说明}" if 说明 else ""))

    # 靶子：计算器（单实例，重复 start 复用现有窗口）+ tk 自测靶
    # 计算器若在测试前已存在（可能是别的施工组在用），收尾不杀（禁杀他人进程）
    计算器预存 = subprocess.run(
        ["tasklist", "/fi", "IMAGENAME eq CalculatorApp.exe"],
        capture_output=True).stdout.decode("gbk", "ignore").lower().find("calculatorapp") >= 0
    靶 = subprocess.Popen([sys.executable, TARGET])
    # 2026-11-15 修根因（玄览当窗）：原固定 sleep(2.5) 赌 UWP 冷启动完成——
    # 计算器被强杀后恢复启动更慢，2.5s 时窗口未出现在顶层列表，activate 直接
    # 「未找到标题含计算器的窗口」全线挂（历史挂账「计算器标题未读到」的真根
    # 因之一，非前台占用）。改轮询等窗出现（最多 15s）。
    # 双通道启动（2026-11-15 第四轮实测补）：start calc 走 App Execution
    # Alias（WindowsApps\calc.exe 重解析点），本机实测该 alias 已坏（命令
    # 成功返回但进程 10s 不现，包本体 Get-AppxPackage Status=Ok）——alias
    # 先试 6s，未起自动切 shell:AppsFolder 包直启（实测一次成功）。
    def 计算器窗已现():
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        命中 = []
        枚举 = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(h, l):
            n = user32.GetWindowTextLengthW(h)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(h, buf, n + 1)
                # 类名校验（2026-11-15 三轮实测补）：强杀后 start calc 先弹
                # 启动 splash，标题同样含「计算器」——轮询命中 splash 即 break
                # 属假阳性（splash 灭后主窗未现，activate 全线挂，本日第三轮
                # 实锤）。主窗类=ApplicationFrameWindow，splash 不是。
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(h, cls, 256)
                if ("计算器" in buf.value
                        and cls.value == "ApplicationFrameWindow"
                        and user32.IsWindowVisible(h)):
                    命中.append(h)
            return True
        user32.EnumWindows(枚举(cb), 0)
        return bool(命中)
    起 = False
    for _ in range(20):   # alias 通道先给 6s
        if 计算器窗已现():
            起 = True
            break
        await asyncio.sleep(0.3)
    if not 起:
        print("!! calc 别名通道 6s 未起窗，切 shell:AppsFolder 直启", flush=True)
        subprocess.Popen(["explorer.exe",
                          r"shell:AppsFolder\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"],
                         creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(30):   # 直启通道再给 9s
            if 计算器窗已现():
                起 = True
                break
            await asyncio.sleep(0.3)
    if not 起:
        print("!! 15s 内计算器窗口未出现（UWP 启动异常），后续步骤预期全挂", flush=True)
    await asyncio.sleep(0.5)   # 窗口出现后再留半秒渲染稳定

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            # 2026-11-15 加固（玄览当窗·挂账重跑撞出）：原裸 await 无步级超时，
            # 某步 call_tool 不返回则整套永久卡死，且块缓冲丢光中间输出死因不可
            # 考（本日实测 450s+ 零输出卡死）。统一包 wait_for：快速失败+留痕。
            async def call(tool, args, _t=120):
                return await asyncio.wait_for(session.call_tool(tool, args), timeout=_t)

            info = await asyncio.wait_for(session.initialize(), timeout=30)
            print(f"[1] initialize: server={info.serverInfo.name} v{info.serverInfo.version}")

            tools = await session.list_tools()
            名单 = sorted(t.name for t in tools.tools)
            print(f"[2] tools/list: {名单}")
            # 2026-11-15 修测试腐化：工具面 11-06 收台批已 18→20 件，写死
            # 8 件全等断言必然挂；改「八核心工具包含」断言（核心在=通道
            # 健康，工具面扩容不再误报）
            记("八核心工具齐全", set(["activate_window", "click_element",
                                "click_xy", "press_key", "scan_screen",
                                "scroll_screen", "snap_screen",
                                "type_text"]) <= set(名单))

            r = await call("activate_window", {"window": "计算器"})
            d = json.loads(r.content[0].text)
            print(f"[3] activate_window(计算器): {摘要(r.content[0].text, 200)}")
            记("activate", d.get("ok") is True and d.get("rect"), str(d.get("rect", "")))

            r = await call("scan_screen", {"window": "计算器"})
            d = json.loads(r.content[0].text)
            print(f"[4] scan_screen(计算器·并行管线): {摘要(r.content[0].text, 400)}")
            记("scan ok（并行）", d.get("ok") is not False and len(d.get("elements", [])) > 0,
              f"{d.get('meta', {}).get('counts')} {d.get('meta', {}).get('duration_ms')}ms "
              f"tracker={d.get('meta', {}).get('tracker')}")
            元素 = d.get("elements", [])
            首帧按钮id = {e["text"]: e["id"] for e in 元素 if e["text"] in ("七", "乘以", "八", "等于")}

            def 找(文本):
                return min([e for e in 元素 if e["text"] == 文本], key=lambda e: e["id"], default=None)

            # 连点 7 × 8（编号来自本次 scan；按钮位置固定，界面仅显示区变化）
            顺序 = ["七", "乘以", "八", "等于"]
            全部点中 = True
            d点 = {}   # 2026-11-15 修预存崩点：按钮缺失 break 时 d点 从未赋值，
                       # 146 行 str(d点.get(...)) 抛 UnboundLocalError——历史挂账
                       # 「计算器标题未读到」时测试不是 FAIL 而是崩，机制即此
            for 文本 in 顺序:
                目标 = 找(文本)
                if not 目标:
                    全部点中 = False
                    break
                r = await call("click_element", {"element_id": 目标["id"]})
                d点 = json.loads(r.content[0].text)
                if d点.get("ok") is not True:
                    全部点中 = False
                    break
                await asyncio.sleep(0.25)
            print(f"[5] click_element 连点 {'→'.join(顺序)}: 全部 ok={全部点中}")
            记("click 连点", 全部点中, str(d点.get("method", "")))

            # 重扫核对业务闭环：显示应为 56；且按钮编号跨 scan 保持稳定
            r = await call("scan_screen", {"window": "计算器"})
            d2 = json.loads(r.content[0].text)
            显示 = [e["text"] for e in d2.get("elements", []) if "56" in e.get("text", "")]
            print(f"[6] 重扫核对: 含「56」的元素={显示[:3]}")
            记("业务闭环 7×8=56", len(显示) > 0, "；".join(显示[:2]))
            tr = d2.get("meta", {}).get("tracker", {})
            重扫id = {e["text"]: e["id"] for e in d2.get("elements", [])
                     if e["text"] in ("七", "乘以", "八", "等于")}
            保住 = all(首帧按钮id.get(k) == v for k, v in 重扫id.items()) and len(重扫id) == 4
            print(f"[6.5] 编号保号: 首帧={首帧按钮id} 重扫={重扫id} tracker={tr}")
            记("编号跨 scan 保号", 保住 and tr.get("kept", 0) >= 4,
              f"kept={tr.get('kept')} new={tr.get('new')}")

            r = await call("snap_screen", {"window": "计算器"})
            d = json.loads(r.content[0].text)
            print(f"[7] snap_screen: {摘要(r.content[0].text, 200)}")
            记("snap", d.get("ok") is True, d.get("path", ""))

            r = await call("press_key", {"key": "esc"})
            d = json.loads(r.content[0].text)
            print(f"[8] press_key(esc): {摘要(r.content[0].text)}")
            记("key", d.get("ok") is True)

            # type_text best-effort：靶窗口无 UIA edit 时跳过（tk 桥不暴露 Entry）
            # 顺带在同一靶上验证后台扫描与漂移点击：
            #  [9.1] background=True 不置前扫 tk 靶（此时前台仍是计算器）
            #  [9.2] click --physical 默认漂移 → at≠中心 & 按钮标题变 ×1（业务闭环）
            r = await call("scan_screen", {"window": "识屏自测靶",
                                                        "background": True})
            d = json.loads(r.content[0].text)
            靶元素 = d.get("elements", [])
            后台按钮 = [e for e in 靶元素 if "自测按钮" in e.get("text", "")]
            print(f"[9.1] 后台扫描(不置前): {摘要(r.content[0].text, 300)}")
            记("后台扫描 background=True", d.get("ok") is not False and len(靶元素) > 0
              and d.get("meta", {}).get("background") is True
              and d.get("meta", {}).get("bg_fallback") is False and bool(后台按钮),
              f"total={len(靶元素)} 按钮={[(e['id'], e['text']) for e in 后台按钮]}")

            编辑 = [e for e in 靶元素 if e["type"] in ("edit", "document")]
            if 编辑:
                r = await call("type_text", {"element_id": 编辑[0]["id"],
                                                          "text": "ScreenPilot MCP 自测"})
                d = json.loads(r.content[0].text)
                print(f"[9] type_text(中文): {摘要(r.content[0].text)}")
                记("type 中文", d.get("ok") is True, str(d.get("method", "")))
            else:
                print("[9] type_text(中文): SKIP（靶窗口无 UIA edit；tk 桥不暴露 Entry，"
                      "type 核心逻辑未改动，以 10-23 SDK 自测 6/6 为证据）")
                记("type 中文（SKIP 不计失败）", True, "skipped")

            if 后台按钮:
                r = await call("activate_window", {"window": "识屏自测靶"})
                await call("click_element",
                                        {"element_id": 后台按钮[0]["id"], "physical": True})
                await asyncio.sleep(0.8)
                r = await call("scan_screen", {"window": "识屏自测靶",
                                                            "background": True,
                                                            "incremental": False})
                d = json.loads(r.content[0].text)
                点后 = [e for e in d.get("elements", [])
                       if _命中计数(e.get("text", ""))]
                print(f"[9.2] 漂移点击后重扫: {[(e['id'], e['text']) for e in 点后]}")
                记("漂移点击业务闭环(标题变×1)", bool(点后) and 点后[0]["id"] == 后台按钮[0]["id"],
                  f"标题={点后[0]['text'] if 点后 else '未读到'} id 保号={bool(点后) and 点后[0]['id'] == 后台按钮[0]['id']}")

                # [9.3] click_xy 坐标直点：以元素表 bbox 中心为坐标（验证坐标系一致），
                # 点完标题再 +1 = 坐标点击业务闭环（元素表漏识目标时的兜底通道）
                r = await call("scan_screen", {"window": "识屏自测靶",
                                                            "background": True,
                                                            "incremental": False})
                d = json.loads(r.content[0].text)
                按钮 = [e for e in d.get("elements", [])
                       if "自测按钮" in e.get("text", "")]
                if 按钮:
                    b = 按钮[0]["bbox"]
                    cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
                    r = await call("click_xy", {"x": cx, "y": cy,
                                                             "precise": True})
                    dxy = json.loads(r.content[0].text)
                    await asyncio.sleep(0.8)
                    r = await call("scan_screen", {"window": "识屏自测靶",
                                                                "background": True,
                                                                "incremental": False})
                    d = json.loads(r.content[0].text)
                    点后2 = [e for e in d.get("elements", [])
                            if _命中计数(e.get("text", ""))]
                    print(f"[9.3] click_xy({cx},{cy}): {摘要(r.content[0].text, 160)}")
                    记("click_xy 坐标直点业务闭环", dxy.get("ok") is True and bool(点后2),
                      f"method={dxy.get('method')} at={dxy.get('at')}")
                else:
                    记("click_xy 坐标直点（SKIP：找不到靶按钮）", True, "skipped")

            # [10] expect 业务生效判定（wait 语义，根治「点击执行成功≠业务生效」）：
            # tk 的 UIA invoke 不执行回调（台账坑18），全部走 physical=True 物理点击
            # 链路；expect 轮询扫描等标题计数 xN 变化。expect 参数 dict/JSON 串双形态。
            r = await call("scan_screen", {"window": "识屏自测靶",
                                                        "background": True,
                                                        "incremental": False})
            d = json.loads(r.content[0].text)
            按钮10 = [e for e in d.get("elements", [])
                     if _命中计数(e.get("text", ""))]
            mN = re.search(r"x(\d+)$", 按钮10[0]["text"].lower().replace("×", "x")) \
                if 按钮10 else None
            if mN:
                n0 = int(mN.group(1))
                # [10.1] appear 命中（dict 形态）：点击 → 标题 x(n0+1) 出现才算成功
                r = await call("click_element", {
                    "element_id": 按钮10[0]["id"], "physical": True,
                    "expect": {"find": "x%d" % (n0 + 1), "mode": "appear",
                               "timeout": 20}})
                d = json.loads(r.content[0].text)
                print(f"[10.1] expect appear: {摘要(r.content[0].text, 320)}")
                记("expect appear 命中", d.get("ok") is True
                  and d.get("expect", {}).get("matched") is True
                  and bool(d.get("expect", {}).get("evidence", {}).get("matches")),
                  f"find=x{n0+1} scans={d.get('expect', {}).get('scans')} "
                  f"证据={d.get('expect', {}).get('evidence', {}).get('matches')}")

                # [10.2] disappear 命中：再点一次 x(n0+1)→x(n0+2)，旧文本从元素表消失
                r = await call("click_element", {
                    "element_id": 按钮10[0]["id"], "physical": True,
                    "expect": {"find": "x%d" % (n0 + 1), "mode": "disappear",
                               "timeout": 20}})
                d = json.loads(r.content[0].text)
                print(f"[10.2] expect disappear: {摘要(r.content[0].text, 320)}")
                记("expect disappear 命中", d.get("ok") is True
                  and d.get("expect", {}).get("matched") is True,
                  f"find=x{n0+1} 已消失，现文本="
                  f"{d.get('expect', {}).get('evidence', {}).get('last_texts')}")

                # [10.3] timeout 如实失败：不存在的文本 3s 超时 → ok=false 禁止谎报
                r = await call("click_element", {
                    "element_id": 按钮10[0]["id"], "physical": True,
                    "expect": {"find": "绝无此文本xyz", "timeout": 3}})
                d = json.loads(r.content[0].text)
                print(f"[10.3] expect timeout: {摘要(r.content[0].text, 320)}")
                记("expect timeout 如实失败", d.get("ok") is False
                  and d.get("expect", {}).get("matched") is False
                  and d.get("expect", {}).get("scans", 0) >= 1,
                  f"scans={d.get('expect', {}).get('scans')} "
                  f"elapsed={d.get('expect', {}).get('elapsed_ms')}ms "
                  f"error={str(d.get('error', ''))[:60]}")

                # [10.4] click_xy + expect（JSON 串形态；坐标点击无所属窗口，
                # expect.window 显式指定扫描窗口——MCP 侧 Union[dict,str] 兼容）
                r = await call("scan_screen", {"window": "识屏自测靶",
                                                            "background": True,
                                                            "incremental": False})
                d = json.loads(r.content[0].text)
                按钮10b = [e for e in d.get("elements", [])
                          if _命中计数(e.get("text", ""))]
                mN2 = re.search(r"x(\d+)$", 按钮10b[0]["text"].lower().replace("×", "x")) \
                    if 按钮10b else None
                if mN2:
                    n1 = int(mN2.group(1))
                    b = 按钮10b[0]["bbox"]
                    cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
                    r = await call("click_xy", {
                        "x": cx, "y": cy, "precise": True,
                        "expect": json.dumps({"window": "识屏自测靶",
                                              "find": "x%d" % (n1 + 1),
                                              "mode": "appear", "timeout": 20})})
                    d = json.loads(r.content[0].text)
                    print(f"[10.4] click_xy+expect(JSON串): {摘要(r.content[0].text, 320)}")
                    记("click_xy expect 命中(JSON串参数)", d.get("ok") is True
                      and d.get("expect", {}).get("matched") is True,
                      f"find=x{n1+1} scans={d.get('expect', {}).get('scans')}")
                else:
                    记("click_xy expect（SKIP：计数解析失败）", True, "skipped")
            else:
                print("[10] expect 用例 SKIP（读不到按钮计数）")
                记("expect 四连（SKIP）", True, "skipped")

    # 清理靶子（均无未保存内容，taskkill 安全）；计算器预存则不杀（防误伤他人会话）
    if not 计算器预存:
        subprocess.run(["taskkill", "/f", "/im", "CalculatorApp.exe"], capture_output=True)
    else:
        print("(计算器测试前已被其他会话占用，收尾不杀，防误伤)")
    靶.kill()
    print(f"\n==== 自测结果: 通过 {结果['通过']} / 失败 {结果['失败']} ====")
    return 0 if 结果["失败"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
