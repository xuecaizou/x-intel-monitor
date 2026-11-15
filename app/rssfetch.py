"""RSS 源监控：抓取通用 RSS/Atom 源（TheHackersNews 等），条目复用 tweets 表与 AI/推送管道。

与 X 账号共用 tweets 表：screen_name=RSS 源名，tweet_id=条目 guid/link 哈希（幂等去重），
media 提取 description 内首图。每源独立 fetch_minutes（0=跟随全局），置顶式增量：
增量：逐条扫描去重（2026-11-09 I 修复——不再按首条 guid 短路整源），
位点 rss_top_{id} 仅作追踪参考、入库完成后推进；source_id=rss:{源id}。
"""
import hashlib
import json
import html as htmllib
import re
from datetime import datetime, timezone, timedelta

import feedparser

from . import db
from .twitter import _http_get

_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def _norm_time(s):
    """RSS/Atom 时间 → GMT 裸值 'YYYY-MM-DD HH:MM'（与 X 侧 published_at 同格式、可直接字符串排序）。
    支持 RFC2822（如 Fri, 06 Nov 2026 17:00:00 +0530）、ISO8601 带偏移。无法解析返回空串。"""
    s = (s or "").strip()
    if not s:
        return ""
    m = re.match(r"(?:[A-Za-z]{3},\s*)?(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\s+"
                 r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([+-]\d{4}|UTC|GMT|Z|IST|EST|PST)?", s)
    if m and _MONTHS.get(m.group(2).title()):
        dt = datetime(int(m.group(3)), _MONTHS[m.group(2).title()], int(m.group(1)),
                      int(m.group(4)), int(m.group(5)), tzinfo=timezone.utc)
        tz = (m.group(7) or "").upper()
        if tz.startswith(("+", "-")) and len(tz) == 5:
            off = timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5]))
            dt = dt - (off if tz[0] == "+" else -off)
        elif tz == "IST":
            dt = dt - timedelta(hours=5, minutes=30)
        elif tz == "EST":
            dt = dt + timedelta(hours=5)
        elif tz == "PST":
            dt = dt + timedelta(hours=8)
        return dt.strftime("%Y-%m-%d %H:%M")
    m2 = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", s)
    if m2:
        try:
            dt = datetime(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)),
                          int(m2.group(4)), int(m2.group(5)), tzinfo=timezone.utc)
            mz = re.search(r"([+-])(\d{2}):?(\d{2})$", s)
            if mz:
                off = timedelta(hours=int(mz.group(2)), minutes=int(mz.group(3)))
                dt = dt - (off if mz.group(1) == "+" else -off)
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    return ""


def _clean(desc):
    s = re.sub(r"<[^>]+>", " ", desc or "")
    s = htmllib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _entry_id(e):
    """条目稳定 id：guid 优先，退化用 link，再退化 title 哈希"""
    raw = (e.get("id") or e.get("link") or e.get("title") or "").strip()
    return hashlib.md5(raw.encode("utf-8", "ignore")).hexdigest()[:20]


def _extract_media(desc):
    m = _IMG_RE.search(desc or "")
    if not m:
        return ""
    u = m.group(1)
    if u.startswith("//"):
        u = "https:" + u
    return u if "twimg.com" in u or u.startswith("http") else ""


