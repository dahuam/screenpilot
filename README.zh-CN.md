# 识屏系统 XuLan ScreenPilot

> **XuLan ScreenPilot** · 把 Windows 真实屏幕变成 AI agent 可操作的结构化元素表。

[English](README.md) | [简体中文](README.zh-CN.md)

把 Windows 真实屏幕变成 AI agent 可操作的结构化元素表。解决 agent 操作真实屏幕时
「看不见元素、点不准坐标」的痛点：agent 不再需要看图猜坐标，而是拿到一张带编号的
元素表，按编号执行 `click` / `type`，闭环操作。

## v1.0.0 变更亮点（2026-11-09）

- **compact 扫描档（默认开）**——编号分配前滤掉空文本非交互壳与重复标签：典型场景
  元素数与提示词字节省约 23%，交互控件零丢失；`compact=false` 回全量表，
  `meta.compact_removed` 报滤除数。
- **隐私默认不入库**——记事本/聊天/密码登录类敏感窗口照常识别，但默认不写入元素书库
  （`meta.privacy_skip`）；显式 `book=true` 才放行。
- **挂死窗不挂 agent**——目标窗口无响应约 2 秒内结构化报错（消息泵探测），不再阻塞
  30 秒+；UIA 调用超时可设，COM 失败分类计数进 `meta.errors.uia_stats`。
- **expect 语义升级**——业务等待超时不再两义 `ok=false`：`action_executed` 区分
  「动作已发出但业务证据未出现」与「动作本身失败」，危险按钮不会因误判被重复点击。
- **裁片强制刷新**——`crop_screen`/`crop_xy` 支持 `force_refresh`，核对动作后的画面
  不再被 60 秒缓存坑。

## 原理：三引擎融合（纯本机 CPU，不依赖大模型）

| 引擎 | 来源 | 覆盖场景 |
|---|---|---|
| `uia` | Windows UIA 控件树（无障碍接口） | 原生应用、浏览器（含网页无障碍树），又准又快 |
| `ocr` | RapidOCR（onnxruntime CPU 推理） | 游戏 / 自绘界面等无控件树场景，全屏认字兜底 |
| `visual` | OpenCV 连通域 + 形状识别族 | 图标候选兜底 + **形状专项**（见下） |

OCR 引擎在 det 主路之外带**单字符 CC 兜底路**（2026-11-02 F4）：det(DBNet)
对孤立单字符全字号盲（计算器显示区读数、计数角标等漏检的根因），det 出框后
由分块连通域提候选、rec 单独识别补齐——双档 conf+ASCII 短 token 白名单控制
误报，验收=`_test_darkocr.py` 6/6。

OCR 模型**启动预热**（2026-11-02 F5）：双实例加载实测 1121ms 是冷首扫固定
大头，`ocr_engine.preload()` 在 MCP server / CLI 入口后台预加载（与参数解析、
截图准备并行），冷首扫只付推理账。rec 提速三案（batch 加大 / batch=1 /
长条宽高分桶）经固定屏受控实验**证伪**（收益依赖当屏框宽高比分布不普适，
且有踩线框 conf 抖动回归面），模型已用 v3 mobile 最小规格、det min=400
再降伤小字——重屏 OCR 慢的真身是框数与长条比（屏型依赖），不设普适开关。

三源结果按 **UIA > OCR > 视觉** 优先级融合去重（中心互含或 IoU≥0.3 视为同一元素），
统一编号（从上到下、从左到右），输出：

- `elements.json` —— 元素表缓存（供 click/type 按编号查询）
- `annotated_scan.png` —— 标注图（红框=UIA、蓝框=OCR、绿框=视觉，编号标在框左上角）

三引擎在 `sp/pipeline.py` 中**并行执行**（UIA 独立 COM 线程 / OCR+视觉线程 / 主线程
截图三方并行），全屏扫描提速约 39%。

### 形状识别族（visual 引擎内的专项通道）

「UIA 不暴露 + OCR 无文本」的图形控件由 visual 引擎内的专项通道兜底：

| 通道 | 认什么 | 产出 |
|---|---|---|
| X 专项（`visual_engine`） | 关闭按钮 ×（色块/细线双通道 + 四闸门 + 四臂验证） | text=`×` |
| 模板匹配（`template_engine`） | 固定像素结构图标：窗口三键、后退/刷新、标签页 × 等 | text=语义名（如「关闭」「刷新」） |
| 形状通道（`visual_engine`） | 勾选框 ☐☑、单选圆点 ○◉、滑块（轨道+钮） | text=`☐☑○◉/滑块` |

- **模板库**：`sp/shape_templates/`（manifest.json + png），增量添加：
  `python -c "from sp import template_engine as t; t.add_template('名字', 裁块BGR, tag='来源')"`
  ——同名多模板共存（暗/亮主题各一副），manifest 可微调每模板阈值（默认 0.93）。
- **防误报分层**：形状语义元素过三道闸——UIA 密度闸（富 UIA 窗口 ≥60 元素自动静音，
  图标与表单控件在富 UIA 场景纯误报源）、标签伴生闸（表单控件必有文本标签伴生，
  游戏装饰小框/环不陪）、融合层容器吞并保护（conf≥0.5 不被无名大容器吞）。
