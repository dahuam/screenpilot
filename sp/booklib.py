# -*- coding: utf-8 -*-
"""元素书库 v2：scan 自动入库 + 文本检索（两级视觉·文本通道）。

定案②：「利用识屏软件自动抓取元素，将各种元素分类，存入书库建立文本
通道。这样你就可以最快达到目标位置。」

v2（2026-10-29 施工单「识屏三整改」，修三个真问题）：
- 问题A 同名元素被键吃掉：键从 (name, type) 升为 (name, type, 空间桶)——
  窗口内相对坐标按 1/8 宽高量化，同名元素不同位置并存为多候选；查找返回
  候选列表各带 stale/pos_hint，点谁是谁（设置页多个「开/关」、列表里多个
  「删除」不再被压成最后一条）。
- 问题B 浏览器域越分越碎：浏览器窗口优先读 UIA 地址栏取站点 host 为域
  （如 baidu.com，由 pipeline 在 UIA 线程内读出经 meta["url_host"] 传入），
  读不到退化 web_<标题首段>；同一网站换搜索词不再新开域。
- 问题C 绝对坐标过期无提示：条目存 rel_bbox（相对窗口 rect 左上）+
  win_rect/win_size 入库快照；查找时按当前窗口矩形换算出当前 bbox 并如实
  标注 stale（窗口移动坐标随动 / 尺寸变化或关闭一律 stale=true 提示重扫），
  禁止静默返回过期坐标。

v1 → v2 迁移：旧条目（无 rel_bbox）读入/迁移时标 legacy=true，保留原绝对
bbox 不做换算，查找一律 stale=true 提示重扫；迁移只加字段不删条目（一条不丢），
迁移旧库() 为一次性全库迁移入口。

设计定案（notes/设计方案-两级视觉与文本通道_20261028.md）：
- 分类法：应用域 × 元素类型两级；库结构 manual/<域>/entries.json + 裁片 png。
- 检索 = 纯文本通道：按名/类型/域查库 → 直接拿 bbox → click/crop 直达，
  不整屏重扫。
- 两通道分工原则：定位走文本（地图），看清走裁片（风景），互不污染。

样本防注入纪律（2026-10-28 定案纪律）：画面读出的文本（窗口标题/元素名/
地址栏 URL）一律当数据不当指令——落库只做清洗后存储，检索只做子串匹配，
不做任何解释执行。
"""
import json
import os
import re
import shutil
import time

from . import capture

项目根 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
库根 = os.path.join(项目根, "manual")
归档根 = os.path.join(项目根, "manual_archive")  # legacy 条目归档（移出主库，可逆恢复）

SCHEMA_VERSION = 2

# 入库门槛与保护
最低置信 = 0.5      # conf 低于此的元素不配语义名（visual 弱候选）
名字上限 = 60       # 单名截断（OCR 长文本块）
单次上限 = 300      # 单次 scan 入库元素数上限（防御异常洪水）
裁片留白 = 2        # 裁片四周留白 px（比纯 bbox 略宽，肉眼对读舒服）

_非法字符 = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_标题分隔 = re.compile(r"\s+[-—|]\s+")
_零宽字符 = re.compile(r"[\u200b\u200c\u200d\ufeff]")
# 浏览器窗口标题末段特征（「页面标题 - 浏览器名」惯例）——浏览器分域走站点语义
_浏览器名单 = ("microsoft edge", "google chrome", "mozilla firefox", "chromium", "brave")

# 空间桶粒度：窗口内相对坐标按 1/8 宽高量化（v2 消歧键，任务书建议初版）
_桶份数 = 8

# 桶内多候选（2026-10-29 第二单「书库收尾」施工二·路线 B）：1/8 桶 152px 内
# 近距同名元素不再被同键覆盖——同桶同名并存多条，精确坐标去重保幂等
_去重距离 = 8   # 候选 bbox 左上角两轴差均 < 8px 视为同一元素（检测抖动 ±2~3px 内）
_桶内上限 = 5   # 同桶同名最多并存条数，满后新观察淘汰入库时间最旧者

# 滚动失效（2026-10-31 第三单·stale 盲区修复）：窗口尺寸没变但页面滚动过，
# rel_bbox 对应元素已移位，查找却仍返回 stale=false 的错坐标（独立复验发现
# 3 例）。pipeline 增量链平移检测出滚动位移后经 meta["scroll_shift"] 传入，
# 内容大幅变化但平移未检出时经 meta["content_shifted"] 兜底（宁保守勿错
# 坐标）；入库把域内滚动前旧条目标 stale_scroll，本帧亲眼重扫到的同键条目
# 自动清标记（坐标已刷新为可信值）。
_滚动失效位移 = 16   # 位移 <16px 的微滚（惯性滚动残余）不标，噪声容忍


def _清洗标题(窗口标题):
    """零宽字符剥离（Edge 标题实测带 U+200B）→ 非法文件名字符清洗 → 分段。"""
    标题 = _零宽字符.sub("", str(窗口标题 or "")).strip()
    if not 标题:
        return []
    标题 = _非法字符.sub(" ", 标题).strip()
    if not 标题:
        return []
    return [p.strip() for p in _标题分隔.split(标题) if p.strip()]


def 是浏览器标题(窗口标题):
    """窗口标题是否像浏览器（末段命中浏览器名单）——pipeline 只对浏览器
    窗口调 UIA 地址栏读取，省非浏览器开销。"""
    段 = _清洗标题(窗口标题)
    if len(段) > 1:
        末段 = 段[-1].lower()
        return any(末段 == b or 末段.startswith(b) for b in _浏览器名单)
    return False


def url主机(url):
    """地址栏 URL → 稳定站点 host（去 www./端口/用户信息，清洗为目录名安全）。

    无 netloc 的内页（about:blank / edge://newtab 等）用 web_<scheme+path>
    造稳定名。解析失败返回 None（调用方退化 web_ 前缀规则）。

    file:/// 与裸盘符路径（2026-11-01 书库治理新增）：地址栏读出的本地文件
    路径统一映射为「文件-<盘符>盘」——修 `D%3A`（百分号编码未解码）与
    `web_d模型.zcodeworkspace…`（路径当站点名）两类畸形域。先 unquote 再判，
    因为地址栏给的是 percent-encoded。
    """
    url = str(url or "").strip()
    if not url:
        return None
    try:
        from urllib.parse import unquote, urlparse
        url = unquote(url)
    except Exception:
        pass
    # 本地路径三形态：file:///D:/… / D:/… / D:\… / 纯盘符 D: ——统一成「文件-D盘」
    m = re.match(r"^(?:file:///)?([a-zA-Z]):(?:[\\/]|$)", url)
    if m:
        return "文件-%s盘" % m.group(1).upper()
    if "://" not in url and ":" not in url:
        # 已是 host 形态（如地址栏读出后再传回的 "baidu.com"）：补 scheme
        # 让 urlparse 出 netloc（否则整串落 path，产出 web_ 前缀错域名）
        url = "https://" + url
    try:
        p = urlparse(url)
    except Exception:
        return None
    host = (p.netloc or "").strip()
    if host:
        host = host.split("@")[-1].split(":")[0]
        if host.lower().startswith("www."):
            host = host[4:]
    else:
        片 = ("%s%s" % (p.scheme or "", p.path or "")).strip("/ ")
        if not 片:
            return None
        host = "web_" + _非法字符.sub("", 片)[:30]
    return _清洗文件名(host)[:40] or None


