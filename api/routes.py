"""X-Intel REST API 路由（127.0.0.1:8083）"""
import json
import os
import re
import time

from fastapi.responses import JSONResponse
from fastapi import APIRouter, BackgroundTasks, HTTPException, Response, Request, Depends
from fastapi.responses import FileResponse
from fastapi.params import Query
from pydantic import BaseModel

from app import db, engine, ai as aimod, twitter, reports, rssfetch, auth, eventexport
from app.config import load, save_runtime
from app.notify import CHANNELS
from app.scheduler import instance as scheduler_instance

router = APIRouter()


def _operator(request: Request) -> str:
    """操作者标识（审计用）：优先会话用户名，其次 Token 名称"""
    try:
        p = auth._extract_principal(request) or {}
        return str(p.get("username") or p.get("name") or "unknown")[:60]
    except Exception:
        return "unknown"

X_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
CHANNEL_SECRET_FIELDS = {
    "wecom": ["webhook"],
    "dingtalk": ["webhook", "secret"],
    "pushplus": ["token"],
    "wxpusher": ["app_token", "uid"],
}
AI_SECRET_FIELDS = ["api_key"]


class ToggleBody(BaseModel):
    enabled: bool = True


class XAccountBody(BaseModel):
    screen_name: str = ""
    display: str = ""
    enabled: bool = True
    fetch_minutes: int | None = None


class ValuesBody(BaseModel):
    values: dict = {}


class AiActionBody(BaseModel):
    action: str = "analyze"


class AiTextBody(BaseModel):
    text: str
    screen_name: str = "external"


def _mask(value):
    v = str(value or "")
    if not v:
        return ""
    m = re.match(r"^(https?://[^/]+)/", v)
    if m:
        return m.group(1) + "/****"  # 掩码统一含 ****，PUT 端过滤 **** 即天然防回写
    return "****" + v[-4:] if len(v) > 4 else "****"


# ---------- 推文流 ----------

@router.get("/api/tweets", dependencies=[Depends(auth.require("read"))])
def api_tweets(screen_name: str = "all", severity: str = "all", q: str = "",
               country: str = "all", industry: str = "all", category: str = "all",
               status: str = "all", tag: str = "",
               limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    total, items = db.list_tweets(screen_name, severity, q.strip(), country, industry,
                                  category, status, tag=tag.strip(), limit=limit, offset=offset)
    return {"total": total, "items": items}


@router.get("/api/facets", dependencies=[Depends(auth.require("read"))])
def api_facets(severity: str = "all", screen_name: str = "all", q: str = "",
               country: str = "all", industry: str = "all", category: str = "all",
               status: str = "all", tag: str = ""):
    """各维度计数随筛选上下文联动：每个维度统计时排除自身条件，其余条件全部生效。
    下拉里的数字 = 在当前其它筛选下选它能得到的真实条数。"""
    return db.facets(sev=severity, acc=screen_name, q=q.strip(), country=country,
                     industry=industry, category=category, status=status, tag=tag.strip())


@router.get("/api/categories", dependencies=[Depends(auth.require("read"))])
def api_categories():
    """情报分类去重计数"""
    return {"items": db.category_facets()}


class FlagBody(BaseModel):
    read: bool | None = None
    ignored: bool | None = None
    starred: bool | None = None


class BatchBody(BaseModel):
    ids: list[int] = []
    op: str = "read"


class AiBatchBody(BaseModel):
    ids: list[int] = []
    action: str = "translate"


class CleanupBody(BaseModel):
    keep_days: int = 7


@router.post("/api/tweets/{tid}/flag", dependencies=[Depends(auth.require("write"))])
def api_tweet_flag(tid: int, body: FlagBody):
    if not db.set_tweet_flags(tid, read=body.read, ignored=body.ignored, starred=body.starred):
        raise HTTPException(400, "无变更字段")
    return {"ok": True}


@router.post("/api/tweets/batch", dependencies=[Depends(auth.require("write"))])
def api_tweets_batch(body: BatchBody):
    if body.op == "ai_analyze":
        r = aimod.batch_process(body.ids, "analyze")
        return {"ok": True, **r}
    if body.op == "ai_translate":
        r = aimod.batch_process(body.ids, "translate")
        return {"ok": True, **r}
    n = db.batch_tweet_op(body.ids, body.op)
    return {"ok": True, "op": body.op, "affected": n}


@router.post("/api/tweets/cleanup", dependencies=[Depends(auth.require("write"))])
def api_tweets_cleanup(body: CleanupBody):
    if body.keep_days not in (1, 2, 3, 7, 30):
        raise HTTPException(400, "keep_days 仅支持 1/2/3/7/30")
    n = db.cleanup_old(body.keep_days)
    return {"ok": True, "deleted": n, "keep_days": body.keep_days}


MEDIA_HOSTS = {"pbs.twimg.com", "ton.twimg.com"}


@router.get("/api/media", dependencies=[Depends(auth.require("read"))])
def api_media(url: str):
    """推文截图代理：经抓取代理拉取 pbs.twimg.com 原图（解决客户端网络无法直连的问题）"""
    from urllib.parse import urlparse as _up
    host = (_up(url).netloc or "").lower()
    if host not in MEDIA_HOSTS:
        raise HTTPException(400, "仅允许代理 pbs.twimg.com / ton.twimg.com 图片")
    try:
        from app.safefetch import validate_url
        validate_url(url, purpose="media")   # 协议/解析/IP 校验（域名已白名单，双重防护）
    except Exception as e:
        raise HTTPException(400, f"URL 未通过校验：{str(e)[:80]}")
    try:
        from app.twitter import _http_get
        cfg = load().get("x", {})
        r = _http_get(url, cfg, timeout=20)
        ct = r.headers.get("content-type", "image/jpeg")
        if not ct.startswith("image/"):
            raise HTTPException(502, "上游返回非图片内容")
        from fastapi.responses import Response
        return Response(content=r.content, media_type=ct,
                        headers={"Cache-Control": "public, max-age=86400"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"图片拉取失败：{type(e).__name__}: {str(e)[:120]}")


@router.post("/api/tweets/{tid}/ai", dependencies=[Depends(auth.require("write"))])
def api_tweet_ai(tid: int, body: AiActionBody):
    if body.action not in ("analyze", "translate"):
        raise HTTPException(400, "action 只支持 analyze / translate")
    try:
        r = engine.run_single_ai(tid, body.action)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:180]}"}
    if r is None:
        raise HTTPException(404, "推文不存在")
    return r


# ---------- 统计 / 抓取 ----------

