# -*- coding: utf-8 -*-
"""GT 生成器（ScreenPilot·基准评测施工单 B）。

从 gt_build/<key>/scan_1.json 出发，按人工核对结论（纳入/剔除/不确定标注）
生成 bench/scenarios/<key>_gt.json。bbox 统一转窗口相对坐标（screen 模式保持绝对）。

用法：python bench/make_gt.py <scenario_key> [...]
人工核对依据记录在各场景函数的注释与 excluded_notes 中。
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import os
import json

BENCH = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BENCH)


def _is_pua(t):
    """私有区字形（图标字体），视觉可见但文本语义不可读。"""
    return len(t) >= 1 and all(0xE000 <= ord(ch) <= 0xF8FF for ch in t)


def load_scan(key, i=1):
    p = os.path.join(BENCH, "scenarios", "gt_build", key, f"scan_{i}.json")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def build(key, name, mode, win, exclude, roi=None, extra_notes=None,
          override=None, interactive_override=None, source="scan_1"):
    """通用生成：exclude={scan_id: 剔除原因}；override={scan_id: 字段覆盖}。"""
    d = load_scan(key)
    off = d["meta"]["offset"]
    out, excluded = [], []
    for e in d["elements"]:
        sid = e["id"]
        if sid in exclude:
            excluded.append({"from_scan_id": sid, "text": e["text"][:20], "reason": exclude[sid]})
            continue
        if mode == "window":
            bbox = [e["bbox"][0] - off[0], e["bbox"][1] - off[1],
                    e["bbox"][2] - off[0], e["bbox"][3] - off[1]]
        else:
            bbox = list(e["bbox"])
        item = {"gt_id": f"{key[:4]}-{sid:03d}", "text": e["text"], "type": e["type"],
                "bbox": bbox, "interactive": bool(e["clickable"]), "from_scan_id": sid}
        if _is_pua(e["text"]):
            item["uncertainty"] = "图标字体私有区字符（PUA），视觉可见但文本语义不可读"
        if override and sid in override:
            item.update(override[sid])
        if interactive_override and sid in interactive_override:
            item["interactive"] = interactive_override[sid]
        out.append(item)
    gt = {"scenario": key, "name": name, "mode": mode, "win": win,
          "built_at": "2026-10-25",
          "build_method": ("pipeline.scan 连跑 3 次取并集，人工逐条核对标注图与原始截图"
                           f"（bench/scenarios/gt_build/{key}/）"),
          "coord": ("窗口相对物理像素 [l,t,r,b]（运行时加 scan meta.offset 转屏幕坐标）"
                    if mode == "window" else "屏幕绝对物理像素 [l,t,r,b]"),
          "roi": roi,
          "gt_rules": ("纳入=可交互控件+独立语义文本+显示值+有意义命名容器+窗口根；"
                       "剔除=无名低置信全窗容器、与已纳元素完全重叠的冗余容器、无对应实体的误报"),
          "excluded_notes": excluded,
          "notes": extra_notes or [],
          "elements": out}
    dst = os.path.join(BENCH, "scenarios", f"{key}_gt.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(gt, f, ensure_ascii=False, indent=1)
    print(f"{key}: GT {len(out)} 元素（剔除 {len(excluded)}）→ {dst}")
    return gt


# ---------------- 各场景人工核对结论 ----------------

def gt_calculator():
    """计算器（宽布局）。3 扫 UIA 集完全一致，仅 OCR 右栏文本 ±2px 抖动。
    剔除：3 标题栏冗余容器；14 显示区冗余文本（与 15 pane 同域）；39/40/41 无名全窗容器。"""
    return build("calculator", "计算器（标准型·宽布局含历史/记忆面板）", "window", "计算器",
                 exclude={3: "标题栏右段冗余容器(window)，与最小化/最大化/关闭完全重叠",
                          14: "显示区冗余重复(text 显示为 0)，与 15 号 pane 同域",
                          39: "无名全窗容器 conf0.6（已知噪声模式，遗留清单在案）",
                          40: "无名全窗容器 conf0.6（已知噪声模式，遗留清单在案）",
                          41: "无名全窗容器 conf0.6（已知噪声模式，遗留清单在案）"},
                 extra_notes=["右侧「历史记录/记忆」面板为宽布局特有；窗口布局随宽度变化，"
                              "GT 固定在宽布局状态（≥~1100px 宽时计算器自动展开右栏）"])


def gt_taskmgr():
    """任务管理器（进程页·按名称排序）。

    实测关键事实：
    - Win11 新版任务管理器（WinUI/TaskManagerWindow，静默提权）对非提权 UIA 客户端
      几乎封闭——UIA 只回窗口节点 1 个元素，99+ 元素全靠 OCR 兜底；
    - 进程列表动态重排（数值秒级变化、同键行序漂移），表体 GT 用「槽位格」口径：
      GT 固定=格子几何位置，跑分时任何出现在格内的文本都判命中（center-in-slot）；
    - 框架（标题/搜索框/页签/导航/按钮/列表头）静止，用精确 bbox + 融合判同口径。
    """
    d = load_scan("taskmgr")
    off = d["meta"]["offset"]
    chrome_ids = [40, 1, 2, 3, 4, 5, 6, 7, 16, 23, 29, 41, 47, 53, 95,
                  17, 9, 11, 12, 14]
    chrome_names = {40: "任务管理器（窗口根）", 1: "标题文本", 2: "搜索框占位文本",
                    3: "进程页签", 4: "运行新任务按钮", 5: "结束任务按钮", 6: "效率模式按钮",
                    7: "导航·进程", 16: "导航·性能", 23: "导航·应用历史记录", 29: "导航·启动应用",
                    41: "导航·用户", 47: "导航·详细信息", 53: "导航·服务", 95: "导航·设置",
                    17: "列表头·名称", 9: "列表头·CPU", 11: "列表头·内存",
                    12: "列表头·磁盘", 14: "列表头·网络"}
    els = {e["id"]: e for e in d["elements"]}
    out = []
    for sid in chrome_ids:
        e = els[sid]
        bbox = [e["bbox"][0] - off[0], e["bbox"][1] - off[1],
                e["bbox"][2] - off[0], e["bbox"][3] - off[1]]
        out.append({"gt_id": f"task-{sid:03d}", "text": chrome_names[sid], "type": e["type"],
                    "bbox": bbox, "interactive": bool(e["clickable"]),
                    "from_scan_id": sid, "match": "bbox"})
    # ---- 表体槽位格：从 scan_1 观测到的格子生成（名称+CPU+内存+磁盘+网络 五列）----
    body = [e for e in d["elements"]
            if e["bbox"][0] > 950 and (e["bbox"][1] + e["bbox"][3]) / 2 >= 585]
    # 列聚类：按 x 中心排序后按间隙分列
    body.sort(key=lambda e: (e["bbox"][0] + e["bbox"][2]) / 2)
    cols, cur = [], [body[0]]
    for e in body[1:]:
        cx = (e["bbox"][0] + e["bbox"][2]) / 2
        prev_cx = (cur[-1]["bbox"][0] + cur[-1]["bbox"][2]) / 2
        if cx - prev_cx > 40:
            cols.append(cur)
            cur = [e]
        else:
            cur.append(e)
    cols.append(cur)
    slot_i = 0
    for col in cols:
        x0 = min(e["bbox"][0] for e in col) - off[0]
        x1 = max(e["bbox"][2] for e in col) - off[0]
        for e in col:
            slot_i += 1
            bbox = [x0, e["bbox"][1] - 4 - off[1], x1, e["bbox"][3] + 4 - off[1]]
            out.append({"gt_id": f"task-slot{slot_i:03d}", "text": "*（动态单元格槽位）",
                        "type": "slot", "bbox": bbox, "interactive": False,
                        "from_scan_id": e["id"], "match": "slot",
                        "uncertainty": "动态列表槽位：跑分时格内任何文本判命中；"
                                       "进程数变化会影响边缘行填充"})
    # 末行若贴窗口下边缘，标注裁切不确定
    win_h = off[1] and (els[40]["bbox"][3] - off[1])
    for item in out:
        if item["type"] == "slot" and item["bbox"][3] >= win_h - 6:
            item["uncertainty"] += "；末行贴视口下缘，可能被裁切"
    gt = {"scenario": "taskmgr", "name": "任务管理器（进程页·按名称排序·密集列表）",
          "mode": "window", "win": "任务管理器", "built_at": "2026-10-25",
          "build_method": ("ctrl+shift+esc 启动（Popen 会卡成无窗提权实例）→ 按名称排序冻结行序 → "
                           "pipeline.scan 连跑 3 次，人工核对标注图。UIA 仅回窗口节点 1 元素，"
                           "表体全靠 OCR（99+/扫）——密集列表兜底能力为本场景主测点"),
          "coord": "窗口相对物理像素 [l,t,r,b]",
          "roi": None,
          "gt_rules": ("框架元素 match=bbox（融合判同口径）；表体 match=slot（预测中心落在槽位格内即命中）。"
                       "动态列表瞬时 bbox 不可作 GT（行序漂移实测），槽位口径保证可复现"),
          "excluded_notes": [
              {"from_scan_id": 8, "reason": "表头排序箭头（旧扫描曾检出，本次未见，不纳）"},
              {"text": "左栏导航图标/状态叶子图标", "reason": "系统未检出，归入弱点清单（漏识），不作 GT"}],
          "notes": ["进程行序动态重排属场景本性，槽位口径消除该失真",
                    "UIA 对该窗几乎全盲（仅 1 窗口节点）是本轮重要实测发现"],
          "elements": out}
    dst = os.path.join(BENCH, "scenarios", "taskmgr_gt.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(gt, f, ensure_ascii=False, indent=1)
    n_slot = sum(1 for x in out if x["type"] == "slot")
    print(f"taskmgr: GT {len(out)} 元素（框架 {len(out)-n_slot} + 槽位 {n_slot}）→ {dst}")
    return gt


def gt_explorer():
    """文件资源管理器（文件夹靶·详细信息视图·最大化）。

    UIA 树肥（135 元素/扫，3 扫全稳），但顶部工具栏图标排、窗口三键、标签页 +/×
    对 UIA 全盲且 OCR 无文本——人工补标并记入弱点清单。
    面包屑 ChevronTextBlock（垃圾名布局文本）与 BreadcrumbBarItemGrid（布局容器）剔除。
    """
    d = load_scan("explorer")
    off = d["meta"]["offset"]  # [0,0]（最大化）
    include = [
        147,                                                         # 窗口根
        5, 77, 80, 81, 82,                                           # 地址栏/搜索框/占位/搜索钮/搜索图标
        8, 15, 22, 28, 34, 40, 46, 52, 58, 64, 70,                   # 面包屑 10 crumb
        16, 23, 29, 35, 41, 47, 53, 59, 65, 71,                      # 面包屑 10 button
        20, 26, 32, 38, 44, 50, 56, 62, 68,                          # 面包屑 9 chevron
        88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100,         # 表头区+4列头+文字+4筛选钮
        102, 108, 115, 122, 129,                                     # 5 文件行 item
        103, 104, 105, 106, 107, 109, 110, 111, 112, 113, 116,       # 单元格 edit + OCR 文本
        117, 118, 119, 120, 123, 124, 125, 126, 127, 128, 130,
        131, 132, 133, 134,
        154, 159, 160, 161, 162,                                     # 导航窗格/项目视图/预览窗格/缩略图/占位文本
        166, 167, 168, 169, 170, 171,                                # 状态栏组/属性字段/5个项目/查看模式/两radio
        87, 101, 114, 121, 135, 136, 137, 138, 139, 140, 141, 142,   # 侧栏 21 项
        143, 144, 145, 146, 155, 156, 163, 164, 165,
        1,                                                           # 标签页文字（OCR）
        83, 84, 85, 86,                                              # 命令栏 新建/排序/查看/预览（GT 修订补标）
    ]
    exclude = {
        2: "与 148 同域无名容器 pane", 3: "与 148 同域无名容器 pane", 19: "全窗宽无名容器 pane",
        148: "全内容区冗余包装 pane（与 150/151 同 bbox）", 149: "无名容器（与 148 同 bbox）",
        150: "全内容区冗余包装 pane 浏览器窗格", 151: "全内容区冗余包装 pane 文件夹布局窗格",
        4: "地址栏无名容器 group", 7: "地址栏内 pane 包装", 13: "面包屑区无名容器 group",
        76: "搜索区无名容器 group", 6: "地址栏重复 edit（与 5 同 bbox 同名）",
        78: "搜索框重复 edit（与 77 同 bbox 同名）", 79: "搜索框内 pane 包装",
        152: "导航窗格包装 pane 控制项宿主", 153: "导航窗格包装 pane 控制项宿主（重复）",
        157: "项目视图包装 pane Shell 文件夹视图", 158: "项目视图包装 generic（重复）",
        12: "ChevronTextBlock 垃圾名布局文本", 21: "ChevronTextBlock 垃圾名布局文本",
        27: "ChevronTextBlock 垃圾名布局文本", 33: "ChevronTextBlock 垃圾名布局文本",
        39: "ChevronTextBlock 垃圾名布局文本", 45: "ChevronTextBlock 垃圾名布局文本",
        51: "ChevronTextBlock 垃圾名布局文本", 57: "ChevronTextBlock 垃圾名布局文本",
        63: "ChevronTextBlock 垃圾名布局文本", 69: "ChevronTextBlock 垃圾名布局文本",
        75: "ChevronTextBlock 垃圾名布局文本",
        17: "BreadcrumbBarItemGrid 布局容器", 24: "BreadcrumbBarItemGrid 布局容器",
        30: "BreadcrumbBarItemGrid 布局容器", 36: "BreadcrumbBarItemGrid 布局容器",
        42: "BreadcrumbBarItemGrid 布局容器", 48: "BreadcrumbBarItemGrid 布局容器",
        54: "BreadcrumbBarItemGrid 布局容器", 60: "BreadcrumbBarItemGrid 布局容器",
        66: "BreadcrumbBarItemGrid 布局容器", 72: "BreadcrumbBarItemGrid 布局容器",
    }
    # 真名覆盖（OCR 把图标字形并进文本/形近误读；GT 存屏幕真值，误读记 uncertainty）
    text_override = {
        109: "示意图", 123: "说明", 130: "预算表", 137: "音乐", 145: "系统工具盘 (D:)",
        146: "高速游戏 (E:)", 155: "系统工具 (F)", 165: "Linux", 86: "预览",
    }
    misread_note = {
        109: "OCR 读作「■示意图」（图标字形并入）", 116: "OCR 读作「数据.csV」（形近大小写）",
        123: "OCR 读作「置 说明」（图标字形并入）", 130: "OCR 读作「直 预算表」（图标字形并入）",
        137: "OCR 读作「①音乐」（图标字形并入）", 141: "列宽截断显示，屏幕即如此",
        145: "OCR 读作「一 系统工具盘 (D:)」", 146: "OCR 读作「= 高速游戏 (E:)」",
        155: "OCR 读作「= 系统工具 (F)」", 165: "OCR 读作「4 Linux」",
        86: "GT 修订（复核补标）：OCR 读作「D 预览」（图标字形并入），真值=预览按钮",
    }
    els = {e["id"]: e for e in d["elements"]}
    out = []
    for sid in include:
        e = els[sid]
        bbox = [e["bbox"][0] - off[0], e["bbox"][1] - off[1],
                e["bbox"][2] - off[0], e["bbox"][3] - off[1]]
        item = {"gt_id": f"expl-{sid:03d}", "text": text_override.get(sid, e["text"]),
                "type": e["type"], "bbox": bbox, "interactive": bool(e["clickable"]),
                "from_scan_id": sid}
        if sid in misread_note:
            item["uncertainty"] = misread_note[sid]
        elif _is_pua(e["text"]):
            item["uncertainty"] = "图标字体私有区字符（PUA），视觉可见但文本语义不可读"
        elif e["type"] == "edit" and e["text"] in ("名称", "修改日期", "类型", "大小"):
            item["uncertainty"] = "Win11 资源管理器树特性：单元格元素 Name=列名而非单元格值"
        out.append(item)
    # ---- 人工补标（漏识目标，crop 放大实测 bbox，窗口相对坐标）----
    manual = [
        ("后退", (26, 55, 43, 71)), ("前进", (76, 56, 90, 70)),
        ("上移", (124, 56, 136, 71)), ("刷新", (172, 56, 185, 70)),
        ("最小化", (2431, 11, 2445, 20)), ("最大化", (2476, 11, 2489, 21)),
        ("关闭", (2522, 10, 2535, 23)), ("标签页关闭", (231, 19, 241, 29)),
    ]
    for i, (t, bb) in enumerate(manual, 1):
        out.append({"gt_id": f"expl-man{i:02d}", "text": t, "type": "button", "bbox": list(bb),
                    "interactive": True, "from_scan_id": None,
                    "uncertainty": "人工补标：UIA 全盲+OCR 无文本（工具栏图标/窗口三键/标签×），"
                                   "bbox 来自 4x 放大裁图实测（crops/）"})
    gt = {"scenario": "explorer", "name": "文件资源管理器（文件夹靶·详细信息视图·最大化）",
          "mode": "window", "win": "文件夹靶", "built_at": "2026-10-25",
          "build_method": ("explorer.exe 打开 bench/scenes/文件夹靶（5 个固定文件）→ pipeline.scan "
                           "连跑 3 次（171 元素/扫完全一致），人工逐条核对标注图+4x 裁图，"
                           "漏识目标人工补标"),
          "coord": "窗口相对物理像素 [l,t,r,b]", "roi": None,
          "gt_rules": "纳入=可交互控件+语义文本+命名区域锚点；剔除=无名/冗余包装容器、"
                      "ChevronTextBlock 垃圾名文本、Grid 布局容器、重复同名元素",
          "excluded_notes": [{"from_scan_id": k, "reason": v} for k, v in exclude.items()],
          "notes": ["GT 修订（2026-10-25 复核）：命令栏 新建/排序/查看/预览 四键补标入 GT（scan 有检出、初版 GT 无声丢失）","工具栏图标排（后退/前进/上移/刷新）、窗口三键、标签页 +/× 整片漏识（人工补标 8 个）",
                    "3 扫元素集完全一致（171/171/171），静态场景稳定样本",
                    "OCR 对图标字形前缀会并入文本（①/=／一/4 等），已在 uncertainty 注明"],
          "elements": out}
    dst = os.path.join(BENCH, "scenarios", "explorer_gt.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(gt, f, ensure_ascii=False, indent=1)
    print(f"explorer: GT {len(out)} 元素（含人工补标 {len(manual)}，剔除 {len(exclude)}）→ {dst}")
    return gt


def gt_edge_bench():
    """Edge + 本地固定 HTML 基准页（最大化）。

    页面内容 UIA 暴露完整（按钮/输入/表格/链接），收藏夹栏 16 个真实书签照实纳入；
    scan1 的 UIA 只有 83 元素——Edge 无障碍树懒激活（重复查询后才全量暴露），
    scan2/3 稳定 160，bench 跑分前先做一次预热扫描。
    """
    d = load_scan("edge_bench", i=3)
    off = d["meta"]["offset"]  # [0,0] 最大化
    include = [
        95, 1, 13, 38,                                    # 窗口根/标签页栏/应用栏/收藏夹栏（区域锚点）
        2, 3, 4, 7,                                       # 搜索标签页/工作区/标签页pane/新建标签页
        10, 11, 12,                                       # 最小化/还原/关闭
        15, 17, 19, 21, 24, 25, 27, 28, 30, 31,           # 返回/刷新/文件/地址栏/链接钮/搜索图标×2/收藏夹/搜索框
        33, 34, 35, 36, 37, 39, 40, 41,                   # 扩展/收藏夹/个人资料/设置及其他/选项卡组/标签页组/分隔符
        58, 59, 60,                                       # 收藏夹菜单/分隔符/其他收藏夹
        42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57,  # 收藏夹 16 书签
        119, 62, 64,                                      # 页面文档/标题/引言
        66, 67, 68, 69, 70, 71,                           # 按钮区标题×2+4按钮
        72, 73, 75, 76, 79, 82, 83, 84,                   # 表单区标题×2+组+标签+输入框
        77, 78, 80, 81, 85, 86, 87, 88, 89, 90, 91,       # 复选×2+标签×2+单选×3+标签×3+备注框
        92, 93, 128, 122, 123, 124, 125, 126, 127,        # 表格区标题×2+table+表头3+表头文本3
        130, 131, 132, 133, 134, 135, 136, 137, 138,      # 单元格+文本（前两行）
        139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149,  # 第三行+武汉行
        150, 151, 156, 157, 159, 160, 162, 163,           # 链接与列表区+3链接+3文本
        165,                                              # 页脚
        6, 20, 153, 154, 155,                             # OCR：标签页文字/地址栏URL/列表三项
    ]
    exclude = {
        96: "全窗无名 pane 包装", 97: "全窗 pane 包装（有名但冗余）", 98: "全窗无名 pane 包装",
        99: "全窗无名 pane 包装", 100: "全窗无名 pane 包装", 101: "全窗无名 pane 包装",
        102: "全窗无名 pane 包装", 105: "全窗无名 pane 包装", 106: "全窗无名 pane 包装",
        107: "全窗无名 pane 包装", 108: "全窗无名 pane 包装", 109: "全窗无名 pane 包装",
        110: "全窗无名 pane 包装", 111: "全窗无名 pane 包装", 112: "全窗无名 pane 包装",
        9: " caption 区 pane 包装", 94: "chrome 顶区 pane 包装",
        103: "内容区 pane 包装", 104: "内容区 pane 包装",
        114: "内容 host pane 包装", 115: "Chrome Legacy Window pane 包装",
        116: "内容 host pane 包装", 117: "内容 host pane 包装", 118: "内容 host pane 包装",
        5: "标签页 pane 重复（与 4 同 bbox）", 8: "标签条空白区 pane",
        113: "页面文档无名 edit（与 119 同 bbox）",
        18: "地址栏无名 group", 29: "地址栏右侧无名 group",
        22: "地址栏内 pane 包装", 32: "扩展钮 pane 包装", 34: "收藏夹钮 pane 包装",
        23: "4px 宽零文本垃圾 text", 26: "4px 宽零文本垃圾 text",
        61: "标题重复 text（与 62 同 bbox，块级/行内双暴露取行内 62）",
        63: "引言区无名 group", 65: "按钮区无名 group", 74: "表单区无名 group",
        120: "表格区无名 group", 152: "链接与列表区无名 group", 164: "页脚区无名 group",
        121: "表格行无名 item（行容器）", 129: "表格行无名 item（行容器）",
        136: "表格行无名 item（行容器）", 143: "表格行无名 item（行容器）",
        # ---- GT 修订（看图验收员复核+toolbar_recheck.png 裁图实测）----
        16: "前进键不可见（Edge 无前进历史时折叠，键位被刷新占用；裁图实测键位上无前进箭头）",
        14: "主页键不可见（新版 Edge 默认不显示主页键；UIA (0,40,32,72) 为隐藏节点，与返回重叠）",
    }
    text_override = {
        78: "订阅邮件通知", 88: "专业版", 159: "回到表单区", 154: "第二项：接口文档",
    }
    misread_note = {
        78: "UIA Name 异常拼接为「订阅邮件通知口订阅短信通知」，bbox 处可见文本=订阅邮件通知",
        88: "UIA Name 异常拼接为「O基础版 O专业版 O企业版」，bbox 处可见文本=专业版",
        159: "UIA Name 异常拼接为「回到按钮区I回到表单区I回到表格区」，bbox 处可见文本=回到表单区",
        154: "OCR 误读「接文档」（真值：接口文档）",
        45: "书签名含图标字形前缀「菌」（屏幕即如此显示）",
        56: "书签名含图标字形前缀「菌」（屏幕即如此显示）",
        17: "GT 修订（复核）：UIA 曾报刷新与前进同框 (40,44,72,76)；5x 裁图（crops/toolbar_recheck.png）"
            "实测可见键位=返回(0-36)+刷新(图标 49-64，占前进键位)，前进不可见已剔除，本框为刷新实键位",
    }
    els = {e["id"]: e for e in d["elements"]}
    out = []
    for sid in include:
        e = els[sid]
        bbox = [e["bbox"][0] - off[0], e["bbox"][1] - off[1],
                e["bbox"][2] - off[0], e["bbox"][3] - off[1]]
        item = {"gt_id": f"edge-{sid:03d}", "text": text_override.get(sid, e["text"]),
                "type": e["type"], "bbox": bbox, "interactive": bool(e["clickable"]),
                "from_scan_id": sid}
        if sid in misread_note:
            item["uncertainty"] = misread_note[sid]
        elif _is_pua(e["text"]):
            item["uncertainty"] = "图标字体私有区字符（PUA）"
        out.append(item)
    # 人工补标：标签页关闭 ×（UIA/OCR 均未检出）
    out.append({"gt_id": "edge-man01", "text": "标签页关闭", "type": "button",
                "bbox": [257, 13, 271, 26], "interactive": True, "from_scan_id": None,
                "uncertainty": "人工补标：标签 × 未进元素表（新建标签页 + 有 id7 覆盖），bbox 来自 5x 裁图"})
    gt = {"scenario": "edge_bench", "name": "Edge + 本地固定 HTML 基准页（最大化）",
          "mode": "window", "win": "ScreenPilot 基准页", "built_at": "2026-10-25",
          "build_method": ("cmd start msedge file:// 打开 bench/assets/bench_page.html（固定内容）→ "
                           "pipeline.scan 连跑 3 次，人工逐条核对标注图+裁图。scan1 UIA 仅 83 元素"
                           "（Edge a11y 树懒激活），scan2/3 稳定 160——bench 跑分前须预热扫描一次"),
          "coord": "窗口相对物理像素 [l,t,r,b]", "roi": None,
          "gt_rules": "纳入=可交互控件+语义文本+命名区域锚点+真实书签；剔除=无名全窗 pane 包装、"
                      "重复同 bbox 元素、零宽垃圾 text、无名行容器",
          "excluded_notes": [{"from_scan_id": k, "reason": v} for k, v in exclude.items()],
          "notes": ["GT 修订（2026-10-25 复核）：剔除不可见的前进/主页 UIA 节点（裁图 crops/toolbar_recheck.png 实测），刷新框经裁图核验保留","Edge 无障碍树懒激活：首扫 UIA 减半，复扫恢复——agent 实操连续扫描场景不受影响",
                    "UIA 对网页内 label 存在 Name 拼接异常（78/88/159 三例）",
                    "收藏夹栏 16 书签为宿主机真实用户数据，稳定纳入"],
          "elements": out}
    dst = os.path.join(BENCH, "scenarios", "edge_bench_gt.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(gt, f, ensure_ascii=False, indent=1)
    print(f"edge_bench: GT {len(out)} 元素（剔除 {len(exclude)}）→ {dst}")
    return gt


def gt_startmenu():
    """开始菜单（全部=列表视图态）·全屏扫描 + ROI 评测。

    本机 Win11 新版开始菜单布局（搜索框/已固定/最近使用/全部[列表]/右侧移动设备面板）。
    实测：应用列表行（字母头 B/C/D+应用名）UIA 全盲、OCR 仅碎片（便签误读便笑、
    Cocos Dashboard 漏识）——人工补标并记弱点清单；「全部」表头 OCR 漏识。
    ROI 外（被遮挡窗口穿透元素）不计入 GT，预测落在 ROI 内按误报计（诚实口径，
    UIA 穿透噪声正是要量化的对象）。
    """
    d = load_scan("startmenu")
    include = [
        335, 310, 337, 330, 305,            # 搜索框 edit/占位文本/移动设备钮/图标字形/搜索图标
        468, 348, 362, 363, 470,            # 移动设备面板 group/手机图/标题/需要设置/功能容器
        471, 472, 475, 489, 490, 491, 513, 514, 517,  # 短信/呼叫/发送文件 三组 item+图标+文本
        541, 565, 573, 574, 575, 576,       # 说明长文本/选择设备/两链接/两设备名
        630, 631, 632, 633, 634, 635, 636,  # 导航菜单/用户项/用户名/电源/字形/更多/字形
        351,                                # 已固定
        429, 446, 451, 454, 456, 458, 461, 460, 465,  # 固定第一行 9 标签
        501, 505, 507, 508,                 # 固定第二行 4 标签（计算器等漏识→弱点）
        523, 537,                           # 最近使用（标题）/全部显示
        603,                                # 视图:列表
        # GT 修订（复核）：推荐区条目为易变内容（相对时间戳/最近文件名），
        # 整区改槽位口径（与 taskmgr 同款），10 个瞬时文本项移出精确 GT
        612, 611, 616, 623,                 # 便签(误读)/新增×2/Docker Desktop
    ]
    exclude = {
        334: "搜索框按钮重复（与 335 edit 同域）", 306: "搜索图标 image 重复（与 305 同 bbox）",
        469: "移动设备面板无名 pane 包装", 300: "搜索框内 pane 包装",
    }
    text_override = {612: "便签"}
    misread_note = {
        612: "OCR 误读「便笑」（真值：便签）——形近误读实例",
        555: "最近第 2 项主名截断显示为 LibreOffice（列宽所限，屏幕即如此）",
        559: "最近第 3 项 LibreOffice (故障排查模式)",
        460: "Solitaire & Casual Games 两行标签被 OCR 拆为两个元素，均真实",
    }
    els = {e["id"]: e for e in d["elements"]}
    out = []
    for sid in include:
        e = els[sid]
        item = {"gt_id": f"start-{sid:03d}", "text": text_override.get(sid, e["text"]),
                "type": e["type"], "bbox": list(e["bbox"]),
                "interactive": bool(e["clickable"]), "from_scan_id": sid}
        if sid in misread_note:
            item["uncertainty"] = misread_note[sid]
        elif _is_pua(e["text"]):
            item["uncertainty"] = "图标字体私有区字符（PUA）"
        out.append(item)
    manual = [
        ("全部（表头）", (76, 1035, 104, 1050), "人工补标：OCR 漏识「全部」表头，2x 裁图实测"),
        ("B（字母头）", (74, 1082, 86, 1095), "人工补标：应用列表字母头 UIA 全盲+OCR 漏识"),
        ("便签（行）", (118, 1114, 149, 1132), "与 612 同位（612 已以真值纳入，此条不重复计）——占位"),
        ("C（字母头）", (75, 1174, 86, 1188), "人工补标：字母头双盲"),
        ("Cocos Dashboard（行）", (120, 1209, 215, 1222), "人工补标：OCR 漏识该行主名"),
        ("D（字母头）", (74, 1266, 86, 1280), "人工补标：字母头双盲"),
    ]
    for i, (t, bb, note) in enumerate(manual, 1):
        if t.startswith("便签"):
            continue  # 便签行已由 612 覆盖，不重复计入 GT
        out.append({"gt_id": f"start-man{i:02d}", "text": t, "type": "text",
                    "bbox": list(bb), "interactive": False, "from_scan_id": None,
                    "uncertainty": note})
    # GT 修订（复核）：推荐区 3 列改槽位格（观测 bbox 聚合，落格即命中）
    for j, (x0, x1) in enumerate([(60, 300), (360, 600), (620, 860)], 1):
        out.append({"gt_id": f"start-slot{j:02d}", "text": "*（推荐位槽位）", "type": "slot",
                    "bbox": [x0, 888, x1, 990], "interactive": False, "from_scan_id": None,
                    "match": "slot",
                    "uncertainty": "GT 修订：最近使用推荐位易变内容（文件名/相对时间戳）改槽位口径，"
                                   "格内任何文本判命中；推荐内容跨时段会变"})
    gt = {"scenario": "startmenu", "name": "开始菜单（全部=列表视图态·Win11 新版布局）",
          "mode": "screen", "win": None, "built_at": "2026-10-25",
          "build_method": ("pyautogui Win 键展开 → 视图切「列表」（设置持久化）→ esc 关菜单 → "
                           "gt_collect 重开连扫 3 次（win 键 toggle 竞态实测：先关再开才稳）。"
                           "人工核对标注图+2x 裁图，字母头/全部表头人工补标"),
          "coord": "屏幕绝对物理像素 [l,t,r,b]",
          "roi": [16, 500, 1140, 1392],
          "gt_rules": ("ROI=开始菜单面板联合区域（主面板+右侧移动设备面板）。GT 只含菜单自身元素；"
                       "全屏扫描带入的被遮挡窗口元素（计算器×2/Edge/桌面图标等 UIA 穿透）"
                       "落在 ROI 内按误报计——该噪声是本场景的诚实测量对象"),
          "excluded_notes": [
              {"from_scan_id": k, "reason": v} for k, v in exclude.items()],
          "notes": ["GT 修订（2026-10-25 复核）：推荐区 10 个瞬时文本项（文件名/相对时间戳）改 3 列槽位格，保证跨时段可复现","应用列表行对 UIA 全盲（XAML ItemsHost 不进树），OCR 仅碎片——与任务管理器"
                    "UIA 封闭、XAML 弹层无子节点同属一类无障碍缺口",
                    "win 键是 toggle：菜单已开时再按=关闭，自动化必须先核验状态再按键",
                    "菜单视图设置（类别/网格/列表）跨开合持久化"],
          "elements": out}
    dst = os.path.join(BENCH, "scenarios", "startmenu_gt.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(gt, f, ensure_ascii=False, indent=1)
    print(f"startmenu: GT {len(out)} 元素（含人工补标 5，剔除 {len(exclude)}）→ {dst}")
    return gt


def gt_ldplayer():
    """雷电模拟器（武林霸业官服·主城画面）·隔层自绘场景。

    UIA 仅 3 节点（窗口+TheRender/sub 渲染容器，二十棒容器吞并坑同源），
    画面全靠 OCR/visual。GT 取 3 扫稳定交集人工核对；聊天滚屏/跑马灯公告/
    倒计时数值属动态内容不入 GT（记 notes）；标题三键与「长城以南」人工补标/
    抖动标注。
    """
    scans = [load_scan("ldplayer", i) for i in (1, 2, 3)]
    off = scans[0]["meta"]["offset"]

    def sig(e):
        return (e["text"], e["bbox"][0] // 8, e["bbox"][1] // 8, e["type"])
    sets = [{sig(e): e for e in s["elements"]} for s in scans]
    common = {k: sets[0][k] for k in sets[0] if k in sets[1] and k in sets[2]}
    by_id1 = {e["id"]: e for e in scans[0]["elements"]}
    els = by_id1

    include = [
        48,                                    # 窗口根
        1, 2, 3,                               # 标签：雷电模拟器/武林霸业/手机控制
        8, 18,                                 # visual × 弹窗关闭钮
        13, 21, 33, 39, 45, 46, 52, 59, 61,    # 右侧工具栏 按键/加量/减量/全屏/截图/多开/安装/设置/更多
        5, 6, 7, 9, 10, 12, 15, 22, 23, 24, 25, 27, 28, 30, 32,  # 顶部活动+任务文本
        34, 35, 40, 41,                        # 任务追踪文本
        36, 37, 38, 43, 44, 57,                # 武功/商城/百晓生/角色/包裹/快捷
        42,                                    # 玩家名
        63, 65, 67, 72, 79, 84, 85,            # 技能/功能文本
        68, 74,                                # 动态数值文本（含 uncertainty）
    ]
    exclude = {
        49: "TheRender 全屏渲染容器（二十棒容器吞并坑同源，冗余包装）",
        50: "sub 全屏渲染容器（同上）",
        69: "聊天滚屏文本（动态）", 75: "公告/聊天文本（动态）",
        76: "公告/聊天文本（动态）", 83: "公告跑马灯文本（动态）",
        # GT 修订（复核）：动态数值文本不入 GT
        20: "713:956 为动态坐标显示（角色移动即变），剔除",
        82: "85% 为动态电量/资源百分比，剔除",
    }
    dyn_note = {
        68: "含动态数值（81.58 随熟练度变化）", 74: "含动态数值（99.99）",
        82: "动态百分比（85%）",
    }
    game_note = "游戏艺术字体，OCR 文本经标注图人工核对与画面一致"
    out = []
    for sid in include:
        e = els[sid]
        bbox = [e["bbox"][0] - off[0], e["bbox"][1] - off[1],
                e["bbox"][2] - off[0], e["bbox"][3] - off[1]]
        item = {"gt_id": f"ldpl-{sid:03d}", "text": e["text"], "type": e["type"],
                "bbox": bbox, "interactive": bool(e["clickable"]), "from_scan_id": sid}
        if sid in dyn_note:
            item["uncertainty"] = dyn_note[sid]
        elif e["source"] == "ocr":
            item["uncertainty"] = game_note
        elif e["source"] == "visual":
            item["uncertainty"] = "visual 引擎检出（游戏弹窗 ×），人工核对为真实关闭钮"
        out.append(item)
    # 人工补标：标题栏三键（□ × »）+ 跨扫抖动的「长城以南」
    manual = [
        ("最大化/还原", (1209, 14, 1223, 26)), ("关闭", (1242, 14, 1255, 26)),
        ("收起侧栏»", (1279, 14, 1291, 25)),
    ]
    for i, (t, bb) in enumerate(manual, 1):
        out.append({"gt_id": f"ldpl-man{i:02d}", "text": t, "type": "button",
                    "bbox": list(bb), "interactive": True, "from_scan_id": None,
                    "uncertainty": "人工补标：模拟器标题栏三键未进元素表，4x 裁图实测"})
    for s in scans:
        for e in s["elements"]:
            if e["text"] == "长城以南":
                bbox = [e["bbox"][0] - off[0], e["bbox"][1] - off[1],
                        e["bbox"][2] - off[0], e["bbox"][3] - off[1]]
                out.append({"gt_id": "ldpl-flk01", "text": "长城以南", "type": "text",
                            "bbox": bbox, "interactive": False, "from_scan_id": e["id"],
                            "uncertainty": "静态地图名但仅 2/3 扫检出（跨扫抖动漏识实例）"})
                break
        else:
            continue
        break
    gt = {"scenario": "ldplayer", "name": "雷电模拟器（武林霸业官服主城·隔层自绘）",
          "mode": "window", "win": "雷电", "built_at": "2026-10-25",
          "build_method": ("模拟器已在跑（禁擅开游戏，仅扫描不点击）→ pipeline.scan 连跑 3 次 → "
                           "取 3 扫稳定交集人工核对标注图；聊天/公告/倒计时等动态文本不入 GT；"
                           "标题三键 4x 裁图人工补标"),
          "coord": "窗口相对物理像素 [l,t,r,b]", "roi": None,
          "gt_rules": "GT=3 扫稳定交集的静态 UI 文本/图标/容器根+人工补标；动态文本不入 GT；"
                      "UIA 渲染容器剔除并记弱点",
          "excluded_notes": [{"from_scan_id": k, "reason": v} for k, v in exclude.items()],
          "notes": ["GT 修订（2026-10-25 复核）：剔除动态坐标 713:956 与电量 85% 两项（动态数值不入 GT）","UIA 树仅窗口+两个全屏渲染容器（pane TheRender/sub）——宿主 UIA 对模拟器"
                    "内部完全失明，OCR/visual 是唯一感知",
                    "元素总数跨扫波动（85/79/80）：聊天滚屏/公告跑马灯/倒计时造成",
                    "游戏文本为艺术字体，GT 文本以标注图人工核对为准（部分可能存在 OCR 形近字）"],
          "elements": out}
    dst = os.path.join(BENCH, "scenarios", "ldplayer_gt.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(gt, f, ensure_ascii=False, indent=1)
    print(f"ldplayer: GT {len(out)} 元素（剔除 {len(exclude)}）→ {dst}")
    return gt


MAKER = {"calculator": gt_calculator, "taskmgr": gt_taskmgr, "explorer": gt_explorer,
         "edge_bench": gt_edge_bench, "startmenu": gt_startmenu, "ldplayer": gt_ldplayer}

if __name__ == "__main__":
    keys = sys.argv[1:] or list(MAKER)
    for k in keys:
        MAKER[k]()
