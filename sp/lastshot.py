# -*- coding: utf-8 -*-
"""最近抓图缓存：两级视觉「裁片通道」的地基（2026-10-28 设计方案施工）。

设计定案（notes/设计方案-两级视觉与文本通道_20261028.md）：
裁片从最近 snap/scan 缓存原图裁、不重截；缓存超龄（画面可能已变）自动重抓再裁。

机制：
- scan / snap 成功后调 register()：原图落 last_shot.png + 状态落 last_shot.json
  （含配套元素缓存路径 elements_json——CLI 与 MCP 的缓存是两份文件，裁片必须
  跟随「最近一次抓图同源」的那份，否则编号错位）。
- crop 前调 load()：状态存在且未超龄 → 直接用缓存原图；
  超龄/缺失 → refetch() 按原方式重抓（有 hwnd 走 PrintWindow 后台抓窗，
  不抢前台；全屏走屏幕拷贝），杜绝「拿旧图裁新画面」的错位。

原图一律原分辨率 PNG 落盘（定案①「存全截屏，根据屏幕分辨率裁片」）。
"""
import json
import os
import time

import numpy as np

from . import capture

# 缓存超龄阈值（秒）：超过则视为「画面可能已变」，crop 自动重抓
MAX_AGE = 60.0

项目根 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
数据目录 = os.path.join(项目根, "mcp_data")
状态路径 = os.path.join(数据目录, "last_shot.json")
原图路径 = os.path.join(数据目录, "last_shot.png")