- **误报纪律**：宁可少认不可乱认——程序化靶 + bench 真图 + 官服实战三层验证全绿才出闸。

## 安装

```bash
pip install uiautomation pyperclip pyautogui pillow opencv-python numpy rapidocr-onnxruntime mcp
```

（本机前四后二均已装好，实际只需新装 `uiautomation pyperclip mcp`；**mcp 必须钉 1.x**，2.x 把 FastMCP 改名 MCPServer）

## 用法（agent 闭环协议）

```bash
# ① 扫描：生成元素表 + 标注图（三引擎并行；连续扫描自动保号）
python screenpilot.py scan                    # 扫全屏（默认不入书库；--book 显式入库）
python screenpilot.py scan --win 记事本       # 只扫标题含关键词的窗口（自动置前；默认入书库）
python screenpilot.py scan --win 靶窗口 --bg  # 后台模式：不置前，PrintWindow 抓窗（被遮挡也真实）
python screenpilot.py scan --hwnd 0x1a2b --bg # 按窗口句柄直定位（优先于 --win）
python screenpilot.py scan --incremental      # 强制增量（默认已智能：同窗口 60s 内连续扫自动增量）
python screenpilot.py scan --full             # 强制全量（基准测量/怀疑增量失真时用）
python screenpilot.py scan --no-book          # 本次不写元素书库（临时/敏感屏幕用）
python screenpilot.py scan --book             # 全屏扫描也写入书库（默认全屏不入库）

# ② 窗口切换（扫/点前把目标窗口拉到前台）
python screenpilot.py activate --win 计算器   # SetActive 置前，返回窗口名与矩形

# ③ 按编号操作（坐标从 elements.json 缓存查询）
python screenpilot.py click 21                # 点击 21 号元素（物理点击默认漂移落点）
python screenpilot.py click 21 --physical     # 跳过控件级调用直接物理点击
python screenpilot.py click 21 --precise      # 像素级精准落点（关闭漂移）
python screenpilot.py type 21 "你好世界"      # 向 21 号输入框输入文本（剪贴板方式，中文安全）
python screenpilot.py key 回车                # 按键，支持中文键名（回车/esc/上/下/删除等）与 ctrl+s 组合
python screenpilot.py scroll down 600         # 滚动
python screenpilot.py snap out.png            # 仅截屏（--win 指定窗口；--bg 后台抓窗）

# ④ 界面变化后重新 scan——同元素编号自动保持（新元素追加新号，meta.tracker 报 kept/new/lost）
python screenpilot.py scanimg 图片.png   # 静态图片识别（OCR+视觉，统考/离线图；UIA 不参与）
```

**静态图片识别（2026-10-25）**：`scanimg` / `sp.pipeline.scan_image(path)` 对图片文件跑
OCR+视觉双引擎，输出与活屏扫描同构的元素表（`meta.source="image"`，bbox=图像像素
坐标）；中文路径安全。公开统考（ScreenSpot-v2 全量 44.81% / ScreenSpot-Pro Windows
抽样）即走此通道，见 `bench/公开统考首考报告_20261025.md` 与 `bench/public_sspot.py`。

所有子命令输出单行 JSON（`{"ok": true/false, ...}`），便于 agent 程序化解析。

## 三项进阶能力（2026-10-23 二期）

### 编号稳定性（帧间追踪）
连续两次 `scan` 之间同一元素**保持编号不变**：`sp/tracker.py` 按上一帧快照做
匹配（IoU + 中心距离 + 文本/类型相似度），匹配上的沿用旧号，新元素追加新号
（编号永不复用），输出仍按位置排序。状态按**窗口分桶**（CLI 内嵌 `elements.json`
的 `tracker` 字段、MCP 存 `mcp_data\track_state.json`），换窗口扫描互不干扰，
server 重启也能续链。实测：计算器连点 7×8 后重扫，四按钮编号纹丝不动（kept=84）。

### 后台抓窗（非前台）
`--bg` / MCP `background=true`：窗口**不置前**、被遮挡也能抓到真实画面——截图走
Win32 `PrintWindow`（PW_RENDERFULLCONTENT，窗口自身绘制到内存位图），UIA 读树
本就不受遮挡影响。黑帧自动回退屏幕拷贝并以 `bg_fallback` 提示；OCR/视觉兜底
覆盖无控件树窗口（游戏）。被遮挡时的按编号点击走「窗口树内 bbox 重定位 +
控件级调用」（不发鼠标，防误点遮挡物），实测 tk 靶被全屏浏览器盖住仍能扫出
按钮并发出调用。⚠️ 物理点击永远是打在屏幕坐标上的——被遮挡窗口的物理点击
会点到遮挡物，后台操作必须用控件级路线。

### 漂移点击（不过度精准）
物理点击默认模拟人手：落点在元素中心附近做**高斯随机偏移**（σ 与元素尺寸挂钩，
限制在 bbox 内缩 25% 安全区，绝不点出界），鼠标移动带 0.06~0.20s 随机时长拖尾。
`--precise` / MCP `precise=true` 恢复像素级精准；环境变量 `SP_NO_JITTER=1` 全局
关闭、`SP_JITTER=1.5` 调幅度。UIA 控件级调用（invoke/SetValue）不经鼠标，不受影响。

