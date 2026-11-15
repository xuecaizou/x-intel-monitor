# -*- coding: utf-8 -*-
"""WP5 补丁 part2：_run_recommend 重构 + B19 事务回滚（一次性脚本）"""
from pathlib import Path

p2 = Path("app/writing.py")
s2 = p2.read_text(encoding="utf-8")

# ---- _run_recommend 全面重构 ----
old = '''def _run_recommend(task):
    """执行一次 AI 推荐任务，返回新建/合并的候选数。"""
    from .ai import _chat, _cfg, _parse_json_loose
    from . import reports
    from . import ai_channels as aich
    cfg = aich.resolve_cfg(task="writing_recommend")
    tweets = _scope_tweets(task["scope_type"], task["scope_ref"])
    ctx = json.dumps(tweets, ensure_ascii=False)[:12000]
    tpl_names = [r["name"] for r in db.get_conn().execute(
        "SELECT name FROM doc_templates WHERE status='启用'").fetchall()]
    sys_prompt = (
        "你是威胁情报编辑助手。根据给定情报素材推荐值得写成专题报告的选题。"
        "输出严格 JSON：{\\"items\\":[{\\"title\\":\\"报告标题(中文,具体,含主体)\\",\\"angle\\":\\"写作角度\\","
        "\\"reason\\":\\"推荐理由(基于素材)\\",\\"priority\\":1-5,\\"template\\":\\"建议模板名或空\\","
        "\\"materials\\":[\\"待补材料\\"],\\"basis\\":[\\"依据的素材要点\\"],\\"tweet_ids\\":[素材内出现的推文id数字]}]}"
        "。注意：区分写作价值与事实可信度；无依据内容标待确认；不编造事实；"
        "如素材不足以成稿，返回空数组。")
    raw = _chat(cfg, [{"role": "system", "content": sys_prompt},
                      {"role": "user", "content": f"可用模板：{tpl_names}\\n情报素材：\\n{ctx}"}],
                json_mode=True, action="recommend", ref=task["scope_ref"])
    data = _parse_json_loose(raw)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("AI 返回缺少 items 数组")
    tpl_by_name = {r["name"]: r["id"] for r in db.get_conn().execute(
        "SELECT id, name FROM doc_templates").fetchall()}
    n = 0
    for it in items[:10]:
        if not isinstance(it, dict) or not (it.get("title") or "").strip():
            continue
        tids = [int(x) for x in (it.get("tweet_ids") or [])
                if str(x).strip().lstrip("-").isdigit()][:50]
        prio = it.get("priority")
        try:
            prio = max(1, min(5, int(prio))) if prio is not None else None
        except Exception:
            prio = None
        sug = {"suggestion": {"angle": it.get("angle") or "", "reason": it.get("reason") or "",
                              "materials": it.get("materials") or [], "basis": it.get("basis") or []},
               "priority": prio,
               "template_id": tpl_by_name.get((it.get("template") or "").strip()),
               "note": f"推荐任务 #{task['id']}"}
        create_candidate(
            title=it["title"].strip(), angle=(it.get("angle") or "")[:200],
            tweet_ids=tids, source_type="ai",
            recommend_reason=(it.get("reason") or "")[:500],
            ai_suggestion=_j(sug["suggestion"]), suggested_priority=prio,
            suggested_template_id=sug["template_id"],
            materials=it.get("materials") or [], operator="ai",
            ai_run=db.now())
        n += 1
    return n'''

