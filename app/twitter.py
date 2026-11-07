"""X/Twitter 抓取器（自 ThreatEye 剥离，三级降级策略已实测）。

可靠性实测（2026-10-25/26，本网络）：
- syndication / nitter 各实例域名直连均被 DNS 污染（解析到 Facebook IP 段）→ 必须走代理
- twiiit.com 为 Nitter 官方轮换器，经代理可返回完整 RSS，但落点不稳（部分实例被
  Cloudflare 盾或挂 Anubis 人机验证）→ 按「内容必须是 <?xml 开头的 RSS」验收，
  命中真实 RSS 时把最终实例写入 kv 缓存，下一轮直连绕过轮换不确定性
- auth_token + ct0（浏览器登录 x.com 后的 Cookie）直连官方 GraphQL 为最稳兜底（实验性）
"""
import datetime
import re
import time
from urllib.parse import urlparse, unquote

import feedparser

from .config import load
from . import db

X_ACC_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_TWEET_ID_RE = re.compile(r"/status(?:es)?/(\d+)")
_HTML_RE = re.compile(r"<[^>]+>")
_WEB_BEARER = "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
_GRAPHQL_FEATURES = ("{\"rweb_tipjar_consumption_enabled\":true,\"responsive_web_graphql_exclude_directive_enabled\":true,"
                     "\"verified_phone_label_enabled\":false,\"creator_subscriptions_tweet_preview_api_enabled\":true,"
                     "\"responsive_web_graphql_timeline_navigation_enabled\":true,\"responsive_web_graphql_skip_user_profile_image_extensions_enabled\":false,"
                     "\"communities_web_enable_tweet_community_results_fetch\":true,\"c9s_tweet_anatomy_moderator_badge_enabled\":true,"
                     "\"articles_preview_enabled\":true,\"responsive_web_edit_tweet_api_enabled\":true,"
                     "\"tweetypie_unmention_optimization_enabled\":true,\"responsive_web_text_conversations_enabled\":false,"
                     "\"freedom_of_speech_not_reach_fetch_enabled\":true,\"standardized_nudges_misinfo\":true,"
                     "\"tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled\":true,\"longform_notetweets_consumption_enabled\":true}")
UA_BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"

import requests


def _proxies(proxy_str):
    if not proxy_str:
        return None
    if proxy_str.startswith("socks5://"):
        proxy_str = "socks5h://" + proxy_str[len("socks5://"):]
    return {"http": proxy_str, "https": proxy_str}


def _http_get(url, cfg, headers=None, timeout=None):
    p = _proxies(cfg.get("proxy"))
    kw = {"headers": {"User-Agent": UA_BROWSER, **(headers or {})}, "timeout": timeout or 25}
    if p:
        kw["proxies"] = p
    r = requests.get(url, **kw)
    r.raise_for_status()
    return r


def _clean(s, n=400):
    return _HTML_RE.sub("", s or "").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()[:n]


def _fmt_time(s):
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime((s or "")[:29].strip(), fmt).strftime("%Y-%m-%d %H:%M")
        except Exception:
            continue
    return (s or "")[:16]


def _tweet(name, tid, text, published, is_rt, is_reply, media_url=""):
    return dict(screen_name=name, tweet_id=tid, text=_clean(text),
                url=f"https://x.com/{name}/status/{tid}",
                published_at=_fmt_time(published), is_retweet=is_rt, is_reply=is_reply,
                media_url=media_url)


def _extract_media(desc):
    """从 Nitter RSS 描述里取第一张媒体原图 URL（/pic/ 代理路径解码回 pbs.twimg.com 原图）"""
    m = re.search(r'<img[^>]+src="([^"]+)"', desc or "")
    if not m:
        return ""
    src = m.group(1)
    if "/pic/" in src:
        raw = unquote(src.split("/pic/", 1)[1])
        if raw.startswith("media/"):
            return "https://pbs.twimg.com/" + raw
        if raw.startswith("http"):
            return raw
        return ""
    if "pbs.twimg.com" in src:
        return src
    return ""