## MCP server（xuanlan-screenpilot）—— agent 直接挂载

`mcp_server.py` 把二十件工具包成 MCP server（stdio 传输，直 import `sp` 引擎包，OCR 模型
进程内常驻）。ZCode 已配置（`~/.zcode/cli/config.json`
→ `mcp.servers.xuanlan-screenpilot`），会话启动即挂载。

| MCP 工具 | 对应 CLI | 说明 |
|---|---|---|
| `scan_screen` | scan | 扫屏幕/窗口 → 元素表 + 标注图路径（可读图核对）；`background=true` 后台抓窗、`hwnd` 句柄直定位；连续扫描自动保号；标注图上/左边缘印屏幕坐标刻度轴；**`incremental` 三态**：缺省=智能（同窗口 60s 内连续扫自动增量，meta 报 `incremental_auto`）、`true` 强制增量（差分只重扫变化区，滚动场景平移对齐复用）、`false` 强制全量；**`book` 三态**：缺省=智能（窗口扫描入库、全屏不入库）、`true` 强制入库、`false` 本次不写书库（临时/敏感屏幕用） |
| `activate_window` | activate | 按标题子串激活（置前）窗口，返回窗口名与矩形 |
| `window_stack` | stack | 窗口 Z 序全景：谁在前台、目标窗被谁遮挡（`occlude_hwnd` 量化可见率）；分不清前台/遮挡时先查它 |
| `click_element` | click | 按编号点击（`physical=true` 强制物理点击；`precise=true` 关漂移；`expect` 业务生效判定；被遮挡自动拦截并报 `top_window`，`check_top=false` 强点） |
| `click_safe` | — | click_element 安全版：`expect` 业务生效判定**必填** + 落点归窗核验恒开——点完必须用业务证据判定成败，超时如实失败（2026-10-29 新增） |
| `click_xy` | clickxy | 按屏幕物理坐标直点——元素表漏识目标兜底（`precise=true` 关漂移；坐标从 snap/标注图读取，与元素表同坐标系；`expect` 业务生效判定，须带 window/hwnd；结果附 `hit_window` 点到了谁；`top_must` 事前拦截错窗） |
| `type_text` | type | 按编号输入文本（中文安全；`precise=true` 聚焦点击关漂移；`expect` 业务生效判定） |
| `press_key` | key | 按键，支持中文键名与 `ctrl+s` 组合 |
| `scroll_screen` | scroll | 滚动 |
| `snap_screen` | snap | 截屏存 `mcp_data\`（`background=true` 后台抓窗），返回路径供读图 |
| `crop_screen` | crop | 按元素编号裁**原分辨率局部图**——整屏图在视觉编码端被摊薄，裁片逐字可读；从最近 scan 同源原图裁，超龄自动重抓 |
| `crop_xy` | cropxy | 按物理坐标区域裁片（看清通道兜底，元素表漏识目标）；坐标系与 `click_xy` 同源 |
| `book_find` | find | 检索元素书库（文本通道）：scan 自动入库 `manual/<应用域>/`，按名查 bbox 直达目标不重扫；无参数返回域清单 |
| `watch_burst` | watch | **连拍窗**（时间维度）：动作后 0~N 秒逐帧差分抓 toast/飘字等瞬时反馈；`action` 支持 click/key/paste/wait（也接受数组）；`rebase` 动作后重抓基线吸收点击浮雕 |
| `verify_typing` | verify | **一隐一显**：动作前抓基线，动作后短窗看 `region`（建议给回显区）是否变化——判定输入是否生效；`ocr_check=true` 附变化前后 OCR 文本 |
| `probe_hang` | hang | **卡死探测**：发键（默认 esc）短窗看内容变化，零变化=判卡死。必须屏幕通道——PrintWindow 对挂起窗**永久阻塞**（实测大坑），挂死场景别用 scan/snap；要求窗口前台无遮挡，置前失败/可见率<0.95 如实拒绝判定 |
| `split_static_dynamic` | split | **抠底动静态分层**：多帧逐像素中位数抠 UI 底图 + 时间轴标准差抠动态掩码 + 跨帧跟踪 + 净位移分层（实体/动画层）；`verify_ocr=true` 附 UI 文本保留率/稳定框误收量化 |
| `register_window` | — | **登记窗口进会话账本**（收台机制·显式登记）：自己拉起的应用窗登记后，任务收尾 `cleanup_windows` 或会话收口自动 WM_CLOSE 温和关闭；`close_candidate=false` 只入账不自动关。不登记的窗永远不碰（2026-11-06 新增） |
| `cleanup_windows` | — | **收台清理**（任务收尾调用）：按会话账本温和关窗，全程 WM_CLOSE 无 taskkill；六门安全链（身份三验→白名单永免→只认本会话账本→跨会话只报不关→脏窗只报不关→幸存者复判绝不强杀）；`targets` 可指定窗，`force=true` 跳过脏窗保护（白名单窗 force 也不碰）；关不掉的写残窗报告 `mcp_data/residual_report.json`（2026-11-06 新增） |

agent 协议：`scan_screen` 拿编号表（需要窗口在前台时先 `activate_window`）→
按编号 `click_element` / `type_text`（物理点击默认漂移落点）→ 界面变了重新
`scan_screen`（同元素编号自动保持）。窗口被遮挡时用 `background=true` 后台抓窗——注意后台图是窗口自身渲染（后台真相），物理点击打的是前台堆叠，其 `meta.occlusion` 给出该窗当前可见率，点击前先置前。
元素表漏识的目标（如游戏自绘「×」关闭按钮，visual 引擎已带专项识别）：从
`snap_screen` 截图或标注图读出目标像素坐标后 `click_xy` 直点。

### 时间维度四件（2026-11-01 B1 转正）

`scan_screen` 抓的是「此刻」；闪过的 toast、输入回显、窗口卡死、游戏动态层都是
「时间段」问题——四件套补这一维（POC 2026-10-27 真靶成绩：连拍 10/10、一隐一显
20/20、ESC 10/10 误差 0、抠底 UI 保留 98.4% + 误收 0% + 稳定移动轨迹 9 条）：

- `watch_burst`：点了按钮「刚才是不是闪了一句提示」→ 动作后连拍逐帧差分，返回
  hit_frames/首个命中时刻/变化框。支持内联动作（click/key/paste/wait），一次调用
  完成「动作→观察」闭环；`rebase` 把点击浮雕吸收进基线。
- `verify_typing`：输入是否生效（自绘输入框、无文字属性控件）→ 动作前抓基线，
  动作后在回显区看变化；`ocr_check=true` 附变化前后 OCR 文本。
- `probe_hang`：窗口疑似卡死 → 发键看内容变化。**唯一安全通道是屏幕拷贝**：挂起
  窗口不处理 WM_PRINT，PrintWindow 会永久阻塞（POC 两次卡死实测），所以挂死场景
  别用 scan/snap 后台模式；判定要求窗口前台无遮挡，置前失败/可见率<0.95 如实拒绝。
- `split_static_dynamic`：游戏/动画画面分离静态 UI 与动态实体 → 多帧中位数抠 UI
  底图、时间轴标准差抠动态掩码、跨帧跟踪后按**净位移**分实体（移动）与动画层
  （原位呼吸灯/跳秒/滚动聊天）。产物 ui_base/dyn_mask/annotated 三图可读图核对。

纪律：前台干扰型用例串行跑（踩坑教训）；多帧抠底执行期进程降 BELOW_NORMAL、结束
恢复（霸业挂机让路）。验收=`_test_watch.py`（自造 tk 靶 19/19，2026-11-01）。

**X 关闭按钮专项识别（2026-10-25）**：visual 引擎双通道检测自绘「×」——
色块+反色笔画（红底白X，免疫与邻界黏连）与全图对角交叉配对+环带干净度
（白底细线X，灭二维码误报），四道闸门（反向/垂直/等长/真交叉）+四臂验证
压文字误报；检出元素 `text="×"`、`conf=0.72`。元素表漏识时 `click_xy` 兜底。

### 业务生效判定 wait 语义（expect，2026-10-25 二十棒后续）

「点击执行成功 ≠ 业务生效」（OCR 合并文字中心落缝隙、invoke 发出但界面没反应）
的根治：`click_element` / `click_xy` / `type_text` 支持可选 `expect` 条件——操作
发出后**轮询扫描等待条件成立才算成功返回**，超时如实返回未命中（`ok=false` +
`expect.matched=false` + 最后一次扫描的命中证据 `expect.evidence`），禁止谎报成功。

```
expect = {find: "文本子串", mode: "appear"|"disappear",
          timeout: 秒(默认10), interval: 毫秒(默认800)}
