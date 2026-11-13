# -*- coding: utf-8 -*-
"""报告编写工作台：待编写线索候选 / 多模板管理 / AI 选题推荐 / 候选转报告。

设计要点（详见 docs/worklogs/collaborative-writing/findings.md）：
  - 候选去重：dedup_key = sha1(规范化标题 + 角度)，活跃状态命中即合并（tweet_ids 并集、has_update 标记），
    不重复建条；AI 重评不覆盖人工标题/优先级/决定。
  - 转报告单事务：report_tasks + 报告文档 + 模板版本引用 + 来源快照 + report_refs 同一 SQL 事务，
    重复点击/并发幂等（锁内二次状态检查）。
  - 模板变量白名单受限替换，禁止 eval；缺失变量明确提示。
  - 模板发布版本不可变；报告固定发布版本，模板后续变化不改写既有报告。
"""
import hashlib
import json
import re
import secrets
import threading
from pathlib import Path

from . import db
from .config import load

ACTIVE_CAND_STATUS = ("待处理", "写作中", "暂缓")
CAND_STATUSES = ("待处理", "写作中", "已转报告", "暂缓", "不采用")
DECISIONS = ("采纳", "暂缓", "不采用", "")

# AI 推荐任务的后台处理线程（单线程串行，避免并发打模型）
_recommend_lock = threading.Lock()
_recommend_thread = None


def _uid(prefix: str) -> str:
    return prefix + secrets.token_hex(6)


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def _j(v):
    return json.dumps(v, ensure_ascii=False)


def _jl(s, default):
    try:
        v = json.loads(s) if s else default
        return v if isinstance(v, (list, dict)) else default
    except Exception:
        return default


def _log_rev(cand_uid, action, operator, detail=""):
    db.get_conn().execute(
        "INSERT INTO candidate_revisions (cand_uid, action, operator, detail, created_at)"
        " VALUES (?,?,?,?,?)", (cand_uid, action, operator, detail[:500], db.now()))


# ============================================================
# 候选
# ============================================================

def create_candidate(title, angle="", origin="", tweet_ids=None, event_uid="",
                     source_type="manual", recommend_reason="", ai_suggestion="",
                     suggested_priority=None, suggested_template_id=None,
                     materials=None, operator="system", priority=3, owner="",
                     human_decision="", decision_note="", dedup=True, ai_run=""):
    """新建候选。dedup 命中活跃候选时合并并返回 (uid, merged=True)。"""
    title = (title or "").strip()
    if not title:
        raise ValueError("标题不能为空")
    if source_type not in ("manual", "tweet", "event", "ai", "batch"):
        source_type = "manual"
    ids = sorted({int(i) for i in (tweet_ids or []) if str(i).strip().lstrip("-").isdigit()})[:50]
    key = hashlib.sha1((_norm(title) + "\x00" + _norm(angle)).encode("utf-8")).hexdigest()[:16]
    with db._lock:
        c = db.get_conn()
        if dedup and key:
            row = c.execute(
                "SELECT * FROM writing_candidates WHERE dedup_key=?"
                " AND status IN ('待处理','写作中','暂缓') ORDER BY id DESC LIMIT 1", (key,)).fetchone()
            if row:
                merged = _jl(row["tweet_ids"], [])
                add = [i for i in ids if i not in merged]
                if add:
                    c.execute("UPDATE writing_candidates SET tweet_ids=?, has_update=1, version=version+1,"
                              " updated_at=? WHERE cand_uid=?",
                              (_j(sorted(merged + add)), db.now(), row["cand_uid"]))
                detail = "去重合并" + (f"（新增情报 {len(add)} 条）" if add else "")
                _log_rev(row["cand_uid"], "dedup_merge", operator, detail)
                c.commit()
                return row["cand_uid"], True
        uid = _uid("cand_")
        c.execute(
            "INSERT INTO writing_candidates (cand_uid, source_type, title, angle, origin, tweet_ids,"
            " event_uid, recommend_reason, ai_suggestion, human_decision, decision_note, owner,"
            " priority, suggested_priority, suggested_template_id, materials_todo, status,"
            " dedup_key, last_ai_run, created_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'待处理',?,?,?,?,?)",
            (uid, source_type, title, angle or "", origin or "", _j(ids), event_uid or "",
             recommend_reason or "", ai_suggestion or "", human_decision, decision_note or "",
             owner or "", int(priority) if priority else 3,
             suggested_priority, suggested_template_id, _j(materials or []), key,
             ai_run or "", operator, db.now(), db.now()))
        _log_rev(uid, "create", operator, f"来源 {source_type}")
        c.commit()
        return uid, False


