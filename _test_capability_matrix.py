# -*- coding: utf-8 -*-
"""能力矩阵自检：一张表核对「能力 × CLI × MCP × 参数穿透 × 测试覆盖」。

为什么要它（2026-11-01 A4）：
  历史两次同类事故——UIA 修复只进 sp/ 没进 MCP 进程；滚动复用/OCR/stale 三件
  修好但 mcp_server.py 从没传 incremental，agent 日常通道一件都享受不到。
  根因不是手艺差，是没有机制能在改完代码后自动告诉你「工具面漏了哪个参数」。
  本脚本把这个教训固化成检查：改完 sp/ 能力/参数，跑它，缺口立刻现形。

检查分三层：
  1. 入口存在：CLI 子命令在 screenpilot.py 注册 / MCP 工具在 mcp_server.py 有 @mcp.tool
  2. 参数穿透：声明的关键参数确实出现在 MCP 工具函数签名里（防「CLI 有 MCP 无」）
  3. 测试覆盖：能力有对应 _test_*.py（存在性检查，不跑用例）

用法：
  python _test_capability_matrix.py          # 打印矩阵，有 FAIL 退出码 1
  python _test_capability_matrix.py --json   # 机器可读

口径：
  - 能力（capability）：agent 干活必需的七类——缺 MCP 入口=FAIL；
  - 管理命令（admin）：书库运维类，CLI 够用、MCP 非必需——缺 MCP 入口=INFO。
"""
import ast
import io
import json
import os
import sys

根 = os.path.dirname(os.path.abspath(__file__))


def _读(p):
    return io.open(os.path.join(根, p), encoding='utf-8').read()


def _cli子命令():
    """解析 screenpilot.py 里所有 子.add_parser('名字') 的注册。"""
    树 = ast.parse(_读('screenpilot.py'))
    名 = set()
    for 节点 in ast.walk(树):
        if isinstance(节点, ast.Call) and isinstance(节点.func, ast.Attribute) \
                and 节点.func.attr == 'add_parser' and 节点.args:
            参 = 节点.args[0]
            if isinstance(参, ast.Constant) and isinstance(参.value, str):
                名.add(参.value)
    return 名


def _mcp工具():
    """解析 mcp_server.py：@mcp.tool 装饰的函数 → {名: 参数名集合}。"""
    树 = ast.parse(_读('mcp_server.py'))
    工具 = {}
    for 节点 in ast.walk(树):
        if not isinstance(节点, ast.FunctionDef):
            continue
        有装饰 = any(
            (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
             and d.func.attr == 'tool')
            or (isinstance(d, ast.Attribute) and d.attr == 'tool')
            for d in 节点.decorator_list)
        if 有装饰:
            工具[节点.name] = {a.arg for a in 节点.args.args}
    return 工具


# 能力矩阵：能力 → (类别, CLI 子命令, MCP 工具, 必须穿透的参数, 测试文件或 None)
矩阵 = [
    ("扫描活屏", "cap", "scan", "scan_screen",
     {"incremental", "book", "background", "hwnd"}, "_test_mcp.py"),
    ("增量扫描", "cap", "scan", "scan_screen", {"incremental"}, "_test_incremental.py"),
    ("置前窗口", "cap", "activate", "activate_window", set(), "_test_mcp.py"),
    ("堆叠感知", "cap", "stack", "window_stack", {"occlude_hwnd"}, "_test_stack.py"),
    ("按编号点击", "cap", "click", "click_element",
     {"physical", "precise", "expect", "check_top"}, "_test_mcp.py"),
    ("安全点击", "cap", None, "click_safe", {"expect"}, "_test_mcp.py"),
    ("坐标点击", "cap", "clickxy", "click_xy",
     {"precise", "expect", "top_must"}, "_test_mcp.py"),
    ("文本输入", "cap", "type", "type_text", {"precise", "expect"}, "_test_mcp.py"),
    ("按键", "cap", "key", "press_key", set(), "_test_mcp.py"),
    ("滚动", "cap", "scroll", "scroll_screen", set(), "_test_scroll_stale.py"),
    ("截屏", "cap", "snap", "snap_screen", {"background", "hwnd"}, "_test_mcp.py"),
    ("裁片-元素", "cap", "crop", "crop_screen", {"pad"}, "_test_twovision.py"),
    ("裁片-坐标", "cap", "cropxy", "crop_xy", {"pad"}, "_test_twovision.py"),
    ("书库检索", "cap", "find", "book_find",
     {"include_legacy", "domain", "element_type"}, "_test_book_v2.py"),
    ("静态图识别", "cap", "scanimg", "scan_image", set(), "_test_shape_detect.py"),
    ("连拍窗", "cap", "watch", "watch_burst",
     {"seconds", "interval", "region", "action", "mode"}, "_test_watch.py"),
    ("打字验证", "cap", "verify", "verify_typing",
     {"wait", "region", "action", "ocr_check"}, "_test_watch.py"),
    ("卡死探测", "cap", "hang", "probe_hang",
     {"key", "wait", "resend_after"}, "_test_watch.py"),
    ("抠底分层", "cap", "split", "split_static_dynamic",
     {"frames", "interval", "std_thresh", "moving_min_dist", "verify_ocr"},
     "_test_watch.py"),
    ("书库清扫", "admin", "booksweep", None, set(), None),
    ("legacy 归档", "admin", "bookarchive", None, set(), "_test_book_clean.py"),
]


