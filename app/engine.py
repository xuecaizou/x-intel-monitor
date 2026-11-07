"""主管道：抓取 → 去重入库 → AI 研判+翻译 → 过滤 → 摘要/即时推送"""
import json
import re
import threading
import time

from . import db, twitter, ai as aimod
from . import rssfetch
from .config import load
from .notify import CHANNELS, build_digest, build_single, build_test
from .notify.base import prepare_image

_lock = threading.Lock()
_running = False

HIGH_KEYWORDS = ["ransomware", "勒索", "breach", "泄露", "0day", "zero-day", "exploit",
                 "data leak", "backdoor", "apt ", "stealer", "threat actor"]

SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def is_running():
    return _running


def _base_severity(t):
    """无 AI 时的兜底分级：关键词命中 high，其余 medium"""
    text = (t.get("text", "") or "").lower()
    return "high" if any(k in text for k in HIGH_KEYWORDS) else "medium"


def _effective_severity(t):
    return t.get("ai_severity") or _base_severity(t)


def _push_filter(cfg):
    """推送过滤配置：{min_severity, countries, industries, keywords}（空=不过滤）"""
    f = (cfg.get("push") or {}).get("filters") or {}

    def _split(s):
        return [x.strip() for x in re.split(r"[、,，/|]", str(s or "")) if x.strip()]
    return {"min_severity": str(f.get("min_severity") or ""),
            "mode": str(f.get("mode") or "and"),
            "categories": _split(f.get("categories")),
            "countries": _split(f.get("countries")),
            "industries": _split(f.get("industries")),
            "tags": _split(f.get("tags")),
            "keywords": _split(f.get("keywords"))}


def _named_filter(cfg, rule_id):
    """按 id 取命名规则（push.rules[]），结构化为 pass_filter 可用的过滤字典；
    未找到返回 None（调用方回退默认规则）"""
    for r in (cfg.get("push") or {}).get("rules") or []:
        if r.get("id") == rule_id:
            f = r
            break
    else:
        return None

    def _split(s):
        return [x.strip() for x in re.split(r"[、,，/|]", str(s or "")) if x.strip()]
    return {"min_severity": str(f.get("min_severity") or ""),
            "mode": str(f.get("mode") or "and"),
            "categories": _split(f.get("categories")),
            "countries": _split(f.get("countries")),
            "industries": _split(f.get("industries")),
            "tags": _split(f.get("tags")),
            "keywords": _split(f.get("keywords"))}


def pass_filter(t, flt):
    """推文是否通过推送过滤（过滤条件全空=通过）。
    六维白名单 + 组合模式：
    - mode='and'（默认）：各维度全部满足才推送；维度内多值为或（命中任一即算该维通过）
    - mode='or'：任一非空维度满足即推送（空维度不参与；全空=放行）"""
    def _sev():
        if not flt["min_severity"]:
            return None
        return SEV_RANK.get(_effective_severity(t), 9) <= SEV_RANK.get(flt["min_severity"], 9)

    def _cat():
        if not flt["categories"]:
            return None
        return (t.get("category") or "") in flt["categories"]

    def _ctry():
        if not flt["countries"]:
            return None
        c = t.get("geo_country") or ""
        return any(k in c for k in flt["countries"])

    def _ind():
        if not flt["industries"]:
            return None
        ind = t.get("industry") or ""
        return any(k in ind for k in flt["industries"])

    def _tag():
        if not flt["tags"]:
            return None
        tt = t.get("tags") or []
        if isinstance(tt, str):
            try:
                tt = json.loads(tt)
            except Exception:
                tt = []
        return any(k in tt for k in flt["tags"])

    def _kw():
        if not flt["keywords"]:
            return None
        hay = f"{t.get('text','')} {t.get('text_zh','')} {t.get('ai_type','')}".lower()
        return any(k.lower() in hay for k in flt["keywords"])

    results = [r for r in (_sev(), _cat(), _ctry(), _ind(), _tag(), _kw()) if r is not None]
    if not results:
        return True
    if (flt.get("mode") or "and").lower() == "or":
        return any(results)
    return all(results)


def _push_sort_key(t):
    return SEV_RANK.get(_effective_severity(t), 9)


