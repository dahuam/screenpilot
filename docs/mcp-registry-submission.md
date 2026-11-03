# MCP 官方注册表提交材料（发布周备料 · 2026-11-03）

> 用途：XuLan ScreenPilot 发布周（11-09 那周）向 MCP 官方注册表提交 server 时的材料包。
> 三名板占位符全部落地（2026-11-04）：GitHub org/user=`dahuam`（2026-11-04 登录确认），许可证=Apache-2.0。材料就绪，提交前按 §3 清单打勾即可。

## 1. server.json（MCP registry manifest，规范版）

放在仓库根（registry 现行规范为 server.json，字段以提交当日
https://github.com/modelcontextprotocol/registry 的 schema 为准核对）：

```json
{
  "$schema": "https://cdn.jsdelivr.net/npm/@modelcontextprotocol/sdk/server.json",
  "name": "io.github.dahuam/screenpilot",
  "display_name": "XuLan ScreenPilot",
  "description": "Turn the real Windows screen into a numbered element table your AI agent can operate. UIA + OCR + visual fusion, pure local CPU, no cloud model. 18 MCP tools: scan, click by id with occlusion checks, typed waits with business-evidence verification, transient-feedback burst watch, and a persistent element book with SigLIP vector search.",
  "version": "1.0.0",
  "packages": [
    {
      "registry_type": "oci",
      "identifier": "ghcr.io/dahuam/screenpilot",
      "version": "1.0.0"
    }
  ],
  "remotes": [],
  "experimental": {},
  "license": "Apache-2.0",
  "repository": {
    "url": "https://github.com/dahuam/screenpilot",
    "source": "github"
  }
}
```

注意：
- `packages` 若不发 OCI 镜像，可换成 pip（PyPI 包发布后）或直接删节（registry 接受纯 GitHub repo 指针的提交形态，以当日 schema 为准）。
- `name` 的 `io.github.dahuam` 前缀是 registry 对 GitHub 来源 server 的命名约定。

## 2. 提交 PR 流程（模型上下文注册表）

1. fork `modelcontextprotocol/registry`，在 `servers/` 下按当日模板加 `screenpilot.json`（或按新流程走 registry API 提交）。
2. PR 描述直接复用 §4 的英文一句话+特性清单。
3. CI 校验过 schema 后等 review；若要求演示，附 bench 报告与 README 截图。

## 3. 发布前置检查清单（提交前逐项打勾）

- [x] 三名板落地进度：对外名=XuLan ScreenPilot ✅；LICENSE=Apache-2.0 已落库（open core）✅；GitHub org/user=`dahuam` 已确认回填（2026-11-04 登录确认）✅——**三名板全部落地**
- [ ] README.md（英）/README.zh-CN.md（中）链接互通、License 节占位符替换
- [ ] 敏感复扫归零（11-03 已做：faef00e + 当窗补刀两处；发布前最后一跑 `git ls-files` 面按敏感词清单 grep——词表在操作侧凭据区，不入本仓库）
- [ ] bench 数字可复现：`SCREENPILOT_DATASETS_DIR` 指向数据集后跑 `bench/public_sspot.py`（数据集不入库）
- [ ] 版本号统一：SERVER_INFO.version / server.json / pyproject（如做 PyPI）三处一致
- [ ] demo GIF/截图：标注图（红蓝绿框+编号）是最直观卖点，录一段「扫→按编号点→expect 判定」闭环

## 4. 英文一句话（PR / 社媒通用）

> XuLan ScreenPilot is an MCP server that turns the real Windows screen into a numbered, structured element table by fusing the UIA accessibility tree, CPU OCR, and OpenCV vision — so agents stop guessing pixel coordinates. It adds frame-stable element ids, occlusion-aware clicking, business-evidence waits, transient-feedback capture, and a persistent element book with vector search. Pure local CPU, no cloud model. Benchmarked on ScreenSpot-v2/Pro (oracle 88.36% / 78.5%).

## 5. 对外名考古（三名板第一项依据）

- 2026-10-23 定名铁律：「记得这是 ScreenPilot」——识屏系统所有产出物（README/MCP server 名与描述/代码文件头）一律带 ScreenPilot 署名；MCP server 命名定为 `xuanlan-screenpilot`。
- 2026-11-03 深夜拍板：对外名升格 **XuLan ScreenPilot**（含 XuLan 署名）；许可证=open core（开源核心侧 Apache-2.0 已落，增值集成闭源不入库）；GitHub 发布账号已定（详情在凭据文件）。
- 署名口径（当窗定）：行文署名一律全名 XuLan ScreenPilot；技术 slug（MCP 实例名 xuanlan-screenpilot / 仓库名与包名 screenpilot / io.github.<owner>/screenpilot）保留小写不动。