def 检查():
    命令 = _cli子命令()
    工具 = _mcp工具()
    行, 缺口 = [], []
    for 名, 类, cli, mcp, 参数, 测试 in 矩阵:
        项 = {"capability": 名, "kind": 类, "cli": cli, "mcp": mcp,
              "params": sorted(参数), "test": 测试,
              "cli_ok": cli is None or cli in 命令,
              "mcp_ok": mcp is None or mcp in 工具,
              "params_ok": True, "test_ok": 测试 is None or os.path.isfile(os.path.join(根, 测试)),
              "issues": []}
        if cli and not 项["cli_ok"]:
            项["issues"].append("CLI 子命令 %r 未注册" % cli)
        if mcp and not 项["mcp_ok"]:
            项["issues"].append("MCP 工具 %r 不存在" % mcp)
        if mcp and 项["mcp_ok"] and 参数:
            签名 = 工具[mcp]
            缺 = sorted(参数 - 签名)
            if 缺:
                项["params_ok"] = False
                项["issues"].append("MCP %s 缺参数 %s（CLI/引擎有、工具面没接）"
                                    % (mcp, "、".join(缺)))
        if 测试 and not 项["test_ok"]:
            项["issues"].append("测试文件 %r 不存在" % 测试)
        # 分级：能力类缺 MCP 入口=FAIL（若声明了工具名）或 WARN（尚未提供工具）；
        # 管理类缺 MCP 入口=INFO（CLI 够用）。参数漏接永远 FAIL。
        if 类 == "cap" and mcp and not 项["mcp_ok"]:
            项["level"] = "FAIL"
        elif 类 == "cap" and mcp is None:
            项["level"] = "WARN"
            项["issues"].append("MCP 尚未提供该工具（能力类，待补）")
        elif 项["issues"]:
            项["level"] = "FAIL" if 类 == "cap" and not 项["params_ok"] else "WARN"
        else:
            项["level"] = "OK"
        if 项["level"] == "FAIL":
            缺口.append(项)
        行.append(项)
    return 行, 缺口


def main(argv=None):
    argv = argv or sys.argv[1:]
    行, 缺口 = 检查()
    if "--json" in argv:
        print(json.dumps({"rows": 行, "fail": len(缺口),
                          "ok": len(行) - len(缺口)}, ensure_ascii=False, indent=1))
    else:
        print("能力矩阵自检（%d 项）" % len(行))
        print("%-4s %-14s %-10s %-16s %-8s %s"
              % ("级别", "能力", "CLI", "MCP", "参数", "说明"))
        for 项 in 行:
            说明 = "；".join(项["issues"]) or ("测试 " + (项["test"] or "—"))
            print("%-4s %-14s %-10s %-16s %-8s %s"
                  % (项["level"], 项["capability"], 项["cli"] or "—",
                     项["mcp"] or "—",
                     "✓" if 项["params_ok"] else "✗", 说明))
        print()
        if 缺口:
            print("FAIL %d 项：" % len(缺口))
            for 项 in 缺口:
                print("  - %s：%s" % (项["capability"], "；".join(项["issues"])))
        else:
            print("全部能力入口与参数穿透对齐（管理类命令缺 MCP 入口按 INFO 处理）。")
    return 1 if 缺口 else 0


if __name__ == "__main__":
    sys.exit(main())