# appear=文本出现才成功；disappear=文本消失才成功；可再加 window/hwnd 指定扫描窗口
# 接受对象或 JSON 字符串两种形态（MCP/CLI 同义）
```

匹配在元素表 text 上做形近归一子串包含（`×→x` + lower）。扫描窗口优先 expect
显式指定，否则用被操作元素所属窗口（hwnd）；坐标点击无所属窗口，expect 必须带
`window` 或 `hwnd`。轮询走后台抓窗（不置前不抢焦点），证据里的元素 id 为该次
扫描的临时编号。CLI 对应 `click/clickxy/type --expect '<JSON>'`。

### 堆叠状态感知（2026-10-28，根治「点了也不知道点没点到」）

屏幕画面是多窗口堆叠合成：agent 分不清哪个窗在前台、目标窗被谁盖着；物理点击
永远打在**最顶层窗口**上，此前点完都不知道点没点到。本层把 Z 序变成结构化数据
+点击前置拦截（`sp/stacking.py`）：

- **`stack`**（CLI）/ **`window_stack`**（MCP）：可见顶层窗 Z 序全景——每窗
  hwnd/标题/矩形/`foreground` 标记/最小化态；`--occlude HWND` 对指定窗做遮挡
  量化（窗口内网格采样 WindowFromPoint 归根比对，返回 `visible_ratio` 可见率
  + `blockers` 遮挡者清单）。
- **`who_at(x,y)`**：某坐标点击下去命中的是谁（事前答案）。
- **落点归窗前置拦截**：`click`/`click_element` 物理点击前核验落点最顶层窗口
  是不是目标窗（元素带 hwnd 时默认开），不符即拒绝并报 `top_window`——
  `--no-check-top`（CLI）/`check_top=false`（MCP）是知情强点逃生口。
- **`click_xy` 事后反馈+事前拦截**：结果一律附 `hit_window`（落到谁身上）；
  `--top-must 子串`/`top_must` 要求落点窗标题含该子串，不符拒绝点击。
- **后台抓窗自带可见率**：窗口模式 `scan` 的 `meta.occlusion` 摘要
  （`visible_ratio`/`foreground`/`top_blocker`）——后台图是窗口自身渲染
  （后台真相），物理点击只打得动前台可见部分，agent 一眼知道要不要先置前。

实靶验收 8/8（`_test_stack.py`）：计算器被自测靶遮挡场景——前台判定/遮挡量化
（ratio 0.915+blocker）/遮挡态点击拦截/置前后拦截解除+业务生效/hit_window 反馈
/top_must 拦截/后台 scan 摘要全过。

### 会话收台：做完工作自动收台（2026-11-06，定案「残窗清理靠机制不靠纪律」）

事故根源：bench 清残留 `taskkill /IM` 按进程名下刀，误伤无辜窗。机制对策=
**登记-清理两段式**（`sp/cleanup.py` 单一真源，bench 与 MCP 共用一份代码）：

- **登记段**（三源进账，不靠纪律）：server 启动即开会话账本
  （`mcp_data/sessions/`）；`register_window` 显式登记自己拉起的窗；
  click/type/press_key/scroll 等六动作工具自动做前后 EnumWindows 差分，
  动作新开的窗登记为 action_diff（advisory，只报不自动关）。
- **清理段**（三个收口，同一份代码）：`cleanup_windows` 任务收口按账本温和关
  （WM_CLOSE×3 轮 SMTO_ABORTIFHUNG，全程无 taskkill）；会话收口
  try/finally + atexit 双挂（换窗/猝死自动清）；bench teardown import 同一模块。
- **安全六门**（每窗关前逐一过）：①身份三验（hwnd+pid 一致，防句柄回收张冠李戴）
  ②白名单永免（explorer/任务管理器/ZCode 自身等，`mcp_data/cleanup_whitelist.json`
  可加免）③只认本会话账本 ④跨会话只报不关 ⑤脏窗三通道识别只报不关
  ⑥幸存者复判——WM_CLOSE 关不掉的（模态/提权窗）写残窗报告，绝不升级强杀。
- **残窗闭环靠机制不靠记性**：会话猝死时残窗报告落盘，下一会话首次工具调用
  自动捎带提醒「上一会话遗留 N 窗」。

验收=`_test_收台.py` 23/23（登记三源/差分/六门/猝死/零触碰/expect 修复）+
bench 五场景 teardown 零残留 + 对用户已开窗口零误伤。

### 两级视觉与文本通道（2026-10-28，定案「这是你的眼睛必须让你看的清」）

实证（同图对比实验）：同一张 2560×1440 原图，整屏看任务栏时钟=光斑，裁 280×54
局部原分辨率看=逐字清——**糊在模型视觉编码端**（任意尺寸压固定视觉 token 预算，
370 万像素摊薄），不在文件端。定案两条 → 两通道：

**通道一「看清」=裁片视觉**（`sp/lastshot.py`）：
- `crop`/`crop_screen(element_id)`：按元素表编号裁原分辨率局部图；从最近
  scan/snap 的**同源原图**裁（原图落 `mcp_data\last_shot.png`+状态
  `last_shot.json`，crop_screen 的编号与该次扫描的元素表严格同源）。
- `cropxy`/`crop_xy(x1,y1,x2,y2)`：物理坐标区域裁片（坐标系与元素表/`click_xy`
  同源；snap 之后没有元素表也能用）。
- **超龄自动重抓**：缓存超 60s（画面可能已变）自动按原方式重抓新图再裁
  （返回 `used="refreshed"`），防「拿旧图裁新画面」错位；新鲜缓存返回
  `used="cache"` 不重截。

**通道二「直达」=元素书库 v2**（`sp/booklib.py`，schema 2，库根 `manual/`）：
- `scan` 尾部自动把带语义名元素（text 非空且 conf≥0.5）入库：
  `manual/<应用域>/entries.json` + 同名裁片 png——应用域 ×元素类型两级分类。
- **域解析 v2**：浏览器窗口优先读 UIA 地址栏取站点 host 为域（如 `baidu.com`，
  同一网站换搜索词/页面不再新开域）；读不到退化 `web_<标题首段>`；非浏览器
  维持标题末段规则。
- **同名消歧（空间桶+桶内多候选）**：键=(名, 类型, 空间桶)——窗口内相对坐标
  按 1/8 宽高度量，设置页多个「开/关」、列表里多个「删除」并存为多候选；
  **同桶同名再按精确坐标去重并存最多 5 条**（bbox 左上角两轴差 <8px 视为同一
  元素=同键更新，相距 8~152px 的近距同名不再被压成一条；满 5 条新观察淘汰
  最旧，库容可控）。`find` 候选各带 `pos_hint`（方位人话+rel 坐标，如
  「窗口上部区域(rel 312,88)」，任意两条必可区分）。
- **窗口锚定与 stale**：每条存 `rel_bbox`（相对窗口 rect 左上）+入库时
  `win_rect/win_size`；查找时窗口活着且尺寸未变 → bbox 按当前窗口位置换算
  （窗口移动坐标随动、stale=false）；窗口关闭/尺寸变化 → 保留原 bbox 并
  `stale=true + reason`，禁止静默返回过期坐标。v1 旧条目迁移标 `legacy=true`
  一律 stale=true 提示重扫（565 条迁移零丢失，备份 `manual_backup_20261029/`）。
- **legacy 归档（2026-10-29 第二单）**：旧条目坐标不可信还占候选位——
  ①`find`/`book_find` 默认排除 legacy（`include_legacy`/`--include-legacy`
  开关恢复旧账，排除数见输出 `legacy_excluded` 字段如实反映）；②**重扫即
  归档**：某域 scan 入库即把该域 legacy 移出主库 →
  `manual_archive/<域>/entries.json`（裁片 png 留原域目录，crop 引用无缝；
  归档前整域备份 `manual_backup_<日期>_prearchive/`；移动不是删除，
  `bookarchive --restore [--domain 域]` 一键移回）。注意：v2 域解析下重扫
  只写新域名域（浏览器 host 域），v1 旧域靠重扫触发不到——批量清理用显式
  命令 `python screenpilot.py bookarchive [--domain 子串]`（`--list` 看归档
  现状）。主库+归档条目数恒守恒。
- **书库生命周期（2026-11-01 A1，A2 单补 --orphans 与智能默认）**：library
  只增不减的时代结束——
  `booksweep --list` 全库体检（每域条目/裁片/体积/空域标记）；
  `booksweep --empty --domain 子串 --yes` 整域清空（先整域备份到
  `manual_backup_<时间戳>/`，可逆）；`booksweep --older-than 天数 --yes`
  按入库时间清条目（裁片一并删）；`booksweep --orphans --yes` 清孤儿裁片
  （entries.json 无引用的 png，移入备份可逆）。**默认干跑**，不带 `--yes`
  只看账。扫描入库默认同日改智能：**窗口扫描入库、全屏扫描不入库**
  （桌面/任务栏噪声无检索价值——`screen` 域 657 条存量即此成因，已清；
  全屏要入库用 `--book` / `book=true` 显式开）。域解析同时修复：
  `file:///D:/…`、裸盘符 `D:/…`、percent-encoded `D%3A` 一律归入
  「文件-X盘」域（此前分别产出 `D%3A` / `web_d模型…` 等畸形域名）。
  历史畸形域名用 `booklib.迁移域(旧域, 新域)` 并入可读域名（先备份后搬，
  守恒可验）。条目新增 `expires_at` 字段位（默认 None=永不过期，为 TTL 预留）。