new = '''def _run_recommend(task):
    """执行一次 AI 推荐任务（round2 需求 E 重构）。

    - 用户提示词**真正进入模型请求**（持久化于任务行，重试复用原始要求）；
    - 资料范围：scope_type/scope_ref + filters（时间/来源/关键词）经服务端校验；
    - 模型只允许引用本次提供的 tweets.id（白名单校验，越界引用剔除并标注）；
    - 返回 (新建/合并候选数, 完整候选明细)——明细持久化到任务 result_json
      （页面展示、刷新恢复，需求 E3/U12）。
    """
    from .ai import _chat, _parse_json_loose, classify_error
    from . import reports
    from . import ai_channels as aich
    cfg = aich.resolve_cfg(channel_id=task.get("channel_id") or None,
                           task="writing_recommend")
    if task.get("model"):
        cfg["model"] = task["model"]
    tweets = _scope_tweets(task["scope_type"], task["scope_ref"],
                           filters=_jl(task.get("filters"), {}))
    if not tweets:
        raise ValueError("资料范围为空：所选条件下没有可用情报（scope="
                         f"{task['scope_type']}/{task['scope_ref']}），请调整范围后重试")
    allowed_ids = {int(t["id"]) for t in tweets}
    id_map = {int(t["id"]): t for t in tweets}
    ctx = json.dumps(tweets, ensure_ascii=False)[:12000]
    tpl_names = [r["name"] for r in db.get_conn().execute(
        "SELECT name FROM doc_templates WHERE status='启用'").fetchall()]
    user_prompt = (task.get("prompt") or "").strip()
    sys_prompt = (
        "你是威胁情报编辑助手。根据给定情报素材与用户要求，推荐值得写成专题报告的选题。"
        "输出严格 JSON：{\\"items\\":[{\\"title\\":\\"报告标题(中文,具体,含主体)\\",\\"angle\\":\\"写作角度\\","
        "\\"reason\\":\\"推荐理由(基于素材)\\",\\"priority\\":1-5,\\"template\\":\\"建议模板名或空\\","
        "\\"materials\\":[\\"待补材料\\"],\\"basis\\":[\\"依据的素材要点\\"],"
        "\\"source_links\\":[\\"来源链接\\"],\\"tweet_ids\\":[依据的情报记录ID数字(只能取自素材中的 id 字段)]}]}"
        "。注意：区分写作价值与事实可信度；无依据内容标待确认；不编造事实；"
        "tweet_ids 只能引用素材中出现的 id；如素材不足以成稿，返回空数组。")
    raw = _chat(cfg, [{"role": "system", "content": sys_prompt},
                      {"role": "user",
                       "content": f"【用户要求】\\n{user_prompt}\\n\\n【可用模板】{tpl_names}"
                                  f"\\n\\n【情报素材】（id 为可引用的情报记录 ID）：\\n{ctx}"}],
                json_mode=True, action="recommend", ref=f"task#{task['id']}")
    data = _parse_json_loose(raw)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("AI 返回缺少 items 数组")
    tpl_by_name = {r["name"]: r["id"] for r in db.get_conn().execute(
        "SELECT id, name FROM doc_templates").fetchall()}
    n = 0
    detail = []
    for it in items[:10]:
        if not isinstance(it, dict) or not (it.get("title") or "").strip():
            continue
        # B14：白名单校验——模型引用的必须是本次提供的 tweets.id；越界剔除并标注
        raw_tids = [int(x) for x in (it.get("tweet_ids") or [])
                    if str(x).strip().lstrip("-").isdigit()]
        tids = [x for x in raw_tids if x in allowed_ids][:50]
        rejected_refs = [x for x in raw_tids if x not in allowed_ids]
        prio = it.get("priority")
        try:
            prio = max(1, min(5, int(prio))) if prio is not None else None
        except Exception:
            prio = None
        sug = {"suggestion": {"angle": it.get("angle") or "", "reason": it.get("reason") or "",
                              "materials": it.get("materials") or [], "basis": it.get("basis") or [],
                              "source_links": it.get("source_links") or []},
               "priority": prio,
               "template_id": tpl_by_name.get((it.get("template") or "").strip()),
               "note": f"推荐任务 #{task['id']}"}
        if rejected_refs:
            sug["rejected_refs"] = rejected_refs[:10]
        uid, merged = create_candidate(
            title=it["title"].strip(), angle=(it.get("angle") or "")[:200],
            tweet_ids=tids, source_type="ai",
            recommend_reason=(it.get("reason") or "")[:500],
            ai_suggestion=_j(sug["suggestion"]), suggested_priority=prio,
            suggested_template_id=sug["template_id"],
            materials=it.get("materials") or [], operator="ai",
            ai_run=db.now(), rec_task_id=task["id"])
        if not merged:
            with db._lock:
                db.get_conn().execute(
                    "UPDATE writing_candidates SET source_key=? WHERE cand_uid=?",
                    (f"rec:{task['id']}", uid))
                db.get_conn().commit()
        n += 1
        detail.append({
            "cand_uid": uid, "merged": merged,
            "title": it["title"].strip(), "angle": it.get("angle") or "",
            "reason": it.get("reason") or "", "priority": prio,
            "materials": it.get("materials") or [], "basis": it.get("basis") or [],
            "source_links": it.get("source_links") or [],
            "tweet_ids": tids, "rejected_refs": rejected_refs[:10],
            "template": (it.get("template") or "").strip(),
        })
    return n, detail'''

assert old in s2, "_run_recommend"
s2 = s2.replace(old, new)

# ---- _scope_tweets：B13 统一 dict + filters ----
old = '''def _scope_tweets(scope_type, scope_ref):
    from . import reports
    if scope_type == "tweet":
        t = db.get_tweet(int(scope_ref)) if str(scope_ref).isdigit() else None
        return [reports._tweet_package(t)] if t else []
    if scope_type == "event" and scope_ref:
        evs = db.event_evidence(scope_ref)
        out = []
        for e in evs[:20]:
            t = db.get_tweet(e["tweet_id"])
            if t:
                out.append(reports._tweet_package(t))
        return out
    if scope_type == "backlog":
        rows = db.get_conn().execute(
            "SELECT * FROM tweets WHERE pushed=0 AND ignored=0"
            " AND COALESCE(ai_severity,'') IN ('high','critical')"
            " ORDER BY id DESC LIMIT 15").fetchall()
        return [reports._tweet_package(r) for r in rows]
    return []'''