def _sources_health(accounts):
    """监控源健康（逐源）：名称 / 近 24h 入库条数 / 最后抓取时间 / 最近一轮成败"""
    import time as _t

    def fmt_ts(v):
        try:
            return _t.strftime("%H:%M", _t.localtime(float(v)))
        except Exception:
            return ""

    def cnt24(name):
        return db.get_conn().execute(
            "SELECT COUNT(*) AS n FROM tweets WHERE screen_name=?"
            " AND created_at>=datetime('now','-24 hours','localtime')",
            (name,)).fetchone()["n"]

    # 最近几轮 trace 解析每源最新成败（新轮优先，X 段 @name、RSS 段 [name]）
    x_status, rss_status = {}, {}
    rows = db.get_conn().execute(
        "SELECT trace FROM fetch_logs ORDER BY id DESC LIMIT 8").fetchall()
    for r in rows:
        trace = dict(r).get("trace") or ""
        for seg in trace.split("｜"):
            seg = seg.strip()
            if seg.startswith("@"):
                nm = seg[1:].split(":")[0].strip()
                if nm and nm not in x_status:
                    x_status[nm] = "✓" in seg  # 分层降级任一层成功即健康
            elif seg.startswith("RSS:"):
                m2 = re.search(r"\[([^\]]+)\] ([^｜]*)", seg)
                if m2 and m2.group(1) not in rss_status:
                    rss_status[m2.group(1)] = "✗" not in m2.group(2)

    def fmt_src(name, ok, kv_last, count_key):
        return {"name": name, "ok": bool(ok), "cnt24": cnt24(count_key), "last": fmt_ts(kv_last)}

    # 计数键与库内 screen_name 一致：X 无 @ 前缀，RSS 带 "RSS:" 前缀（2026-11-08 修复恒 0）
    x_items = [fmt_src("@" + a["screen_name"],
                       x_status.get(a["screen_name"], True),
                       db.get_kv("acc_last_" + a["screen_name"].lower()),
                       a["screen_name"])
               for a in accounts]
    rss_items = [fmt_src("[RSS] " + s["name"], rss_status.get(s["name"], True),
                         db.get_kv("rss_last_" + str(s["id"])),
                         "RSS:" + s["name"])
                 for s in db.list_rss_sources()]
    return {
        "x": {"total": len(accounts), "enabled": len([a for a in accounts if a["enabled"]]),
              "items": x_items},
        "rss": {"total": len(db.list_rss_sources()),
                "enabled": len(db.list_rss_sources(enabled_only=True)),
                "items": rss_items},
    }


@router.get("/api/stats", dependencies=[Depends(auth.require("read"))])
def api_stats():
    push_ok, push_total, push_sim = db.push_stats_today()
    a = db.ai_stats()
    lf = db.get_kv("last_fetch")
    accounts = db.list_x_accounts()
    ai = load().get("ai", {})
    return {
        "today": db.get_conn().execute(
            "SELECT COUNT(*) AS n FROM tweets WHERE date(created_at)=date('now','localtime')").fetchone()["n"],
        "total": db.get_conn().execute("SELECT COUNT(*) AS n FROM tweets").fetchone()["n"],
        "ai_analyzed": a["analyzed"], "ai_translated": a["translated"],
        "ai_today": a["today_analyzed"],
        "ai_auto_on": bool(ai.get("enabled") and ai.get("auto_analyze") and ai.get("api_key")),
        "geo_tagged": db.get_conn().execute(
            "SELECT COUNT(*) AS n FROM tweets WHERE geo_country!=''").fetchone()["n"],
        "categorized": db.get_conn().execute(
            "SELECT COUNT(*) AS n FROM tweets WHERE category!=''").fetchone()["n"],
        "accounts": len(accounts), "accounts_enabled": len([a for a in accounts if a["enabled"]]),
        "sources": _sources_health(accounts),
        "push_ok": push_ok, "push_total": push_total, "push_sim": push_sim,
        "next_run_in": (scheduler_instance().next_run_in() if scheduler_instance() else None),
        "fetch_running": engine.is_running(),
        "fetch_interval_minutes": load()["fetch"]["interval_minutes"],
        "last_fetch": json.loads(lf) if lf else None,
    }


def _effective_interval_and_reschedule():
    """来源频率/全局周期变更后，把调度器重设为有效最小周期"""
    try:
        from .scheduler import instance
        sch = instance()
        if sch is not None:
            sch.reschedule(effective_interval_minutes(load()))
    except Exception:
        pass


@router.post("/api/fetch/run", dependencies=[Depends(auth.require("write"))])
def api_fetch_run(bg: BackgroundTasks):
    bg.add_task(engine.run_fetch, True)
    return {"ok": True, "msg": "抓取任务已启动"}


@router.get("/api/fetch/logs", dependencies=[Depends(auth.require("read"))])
def api_fetch_logs(limit: int = Query(30, ge=1, le=200)):
    """抓取日志：每轮的触发方式/到期与成功账号数/新增与 AI 数/分层追踪明细"""
    return {"items": db.list_fetch_logs(limit)}


# ---------- X 账号管理 ----------

@router.get("/api/x/accounts", dependencies=[Depends(auth.require("read"))])
def api_x_accounts():
    return {"items": db.list_x_accounts()}


@router.post("/api/x/accounts", dependencies=[Depends(auth.require("write"))])
def api_x_add(body: XAccountBody):
    name = body.screen_name.strip().lstrip("@")
    if not twitter.X_ACC_RE.match(name):
        raise HTTPException(400, "非法 X 用户名（1-15 位字母/数字/下划线）")
    if not db.add_x_account(name, body.display.strip() or name):
        raise HTTPException(409, f"@{name} 已存在")
    _effective_interval_and_reschedule()
    return {"ok": True, "screen_name": name}


@router.post("/api/x/accounts/{acc_id}/toggle", dependencies=[Depends(auth.require("write"))])
def api_x_toggle(acc_id: int, body: ToggleBody):
    if not db.update_x_account(acc_id, enabled=body.enabled):
        raise HTTPException(404, "账号不存在")
    _effective_interval_and_reschedule()
    return {"ok": True, "enabled": body.enabled}


@router.put("/api/x/accounts/{acc_id}", dependencies=[Depends(auth.require("write"))])
def api_x_update(acc_id: int, body: XAccountBody):
    name = body.screen_name.strip().lstrip("@") if body.screen_name else None
    if name and not twitter.X_ACC_RE.match(name):
        raise HTTPException(400, "非法 X 用户名")
    if not db.update_x_account(acc_id, screen_name=name,
                               display=(body.display.strip() or None) if body.display else None,
                               fetch_minutes=body.fetch_minutes):
        raise HTTPException(404, "账号不存在或无变更")
    _effective_interval_and_reschedule()
    return {"ok": True}


@router.delete("/api/x/accounts/{acc_id}", dependencies=[Depends(auth.require("write"))])
def api_x_delete(acc_id: int):
    if not db.delete_x_account(acc_id):
        raise HTTPException(404, "账号不存在")
    _effective_interval_and_reschedule()
    return {"ok": True}


# ---------- RSS 源（监控源 02） ----------

class RssBody(BaseModel):
    name: str = ""
    url: str = ""
    enabled: bool = True
    fetch_minutes: int | None = None