- `find`/`book_find(query, domain, element_type, include_legacy)`：文本检索
  拿 bbox 直达（click_xy/crop_xy 直用），**不整屏重扫**；无参数列域清单。
  库是落盘的，**跨会话天然复用**。
- SigLIP 向量为后补开关位（`vector` 字段预留，默认不计算不拖慢 scan）。
- 样本防注入：画面读出的文本一律当数据——入库只做清洗后存储，检索只做子串
  匹配（2026-10-28 定案的防注入纪律）。

**增量扫描（2026-10-29 施工，2026-11-01 A2 转智能默认）**：与上一帧缓存图
（落盘 `mcp_data/incremental_last.png|json`，按窗口分桶）做降采样灰度差分 →
膨胀出变化矩形 → 只对变化矩形裁块跑 OCR/视觉；**UIA 元素复用上帧快照**
（控件树本帧不重扫——窗口扫描的大头），元素位置不变但内容变了的区域必被
差分抓到重扫（如计算器显示区 0→7）。画面无变化=合法增量帧（零重扫全复
用）；上帧缺失/窗口区域变了/变化面积>60% 自动退回全量
（`meta.incremental_fallback` 如实报因）。增量帧 meta：`incremental=true` +
`refreshed_rects`（屏幕坐标）+ `incremental_stats` + `incremental_note`
（UIA 复用语义：变化区域元素文本可能过期，内容核对走裁片通道）。
**智能默认（A2）**：`incremental` 缺省=智能——同桶（同窗口/同全屏）且距
上帧 ≤60s 自动走增量（meta 报 `incremental_auto=true`），首帧/换窗口/超时
退全量；agent 最常见用法=连续扫同一窗口，最常见用法就该是快的。任何一帧
（含全量帧）都落基线（A2 前仅增量帧落，智能判定会等不到原料）。
`--incremental`/`incremental=true` 强制增量，`--full`/`incremental=false`
强制全量（基准测量必用——裸 scan 已是智能增量，测全量基线不再代表全量）。
实测：同窗口连续扫 8561→2431ms（-71%）；静止画面 8809→141ms（-98%）；
计算器连点场景全量 1786ms vs 增量 891ms（-50%）。
  匹配（2026-10-28 定案的防注入纪律）。