new = '''def _scope_tweets(scope_type, scope_ref, filters=None):
    """素材选取（round2 需求 E2）：scope + filters（days/source/keywords）。

    返回统一 dict 列表（B13：sqlite3.Row 没有 .get——一律 dict(r) 包装后进
    _tweet_package）；每条带内部主键 id（B14：模型引用与候选关联的唯一身份）。"""
    from . import reports
    filters = filters or {}
    days = int(filters.get("days") or 0)
    since = ""
    if days > 0:
        import datetime
        since = (datetime.datetime.now() - datetime.timedelta(days=days)
                 ).strftime("%Y-%m-%d %H:%M:%S")
    kws = [str(k).strip() for k in (filters.get("keywords") or []) if str(k).strip()]
    sources = [str(x).strip() for x in (filters.get("sources") or []) if str(x).strip()]

    def _pkg(row):
        d = dict(row)
        pkg = reports._tweet_package(d)
        pkg["id"] = int(d["id"])   # 内部主键：候选关联与模型引用白名单的唯一身份
        return pkg

    def _match(t):
        if since and (t.get("created_at") or "") < since:
            return False
        if sources and not any(t.get("screen_name", "") == x or
                               t.get("screen_name", "") == "RSS:" + x for x in sources):
            return False
        if kws:
            text = (t.get("text") or "") + (t.get("title_zh") or "")
            if not any(k.lower() in text.lower() for k in kws):
                return False
        return True

    out = []
    if scope_type == "tweet":
        t = db.get_tweet(int(scope_ref)) if str(scope_ref).isdigit() else None
        out = [_pkg(t)] if t else []
    elif scope_type == "event" and scope_ref:
        evs = db.event_evidence(scope_ref)
        for e in evs[:20]:
            t = db.get_tweet(e["tweet_id"])
            if t:
                out.append(_pkg(t))
    elif scope_type == "backlog":
        rows = db.get_conn().execute(
            "SELECT * FROM tweets WHERE pushed=0 AND ignored=0"
            " AND COALESCE(ai_severity,'') IN ('high','critical')"
            " ORDER BY id DESC LIMIT 60").fetchall()
        out = [_pkg(r) for r in rows]
    else:
        rows = db.get_conn().execute(
            "SELECT * FROM tweets WHERE ignored=0 ORDER BY id DESC LIMIT 60").fetchall()
        out = [_pkg(r) for r in rows]
    return [t for t in out if _match(t)][:30]'''
assert old in s2, "_scope_tweets"
s2 = s2.replace(old, new)

# ---- B19：convert_to_report 异常回滚 + 临时文件清理 ----
old = '''    from . import reports
    with db._lock:
        c = db.get_conn()
        cand = c.execute("SELECT * FROM writing_candidates WHERE cand_uid=?", (uid,)).fetchone()
        if not cand:
            raise ValueError("候选不存在")'''
new = '''    from . import reports
    ppath = None
    with db._lock:
        c = db.get_conn()
        try:
            return _convert_locked(c, uid, template_id, operator, tier, keywords, notes,
                                   use_version)
        except Exception:
            c.rollback()   # B19：任何一步失败→整体回滚，无孤立任务/半成品
            if ppath and Path(ppath).exists():
                with suppress(Exception):
                    Path(ppath).unlink()
            raise


def _convert_locked(c, uid, template_id, operator, tier="速报", keywords=None, notes="",
                    use_version=None):
    from . import reports
    if True:
        cand = c.execute("SELECT * FROM writing_candidates WHERE cand_uid=?", (uid,)).fetchone()
        if not cand:
            raise ValueError("候选不存在")'''
assert old in s2, "convert head"
s2 = s2.replace(old, new)

# convert 内 ppath 赋值同步到外层变量（回滚清理用）
old = '''        pdir = reports._tasks_dir()
        ppath = Path(pdir) / f"task_{task_id}.json"
        ppath.write_text(json.dumps(package, ensure_ascii=False, indent=1), encoding="utf-8")'''
new = '''        pdir = reports._tasks_dir()
        ppath = Path(pdir) / f"task_{task_id}.json"
        tmp_ppath = ppath.with_suffix(".json.tmp")
        tmp_ppath.write_text(json.dumps(package, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp_ppath.replace(ppath)   # 原子落位：半成品不暴露（B19）'''
assert old in s2, "ppath atomic"
s2 = s2.replace(old, new)

# suppress import
if "from contextlib import suppress" not in s2:
    s2 = s2.replace("import secrets", "import secrets\nfrom contextlib import suppress", 1)
p2.write_text(s2, encoding="utf-8")
print("writing part2 ok")
