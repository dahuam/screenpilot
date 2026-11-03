# -*- coding: utf-8 -*-
"""截屏模块：屏幕拷贝走 PyAutoGUI（底层 PIL ImageGrab），后台抓窗走 Win32 PrintWindow。

统一返回 BGR ndarray（OpenCV 惯例）；进程已设 DPI 感知，
截图为物理分辨率，与 UIA 坐标、鼠标点击同坐标系。
"""
import ctypes

import numpy as np
import pyautogui

# 关闭鼠标移到屏幕角落即中止的 fail-safe，避免命令行自动化误触发抛异常
pyautogui.FAILSAFE = False


def screen_size():
    """返回物理像素屏幕尺寸 (宽, 高)。"""
    return pyautogui.size()


def load_image(path):
    """从图片文件读入 BGR ndarray（静态图识别入口，统考/离线图用）。

    坑：cv2.imread 在 Windows 上对中文/非 ASCII 路径会静默失败返回 None——
    统一走 np.fromfile + cv2.imdecode（Unicode 路径安全，与 save_image 对偶）。
    """
    import os
    import cv2
    if not os.path.isfile(path):
        raise FileNotFoundError("图片不存在: %s" % path)
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("图片解码失败（不支持的格式或文件损坏）: %s" % path)
    return img


def shot_full():
    """全屏截图，返回 BGR ndarray。"""
    img = pyautogui.screenshot()  # PIL Image，RGB
    return np.asarray(img)[:, :, ::-1].copy()  # RGB 转 BGR


def shot_region(l, t, r, b):
    """按物理像素矩形 [l, t, r, b] 区域截图（用于窗口截图），返回 BGR ndarray。"""
    img = pyautogui.screenshot(region=(int(l), int(t), int(r - l), int(b - t)))
    return np.asarray(img)[:, :, ::-1].copy()


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


# 64 位进程坑：HDC/HBITMAP 是 64 位句柄，windll 默认按 32 位 c_int 传参会截断
# 高位——必须显式设 argtypes/restype（幂等，重复赋值无害）。
user32 = ctypes.windll.user32
gdi = ctypes.windll.gdi32
user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
user32.PrintWindow.restype = ctypes.c_int
user32.GetWindowDC.argtypes = [ctypes.c_void_p]
user32.GetWindowDC.restype = ctypes.c_void_p
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.ReleaseDC.restype = ctypes.c_int
gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi.CreateCompatibleDC.restype = ctypes.c_void_p
gdi.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
gdi.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi.SelectObject.restype = ctypes.c_void_p
gdi.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                          ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_uint32]
gdi.GetDIBits.restype = ctypes.c_int
gdi.DeleteObject.argtypes = [ctypes.c_void_p]
gdi.DeleteObject.restype = ctypes.c_int
gdi.DeleteDC.argtypes = [ctypes.c_void_p]
gdi.DeleteDC.restype = ctypes.c_int


def window_rect_hwnd(hwnd):
    """按窗口句柄取屏幕矩形 [l, t, r, b]（物理像素，DPI 感知进程内坐标正确）。

    坑：UIA NativeWindowHandle 可能给出超 int32 的值（高位为 1 的 HWND 被
    当作大正整数传回），ctypes 传参会 OverflowError——统一按无符号 32 位规范。
    """
    hwnd = int(hwnd) & 0xFFFFFFFF
    rc = RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rc)):
        raise RuntimeError("GetWindowRect 失败（句柄无效？）: hwnd=%s" % hwnd)
    return [rc.left, rc.top, rc.right, rc.bottom]


def shot_window_hwnd(hwnd, fallback_screen=True):
    """PrintWindow 后台抓窗：窗口被遮挡/不在前台也能抓到自身画面（非前台方式）。

    PW_RENDERFULLCONTENT(2) 要求 Win 8.1+，对 CEF/硬件加速窗口也大多有效；
    对部分 UWP/全屏 D3D 窗口可能产出黑帧或失败——黑帧或异常时按
    fallback_screen 回退屏幕区域拷贝（若此时窗口被遮挡，混入遮挡内容，
    由调用方通过返回的 fallback 标记向 agent 提示）。

    返回 (img_bgr, rect[l,t,r,b], fallback: bool)。
    """
    hwnd = int(hwnd) & 0xFFFFFFFF
    if not ctypes.windll.user32.IsWindow(hwnd):
        raise RuntimeError("无效窗口句柄: %s" % hwnd)
    rect = window_rect_hwnd(hwnd)
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    if w <= 0 or h <= 0:
        raise RuntimeError("窗口尺寸异常: %s" % rect)
    user32, gdi = ctypes.windll.user32, ctypes.windll.gdi32
    hdc窗口 = user32.GetWindowDC(hwnd)
    hdc内存 = gdi.CreateCompatibleDC(hdc窗口)
    h位图 = gdi.CreateCompatibleBitmap(hdc窗口, w, h)
    img = None
    fallback = True
    try:
        gdi.SelectObject(hdc内存, h位图)
        # 2 = PW_RENDERFULLCONTENT：含 DirectComposition/硬件加速内容
        ok = user32.PrintWindow(hwnd, hdc内存, 2)
        if ok:
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h  # 负高：自上而下行序
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0  # BI_RGB
            buf = ctypes.create_string_buffer(w * h * 4)
            行数 = gdi.GetDIBits(hdc内存, h位图, 0, h, buf, ctypes.byref(bmi), 0)
            if 行数 == h:
                arr = np.frombuffer(buf.raw, dtype=np.uint8).reshape(h, w, 4)
                img = arr[:, :, :3].copy()  # BGRA -> BGR
    finally:
        gdi.DeleteObject(h位图)
        gdi.DeleteDC(hdc内存)
        user32.ReleaseDC(hwnd, hdc窗口)

    if img is not None:
        # 黑帧判定：几乎全黑且无任何纹理（PrintWindow 对部分 UWP/D3D 的典型失败态）
        if float(img.std()) > 2.0 or float(img.mean()) > 8.0:
            return img, rect, False

    if fallback_screen:
        img = shot_region(*rect)
        return img, rect, True
    raise RuntimeError("PrintWindow 后台抓取失败（黑帧/无效），且未启用屏幕回退")


def save_image(img_bgr, path):
    """把 BGR ndarray 保存为图片文件。

    注意坑：cv2.imwrite 在 Windows 上对中文/非 ASCII 路径会静默失败，
    故用 cv2.imencode + ndarray.tofile（Unicode 路径安全）。
    """
    import os
    import cv2
    扩展名 = os.path.splitext(path)[1] or ".png"
    ok, buf = cv2.imencode(扩展名, img_bgr)
    if not ok:
        raise RuntimeError("图像编码失败: %s" % 扩展名)
    buf.tofile(path)
    return path