**分工原则：定位走文本（地图），看清走裁片（风景），互不污染**——看整屏图
估坐标不可靠（反面证据在案），找目标一律查库/扫元素表；看内容一律裁片读。

实靶验收 9/9（`_test_twovision.py`，计算器）：scan 自动入库 81 条/find「七」
跨进程命中（Win11 中文计算器数字按钮 UIA 名=「七八九」，阿拉伯「7」命中屏幕
文本条目——两路皆通）/查库 bbox 直达点击显示 0→7 业务生效/crop 显示区「7」
逐字可读/超龄 refreshed/snap 后 cropxy 全过。

### 坐标刻度轴（标注图直读物理坐标）

`scan` 标注图（CLI 与 MCP 同一渲染）上边缘与左边缘印**屏幕物理坐标刻度轴**：
每 100px 主刻度线+坐标数字、50px 半格短刻度（黄色，与红/蓝/绿元素框区分）。
数字标的是屏幕坐标（窗口模式已含窗口 offset），从标注图直接读数即可 `click_xy`
直点元素表漏识目标，比凭空猜坐标快得多。

手动调用方式：

```bash
python mcp_server.py        # stdio 启动（agent 客户端托管，勿交互运行）
python _test_mcp.py         # 端到端自测：起子进程走 MCP 协议（16 用例：保号+后台+漂移+连点闭环+expect 四连核对）
python bench_scan.py 全屏 3 # 串行 vs 并行 同进程公平基准
```

