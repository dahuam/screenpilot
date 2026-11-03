# -*- coding: utf-8 -*-
"""书库指针索引层（2026-11-01 深夜·决策页「书库指针索引化」施工①②）。

设计定调：书库采用指针索引的方式，分类保存；识屏=AI 的眼睛，三把尺子
（速度↑ 准确率↑ 资源占用↓）。

设计（方案页 notes/方案-书库指针索引化_20261101.md）：
- entries.json 域账本仍是唯一正本，本模块只建派生索引视图——索引坏了
  rebuild 即愈，不动任何存量数据。
- 分类保存：manual/_index/by_type/<type>.json 分片，条目=瘦指针
  {name, type, domain, bbox, crop, legacy}（bbox=入库绝对坐标，与 _条目键
  同源，可唯一定位正本条目）。
- 一致性：每片带全库指纹（各域 entries.json 的 mtime+size 哈希）。任何写
  路径（入库/清扫/迁移/恢复归档）都不必挂钩子——指纹一变自动重建，不会漏。
- 检索：query 在指针片上子串过滤（片小，只读相关片）→ 命中的指针按
  (name,type,bbox) 回读所在域账本取全条 → 复用 booklib._换算条目 保持
  输出结构 100% 兼容（stale/pos_hint 语义不变）。

样本防注入纪律同书库：指针里的名字只做子串匹配，不解释执行。
"""
import hashlib
import json
import os

from . import booklib

索引根 = os.path.join(booklib.库根, "_index")

# 瘦指针保留字段：定位正本（name/type/bbox）+ 摘要展示（crop/time）+ 过滤
# （legacy/expires_at）。其余重元数据（window_title/hwnd/conf/source/win_rect…）
# 要时按指针回读正本条目取得。expires_at 为 F1 TTL 消费预留——旧索引片无此
# 字段时 get 返回 None=不过期（兼容安全），指纹失效重建后自动带上。
_指针字段 = ("name", "type", "domain", "bbox", "crop", "time", "legacy",
            "expires_at")


def _指纹():
    """全库指纹：各域 entries.json 的 (域名, mtime_ns, size) 排序哈希。"""
    h = hashlib.sha1()
    if os.path.isdir(booklib.库根):
        for 域名 in sorted(os.listdir(booklib.库根)):
            路径 = os.path.join(booklib.库根, 域名, "entries.json")
            if os.path.isfile(路径):
                st = os.stat(路径)
                h.update(("%s\x00%d\x00%d\x01" % (域名, st.st_mtime_ns, st.st_size)).encode("utf-8", "ignore"))
    return h.hexdigest()


def _片路径():
    return os.path.join(索引根, "index.json")


def _载索引():
    """读索引（内存缓存，指纹失效自动过期）。返回 {type: [指针,...]} 或 None。"""
    global _索引缓存, _索引缓存指纹
    fp = _指纹()
    if _索引缓存 is not None and _索引缓存指纹 == fp:
        return _索引缓存
    if os.path.isfile(_片路径()):
        try:
            with open(_片路径(), "r", encoding="utf-8") as f:
                数据 = json.load(f)
            if 数据.get("fingerprint") == fp:
                _索引缓存 = 数据.get("by_type") or {}
                _索引缓存指纹 = fp
                return _索引缓存
        except Exception:
            pass
    return None


_索引缓存 = None
_索引缓存指纹 = ""

# 域账本回读缓存（同指纹内有效）——MCP 常驻进程重复 find 同域不再反复 parse
_域缓存 = {}
_域缓存指纹 = ""


def _回读域(域名):
    global _域缓存, _域缓存指纹
    fp = _指纹()
    if _域缓存指纹 != fp:
        _域缓存 = {}
        _域缓存指纹 = fp
    if 域名 not in _域缓存:
        _域缓存[域名] = booklib._读域(域名)["entries"]
    return _域缓存[域名]