@router.get("/api/rss/sources", dependencies=[Depends(auth.require("read"))])
def api_rss_list():
    return {"items": db.list_rss_sources()}


@router.post("/api/rss/sources", dependencies=[Depends(auth.require("write"))])
def api_rss_add(body: RssBody):
    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "RSS 地址必须以 http(s):// 开头")
    name = body.name.strip()
    if not name:
        from urllib.parse import urlparse
        host = urlparse(url).netloc
        name = {"feeds.feedburner.com": "TheHackersNews"}.get(host, host.split(".")[0].title())
    if not db.add_rss_source(name, url):
        raise HTTPException(409, f"该 RSS 地址已存在（{name}）")
    _effective_interval_and_reschedule()
    return {"ok": True, "name": name}


@router.post("/api/rss/test", dependencies=[Depends(auth.require("write"))])
def api_rss_test(body: RssBody):
    """连通性测试（不入库）：返回条目数与样例标题"""
    return rssfetch.test_source(body.url.strip())


@router.post("/api/rss/sources/{src_id}/toggle", dependencies=[Depends(auth.require("write"))])
def api_rss_toggle(src_id: int, body: ToggleBody):
    if not db.update_rss_source(src_id, enabled=body.enabled):
        raise HTTPException(404, "源不存在")
    _effective_interval_and_reschedule()
    return {"ok": True, "enabled": body.enabled}


@router.put("/api/rss/sources/{src_id}", dependencies=[Depends(auth.require("write"))])
def api_rss_update(src_id: int, body: RssBody):
    if not db.update_rss_source(src_id,
                                name=(body.name.strip() or None) if body.name else None,
                                url=(body.url.strip() or None) if body.url else None,
                                fetch_minutes=body.fetch_minutes):
        raise HTTPException(404, "源不存在或无变更")
    _effective_interval_and_reschedule()
    return {"ok": True}


@router.delete("/api/rss/sources/{src_id}", dependencies=[Depends(auth.require("write"))])
def api_rss_delete(src_id: int):
    if not db.delete_rss_source(src_id):
        raise HTTPException(404, "源不存在")
    _effective_interval_and_reschedule()
    return {"ok": True}


# ---------- X 采集设置（初次抓取条数等） ----------

X_SETTINGS_FIELDS = {"first_fetch_limit", "max_items", "show_replies"}


@router.get("/api/x/settings", dependencies=[Depends(auth.require("read"))])
def api_x_settings():
    x = dict(load().get("x", {}))
    return {"values": {k: x.get(k) for k in X_SETTINGS_FIELDS}}


@router.put("/api/x/settings", dependencies=[Depends(auth.require("write"))])
def api_x_settings_put(body: ValuesBody):
    values = {}
    for k, v in (body.values or {}).items():
        if k not in X_SETTINGS_FIELDS:
            continue
        if k == "show_replies":
            values[k] = bool(v)
        else:
            try:
                values[k] = max(1, min(100, int(v)))
            except Exception:
                raise HTTPException(400, f"{k} 需为 1-100 的整数")
    if values:
        save_runtime({"x": values})
    return {"ok": True, "saved": list(values.keys())}


# ---------- AI 设置 ----------

@router.get("/api/ai/config", dependencies=[Depends(auth.require("read"))])
def api_ai_config():
    ai = dict(load().get("ai", {}))
    out = {k: ai.get(k) for k in ("enabled", "base_url", "model", "auto_analyze", "auto_limit")}
    out["api_key"] = _mask(ai.get("api_key"))
    out["has_key"] = bool(ai.get("api_key"))
    return {"values": out}


@router.put("/api/ai/config", dependencies=[Depends(auth.require("write"))])
def api_ai_config_put(body: ValuesBody):
    allowed = {"enabled", "base_url", "model", "api_key", "auto_analyze", "auto_limit"}
    values = {k: v for k, v in (body.values or {}).items() if k in allowed}
    clean = {k: v for k, v in values.items() if "****" not in str(v)}
    if clean:
        save_runtime({"ai": clean})
    return {"ok": True, "saved": list(clean.keys())}


@router.post("/api/ai/test", dependencies=[Depends(auth.require("write"))])
def api_ai_test():
    try:
        return aimod.test_ai()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:180]}"}


@router.get("/api/ai/models", dependencies=[Depends(auth.require("read"))])
def api_ai_models():
    """拉取 AI 厂商 /models 模型列表（OpenAI 兼容端点，按当前已保存配置）"""
    try:
        ids = aimod.list_models()
        return {"ok": True, "models": ids}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:180]}", "models": []}


@router.get("/api/ai/logs", dependencies=[Depends(auth.require("read"))])
def api_ai_logs(limit: int = Query(100, ge=1, le=500), action: str = Query("")):
    """AI 调用审计日志：动作（研判/翻译/分类/测试）/模型/耗时/状态；附今日统计"""
    return {"items": db.list_ai_logs(limit, action.strip()), "stats": db.ai_log_stats()}


@router.post("/api/ai/classify", dependencies=[Depends(auth.require("write"))])
def api_ai_classify(bg: BackgroundTasks, limit: int = Query(200, ge=10, le=200)):
    """对近期未分类推文做情报分类：脚本规则优先，AI 批量兜底（后台执行）"""
    bg.add_task(engine.classify_recent, limit)
    return {"ok": True, "msg": f"分类任务已启动（本轮最多 {limit} 条）"}


@router.post("/api/ai/backfill", dependencies=[Depends(auth.require("write"))])
def api_ai_backfill(bg: BackgroundTasks, limit: int = Query(10, ge=1, le=50)):
    """为缺地区标签的旧推文批量补跑 AI 打标（后台执行）"""
    bg.add_task(engine.backfill_tags, limit)
    return {"ok": True, "msg": f"补标任务已启动（本轮最多 {limit} 条）"}


# ---------- AI 独立接口（供外部工具 / AI Agent 直接调用，不落库） ----------

def _normalize_ai_fields(f):
    def j(key):
        try:
            return json.loads(f.get(key) or "[]")
        except Exception:
            return []
    return {
        "threat_type": f.get("ai_type", ""),
        "severity": f.get("ai_severity", ""),
        "confidence": f.get("ai_confidence", 0),
        "assessment": f.get("ai_assessment", ""),
        "recommendations": j("ai_recommendations"),
        "actors": j("ai_actors"),
        "iocs": j("ai_iocs"),
        "tags": j("tags"),
        "country": f.get("geo_country", ""),
        "province": f.get("geo_province", ""),
        "city": f.get("geo_city", ""),
        "organizations": j("org"),
        "industry": f.get("industry", ""),
        "model": f.get("ai_model", ""),
    }


