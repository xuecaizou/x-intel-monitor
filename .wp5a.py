# -*- coding: utf-8 -*-
"""WP5 补丁：提示词驱动候选 + B13/B14/B15/B16/B18/B19/B22（一次性脚本）"""
from pathlib import Path

# ============ 1) db.py：schema 迁移（幂等） ============
p = Path("app/db.py")
s = p.read_text(encoding="utf-8")
anchor = '''        # 2026-11-16 round2 迁移（幂等）：更新 sha（前端精确确认）+ 建议幂等键唯一'''
assert anchor in s
add = '''        # 2026-11-16 round2 需求 E：推荐任务持久化用户提示词/通道/结果明细
        try:
            get_conn().execute("ALTER TABLE ai_recommend_tasks ADD COLUMN prompt TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            get_conn().execute("ALTER TABLE ai_recommend_tasks ADD COLUMN channel_id TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            get_conn().execute("ALTER TABLE ai_recommend_tasks ADD COLUMN model TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            get_conn().execute("ALTER TABLE ai_recommend_tasks ADD COLUMN result_json TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            get_conn().execute("ALTER TABLE ai_recommend_tasks ADD COLUMN filters TEXT DEFAULT '{}'")
        except Exception:
            pass
        try:
            get_conn().execute("ALTER TABLE writing_candidates ADD COLUMN rec_task_id INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            get_conn().execute("ALTER TABLE writing_candidates ADD COLUMN source_key TEXT DEFAULT ''")
        except Exception:
            pass
''' + anchor
s = s.replace(anchor, add, 1)
p.write_text(s, encoding="utf-8")
print("db migration ok")

# ============ 2) writing.py 重构 ============
p2 = Path("app/writing.py")
s2 = p2.read_text(encoding="utf-8")

# 2a) create_candidate：B15 全状态身份 + B16 dedup_scope
old = '''def create_candidate(title, angle="", origin="", tweet_ids=None, event_uid="",
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
    key = hashlib.sha1((_norm(title) + "\\x00" + _norm(angle)).encode("utf-8")).hexdigest()[:16]
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
                return row["cand_uid"], True'''
new = '''def create_candidate(title, angle="", origin="", tweet_ids=None, event_uid="",
                     source_type="manual", recommend_reason="", ai_suggestion="",
                     suggested_priority=None, suggested_template_id=None,
                     materials=None, operator="system", priority=3, owner="",
                     human_decision="", decision_note="", dedup=True, ai_run="",
                     dedup_scope="", rec_task_id=0):
    """新建候选。dedup 命中活跃候选时合并并返回 (uid, merged=True)。

    2026-11-16 round2（B15/B16）：
      - dedup_scope：批次类来源传情报集合身份（如排序后的 tweet_ids）——
        不同情报批次同默认标题**不再自动合并**（B16；同标题仅作相似提示）；
      - dedup 命中"不采用"：人工决定保留，**不重建**待处理记录（返回 suppressed=True，
        B15）；命中"已转报告"：允许新建（新证据的新选题），留痕关联。
    """
    title = (title or "").strip()
    if not title:
        raise ValueError("标题不能为空")
    if source_type not in ("manual", "tweet", "event", "ai", "batch"):
        source_type = "manual"
    ids = sorted({int(i) for i in (tweet_ids or []) if str(i).strip().lstrip("-").isdigit()})[:50]
    scope_part = _norm(dedup_scope) if dedup_scope else ""
    key = hashlib.sha1((_norm(title) + "\\x00" + _norm(angle) + "\\x00" + scope_part)
                       .encode("utf-8")).hexdigest()[:16]
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
            # B15：人工"不采用"的稳定身份——重复推荐不复活、不新建
            row_rej = c.execute(
                "SELECT cand_uid, status FROM writing_candidates WHERE dedup_key=?"
                " AND status='不采用' ORDER BY id DESC LIMIT 1", (key,)).fetchone()
            if row_rej:
                _log_rev(row_rej["cand_uid"], "recommend_suppressed", operator,
                         f"重复推荐被人工决定（不采用）抑制；任务 #{rec_task_id or '-'}")
                c.commit()
                return row_rej["cand_uid"], False'''
assert old in s2, "create_candidate"
s2 = s2.replace(old, new)

# 2b) 新建 INSERT 带新列
old = """            (uid, source_type, title, angle or "", origin or "", _j(ids), event_uid or "",
             recommend_reason or "", ai_suggestion or "", human_decision, decision_note or "",
             owner or "", int(priority) if priority else 3,
             suggested_priority, suggested_template_id, _j(materials or []), key,
             ai_run or "", operator, db.now(), db.now()))"""
new = """            (uid, source_type, title, angle or "", origin or "", _j(ids), event_uid or "",
             recommend_reason or "", ai_suggestion or "", human_decision, decision_note or "",
             owner or "", int(priority) if priority else 3,
             suggested_priority, suggested_template_id, _j(materials or []), key,
             ai_run or "", operator, db.now(), db.now(), int(rec_task_id or 0), key)"""