def get_candidate(uid):
    r = db.get_conn().execute("SELECT * FROM writing_candidates WHERE cand_uid=?", (uid,)).fetchone()
    return _norm_cand(r) if r else None


def _norm_cand(r):
    d = dict(r)
    for k in ("tweet_ids", "materials_todo", "ai_suggestion", "evidence_snapshot"):
        d[k] = _jl(d.get(k), [] if k != "evidence_snapshot" else "")
    d["has_update"] = int(d.get("has_update") or 0)
    return d


def list_candidates(status="", q="", source_type="", limit=50, offset=0):
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if source_type:
        where.append("source_type=?")
        args.append(source_type)
    if q:
        where.append("(title LIKE ? OR angle LIKE ? OR owner LIKE ?)")
        args += [f"%{q}%"] * 3
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.get_conn().execute(
        f"SELECT COUNT(*) n FROM writing_candidates{w}", args).fetchone()["n"]
    rows = db.get_conn().execute(
        f"SELECT * FROM writing_candidates{w} ORDER BY priority ASC, id DESC LIMIT ? OFFSET ?",
        args + [int(limit), int(offset)]).fetchall()
    return total, [_norm_cand(r) for r in rows]


def update_candidate(uid, fields, operator, expected_version=None):
    """人工编辑：标题/角度/来源/负责人/优先级/建议模板/备注/待补材料/状态。乐观锁。"""
    allowed = {"title", "angle", "origin", "owner", "priority", "suggested_template_id",
               "materials_todo", "status", "human_decision", "decision_note"}
    sets, args = [], []
    for k, v in (fields or {}).items():
        if k not in allowed:
            continue
        if k == "status" and v not in CAND_STATUSES:
            raise ValueError(f"非法状态：{v}")
        if k == "human_decision" and v not in DECISIONS:
            raise ValueError(f"非法决定：{v}")
        if k == "priority":
            v = max(1, min(5, int(v)))
        if k in ("materials_todo",) and not isinstance(v, list):
            raise ValueError("materials_todo 须为数组")
        sets.append(f"{k}=?")
        args.append(_j(v) if isinstance(v, list) else v)
    if not sets:
        return False, "无有效字段"
    with db._lock:
        c = db.get_conn()
        row = c.execute("SELECT version, status FROM writing_candidates WHERE cand_uid=?", (uid,)).fetchone()
        if not row:
            return False, "候选不存在"
        if expected_version is not None and int(expected_version) != int(row["version"]):
            return False, f"version_mismatch 当前 v{row['version']}"
        c.execute(f"UPDATE writing_candidates SET {', '.join(sets)}, version=version+1, updated_at=?"
                  f" WHERE cand_uid=?", args + [db.now(), uid])
        _log_rev(uid, "update", operator, ",".join(sets))
        c.commit()
    return True, "ok"


def ai_update_candidate(uid, suggestion, operator="ai"):
    """AI 重评：不覆盖人工标题/优先级/决定；已有 ai_suggestion 合并；标记线索有更新。"""
    with db._lock:
        c = db.get_conn()
        row = c.execute("SELECT * FROM writing_candidates WHERE cand_uid=?", (uid,)).fetchone()
        if not row:
            return False, "候选不存在"
        c.execute("UPDATE writing_candidates SET ai_suggestion=?, suggested_priority=?,"
                  " suggested_template_id=COALESCE(?, suggested_template_id),"
                  " last_ai_run=?, has_update=1, version=version+1, updated_at=?"
                  " WHERE cand_uid=?",
                  (_j(suggestion.get("suggestion") or {}), suggestion.get("priority"),
                   suggestion.get("template_id"), db.now(), db.now(), uid))
        _log_rev(uid, "ai_update", operator, str(suggestion.get("note") or "")[:300])
        c.commit()
    return True, "ok"