def run_ai_stage(new_tweets, cfg):
    """AI 阶段：本轮新推文**全部**研判+翻译（推送门控依赖它们完成）；
    历史遗留未研判的按 auto_limit 限额补处理。失败推文 ai_retries+1 供下轮重试"""
    acfg = dict(cfg.get("ai", {}))
    if not (acfg.get("enabled") and acfg.get("auto_analyze") and acfg.get("api_key")):
        return 0
    done = 0
    def _run(t):
        nonlocal done
        try:
            aimod.process_tweet(t, acfg)
            done += 1
            time.sleep(0.5)
        except Exception as e:
            db.update_ai(t["id"], {"ai_status": "error",
                                   "ai_error": f"{type(e).__name__}: {str(e)[:180]}",
                                   "ai_analyzed_at": db.now()})
    for t in new_tweets:  # 新推文不限量——推送门控依赖它们完成研判
        _run(t)
    for t in db.tweets_pending_ai(int(acfg.get("auto_limit", 10))):  # 历史遗留限额补
        _run(t)
    return done


def _ai_realtime(cfg):
    """AI 实时模式判定：启用 + 自动分析 + 有 Key（三者齐备才要求推送内容必经研判）"""
    ai = cfg.get("ai", {})
    return bool(ai.get("enabled") and ai.get("auto_analyze") and ai.get("api_key"))


def _pushable_by_ai(t, ai_realtime):
    """AI 实时开启时，仅已研判成功（或重试 2 轮仍失败、附错误说明）的推文可推送"""
    if not ai_realtime:
        return True
    st = t.get("ai_status")
    if st == "ok":
        return True
    if st == "error" and int(t.get("ai_retries") or 0) >= 2:
        return True
    return False


def _enabled_channels(cfg):
    """返回 [(ch, cc, sim)]：遍历每类型通道的 instances 列表（多实例），
    单实例旧结构（直接放凭据在 channels.{key} 下）兼容读取。
    cc 含 instance_name / filters（实例级四维推送筛选）。"""
    out = []
    for ch in CHANNELS:
        base = dict(cfg.get("channels", {}).get(ch.key, {}))
        instances = base.get("instances")
        if not isinstance(instances, list) or not instances:
            # 旧单实例：把 base 本身视作唯一实例
            if base.get("enabled"):
                cc = {k: v for k, v in base.items() if k not in ("instances",)}
                cc.setdefault("instance_name", ch.display)
                out.append((ch, cc, not ch.configured(cc)))
            continue
        for i, inst in enumerate(instances):
            if not inst.get("enabled"):
                continue
            cc = {k: v for k, v in base.items() if k not in ("instances", "enabled", "instance_name", "filters")}
            cc.update({k: v for k, v in inst.items() if k not in ("filters",)})
            cc["instance_name"] = inst.get("name") or f"{ch.display}{i + 1}"
            cc["filters"] = inst.get("filters") or {}
            out.append((ch, cc, not ch.configured(cc)))
    return out