def register(img, meta=None, elements_json=None, shot_path=None):
    """登记最近一次抓图：原图 + 状态落盘。

    img: BGR ndarray（scan/snap 的原图，非标注图）。
    meta: scan/snap 返回的 meta（取 offset/window/hwnd/source）。
    elements_json: 配套元素缓存绝对路径（scan 时传；snap 无元素表传 None）。
    shot_path: 原图落盘路径（缺省 last_shot.png；CLI snap 已另存用户的图，
        仍需在此落一份标准缓存供 crop 用）。
    """
    meta = meta or {}
    os.makedirs(数据目录, exist_ok=True)
    if shot_path and os.path.abspath(shot_path) != os.path.abspath(原图路径) \
            and os.path.isfile(shot_path):
        # snap 已存用户指定路径：copy 文件到缓存位（比重新编码快且无损）
        try:
            import shutil
            shutil.copyfile(shot_path, 原图路径)
        except Exception:
            capture.save_image(img, 原图路径)
    else:
        capture.save_image(img, 原图路径)
    hwnd = int(meta.get("hwnd") or 0)
    状态 = {
        "png": 原图路径,
        "source": meta.get("source", "scan" if elements_json else "snap"),
        "window": meta.get("window"),
        "hwnd": hwnd,
        "offset": list(meta.get("offset") or [0, 0]),
        "elements_json": elements_json,
        "epoch": time.time(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "size": [int(img.shape[1]), int(img.shape[0])],
    }
    with open(状态路径, "w", encoding="utf-8") as f:
        json.dump(状态, f, ensure_ascii=False, indent=1)
    return 状态


def load(max_age=MAX_AGE):
    """读取未超龄的最近抓图，返回 (img_bgr, 状态dict)；缺失/超龄/损坏返回 None。"""
    if not os.path.isfile(状态路径) or not os.path.isfile(原图路径):
        return None
    try:
        with open(状态路径, "r", encoding="utf-8") as f:
            状态 = json.load(f)
    except Exception:
        return None
    if time.time() - float(状态.get("epoch", 0)) > max_age:
        return None
    try:
        img = capture.load_image(原图路径)
    except Exception:
        return None
    return img, 状态


def refetch(状态):
    """按原方式重抓一张新图（画面可能已变，防旧图错位）。

    有 hwnd：PrintWindow 后台抓窗（不抢前台；黑帧自动回退屏幕拷贝）；
    否则：全屏屏幕拷贝。返回 (img_bgr, offset, window)——offset 取重抓时
    的实时窗口原点（窗口可能被挪动过），调用方用它把屏幕坐标换算到图内。
    """
    hwnd = int(状态.get("hwnd") or 0)
    if hwnd and capture and _窗口活着(hwnd):
        img, rect, _fallback = capture.shot_window_hwnd(hwnd)
        return img, [rect[0], rect[1]], 状态.get("window")
    img = capture.shot_full()
    return img, [0, 0], 状态.get("window")


def _窗口活着(hwnd):
    try:
        return bool(capture.user32.IsWindow(int(hwnd) & 0xFFFFFFFF))
    except Exception:
        return False


def status():
    """读最近抓图状态（不判超龄），返回状态 dict 或 None。"""
    if not os.path.isfile(状态路径):
        return None
    try:
        with open(状态路径, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _取图(force_refresh=False):
    """拿最近抓图：缓存未超龄直接用；超龄/损坏按原方式重抓（防旧图错位）。

    force_refresh=True 跳过缓存直接重抓（核对类调用用：画面刚被动作改变、
    60s 超龄线内也可能拿旧图，2026-11-04 实操体感评测·体验问题4）。

    返回 (img_bgr, offset, used, 状态)——used="cache"|"refreshed"；
    refreshed 时 offset 取重抓时实时窗口原点（窗口可能被挪动过）。
    """
    if not force_refresh:
        缓存 = load()
        if 缓存 is not None:
            img, 状态 = 缓存
            return img, list(状态.get("offset") or [0, 0]), "cache", 状态
    状态 = status()
    if 状态 is None:
        raise RuntimeError("无最近抓图缓存（请先 scan / snap）")
    img, offset, _win = refetch(状态)
    return img, list(offset), "refreshed", 状态


def crop_xy(x1, y1, x2, y2, pad=0, out_path=None, force_refresh=False):
    """按屏幕物理坐标区域裁片（两级视觉·看清通道）。

    坐标系与元素表同源（物理像素）；区域换算到最近一次 scan/snap 的原图内
    裁出，原分辨率不缩放（定案①「存全截屏，根据屏幕分辨率裁片」）。
    缓存超龄自动重抓（refetch）再裁，防「拿旧图裁新画面」错位；
    force_refresh=True 无视缓存直接重抓（画面刚变过的核对类裁片用）。

    返回 (裁片绝对路径, 信息 dict)：used=cache|refreshed、offset、
    shot_time（图的实际拍摄时间）、crop_size=[w,h]。
    """
    img, offset, used, 状态 = _取图(force_refresh=force_refresh)
    ox, oy = offset
    ix1, iy1 = int(min(x1, x2)) - int(pad) - ox, int(min(y1, y2)) - int(pad) - oy
    ix2, iy2 = int(max(x1, x2)) + int(pad) - ox, int(max(y1, y2)) + int(pad) - oy
    h, w = img.shape[:2]
    ix1, iy1 = max(0, ix1), max(0, iy1)
    ix2, iy2 = min(w, ix2), min(h, iy2)
    if ix2 - ix1 < 2 or iy2 - iy1 < 2:
        raise ValueError(
            "裁片区域落在图外或过小（图内区域 [%s,%s,%s,%s]，图 %dx%d，offset=%s）"
            % (ix1, iy1, ix2, iy2, w, h, offset))
    裁 = img[iy1:iy2, ix1:ix2]
    if out_path:
        out = os.path.abspath(out_path)
    else:
        out = os.path.join(数据目录, "crop_xy_%s.png" % time.strftime("%H%M%S"))
    目录 = os.path.dirname(out)
    if 目录 and not os.path.isdir(目录):
        os.makedirs(目录, exist_ok=True)
    capture.save_image(裁, out)
    return out, {"used": used, "offset": offset,
                 "shot_time": 状态.get("time"), "crop_size": [ix2 - ix1, iy2 - iy1]}


def crop_element(el, pad=0, out_path=None, force_refresh=False):
    """按元素 bbox 裁片（两级视觉·看清通道）。

    el：元素 dict（含 bbox）——必须来自与最近抓图同源的元素表
    （last_shot.json 的 elements_json 指向的那份，MCP crop_screen 工具负责
    同源查询；CLI 直接传用户 --file 查出的 dict）。bbox 为屏幕坐标，
    换算图内坐标后裁出，原分辨率不缩放。
    force_refresh=True 无视缓存直接重抓（画面刚变过的核对类裁片用）。
    """
    bbox = el.get("bbox") if isinstance(el, dict) else getattr(el, "bbox", None)
    if not bbox or len(bbox) != 4:
        raise ValueError("元素无有效 bbox（无位置信息的元素不可裁片）")
    l, t, r, b = bbox
    if r - l < 1 or b - t < 1:
        raise ValueError("元素 bbox 尺寸异常: %s" % (bbox,))
    return crop_xy(l, t, r, b, pad=pad, out_path=out_path,
                   force_refresh=force_refresh)