def fetch_source(src, cfg, limit=15):
    """抓取单个 RSS 源，返回 (新条目数, 追踪明细, 错误)。
    条目字段与 twitter._tweet 对齐，直接 insert_tweet 进管道。

    增量语义（2026-11-09 缺陷 I 修复）：**逐条扫描去重，不再按首条 GUID
    短路整源**——历史缺陷：首位是置顶条目或新条目插入非首位时，首条 GUID
    未变 → 整源跳过 → 漏采。幂等由 insert_tweet UNIQUE(source_id, tweet_id)
    保证；位点 rss_top_{id} 仅在本轮全部入库尝试完成后推进（入库异常不推进），
    只作追踪参考不再参与跳过判断；条目携带 source_id（rss:{id}）——来源改名
    不重复入库、归属不变。"""
    name, url = src["name"], src["url"]
    sid = src["id"]
    # 2026-11-16 round2 需求 D：RSS 独立网络选择——默认**直连**（不再继承 X 的
    # socks5 代理）。显式配置 rss.proxy / 源级 proxy 时走代理并受 trusted_proxy 约束。
    rcfg = dict(cfg.get("rss") or {})
    proxy = (src.get("proxy") or "").strip() or (rcfg.get("proxy") or "").strip()
    diag = {"attempt_at": db.now(), "ok_at": "", "http_status": 0, "parsed": 0,
            "scanned": 0, "new": 0, "dup": 0, "error": "", "error_class": "",
            "via_proxy": bool(proxy), "trigger": src.get("_trigger") or ""}
    db.set_kv(f"rss_diag_{sid}", json.dumps(diag, ensure_ascii=False))
    try:
        # SSRF 校验（第四批）：仅 http/https；禁止回环/私网/链路本地（可用 fetch.allow_private_urls 显式放行）
        from .safefetch import validate_url, UnsafeUrlError, check_proxy_trust, fetch_limited
        from .ai import classify_error
        from .twitter import _proxies
        try:
            validate_url(url, purpose="rss")   # RSS 永不允许私网（无白名单用途）
            check_proxy_trust("rss", proxy)
        except UnsafeUrlError as ue:
            diag["error"] = f"URL 拒绝（出站策略）：{ue}"
            diag["error_class"] = "outbound_policy"
            db.set_kv(f"rss_diag_{sid}", json.dumps(diag, ensure_ascii=False))
            return 0, "", diag["error"]
        # 2026-11-09 H：受控抓取——重定向逐跳校验、≤8MB、显式超时在应用层实际生效
        status, content, _final = fetch_limited(
            url, timeout=max(20, int((src.get("_timeout") or 20))),
            purpose="rss", proxies=_proxies(proxy))
        diag["http_status"] = int(status)
        if status != 200:
            diag["error"] = f"HTTP {status}"
            diag["error_class"] = "http"
            db.set_kv(f"rss_diag_{sid}", json.dumps(diag, ensure_ascii=False))
            return 0, f"HTTP {status}", f"HTTP {status}"
        if not (content.lstrip()[:5].startswith(b"<?xml") or b"<feed" in content[:600]
                or b"<rss" in content[:600]):
            diag["error"] = "响应非 RSS/Atom（可能被 WAF 拦截）"
            diag["error_class"] = "content"
            db.set_kv(f"rss_diag_{sid}", json.dumps(diag, ensure_ascii=False))
            return 0, "响应非 RSS/Atom", "非 RSS 内容（可能被 WAF 拦截）"
        feed = feedparser.parse(content)
        diag["parsed"] = len(feed.entries or [])
        if not feed.entries:
            diag["error"] = "空 feed（解析 0 条目）"
            diag["error_class"] = "empty"
            db.set_kv(f"rss_diag_{sid}", json.dumps(diag, ensure_ascii=False))
            return 0, "0 条目", "空 feed"
        scanned = feed.entries[:limit]
        diag["scanned"] = len(scanned)
        new = 0
        for e in scanned:
            eid = _entry_id(e)
            link = e.get("link") or ""
            desc = e.get("description", "") or e.get("summary", "") or ""
            text = _clean(desc) or (e.get("title") or "").strip()
            if not text:
                continue
            t = {
                "screen_name": f"RSS:{name}",  # 加前缀防与同名 X 账号数据混淆
                "tweet_id": eid,
                "source_id": f"rss:{src['id']}",  # 稳定来源身份：改名不重复入库
                "text": (e.get("title", "").strip() + "\n" + text).strip()[:2000],
                "url": link,
                "published_at": _norm_time(e.get("published") or e.get("updated") or ""),
                "is_retweet": 0,
                "is_reply": 0,
                "media_url": _extract_media(desc),
            }
            rid = db.insert_tweet(t)
            if rid:
                new += 1
            else:
                diag["dup"] += 1
        # 位点推进：仅在全部条目入库尝试完成（无异常）后写入，仅作追踪参考
        db.set_kv(f"rss_top_{sid}", _entry_id(scanned[0]))
        diag["new"] = new
        diag["ok_at"] = db.now()
        diag["error"] = ""
        diag["error_class"] = ""
        db.set_kv(f"rss_diag_{sid}", json.dumps(diag, ensure_ascii=False))
        trace = f"✓{new}条/{len(scanned)}扫描（去重{diag['dup']}）"
        return new, trace, ""
    except Exception as e:
        from .ai import classify_error
        diag["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        diag["error_class"] = classify_error(e)
        db.set_kv(f"rss_diag_{sid}", json.dumps(diag, ensure_ascii=False))
        return 0, "", diag["error"]


def _global_minutes(cfg):
    """RSS"跟随全局"频率：统一读调度字段 fetch.interval_minutes（原读 x.interval_minutes，
    与调度器使用的 fetch.interval_minutes 不一致，导致全局改 5 分钟时 RSS 仍按 15 分钟）"""
    try:
        return int((cfg.get("fetch") or {}).get("interval_minutes", 15) or 15)
    except Exception:
        return 15


def run_rss_round(trigger="schedule", force_ids=None):
    """一轮 RSS 抓取：到期源全部抓取，返回汇总 dict（供 engine 并入 fetch_logs）。

    2026-11-16 round2 需求 D 修复：
      - 节流位点改为 **成功时间**（rss_ok_{id}）：失败后立即可重试，
        不再出现"失败前写位点→手动重试 due=0 永不再试"；
      - force_ids：页面"立即抓取此源"——跳过到期判断（保留互斥）；
      - 进行中互斥：rss_busy_{id} 防并发重复抓取；调度跳过带原因（写入诊断）。
    """
    from .config import load
    import time as _t
    cfg = load()
    srcs = db.list_rss_sources(enabled_only=True)
    gmin = _global_minutes(cfg)
    try:
        rmax = int((cfg.get("rss") or {}).get("max_items") or 30)
    except Exception:
        rmax = 30
    force = {int(x) for x in (force_ids or []) if str(x).strip().lstrip("-").isdigit()}
    due, ok, total_new, traces, errs, skipped = 0, 0, 0, [], [], []
    tmo = max(20, int((cfg.get("rss") or {}).get("timeout_seconds")
                      or (cfg.get("fetch") or {}).get("timeout_seconds") or 20))
    for s in srcs:
        sid = s["id"]
        minutes = int(s.get("fetch_minutes") or 0) or gmin
        forced = sid in force
        busy = db.get_kv(f"rss_busy_{sid}")
        if busy and str(busy).strip() and _t.time() - float(busy) < 600:
            msg = "跳过：上一轮仍在进行中（互斥保护）"
            if forced:
                errs.append(f"{s['name']}: {msg}")
                traces.append(f"[{s['name']}] ⏳ {msg}")
            skipped.append({"id": sid, "name": s["name"], "reason": msg})
            continue
        last_ok = db.get_kv(f"rss_ok_{sid}")
        if not forced and last_ok and _t.time() - float(last_ok) < minutes * 60:
            skipped.append({"id": sid, "name": s["name"], "reason": "未到期"})
            continue
        db.set_kv(f"rss_busy_{sid}", str(_t.time()))
        due += 1
        try:
            new, trace, err = fetch_source({**s, "_timeout": tmo, "_trigger": trigger}, cfg,
                                           limit=rmax)
        finally:
            db.set_kv(f"rss_busy_{sid}", "")   # 清除互斥（原实现写当前时间导致 600s 误锁）
        total_new += new
        if err:
            errs.append(f"{s['name']}: {err}")
            traces.append(f"[{s['name']}] ✗ {err}")
        else:
            ok += 1
            # 节流位点=成功时间（失败后可立即重试——需求 D2 修复）
            db.set_kv(f"rss_ok_{sid}", str(_t.time()))
            traces.append(f"[{s['name']}] {trace}")
    return {"due": due, "ok": ok, "new": total_new, "skipped": skipped,
            "trace": " ｜ ".join(traces), "error": "；".join(errs)}


def fetch_now(source_id, trigger="manual"):
    """页面"立即抓取此源"：强制单源抓取（保留互斥），返回诊断（需求 D3）。"""
    srcs = {s["id"]: s for s in db.list_rss_sources(enabled_only=True)}
    src = srcs.get(int(source_id))
    if not src:
        return {"ok": False, "error": "来源不存在或未启用"}
    from .config import load
    import time as _t
    cfg = load()
    sid = src["id"]
    busy = db.get_kv(f"rss_busy_{sid}")
    if busy and str(busy).strip() and _t.time() - float(busy) < 600:
        return {"ok": False, "error": "抓取进行中（互斥保护），请稍后", "busy": True}
    db.set_kv(f"rss_busy_{sid}", str(_t.time()))
    try:
        rmax = int((cfg.get("rss") or {}).get("max_items") or 30)
        tmo = max(20, int((cfg.get("rss") or {}).get("timeout_seconds")
                          or (cfg.get("fetch") or {}).get("timeout_seconds") or 20))
        new, trace, err = fetch_source({**src, "_timeout": tmo, "_trigger": trigger}, cfg,
                                       limit=rmax)
    finally:
        db.set_kv(f"rss_busy_{sid}", "")
    if not err:
        db.set_kv(f"rss_ok_{sid}", str(_t.time()))
    diag = json.loads(db.get_kv(f"rss_diag_{sid}") or "{}")
    return {"ok": not err, "new": new, "trace": trace, "error": err, "diag": diag}


def source_diagnostics():
    """全部来源的诊断摘要（来源页展示：尝试/成功/原因/计数——需求 D3）。"""
    out = []
    import time as _t
    for s in db.list_rss_sources():
        sid = s["id"]
        diag = {}
        try:
            diag = json.loads(db.get_kv(f"rss_diag_{sid}") or "{}")
        except Exception:
            diag = {}
        ok_at = db.get_kv(f"rss_ok_{sid}")
        out.append({
            "id": sid, "name": s["name"], "url": s["url"], "enabled": s["enabled"],
            "fetch_minutes": s.get("fetch_minutes") or 0,
            "attempt_at": diag.get("attempt_at", ""),
            "ok_at": (diag.get("ok_at") or
                      (_t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(float(ok_at)))
                       if ok_at else "")),
            "http_status": diag.get("http_status", 0),
            "parsed": diag.get("parsed", 0), "scanned": diag.get("scanned", 0),
            "new": diag.get("new", 0), "dup": diag.get("dup", 0),
            "error": diag.get("error", ""), "error_class": diag.get("error_class", ""),
            "via_proxy": bool(diag.get("via_proxy")),
        })
    return out