def dispatch(new_tweets, cfg):
    """摘要推送（≤N 条）+ 严重推文即时单推；全局过滤 + 每实例级四维筛选；凭据留空走模拟模式"""
    if not new_tweets:
        return 0
    flt = _push_filter(cfg)
    order_all = sorted((t for t in new_tweets if pass_filter(t, flt)), key=_push_sort_key)
    if not order_all:
        db.add_push_log("推送过滤", f"本轮 {len(new_tweets)} 条全部被过滤", "成功",
                        f"过滤条件：{flt}")
        return 0
    max_items = int(cfg.get("push", {}).get("digest_max_items", 8))
    pushed_ids = []
    sent = 0
    for ch, cc, sim in _enabled_channels(cfg):
        # 实例过滤 = 绑定的命名规则（filter_rule）；未绑定 → 全局默认规则
        rule_id = str(cc.get("filter_rule") or "")
        rule = _named_filter(cfg, rule_id) if rule_id else None
        if rule_id and rule is None:
            db.add_push_log(ch.display, f"实例 {cc.get('instance_name')}：绑定规则 {rule_id} 不存在，回退默认过滤",
                            "成功", "")
        inst_flt = rule if rule is not None else flt
        rule_name = ""
        if rule is not None:
            for r in (cfg.get("push") or {}).get("rules") or []:
                if r.get("id") == rule_id:
                    rule_name = r.get("name") or rule_id
                    break
        order = sorted((t for t in order_all if pass_filter(t, inst_flt)), key=_push_sort_key)
        if not order:
            db.add_push_log(ch.display, f"实例 {cc.get('instance_name')}：本轮条目全部被过滤",
                            "成功", f"规则：{rule_name or '默认'} / {inst_flt}")
            continue
        disp = f"{ch.display}·{cc.get('instance_name')}"
        batch = order[:max_items]
        try:
            if sim:
                ok, detail = True, "模拟模式（未配置凭据）"
            else:
                ok, detail = ch.send(cc, "X-Intel 推文摘要", build_digest(batch))
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"[:200]
        status = "模拟成功" if (sim and ok) else ("成功" if ok else "失败")
        db.add_push_log(disp, f"摘要 {len(batch)} 条", status, detail,
                        content=build_digest(batch))
        sent += 1 if ok else 0
        for t in order:
            if _effective_severity(t) not in ("critical", "high"):
                continue
            try:
                if sim:
                    ok, detail = True, "模拟模式（未配置凭据）"
                else:
                    image = None
                    if t.get("media_url"):  # 企微机器人 markdown 不渲染图片 → 截图以 image 消息直传
                        image = prepare_image(t)
                    ok, detail = ch.send(cc, f"X-Intel · @{t['screen_name']}",
                                         build_single(t), image=image)
            except Exception as e:
                ok, detail = False, f"{type(e).__name__}: {e}"[:200]
            status = "模拟成功" if (sim and ok) else ("成功" if ok else "失败")
            db.add_push_log(disp, f"即时：{(t.get('text', '') or '')[:24]}", status, detail,
                            content=build_single(t))
            sent += 1 if ok else 0
            time.sleep(4.5)  # 企微机器人限频约 20 条/分钟，即时单推每条含图≈2 消息
        pushed_ids.extend(t["id"] for t in order)
    db.mark_pushed(list(set(pushed_ids)))  # 已进入推送流程的推文标记，防重复推送
    return sent


def run_fetch(manual=False, cfg=None):
    global _running
    with _lock:
        if _running:
            return {"ok": False, "msg": "已有抓取任务在运行"}
        _running = True
    trigger = "manual" if manual else "schedule"
    try:
        cfg = cfg or load()
        new_tweets, errors, trace, ok_acc, due_acc = twitter.fetch_all_accounts(cfg)
        before_ids = {t["id"] for t in new_tweets}
        # RSS 源（监控源 02）：独立到期调度，条目入库复用 tweets 管道
        rss_round = rssfetch.run_rss_round(trigger)
        if rss_round["due"]:
            trace = trace + [f"RSS:{rss_round['trace']}"]
            if rss_round["error"]:
                errors.append(rss_round["error"])
            if rss_round["new"]:
                names = ["RSS:" + s["name"] for s in db.list_rss_sources(enabled_only=True)]
                fresh = [dict(r) for r in db.get_conn().execute(
                    "SELECT * FROM tweets WHERE screen_name IN "
                    f"({','.join('?' for _ in names)}) "
                    "ORDER BY id DESC LIMIT ?", names + [rss_round["new"]]).fetchall()]
                new_tweets = new_tweets + [t for t in fresh if t["id"] not in before_ids]
        ai_done = run_ai_stage(new_tweets, cfg)
        ai_realtime = _ai_realtime(cfg)
        # 待推送：未推送且未忽略；AI 实时开启时仅放行已研判（或重试 2 轮失败附说明）的推文
        pending = [t for t in db.tweets_pending_push(200)
                   if pass_filter(t, _push_filter(cfg)) and _pushable_by_ai(t, ai_realtime)]
        pushed = dispatch(pending, cfg)
        db.add_fetch_log(trigger, due_acc, ok_acc, len(new_tweets), ai_done,
                         "；".join(errors[-3:]), "｜".join(trace))
        # 新推文分类兜底：AI 研判失败导致无分类的，用脚本规则补（零成本）
        for t in new_tweets:
            row = db.get_tweet(t["id"])
            if row and not row.get("category"):
                cat = aimod.rule_classify(row.get("text"))
                if cat:
                    db.update_ai(t["id"], {"category": cat})
        db.set_kv("last_fetch", json.dumps(
            {"at": db.now(), "new": len(new_tweets), "ai": ai_done,
             "pushed": pushed, "manual": bool(manual)}, ensure_ascii=False))
        return {"ok": True, "new": len(new_tweets), "ai": ai_done, "pushed": pushed}
    except Exception as e:
        err = str(e)[:280]
        try:
            db.add_fetch_log(trigger, len(db.list_x_accounts(enabled_only=True)), 0, 0, 0,
                             err, getattr(e, "trace", ""))
        except Exception:
            pass
        db.set_kv("last_fetch", json.dumps({"at": db.now(), "error": err}, ensure_ascii=False))
        return {"ok": False, "error": err}
    finally:
        _running = False


