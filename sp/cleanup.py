# -*- coding: utf-8 -*-
"""收台机制（登记-清理两段式）—— ScreenPilot 单一真源（2026-11-06 设计页 §5）

设计正本=screenpilot库 notes/设计-做完工作自动收台_20261106.md。
事故根源=事件卡 #005（taskkill /IM 按进程名下刀误杀 explorer 与老板 Edge 窗）。
老板拍板：「残窗清理靠机制不靠纪律」。

两段式：
  登记段——三源进账（launch=bench 拉靶 / explicit=agent 显式登记 /
  action_diff=动作前后 EnumWindows 差分），写 mcp_data/sessions/<会话>.jsonl；
  清理段——WM_CLOSE 温和投递 × 三轮（SMTO_ABORTIFHUNG 2s cap，全程无
  taskkill），每窗过安全六门，清不掉的进残窗报告，下窗首调捎带提醒。

安全六门（防 #005 重演，设计页 §4）：
  1 身份三验：IsWindow + pid 一致（hwnd 才是主键，防句柄回收张冠李戴）
  2 白名单永免：explorer/任务栏/任务管理器/ZCode 自身/系统外壳窗，名单可配置
  3 只认本会话账本：close_candidate（launch/explicit）才自动关，
    action_diff 只列不关（agent 判明后可显式 force 单窗）
  4 跨会话只报不关：会话收口只清自己账本，别人的遗留窗进残窗报告
  5 脏内容保护：关前 UIA 快查「已修改/未保存」类标记，脏窗只报不关
  6 幸存者不升级：WM_CLOSE 三轮后仍在 → 残窗报告返回 agent，绝不强杀

bench 与 MCP 都 import 本模块，清理逻辑只有这一份。
"""
import ctypes
import json
import os
import socket
import sys
import time
from ctypes import wintypes

# 工程根（sp/ 的父目录）；账本与报告落 mcp_data/，与 MCP 数据同目录
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSIONS_DIR = os.path.join(ROOT, "mcp_data", "sessions")
REPORT_PATH = os.path.join(ROOT, "mcp_data", "residual_report.json")
WHITELIST_PATH = os.path.join(ROOT, "mcp_data", "cleanup_whitelist.json")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_CLOSE = 0x0010
SMTO_ABORTIFHUNG = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 关窗实测参数（沿用 bench close_window_gently：三轮重试治暂态竞态）
_轮间隔秒 = 0.8
_轮间停秒 = 1.2
_WM_CLOSE超时毫秒 = 2000  # 设计页 §4：2s cap，挂死窗不阻塞收台

# 门2 默认白名单（exe 基名小写 / 窗口类名精确）；mcp_data/cleanup_whitelist.json
# 存在则合并追加（只加不减，加免是放宽，收紧须改本文件+评审）
默认白名单_exe = [
    "explorer.exe",      # 外壳+文件资源管理器（#005 主受害者）
    "taskmgr.exe",       # 任务管理器（提权窗 WM_CLOSE 被 UIPI 拦，碰它=白费）
    "cmd.exe", "conhost.exe", "windowsterminal.exe", "microsoftterminal.exe",
    "zcode.exe", "zcode_cli.exe",   # ZCode 自身（宿主与 CLI 窗）
    "nvidia share.exe", "nvcontainer.exe",           # NVIDIA overlay
    "applicationframehost.exe",                      # 系统 UWP 宿主外壳窗
    "startmenuexperiencehost.exe", "searchhost.exe", "shellexperiencehost.exe",
    "textinputhost.exe", "windowsinputexperience.exe",
    "sihost.exe", "ctfmon.exe", "dwm.exe", "winlogon.exe", "csrss.exe",
    "lsass.exe", "services.exe", "smss.exe", "system", "system idle process",
]
默认白名单_cls = [
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "TaskListThumbnailWnd", "XamlExplorerHostIslandWindow",
    "Windows.UI.Core.CoreWindow",   # 系统级 UWP 宿主窗（开始菜单/搜索/输入）
    "Tooltips_class32", "SysShadow",
]

# ---------------------------------------------------------------------------
# Win32 辅助
# ---------------------------------------------------------------------------

user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_ulong)]
user32.SendMessageTimeoutW.restype = ctypes.c_ulong

_dwmapi = ctypes.windll.dwmapi
_DWMWA_CLOAKED = 14