@router.post("/api/ai/analyze", dependencies=[Depends(auth.require("write"))])
def api_ai_analyze(body: AiTextBody):
    """研判任意文本（不需要先入库）：返回结构化威胁研判结果"""
    if not body.text.strip():
        raise HTTPException(400, "text 不能为空")
    try:
        fields = aimod.analyze_tweet({"screen_name": body.screen_name, "text": body.text})
        return {"ok": True, "result": _normalize_ai_fields(fields)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


@router.post("/api/ai/translate", dependencies=[Depends(auth.require("write"))])
def api_ai_translate(body: AiTextBody):
    """翻译任意文本为中文：返回 {title_zh, text_zh}"""
    if not body.text.strip():
        raise HTTPException(400, "text 不能为空")
    try:
        f = aimod.translate_tweet({"text": body.text})
        return {"ok": True, "title_zh": f.get("title_zh", ""), "text_zh": f.get("text_zh", "")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


@router.post("/api/ai/tag", dependencies=[Depends(auth.require("write"))])
def api_ai_tag(body: AiTextBody):
    """地理/实体标签抽取：返回 国家/省州/城市/涉事单位/行业"""
    if not body.text.strip():
        raise HTTPException(400, "text 不能为空")
    try:
        fields = aimod.analyze_tweet({"screen_name": body.screen_name, "text": body.text})
        r = _normalize_ai_fields(fields)
        return {"ok": True, "result": {k: r[k] for k in
                ("country", "province", "city", "organizations", "industry", "model")}}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ---------- 后台 AI 任务（第三批） ----------


class AiTaskBody(BaseModel):
    op: str = "analyze_backlog"     # analyze_backlog / retry_failed
    limit: int = 20


@router.post("/api/ai/tasks", dependencies=[Depends(auth.require("write"))])
def api_ai_task_submit(body: AiTaskBody):
    """提交后台 AI 任务（页面只提交与查询进度，不再承担长批量请求）。
    op: analyze_backlog=未研判+首败重试；retry_failed=研判失败积压。"""
    if body.op not in ("analyze_backlog", "retry_failed"):
        raise HTTPException(400, "op 仅支持 analyze_backlog/retry_failed")
    limit = max(1, min(500, int(body.limit)))
    active = db.get_conn().execute(
        "SELECT COUNT(*) n FROM ai_tasks WHERE status IN ('待处理','处理中')").fetchone()["n"]
    if active >= 3:
        raise HTTPException(409, "已有后台 AI 任务在队列中，请等待完成")
    tid = db.create_ai_task(body.op, {"limit": limit}, total=0)
    return {"ok": True, "id": tid, "op": body.op, "limit": limit}


@router.get("/api/ai/tasks", dependencies=[Depends(auth.require("read"))])
def api_ai_tasks(limit: int = 20):
    items = db.list_ai_tasks(limit)
    active = [t for t in items if t["status"] in ("待处理", "处理中")]
    return {"items": items, "active": len(active)}


@router.post("/api/ai/tasks/{task_id}/cancel", dependencies=[Depends(auth.require("write"))])
def api_ai_task_cancel(task_id: int):
    with db._lock:
        cur = db.get_conn().execute(
            "UPDATE ai_tasks SET status='已取消', lease_until=NULL, updated_at=?"
            " WHERE id=? AND status IN ('待处理','处理中')", (db.now(), task_id))
        db.get_conn().commit()
        if cur.rowcount == 0:
            raise HTTPException(409, "任务不在可取消状态")
    return {"ok": True}


# ---------- 人工修订（第三批：ai_modified 保护） ----------


class ManualBody(BaseModel):
    geo_country: str | None = None
    geo_province: str | None = None
    geo_city: str | None = None
    industry: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    note: str = ""


@router.post("/api/tweets/{tid}/manual", dependencies=[Depends(auth.require("write"))])
def api_tweet_manual(tid: int, body: ManualBody):
    """人工修订：写入人工有效值并置 ai_modified=1；
    此后自动研判/批量补标跳过该推文（有效值在列表/筛选/推送/报告/导出读取一致）。"""
    t = db.get_tweet(tid)
    if not t:
        raise HTTPException(404, "推文不存在")
    fields = {}
    for k in ("geo_country", "geo_province", "geo_city", "industry", "category"):
        v = getattr(body, k)
        if v is not None:
            fields[k] = str(v).strip()[:60]
    if body.tags is not None:
        tags = [str(x).strip()[:40] for x in body.tags if str(x).strip()][:10]
        fields["tags"] = json.dumps(tags, ensure_ascii=False)
    if not fields and not body.note:
        raise HTTPException(400, "无可修订字段")
    fields["geo_basis"] = "manual" if "geo_country" in fields else t.get("geo_basis", "")
    fields["ai_analyzed_at"] = db.now()
    ver = db.manual_override(tid, fields)   # 原子：字段 + ai_modified=1 + 版本+1
    if ver is None:
        raise HTTPException(404, "推文不存在")
    db.add_ai_log("人工修订", detail=(body.note or "manual override")[:180], tweet_id=tid,
                  status="ok")
    return {"ok": True, "modified": fields, "analysis_version": ver}


class ManualForceBody(BaseModel):
    action: str = "reanalyze"       # reanalyze=解除人工保护后重新分析


@router.post("/api/tweets/{tid}/manual/force", dependencies=[Depends(auth.require("write"))])
def api_tweet_manual_force(tid: int, body: ManualForceBody):
    """显式重新分析：解除人工保护后重新研判（旧人工值被覆盖，属显式操作）。"""
    if body.action != "reanalyze":
        raise HTTPException(400, "action 仅支持 reanalyze")
    r = engine.run_single_ai(tid, "analyze", force=True)
    if not r:
        raise HTTPException(404, "推文不存在")
    return r


class ReviewBody(BaseModel):
    note: str = ""


@router.post("/api/tweets/{tid}/review", dependencies=[Depends(auth.require("write"))])
def api_tweet_review(tid: int, body: ReviewBody):
    """人工审核通过：将当前版本标记为已核验（ai_review=manual）。
    约束：审核绑定当前 analysis_version；内容再次变化（版本推进）后审核自动失效。"""
    t = db.get_tweet(tid)
    if not t:
        raise HTTPException(404, "推文不存在")
    ver = int(t.get("analysis_version") or 0)
    db.update_ai(tid, {"ai_review": "manual", "ai_review_at": db.now(),
                       "ai_review_version": ver})
    db.add_ai_log("人工审核", detail=(body.note or "review approved")[:180],
                  tweet_id=tid, status="ok")
    return {"ok": True, "reviewed_version": ver}


@router.get("/api/ai/conflicts", dependencies=[Depends(auth.require("read"))])
def api_ai_conflicts(limit: int = 50):
    """AI 结果冲突审计（版本不符/人工保护导致未应用的结果）"""
    return {"items": db.list_conflicts(limit)}


# ---------- 推送过滤 ----------

PUSH_FILTER_FIELDS = {"mode", "min_severity", "categories", "countries", "industries", "tags", "keywords"}


@router.get("/api/push/filters", dependencies=[Depends(auth.require("read"))])
def api_push_filters():
    f = dict(load().get("push", {}).get("filters", {}))
    return {"values": {k: f.get(k, "") for k in PUSH_FILTER_FIELDS}}


# ---------- 推送规则库（多组命名规则，通道实例按 id 绑定） ----------

class RuleBody(BaseModel):
    name: str
    mode: str = "and"
    min_severity: str = ""
    categories: str = ""
    countries: str = ""
    industries: str = ""
    tags: str = ""
    keywords: str = ""


def _rules(cfg):
    return (cfg.get("push") or {}).get("rules") or []


def _save_rules(rules):
    save_runtime({"push": {"rules": rules}})


@router.get("/api/push/rules", dependencies=[Depends(auth.require("read"))])
def api_push_rules():
    cfg = load()
    rules = _rules(cfg)
    # 附加引用计数（哪些实例绑定了该规则）
    ref = {}
    for ch in CHANNELS:
        for it in (_load_channel(ch.key).get("instances") or []):
            rid = str(it.get("filter_rule") or "")
            if rid:
                ref[rid] = ref.get(rid, 0) + 1
    return {"items": [{**r, "refs": ref.get(r.get("id"), 0)} for r in rules]}


@router.post("/api/push/rules", dependencies=[Depends(auth.require("write"))])
def api_push_rules_add(body: RuleBody):
    cfg = load()
    rules = _rules(cfg)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "规则名称不能为空")
    if any(r.get("name") == name for r in rules):
        raise HTTPException(409, f"规则「{name}」已存在")
    mode = body.mode.strip().lower()
    if mode not in ("and", "or"):
        mode = "and"
    rule = {"id": "r" + str(int(time.time() * 1000))[-9:], "name": name, "mode": mode,
            "min_severity": body.min_severity.strip(),
            "categories": body.categories.strip(),
            "countries": body.countries.strip(),
            "industries": body.industries.strip(),
            "tags": body.tags.strip(),
            "keywords": body.keywords.strip()}
    rules.append(rule)
    _save_rules(rules)
    db.reevaluate_filtered()
    return {"ok": True, "id": rule["id"]}


@router.put("/api/push/rules/{rid}", dependencies=[Depends(auth.require("write"))])
def api_push_rules_update(rid: str, body: RuleBody):
    cfg = load()
    rules = _rules(cfg)
    for r in rules:
        if r.get("id") == rid:
            if body.name.strip():
                r["name"] = body.name.strip()
            _m = body.mode.strip().lower()
            r["mode"] = _m if _m in ("and", "or") else "and"
            r["min_severity"] = body.min_severity.strip()
            r["categories"] = body.categories.strip()
            r["countries"] = body.countries.strip()
            r["industries"] = body.industries.strip()
            r["tags"] = body.tags.strip()
            r["keywords"] = body.keywords.strip()
            _save_rules(rules)
            db.reevaluate_filtered()
            return {"ok": True}
    raise HTTPException(404, "规则不存在")


@router.delete("/api/push/rules/{rid}", dependencies=[Depends(auth.require("write"))])
def api_push_rules_del(rid: str):
    cfg = load()
    rules = _rules(cfg)
    rest = [r for r in rules if r.get("id") != rid]
    if len(rest) == len(rules):
        raise HTTPException(404, "规则不存在")
    _save_rules(rest)
    db.reevaluate_filtered()
    # 清除各实例对该规则的绑定（回退默认）
    changed = {}
    for ch in CHANNELS:
        cc = _load_channel(ch.key)
        dirty = False
        for it in cc.get("instances") or []:
            if str(it.get("filter_rule") or "") == rid:
                it["filter_rule"] = ""
                dirty = True
        if dirty:
            changed[ch.key] = cc
    for key, cc in changed.items():
        _save_channel(key, cc)
    return {"ok": True, "unbound": len(changed)}


@router.put("/api/push/filters", dependencies=[Depends(auth.require("write"))])
def api_push_filters_put(body: ValuesBody):
    values = {k: str(v).strip() for k, v in (body.values or {}).items() if k in PUSH_FILTER_FIELDS}
    if values:
        save_runtime({"push": {"filters": values}})
    db.reevaluate_filtered()   # 规则变更：已过滤投递重新评估（不永久跳过）
    return {"ok": True, "saved": list(values.keys())}


# ---------- 推送通道（多实例：每类型 N 个实例，实例级推送筛选） ----------

FILTER_FIELDS = ["min_severity", "countries", "industries", "keywords"]


def _migrate_channel_instances(key, cc):
    """旧单实例结构（凭据直接在 key 下+enabled）→ instances 列表（一次迁移，幂等）"""
    if isinstance(cc.get("instances"), list):
        return cc
    if not cc:
        return {"instances": []}
    inst = {k: v for k, v in cc.items() if k not in ("instances",)}
    inst.setdefault("name", "默认")
    inst.setdefault("enabled", inst.pop("enabled", True))
    return {"instances": [inst]}


def _load_channel(key):
    cc = dict(load().get("channels", {}).get(key, {}))
    return _migrate_channel_instances(key, cc)


def _save_channel(key, cc):
    save_runtime({"channels": {key: cc}})


@router.get("/api/channels", dependencies=[Depends(auth.require("read"))])
def api_channels():
    cfg = load()
    items = []
    for ch in CHANNELS:
        cc = _load_channel(ch.key)
        insts = cc.get("instances") or []
        items.append({
            "key": ch.key, "name": ch.display,
            "instances": [{
                "idx": i,
                "iid": str(it.get("iid") or ""),
                "name": (it.get("name") or f"{ch.display}{i + 1}"),
                "enabled": bool(it.get("enabled")),
                "simulated": not ch.configured(it),
                "filter_rule": str(it.get("filter_rule") or ""),
            } for i, it in enumerate(insts)],
            "last": db.last_push_for(ch.display),
        })
    return {"items": items}


class InstanceBody(BaseModel):
    name: str = ""
    enabled: bool = True
    values: dict = {}
    filter_rule: str = ""  # 绑定的推送规则 id；空=使用默认规则


@router.post("/api/channels/{key}/instances", dependencies=[Depends(auth.require("write"))])
def api_channel_add(key: str, body: InstanceBody):
    if key not in CHANNEL_SECRET_FIELDS:
        raise HTTPException(404, "通道不存在")
    cc = _load_channel(key)
    inst = {"name": body.name.strip() or f"实例{len(cc['instances']) + 1}",
            "enabled": body.enabled}
    for f in CHANNEL_SECRET_FIELDS[key]:
        v = str(body.values.get(f, "")).strip()
        if v:
            inst[f] = v
    inst["filter_rule"] = body.filter_rule.strip()
    cc["instances"].append(inst)
    _save_channel(key, cc)
    return {"ok": True, "idx": len(cc["instances"]) - 1}


@router.put("/api/channels/{key}/instances/{idx}", dependencies=[Depends(auth.require("write"))])
def api_channel_update(key: str, idx: int, body: InstanceBody):
    if key not in CHANNEL_SECRET_FIELDS:
        raise HTTPException(404, "通道不存在")
    cc = _load_channel(key)
    if idx < 0 or idx >= len(cc["instances"]):
        raise HTTPException(404, "实例不存在")
    inst = cc["instances"][idx]
    if body.name.strip():
        inst["name"] = body.name.strip()
    inst["enabled"] = body.enabled
    clean = {k: v for k, v in ((f, str(body.values.get(f, "")).strip()) for f in CHANNEL_SECRET_FIELDS[key])
             if v and "****" not in v and "…" not in v}  # 掩码值（旧版 …/统一 ****）一律不落库
    inst.update(clean)
    inst["filter_rule"] = body.filter_rule.strip()  # 空字符串=使用默认规则
    _save_channel(key, cc)
    return {"ok": True}


@router.delete("/api/channels/{key}/instances/{idx}", dependencies=[Depends(auth.require("write"))])
def api_channel_del(key: str, idx: int):
    if key not in CHANNEL_SECRET_FIELDS:
        raise HTTPException(404, "通道不存在")
    cc = _load_channel(key)
    if idx < 0 or idx >= len(cc["instances"]):
        raise HTTPException(404, "实例不存在")
    cc["instances"].pop(idx)
    _save_channel(key, cc)
    return {"ok": True}


@router.get("/api/channels/{key}/instances/{idx}/config", dependencies=[Depends(auth.require("read"))])
def api_channel_inst_config(key: str, idx: int):
    """实例配置（凭据脱敏回显，含 filter_rule 绑定）"""
    if key not in CHANNEL_SECRET_FIELDS:
        raise HTTPException(404, "通道不存在")
    cc = _load_channel(key)
    if idx < 0 or idx >= len(cc["instances"]):
        raise HTTPException(404, "实例不存在")
    inst = cc["instances"][idx]
    out = {"name": inst.get("name", ""), "enabled": bool(inst.get("enabled"))}
    for f in CHANNEL_SECRET_FIELDS[key]:
        out[f] = _mask(inst.get(f, ""))
    out["filter_rule"] = str(inst.get("filter_rule") or "")
    return {"key": key, "idx": idx, "values": out,
            "iid": str(inst.get("iid") or ""), "enabled_since": inst.get("enabled_since", "")}


@router.post("/api/channels/{key}/instances/{idx}/test", dependencies=[Depends(auth.require("write"))])
def api_channel_inst_test(key: str, idx: int):
    """单实例测试推送"""
    if key not in CHANNEL_SECRET_FIELDS:
        raise HTTPException(404, "通道不存在")
    ch = next((c for c in CHANNELS if c.key == key), None)
    if not ch:
        raise HTTPException(404, "通道不存在")
    cc = _load_channel(key)
    if idx < 0 or idx >= len(cc["instances"]):
        raise HTTPException(404, "实例不存在")
    inst = dict(cc["instances"][idx])
    inst.setdefault("instance_name", inst.get("name") or ch.display)
    sim = not ch.configured(inst)
    try:
        if sim:
            ok, detail = True, "模拟模式（未配置凭据）"
        else:
            ok, detail = ch.send(inst, "X-Intel 测试消息", engine.build_test())
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"[:200]
    status = "模拟成功" if (sim and ok) else ("成功" if ok else "失败")
    disp = f"{ch.display}·{inst.get('name') or ch.display}"
    db.add_push_log(disp, "测试消息", status, detail)
    return {"ok": ok, "simulated": sim, "status": status, "detail": detail}


# 兼容旧前端/外部调用：按类型整体测试（第一个实例）
@router.post("/api/channels/{key}/test", dependencies=[Depends(auth.require("write"))])
def api_channel_test(key: str):
    r = engine.test_channel(key, load())
    if r is None:
        raise HTTPException(404, "通道不存在")
    return r


# ---------- 历史补发 / 迁移预览（显式、可预览、限范围） ----------


class BackfillBody(BaseModel):
    since: str = ""          # 入库时间下界（YYYY-MM-DD HH:MM:SS，含）
    until: str = ""          # 入库时间上界（含），空=现在
    limit: int = 100
    preview: bool = True


@router.post("/api/channels/{key}/instances/{idx}/backfill", dependencies=[Depends(auth.require("write"))])
def api_instance_backfill(key: str, idx: int, body: BackfillBody):
    """历史补发：为指定实例按入库时间窗创建投递记录（预览或执行）。
    显式操作——默认迁移不补发历史；执行受 limit 限速限范围。"""
    if key not in CHANNEL_SECRET_FIELDS:
        raise HTTPException(404, "通道不存在")
    cc = _load_channel(key)
    if idx < 0 or idx >= len(cc.get("instances") or []):
        raise HTTPException(404, "实例不存在")
    inst = cc["instances"][idx]
    iid = str(inst.get("iid") or "")
    since = body.since or "1970-01-01 00:00:00"
    until = body.until or "2999-12-31 23:59:59"
    cands = db.delivery_candidates(iid, since, cap=max(1, int(body.limit)) * 4)
    cands = [t for t in cands if t["created_at"] <= until][: max(1, int(body.limit))]
    if body.preview:
        return {"iid": iid, "preview": True, "count": len(cands),
                "since": since, "until": until,
                "samples": [{"id": t["id"], "title": (t.get("title_zh") or t["text"][:40]),
                             "created_at": t["created_at"]} for t in cands[:10]]}
    from .engine import pass_filter, _effective_rule, _push_filter
    rule_flt, rule_id, rule_snap = _effective_rule(load(), inst)
    created = filtered = 0
    for t in cands:
        hit = pass_filter(t, rule_flt)
        if db.create_delivery(t["id"], iid, key, rule_id, rule_snap,
                              "待发送" if hit else "已过滤",
                              reason="" if hit else "未命中规则[历史补发]"):
            created += 1
        else:
            filtered += 1
    return {"iid": iid, "executed": True, "created": created, "filtered": filtered,
            "since": since, "until": until}


@router.get("/api/admin/migration/preview", dependencies=[Depends(auth.require("admin"))])
def api_migration_preview():
    """迁移预览（只读）：涉及记录数、历史截止点（enabled_since）、投递状态分布、未知数量。
    迁移本身幂等可重复执行：新表 IF NOT EXISTS + report_refs 回填 INSERT OR IGNORE + iid 补齐。"""
    c = db.get_conn()
    insts = []
    for k, cc in ((load().get("channels") or {}).items()):
        for it in (cc.get("instances") or []):
            insts.append({"channel": k, "name": it.get("name"), "iid": it.get("iid"),
                          "enabled_since": it.get("enabled_since"),
                          "enabled": bool(it.get("enabled"))})
    msgs = {}
    for r in c.execute("SELECT state, COUNT(*) n FROM delivery_msgs GROUP BY state"):
        msgs[r["state"]] = r["n"]
    return {
        "tweets_total": c.execute("SELECT COUNT(*) n FROM tweets").fetchone()["n"],
        "pushed_1": c.execute("SELECT COUNT(*) n FROM tweets WHERE pushed=1").fetchone()["n"],
        "pushed_0": c.execute("SELECT COUNT(*) n FROM tweets WHERE pushed=0").fetchone()["n"],
        "report_tasks": c.execute("SELECT COUNT(*) n FROM report_tasks").fetchone()["n"],
        "report_refs": c.execute("SELECT COUNT(*) n FROM report_refs").fetchone()["n"],
        "deliveries": c.execute("SELECT COUNT(*) n FROM deliveries").fetchone()["n"],
        "delivery_msgs_by_state": msgs,
        "instances": insts,
        "note": "旧 pushed=1 不转换为'所有实例已发送'（历史实例级证据不存在）；"
                "迁移后实例 enabled_since=迁移时刻，默认不补发历史；补发走 backfill 显式接口",
    }


# ---------- 事件聚合 / 审核 / 导出（第六批） ----------


class EventBody(BaseModel):
    title: str
    summary: str = ""
    severity: str = ""
    tweet_ids: list[int] = []


class EventUpdateBody(BaseModel):
    title: str | None = None
    summary: str | None = None
    severity: str | None = None
    status: str | None = None
    expected_version: int | None = None


class EvidenceBody(BaseModel):
    tweet_id: int
    basis: str = "manual"
    source_kind: str = "original"
    note: str = ""


class MergeBody(BaseModel):
    src_uid: str
    dst_uid: str
    reason: str = ""


class SplitBody(BaseModel):
    tweet_ids: list[int]
    new_title: str = ""


class ReviewBody2(BaseModel):
    event_version: int
    decision: str          # approved / rejected
    note: str = ""


@router.get("/api/events", dependencies=[Depends(auth.require("read"))])
def api_events(status: str = "all", q: str = "", limit: int = Query(50, ge=1, le=200),
               offset: int = Query(0, ge=0)):
    total, items = db.list_events(status, q.strip(), limit, offset)
    return {"total": total, "items": items}


@router.post("/api/events", dependencies=[Depends(auth.require("write"))])
def api_event_create(body: EventBody, request: Request):
    if not body.title.strip():
        raise HTTPException(400, "title 不能为空")
    uid, v = db.create_event(body.title.strip(), body.summary.strip(), body.severity.strip(),
                             _operator(request), body.tweet_ids, "manual")
    return {"ok": True, "event_uid": uid, "version": v}


@router.get("/api/events/{uid}", dependencies=[Depends(auth.require("read"))])
def api_event_detail(uid: str):
    ev = db.get_event(uid)
    if not ev:
        raise HTTPException(404, "事件不存在")
    ev["evidence"] = db.event_evidence(uid)
    ev["stats"] = db.evidence_stats(uid)
    ev["revisions"] = db.event_revisions(uid)
    ev["reviews"] = db.event_reviews(uid)
    ev["review_latest"] = db.event_review_latest(uid)
    ev["candidates"] = []   # 这里不返回候选（需按推文查）
    return ev


@router.put("/api/events/{uid}", dependencies=[Depends(auth.require("write"))])
def api_event_update(uid: str, body: EventUpdateBody, request: Request):
    ok, v = db.update_event(uid, body.title, body.summary, body.severity, body.status,
                            _operator(request), body.expected_version)
    if not ok:
        raise HTTPException(409, f"更新失败（版本冲突或无效）。当前版本 {v}")
    return {"ok": True, "version": v}


@router.post("/api/events/{uid}/evidence", dependencies=[Depends(auth.require("write"))])
def api_event_add_evidence(uid: str, body: EvidenceBody, request: Request):
    ok, v = db.add_evidence(uid, body.tweet_id, body.basis, body.source_kind,
                            body.note, _operator(request))
    if not ok:
        raise HTTPException(409, f"证据未添加（重复或依据非法）。当前版本 {v}")
    return {"ok": True, "version": v}


@router.delete("/api/events/{uid}/evidence/{tweet_id}", dependencies=[Depends(auth.require("write"))])
def api_event_remove_evidence(uid: str, tweet_id: int, request: Request):
    ok, v = db.remove_evidence(uid, tweet_id, _operator(request))
    if not ok:
        raise HTTPException(404, "证据不存在")
    return {"ok": True, "version": v}


@router.post("/api/events/merge", dependencies=[Depends(auth.require("write"))])
def api_event_merge(body: MergeBody, request: Request):
    ok, dst = db.merge_events(body.src_uid, body.dst_uid, _operator(request), body.reason)
    if not ok:
        raise HTTPException(400, "合并失败（事件不存在或相同）")
    return {"ok": True, "target": dst}


@router.post("/api/events/{uid}/split", dependencies=[Depends(auth.require("write"))])
def api_event_split(uid: str, body: SplitBody, request: Request):
    if not body.tweet_ids:
        raise HTTPException(400, "tweet_ids 不能为空")
    ok, new_uid = db.split_event(uid, body.tweet_ids, body.new_title, _operator(request))
    if not ok:
        raise HTTPException(404, "事件不存在")
    return {"ok": True, "new_event": new_uid}


@router.get("/api/tweets/{tid}/event-candidates", dependencies=[Depends(auth.require("read"))])
def api_event_candidates(tid: int):
    """候选关联：仅共享标识等弱信号，**不得自动合并**，由人工决定"""
    return {"items": db.candidate_events(tid),
            "note": "共享 CVE/IP/组织名等标识不构成自动合并依据，需人工确认"}


@router.get("/api/events/review/queue", dependencies=[Depends(auth.require("read"))])
def api_event_review_queue(status: str = "", limit: int = 50):
    return {"items": db.review_queue(status, limit)}


@router.post("/api/events/{uid}/review", dependencies=[Depends(auth.require("write"))])
def api_event_review(uid: str, body: ReviewBody2, request: Request):
    ok, reason = db.review_event(uid, body.event_version, body.decision, body.note,
                                 _operator(request))
    if not ok:
        raise HTTPException(409 if reason.startswith("version") or reason == "already_reviewed" else 400,
                            f"审核未通过：{reason}")
    return {"ok": True, "decision": body.decision, "event_version": body.event_version}


@router.get("/api/events/{uid}/export.json", dependencies=[Depends(auth.require("read"))])
def api_event_export_json(uid: str):
    data = eventexport.export_event_json(uid)
    if data is None:
        raise HTTPException(404, "事件不存在")
    return JSONResponse(data, headers={"Content-Disposition":
                                       f'attachment; filename="{uid}.json"'})


@router.get("/api/events/{uid}/timeline.csv", dependencies=[Depends(auth.require("read"))])
def api_event_timeline_csv(uid: str):
    csv_text = eventexport.export_event_timeline_csv(uid)
    if csv_text is None:
        raise HTTPException(404, "事件不存在")
    return Response(content=csv_text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{uid}-timeline.csv"'})


@router.get("/api/events/{uid}/iocs.json", dependencies=[Depends(auth.require("read"))])
def api_event_ioc_json(uid: str):
    data = eventexport.export_event_ioc_json(uid)
    if data is None:
        raise HTTPException(404, "事件不存在")
    return JSONResponse(data, headers={"Content-Disposition":
                                       f'attachment; filename="{uid}-iocs.json"'})


@router.get("/api/events/{uid}/iocs.csv", dependencies=[Depends(auth.require("read"))])
def api_event_ioc_csv(uid: str):
    csv_text = eventexport.export_event_ioc_csv(uid)
    if csv_text is None:
        raise HTTPException(404, "事件不存在")
    return Response(content=csv_text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{uid}-iocs.csv"'})


# ---------- 报告任务模块（桥接 ZCode report-writer Agent） ----------

class ReportTaskBody(BaseModel):
    title: str
    keywords: list[str] = []
    tweet_ids: list[int] = []
    tier: str = "速报"
    notes: str = ""
    auto_push: bool = False


class ReportStatusBody(BaseModel):
    action: str = "claim"
    assignee: str = "report-writer@unknown"
    note: str = ""


class ReportDeliverBody(BaseModel):
    deliver_dir: str
    docx: str
    word_count: int | None = None
    ledger_total: int | None = None
    ledger_pass: int | None = None
    warnings: list[str] = []
    agent_gate: str = ""
    auto_push: bool = False
    word_limit: int | None = None


@router.post("/api/reports/tasks", dependencies=[Depends(auth.require("write"))])
def api_reports_create(body: ReportTaskBody):
    if not body.title.strip():
        raise HTTPException(400, "title 不能为空")
    r = reports.create_task(body.title.strip(), body.keywords, body.tier,
                            body.notes, body.tweet_ids, int(body.auto_push))
    return r


@router.get("/api/reports/tasks", dependencies=[Depends(auth.require("read"))])
def api_reports_list(status: str = "", limit: int = Query(20, ge=1, le=100),
                     offset: int = Query(0, ge=0)):
    total, items = reports.list_tasks(status, limit, offset)
    return {"tasks": items, "total": total}


@router.get("/api/reports/tasks/{tid}/package", dependencies=[Depends(auth.require("read"))])
def api_reports_package(tid: int):
    t = reports.get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    ppath = t.get("package_path")
    if not ppath or not os.path.isfile(ppath):
        raise HTTPException(404, "线索包不存在")
    return FileResponse(ppath, media_type="application/json", filename=os.path.basename(ppath))


@router.post("/api/reports/tasks/{tid}/status", dependencies=[Depends(auth.require("write"))])
def api_reports_status(tid: int, body: ReportStatusBody):
    if body.action == "claim":
        ok = reports.claim(tid, body.assignee)
        if not ok:
            raise HTTPException(409, "任务已被领取")
        t = reports.get_task(tid)
        return {"id": tid, "status": t["status"]}
    if body.action == "heartbeat":
        ok = reports.heartbeat(tid, body.assignee, body.note)
        return {"ok": ok}
    if body.action == "abort":
        ok = reports.abort(tid, body.note)
        if not ok:
            raise HTTPException(409, "任务不在写作中状态")
        return {"id": tid, "status": "待写"}
    raise HTTPException(400, "action 仅支持 claim/heartbeat/abort")


@router.post("/api/reports/tasks/{tid}/deliver", dependencies=[Depends(auth.require("write"))])
def api_reports_deliver(tid: int, body: ReportDeliverBody):
    r = reports.deliver(tid, body.model_dump())
    if r is None:
        raise HTTPException(404, "任务不存在")
    if r.get("conflict"):
        raise HTTPException(409, f"状态为 {r.get('status')}，仅'写作中'可交付")
    return r


@router.post("/api/reports/tasks/{tid}/push", dependencies=[Depends(auth.require("write"))])
def api_reports_push(tid: int):
    r = reports.push_task(tid)
    if r is None:
        raise HTTPException(404, "任务不存在")
    if r.get("error"):
        raise HTTPException(409, r["error"])
    return r


@router.delete("/api/reports/tasks/{tid}", dependencies=[Depends(auth.require("write"))])
def api_reports_delete(tid: int):
    ok = reports.delete_task(tid)
    if not ok:
        if reports.get_task(tid) and reports.get_task(tid)["status"] == "写作中":
            raise HTTPException(409, "写作中的任务不可删除（请先 abort）")
        raise HTTPException(404, "任务不存在")
    return {"ok": True, "deleted": tid}


@router.get("/api/push/logs", dependencies=[Depends(auth.require("read"))])
def api_push_logs(limit: int = Query(20, ge=1, le=100)):
    return {"items": db.list_push_logs(limit)}


# ---------- 认证（第四批） ----------

class LoginBody(BaseModel):
    username: str
    password: str


class TokenBody(BaseModel):
    name: str
    role: str = "read"


@router.post("/api/auth/login")
def api_auth_login(body: LoginBody, response: Response):
    if not auth.init_ready():
        raise HTTPException(503, "系统未初始化管理员：请在本机创建 data/admin_init.txt 后重启")
    u = auth.authenticate(body.username.strip(), body.password)
    if not u:
        raise HTTPException(401, "用户名或密码错误")
    sid, csrf = auth.new_session(u["username"])
    response.set_cookie(
        auth.COOKIE_NAME, sid, max_age=auth.SESSION_HOURS * 3600,
        httponly=True, samesite="lax", secure=False, path="/")
    return {"ok": True, "username": u["username"], "role": u["role"], "csrf": csrf}


@router.post("/api/auth/logout")
def api_auth_logout(request: Request):
    sid = request.cookies.get(auth.COOKIE_NAME, "")
    if sid:
        auth.drop_session(sid)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@router.get("/api/auth/me")
def api_auth_me(request: Request):
    p = auth._extract_principal(request)
    if not p:
        raise HTTPException(401, "未认证")
    return {"ok": True, "username": p.get("username", ""), "role": p.get("role", ""),
            "kind": p.get("kind", ""), "csrf": p.get("csrf", "")}


@router.get("/api/auth/tokens", dependencies=[Depends(auth.require("admin"))])
def api_auth_tokens():
    return {"items": auth.list_tokens()}


@router.post("/api/auth/tokens", dependencies=[Depends(auth.require("admin"))])
def api_auth_token_add(body: TokenBody):
    if not body.name.strip():
        raise HTTPException(400, "Token 名称不能为空")
    try:
        raw = auth.create_token(body.name.strip(), body.role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "token": raw, "note": "完整 Token 仅显示这一次，请立即保存"}


@router.post("/api/auth/tokens/{tid}/revoke", dependencies=[Depends(auth.require("admin"))])
def api_auth_token_revoke(tid: int):
    if not auth.revoke_token(tid):
        raise HTTPException(404, "Token 不存在")
    return {"ok": True}