def _syndication_media(obj):
    """syndication/GraphQL JSON 里的媒体原图"""
    for key in ("extended_entities", "entities"):
        media = ((obj.get(key) or {}).get("media")) or []
        if media:
            return media[0].get("media_url_https") or media[0].get("media_url") or ""
    return ""


# ---- 层 A：syndication ----

def _fetch_syndication(name, cfg):
    url = (f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{name}?showReplies=true")
    r = _http_get(url, cfg, headers={"Referer": "https://twitter.com/"}, timeout=15)
    if "json" not in (r.headers.get("content-type") or ""):
        raise RuntimeError("syndication 返回非 JSON（可能被限流）")
    out = []

    def walk(node):
        if isinstance(node, dict):
            text = node.get("full_text") or node.get("text")
            tid = node.get("id_str") or node.get("id")
            if text and tid and node.get("created_at"):
                out.append((str(tid), text, node))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(r.json())
    items = []
    for tid, text, obj in out[: int(cfg.get("max_items", 20))]:
        items.append(_tweet(name, tid, text, obj.get("created_at", ""),
                            "retweeted_status" in obj or text.startswith("RT @"),
                            bool(obj.get("in_reply_to_status_id_str")),
                            _syndication_media(obj)))
    return items


# ---- 层 B：Nitter RSS（实例缓存 + twiiit 轮换） ----

def _fetch_nitter(name, cfg, kv_get, kv_set):
    cached = kv_get("x_nitter_instance")
    urls = []
    if cached:
        urls.append(f"https://{cached}/{name}/rss")
    urls += [f"https://{h}/{name}/rss" for h in cfg.get("nitter_instances") or []]
    urls += [f"https://twiiit.com/{name}/rss"] * 3
    urls = list(dict.fromkeys(urls))  # 去重（缓存实例可能与配置实例相同）
    last_err = "无可用实例"
    for url in urls:
        try:
            r = _http_get(url, cfg, timeout=15)
            host = url.split("/")[2]
            if r.status_code == 200 and r.content.lstrip().startswith(b"<?xml"):
                feed = feedparser.parse(r.content)
                if feed.entries:
                    final_host = urlparse(r.url).netloc or host
                    if final_host != "twiiit.com":
                        kv_set("x_nitter_instance", final_host)
                    items = []
                    for e in feed.entries[: int(cfg.get("max_items", 20))]:
                        m = _TWEET_ID_RE.search(e.get("link", ""))
                        if not m:
                            continue
                        title = (e.get("title") or "").strip()
                        desc = e.get("description", "") or ""
                        items.append(_tweet(name, m.group(1), _clean(desc) or title,
                                            e.get("published", ""),
                                            title.startswith("RT by "),
                                            "replying-to" in desc.lower(),
                                            _extract_media(desc)))
                    if items:
                        return items
                    last_err = f"{host} RSS 无可识别推文条目"
                else:
                    last_err = f"{host} RSS 0 条"
            else:
                last_err = f"{host} HTTP {r.status_code}（盾/挑战页）"
        except Exception as e:
            last_err = f"{url.split('/')[2]}: {type(e).__name__}"
        time.sleep(1.2)
    raise RuntimeError(f"Nitter 层失败（{last_err}）")


# ---- 层 C：Cookie 直连 GraphQL（实验性） ----

def _fetch_cookie(name, cfg, kv_get, kv_set):
    token, ct0 = cfg.get("auth_token") or "", cfg.get("ct0") or ""
    if not (token and ct0):
        raise RuntimeError("未配置 X 登录 Cookie（auth_token / ct0）")
    headers = {"authorization": _WEB_BEARER, "cookie": f"auth_token={token}; ct0={ct0}",
               "x-csrf-token": ct0, "Referer": "https://x.com/home"}

    def gql(qid, opname, variables):
        from urllib.parse import quote
        url = (f"https://x.com/i/api/graphql/{qid}/{opname}"
               f"?variables={quote(variables)}&features={quote(_GRAPHQL_FEATURES)}")
        return _http_get(url, {**cfg, "proxy": cfg.get("proxy")}, headers=headers, timeout=20).json()

    qid = kv_get("x_gql_queryid")
    uid = kv_get(f"x_uid_{name.lower()}")
    if not qid:
        html = _http_get("https://x.com/home", cfg, headers=headers, timeout=20).text
        srcs = re.findall(r'src="(https://abs\.twimg\.com/responsive-web/client-web/main\.[^"]+\.js)"', html)
        if not srcs:
            raise RuntimeError("未能定位 x.com 主 bundle")
        bundle = _http_get(srcs[0], cfg, headers=headers, timeout=30).text
        m = re.search(r'queryId:"([0-9a-f]{20,})",operationName:"UserTweets"', bundle)
        if not m:
            raise RuntimeError("bundle 中未找到 UserTweets queryId")
        qid = m.group(1)
        kv_set("x_gql_queryid", qid)
    if not uid:
        data = gql(qid, "UserByScreenName", f'{{"screen_name":"{name}"}}')
        uid = ((data.get("data") or {}).get("user") or {}).get("rest_id")
        if not uid:
            raise RuntimeError("未能解析 userId（Cookie 可能已失效）")
        kv_set(f"x_uid_{name.lower()}", uid)
    data = gql(qid, "UserTweets", f'{{"userId":"{uid}","count":20,"includePromotedContent":false}}')
    out = []

    def walk(node):
        if isinstance(node, dict):
            text = node.get("full_text") or node.get("text")
            tid = node.get("id_str") or node.get("id")
            if text and tid and node.get("created_at"):
                out.append((str(tid), text, node))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(data)
    items = [_tweet(name, tid, text, obj.get("created_at", ""),
                    "retweeted_status" in obj or text.startswith("RT @"),
                    bool(obj.get("in_reply_to_status_id_str")),
                    _syndication_media(obj))
             for tid, text, obj in out[: int(cfg.get("max_items", 20))]]
    if not items:
        raise RuntimeError("GraphQL 返回 0 条（Cookie/queryId 可能已失效）")
    return items


def _due_accounts(accounts, kv_get):
    """每账号频率调度：fetch_minutes=0 跟随全局节奏；>0 时按账号自身的到期时间抓取"""
    now = time.time()
    due = []
    for a in accounts:
        fm = int(a.get("fetch_minutes") or 0)
        if fm <= 0:
            due.append(a)
            continue
        last = kv_get(f"acc_last_{a['screen_name'].lower()}")
        try:
            last = float(last) if last else 0
        except Exception:
            last = 0
        if now - last >= fm * 60:
            due.append(a)
    return due


def _truncate_incremental(items, known_top):
    """增量短路：推文按新到旧排列，截断到上次已入库的置顶 id 为止（未更新的不处理）"""
    if not known_top:
        return items
    for i, t in enumerate(items):
        if t["tweet_id"] == known_top:
            return items[:i]
    return items


def fetch_all_accounts(cfg=None):
    """遍历**到期**的账号抓取推文，入库去重。

    返回 (新推文列表, 错误摘要, 分层追踪明细, 成功账号数, 到期账号数)。
    分层追踪：每账号记录各层尝试结果与耗时（如 "syndication✗429 → nitter✓20条/2.4s"），
    供 fetch_logs 持久化。仅当所有到期账号全部失败时才抛异常。
    """
    cfg = cfg or load()
    xcfg = dict(cfg.get("x", {}))
    # 代理回退：x.proxy 未配置时回退 network.proxy（保证 X 采集永远走代理）
    if not xcfg.get("proxy"):
        xcfg["proxy"] = (cfg.get("network") or {}).get("proxy") or ""
    kv_get, kv_set = db.get_kv, db.set_kv
    accounts = [a for a in db.list_x_accounts(enabled_only=True) if X_ACC_RE.match(a["screen_name"])]
    if not accounts:
        raise RuntimeError("监控账号列表为空，请先在页面添加")
    due = _due_accounts(accounts, kv_get)
    if not due:
        return [], [], ["无到期账号（本轮快速跳过）"], 0, 0
    strategy = (xcfg.get("strategy") or "auto").lower()
    first_limit = max(1, int(xcfg.get("first_fetch_limit") or 10))
    layers = ("syndication", "nitter", "cookie") if strategy == "auto" else (strategy,)
    # 熔断：syndication 连续失败 ≥3 轮后，30 分钟内跳过该层（省掉每轮 15s+ 超时）
    if "syndication" in layers:
        try:
            if time.time() < float(kv_get("syn_skip_until") or 0):
                layers = tuple(l for l in layers if l != "syndication")
        except Exception:
            pass
    new_tweets, errors, ok_accounts, trace = [], [], 0, []
    for acc in due:
        name = acc["screen_name"]
        items = None
        attempts = []
        # 整体重试两次：代理抖动常在秒级恢复（实测 0.5s 正常→下一秒超时），隔 4 秒再试
        for overall in range(3):
            for layer in layers:
                t0 = time.time()
                try:
                    if layer == "syndication":
                        items = _fetch_syndication(name, xcfg)
                    elif layer == "nitter":
                        items = _fetch_nitter(name, xcfg, kv_get, kv_set)
                    elif layer == "cookie":
                        items = _fetch_cookie(name, xcfg, kv_get, kv_set)
                    attempts.append(f"{layer}✓{len(items or [])}条/{time.time() - t0:.1f}s")
                    if layer == "syndication":
                        kv_set("syn_fail_streak", "0")
                    break
                except Exception as e:
                    tag = "" if overall == 0 else "（重试）"
                    attempts.append(f"{layer}✗{str(e)[:60]}({time.time() - t0:.1f}s){tag}")
                    if layer == "syndication":
                        try:
                            streak = int(kv_get("syn_fail_streak") or 0) + 1
                        except Exception:
                            streak = 1
                        kv_set("syn_fail_streak", str(streak))
                        if streak >= 3:
                            kv_set("syn_skip_until", str(time.time() + 1800))
            if items is not None:
                break
            if overall < 2:
                time.sleep(4)
        trace.append(f"@{name}: " + " → ".join(attempts))
        if items is None:
            errors.append(f"@{name} 全层失败（{' → '.join(attempts)}）")
            continue
        ok_accounts += 1
        # 增量短路：只处理上次置顶 id 之后的新推文
        known_top = kv_get(f"acc_top_{name.lower()}")
        items = _truncate_incremental(items, known_top)
        # 首次抓取（无位点）：按「初次抓取条数」设置回溯，避免新账号一次性灌入过多
        if not known_top and len(items) > first_limit:
            items = items[:first_limit]
        if items:
            kv_set(f"acc_top_{name.lower()}", items[0]["tweet_id"])
        if not xcfg.get("show_replies", True):
            items = [t for t in items if not t["is_reply"]]
        for t in items:
            row_id = db.insert_tweet(t)
            if row_id:
                t["id"] = row_id
                new_tweets.append(t)
        kv_set(f"acc_last_{name.lower()}", str(time.time()))
    for e in errors:
        print("[x-intel]", e)
    if ok_accounts == 0:
        err = RuntimeError("；".join(errors[-3:]) or "所有账号均未取到数据")
        err.trace = "｜".join(trace[-2:])  # 全层失败时也把分层明细带给 fetch_logs
        raise err
    return new_tweets, errors, trace, ok_accounts, len(due)