## 元素字段

`id`（编号）/ `type`（button/edit/text/link/checkbox/item/icon…）/ `text`（UIA 名或 OCR 文本）/
`bbox`（物理像素 [l,t,r,b]）/ `source`（uia|ocr|visual）/ `conf` / `clickable`

## 实测性能（本机 2560×1440，GTX 1070 Ti 未使用，纯 CPU；并行管线后）

| 场景 | 耗时 | 说明 |
|---|---|---|
| 全屏扫描（并行） | ~2.9s（串行基线 4.8s，**提速 39%**） | ~654 元素 |
| 窗口扫描（小窗） | ~1.1-1.3s | 小窗 OCR 面积小，提速 9%（瓶颈在固定开销） |
| 游戏窗口（CefFlashBrowser 微端） | ~1.5-2.5s | OCR 兜底工具栏按钮，UIA 拿 CEF 状态文本 |

## 已知限制与坑（实测）

1. **前台锁定**：后台进程调用 `SetForegroundWindow` 会被系统拦截，导致「扫的 A 窗口、
   截图拍到 B 窗口」。已统一走 `uia_engine.activate_control`：SetActive → 核验前台 →
   失败降级「最小化→还原→SetForegroundWindow」（还原动作系统强制激活，可穿透
   「前台进程拒绝让位」锁，10-23 实测用户正用全屏浏览器时 SetActive 静默失效、
   此手法有效）→ 再核验。扫前先 activate；窗口被遮挡时直接用 `--bg` 后台模式。
2. **DPI 缩放**：入口处已设 `SetProcessDpiAwareness(2)`，保证截屏/UIA/点击三者同一
   物理像素坐标系；若在其他机器出现坐标偏移，先查缩放。
3. **OCR 置信度是字符串**：rapidocr 1.2.3 返回 `[box, text, score_str]`，score 需 `float()`。
4. **中文输入**：`pyautogui.write` 不支持中文，`type` 命令统一走剪贴板 + Ctrl+V。
5. **编号时效（二期后）**：同一窗口连续 scan 编号自动保号（tracker 帧间追踪）；
   但元素**消失后其编号不再存在**，操作引发界面变化后仍应重扫核对效果。
6. **控件级优先**：对 UIA 来源的 edit 元素 `type` 走 `ValuePattern.SetValue`，
   click 优先控件级调用（`--physical` 可强制物理点击）。被遮挡时 ControlFromPoint
   会拿到**遮挡物**的控件——click/type 已做「顶层窗口归属核验 + 窗口树内重定位」
   防误操作；物理点击永远点屏幕坐标，被遮挡时必落在遮挡物上。
7. **标注图坐标**：元素 bbox 为屏幕坐标，窗口模式标注图必须平移窗口原点
   （`annotate(..., offset=meta["offset"])`），否则全部框偏移——10-23 游戏实测发现并已修。
8. **UIA COM 跨线程**：STA 接口指针不能跨线程使用；并行管线中 UIA 的查找/置前/遍历
   全部在独立 COM 线程内完成（线程内 `CoInitialize`），主线程只拿纯数据结果。
9. **64 位 ctypes 句柄**：HDC/HBITMAP 是 64 位句柄，`windll` 默认按 32 位 c_int 传参
   会截断高位——PrintWindow 全套 GDI 调用已在 `capture.py` 显式设 argtypes/restype；
   UIA `NativeWindowHandle` 可能给出超 int32 的值，传 Win32 前统一 `& 0xFFFFFFFF`。
10. **UIA 无障碍桥缓存旧名**：部分框架（tk 实测）按钮改名后 UIA `Name` 不刷新，
    OCR 读到的画面新文本会被旧名掩盖——融合层已加「OCR 长文本完整包含 UIA 旧名
    且 conf≥0.7 时以画面为准」规则（fuse.py）。