def record_candidate_status_change_by_doc(doc_uid, status, operator):
    """文档状态联动候选（进入编辑器/审阅时把候选置为写作中）。"""
    with db._lock:
        c = db.get_conn()
        row = c.execute("SELECT status FROM writing_candidates WHERE doc_uid=?", (doc_uid,)).fetchone()
        if not row or row["status"] not in ("待处理", "暂缓"):
            return
        c.execute("UPDATE writing_candidates SET status=?, version=version+1, updated_at=?"
                  " WHERE doc_uid=?", (status, db.now(), doc_uid))
        _log_rev_by_doc(doc_uid, "candidate_status", operator, status)
        c.commit()


def _log_rev_by_doc(doc_uid, action, operator, detail):
    r = db.get_conn().execute("SELECT cand_uid FROM writing_candidates WHERE doc_uid=?", (doc_uid,)).fetchone()
    if r:
        _log_rev(r["cand_uid"], action, operator, detail)


def candidate_revisions(uid, limit=100):
    return [dict(r) for r in db.get_conn().execute(
        "SELECT * FROM candidate_revisions WHERE cand_uid=? ORDER BY id DESC LIMIT ?",
        (uid, int(limit))).fetchall()]


def delete_candidate(uid, operator):
    with db._lock:
        c = db.get_conn()
        row = c.execute("SELECT status, doc_uid FROM writing_candidates WHERE cand_uid=?", (uid,)).fetchone()
        if not row:
            return False, "候选不存在"
        if row["status"] not in ("待处理", "暂缓", "不采用"):
            return False, "当前状态不可删除（已转报告/写作中）"
        if row["doc_uid"]:
            return False, "已关联文档不可删除"
        c.execute("DELETE FROM writing_candidates WHERE cand_uid=?", (uid,))
        _log_rev(uid, "delete", operator, "")
        c.execute("INSERT INTO candidate_revisions (cand_uid, action, operator, detail, created_at)"
                  " VALUES (?,?,?,?,?)", (uid, "delete", operator, "", db.now()))
        c.commit()
    return True, "ok"


# ============================================================
# 批量 / 事件入口
# ============================================================

def candidates_from_tweets(tweet_ids, operator, title="", angle=""):
    """批量情报合并为写作候选（单条=单候选）。返回创建/合并结果列表。"""
    from . import reports
    ids = [int(i) for i in (tweet_ids or [])][:50]
    if not ids:
        raise ValueError("未选择情报")
    out = []
    if len(ids) == 1 and not angle:
        t = db.get_tweet(ids[0])
        title = title or (t["title_zh"] or (t["text"] or "")[:60] if t else f"情报 #{ids[0]}")
    if len(ids) > 1:
        title = title or "多源合并线索"
    uid, merged = create_candidate(title=title, angle=angle, tweet_ids=ids,
                                   source_type="batch" if len(ids) > 1 else "tweet",
                                   origin="情报批量选择", operator=operator)
    out.append({"cand_uid": uid, "merged": merged, "tweet_ids": ids})
    return out


def candidate_from_event(event_uid, operator, title="", angle=""):
    ev = db.get_event(event_uid)
    if not ev:
        raise ValueError("事件不存在")
    evs = db.event_evidence(event_uid)
    ids = [e["tweet_id"] for e in evs]
    title = title or ev["title"]
    uid, merged = create_candidate(title=title, angle=angle or "事件综合分析",
                                   tweet_ids=ids, event_uid=event_uid,
                                   source_type="event", origin=f"事件 {event_uid}",
                                   operator=operator)
    return {"cand_uid": uid, "merged": merged, "tweets": len(ids)}


# ============================================================
# AI 推荐
# ============================================================

def trigger_recommend(scope_type="backlog", scope_ref="", operator="system"):
    if scope_type not in ("manual", "tweet", "event", "backlog"):
        raise ValueError("scope_type 非法")
    with db._lock:
        cur = db.get_conn().execute(
            "INSERT INTO ai_recommend_tasks (scope_type, scope_ref, status, created_by, created_at, updated_at)"
            " VALUES (?,?, 'queued', ?, ?, ?)",
            (scope_type, scope_ref or "", operator, db.now(), db.now()))
        tid = cur.lastrowid
        db.get_conn().commit()
    _ensure_recommend_thread()
    return tid


