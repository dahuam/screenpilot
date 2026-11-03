# -*- coding: utf-8 -*-
"""ScreenSpot-Pro 图片下载器（ScreenPilot·施工单 D）。

按 public_sspot.load_pro 同参数抽样（保证考卷与图片一致、可复现），
仅下载抽中题目用到的图片（4K 级截图，按需下载避免整库数 GB）。

通道：hf-mirror.com（resolve 直链 + CDN 跳转，10-25 实测 ~25MB/s）。
用法：python bench/fetch_sspro_images.py [--sample 200] [--seed 42] [--platform windows]
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import os
import time
import argparse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from public_sspot import load_pro, SSPRO_IMG_DIR  # noqa: E402

BASE_URL = "https://hf-mirror.com/datasets/likaixin/ScreenSpot-Pro/resolve/main/images/"


def 下载一张(relpath):
    """下载单张图（幂等：已存在且 >10KB 跳过；urllib 自动跟随 CDN 跳转，3 次重试）。

    坑：subprocess 调 curl 传中文落盘路径会被 GBK/UTF-8 编码坑掉（10-25 实测
    全部静默失败），纯 Python urllib 无此患。
    """
    dst = os.path.join(SSPRO_IMG_DIR, *relpath.split("/"))
    if os.path.isfile(dst) and os.path.getsize(dst) > 10240:
        return None
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    url = BASE_URL + relpath
    for 尝试 in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=120) as resp, open(dst, "wb") as f:
                f.write(resp.read())
            if os.path.getsize(dst) > 10240:
                return relpath
        except Exception:
            time.sleep(2)
    return relpath  # 返回失败清单


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--platform", default="windows")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    题s = load_pro(platform=args.platform, sample=args.sample, seed=args.seed,
                   sample_scale="all" if args.sample == 0 else None)
    图s = sorted({t["img_path"] for t in 题s})
    rels = [os.path.relpath(p, SSPRO_IMG_DIR).replace("\\", "/") for p in 图s]
    print(f"抽样 {len(题s)} 题，去重图片 {len(rels)} 张 → {SSPRO_IMG_DIR}")

    ok = fail = skip = 0
    with ThreadPoolExecutor(max_workers=args.workers) as 池:
        for res in 池.map(下载一张, rels):
            if res is None:
                skip += 1
            elif isinstance(res, str):
                fail += 1
                print("  FAIL:", res)
            else:
                ok += 1
    print(f"完成：新下 {ok} 已有 {skip} 失败 {fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
