# -*- coding: utf-8 -*-
"""sp 包：识屏系统（screenpilot）核心模块集合。

导入本包即设置进程 DPI 感知（物理像素坐标系），
保证「截屏、UIA 坐标、鼠标点击」三者处于同一物理像素坐标系，
避免 Windows 150% 等缩放场景下必然错位的问题。
"""
import ctypes


def enable_dpi_awareness():
    """设置 Per-Monitor V2 DPI 感知；失败则退回系统级 DPI 感知；再失败静默放过。"""
    try:
        # 2 = PER_MONITOR_DPI_AWARE（按显示器 DPI 感知 V1；shcore 接口）
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# 包被导入时立即生效，确保任何引擎初始化之前坐标系已正确
enable_dpi_awareness()