def list_recommend_tasks(limit=20):
    return [dict(r) for r in db.get_conn().execute(
        "SELECT * FROM ai_recommend_tasks ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()]


def retry_recommend_task(task_id):
    with db._lock:
        cur = db.get_conn().execute(
            "UPDATE ai_recommend_tasks SET status='queued', error='', updated_at=? WHERE id=?",
            (db.now(), int(task_id)))
        db.get_conn().commit()
    if cur.rowcount > 0:
        _ensure_recommend_thread()   # worker 空转后已退出，重试需重新拉起
    return cur.rowcount > 0


def _ensure_recommend_thread():
    global _recommend_thread
    with _recommend_lock:
        if _recommend_thread is None or not _recommend_thread.is_alive():
            _recommend_thread = threading.Thread(
                target=_recommend_worker, name="ai-recommend", daemon=True)
            _recommend_thread.start()


def _recommend_worker():
    while True:
        task = None
        with db._lock:
            row = db.get_conn().execute(
                "SELECT * FROM ai_recommend_tasks WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row:
                db.get_conn().execute(
                    "UPDATE ai_recommend_tasks SET status='running', attempts=attempts+1, updated_at=?"
                    " WHERE id=?", (db.now(), row["id"]))
                db.get_conn().commit()
                task = dict(row)
        if task is None:
            break
        try:
            n = _run_recommend(task)
            with db._lock:
                db.get_conn().execute(
                    "UPDATE ai_recommend_tasks SET status='done', result=?, error='', updated_at=? WHERE id=?",
                    (_j({"items": n}), db.now(), task["id"]))
                db.get_conn().commit()
        except Exception as e:
            with db._lock:
                db.get_conn().execute(
                    "UPDATE ai_recommend_tasks SET status='failed', error=?, updated_at=? WHERE id=?",
                    (str(e)[:300], db.now(), task["id"]))
                db.get_conn().commit()


def _scope_tweets(scope_type, scope_ref):
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
    return []


def _run_recommend(task):
    """执行一次 AI 推荐任务，返回新建/合并的候选数。"""
    from .ai import _chat, _cfg, _parse_json_loose
    from . import reports
    cfg = _cfg()
    tweets = _scope_tweets(task["scope_type"], task["scope_ref"])
    ctx = json.dumps(tweets, ensure_ascii=False)[:12000]
    tpl_names = [r["name"] for r in db.get_conn().execute(
        "SELECT name FROM doc_templates WHERE status='启用'").fetchall()]
    sys_prompt = (
        "你是威胁情报编辑助手。根据给定情报素材推荐值得写成专题报告的选题。"
        "输出严格 JSON：{\"items\":[{\"title\":\"报告标题(中文,具体,含主体)\",\"angle\":\"写作角度\","
        "\"reason\":\"推荐理由(基于素材)\",\"priority\":1-5,\"template\":\"建议模板名或空\","
        "\"materials\":[\"待补材料\"],\"basis\":[\"依据的素材要点\"],\"tweet_ids\":[素材内出现的推文id数字]}]}"
        "。注意：区分写作价值与事实可信度；无依据内容标待确认；不编造事实；"
        "如素材不足以成稿，返回空数组。")
    raw = _chat(cfg, [{"role": "system", "content": sys_prompt},
                      {"role": "user", "content": f"可用模板：{tpl_names}\n情报素材：\n{ctx}"}],
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
    return n


# ============================================================
# 模板
# ============================================================

def create_template(name, description="", variables=None, agent_notes="", check_rules=None,
                    body_md="", operator="system"):
    name = (name or "").strip()
    if not name:
        raise ValueError("模板名不能为空")
    _validate_variables(variables)
    from . import doccollab
    with db._lock:
        c = db.get_conn()
        if c.execute("SELECT 1 FROM doc_templates WHERE name=?", (name,)).fetchone():
            raise ValueError("同名模板已存在")
        doc_uid = _uid("doc_")
        c.execute(
            "INSERT INTO doc_documents (doc_uid, title, kind, initial_md, default_permission,"
            " created_by, created_at, updated_at) VALUES (?,?, 'template', ?, 'read', ?, ?, ?)",
            (doc_uid, f"模板：{name}", body_md or "", operator, db.now(), db.now()))
        c.execute(
            "INSERT INTO doc_templates (name, description, variables, agent_notes, check_rules,"
            " body_doc_uid, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, description or "", _j(variables or []), agent_notes or "", _j(check_rules or []),
             doc_uid, operator, db.now(), db.now()))
        tid = c.execute("SELECT id FROM doc_templates WHERE name=?", (name,)).fetchone()["id"]
        if body_md:
            c.execute("UPDATE doc_documents SET initialized=1 WHERE doc_uid=?", (doc_uid,))
            c.execute("INSERT INTO doc_revisions (doc_uid, rev, title, body_md, revision_kind,"
                      " content_hash, created_by, created_at) VALUES (?,1,?,?,'init',?,?,?)",
                      (doc_uid, f"模板：{name}", body_md, _hash_md(body_md), operator, db.now()))
        c.commit()
    doccollab.invalidate_room(doc_uid)
    return {"id": tid, "doc_uid": doc_uid}


def _validate_variables(variables):
    for v in (variables or []):
        if not isinstance(v, dict) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(v.get("name", ""))):
            raise ValueError("变量名须为合法标识符且提供 name")


def _hash_md(s):
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:32]


def get_template(tid):
    r = db.get_conn().execute("SELECT * FROM doc_templates WHERE id=?", (int(tid),)).fetchone()
    return _norm_tpl(r) if r else None


def _norm_tpl(r):
    d = dict(r)
    d["variables"] = _jl(d.get("variables"), [])
    d["check_rules"] = _jl(d.get("check_rules"), [])
    return d


def list_templates(include_disabled=False):
    w = "" if include_disabled else " WHERE status='启用'"
    return [_norm_tpl(r) for r in db.get_conn().execute(
        f"SELECT * FROM doc_templates{w} ORDER BY id").fetchall()]


def update_template(tid, fields, operator):
    allowed = {"name", "description", "variables", "agent_notes", "check_rules"}
    sets, args = [], []
    for k, v in (fields or {}).items():
        if k not in allowed:
            continue
        if k == "variables":
            _validate_variables(v)
            v = _j(v)
        if k == "check_rules":
            v = _j(v)
        sets.append(f"{k}=?")
        args.append(v)
    if not sets:
        return False
    with db._lock:
        db.get_conn().execute(
            f"UPDATE doc_templates SET {', '.join(sets)}, updated_at=? WHERE id=?",
            args + [db.now(), int(tid)])
        db.get_conn().commit()
    return True


def publish_template(tid, changelog, operator, body_md=None):
    """发布：把当前正文快照为不可变版本。body_md=None 时由协作内核取当前文本。"""
    from . import doccollab
    tpl = get_template(tid)
    if not tpl:
        raise ValueError("模板不存在")
    if body_md is None:
        body_md = doccollab.current_text(tpl["body_doc_uid"]) or ""
    if not (body_md or "").strip():
        raise ValueError("模板正文为空，不能发布")
    with db._lock:
        c = db.get_conn()
        nv = (tpl["current_version"] or 0) + 1
        c.execute(
            "INSERT INTO template_versions (template_id, version, body_md, variables, agent_notes,"
            " check_rules, changelog, published_by, published_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, nv, body_md,
             _j(tpl["variables"]) if isinstance(tpl["variables"], list) else tpl["variables"],
             tpl["agent_notes"],
             _j(tpl["check_rules"]) if isinstance(tpl["check_rules"], list) else tpl["check_rules"],
             changelog or "", operator, db.now()))
        c.execute("UPDATE doc_templates SET current_version=?, updated_at=? WHERE id=?",
                  (nv, db.now(), tid))
        c.commit()
    return nv


