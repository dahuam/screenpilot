# -*- coding: utf-8 -*-
"""dev60 闸门族基准接线（任务A·闸门族小重构·施工报告附件）。

public_sspot.py 逐字节零改动（红线）；本脚本只做 dev 题集过滤接线：
读 results/ssv2_r2_dev/dev_qids.json（F 组定型留档 60 题），过滤 load_v2()，
其余（run/summarize/匹配器/判分/oracle）全部原样复用。

用法：
  python bench/_dev60_gate.py --tag ssv2_dev60_pre
  python bench/_dev60_gate.py --tag ssv2_dev60_post
"""
import os
import sys
import json
import time
import argparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bench"))

import public_sspot as ps  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="dev60 闸门族基准（sspot v2 固定 60 题）")
    ap.add_argument("--tag", required=True, help="结果目录名")
    ap.add_argument("--matcher", choices=["heuristic", "semantic"], default="semantic")
    args = ap.parse_args()

    qids_path = os.path.join(ROOT, "bench", "results", "ssv2_r2_dev", "dev_qids.json")
    with open(qids_path, encoding="utf-8") as f:
        dev_qids = set(json.load(f))
    题s = [t for t in ps.load_v2() if t["qid"] in dev_qids]
    assert len(题s) == len(dev_qids), f"dev60 题集不全：找到 {len(题s)}/{len(dev_qids)}"
    n_icon = sum(1 for t in 题s if t["ui_type"] == "icon")
    print(f"dev60 题集就绪：{len(题s)} 题（icon {n_icon} / text {len(题s) - n_icon}）")

    matcher = None
    semantic_mod = None
    if args.matcher == "semantic":
        import semantic_matcher as semantic_mod
        matcher = semantic_mod.SemanticMatcher(threads=12)
        print(f"语义匹配器已加载：kind={semantic_mod.MODEL_KIND} "
              f"(短路={semantic_mod.SHORT_CIRCUIT}, 采纳={semantic_mod.SEM_ADOPT})")

    out_dir = os.path.join(ROOT, "bench", "results", args.tag)
    print(f"题量 {len(题s)} → {out_dir}")
    t0 = time.perf_counter()
    jsonl = ps.run(题s, out_dir, args.tag, matcher=matcher, semantic_mod=semantic_mod)
    s = ps.summarize(jsonl, args.tag)
    print(f"\n=== {args.tag} 结果 ===")
    print(f"acc={s['acc']}  oracle={s['oracle_rate']}  acc|oracle={s['acc_on_oracle']}  "
          f"n={s['n']}  归因={s['fail_types']}")
    print("by_ui_type:", json.dumps(s["by_ui_type"], ensure_ascii=False))
    print(f"总墙钟={int(time.perf_counter() - t0)}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
