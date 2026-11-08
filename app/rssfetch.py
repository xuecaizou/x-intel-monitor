"""RSS 源监控：抓取通用 RSS/Atom 源（TheHackersNews 等），条目复用 tweets 表与 AI/推送管道。

与 X 账号共用 tweets 表：screen_name=RSS 源名，tweet_id=条目 guid/link 哈希（幂等去重），
media 提取 description 内首图。每源独立 fetch_minutes（0=跟随全局），置顶式增量：
记录上次首条 guid（kv rss_top_{id}），无变化整源短路。
"""
import hashlib
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
    """抓取单个 RSS 源，返回 (新条目数, 追踪明细, 错误)
    条目字段与 twitter._tweet 对齐，直接 insert_tweet 进管道"""
    name, url = src["name"], src["url"]
    xcfg = dict(cfg.get("x", {}))
    if not xcfg.get("proxy"):
        xcfg["proxy"] = (cfg.get("network") or {}).get("proxy") or ""
    try:
        # SSRF 校验（第四批）：仅 http/https；禁止回环/私网/链路本地（可用 fetch.allow_private_urls 显式放行）
        from .safefetch import validate_url, UnsafeUrlError, check_proxy_trust, fetch_limited
        from .twitter import _proxies
        try:
            validate_url(url, purpose="rss")   # RSS 永不允许私网（无白名单用途）
            check_proxy_trust("rss", xcfg.get("proxy") or "")
        except UnsafeUrlError as ue:
            return 0, "", f"URL 拒绝（出站策略）：{ue}"
        # 2026-11-09 H：从裸 _http_get（默认自动重定向+整包读取）迁移到受控抓取——
        # 重定向逐跳校验、≤8MB、显式超时在应用层实际生效
        status, content, _final = fetch_limited(
            url, timeout=max(20, int((src.get("_timeout") or 20))),
            purpose="rss", proxies=_proxies(xcfg.get("proxy") or ""))
        if status != 200:
            return 0, f"HTTP {status}", f"HTTP {status}"
        if not (content.lstrip()[:5].startswith(b"<?xml") or b"<feed" in content[:600]
                or b"<rss" in content[:600]):
            return 0, "响应非 RSS/Atom", "非 RSS 内容（可能被 WAF 拦截）"
        feed = feedparser.parse(content)
        if not feed.entries:
            return 0, "0 条目", "空 feed"
        # 增量短路：首条 guid 与上次一致 → 无更新
        top_id = _entry_id(feed.entries[0])
        if db.get_kv(f"rss_top_{src['id']}") == top_id:
            return 0, "无更新（首条 guid 未变）", ""
        new, trace_parts = 0, []
        media_fail = 0
        for e in feed.entries[:limit]:
            eid = _entry_id(e)
            link = e.get("link") or ""
            desc = e.get("description", "") or e.get("summary", "") or ""
            text = _clean(desc) or (e.get("title") or "").strip()
            if not text:
                continue
            t = {
                "screen_name": f"RSS:{name}",  # 加前缀防与同名 X 账号数据混淆
                "tweet_id": eid,
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
        db.set_kv(f"rss_top_{src['id']}", top_id)
        trace = f"✓{new}条/{len(feed.entries[:limit])}扫描"
        return new, trace, ""
    except Exception as e:
        return 0, "", f"{type(e).__name__}: {str(e)[:120]}"


def _global_minutes(cfg):
    """RSS"跟随全局"频率：统一读调度字段 fetch.interval_minutes（原读 x.interval_minutes，
    与调度器使用的 fetch.interval_minutes 不一致，导致全局改 5 分钟时 RSS 仍按 15 分钟）"""
    try:
        return int((cfg.get("fetch") or {}).get("interval_minutes", 15) or 15)
    except Exception:
        return 15


def run_rss_round(trigger="schedule"):
    """一轮 RSS 抓取：到期源全部抓取，返回汇总 dict（供 engine 并入 fetch_logs）"""
    from .config import load
    cfg = load()
    srcs = db.list_rss_sources(enabled_only=True)
    gmin = _global_minutes(cfg)
    due, ok, total_new, traces, errs = 0, 0, 0, [], []
    try:
        tmo = max(20, int((cfg.get("fetch") or {}).get("timeout_seconds") or 20))
    except Exception:
        tmo = 20
    for s in srcs:
        minutes = int(s.get("fetch_minutes") or 0) or gmin
        k = f"rss_last_{s['id']}"
        last = db.get_kv(k)
        import time as _t
        if last and _t.time() - float(last) < minutes * 60:
            continue  # 未到期
        db.set_kv(k, str(_t.time()))
        due += 1
        new, trace, err = fetch_source({**s, "_timeout": tmo}, cfg)
        total_new += new
        if err:
            errs.append(f"{s['name']}: {err}")
            traces.append(f"[{s['name']}] ✗ {err}")
        else:
            ok += 1
            traces.append(f"[{s['name']}] {trace}")
    return {"due": due, "ok": ok, "new": total_new,
            "trace": " ｜ ".join(traces), "error": "；".join(errs)}


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