def duplicate_template(tid, operator, new_name=""):
    tpl = get_template(tid)
    if not tpl:
        raise ValueError("模板不存在")
    from . import doccollab
    body = doccollab.current_text(tpl["body_doc_uid"]) or ""
    r = create_template(new_name or f"{tpl['name']} 副本", tpl["description"], tpl["variables"],
                        tpl["agent_notes"], tpl["check_rules"], body, operator)
    return r


def set_template_status(tid, status, operator):
    if status not in ("启用", "停用"):
        raise ValueError("非法状态")
    with db._lock:
        db.get_conn().execute("UPDATE doc_templates SET status=?, updated_at=? WHERE id=?",
                              (status, db.now(), int(tid)))
        db.get_conn().commit()
    return True


def template_versions(tid):
    return [dict(r) for r in db.get_conn().execute(
        "SELECT id, template_id, version, body_md, changelog, published_by, published_at"
        " FROM template_versions WHERE template_id=? ORDER BY version DESC", (int(tid),)).fetchall()]


VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def apply_variables(body_md, variables_def, values):
    """白名单受限替换：仅替换已定义变量；未定义占位与缺失值明确返回。禁止任何代码执行。"""
    defined = {v["name"]: v for v in (variables_def or []) if isinstance(v, dict) and v.get("name")}
    missing, unknown = [], []
    used = set()

    def _sub(m):
        name = m.group(1)
        if name not in defined:
            unknown.append(name)
            return m.group(0)
        used.add(name)
        if values.get(name) in (None, ""):
            missing.append(name)
            return m.group(0)
        return str(values[name])

    filled = VAR_RE.sub(_sub, body_md or "")
    unused = [n for n in defined if n not in used and defined[n].get("required")]
    missing = sorted(set(missing) | set(unused))
    return filled, sorted(set(missing)), sorted(set(unknown))