11. **tk 的 UIA 桥不执行 invoke**：tkinter 控件能被定位、能报告 Invoke pattern，
    但控件级调用不触发 command 回调（真实 Win32/Qt/UWP/Electron 应用正常，
    计算器 7×8 连点闭环为证）；tk 靶上的点击业务验证用物理点击。

## 目录结构

```
screenpilot.py        CLI 入口（扫描/操作/书库/时间维度共十九子命令；--bg/--hwnd/--precise）
mcp_server.py         MCP stdio server（xuanlan-screenpilot，二十工具；background/hwnd/precise + 编号保号 + 堆叠协议 + 两级视觉 + 时间维度四件 + 会话收台）
sp/                   引擎包
  pipeline.py         扫描管线（三引擎并行：UIA 线程 A / OCR+视觉 线程 B / 主线程截图；后台抓窗+编号追踪+自动入库接线；scan_image 静态图识别）
  capture.py          截屏（DPI 感知，全屏/窗口区域/PrintWindow 后台抓窗；load_image 图片读入）
  uia_engine.py       UIA 控件树引擎 + activate_control（SetActive→核验→最小化还原 降级链）
  ocr_engine.py       RapidOCR 文字引擎
  visual_engine.py    OpenCV 图标候选引擎
  fuse.py             三源融合 + 编号（含 UIA 旧名被 OCR 新文本覆盖规则）
  tracker.py          帧间编号追踪（IoU+中心距+文本相似匹配，保号/追加/按窗口分桶）
  waiter.py           wait 语义（expect 业务生效判定：解析/轮询扫描/命中证据）
  annotate.py         标注图渲染（支持窗口 offset 平移 + 屏幕坐标刻度轴）
  executor.py         动作执行（控件级优先→遮挡核验+树内重定位；物理点击默认漂移落点+拖尾）
  stacking.py         堆叠状态感知（Z 序全景 stack/遮挡量化 occlusion/落点归窗 who_at）
  lastshot.py         最近抓图缓存 + 裁片（两级视觉·看清通道：crop_xy/crop_element，超龄自动重抓）
  booklib.py          元素书库（两级视觉·文本通道：scan 自动入库 manual/<域>/ + 文本检索）
  watch.py            时间维度四件（连拍窗 watch_burst/一隐一显 verify_typing/卡死探测 probe_hang/抠底分层 split_static_dynamic；POC 转正，见头注来源与成绩）
  model.py            元素数据类（含所属窗口 hwnd）
manual/               元素书库根（scan 自动写入：<应用域>/entries.json + 裁片 png；schema 2=空间桶消歧+窗口锚定 stale；跨会话复用）
manual_archive/       legacy 旧条目归档（bookarchive 移出主库落这里，--restore 一键移回；裁片 png 仍在 manual/<域>/）
mcp_data/             MCP server 运行数据（元素缓存/标注图/截图/track_state.json 保号链/last_shot.* 裁片缓存/incremental_last.* 增量基线）
_test_mcp.py          MCP 端到端自测（计算器+tk 自造靶，勿用 notepad——有会话恢复坑）
_test_stack.py        堆叠状态感知验收（计算器被自测靶遮挡实靶 8 用例）
_test_twovision.py    两级视觉与文本通道验收（计算器靶 9 用例：入库/检索/直达点击/裁片/超龄）
_test_book_v2.py      书库数据模型 v2 验收（同名消歧/域漂移/窗口移动 stale/旧库不丢/域解析 5 用例）
_test_book_clean.py   书库收尾验收（归档恢复闭环/近距同名双候选/幂等/库容/全库清零 7 用例）
_test_incremental.py  增量扫描验收（提速/内容重扫+集合一致/滚动不丢 3 用例）
_tk_dual_target.py    双同名按钮消歧自测靶（书库 v2 用）
_tk_near_dual.py      近距同名按钮消歧自测靶（同桶双「确定」顶部距 60px，书库收尾用）
_tk_watch_target.py   时间维度四件自测靶（toast/一隐一显/真冻结/双动画 Canvas；控件 bbox 自上报）
_test_watch.py        时间维度四件验收（POC 原达标线复验 19 用例，2026-11-01）
tk_target.py          自测靶窗口（tkinter，按钮带点击计数，可控无副作用）
_smoke_v2.py          二期三功能端到端冒烟（保号/后台抓窗/漂移点击）
bench_scan.py         串行 vs 并行 同进程公平基准
验收_20261022/        验收1：记事本（scan+菜单点击）
验收_20261023/        验收2：浏览器跳转必应；验收3：位图 OCR；首调验证+游戏实测证据
验收_20261028_两级视觉/  两级视觉验收证据（裁片图/snap/超龄重抓）
```

## 许可证（License）

Open core 结构：本仓库核心引擎以 [Apache-2.0](LICENSE) 开源；商业增值集成（闭源部分）不在本仓库内。

---
XuLan ScreenPilot · 识屏系统 · 2026-10-23 二期（编号稳定性+后台抓窗+漂移点击）· 2026-10-28 三期（堆叠状态感知+两级视觉与文本通道）· 2026-10-29（书库数据模型 v2+增量扫描+click_safe）· 2026-10-29 二单（书库收尾：legacy 归档+桶内多候选）
