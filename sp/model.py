# -*- coding: utf-8 -*-
"""元素数据模型：三源（UIA/OCR/视觉）统一的结构化元素表。"""
from dataclasses import dataclass, field

# 元素来源常量
SOURCE_UIA = "uia"        # UI Automation 控件树
SOURCE_OCR = "ocr"        # RapidOCR 文字识别
SOURCE_VISUAL = "visual"  # OpenCV 连通域图标候选


@dataclass
class Element:
    """屏幕上的一个可操作元素。

    坐标一律为物理像素（DPI 感知进程内），bbox 格式 [l, t, r, b]。
    """

    type: str                 # button/edit/text/checkbox/combobox/item/link/menu_item/tab/tree_item/window/icon/...
    text: str                 # 元素文本/名称（OCR 融合后可能补全）
    bbox: list                # [l, t, r, b] 物理像素
    source: str               # uia | ocr | visual
    conf: float               # 置信度 0~1（UIA 有名为 1.0，视觉为低置信 0.3）
    clickable: bool           # 是否可点击
    id: int = 0               # 融合后统一编号，从 1 开始；0 表示未编号
    hwnd: int = 0             # 所属顶层窗口句柄（仅 UIA 元素；后台点击时树内重定位用）

    def center(self):
        """返回中心点 (x, y)。"""
        l, t, r, b = self.bbox
        return (l + r) // 2, (t + b) // 2

    def to_dict(self):
        """序列化为 JSON 友好的字典（字段顺序固定，便于 agent 解析）。"""
        d = {
            "id": self.id,
            "type": self.type,
            "text": self.text,
            "bbox": list(self.bbox),
            "source": self.source,
            "conf": round(float(self.conf), 3),
            "clickable": bool(self.clickable),
        }
        if self.hwnd:
            d["hwnd"] = int(self.hwnd)
        return d


def element_from_dict(d):
    """从缓存 JSON 字典重建 Element（用于 click/type 时查缓存）。"""
    return Element(
        type=d.get("type", "generic"),
        text=d.get("text", ""),
        bbox=list(d.get("bbox", [0, 0, 0, 0])),
        source=d.get("source", "uia"),
        conf=float(d.get("conf", 0.0)),
        clickable=bool(d.get("clickable", False)),
        id=int(d.get("id", 0)),
        hwnd=int(d.get("hwnd", 0) or 0),
    )