def _cloaked(hwnd):
    """UWP 幽灵窗判定（IsWindowVisible=True 但 DWM 斗篷遮蔽）。"""
    v = ctypes.c_int(0)
    _dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED,
                                  ctypes.byref(v), ctypes.sizeof(v))
    return v.value != 0


def _标题(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _类名(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid_of(hwnd):
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _exe_path(pid):
    """进程 exe 全路径（PROCESS_QUERY_LIMITED_INFORMATION 权限即可，勿提权）。"""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def _log(*args):
    """只写 stderr（stdout 是 MCP 协议通道，禁止 print）。"""
    try:
        sys.stderr.write("[cleanup] %s\n"
                         % " ".join(str(a) for a in args))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 快照与差分
# ---------------------------------------------------------------------------

def snapshot():
    """可见顶层窗快照：[{hwnd,title,cls,pid,exe}]（只收有标题窗，毫秒级）。"""
    out = []
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(h, _l):
        try:
            if not user32.IsWindowVisible(h) or _cloaked(h):
                return True
            title = _标题(h)
            if not title:
                return True
            found.append(int(h))
        except Exception:
            pass
        return True

    user32.EnumWindows(_cb, 0)
    for h in found:
        try:
            pid = _pid_of(h)
            out.append({"hwnd": h, "title": _标题(h), "cls": _类名(h),
                        "pid": pid, "exe": _exe_path(pid)})
        except Exception:
            continue
    return out


def 差分(前快照, 后快照):
    """动作前后快照差分，返回新增窗（后集合里前集合没有的 hwnd）。"""
    旧 = {w["hwnd"] for w in 前快照}
    return [w for w in 后快照 if w["hwnd"] not in 旧]


# ---------------------------------------------------------------------------
# 会话账本（jsonl：头行 + 追加条目；bench 与 MCP 同目录分文件互不干扰）
# ---------------------------------------------------------------------------

账本 = None  # 模块级当前会话账本（init_session 后可用）
_启动时刻 = None  # init_session 时刻（判残窗报告新旧用）


class SessionLedger:
    def __init__(self, session_id, path):
        self.session_id = session_id
        self.path = path
        self.收口已跑 = False

    def append(self, entry):
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            _log("账本写入失败:", e)

    def load(self):
        条 = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            条.append(json.loads(line))
                        except Exception:
                            continue
        except Exception:
            pass
        return 条

    def entries(self):
        """登记条目（去掉头行与 resolved 结算行）。"""
        return [d for d in self.load()
                if d.get("hwnd") and not d.get("kind")]

    def mark_resolved(self, hwnd, how):
        self.append({"kind": "resolved", "ts": _now(), "hwnd": int(hwnd),
                     "how": how})


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def init_session(kind="mcp"):
    """开会话账本：session_id=启动时刻+进程号（bench 进程同规则分文件）。"""
    global 账本
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    sid = "%s_%s_%d" % (kind, time.strftime("%Y%m%d_%H%M%S"),
                        os.getpid())
    path = os.path.join(SESSIONS_DIR, sid + ".jsonl")
    账本 = SessionLedger(sid, path)
    _启动时刻 = time.strftime("%Y-%m-%d %H:%M:%S")
    账本.append({"kind": "header", "ts": _now(), "pid": os.getpid(),
                 "machine": socket.gethostname(), "session_id": sid,
                 "mode": kind})
    return 账本


def _登记条(窗, source, close_candidate):
    条 = {"ts": _now(), "hwnd": int(窗["hwnd"]), "title": 窗["title"],
          "pid": int(窗["pid"]), "exe": 窗.get("exe", ""),
          "cls": 窗.get("cls", ""),
          "source": source, "close_candidate": bool(close_candidate)}
    if 账本:
        账本.append(条)
    return 条


def register_by_title(title_substr, source="explicit", close_candidate=True):
    """按标题子串登记（可多窗全登）。返回登记条目列表。"""
    命中 = [w for w in snapshot() if title_substr in w["title"]]
    return [_登记条(w, source, close_candidate) for w in 命中]


def register_hwnd(hwnd, source="explicit", close_candidate=True):
    """按 hwnd 登记单窗（bench resolve_target 场景）。"""
    hwnd = int(hwnd)
    for w in snapshot():
        if w["hwnd"] == hwnd:
            return _登记条(w, source, close_candidate)
    return None


def diff_register(前快照):
    """动作差分登记（设计页 §3）：新增窗登记 action_diff（advisory 只报不关）。

    返回新增窗列表（供工具返回捎带 windows_opened）；无账本或无新增返回 []。
    """
    if not 账本:
        return []
    try:
        新增 = 差分(前快照, snapshot())
        for w in 新增:
            _登记条(w, "action_diff", False)
        return 新增
    except Exception as e:
        _log("差分登记失败:", e)
        return []


# ---------------------------------------------------------------------------
# 安全六门与关窗
# ---------------------------------------------------------------------------

def _白名单():
    exe = set(默认白名单_exe)
    cls = set(默认白名单_cls)
    try:
        if os.path.exists(WHITELIST_PATH):
            with open(WHITELIST_PATH, "r", encoding="utf-8") as f:
                extra = json.load(f)
            exe |= {s.lower() for s in extra.get("exe", [])}
            cls |= set(extra.get("cls", []))
    except Exception as e:
        _log("白名单配置读取失败（用内置默认）:", e)
    return exe, cls


def _exe基名(exe_path):
    return os.path.basename(exe_path or "").lower()


_白名单exe, _白名单cls = _白名单()


def 白名单命中(窗):
    """门2：白名单永免（exe 基名或类名命中）。"""
    if _exe基名(窗.get("exe")) in _白名单exe:
        return "exe:" + _exe基名(窗.get("exe"))
    if 窗.get("cls") in _白名单cls:
        return "cls:" + 窗["cls"]
    return None


def 身份三验(条):
    """门1：IsWindow + pid 一致（hwnd 主键）。返回 (通过?, 说明, 当前标题)。"""
    hwnd = int(条["hwnd"])
    if not user32.IsWindow(hwnd):
        return False, "句柄已失效（IsWindow=False）", ""
    now_pid = _pid_of(hwnd)
    if now_pid != int(条.get("pid", -1)):
        return False, ("句柄疑似回收复用（登记 pid=%s，现 pid=%s），拒关"
                       % (条.get("pid"), now_pid)), _标题(hwnd)
    return True, "", _标题(hwnd)


_脏标记词 = ("已修改", "未保存", "未保存的更改", "unsaved", "modified",
             "don't save", "do you want to save")
# 脏文本只认短控件（TabItem/状态字）；长 Name=正文整段，词命中=误伤
_脏文本上限 = 120


def 脏标记(hwnd, 最大深度=4):
    """门5：脏窗判定（2026-11-06 记事本实测三通道）。

    a) 窗口标题 '*' 前缀（Win11 记事本打字后顶层标题带 *，GetWindowText 零成本）；
    b) UIA TabItem 控件 Name 含「已修改/未保存」类词或 '*' 前缀
       （实测 TabItem Name='dirtyflagtest. 已修改。'）；
    c) UIA 浅层短文本含脏词（状态栏/对话框文字）。
    返回 (脏?, 证据)。UIA 失败=通道 a 兜底，再失败按不脏放行（报告如实标注）。
    """
    try:
        # 通道 a：标题 * 前缀（记事本系脏标记约定）
        t = _标题(int(hwnd))
        if t.startswith("*"):
            return True, "窗口标题带未保存标记: %r" % t[:60]
    except Exception:
        pass
    try:
        import uiautomation as auto
        wc = auto.ControlFromHandle(int(hwnd))
        if wc is None:
            return False, None
        命中 = []

        def _看(name, ctype):
            if not name or len(name) > _脏文本上限:
                return False
            词语命中 = any(k in name.lower() or k in name
                           for k in _脏标记词)
            if 词语命中:
                命中.append("%s:%r" % (ctype, name[:60]))
                return True
            if ctype == "TabItemControl" and name.startswith("*"):
                命中.append("TabItem脏标记: %r" % name[:60])
                return True
            return False

        def _走(ctrl, depth):
            if 命中 or depth > 最大深度:
                return
            try:
                name = ctrl.Name or ""
                ctype = ctrl.ControlTypeName
            except Exception:
                name, ctype = "", ""
            if _看(name, ctype):
                return
            try:
                for ch in ctrl.GetChildren():
                    _走(ch, depth + 1)
                    if 命中:
                        return
            except Exception:
                pass

        _走(wc, 0)
        return (True, 命中[0]) if 命中 else (False, None)
    except Exception as e:
        _log("脏标记快查失败（按不脏放行）:", e)
        return False, None


def 保存确认框在(hwnd):
    """WM_CLOSE 后查「保存/不保存」确认框（窗内浮层）——弹了=脏窗铁证。"""
    try:
        import uiautomation as auto
        wc = auto.ControlFromHandle(int(hwnd))
        if wc is None:
            return False
        btns = []
        for b in wc.GetButtonControls():
            try:
                btns.append(b.Name or "")
            except Exception:
                continue
            if len(btns) > 12:
                break
        return ("保存" in btns and "不保存" in btns) or \
               ("Save" in btns and "Don\u2019t save" in btns) or \
               ("Save" in btns and "Don't save" in btns)
    except Exception:
        return False


def _wm_close(hwnd):
    user32.SendMessageTimeoutW(int(hwnd), WM_CLOSE, None, None,
                               SMTO_ABORTIFHUNG, _WM_CLOSE超时毫秒, None)


def _窗在(hwnd):
    """窗是否还在（按 hwnd 事实判定，不沿用旧判据——体感账 #14 教训）。"""
    return bool(user32.IsWindow(int(hwnd)) and user32.IsWindowVisible(int(hwnd)))


def close_entry(条, force=False, check_dirty=True):
    """按账本条目温和关一窗（六门全过才动 WM_CLOSE）。

    返回 {"hwnd","title","result": "closed"|"survivor"|"dirty"|"whitelist"
                       |"identity"|"gone",
          "detail": str, "force": bool}
    """
    hwnd = int(条["hwnd"])
    标题 = 条.get("title", "")
    out = {"hwnd": hwnd, "title": 标题, "result": None, "detail": "",
           "force": bool(force)}

    # 门1 身份三验
    ok, 说明, 现标题 = 身份三验(条)
    if not ok:
        out["result"] = "gone" if "已失效" in 说明 else "identity"
        out["detail"] = 说明
        return out
    if 现标题 and 标题 and (标题 not in 现标题):
        out["detail"] = "标题已漂移（登记=%r 现=%r），hwnd+pid 双验通过照关" % (标题, 现标题)
        标题漂移注 = True
    else:
        标题漂移注 = False

    # 门2 白名单永免（force 也不解锁——#005 的 explorer 就是这么死的）
    # 本进程自身的窗一律永免（清理自己=自戕；不按 exe 免 python——
    # 那会把 tk 靶窗/用户脚本窗全误免，2026-11-06 实测踩中改判）
    if int(条.get("pid", -1)) == os.getpid():
        out["result"] = "whitelist"
        out["detail"] = "本进程自身窗永免（pid=self）"
        return out
    hit = 白名单命中({"exe": 条.get("exe", ""), "cls": 条.get("cls", "")})
    if hit:
        out["result"] = "whitelist"
        out["detail"] = "白名单永免（%s）" % hit
        return out

    # 门5 脏内容保护（force=agent 判明后的显式决定，跳过）
    if check_dirty and not force:
        脏, 证据 = 脏标记(hwnd)
        if 脏:
            out["result"] = "dirty"
            out["detail"] = "脏窗只报不关（未保存标记：%r）——决定权还给 agent" % 证据
            return out

    # WM_CLOSE × 三轮（门6 前最后一道动作；SMTO_ABORTIFHUNG 挂死窗不阻塞）
    for i in range(3):
        if not _窗在(hwnd):
            break
        _wm_close(hwnd)
        time.sleep(_轮间隔秒)
    time.sleep(_轮间停秒)

    if not _窗在(hwnd):
        out["result"] = "closed"
        if 标题漂移注:
            out["detail"] = out["detail"] + "（已关）"
        return out

    # 门6 幸存者不升级：绝不强杀，报告返回 agent；弹保存确认框=脏窗铁证复判
    out["result"] = "survivor"
    out["detail"] = ("WM_CLOSE×3 后仍在（模态弹窗/提权窗/挂死？），写残窗报告，"
                     "不强杀")
    if 保存确认框在(hwnd):
        out["result"] = "dirty_survivor"
        out["detail"] = ("WM_CLOSE 弹出保存确认框=有未保存内容（铁证）。"
                         "决定权在 agent：保存/不保存/取消，机制不强杀")
    return out


# ---------------------------------------------------------------------------
# 清理入口（MCP 工具与会话收口共用）
# ---------------------------------------------------------------------------

def _写报告(payload):
    try:
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
    except Exception as e:
        _log("残窗报告写盘失败:", e)


def _按标题找窗(title_substr):
    return [w for w in snapshot() if title_substr in w["title"]]


def cleanup_windows(targets=None, force=False):
    """清理主入口（MCP 工具 cleanup_windows 的实现，bench 亦复用）。

    targets=None：清本会话账本 close_candidate 窗（launch/explicit）；
    targets=list：指定窗（int hwnd 或 str 标题子串混合）——来源不受门3限制，
    但门1身份/门2白名单恒生效；force=True 时跳过门5脏检查（agent 判明后的
    显式决定）。

    返回 {ok, closed, survivors, dirty_skipped, skipped, report, note}。
    """
    report = None
    try:
        计划 = []   # 每项: {"条": 条目或None, "窗": 快照条目}
        if targets is None:
            if not 账本:
                return {"ok": False, "error": "无会话账本（server 未 init_session？）"}
            seen = set()
            for 条 in 账本.entries():
                if not 条.get("close_candidate") or 条["hwnd"] in seen:
                    continue
                seen.add(条["hwnd"])
                计划.append(条)
        else:
            for t in targets:
                if isinstance(t, int):
                    计划.append({"hwnd": int(t), "title": "", "pid": -1,
                                 "exe": "", "cls": "", "source": "explicit",
                                 "close_candidate": True, "_手填": True})
                else:
                    for w in _按标题找窗(str(t)):
                        计划.append({"hwnd": w["hwnd"], "title": w["title"],
                                     "pid": w["pid"], "exe": w["exe"],
                                     "cls": w["cls"], "source": "explicit",
                                     "close_candidate": True})
                # 手填 hwnd 无 exe/cls——补快照身份（白名单与脏查要用）
            补 = {w["hwnd"]: w for w in snapshot()}
            for 条 in 计划:
                w = 补.get(int(条["hwnd"]))
                if w:
                    条.setdefault("pid", w["pid"])
                    if not 条.get("exe"):
                        条["exe"] = w["exe"]
                    if not 条.get("cls"):
                        条["cls"] = w["cls"]
                    if not 条.get("title"):
                        条["title"] = w["title"]

        closed, survivors, dirty, skipped, gone = [], [], [], [], []
        for 条 in 计划:
            r = close_entry(条, force=force)
            how = r["result"]
            if how == "closed":
                closed.append(r)
            elif how in ("survivor", "dirty_survivor"):
                survivors.append(r)
            elif how == "dirty":
                dirty.append(r)
            elif how == "whitelist":
                skipped.append(r)
            else:  # gone / identity
                gone.append(r)
            if 账本 and how in ("closed", "gone"):
                try:
                    账本.mark_resolved(条["hwnd"], how)
                except Exception:
                    pass

        if survivors or dirty:
            payload = {"ts": _now(), "reason": "cleanup_windows 残留",
                       "session": 账本.session_id if 账本 else None,
                       "survivors": survivors, "dirty": dirty,
                       "closed": [c["title"] for c in closed]}
            _写报告(payload)
            report = REPORT_PATH
        return {"ok": True, "closed": closed, "survivors": survivors,
                "dirty_skipped": dirty, "skipped_whitelist": skipped,
                "gone_or_refused": gone, "report": report,
                "note": ("清不掉的已写残窗报告，下窗首调会捎带提醒"
                         if report else None)}
    except Exception as e:
        _log("cleanup_windows 异常:", e)
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 启动扫描（下窗首调捎带提醒）与会话收口
# ---------------------------------------------------------------------------

_通知 = None  # 启动扫描结果，take_notice 首调吐出


def startup_scan():
    """启动时扫残留：旧报告 + 历史账本活体核对 → 组通知（不关任何窗）。"""
    global _通知
    提示 = []
    try:
        if os.path.exists(REPORT_PATH):
            with open(REPORT_PATH, "r", encoding="utf-8") as f:
                rep = json.load(f)
            ts = rep.get("ts", "")
            # 报告生成早于本会话启动 = 上窗遗留，提醒一次
            if ts and _启动时刻 and ts < _启动时刻:
                幸存 = [s.get("title", "") for s in rep.get("survivors", [])]
                脏 = [d.get("title", "") for d in rep.get("dirty", [])]
                外窗 = [x.get("title", "") for x in rep.get("foreign", [])]
                if 幸存 or 脏 or 外窗:
                    提示.append("上次残窗报告(%s)：幸存窗 %s；脏窗(未保存) %s；"
                                "他窗遗留 %s"
                                % (ts, 幸存 or "无", 脏 or "无", 外窗 or "无"))
    except Exception as e:
        _log("旧报告读取失败:", e)
    try:
        # 历史账本活体核对：非本会话的 close_candidate 且窗还活着 → 遗留
        本窗 = 账本.session_id if 账本 else ""
        活窗 = {w["hwnd"] for w in snapshot()}
        遗留 = []
        try:
            名单 = sorted(os.listdir(SESSIONS_DIR))
        except Exception:
            名单 = []
        for fn in 名单:
            if not fn.endswith(".jsonl") or fn.startswith(本窗):
                continue
            try:
                with open(os.path.join(SESSIONS_DIR, fn), "r",
                          encoding="utf-8") as f:
                    for line in f:
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        if (d.get("hwnd") and d.get("close_candidate")
                                and not d.get("kind")
                                and d["hwnd"] in 活窗):
                            遗留.append({"from": fn.replace(".jsonl", ""),
                                         "hwnd": d["hwnd"],
                                         "title": d.get("title", "")})
            except Exception as e:
                _log("账本文件读取失败(%s):" % fn, e)
        if 遗留:
            去重 = {(x["hwnd"]): x for x in 遗留}
            提示.append("上一会话遗留 %d 窗仍开着：%s（cleanup_windows 可清，"
                        "或逐窗判明后处置）"
                        % (len(去重),
                           [x["title"][:24] for x in list(去重.values())[:6]]))
    except Exception as e:
        _log("历史账本扫描失败:", e)
    _通知 = "；".join(提示) if 提示 else None
    return _通知


def take_notice():
    """首调吐通知（一次性；设计页：残窗闭环也靠机制不靠记性）。"""
    global _通知
    n, _通知 = _通知, None
    return n


def session_exit_cleanup():
    """会话收口（mcp.run finally + atexit 双挂，幂等）：
    清自己账本 close_candidate → 幸存/脏写残窗报告 → 扫其他会话遗留一并入报告。
    绝不清别人的窗（门4）。"""
    global _通知
    if 账本 and 账本.收口已跑:
        return
    if 账本:
        账本.收口已跑 = True
    try:
        结果 = cleanup_windows(targets=None)
    except Exception as e:
        _log("会话收口清理异常:", e)
        结果 = {"closed": [], "survivors": [], "dirty_skipped": []}
    try:
        # 其他会话遗留（活着的未闭环 close_candidate）只报不关
        活窗 = {w["hwnd"] for w in snapshot()}
        本窗 = 账本.session_id if 账本 else ""
        遗留 = []
        for fn in os.listdir(SESSIONS_DIR):
            if not fn.endswith(".jsonl") or fn.startswith(本窗):
                continue
            已决 = set()
            自己 = []
            with open(os.path.join(SESSIONS_DIR, fn), "r",
                      encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("kind") == "resolved":
                        已决.add(d["hwnd"])
                    elif d.get("hwnd") and d.get("close_candidate"):
                        自己.append(d)
            for d in 自己:
                if d["hwnd"] not in 已决 and d["hwnd"] in 活窗:
                    遗留.append({"from": fn.replace(".jsonl", ""),
                                 "hwnd": d["hwnd"], "title": d.get("title", "")})
        if 遗留 or 结果.get("survivors") or 结果.get("dirty_skipped"):
            _写报告({"ts": _now(), "reason": "会话收口",
                     "session": 本窗,
                     "survivors": 结果.get("survivors", []),
                     "dirty": 结果.get("dirty_skipped", []),
                     "foreign": 遗留,
                     "closed": [c.get("title", "")
                                for c in 结果.get("closed", [])]})
            _通知 = ("上一会话留窗：%s"
                     % ([x["title"][:24] for x in 遗留[:6]] or
                        [s.get("title", "")[:24]
                         for s in 结果.get("survivors", [])]))
        if 账本:
            账本.append({"kind": "exit", "ts": _now(),
                         "closed": len(结果.get("closed", [])),
                         "survivors": len(结果.get("survivors", []))})
    except Exception as e:
        _log("会话收口报告阶段异常:", e)