assert old in s2, "insert cols"
s2 = s2.replace(old, new)
old = """            "INSERT INTO writing_candidates (cand_uid, source_type, title, angle, origin, tweet_ids,"
            " event_uid, recommend_reason, ai_suggestion, human_decision, decision_note, owner,"
            " priority, suggested_priority, suggested_template_id, materials_todo, status,"
            " dedup_key, last_ai_run, created_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'待处理',?,?,?,?,?)","""
new = """            "INSERT INTO writing_candidates (cand_uid, source_type, title, angle, origin, tweet_ids,"
            " event_uid, recommend_reason, ai_suggestion, human_decision, decision_note, owner,"
            " priority, suggested_priority, suggested_template_id, materials_todo, status,"
            " dedup_key, last_ai_run, created_by, created_at, updated_at, rec_task_id, source_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'待处理',?,?,?,?,?,?,?,?)","""
assert old in s2, "insert sql"
s2 = s2.replace(old, new)

# 2c) trigger_recommend：持久化 prompt/channel/model/filters
old = """def trigger_recommend(scope_type="backlog", scope_ref="", operator="system"):
    with db._lock:
        cur = db.get_conn().execute(
            "INSERT INTO ai_recommend_tasks (scope_type, scope_ref, status, created_by, created_at, updated_at)"
            " VALUES (?,?, 'queued', ?, ?, ?)",
            (scope_type, scope_ref or "", operator, db.now(), db.now()))
        tid = cur.lastrowid
        db.get_conn().commit()
    _ensure_recommend_thread()
    return tid"""
new = """def trigger_recommend(scope_type="backlog", scope_ref="", operator="system",
                      prompt="", channel_id="", model="", filters=None):
    \"\"\"创建 AI 推荐任务（round2 需求 E1：用户提示词驱动）。

    prompt 原样持久化并真正进入模型请求；通道/模型持久化到任务（开始时快照，
    执行中改默认通道不影响本任务）；filters 为资料筛选条件 dict。\"\"\"
    from . import ai_channels as aich
    aich.migrate_legacy()
    if not (prompt or "").strip():
        raise ValueError("提示词不能为空（需求 E：用户主动输入提示词驱动推荐）")
    eff = aich.resolve_cfg(channel_id=channel_id or None, task="writing_recommend")
    with db._lock:
        cur = db.get_conn().execute(
            "INSERT INTO ai_recommend_tasks (scope_type, scope_ref, status, prompt, channel_id,"
            " model, filters, created_by, created_at, updated_at)"
            " VALUES (?,?, 'queued', ?,?,?,?,?, ?, ?)",
            (scope_type, scope_ref or "", (prompt or "").strip()[:4000],
             eff.get("channel_id") or "", (model or "").strip() or eff.get("model") or "",
             _j(filters or {}), operator, db.now(), db.now()))
        tid = cur.lastrowid
        db.get_conn().commit()
    _ensure_recommend_thread()
    return tid"""
assert old in s2, "trigger"
s2 = s2.replace(old, new)

# 2d) worker：B22 租约恢复 + 结果明细持久化
old = """def _recommend_worker():
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
            break"""
new = """def recover_recommend_tasks(stale_minutes=10):
    \"\"\"启动恢复（B22）：重启后残留 queued/running 的任务按租约回收——
    running 超过 stale_minutes 视为陈旧回 queued（幂等，不伪造结果）；
    并拉起 worker（lifespan 调用）。\"\"\"
    import time as _t
    cutoff = (db.now() and _t.time() - stale_minutes * 60)
    with db._lock:
        c = db.get_conn()
        rows = c.execute("SELECT id, updated_at FROM ai_recommend_tasks WHERE status='running'"
                         ).fetchall()
        n = 0
        for r in rows:
            try:
                ts = _t.mktime(_t.strptime(r["updated_at"], "%Y-%m-%d %H:%M:%S"))
            except Exception:
                ts = 0
            if _t.time() - ts > stale_minutes * 60:
                c.execute("UPDATE ai_recommend_tasks SET status='queued',"
                          " error='worker 重启回收（租约过期）', updated_at=? WHERE id=?",
                          (db.now(), r["id"]))
                n += 1
        c.commit()
    if n or _has_queued():
        _ensure_recommend_thread()
    return n


def _has_queued():
    return bool(db.get_conn().execute(
        "SELECT 1 FROM ai_recommend_tasks WHERE status='queued' LIMIT 1").fetchone())


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
            break"""
assert old in s2, "worker"
s2 = s2.replace(old, new)

# 2e) worker 完成分支：结果明细持久化
old = """        try:
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
                db.get_conn().commit()"""
new = """        try:
            n, detail = _run_recommend(task)
            with db._lock:
                db.get_conn().execute(
                    "UPDATE ai_recommend_tasks SET status='done', result=?, result_json=?, error='',"
                    " updated_at=? WHERE id=?",
                    (_j({"items": n}), _j(detail), db.now(), task["id"]))
                db.get_conn().commit()
        except Exception as e:
            with db._lock:
                db.get_conn().execute(
                    "UPDATE ai_recommend_tasks SET status='failed', error=?, updated_at=? WHERE id=?",
                    (str(e)[:300], db.now(), task["id"]))
                db.get_conn().commit()"""
assert old in s2, "worker done"
s2 = s2.replace(old, new)
p2.write_text(s2, encoding="utf-8")
print("writing part1 ok")
