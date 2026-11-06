# -*- coding: utf-8 -*-
"""wait 语义（业务生效判定）—— ScreenPilot

根治「点击执行成功 ≠ 业务生效」（台账坑12 / Q-012：OCR 合并文字中心落缝隙、
invoke 发出但界面没反应等场景）：click / type 动作发出后按 expect 条件轮询
扫描，条件成立才算成功返回；超时如实返回未命中状态并附最后一次扫描的命中
证据，禁止谎报成功。

expect 条件：{find: "文本子串", mode: "appear"|"disappear",
timeout: 秒(默认10), interval: 毫秒(默认800)}，可加 window/hwnd 指定扫描窗口。
匹配在元素表 text 上做归一化子串包含（×→x + lower，台账坑20：OCR 对 ×/x
形近替换常见，别硬卡精确串）；disappear = 该文本从元素表消失。

实现约束（台账坑14/坑6）：
- UIA STA 不能跨线程——本模块不持有任何 UIA 对象，轮询全部走 pipeline.scan
  （UIA 查找/遍历在独立 COM 线程内完成，跨线程只传纯数据）；
- 轮询扫描不传 tracker_state（自然编号、不读写保号链快照）——expect 只判
  文本，编号非其语义；元素消失后编号不复用（坑6），证据里的 id 为该次扫描
  的临时编号。
- 扫描窗口必须明确（窗口模式只扫该窗口，禁止全屏乱扫）：优先 expect 显式
  指定，否则用被操作元素的所属窗口（hwnd）；两者皆无时拒绝执行。
"""
import json
import time

import ctypes

from . import pipeline

默认timeout秒 = 10
默认interval毫秒 = 800

# 窗口消失类报错特征（体感账 #14：目标窗消失恰是 disappear 模式的成功态）。
# 注意「窗口无响应」不算——挂死≠消失，仍走正常轮询。
_窗消失特征 = ("句柄已失效", "未找到标题含", "窗口可能已关闭")


def _窗没了(hwnd):
    if not hwnd:
        return False
    try:
        return not ctypes.windll.user32.IsWindow(int(hwnd))
    except Exception:
        return False


def 归一(text):
    """形近归一：lower + ×→x（台账坑20）。"""
    return (text or "").lower().replace("×", "x")


def parse_expect(expect):
    """解析并校验 expect 条件；接受 dict 或 JSON 字符串。

    返回规范 dict；None 表示未提供（等待跳过）。非法输入抛 ValueError。
    """
    if expect is None:
        return None
    if isinstance(expect, str):
        s = expect.strip()
        if not s:
            return None
        try:
            expect = json.loads(s)
        except Exception as e:
            raise ValueError("expect JSON 解析失败: %s" % e)
    if not isinstance(expect, dict):
        raise ValueError("expect 须为对象 {find, mode, timeout, interval[, window/hwnd]}"
                         " 或其 JSON 字符串")
    find = str(expect.get("find", "") or "").strip()
    if not find:
        raise ValueError("expect.find 必填：要等待出现的文本子串")
    mode = str(expect.get("mode", "appear") or "appear").strip().lower()
    if mode not in ("appear", "disappear"):
        raise ValueError("expect.mode 只支持 appear / disappear（默认 appear）")
    try:
        timeout = float(expect.get("timeout") if expect.get("timeout") is not None
                        else 默认timeout秒)
        interval = float(expect.get("interval") if expect.get("interval") is not None
                         else 默认interval毫秒)
    except (TypeError, ValueError):
        raise ValueError("expect.timeout（秒）/ interval（毫秒）须为数字")
    if timeout <= 0 or timeout > 120:
        raise ValueError("expect.timeout 须在 (0, 120] 秒内")
    if interval <= 0:
        raise ValueError("expect.interval 须 >0 毫秒")
    hwnd = expect.get("hwnd")
    try:
        hwnd = int(hwnd) if hwnd else None
    except (TypeError, ValueError):
        raise ValueError("expect.hwnd 须为整数窗口句柄")
    win = str(expect.get("window") or "").strip() or None
    return {"find": find, "mode": mode, "timeout": timeout,
            "interval": interval, "window": win, "hwnd": hwnd}


def _匹配元素(元素, find_norm):
    """元素表按归一化子串匹配，返回命中证据列表（id/文本/bbox/来源）。"""
    return [{"id": e.id, "text": e.text[:60], "bbox": list(e.bbox),
             "source": e.source}
            for e in 元素 if find_norm in 归一(e.text)]