def template_preview(tid, values=None, candidate_uid=""):
    tpl = get_template(tid)
    if not tpl:
        raise ValueError("模板不存在")
    values = dict(values or {})
    if candidate_uid:
        cand = get_candidate(candidate_uid)
        if cand:
            values.setdefault("title", cand["title"])
            values.setdefault("angle", cand["angle"])
    filled, missing, unknown = apply_variables(_template_body(tpl), tpl["variables"], values)
    return {"filled": filled, "missing": missing, "unknown": unknown}


def _template_body(tpl):
    from . import doccollab
    return doccollab.current_text(tpl["body_doc_uid"]) or ""


# ============================================================
# 候选转报告（单事务 + 幂等）
# ============================================================

def convert_to_report(uid, template_id, operator, tier="速报", keywords=None, notes="",
                      use_version=None):
    """候选 → 报告任务 + 协作文档 + 模板版本引用 + 来源快照。重复调用幂等。"""
    from . import reports
    with db._lock:
        c = db.get_conn()
        cand = c.execute("SELECT * FROM writing_candidates WHERE cand_uid=?", (uid,)).fetchone()
        if not cand:
            raise ValueError("候选不存在")
        if cand["status"] == "已转报告" and cand["doc_uid"]:
            doc = c.execute("SELECT doc_uid, report_task_id FROM doc_documents"
                            " WHERE doc_uid=?", (cand["doc_uid"],)).fetchone()
            return {"cand_uid": uid, "doc_uid": cand["doc_uid"],
                    "report_task_id": cand["report_task_id"], "existed": True}
        if cand["status"] == "不采用":
            raise ValueError("候选已标记不采用")
        tpl = None
        tver = None
        if template_id:
            tpl = c.execute("SELECT * FROM doc_templates WHERE id=?", (int(template_id),)).fetchone()
            if not tpl:
                raise ValueError("模板不存在")
            if not tpl["current_version"]:
                raise ValueError("模板尚未发布任何版本")
            ver = int(use_version) if use_version else int(tpl["current_version"])
            tver = c.execute("SELECT * FROM template_versions WHERE template_id=? AND version=?",
                             (tpl["id"], ver)).fetchone()
            if not tver:
                raise ValueError("模板版本不存在")
        ids = _jl(cand["tweet_ids"], [])
        tweets = []
        for tid in ids:
            t = db.get_tweet(tid)
            if t:
                tweets.append(reports._tweet_package(t))
        partial_n = sum(1 for x in tweets if x["ai"]["integrity"] in ("partial", "invalid"))
        # 1) 报告任务行
        cur = c.execute(
            "INSERT INTO report_tasks (title, keywords, tier, notes, tweet_ids, auto_push,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (cand["title"], _j(keywords or []), tier if tier in ("速报", "完整") else "速报",
             notes or cand["angle"], _j(ids), 0, db.now(), db.now()))
        task_id = cur.lastrowid
        package = {"title": cand["title"], "keywords": keywords or [], "tier": tier,
                   "notes": notes or cand["angle"], "tweets": tweets,
                   "disclaimer": reports.DISCLAIMER,
                   "integrity_summary": {
                       "total": len(tweets),
                       "partial_or_invalid": partial_n,
                       "note": ("包含部分/无效 AI 结果，不得直接作为已核验结论引用" if partial_n
                                else "全部为完整研判")}}
        package["task_id"] = task_id
        pdir = reports._tasks_dir()
        ppath = Path(pdir) / f"task_{task_id}.json"
        ppath.write_text(json.dumps(package, ensure_ascii=False, indent=1), encoding="utf-8")
        c.execute("UPDATE report_tasks SET package_path=? WHERE id=?", (str(ppath), task_id))
        # 2) 协作文档（模板发布版本固定 + 用候选信息实例化白名单变量；缺失保留占位）
        doc_uid = _uid("doc_")
        init_md = tver["body_md"] if tver else ""
        if tver and tpl:
            filled, _missing, _unknown = apply_variables(
                tver["body_md"], _jl(tpl["variables"], []),
                {"title": cand["title"], "angle": cand["angle"] or "",
                 "origin": cand["origin"] or "", "owner": cand["owner"] or ""})
            init_md = filled
        c.execute(
            "INSERT INTO doc_documents (doc_uid, title, kind, status, template_id, template_version,"
            " initial_md, initialized, candidate_uid, report_task_id, source_snapshot,"
            " created_by, created_at, updated_at)"
            " VALUES (?,?,'report','编辑中',?,?,?,?,?,?,?,?,?,?)",
            (doc_uid, cand["title"], tpl["id"] if tpl else None,
             tver["version"] if tver else None, init_md, 1 if init_md else 0,
             uid, task_id, _j({"tweets": tweets, "candidate": _norm_cand(cand),
                               "converted_at": db.now()}),
             operator, db.now(), db.now()))
        if init_md:
            c.execute("INSERT INTO doc_revisions (doc_uid, rev, title, body_md, revision_kind,"
                      " content_hash, created_by, created_at) VALUES (?,1,?,?,'init',?,?,?)",
                      (doc_uid, cand["title"], init_md, _hash_md(init_md), operator, db.now()))
        c.execute("INSERT OR IGNORE INTO doc_acl (doc_uid, principal, permission, created_at)"
                  " VALUES (?,?, 'write', ?)", (doc_uid, operator, db.now()))
        # 3) 候选联动 + 引用保护
        c.execute("UPDATE writing_candidates SET status='已转报告', doc_uid=?, report_task_id=?,"
                  " version=version+1, updated_at=? WHERE cand_uid=?", (doc_uid, task_id, db.now(), uid))
        _log_rev(uid, "convert", operator, f"报告任务 #{task_id} · 文档 {doc_uid}")
        for tid in ids:
            c.execute("INSERT OR IGNORE INTO report_refs (report_id, tweet_id, created_at)"
                      " VALUES (?,?,?)", (task_id, tid, db.now()))
        c.commit()
    return {"cand_uid": uid, "doc_uid": doc_uid, "report_task_id": task_id, "existed": False}