def 清洗域名(窗口标题):
    """窗口标题 → 应用域目录名（v1 口径，保持兼容）。

    规则：零宽字符剥离 → 非法文件名字符清洗 → 按「 - 」等分隔符切段：
    - 末段命中浏览器名单 → 取首段（页面标题）为域——浏览器主战场按站点
      分域，不按浏览器名分域（所有网站的元素混进一个「Edge」域=互相污染）；
    - 其余维持首版口径取末段（「文档 - 记事本」的应用名段）。
    截 40 字符 → 空则 "unknown"。
    """
    段 = _清洗标题(窗口标题)
    if not 段:
        return "unknown"
    if len(段) > 1:
        末段 = 段[-1].lower()
        if any(末段 == b or 末段.startswith(b) for b in _浏览器名单):
            名 = 段[0]
        else:
            名 = 段[-1]
    else:
        名 = 段[0]
    return (名 or "unknown")[:40].strip() or "unknown"


def 解析域(窗口标题, url_host=None):
    """v2 域解析：浏览器窗口地址栏 host 优先，退化 web_<标题首段>。

    - 浏览器窗口（标题末段命中名单）且地址栏读出 host → 域=host（如
      baidu.com）——同一站点换页面/搜索词不再新开域（问题B 根治）；
    - 浏览器窗口但 host 读不到 → 域="web_"+标题首段（与 v1 浏览器域显式区分）；
    - 非浏览器窗口维持 v1「标题末段」规则不变（Win11 计算器 U+200B 坑已处理）。
    域名目录名即条目 domain_key 字段值（一一对应）。
    """
    段 = _清洗标题(窗口标题)
    if not 段:
        return "unknown"
    浏览器 = False
    if len(段) > 1:
        末段 = 段[-1].lower()
        浏览器 = any(末段 == b or 末段.startswith(b) for b in _浏览器名单)
    if 浏览器:
        host = url主机(url_host) if url_host else None
        if host:
            return host
        return (("web_" + 段[0])[:40].strip() or "unknown")
    if len(段) > 1:
        名 = 段[-1]
    else:
        名 = 段[0]
    return (名 or "unknown")[:40].strip() or "unknown"


def _清洗文件名(文本):
    名 = _非法字符.sub("", str(文本)).strip()
    return (名[:40].strip() or "unnamed")


def _域路径(域名):
    return os.path.join(库根, 域名)


def _窗口活着(hwnd):
    try:
        return bool(capture.user32.IsWindow(int(hwnd) & 0xFFFFFFFF))
    except Exception:
        return False