def 重建():
    """全量重建索引（冷启动/指纹过期/怀疑不一致时跑）。返回摘要。"""
    指针数 = 0
    域数 = 0
    片 = {}   # type -> [指针, ...]
    if os.path.isdir(booklib.库根):
        for 域名 in booklib._选域(None, booklib.库根):
            数据 = booklib._读域(域名)
            if not 数据["entries"]:
                continue
            域数 += 1
            for i, e in enumerate(数据["entries"]):
                if not isinstance(e, dict):
                    continue
                p = {k: e.get(k) for k in _指针字段}
                p["domain"] = 域名
                p["legacy"] = not e.get("rel_bbox")
                p["seq"] = i
                片.setdefault(str(e.get("type") or "unknown"), []).append(p)
                指针数 += 1
    os.makedirs(索引根, exist_ok=True)
    fp = _指纹()
    with open(_片路径(), "w", encoding="utf-8") as f:
        json.dump({"updated": booklib.time.strftime("%Y-%m-%d %H:%M:%S"),
                   "fingerprint": fp, "by_type": 片}, f, ensure_ascii=False, indent=1)
    global _索引缓存, _索引缓存指纹, _域缓存
    _索引缓存 = 片
    _索引缓存指纹 = fp
    _域缓存 = {}
    return {"pointers": 指针数, "domains": 域数, "types": len(片), "fingerprint": fp}


def 有效():
    """指纹一致且索引文件可读 = 索引可用。不一致返回 False（调用方先重建）。"""
    return _载索引() is not None


def _全部指针(类型=None, domain=None):
    索引 = _载索引()
    if 索引 is None:
        raise RuntimeError("索引未就绪")
    类型s = [类型] if 类型 else sorted(索引)
    指针s = []
    for t in 类型s:
        for p in 索引.get(t) or []:
            if domain and domain.lower() not in str(p.get("domain", "")).lower():
                continue
            指针s.append(p)
    return 指针s


def _回读全条(指针):
    """按指针 (name,type,bbox) 回读所在域账本取全条；取不到返回 None（正本
    已被清扫/改动而指纹尚未过期不会发生——指纹含该域 mtime；兜底宁缺勿错）。"""
    数据 = booklib._读域(指针.get("domain"))
    键 = (str(指针.get("name")), str(指针.get("type")),
          tuple(指针.get("bbox") or ()))
    for e in 数据["entries"]:
        if (str(e.get("name")), str(e.get("type")), tuple(e.get("bbox") or ())) == 键:
            return e
    return None


def 查找明细(query=None, domain=None, 类型=None, limit=30, include_legacy=False):
    """索引路径的查找明细：返回 (命中列表, legacy排除数, 过期排除数)，结构与
    booklib.查找明细 完全一致。索引不可用时抛异常，由调用方回退旧路径。
    TTL 到期条目一律排除（F1 施工②；含 include_legacy=True，作废不占候选）。"""
    if not 有效():
        重建()
    q = (query or "").lower()
    命中 = []
    排除 = 0
    过期排除 = 0
    for p in _全部指针(类型=类型, domain=domain):
        if q and q not in str(p.get("name", "")).lower():
            continue
        if booklib._条目过期(p):
            过期排除 += 1
            continue
        if not include_legacy and p.get("legacy"):
            排除 += 1
            continue
        命中.append(p)
    # 与旧路径同序：域字典序 → 域内条目序，limit 截断集完全一致（回归零差异）
    命中.sort(key=lambda p: (str(p.get("domain")), p.get("seq") or 0))
    结果 = []
    for p in 命中:
        域名 = p.get("domain")
        全条 = None
        键 = (str(p.get("name")), str(p.get("type")), tuple(p.get("bbox") or ()))
        for e in _回读域(域名):
            if (str(e.get("name")), str(e.get("type")), tuple(e.get("bbox") or ())) == 键:
                全条 = e
                break
        if 全条 is None:
            continue   # 指针悬空（理论上指纹机制防住），宁缺勿错
        结果.append(booklib._换算条目(全条, 域名))
        if len(结果) >= limit:
            return 结果, 排除, 过期排除
    return 结果, 排除, 过期排除