def test_source(url):
    """连通性测试：抓一次返回条目数与前 3 条标题（不入库）。

    2026-11-09 H：补齐出站校验（历史缺陷——fetch_source 有 validate_url，
    来源测试却裸走 _http_get，可被当作 SSRF 探针）。"""
    from .config import load
    from .safefetch import validate_url, UnsafeUrlError, check_proxy_trust, fetch_limited
    from .twitter import _proxies
    cfg = load()
    xcfg = dict(cfg.get("x", {}))
    if not xcfg.get("proxy"):
        xcfg["proxy"] = (cfg.get("network") or {}).get("proxy") or ""
    try:
        validate_url(url, purpose="rss")
        check_proxy_trust("rss", xcfg.get("proxy") or "")
    except UnsafeUrlError as ue:
        return {"ok": False, "error": f"URL 拒绝（出站策略）：{str(ue)[:120]}"}
    try:
        status, content, _final = fetch_limited(url, timeout=20, purpose="rss",
                                                proxies=_proxies(xcfg.get("proxy") or ""))
        if status != 200:
            return {"ok": False, "error": f"HTTP {status}"}
        feed = feedparser.parse(content)
        if not feed.entries:
            return {"ok": False, "error": "解析成功但 0 条目（内容可能被 WAF 拦截）"}
        return {"ok": True, "title": feed.feed.get("title", ""),
                "entries": len(feed.entries),
                "sample": [(e.get("title") or "")[:60] for e in feed.entries[:3]]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}
