# XuLan ScreenPilot

> Turn the real Windows screen into a structured element table your AI agent can operate.

[English](README.md) | [简体中文](README.zh-CN.md)

**XuLan ScreenPilot** solves the two classic pain points of GUI automation for AI agents — *not seeing the elements* and *missing the click* — by giving the agent a numbered element table instead of a screenshot to guess coordinates from. The agent scans once, reads the table, and drives the screen by element id (`click` / `type`), in a closed loop.

Pure local CPU. No cloud model, no GPU, no API key.

## How it works

Three engines run in parallel and fuse into one element table (priority `UIA > OCR > visual`):

| Engine | Source | Covers |
|---|---|---|
| `uia` | Windows UIA accessibility tree | Native apps, browsers (incl. web a11y tree) — precise and fast |
| `ocr` | RapidOCR (onnxruntime, CPU) | Games and custom-drawn UI with no control tree; full-screen text fallback |
| `visual` | OpenCV connected components + shape detectors | Icon candidates; dedicated channels for checkboxes / radio dots / sliders / close buttons |

Fusion dedupes by center-containment or IoU ≥ 0.3, numbers elements top-to-bottom / left-to-right, and produces:

- `elements.json` — the element table cache (queried by id for click/type)
- `annotated_scan.png` — annotated screenshot (red = UIA, blue = OCR, green = visual, id printed on each box)

On top of the table:

- **Frame-to-frame id stability** — rescan keeps the same element on the same id; new elements get new ids (`meta.tracker` reports kept/new/lost).
- **Background capture** — grab a covered window's real pixels via `PrintWindow` (no need to raise it).
- **Drifted clicks** — physical clicks land with Gaussian jitter and move trails, so automation doesn't look robotic; `precise=True` restores pixel accuracy.
- **Stacking awareness** — every coordinate click reports which top-level window it actually hit (`hit_window`), and can refuse clicks on occluded targets (`top_must`).
- **Two-level vision + text channel** — crop any element at native resolution to read fine text/icons; a SigLIP vector book lets you find UI elements by name or by image without rescanning.
- **Time dimension** — burst watch for toasts/transient feedback, show-verify for typing, ESC probe for hangs, and static/dynamic separation (UI base layer vs. moving entities) across frames.
- **Business-level waits** — `expect {find, mode: appear|disappear}` makes a click "successful" only when the business evidence shows up on screen.
- **Session cleanup** — a per-session ledger tracks windows the agent launched; `cleanup_windows` closes them at task end (WM_CLOSE only, six-gate safety chain, no taskkill); a crashed session leaves a residual report that the next session auto-surfaces.

## Install

Requirements: Windows 10/11, Python 3.11+, CPU only.

```bat
git clone <repo-url> screenpilot && cd screenpilot
pip install uiautomation rapidocr-onnxruntime onnxruntime opencv-python numpy pillow pyperclip pyautogui mcp
python screenpilot.py scan                       :: full-screen scan, prints the element table
```

Tested on Python 3.13. The optional element-book vector channel and the benchmark matcher additionally need `tokenizers` and the SigLIP int8 ONNX weights under `SCREENPILOT_MODELS_DIR`.

Optional: the semantic matcher and the element-book vector channel use SigLIP int8 ONNX models. Set `SCREENPILOT_MODELS_DIR` to a local folder holding the model directories (see [README.zh-CN.md](README.zh-CN.md) §两级视觉与文本通道). Datasets for the public benchmark go under `SCREENPILOT_DATASETS_DIR`.

## Quickstart: the agent closed loop

```python
# MCP tools (stdio server) or the CLI expose the same verbs:
scan_screen(window="记事本")        # 1. scan -> numbered element table
click_element(element_id=7)        # 2. act by id (UIA control-level when possible)
type_text(element_id=3, text="hi") # 3. type into an edit field
click_safe(element_id=12, expect={"find": "设置", "mode": "appear"})   # 4. act only when evidence shows
watch_burst(window="记事本", seconds=3)   # 5. catch transient feedback frames
```

MCP config (e.g. Claude Desktop / ZCode):

```json
{"mcpServers": {"screenpilot": {
  "command": "python",
  "args": ["<path>/screenpilot/mcp_server.py"],
  "env": {"PYTHONIOENCODING": "utf-8"}
}}}
```

## Benchmarks

Public GUI-grounding benchmarks, frozen matcher, official scoring. Pure CPU on a desktop with a GTX 1070 Ti (GPU unused):

| Benchmark | Scope | acc | oracle* | Latency |
|---|---|---|---|---|
| **ScreenSpot-v2** | full 1,272 questions | **49.61%** | **88.36%** | 1.94 s/question |
| **ScreenSpot-Pro** | Windows, stratified 200 questions (4K-heavy) | **18.00%** | **78.5%** | 3.87 s/question |

\* oracle = the GT target has a recognizable candidate in the element table — an upper bound assuming a perfect matcher. It separates the *recognition engine's* account from the *matcher's* account: on ScreenSpot-Pro the gap is almost entirely matcher-side (`match_error` 88% of misses), which is where the roadmap points. The `acc` numbers are with a frozen heuristic matcher standing in for the agent; in real use the agent reads the table itself, so live experience tracks the oracle line more than acc. We report both, no cherry-picking.

Reproduce: `python bench/public_sspot.py --dataset v2` / `--dataset pro` (datasets not included; see the docstring for the two HF sources).

## MCP server

20 tools over stdio (`scan_screen`, `scan_image`, `click_element`, `click_safe`, `click_xy`, `type_text`, `press_key`, `scroll_screen`, `snap_screen`, `crop_screen`, `crop_xy`, `book_find`, `watch_burst`, `verify_typing`, `probe_hang`, `split_static_dynamic`, `window_stack`, `activate_window`, `register_window`, `cleanup_windows`). `scan_image` runs the same engines offline on a static file; `book_find` queries the persistent element book (two-level: text match + SigLIP vector search) so recurrent UIs are found without a full rescan.

**Session cleanup** (register-then-clean, `sp/cleanup.py`): the server opens a session ledger on start; `register_window` logs windows the agent launched itself, and the six state-changing tools auto-log windows newly opened by an action (advisory only). At task end, `cleanup_windows` closes ledger windows gently — WM_CLOSE only, no taskkill anywhere — behind a six-gate safety chain: identity re-check (hwnd+pid), permanent whitelist (explorer / task manager / own process), own-session ledger only, cross-session windows report-only, dirty-content protection (unsaved work is never closed), and survivor re-check that reports instead of escalating. If a session dies, a residual report is written and the next session's first tool call surfaces it automatically.

## Documentation

Full docs (in Chinese, with internals, known limitations, and the test-suite map): [README.zh-CN.md](README.zh-CN.md).

## Status & roadmap

Working system in daily use as the studio's screen channel. Known gaps are documented, not hidden: dark-theme professional apps (deep-background small text) hurt OCR recall, 4K toolbar icons stress the matcher, and the Pro benchmark score is capped by the frozen matcher — see [README.zh-CN.md](README.zh-CN.md) §已知限制与坑.

## License

Open core: the core engine in this repository is released under the [Apache License 2.0](LICENSE). Commercial value-added integrations (closed source) are not part of this repository.

## Acknowledgements

Built on [RapidOCR](https://github.com/RapidAI/RapidOCR), [uiautomation](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows), OpenCV, and onnxruntime.