def _空间桶(rel_l, rel_t, win_w, win_h):
    """窗口内相对坐标 → 空间桶二元组（按 1/8 宽高度量化的格子号）。

    同名元素落在不同格子 = 不同条目（消歧的根）；同一格子的同名条目按
    「同键更新」幂等覆盖（库容可控）。
    """
    bw = max(1, int(win_w) // _桶份数)
    bh = max(1, int(win_h) // _桶份数)
    return [max(0, int(rel_l)) // bw, max(0, int(rel_t)) // bh]


def _方位提示(bx, by, 前缀="窗口"):
    """空间桶 → 人话方位（「窗口左上区域」），同名多候选时辅助 agent 分辨。"""
    横 = "左" if bx < 3 else ("中" if bx < 5 else "右")
    纵 = "上" if by < 3 else ("中" if by < 5 else "下")
    if 横 == "中" and 纵 == "中":
        return "%s中部" % 前缀
    if 横 == "中":
        return "%s%s部区域" % (前缀, 纵)
    if 纵 == "中":
        return "%s%s侧区域" % (前缀, 横)
    return "%s%s%s区域" % (前缀, 纵, 横)


def _读域(域名):
    """读域条目表；不存在/损坏返回空域骨架（样本防注入：坏 JSON 当空库不崩）。

    v1 条目（无 rel_bbox）读入时惰性补 legacy=true 标注（内存生效，随下次
    写域落盘）——保证任何路径读到的条目都带消歧所需的标注。
    """
    路径 = os.path.join(_域路径(域名), "entries.json")
    if os.path.isfile(路径):
        try:
            with open(路径, "r", encoding="utf-8") as f:
                数据 = json.load(f)
            if isinstance(数据, dict) and isinstance(数据.get("entries"), list):
                for e in 数据["entries"]:
                    if isinstance(e, dict) and not e.get("rel_bbox"):
                        e.setdefault("legacy", True)
                return 数据
        except Exception:
            pass
    return {"domain": 域名, "schema": SCHEMA_VERSION, "updated": "", "entries": []}


def _写域(域名, 数据):
    目录 = _域路径(域名)
    os.makedirs(目录, exist_ok=True)
    数据["domain"] = 域名
    数据["schema"] = SCHEMA_VERSION
    数据["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(目录, "entries.json"), "w", encoding="utf-8") as f:
        json.dump(数据, f, ensure_ascii=False, indent=1)


def 迁移旧库():
    """一次性 v1→v2 迁移：旧条目标 legacy=true，顶层升 schema=2。

    只加字段不删条目不改坐标（一条不丢）；已迁移的域跳过重写（幂等，可重跑）。
    返回摘要 {domains: {域: {total, migrated}}, total, migrated}。
    """
    摘要 = {"domains": {}, "total": 0, "migrated": 0}
    if not os.path.isdir(库根):
        return 摘要
    for 域名 in sorted(os.listdir(库根)):
        路径 = os.path.join(_域路径(域名), "entries.json")
        if not os.path.isfile(路径):
            continue
        try:
            with open(路径, "r", encoding="utf-8") as f:
                数据 = json.load(f)
        except Exception:
            continue
        if not (isinstance(数据, dict) and isinstance(数据.get("entries"), list)):
            continue
        已迁移 = 0
        for e in 数据["entries"]:
            if isinstance(e, dict) and not e.get("rel_bbox") and not e.get("legacy"):
                e["legacy"] = True
                已迁移 += 1
        if 数据.get("schema") != SCHEMA_VERSION or 已迁移:
            _写域(域名, 数据)
        摘要["domains"][域名] = {"total": len(数据["entries"]), "migrated": 已迁移}
        摘要["total"] += len(数据["entries"])
        摘要["migrated"] += 已迁移
    return 摘要


def _选域(域名, 根目录):
    """--domain 子串 → 实际域目录名列表（大小写不敏感；缺省=全部）。

    域目录名可能含特殊字符（v1 遗留「Microsoft​ Edge」带零宽字符）敲不出来，
    子串匹配是唯一顺手的定位方式；精确名也是自身子串，天然兼容。
    """
    if not os.path.isdir(根目录):
        return []
    if 域名:
        针 = 域名.lower()
        return sorted(d for d in os.listdir(根目录)
                      if 针 in d.lower()
                      and os.path.isfile(os.path.join(根目录, d, "entries.json")))
    return sorted(d for d in os.listdir(根目录)
                  if os.path.isfile(os.path.join(根目录, d, "entries.json")))


def _条目键(e):
    """条目身份键：归档/恢复去重用（名字 + 入库时绝对 bbox 唯一定位一条）。"""
    return (str(e.get("name")), str(e.get("type")), tuple(e.get("bbox") or ()))


def _条目过期(e):
    """TTL 消费（2026-11-01 F1 施工②）：expires_at 非空且已到 → 过期。

    格式与条目 time 同源（"%Y-%m-%d %H:%M:%S"）；坏值/无法解析当不过期——
    宁漏勿错（错杀活条目会让 find 凭空少候选，漏杀只是晚几天被清扫）。
    """
    if not isinstance(e, dict):
        return False
    到期 = str(e.get("expires_at") or "").strip()
    if not 到期:
        return False
    try:
        return time.mktime(time.strptime(到期, "%Y-%m-%d %H:%M:%S")) <= time.time()
    except ValueError:
        return False


def 归档域(域名=None):
    """legacy 条目归档：主库 entries.json 移出 → manual_archive/<域>/entries.json。

    - 移动不是删除（红线4）：条目原样搬去归档文件，裁片 png 留在原域目录
      （crop 相对路径引用继续有效，恢复后无缝）；
    - 归档前整域备份到 manual_backup_<今日>_prearchive/<域>/（已有备份不覆盖，
      保最初状态）；
    - 归档文件已存在则追加合并（同名同 bbox 不重复），可重跑幂等；
    - 域名 None=全库；子串匹配指定域。
    返回 {域: {archived, kept}}。
    """
    摘要 = {}
    for d in _选域(域名, 库根):
        数据 = _读域(d)
        legacy条目 = [e for e in 数据["entries"]
                     if isinstance(e, dict) and not e.get("rel_bbox")]
        if not legacy条目:
            摘要[d] = {"archived": 0, "kept": len(数据["entries"])}
            continue
        备份根 = os.path.join(项目根, "manual_backup_%s_prearchive"
                              % time.strftime("%Y%m%d"))
        备份目录 = os.path.join(备份根, d)
        if not os.path.isdir(备份目录):
            shutil.copytree(_域路径(d), 备份目录)
        归档 = _读归档(d)
        已有键 = {_条目键(e) for e in 归档["entries"] if isinstance(e, dict)}
        新增 = 0
        for e in legacy条目:
            键 = _条目键(e)
            if 键 in 已有键:
                continue
            归档["entries"].append(e)
            已有键.add(键)
            新增 += 1
        归档["archived_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _写归档(d, 归档)
        legacy_ids = {id(e) for e in legacy条目}
        数据["entries"] = [e for e in 数据["entries"] if id(e) not in legacy_ids]
        _写域(d, 数据)
        摘要[d] = {"archived": 新增, "kept": len(数据["entries"])}
    return 摘要


def 恢复归档(域名=None):
    """归档逆操作：manual_archive/<域>/entries.json 条目**无条件全量移回**主库
    （移动不是复制，守恒）。不做同键判重——legacy 与 v2 条目同名同 bbox 是合法
    共存（同一元素的新旧两份），判重会把旧条目误吞；幂等由「归档侧移走后为空、
    空了删域目录」天然保证。域名 None=全库；子串匹配。返回 {域: {restored}}。
    """
    摘要 = {}
    for d in _选域(域名, 归档根):
        归档 = _读归档(d)
        if not 归档["entries"]:
            摘要[d] = {"restored": 0}
            continue
        数据 = _读域(d)
        数据["entries"].extend(归档["entries"])
        _写域(d, 数据)
        恢复数 = len(归档["entries"])
        归档["entries"] = []
        shutil.rmtree(os.path.join(归档根, d), ignore_errors=True)
        摘要[d] = {"restored": 恢复数}
    return 摘要


def 归档清单():
    """列出归档目录各域条目数（bookarchive --list 用）。"""
    域 = []
    if not os.path.isdir(归档根):
        return 域
    for d in sorted(os.listdir(归档根)):
        归档 = _读归档(d)
        if 归档["entries"]:
            域.append({"domain": d, "archived": len(归档["entries"]),
                       "archived_at": 归档.get("archived_at", "")})
    return 域


def _读归档(域名):
    """读域归档文件；不存在/损坏返回空骨架（坏 JSON 当空归档不崩）。"""
    路径 = os.path.join(归档根, 域名, "entries.json")
    if os.path.isfile(路径):
        try:
            with open(路径, "r", encoding="utf-8") as f:
                数据 = json.load(f)
            if isinstance(数据, dict) and isinstance(数据.get("entries"), list):
                return 数据
        except Exception:
            pass
    return {"domain": 域名, "archived_at": "", "entries": []}


def _写归档(域名, 数据):
    目录 = os.path.join(归档根, 域名)
    os.makedirs(目录, exist_ok=True)
    数据["domain"] = 域名
    with open(os.path.join(目录, "entries.json"), "w", encoding="utf-8") as f:
        json.dump(数据, f, ensure_ascii=False, indent=1)


def 入库(元素列表, img, meta):
    """scan 元素自动入库（pipeline.scan 尾部调用，静态图 scan_image 不入）。

    v2：条目带 rel_bbox/win_rect/win_size/domain_key/bucket，键升级为
    (name, type, 空间桶)——同名不同位并存，同键幂等更新。meta 可带
    url_host（pipeline 从浏览器地址栏读出）供域解析。

    只收 text 非空且 conf≥0.5 的元素（有语义名才查得到）；裁片从本次扫描
    原图按 bbox-offset 裁出。返回摘要 dict（domain/added/updated/total/
    scanned_in/legacy/archived），由调用方塞进 meta["book"]；异常向上抛由
    pipeline 统一兜（不碍扫描主流程）。
    """
    窗口标题 = meta.get("window")
    域名 = 解析域(窗口标题, meta.get("url_host")) if 窗口标题 else "screen"
    # 向量层（2026-11-02 F3·B3 以图查库）：BOOKVEC=1 才接——无文字 icon 入库
    # + 本轮条目裁片编码进向量库；缺省关=零行为变化。函数内 import 防循环
    # （bookvec 顶层依赖本模块）。
    向量开 = False
    if os.environ.get("BOOKVEC") == "1":
        from . import bookvec
        向量开 = True
    元素数 = 0
    滚动标记数 = 0
    数据 = _读域(域名)
    os.makedirs(_域路径(域名), exist_ok=True)  # 裁片落盘先于 _写域，目录必须先在

    # 滚动失效标记（先于元素入库）：本帧检测到页面滚动（或内容大幅变化但
    # 平移未检出的保守兜底）→ 域内滚动前旧条目的 rel_bbox 已随内容移位，
    # 全部标 stale_scroll（查找如实报 stale=true）；本帧重扫到的元素在下方
    # 同键更新时自动清标记（新坐标亲眼所见，可信）
    滚动位移 = meta.get("scroll_shift") or None
    内容位移 = bool(meta.get("content_shifted")) and 滚动位移 is None
    if (滚动位移 and len(滚动位移) == 2 and
            max(abs(int(滚动位移[0])), abs(int(滚动位移[1]))) >= _滚动失效位移):
        for e in 数据["entries"]:
            if isinstance(e, dict) and e.get("rel_bbox") and not e.get("legacy"):
                e["stale_scroll"] = [int(滚动位移[0]), int(滚动位移[1])]
                滚动标记数 += 1
    elif 内容位移:
        for e in 数据["entries"]:
            if isinstance(e, dict) and e.get("rel_bbox") and not e.get("legacy"):
                e["stale_scroll"] = "content_shifted"
                滚动标记数 += 1

    # 窗口矩形锚定（问题C）：窗口模式取实时 rect；全屏模式以屏幕为「窗口」
    hwnd = int(meta.get("hwnd") or 0)
    sw, sh = meta.get("screen") or capture.screen_size()
    if hwnd and _窗口活着(hwnd):
        try:
            win_rect = [int(v) for v in capture.window_rect_hwnd(hwnd)]
        except Exception:
            win_rect = None
    elif hwnd:
        win_rect = None  # 扫描瞬间窗口已消失（罕见）：条目退化为无锚定
    else:
        win_rect = [0, 0, int(sw), int(sh)]
    if win_rect and win_rect[2] - win_rect[0] > 0 and win_rect[3] - win_rect[1] > 0:
        锚定 = {"win_rect": win_rect,
               "win_size": [win_rect[2] - win_rect[0], win_rect[3] - win_rect[1]]}
    else:
        锚定 = None

    # 索引只建 v2 条目（legacy 条目冻结留档，不参与同键覆盖）：
    # 键 → 同桶同名候选列表（桶内多候选，近距同名不再被同键覆盖压成一条）
    索引 = {}
    for e in 数据["entries"]:
        if isinstance(e, dict) and e.get("rel_bbox"):
            索引.setdefault((e.get("name"), e.get("type"), tuple(e.get("bucket") or ())),
                            []).append(e)
    ox, oy = (meta.get("offset") or [0, 0])[:2]
    h, w = img.shape[:2]
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    added = updated = 0
    # TTL 写入通道（F1 施工②）：meta.book_ttl_hours=N → 本轮入库条目 N 小时后
    # 到期（find 不再出、清扫可清）。缺省 None=永不过期（现状行为零变化）；
    # 同键重扫更新时新条目 expires_at 覆盖旧值——重扫到=元素还活着=以本轮为准。
    入库到期 = None
    _ttl = meta.get("book_ttl_hours")
    if _ttl:
        try:
            入库到期 = time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(time.time() + float(_ttl) * 3600))
        except (TypeError, ValueError):
            入库到期 = None

    向量记录 = []   # (条目键, 裁剪框)——循环后批量编码（F3 以图查库）
    for el in 元素列表:
        if 元素数 >= 单次上限:
            break
        text = (getattr(el, "text", "") or "").strip()
        try:
            conf = float(getattr(el, "conf", 0) or 0)
        except (TypeError, ValueError):
            conf = 0.0
        # 入库门槛（F3 分级）：有文字守 0.5 原线零变化；BOOKVEC 开时无文字
        # visual icon（conf≥0.3）也入库——纯图标/无文字元素（游戏自绘 UI、
        # 专业软件工具栏）走向量通道可查，空名在文字检索天然不命中（q 非空
        # 时子串过滤排除），不污染文本通道。
        icon无文字 = (向量开 and not text
                     and getattr(el, "source", "") == "visual"
                     and (getattr(el, "type", "") or "") == "icon")
        if text:
            if conf < 最低置信:
                continue
        elif not icon无文字 or conf < bookvec._icon最低置信:
            continue
        bbox = getattr(el, "bbox", None)
        if not bbox or len(bbox) != 4:
            continue
        l, t, r, b = bbox
        ix1, iy1 = max(0, int(l) - ox - 裁片留白), max(0, int(t) - oy - 裁片留白)
        ix2, iy2 = min(w, int(r) - ox + 裁片留白), min(h, int(b) - oy + 裁片留白)
        if ix2 - ix1 < 2 or iy2 - iy1 < 2:
            continue
        元素数 += 1
        名字 = text[:名字上限]
        类型 = getattr(el, "type", "") or "unknown"
        if icon无文字:
            # 无文字条目不落裁片（同名全撞车+重扫产孤儿）：向量从内存原图
            # 裁剪框编码，crop=None；要看图按 bbox 重裁屏。
            文件名 = None
        else:
            文件名 = "%s_%s.png" % (_清洗文件名(类型), _清洗文件名(名字))
            capture.save_image(img[iy1:iy2, ix1:ix2], os.path.join(_域路径(域名), 文件名))
        条目 = {
            "name": 名字,
            "type": 类型,
            "bbox": [int(l), int(t), int(r), int(b)],
            "crop": 文件名,            "window_title": meta.get("window"),
            "hwnd": hwnd,
            "source": getattr(el, "source", "") or "",
            "conf": round(conf, 3),
            "time": now,
            "domain_key": 域名,
            # 过期时间（2026-11-01 A1 预留字段；F1 施工②接通消费）：None=永不过期。
            # find 按此过滤、清扫 --expired 可清；缺省扫描入库=None（零变化）。
            "expires_at": 入库到期,
        }
        if 锚定:
            条目["rel_bbox"] = [int(l) - 锚定["win_rect"][0], int(t) - 锚定["win_rect"][1],
                                int(r) - 锚定["win_rect"][0], int(b) - 锚定["win_rect"][1]]
            条目.update(锚定)
            条目["bucket"] = _空间桶(条目["rel_bbox"][0], 条目["rel_bbox"][1],
                                     锚定["win_size"][0], 锚定["win_size"][1])
        else:
            条目["legacy"] = True  # 窗口矩形取不到：与 v1 同待遇，stale 处理
        键 = (条目["name"], 条目["type"], tuple(条目.get("bucket") or ()))
        候选列表 = 索引.setdefault(键, [])
        # 精确坐标去重：与同桶同名现有候选左上角两轴差均 < 8px → 同一元素，
        # 更新原条目（幂等的根：检测抖动 ±2~3px 不产生重复条目）
        命中条目 = None
        if 条目.get("rel_bbox"):
            rl, rt = 条目["rel_bbox"][0], 条目["rel_bbox"][1]
            for 旧候选 in 候选列表:
                ob = 旧候选.get("rel_bbox")
                if ob and max(abs(ob[0] - rl), abs(ob[1] - rt)) < _去重距离:
                    命中条目 = 旧候选
                    break
        if 命中条目 is not None:
            命中条目.update(条目)
            命中条目.pop("stale_scroll", None)   # 本帧亲眼重扫到：坐标可信
            updated += 1
        elif len(候选列表) >= _桶内上限:
            # 同桶同名满 5 条：新观察淘汰入库时间最旧者（库容可控）
            最旧 = min(候选列表, key=lambda x: str(x.get("time") or ""))
            最旧.update(条目)
            最旧.pop("stale_scroll", None)   # 淘汰者被本帧新观察顶替：坐标可信
            updated += 1
        else:
            数据["entries"].append(条目)
            if 条目.get("rel_bbox"):
                候选列表.append(条目)
            added += 1
        if 向量开:
            向量记录.append(((条目["name"], 条目["type"],
                              tuple(条目.get("bbox") or ())),
                             (ix1, iy1, ix2, iy2)))

    if 元素数 or 滚动标记数:
        _写域(域名, 数据)
    # 向量编码（F3）：本轮写入条目批量过 SigLIP（同键更新也追加新向量行，
    # 查询端同键取最大 cos=新观察覆盖语义）；失败只记账不碍入库主流程
    向量账 = None
    if 向量开 and 向量记录:
        try:
            框s = [框 for _k, 框 in 向量记录]
            vecs = bookvec.编码裁片图(img, 框s)
            记录s = [(k, vecs[i]) for i, (k, _框) in enumerate(向量记录)]
            n = bookvec.追加向量(域名, 记录s)
            向量账 = {"encoded": n}
        except Exception as e:   # 模型缺失/推理失败：条目已入库，向量缺行
            向量账 = {"encoded": 0, "error": str(e)}
    legacy数 = sum(1 for e in 数据["entries"]
                  if isinstance(e, dict) and not e.get("rel_bbox"))
    # 重扫即归档（2026-10-29 第二单施工一）：本轮真扫到元素且域内仍有 legacy
    # 旧条目 → 该域 legacy 全部移出主库（旧坐标已废还占候选位，留着只会脏查）。
    # 判据取「scanned_in>0」而非「新增 v2≥legacy」：重扫多为同键更新（added=0），
    # 按新增判会永不清账；legacy 本就全 stale 不可用，归档无损失。
    归档数 = 0
    if 元素数 and legacy数:
        归档摘要 = 归档域(域名)
        归档数 = 归档摘要.get(域名, {}).get("archived", 0)
        legacy数 -= 归档数
    return {"domain": 域名, "added": added, "updated": updated,
            "total": len(数据["entries"]) - 归档数, "scanned_in": 元素数,
            "legacy": legacy数, "archived": 归档数,
            "scroll_staled": 滚动标记数, "vector": 向量账}


def _换算条目(e, 域名):
    """命中条目 → 返回副本：bbox 换算为当前坐标 + stale/pos_hint 如实标注。

    v2 条目（带 rel_bbox）：窗口活着且尺寸未变 → bbox=当前rect左上+rel_bbox、
    stale=false（窗口移动坐标随动）；尺寸变了/窗口关闭 → 保留入库时 bbox 并
    stale=true + reason，禁止静默返回过期坐标。v1 legacy 条目：一律 stale=true
    提示重扫。原条目不改动（库文件只写入时动）。
    """
    d = dict(e, domain=域名)
    rel = d.get("rel_bbox")
    if not isinstance(rel, list) or len(rel) != 4:
        d["legacy"] = True
        d["stale"] = True
        d["reason"] = "v1 旧条目（无窗口锚定），坐标不可信，请重扫刷新"
        bbox = d.get("bbox") or [0, 0, 0, 0]
        try:
            sw, sh = capture.screen_size()
            bx = min(_桶份数 - 1, max(0, int(bbox[0]) * _桶份数 // max(1, int(sw))))
            by = min(_桶份数 - 1, max(0, int(bbox[1]) * _桶份数 // max(1, int(sh))))
            d["pos_hint"] = _方位提示(bx, by, 前缀="屏幕")
        except Exception:
            d["pos_hint"] = None
        return d

    hwnd = int(d.get("hwnd") or 0)
    win_size = d.get("win_size") or [0, 0]
    bucket = d.get("bucket") or [0, 0]
    # 方位人话 + rel 坐标：桶内多候选后同桶同名 pos_hint 文案相同，附 rel
    # 坐标保证任意两条候选的 pos_hint 必可区分（近距双「确定」验收项）
    d["pos_hint"] = "%s(rel %d,%d)" % (_方位提示(bucket[0], bucket[1]),
                                       rel[0], rel[1])
    # 数据陈旧度：条目入库距今秒数——窗口没动但内容可能变过（滚动/翻页
    # 不改变窗口尺寸，无法从书库侧确知），age_s 让调用方自行判断可信度
    try:
        d["age_s"] = max(0, int(time.time() - time.mktime(
            time.strptime(d.get("time") or "", "%Y-%m-%d %H:%M:%S"))))
    except Exception:
        pass
    if hwnd:
        if not _窗口活着(hwnd):
            d["stale"] = True
            d["reason"] = "窗口已关闭"
            return d
        try:
            rect = [int(v) for v in capture.window_rect_hwnd(hwnd)]
        except Exception:
            d["stale"] = True
            d["reason"] = "取当前窗口矩形失败，请重扫"
            return d
        cur_size = [rect[2] - rect[0], rect[3] - rect[1]]
        if cur_size == list(win_size):
            d["bbox"] = [rect[0] + rel[0], rect[1] + rel[1],
                         rect[0] + rel[2], rect[1] + rel[3]]
            d["cur_win_rect"] = rect
            滚标 = d.get("stale_scroll")
            if 滚标:
                # 滚动失效（2026-10-31 第三单）：窗口尺寸没变但页面滚动过，
                # 元素已随内容移位——如实报 stale，禁止静默返回错坐标
                d["stale"] = True
                if isinstance(滚标, str):   # content_shifted 兜底（平移未检出）
                    d["reason"] = ("入库后页面内容大幅变化过（疑似滚动/翻页），"
                                   "bbox 为入库时坐标，须重扫")
                else:
                    dy = 滚标[1] if isinstance(滚标, (list, tuple)) and len(滚标) >= 2 else 0
                    d["reason"] = ("入库后页面滚动过（位移 y%+d），元素已移位，"
                                   "bbox 为入库时坐标，须重扫" % dy)
            else:
                d["stale"] = False
        else:
            d["stale"] = True
            d["reason"] = ("窗口尺寸已变化（入库 %dx%d → 现 %dx%d），"
                           "bbox 为入库时坐标，须重扫"
                           % (win_size[0], win_size[1], cur_size[0], cur_size[1]))
        return d
    # 全屏条目（hwnd=0，win_rect 即屏幕）
    try:
        sw, sh = capture.screen_size()
    except Exception:
        sw, sh = 0, 0
    if [int(sw), int(sh)] == list(win_size):
        d["bbox"] = list(rel)
        滚标 = d.get("stale_scroll")
        if 滚标:
            d["stale"] = True
            if isinstance(滚标, str):   # content_shifted 兜底（平移未检出）
                d["reason"] = ("入库后页面内容大幅变化过（疑似滚动/翻页），"
                               "bbox 为入库时坐标，须重扫")
            else:
                dy = 滚标[1] if isinstance(滚标, (list, tuple)) and len(滚标) >= 2 else 0
                d["reason"] = ("入库后页面滚动过（位移 y%+d），元素已移位，"
                               "bbox 为入库时坐标，须重扫" % dy)
        else:
            d["stale"] = False
    else:
        d["stale"] = True
        d["reason"] = "屏幕分辨率已变化（入库 %dx%d → 现 %dx%d），须重扫" % (
            win_size[0], win_size[1], sw, sh)
    return d


def 查找明细(query=None, domain=None, 类型=None, limit=30,
             include_legacy=False):
    """查找() 明细版：返回 (命中列表, legacy排除数, 过期排除数)——CLI/MCP 摘要
    如实反映「有多少旧条目/到期条目被默认排除」。查找() 即本函数的首个返回值。

    TTL 消费（F1 施工②）：expires_at 已到的条目一律排除（含 include_legacy=
    True——到期是生命周期语义，与 v1/v2 无关，作废条目查出来只能误导）。
    """
    命中 = []
    排除 = 0
    过期排除 = 0
    if not os.path.isdir(库根):
        return 命中, 排除, 过期排除
    # 指针索引（2026-11-01 方案页，11-01 晚定案默认翻转）：默认走 by_type
    # 指针片；BOOKLIB_INDEX=0 显式关闭。索引层任何异常自动回退下方全扫旧路径
    # （entries.json 仍是正本，回退无险）
    if os.environ.get("BOOKLIB_INDEX") != "0":
        try:
            from . import bookindex
            return bookindex.查找明细(query=query, domain=domain, 类型=类型,
                                      limit=limit, include_legacy=include_legacy)
        except Exception:
            pass
    q = (query or "").lower()
    for 域名 in sorted(os.listdir(库根)):
        if 域名.startswith("_"):   # _index 等派生目录不是域
            continue
        域目录 = os.path.join(库根, 域名)
        if not os.path.isdir(域目录):
            continue
        if domain and domain.lower() not in 域名.lower():
            continue
        数据 = _读域(域名)
        for e in 数据["entries"]:
            if q and q not in str(e.get("name", "")).lower():
                continue
            if 类型 and e.get("type") != 类型:
                continue
            # TTL 到期条目排除（F1 施工②）：作废候选不再占位（计数如实上报）
            if _条目过期(e):
                过期排除 += 1
                continue
            # 默认排除 legacy（无 rel_bbox 的 v1 旧条目）：坐标不可信还占
            # 候选位；include_legacy=True 时旧行为原样返回
            if not include_legacy and not e.get("rel_bbox"):
                排除 += 1
                continue
            命中.append(_换算条目(e, 域名))
            if len(命中) >= limit:
                return 命中, 排除, 过期排除
    return 命中, 排除, 过期排除


def 查找(query=None, domain=None, 类型=None, limit=30, include_legacy=False):
    """文本检索书库：跨域遍历 manual/*/entries.json。

    query：名字子串匹配（不区分大小写）；domain：域名子串匹配；
    类型：精确匹配元素 type。命中条目附 domain 字段返回。

    v2：同名元素按空间桶并存为多条候选（受 limit 限），每条附 stale 与
    pos_hint——stale=false 的 bbox 已按当前窗口矩形换算（窗口移动坐标随动），
    stale=true 须重扫（reason 说明原因）；单条命中时结构与 v1 一致（向后兼容）。
    include_legacy：是否含 v1 旧条目（默认 False——stale 坐标不可信不占候选位，
    排除数由查找明细() 供 CLI/MCP 摘要；True=旧行为）。
    """
    命中, _排除, _过期 = 查找明细(query=query, domain=domain, 类型=类型,
                                 limit=limit, include_legacy=include_legacy)
    return 命中


def 域清单():
    """列出所有域及各自条目数（检索前探库用）。"""
    域 = []
    if not os.path.isdir(库根):
        return 域
    for 域名 in sorted(os.listdir(库根)):
        数据 = _读域(域名)
        if 数据["entries"]:
            域.append({"domain": 域名, "total": len(数据["entries"]),
                       "updated": 数据.get("updated", "")})
    return 域


# ── 清扫（2026-11-01 书库治理 A1）────────────────────────────────────────
# 背景：书库只增不减——scan 全量入库 + 测试靶域 + 畸形域名 + 空域长期堆积。
# 本组提供三种清扫模式，红线与归档一致：移动不删除、先备份、默认干跑。

def 库概况():
    """全库体检：每域条目数/裁片数/体积/最后更新/到期数，另标空域。供 booksweep --list。"""
    rows = []
    if not os.path.isdir(库根):
        return {"domains": rows, "total_entries": 0, "total_bytes": 0,
                "total_expired": 0}
    for 域名 in sorted(os.listdir(库根)):
        if 域名.startswith("_"):   # _index 等派生目录不是域
            continue
        目录 = _域路径(域名)
        if not os.path.isdir(目录):
            continue
        数据 = _读域(域名)
        条目数 = len(数据["entries"])
        到期数 = sum(1 for e in 数据["entries"] if _条目过期(e))
        裁片数 = 体积 = 0
        for 名 in os.listdir(目录):
            路径 = os.path.join(目录, 名)
            if os.path.isfile(路径):
                体积 += os.path.getsize(路径)
                if 名.lower().endswith(".png"):
                    裁片数 += 1
        rows.append({"domain": 域名, "entries": 条目数, "crops": 裁片数,
                     "bytes": 体积, "updated": 数据.get("updated", ""),
                     "expired": 到期数, "empty": 条目数 == 0})
    return {"domains": rows,
            "total_entries": sum(r["entries"] for r in rows),
            "total_bytes": sum(r["bytes"] for r in rows),
            "total_expired": sum(r["expired"] for r in rows)}


def _备份域(域名, 标记):
    """清扫前整域复制到 manual_backup_<标记>/<域>/（含 entries.json 与裁片）。

    复制不是移动——清扫失败时原库完好；备份目录名带标记可追溯。
    """
    源 = _域路径(域名)
    目标根 = os.path.join(项目根, "manual_backup_%s" % 标记)
    目标 = os.path.join(目标根, 域名)
    if os.path.isdir(目标):
        return 目标  # 同标记重跑不覆盖首份备份
    os.makedirs(目标根, exist_ok=True)
    shutil.copytree(源, 目标)
    return 目标


def 清扫(域名=None, 空域=False, 龄期天数=None, 过期=False, 干跑=True, 标记=None):
    """书库清扫（四模式，可组合；A1 施工，F1 增过期模式）。

    - 域名：子串匹配（同 _选域 口径），缺省=全部域；
    - 空域=True：清空匹配域的全部条目与裁片（整域移入备份，可整域恢复）；
    - 龄期天数=N：删除 time 早于 N 天的条目（裁片一并删；备份 entries.json）；
    - 过期=True：清 TTL 已到期条目（expires_at 非空且已到；F1 施工②消费侧）；
    - 干跑=True（默认）：只出账不动库；调用方必须显式关掉才真干。

    返回 {"dry_run", "backup", "domains": [{domain, mode, entries_before,
    entries_after, removed, backup}]}。红线：先备份再动、移动不硬删。
    """
    标记 = 标记 or time.strftime("%Y%m%d_%H%M%S")
    报告 = {"dry_run": bool(干跑), "backup": "", "domains": []}
    if not os.path.isdir(库根):
        return 报告
    候选 = _选域(域名, 库根)
    if not 候选:
        return 报告
    截止 = ""
    if 龄期天数 is not None:
        截止 = time.strftime("%Y-%m-%d %H:%M:%S",
                           time.localtime(time.time() - float(龄期天数) * 86400))
    for 域 in 候选:
        数据 = _读域(域)
        原数 = len(数据["entries"])
        if 空域:
            命中 = list(range(原数))          # 整域清
        elif 过期:
            命中 = [i for i, e in enumerate(数据["entries"]) if _条目过期(e)]
        elif 龄期天数 is not None:
            命中 = [i for i, e in enumerate(数据["entries"])
                    if isinstance(e, dict) and str(e.get("time") or "") < 截止]
        else:
            命中 = []
        if not 命中 and not 空域:
            报告["domains"].append({"domain": 域, "mode": "skip", "entries_before": 原数,
                                    "entries_after": 原数, "removed": 0, "backup": ""})
            continue
        模式 = ("empty" if 空域 else
                ("expired" if 过期 else ("older_than_%sd" % 龄期天数)))
        if 干跑:
            报告["domains"].append({"domain": 域, "mode": 模式, "entries_before": 原数,
                                    "entries_after": 原数 - len(命中),
                                    "removed": len(命中), "backup": "(dry-run 未备份)"})
            continue
        目标 = _备份域(域, 标记)
        报告["backup"] = os.path.join(项目根, "manual_backup_%s" % 标记)
        if 空域:
            # 整域清：备份已完整（含裁片），直接移除域目录，不留空壳
            shutil.rmtree(_域路径(域), ignore_errors=True)
            报告["domains"].append({"domain": 域, "mode": 模式, "entries_before": 原数,
                                    "entries_after": 0, "removed": len(命中),
                                    "backup": 目标})
            continue
        保留 = []
        for i, e in enumerate(数据["entries"]):
            if i in set(命中):
                # 裁片随条目删（备份里已有原图，可恢复）
                裁片 = e.get("crop") if isinstance(e, dict) else None
                if 裁片:
                    路径 = os.path.join(_域路径(域), 裁片)
                    try:
                        if os.path.isfile(路径):
                            os.remove(路径)
                    except Exception:
                        pass
                continue
            保留.append(e)
        数据["entries"] = 保留
        _写域(域, 数据)
        报告["domains"].append({"domain": 域, "mode": 模式, "entries_before": 原数,
                                "entries_after": len(保留), "removed": len(命中),
                                "backup": 目标})
    return 报告


def 清孤儿(干跑=True, 标记=None):
    """清孤儿裁片（书库生命周期第四模式，2026-11-01 残账清理）。

    孤儿 = 域目录里存在、但 entries.json 无任何条目引用的 png（同名覆盖/
    重复入库的历史残渣）。book_find 永远查不到它们，留着只占体积。

    红线同清扫：默认干跑；真干前把孤儿逐域移入 manual_backup_<标记>/孤儿/<域>/
    （移动不是硬删，可手工搬回）。
    返回 {"dry_run", "backup", "domains": [{domain, orphans}]}。
    """
    标记 = 标记 or time.strftime("%Y%m%d_%H%M%S")
    报告 = {"dry_run": bool(干跑), "backup": "", "domains": []}
    if not os.path.isdir(库根):
        return 报告
    总孤儿 = 0
    for 域 in sorted(os.listdir(库根)):
        域目录 = _域路径(域)
        if not os.path.isdir(域目录):
            continue
        数据 = _读域(域)
        引用 = {e.get("crop") for e in 数据["entries"] if isinstance(e, dict)}
        孤儿 = [f for f in sorted(os.listdir(域目录))
                if f.lower().endswith(".png") and f not in 引用]
        if not 孤儿:
            continue
        if 干跑:
            报告["domains"].append({"domain": 域, "orphans": len(孤儿)})
            总孤儿 += len(孤儿)
            continue
        目标根 = os.path.join(项目根, "manual_backup_%s" % 标记, "孤儿", 域)
        os.makedirs(目标根, exist_ok=True)
        搬走 = 0
        for f in 孤儿:
            try:
                shutil.move(os.path.join(域目录, f), os.path.join(目标根, f))
                搬走 += 1
            except Exception:
                pass
        if 搬走:
            报告["domains"].append({"domain": 域, "orphans": 搬走})
            总孤儿 += 搬走
    if not 干跑 and 总孤儿:
        报告["backup"] = os.path.join(项目根, "manual_backup_%s" % 标记, "孤儿")
    报告["total_orphans"] = 总孤儿
    return 报告


def 恢复清扫(标记):
    """把 manual_backup_<标记>/ 下的域整域复制回主库（清扫的逆操作）。

    已存在的同域目录不动（防覆盖在用数据），只回补缺失域；返回恢复域列表。
    """
    备份根 = os.path.join(项目根, "manual_backup_%s" % 标记)
    恢复 = []
    if not os.path.isdir(备份根):
        return 恢复
    for 域 in sorted(os.listdir(备份根)):
        源 = os.path.join(备份根, 域)
        目标 = _域路径(域)
        if not os.path.isdir(源) or os.path.isdir(目标):
            continue
        shutil.copytree(源, 目标)
        恢复.append(域)
    return 恢复


# ── backup-gc 备份生命周期（2026-11-01 F1 施工①）─────────────────────────
# 背景：清扫/归档每次都先整域备份（manual_backup_*），只增不减——13 份纯占盘。
# 规则（定线原文）：每域留最近 2 份 + 超 30 天的份整体清。干跑默认。

def _备份份时间(目录名):
    """manual_backup_<标记>[后缀] → 份时间 epoch；解析不出返回 None。

    标记两种：%Y%m%d_%H%M%S（清扫时间戳）/ %Y%m%d（prearchive 按日）。
    解析不出的份不参与 GC（宁保留勿误删）。
    """
    m = 目录名[len("manual_backup_"):].replace("_prearchive", "")
    for fmt in ("%Y%m%d_%H%M%S", "%Y%m%d"):
        try:
            return time.mktime(time.strptime(m, fmt))
        except ValueError:
            continue
    return None


def 备份回收(干跑=True, 每域保留=2, 龄期天数=30):
    """备份生命周期回收（booksweep --backup-gc，F1 施工①）。

    对象=项目根/manual_backup_*（清扫/归档/迁移产生的整域备份份）。
    规则两把刀（取并集）：
    - 超 30 天的份整体清（硬龄线，30 天内也只当近期备份用）；
    - 每域只留最近 2 份（龄线内多份里，每域第 3 份起的域目录清；清完空份
      目录一并删）。
    删除=真删（rmtree）——GC 对象本身就是备份，再「备份备份」无限递归，
    违背每域≤2 验收线；安全闸=干跑默认 + 调用方显式 --yes，真删清单全打印。
    解析不出时间的份/域目录一律不动。
    返回 {"dry_run", "keep_per_domain", "max_age_days", "removed"/"would_remove",
    "freed_bytes"/"would_free_bytes", "domain_kept"}。
    """
    报告 = {"dry_run": bool(干跑), "keep_per_domain": 每域保留,
            "max_age_days": 龄期天数}
    if not os.path.isdir(项目根):
        return 报告
    now = time.time()
    份s = {}    # 份路径 -> 份时间（None=解析不出）
    域占 = {}   # 域名 -> [(份时间, 份路径, 域目录)]
    for 名 in sorted(os.listdir(项目根)):
        if not 名.startswith("manual_backup_"):
            continue
        路径 = os.path.join(项目根, 名)
        if not os.path.isdir(路径):
            continue
        t = _备份份时间(名)
        份s[路径] = t
        for d in sorted(os.listdir(路径)):
            域目录 = os.path.join(路径, d)
            if os.path.isdir(域目录):
                域占.setdefault(d, []).append((t, 路径, 域目录))
    删集 = set()
    # 刀一：超龄份整体清（解析不出时间的份不动）
    for 路径, t in 份s.items():
        if t is not None and (now - t) > 龄期天数 * 86400:
            删集.add(路径)
    # 刀二：每域留最近 N 份（在未被整份清掉的份里比）
    for 出现 in 域占.values():
        存活 = [x for x in 出现 if x[1] not in 删集]
        有时 = sorted((x for x in 存活 if x[0] is not None),
                     key=lambda x: x[0], reverse=True)
        for _t, _p, 目录 in 有时[max(0, int(每域保留)):]:
            删集.add(目录)
    # 去嵌套：整份已在删集的，份内域目录从删集去重（父路径吸收子路径）
    终删 = []
    for p in sorted(删集):
        if any(p == q or p.startswith(q + os.sep) for q in 终删):
            continue
        终删.append(p)

    def _体积(p):
        n = 0
        for 根, _ds, fs in os.walk(p):
            for f in fs:
                try:
                    n += os.path.getsize(os.path.join(根, f))
                except OSError:
                    pass
        return n

    清单 = []
    for p in 终删:
        t = 份s.get(p)
        if t is None and os.path.dirname(p) in 份s:
            t = 份s[os.path.dirname(p)]   # 域目录项：龄期随父份
        清单.append({"path": p, "bytes": _体积(p),
                     "age_days": (round((now - t) / 86400, 1)
                                  if t is not None else None)})
    # 每域留存账：未被删的份里各域目录数
    删路径集 = set(终删)
    留存 = {}
    for d, 出现 in 域占.items():
        留存[d] = sum(1 for _t, p, 目录 in 出现
                      if p not in 删路径集 and 目录 not in 删路径集)
    报告["domain_kept"] = 留存
    报告["max_age_days"] = 龄期天数
    if 干跑:
        报告["would_remove"] = 清单
        报告["would_free_bytes"] = sum(x["bytes"] for x in 清单)
    else:
        for x in 清单:
            shutil.rmtree(x["path"], ignore_errors=True)
        报告["removed"] = 清单
        报告["freed_bytes"] = sum(x["bytes"] for x in 清单)
    return 报告


def 迁移域(旧域, 新域, 标记=None):
    """把一个域的条目并入另一个域（历史畸形域名整改用，2026-11-01 A1）。

    用途：`D%3A` / `web_<路径>` 这类历史畸形域名，在 url主机 修正后已不会再
    产生；存量数据并入可读域名（如「文件-D盘」），域清单回归可读。

    红线同归档：先备份旧域到 manual_backup_<标记>/，再搬条目 + 裁片，
    domain_key 一并改写；目标域按（名,类型,桶,近坐标）去重，搬完删旧域。
    返回 {"from", "to", "moved", "duplicated", "backup"}。
    """
    标记 = 标记 or time.strftime("%Y%m%d_%H%M%S")
    结果 = {"from": 旧域, "to": 新域, "moved": 0, "duplicated": 0, "backup": ""}
    if not os.path.isdir(_域路径(旧域)):
        return 结果
    旧数据 = _读域(旧域)
    if not 旧数据["entries"]:
        结果["backup"] = ""
        return 结果
    _备份域(旧域, 标记)
    结果["backup"] = os.path.join(项目根, "manual_backup_%s" % 标记, 旧域)
    新数据 = _读域(新域)
    新目录 = _域路径(新域)
    os.makedirs(新目录, exist_ok=True)
    已有 = set()
    for e in 新数据["entries"]:
        if isinstance(e, dict):
            已有.add((e.get("name"), e.get("type"), tuple(e.get("bbox") or ())))
    for e in 旧数据["entries"]:
        if not isinstance(e, dict):
            continue
        键 = (e.get("name"), e.get("type"), tuple(e.get("bbox") or ()))
        if 键 in 已有:
            结果["duplicated"] += 1
            continue
        条目 = dict(e)
        条目["domain_key"] = 新域
        裁片 = 条目.get("crop")
        if 裁片:
            源图 = os.path.join(_域路径(旧域), 裁片)
            目标图 = os.path.join(新目录, 裁片)
            try:
                if os.path.isfile(源图) and not os.path.isfile(目标图):
                    shutil.copy2(源图, 目标图)
            except Exception:
                pass
        新数据["entries"].append(条目)
        已有.add(键)
        结果["moved"] += 1
    _写域(新域, 新数据)
    shutil.rmtree(_域路径(旧域), ignore_errors=True)
    return 结果