# ============================================================
# 首页汇总（服务端全量聚合，非列表截断）
# ============================================================

def summary():
    c = db.get_conn()
    return {
        "candidates_active": c.execute(
            "SELECT COUNT(*) n FROM writing_candidates WHERE status IN ('待处理','写作中','暂缓')").fetchone()["n"],
        "candidates_pending": c.execute(
            "SELECT COUNT(*) n FROM writing_candidates WHERE status='待处理'").fetchone()["n"],
        "candidates_has_update": c.execute(
            "SELECT COUNT(*) n FROM writing_candidates WHERE has_update=1"
            " AND status IN ('待处理','写作中','暂缓')").fetchone()["n"],
        "docs_editing": c.execute(
            "SELECT COUNT(*) n FROM doc_documents WHERE kind='report' AND status='编辑中'").fetchone()["n"],
        "docs_review": c.execute(
            "SELECT COUNT(*) n FROM doc_documents WHERE kind='report' AND status='待审阅'").fetchone()["n"],
        "docs_total": c.execute(
            "SELECT COUNT(*) n FROM doc_documents WHERE kind='report'").fetchone()["n"],
        "templates_active": c.execute(
            "SELECT COUNT(*) n FROM doc_templates WHERE status='启用'").fetchone()["n"],
        "recommend_running": c.execute(
            "SELECT COUNT(*) n FROM ai_recommend_tasks WHERE status IN ('queued','running')").fetchone()["n"],
    }
