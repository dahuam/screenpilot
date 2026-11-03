# -*- coding: utf-8 -*-
"""帧间编号追踪：两次 scan 之间同一元素保持编号稳定 —— ScreenPilot

原理：
- 本帧扫描完成后与上一帧元素快照做匹配（IoU + 中心距离 + 文本/类型相似度）；
- 匹配上的沿用上一帧编号，未匹配的新元素追加新编号（编号不复用，单调递增）；
- 输出仍按「从上到下、从左到右」排序（agent 易读），但 id 不再随屏幕微变而漂移。

状态快照可 JSON 序列化：CLI 每次是独立进程，靠缓存文件续状态；MCP server 进程
常驻，内存持有 + 落盘双保险（重启后也能续上）。OCR 文本抖动、浮窗时钟变化会让
个别元素匹配失败按新元素处理（编号增长），无害。
"""
import math

from . import fuse
from .model import Element


def _中心距(a, b):
    ax, ay = a.center()
    bx, by = b.center()
    return math.hypot(ax - bx, ay - by)


def _match_score(新, 旧):
    """帧间两元素的匹配分，<=0 视为不可能同元素。

    空间为主（IoU 或中心距离），文本/类型为辅加分：同位置同文本同类型稳配；
    位置吻合但文本变化（如按钮按下后改标题）仍可配；空间不沾边的直接 0 分。
    """
    l1, t1, r1, b1 = 新.bbox
    l2, t2, r2, b2 = 旧.bbox
    iou = fuse._iou(新.bbox, 旧.bbox)
    if iou > 0.4:
        分 = iou * 2.0
    else:
        距 = _中心距(新, 旧)
        # 容差取两元素较小边长的一半与 20px 的较大者：小按钮微动可配，远处的绝不配
        小边 = min(r1 - l1, b1 - t1, r2 - l2, b2 - t2)
        容差 = max(20.0, 小边 / 2.0)
        if 距 > 容差:
            return 0.0
        分 = 0.6 * (1.0 - 距 / 容差)

    if 新.text and 旧.text:
        if 新.text == 旧.text:
            分 += 1.5
        elif 新.text in 旧.text or 旧.text in 新.text:
            分 += 1.0
        else:
            分 -= 0.2  # 文本完全不同轻度扣分，不硬否（OCR 抖动常见）
    if 新.type == 旧.type:
        分 += 0.4
    return 分


# 配对成立的最小分数：纯位置最高 0.6+0.4=1.0，须贴得近且类型同才够
_配对阈值 = 1.0


class IdTracker:
    """跨 scan 的编号追踪器。state 为 None 表示首帧（走自然编号）。"""

    def __init__(self, state=None):
        self._上帧 = []          # 上一帧元素快照 [{id,type,text,bbox,source}]
        self._next_id = 0        # 历史最大编号（新元素从这里递增，永不复用）
        if state and state.get("elements"):
            self._上帧 = list(state["elements"])
            self._next_id = int(state.get("next_id", 0)) or max(
                [e["id"] for e in self._上帧] + [0])

    def apply(self, 元素, row_tol=24, screen_wh=None):
        """对融合后的元素列表做稳定编号，返回统计 dict（元素 id 就地更新）。"""
        # 先按空间排自然编号（首帧即最终编号；有上帧时作为新元素编号候选基准）
        fuse.assign_ids(元素, row_tol=row_tol, screen_wh=screen_wh)

        统计 = {"kept": 0, "new": 0, "lost": 0}
        if not self._上帧:
            统计["new"] = len(元素)
            self._next_id = max([e.id for e in 元素] + [0])
        else:
            旧s = [Element(type=d["type"], text=d.get("text", ""),
                           bbox=list(d["bbox"]), source=d.get("source", "uia"),
                           conf=0.0, clickable=False, id=d["id"])
                   for d in self._上帧]
            # 打全部分数对，贪心配对（分数降序，一旧只配一新）
            对 = []
            for n in 元素:
                for o in 旧s:
                    分 = _match_score(n, o)
                    if 分 >= _配对阈值:
                        对.append((分, n, o))
            对.sort(key=lambda x: -x[0])
            用过新, 用过旧 = set(), set()
            for 分, n, o in 对:
                if id(n) in 用过新 or id(o) in 用过旧:
                    continue
                用过新.add(id(n))
                用过旧.add(id(o))
                n.id = o.id
                统计["kept"] += 1
            统计["lost"] = len(旧s) - len(用过旧)
            for n in 元素:
                if id(n) not in 用过新:
                    self._next_id += 1
                    n.id = self._next_id
                    统计["new"] += 1

        # 输出顺序仍按位置（行容差分行），id 只是标签不连续无妨
        元素.sort(key=lambda e: (e.bbox[1], e.center()[0]))
        self._上帧 = [{"id": e.id, "type": e.type, "text": e.text,
                       "bbox": list(e.bbox), "source": e.source} for e in 元素]
        return 统计

    def snapshot(self):
        """导出可 JSON 落盘的状态快照。"""
        return {"version": 1, "next_id": self._next_id, "elements": self._上帧}