def wait_for(条件, 默认hwnd=0, 默认win=None):
    """按已解析的 expect 条件轮询扫描，返回等待报告 dict。

    扫描窗口：条件内 window/hwnd 优先，否则用调用方给的默认（被操作元素
    所属窗口）；都没有则拒绝（禁止全屏乱扫）。扫描走 background=True
    （PrintWindow 抓窗，不置前不抢焦点——物理点击后窗口本就在前台，后台
    抓窗不改变前台状态）。UIA 元素表不受 PrintWindow 影响；黑帧回退在
    报告 bg_fallback 里如实标注。
    """
    目标hwnd = 条件["hwnd"] or (int(默认hwnd) if 默认hwnd else None)
    目标win = 条件["window"] or 默认win
    if not 目标hwnd and not 目标win:
        raise ValueError("expect 无法确定扫描窗口：请传 expect.window（标题子串）"
                         "或 expect.hwnd（被操作元素为 OCR/视觉来源无 hwnd 时必填）")
    find_norm = 归一(条件["find"])
    appear = 条件["mode"] == "appear"
    开始 = time.monotonic()
    deadline = 开始 + 条件["timeout"]
    轮数 = 0
    报告 = {
        "matched": False, "mode": 条件["mode"], "find": 条件["find"],
        "timeout_s": 条件["timeout"], "interval_ms": 条件["interval"],
        "elapsed_ms": 0, "scans": 0, "window": None, "bg_fallback": False,
        "evidence": {"hit": False, "matches": [], "last_total": 0,
                     "last_texts": []},
    }
    while True:
        轮数 += 1
        # 窗口消失快判（体感账 #14 修复）：IsWindow=False 是 Win32 事实——
        # disappear 模式=目标窗没了=成功（verified_by_disappearance）；
        # appear 模式=窗都没了文本不可能出现，如实失败提前返回，不空轮询。
        if _窗没了(目标hwnd):
            报告.update(scans=轮数,
                        elapsed_ms=int((time.monotonic() - 开始) * 1000),
                        matched=not appear, window_gone=True)
            报告["evidence"] = {
                "hit": not appear, "matches": [], "last_total": 0,
                "last_texts": [],
                "verified_by_disappearance": not appear,
                "note": "IsWindow=False（窗口已关闭）",
            }
            return 报告
        try:
            结果 = pipeline.scan(win=目标win, hwnd=目标hwnd, background=True,
                                 no_focus=True)
        except Exception as e:
            msg = str(e)
            窗关了 = any(k in msg for k in _窗消失特征)
            if 窗关了:
                # 扫描通道报窗消失：与 IsWindow 同语义（体感账 #14 实锤场景
                # ——点关闭后轮询抛「句柄已失效」，旧代码把成功态报成 ok:false）
                报告.update(scans=轮数,
                            elapsed_ms=int((time.monotonic() - 开始) * 1000),
                            matched=not appear, window_gone=True)
                报告["evidence"] = {
                    "hit": not appear, "matches": [], "last_total": 0,
                    "last_texts": [],
                    "verified_by_disappearance": not appear,
                    "note": "扫描报窗口已消失: %s" % msg[:120],
                }
                return 报告
            raise
        元素 = 结果["elements"]
        报告["window"] = 结果["meta"].get("window") or 报告["window"]
        报告["bg_fallback"] = bool(结果["meta"].get("bg_fallback"))
        残留 = _匹配元素(元素, find_norm)
        命中 = bool(残留) == appear  # appear: 找到即中；disappear: 找不到即中
        报告.update(scans=轮数,
                    elapsed_ms=int((time.monotonic() - 开始) * 1000),
                    matched=命中)
        报告["evidence"] = {
            "hit": 命中,
            "matches": 残留[:8],   # appear 命中=出现的元素；disappear 超时=仍在的元素
            "last_total": len(元素),
            "last_texts": [e.text[:24] for e in 元素[:24]],
        }
        if 命中:
            return 报告
        剩余 = deadline - time.monotonic()
        if 剩余 <= 0:
            return 报告
        time.sleep(min(条件["interval"] / 1000.0, 剩余))


def apply_expect(输出, expect, 默认hwnd=0, 默认win=None):
    """CLI 与 MCP 共用的收尾粘合：在动作结果 dict 上应用 expect 等待。

    - 无 expect：原样返回（向后兼容，现有调用零破坏）；
    - expect 参数非法：ok=False + expect_error（不撒谎）；
    - 动作本身失败：不再等待，动作错误如实返回；
    - 等待超时未命中：整体 ok=False + error + expect 报告（证据见
      expect.evidence）——超时必须如实失败，禁止谎报成功。
    """
    try:
        条件 = parse_expect(expect)
    except ValueError as e:
        输出["ok"] = False
        输出["expect_error"] = str(e)
        return 输出
    if 条件 is None:
        return 输出
    if not 输出.get("ok"):
        return 输出
    try:
        报告 = wait_for(条件, 默认hwnd=默认hwnd, 默认win=默认win)
    except Exception as e:
        输出["ok"] = False
        输出["action_executed"] = True  # 动作已发出，只是轮询验证没跑成
        输出["expect_error"] = "expect 轮询扫描失败: %s" % e
        return 输出
    输出["expect"] = 报告
    if not 报告["matched"]:
        # 走到这里的前提是动作本身 ok（已发出）——ok=false 只表达「验证未命中」，
        # 用 action_executed 把「动作已执行」单独说清，防 agent 误读成点击没生效
        # 而对危险按钮重复点击（2026-11-04 实操体感评测·体验问题3）。
        输出["ok"] = False
        输出["action_executed"] = True
        输出["error"] = ("expect 未命中（timeout %gs 如实失败；动作已执行，"
                         "未命中≠动作失败，勿盲目重复危险操作）：%s '%s'；"
                         "最后一次扫描证据见 expect.evidence"
                         % (报告["timeout_s"], 报告["mode"], 报告["find"]))
    return 输出