def run_single_ai(tweet_id, action):
    """手动单条 AI：action = analyze | translate"""
    t = db.get_tweet(tweet_id)
    if not t:
        return None
    cfg = load().get("ai", {})
    if action == "translate":
        fields = aimod.translate_tweet(t, cfg)
        db.update_ai(tweet_id, fields)
        return {"ok": True, "fields": fields}
    fields = aimod.analyze_tweet(t, cfg)
    db.update_ai(tweet_id, fields)
    return {"ok": True, "fields": fields}


def backfill_tags(limit_n=10, cfg=None):
    """为已研判但缺地区标签的旧推文补跑 AI 打标（不重做翻译）"""
    rows = db.tweets_missing_geo(limit_n)
    acfg = cfg or load().get("ai", {})
    done, errors = 0, 0
    for t in rows:
        try:
            fields = aimod.analyze_tweet(t, acfg)
            # 确实无地理信息的推文标「未知」，避免被反复重试浪费额度
            if not fields.get("geo_country"):
                fields["geo_country"] = "未知"
            db.update_ai(t["id"], fields)
            done += 1
        except Exception as e:
            db.update_ai(t["id"], {"ai_error": f"backfill {type(e).__name__}: {str(e)[:150]}"})
            errors += 1
        time.sleep(1)
    return {"done": done, "errors": errors, "remaining_hint": len(rows)}


def test_channel(key, cfg):
    for ch in CHANNELS:
        if ch.key != key:
            continue
        cc = dict(cfg.get("channels", {}).get(key, {}))
        sim = not ch.configured(cc)
        try:
            if sim:
                ok, detail = True, "模拟模式（未配置凭据）"
            else:
                ok, detail = ch.send(cc, "X-Intel 测试消息", build_test())
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"[:200]
        status = "模拟成功" if (sim and ok) else ("成功" if ok else "失败")
        db.add_push_log(ch.display, "测试消息", status, detail)
        return {"ok": ok, "simulated": sim, "status": status, "detail": detail}
    return None


def classify_recent(limit=200, cfg=None):
    """对近期未分类的推文做情报分类：脚本规则优先，AI 批量兜底（可后台执行）"""
    cfg = cfg or load()
    acfg = dict(cfg.get("ai", {}))
    pending = db.tweets_missing_category(limit)
    if not pending:
        return {"rule": 0, "ai": 0, "fail": 0, "total": 0}
    rule_done = 0
    ai_todo = []
    for t in pending:
        cat = aimod.rule_classify(t.get("text"))
        if cat:
            db.update_ai(t["id"], {"category": cat})
            rule_done += 1
        else:
            ai_todo.append(t)
    ai_done, fail = 0, 0
    if ai_todo:
        try:
            r = aimod.classify_batch(ai_todo, acfg)
            ai_done, fail = r["done"], r["fail"]
        except Exception as e:
            for t in ai_todo:
                db.update_ai(t["id"], {"ai_error": f"classify {type(e).__name__}: {str(e)[:150]}"})
            fail = len(ai_todo)
    return {"rule": rule_done, "ai": ai_done, "fail": fail, "total": rule_done + ai_done}