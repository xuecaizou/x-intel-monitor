"""SQLite 存储层：tweets(含 AI 结果列) / x_accounts / push_logs / kv / fetch_logs / report_tasks"""
import datetime
import json
import os
import sqlite3
import threading
import uuid
import time

from .config import DATA_DIR

_lock = threading.Lock()
_conn = None


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_conn():
    global _conn
    if _conn is None:
        DATA_DIR.mkdir(exist_ok=True)
        _conn = sqlite3.connect(DATA_DIR / "xintel.db", check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        # WAL（第四批）：单写多读并发更稳；备份必须用 backup API（含 WAL 未检查点数据）
        try:
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
    return _conn


def init_db():
    with _lock:
        get_conn().executescript("""
        CREATE TABLE IF NOT EXISTS tweets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            screen_name TEXT NOT NULL,
            tweet_id TEXT NOT NULL,
            text TEXT DEFAULT '',
            url TEXT DEFAULT '',
            published_at TEXT DEFAULT '',
            is_retweet INTEGER DEFAULT 0,
            is_reply INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            ai_status TEXT DEFAULT '',
            ai_error TEXT DEFAULT '',
            ai_model TEXT DEFAULT '',
            ai_severity TEXT DEFAULT '',
            ai_type TEXT DEFAULT '',
            ai_actors TEXT DEFAULT '',
            ai_iocs TEXT DEFAULT '',
            ai_assessment TEXT DEFAULT '',
            ai_recommendations TEXT DEFAULT '',
            ai_confidence INTEGER DEFAULT 0,
            ai_analyzed_at TEXT DEFAULT '',
            title_zh TEXT DEFAULT '',
            text_zh TEXT DEFAULT '',
            translated_at TEXT DEFAULT '',
            UNIQUE(screen_name, tweet_id)
        );
        CREATE INDEX IF NOT EXISTS idx_tweets_created ON tweets(created_at);
        CREATE TABLE IF NOT EXISTS x_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            screen_name TEXT UNIQUE NOT NULL,
            display TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            fetch_minutes INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS push_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            kind TEXT DEFAULT '',
            status TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            content TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS fetch_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            trigger TEXT DEFAULT 'schedule',
            accounts_due INTEGER DEFAULT 0,
            accounts_ok INTEGER DEFAULT 0,
            new INTEGER DEFAULT 0,
            ai INTEGER DEFAULT 0,
            error TEXT DEFAULT '',
            trace TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS report_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            keywords TEXT DEFAULT '[]',
            tier TEXT DEFAULT '速报',
            notes TEXT DEFAULT '',
            tweet_ids TEXT DEFAULT '[]',
            package_path TEXT,
            status TEXT DEFAULT '待写',
            assignee TEXT DEFAULT '',
            deliver_dir TEXT DEFAULT '',
            docx TEXT DEFAULT '',
            gate_exit INTEGER,
            gate_summary TEXT DEFAULT '',
            word_count INTEGER,
            ledger_total INTEGER,
            ledger_pass INTEGER,
            warnings TEXT DEFAULT '[]',
            auto_push INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS rss_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT UNIQUE NOT NULL,
            enabled INTEGER DEFAULT 1,
            fetch_minutes INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS delivery_msgs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id TEXT NOT NULL,
            channel_key TEXT NOT NULL,
            msg_type TEXT NOT NULL DEFAULT 'digest',
            report_task_id INTEGER,
            title TEXT DEFAULT '',
            state TEXT NOT NULL DEFAULT '待发送',
            attempts INTEGER DEFAULT 0,
            next_attempt_at REAL,
            lease_until REAL,
            rule_id TEXT DEFAULT '',
            rule_snapshot TEXT DEFAULT '',
            content TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_dmsg_state ON delivery_msgs(instance_id, state);
        CREATE INDEX IF NOT EXISTS idx_dmsg_report ON delivery_msgs(report_task_id);
        CREATE TABLE IF NOT EXISTS delivery_items (
            msg_id INTEGER NOT NULL,
            tweet_id INTEGER NOT NULL,
            UNIQUE(msg_id, tweet_id)
        );
        CREATE INDEX IF NOT EXISTS idx_ditems_tweet ON delivery_items(tweet_id);
        CREATE TABLE IF NOT EXISTS deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tweet_id INTEGER NOT NULL,
            instance_id TEXT NOT NULL,
            channel_key TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT '待发送',
            msg_id INTEGER,
            rule_id TEXT DEFAULT '',
            rule_snapshot TEXT DEFAULT '',
            reason TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT,
            UNIQUE(tweet_id, instance_id)
        );
        CREATE INDEX IF NOT EXISTS idx_deliv_inst ON deliveries(instance_id, state);
        CREATE INDEX IF NOT EXISTS idx_deliv_tweet ON deliveries(tweet_id);
        CREATE TABLE IF NOT EXISTS report_refs (
            report_id INTEGER NOT NULL,
            tweet_id INTEGER NOT NULL,
            created_at TEXT,
            UNIQUE(report_id, tweet_id)
        );
        CREATE INDEX IF NOT EXISTS idx_rrefs_tweet ON report_refs(tweet_id);
        CREATE TABLE IF NOT EXISTS delivery_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id INTEGER NOT NULL,
            attempt_no INTEGER NOT NULL,
            started_at TEXT, finished_at TEXT,
            result TEXT DEFAULT '',          -- success / retry / permanent / unknown / simulated
            detail TEXT DEFAULT '',          -- 脱敏后对端响应标识
            next_attempt_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_datt_msg ON delivery_attempts(msg_id);
        CREATE TABLE IF NOT EXISTS ai_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,              -- analyze_backlog / retry_failed
            params TEXT DEFAULT '{}',
            status TEXT NOT NULL DEFAULT '待处理',  -- 待处理/处理中/完成/失败/已取消
            total INTEGER DEFAULT 0, done INTEGER DEFAULT 0, fail INTEGER DEFAULT 0, skip INTEGER DEFAULT 0,
            claimed_at TEXT, lease_until REAL,
            note TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ai_result_conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tweet_id INTEGER NOT NULL,
            base_version INTEGER NOT NULL,
            current_version INTEGER NOT NULL,
            reason TEXT DEFAULT '',
            result_json TEXT DEFAULT '',
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_aiconf_tweet ON ai_result_conflicts(tweet_id);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uid TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            summary TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT '候选',   -- 候选/已确认/已归档
            version INTEGER DEFAULT 1,
            severity TEXT DEFAULT '',
            first_seen TEXT DEFAULT '',
            last_seen TEXT DEFAULT '',
            -- 证据内容指纹（2026-11-09 E）：底层情报(成员+analysis_version)变化即推进
            evidence_fingerprint TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
        CREATE TABLE IF NOT EXISTS event_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uid TEXT NOT NULL,
            tweet_id INTEGER NOT NULL,
            basis TEXT NOT NULL DEFAULT 'manual',   -- manual/official_same/victim_same/shared_indicator
            source_kind TEXT NOT NULL DEFAULT 'original',  -- original/repost/translation
            note TEXT DEFAULT '',
            created_at TEXT,
            UNIQUE(event_uid, tweet_id)
        );
        CREATE INDEX IF NOT EXISTS idx_evidev_tweet ON event_evidence(tweet_id);
        CREATE INDEX IF NOT EXISTS idx_evidev_event ON event_evidence(event_uid);
        CREATE TABLE IF NOT EXISTS event_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uid TEXT NOT NULL,
            version INTEGER NOT NULL,
            action TEXT NOT NULL,          -- create/update/merge/split/unlink/review/status
            detail TEXT DEFAULT '',
            operator TEXT DEFAULT '',
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_evrev_uid ON event_revisions(event_uid);
        CREATE TABLE IF NOT EXISTS event_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uid TEXT NOT NULL,
            event_version INTEGER NOT NULL,
            decision TEXT NOT NULL,        -- approved/rejected
            -- 审核绑定证据指纹：底层情报再变 → 旧决定自动成为历史（不得冒充当前批准）
            evidence_fingerprint TEXT DEFAULT '',
            note TEXT DEFAULT '',
            operator TEXT DEFAULT '',
            created_at TEXT,
            UNIQUE(event_uid, event_version)
        );
        CREATE INDEX IF NOT EXISTS idx_evrevw_uid ON event_reviews(event_uid);
        CREATE TABLE IF NOT EXISTS ai_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            action TEXT DEFAULT '',
            model TEXT DEFAULT '',
            tweet_id INTEGER,
            ref TEXT DEFAULT '',
            ms INTEGER DEFAULT 0,
            status TEXT DEFAULT '',
            detail TEXT DEFAULT ''
        );

        -- ============ 报告编写工作台（协作写作） ============
        -- 模板：元信息与发布版本分离；正文是 kind=template 的协作文档
        CREATE TABLE IF NOT EXISTS doc_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT DEFAULT '',
            status TEXT DEFAULT '启用',
            current_version INTEGER,
            variables TEXT DEFAULT '[]',
            agent_notes TEXT DEFAULT '',
            check_rules TEXT DEFAULT '[]',
            body_doc_uid TEXT DEFAULT '',
            created_by TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS template_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            body_md TEXT NOT NULL,
            variables TEXT DEFAULT '[]',
            agent_notes TEXT DEFAULT '',
            check_rules TEXT DEFAULT '[]',
            changelog TEXT DEFAULT '',
            published_by TEXT DEFAULT '',
            published_at TEXT,
            UNIQUE(template_id, version)
        );
        -- 待编写线索候选
        CREATE TABLE IF NOT EXISTS writing_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cand_uid TEXT UNIQUE,
            source_type TEXT DEFAULT 'manual',
            title TEXT NOT NULL,
            angle TEXT DEFAULT '',
            origin TEXT DEFAULT '',
            tweet_ids TEXT DEFAULT '[]',
            event_uid TEXT DEFAULT '',
            recommend_reason TEXT DEFAULT '',
            ai_suggestion TEXT DEFAULT '',
            human_decision TEXT DEFAULT '',
            decision_note TEXT DEFAULT '',
            owner TEXT DEFAULT '',
            priority INTEGER DEFAULT 3,
            suggested_priority INTEGER,
            suggested_template_id INTEGER,
            materials_todo TEXT DEFAULT '[]',
            status TEXT DEFAULT '待处理',
            version INTEGER DEFAULT 1,
            doc_uid TEXT DEFAULT '',
            report_task_id INTEGER,
            evidence_snapshot TEXT DEFAULT '',
            last_ai_run TEXT DEFAULT '',
            has_update INTEGER DEFAULT 0,
            dedup_key TEXT DEFAULT '',
            created_by TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_wcand_status ON writing_candidates(status);
        CREATE TABLE IF NOT EXISTS candidate_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cand_uid TEXT NOT NULL,
            action TEXT DEFAULT '',
            operator TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ai_recommend_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_type TEXT DEFAULT 'manual',
            scope_ref TEXT DEFAULT '',
            status TEXT DEFAULT 'queued',
            result TEXT DEFAULT '',
            error TEXT DEFAULT '',
            attempts INTEGER DEFAULT 0,
            created_by TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT
        );
        -- 协作文档（报告正文与模板正文共用内核）
        CREATE TABLE IF NOT EXISTS doc_documents (
            doc_uid TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            kind TEXT DEFAULT 'report',
            status TEXT DEFAULT '编辑中',
            template_id INTEGER,
            template_version INTEGER,
            initial_md TEXT DEFAULT '',
            initialized INTEGER DEFAULT 0,
            default_permission TEXT DEFAULT 'read',
            candidate_uid TEXT DEFAULT '',
            report_task_id INTEGER,
            source_snapshot TEXT DEFAULT '',
            review_rev INTEGER,
            review_decision TEXT DEFAULT '',
            review_note TEXT DEFAULT '',
            review_by TEXT DEFAULT '',
            review_at TEXT DEFAULT '',
            created_by TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS doc_acl (
            doc_uid TEXT NOT NULL,
            principal TEXT NOT NULL,
            permission TEXT NOT NULL DEFAULT 'read',
            created_at TEXT,
            UNIQUE(doc_uid, principal)
        );
        -- CRDT 增量日志：协作正文的权威存储（房间重建按 seq 回放）
        CREATE TABLE IF NOT EXISTS doc_updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_uid TEXT NOT NULL,
            seq INTEGER NOT NULL,
            update_blob BLOB NOT NULL,
            created_at TEXT,
            UNIQUE(doc_uid, seq)
        );
        -- 业务修订（不可变快照，业务版本唯一事实）
        CREATE TABLE IF NOT EXISTS doc_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_uid TEXT NOT NULL,
            rev INTEGER NOT NULL,
            title TEXT DEFAULT '',
            body_md TEXT NOT NULL,
            revision_kind TEXT DEFAULT 'save',
            base_rev INTEGER,
            content_hash TEXT DEFAULT '',
            created_by TEXT DEFAULT '',
            created_at TEXT,
            UNIQUE(doc_uid, rev)
        );
        -- Agent/用户修改建议（锚点精确匹配，逐条接受/拒绝）
        CREATE TABLE IF NOT EXISTS doc_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_uid TEXT NOT NULL,
            suggestion_uid TEXT UNIQUE,
            base_rev INTEGER NOT NULL,
            agent TEXT NOT NULL,
            target TEXT DEFAULT '',
            rationale TEXT DEFAULT '',
            sources TEXT DEFAULT '[]',
            patch_kind TEXT DEFAULT 'replace',
            anchor TEXT DEFAULT '',
            old_text TEXT DEFAULT '',
            new_text TEXT DEFAULT '',
            client_id TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            reviewed_by TEXT DEFAULT '',
            reviewed_at TEXT DEFAULT '',
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_dsug_doc ON doc_suggestions(doc_uid, status);
        -- Word 导出任务（捕获修订→转换→产物）
        CREATE TABLE IF NOT EXISTS doc_exports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_uid TEXT NOT NULL,
            rev INTEGER NOT NULL,
            content_hash TEXT DEFAULT '',
            template_id INTEGER,
            template_version INTEGER,
            style_version TEXT DEFAULT '',
            pandoc_version TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            error TEXT DEFAULT '',
            missing_images TEXT DEFAULT '[]',
            file_path TEXT DEFAULT '',
            file_name TEXT DEFAULT '',
            sha256 TEXT DEFAULT '',
            size INTEGER DEFAULT 0,
            created_by TEXT DEFAULT '',
            created_at TEXT, finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ws_tickets (
            ticket TEXT PRIMARY KEY,
            doc_uid TEXT NOT NULL,
            principal TEXT NOT NULL,
            permission TEXT NOT NULL DEFAULT 'read',
            used INTEGER DEFAULT 0,
            expires_at REAL,
            created_at TEXT
        );
        -- Agent 直接受控修改的幂等记录（client_id 唯一）
        CREATE TABLE IF NOT EXISTS doc_agent_edits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_uid TEXT NOT NULL,
            client_id TEXT NOT NULL,
            rev INTEGER,
            agent TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            created_at TEXT,
            UNIQUE(doc_uid, client_id)
        );
        """)
        os.makedirs(os.path.join(DATA_DIR, "report_tasks"), exist_ok=True)
        os.makedirs(os.path.join(DATA_DIR, "doc_assets"), exist_ok=True)
        os.makedirs(os.path.join(DATA_DIR, "word_exports"), exist_ok=True)
        # 来源身份：局部唯一索引（同来源内幂等）+ 存量回填（可重复；无法映射的记录留空=待处理）
        try:
            get_conn().execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_tweets_src_tid"
                " ON tweets(source_id, tweet_id) WHERE source_id!=''")
            get_conn().execute(
                "UPDATE tweets SET source_id=(SELECT id FROM x_accounts x WHERE x.screen_name=tweets.screen_name)"
                " WHERE source_id='' AND screen_name IN (SELECT screen_name FROM x_accounts)")
            get_conn().execute(
                "UPDATE tweets SET source_id=(SELECT 'rss:' || id FROM rss_sources r"
                " WHERE r.name=substr(tweets.screen_name,5))"
                " WHERE source_id='' AND screen_name LIKE 'RSS:%'")
            get_conn().execute(
                "UPDATE tweets SET text_hash=(CASE WHEN text='' THEN ''"
                " ELSE substr(hex(sha256(CAST(text AS BLOB))),1,32) END)"
                " WHERE text_hash='' AND text!=''") if False else None
            get_conn().commit()
        except Exception:
            pass
        # 内容哈希：Python 侧回填（sha3 不依赖 sqlite 编译项），可重复
        try:
            import hashlib as _hl
            rows = get_conn().execute(
                "SELECT id, text FROM tweets WHERE text_hash='' AND text!='' LIMIT 2000").fetchall()
            for r in rows:
                h = _hl.sha256(r["text"].encode("utf-8", "ignore")).hexdigest()[:32]
                get_conn().execute("UPDATE tweets SET text_hash=? WHERE id=?", (h, r["id"]))
            if rows:
                get_conn().commit()
        except Exception:
            pass
        # delivery_msgs 补列（幂等）：claim_token（owner 令牌）
        try:
            get_conn().execute("ALTER TABLE delivery_msgs ADD COLUMN claim_token TEXT DEFAULT ''")
        except Exception:
            pass
        # 事件证据指纹列（2026-11-09 E，幂等）：底层情报变化可被检测
        for _tbl in ("events", "event_reviews"):
            try:
                get_conn().execute(f"ALTER TABLE {_tbl} ADD COLUMN evidence_fingerprint TEXT DEFAULT ''")
            except Exception:
                pass
        # 迁移（可重复）：既有报告任务的推文引用回填到 report_refs（UNIQUE 幂等）
        try:
            get_conn().execute(
                "INSERT OR IGNORE INTO report_refs(report_id, tweet_id, created_at)"
                " SELECT id, j.value, datetime('now','localtime') FROM report_tasks,"
                " json_each(CASE WHEN substr(report_tasks.tweet_ids,1,1)='[' THEN report_tasks.tweet_ids"
                " ELSE '[]' END) j")
            get_conn().commit()
        except Exception:
            pass
        # 早期建库可能缺列，逐列补齐（已存在则忽略报错）
        added_pushed = False
        for col in ("ai_status", "ai_error", "ai_model", "ai_severity", "ai_type",
                    "ai_actors", "ai_iocs", "ai_assessment", "ai_recommendations",
                    "ai_confidence", "ai_analyzed_at", "title_zh", "text_zh", "translated_at",
                    "geo_country", "geo_province", "geo_city", "org", "industry",
                    "geo_basis", "tags", "media_url", "starred", "read", "ignored", "pushed", "ai_retries",
                    "source_id", "text_hash", "ai_modified", "ai_repair",
                    "analysis_version", "ai_integrity", "ai_review", "ai_review_at", "ai_review_version",
                    "category"):
            try:
                get_conn().execute(f"ALTER TABLE tweets ADD COLUMN {col} "
                                   + ("INTEGER DEFAULT 0" if col in ("ai_confidence", "starred", "read", "ignored", "pushed", "ai_retries", "analysis_version")
                                      else "TEXT DEFAULT ''"))
                if col == "pushed":
                    added_pushed = True  # 存量推文视为已处理，防止升级后历史推文被整批推送
            except Exception:
                pass
        if added_pushed:
            get_conn().execute("UPDATE tweets SET pushed=1")
        # tags 列以 JSON 数组文本存储（'[]' 为空），统一把空值规范成 '[]'，避免 json_each 遇空串报错
        get_conn().execute("UPDATE tweets SET tags='[]' WHERE tags='' OR tags IS NULL")
        # push_logs 旧表补 content 列（推送内容留档）
        try:
            get_conn().execute("ALTER TABLE push_logs ADD COLUMN content TEXT DEFAULT ''")
        except Exception:
            pass
        # report_tasks 旧表补列（逐列容错）
        for col in ("agent_gate", "word_limit"):
            try:
                get_conn().execute(f"ALTER TABLE report_tasks ADD COLUMN {col} TEXT DEFAULT ''")
            except Exception:
                pass
        try:
            get_conn().execute("ALTER TABLE x_accounts ADD COLUMN fetch_minutes INTEGER DEFAULT 0")
        except Exception:
            pass


# ---------- 推文 ----------

def insert_tweet(t):
    """幂等入库；成功返回行 id，重复返回 None。
    第三批：text 保存完整原文（上限 10000，展示/模型输入另行裁剪）；
    source_id 稳定来源身份（来源改名不丢归属）；text_hash 内容完整性；
    同 source_id+tweet_id 已存在（来源改名场景）视为重复。"""
    text = (t.get("text", "") or "")[:10000]
    import hashlib as _hl
    thash = _hl.sha256(text.encode("utf-8", "ignore")).hexdigest()[:32] if text else ""
    sid = str(t.get("source_id") or "")
    with _lock:
        if sid:
            dup = get_conn().execute(
                "SELECT 1 FROM tweets WHERE source_id=? AND tweet_id=?",
                (sid, t["tweet_id"])).fetchone()
            if dup:
                return None
        cur = get_conn().execute(
            "INSERT OR IGNORE INTO tweets (screen_name, tweet_id, text, url, published_at,"
            " is_retweet, is_reply, media_url, created_at, source_id, text_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (t["screen_name"], t["tweet_id"], text, t.get("url", ""),
             t.get("published_at", ""), int(t.get("is_retweet", 0)), int(t.get("is_reply", 0)),
             t.get("media_url", ""), now(), sid, thash))
        get_conn().commit()
        if cur.rowcount > 0:
            return cur.lastrowid
        return None


def get_tweet(tid):
    r = get_conn().execute("SELECT * FROM tweets WHERE id=?", (int(tid),)).fetchone()
    return dict(r) if r else None


from contextlib import contextmanager as _ctxmgr


@_ctxmgr
def snapshot_conn():
    """独立只读连接（2026-11-09 E）：导出用一致性读取事务。

    WAL 模式下读连接获得独立快照——与写连接完全隔离：
    导出事务与写请求交错不再出现 'cannot commit - no transaction is active'
    （共享连接上 BEGIN 与写入互相踩踏的历史缺陷），也不阻塞写。
    用法：with db.snapshot_conn() as c: ...（事务内完成全部读取）"""
    DATA_DIR.mkdir(exist_ok=True)
    ro = sqlite3.connect(f"file:{(DATA_DIR / 'xintel.db').as_posix()}?mode=ro", uri=True,
                         check_same_thread=False, timeout=10)
    ro.row_factory = sqlite3.Row
    ro.execute("BEGIN DEFERRED")
    try:
        yield ro
        ro.execute("COMMIT")
    finally:
        try:
            ro.close()
        except Exception:
            pass


def _row_tweet(r):
    d = dict(r)
    for col in ("ai_actors", "ai_iocs", "ai_recommendations", "org", "tags"):
        try:
            d[col] = json.loads(d.get(col) or "[]")
        except Exception:
            d[col] = []
    return d


def list_tweets(screen_name="all", severity="all", q="", country="", industry="",
                category="all", status="all", tag="", limit=30, offset=0):
    """status 组合过滤：all=未忽略（默认） / unread / read / ignored / starred；
    category 按情报分类过滤；tag 按关键词标签过滤（JSON 数组 LIKE 匹配，如 勒索/C2）"""
    conds, args = [], []
    if screen_name and screen_name != "all":
        conds.append("screen_name=?")
        args.append(screen_name)
    if severity and severity != "all":
        conds.append("ai_severity=?")
        args.append(severity)
    if country and country != "all":
        conds.append("geo_country LIKE ?")
        args.append(f"%{country}%")
    if industry and industry != "all":
        conds.append("industry LIKE ?")
        args.append(f"%{industry}%")
    if category and category != "all":
        conds.append("category=?")
        args.append(category)
    if tag:
        # tags 为 JSON 数组文本；CASE 兜底空串/非 JSON，防 json_each 解析报错。
        # 标签是离散值，用精确匹配（与 facets 标签计数同语义），否则子串匹配会造成"计数 21 实际 23"式偏差
        conds.append("EXISTS (SELECT 1 FROM json_each("
                     "CASE WHEN substr(tags,1,1)='[' THEN tags ELSE '[]' END) je"
                     " WHERE je.value = ?)")
        args.append(tag)
    if status == "unread":
        conds.append("read=0 AND ignored=0")
    elif status == "read":
        conds.append("read=1 AND ignored=0")
    elif status == "ignored":
        conds.append("ignored=1")
    elif status == "starred":
        conds.append("starred=1")
    else:
        conds.append("ignored=0")  # 默认视图隐藏已忽略
    if q:
        conds.append("(text LIKE ? OR text_zh LIKE ? OR ai_assessment LIKE ? OR ai_type LIKE ?"
                     " OR geo_country LIKE ? OR geo_province LIKE ? OR geo_city LIKE ?"
                     " OR org LIKE ? OR industry LIKE ? OR tags LIKE ?)")
        args += [f"%{q}%"] * 10
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    c = get_conn()
    total = c.execute("SELECT COUNT(*) AS n FROM tweets" + where, args).fetchone()["n"]
    # 统一排序键：published_at（GMT 裸值，X 与 RSS 同格式）优先，缺失回退 created_at；
    # 不能用 CAST(tweet_id AS INTEGER)——RSS 的 tweet_id 是 md5 十六进制，CAST 恒为 0
    rows = c.execute("SELECT * FROM tweets" + where +
                     " ORDER BY COALESCE(NULLIF(published_at,''), created_at) DESC, id DESC"
                     " LIMIT ? OFFSET ?",
                     args + [int(limit), int(offset)]).fetchall()
    return total, [_row_tweet(r) for r in rows]


def set_tweet_flags(tweet_id, read=None, ignored=None, starred=None):
    sets, args = [], []
    if read is not None:
        sets.append("read=?"); args.append(int(read))
    if ignored is not None:
        sets.append("ignored=?"); args.append(int(ignored))
    if starred is not None:
        sets.append("starred=?"); args.append(int(starred))
    if not sets:
        return False
    args.append(int(tweet_id))
    with _lock:
        get_conn().execute(f"UPDATE tweets SET {', '.join(sets)} WHERE id=?", args)
        get_conn().commit()
    return True


def update_tweet_text(tid, text):
    """原文更新：写文本 + 推进分析版本（使旧分析结果过期）。返回新版本"""
    import hashlib as _hl
    thash = _hl.sha256((text or "").encode("utf-8", "ignore")).hexdigest()[:32] if text else ""
    with _lock:
        get_conn().execute(
            "UPDATE tweets SET text=?, text_hash=?, analysis_version=COALESCE(analysis_version,0)+1"
            " WHERE id=?", ((text or "")[:10000], thash, int(tid)))
        get_conn().commit()
    return get_analysis_version(tid)


def batch_tweet_op(ids, op):
    """op: read / unread / ignore / unignore / delete（批量管理操作，返回影响行数）"""
    if not ids:
        return 0
    ids = [int(i) for i in ids][:200]
    ph = ",".join("?" * len(ids))
    sql = {"read": f"UPDATE tweets SET read=1 WHERE id IN ({ph})",
           "unread": f"UPDATE tweets SET read=0 WHERE id IN ({ph})",
           "ignore": f"UPDATE tweets SET ignored=1, read=1 WHERE id IN ({ph})",
           "unignore": f"UPDATE tweets SET ignored=0 WHERE id IN ({ph})",
           "delete": f"DELETE FROM tweets WHERE id IN ({ph}) AND starred=0"
                     f" AND id NOT IN (SELECT tweet_id FROM report_refs)"
                     f" AND id NOT IN (SELECT tweet_id FROM event_evidence)"
                     f" AND id NOT IN (SELECT j.value FROM writing_candidates,"
                     f"   json_each(CASE WHEN substr(writing_candidates.tweet_ids,1,1)='['"
                     f"   THEN writing_candidates.tweet_ids ELSE '[]' END) j"
                     f"   WHERE writing_candidates.status IN ('待处理','写作中','暂缓','已转报告'))"
                     f" AND id NOT IN (SELECT di.tweet_id FROM delivery_items di"
                     f"   JOIN delivery_msgs m ON m.id=di.msg_id"
                     f"   WHERE m.state IN ('待发送','发送中','等待重试'))"}.get(op)
    if not sql:
        return 0
    with _lock:
        cur = get_conn().execute(sql, ids)
        get_conn().commit()
        return cur.rowcount


def cleanup_old(keep_days):
    """一键清理：删除 keep_days 天前的推文。
    保护（2026-11-08）：收藏、被报告任务引用（report_refs）、存在在途投递
    （待发送/发送中/等待重试消息成员）的推文一律豁免，返回删除数。"""
    with _lock:
        cur = get_conn().execute(
            "DELETE FROM tweets WHERE starred=0"
            " AND id NOT IN (SELECT tweet_id FROM report_refs)"
            " AND id NOT IN (SELECT tweet_id FROM event_evidence)"
            " AND id NOT IN (SELECT j.value FROM writing_candidates,"
            "   json_each(CASE WHEN substr(writing_candidates.tweet_ids,1,1)='['"
            "   THEN writing_candidates.tweet_ids ELSE '[]' END) j"
            "   WHERE writing_candidates.status IN ('待处理','写作中','暂缓','已转报告'))"
            " AND id NOT IN (SELECT di.tweet_id FROM delivery_items di"
            "   JOIN delivery_msgs m ON m.id=di.msg_id"
            "   WHERE m.state IN ('待发送','发送中','等待重试'))"
            " AND created_at < datetime('now','localtime',?)",
            (f"-{int(keep_days)} days",))
        get_conn().commit()
        return cur.rowcount


# ---------- 抓取日志 ----------

def add_fetch_log(trigger, accounts_due, accounts_ok, new, ai, error="", trace=""):
    with _lock:
        get_conn().execute(
            "INSERT INTO fetch_logs (at, trigger, accounts_due, accounts_ok, new, ai, error, trace)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (now(), trigger, int(accounts_due), int(accounts_ok), int(new), int(ai),
             error[:300], trace[:1200]))
        # 只保留最近 500 轮
        get_conn().execute(
            "DELETE FROM fetch_logs WHERE id NOT IN (SELECT id FROM fetch_logs ORDER BY id DESC LIMIT 500)")
        get_conn().commit()


def list_fetch_logs(limit=30):
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM fetch_logs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()]


# ---------- AI 日志 ----------

def add_ai_log(action, model="", tweet_id=None, ref="", ms=0, status="ok", detail=""):
    with _lock:
        get_conn().execute(
            "INSERT INTO ai_logs (at, action, model, tweet_id, ref, ms, status, detail)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (now(), action[:20], model[:60], tweet_id, ref[:40], int(ms), status[:10], detail[:200]))
        # 只保留最近 500 条
        get_conn().execute(
            "DELETE FROM ai_logs WHERE id NOT IN (SELECT id FROM ai_logs ORDER BY id DESC LIMIT 500)")
        get_conn().commit()


def list_ai_logs(limit=100, action=""):
    sql = "SELECT * FROM ai_logs"
    args = []
    if action:
        sql += " WHERE action=?"
        args.append(action)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in get_conn().execute(sql, args).fetchall()]


def ai_log_stats():
    """今日 AI 调用统计：次数/成功/失败/平均耗时"""
    row = get_conn().execute(
        "SELECT COUNT(*) AS n, SUM(status='ok') AS ok, AVG(ms) AS avg_ms FROM ai_logs"
        " WHERE date(at)=date('now','localtime')").fetchone()
    n = row["n"] or 0
    return {"calls": n, "ok": row["ok"] or 0,
            "error": n - (row["ok"] or 0),
            "avg_ms": int(row["avg_ms"] or 0)}


def facets(sev="all", acc="all", q="", country="all", industry="all",
           category="all", status="all", tag=""):
    """按当前筛选上下文统计各维度计数（2026-11-07 修复"下拉显示 N 实际 0 条"）：
    每个维度统计时排除自身维度的条件（国家维不套国家筛选，分类维不套分类筛选，标签维不套标签），
    其余条件（等级/账号/搜索词/状态域）全部生效——下拉数字=在当前其它条件下选它能得到的真实条数。
    状态域始终生效，与推文流默认视图（未忽略）一致。"""
    c = get_conn()

    def _base(skip):
        conds, args = [], []
        if sev and sev != "all":
            conds.append("ai_severity=?"); args.append(sev)
        if acc and acc != "all":
            conds.append("screen_name=?"); args.append(acc)
        if country and country != "all" and skip != "country":
            conds.append("geo_country LIKE ?"); args.append(f"%{country}%")
        if industry and industry != "all" and skip != "industry":
            conds.append("industry LIKE ?"); args.append(f"%{industry}%")
        if category and category != "all" and skip != "category":
            conds.append("category=?"); args.append(category)
        if tag and skip != "tag":
            conds.append("EXISTS (SELECT 1 FROM json_each("
                         "CASE WHEN substr(tags,1,1)='[' THEN tags ELSE '[]' END) je"
                         " WHERE je.value = ?)")
            args.append(tag)
        if q:
            conds.append("(text LIKE ? OR text_zh LIKE ? OR ai_assessment LIKE ? OR ai_type LIKE ?"
                         " OR geo_country LIKE ? OR geo_province LIKE ? OR geo_city LIKE ?"
                         " OR org LIKE ? OR industry LIKE ? OR tags LIKE ?)")
            args += [f"%{q}%"] * 10
        if status == "unread":
            conds.append("read=0 AND ignored=0")
        elif status == "read":
            conds.append("read=1 AND ignored=0")
        elif status == "ignored":
            conds.append("ignored=1")
        elif status == "starred":
            conds.append("starred=1")
        else:
            conds.append("ignored=0")
        return conds, args

    def _fac(col, skip):
        conds, args = _base(skip)
        conds.append(f"{col}!='' AND {col}!='未知'")
        where = " WHERE " + " AND ".join(f"({x})" for x in conds)
        rows = c.execute(f"SELECT {col} AS v, COUNT(*) AS n FROM tweets{where}"
                         f" GROUP BY {col} ORDER BY n DESC LIMIT 60", args).fetchall()
        return [{"name": r["v"], "count": r["n"]} for r in rows]

    # tags：JSON 数组 Python 侧聚合（同基线，skip=tag）
    conds, args = _base("tag")
    where = " WHERE " + " AND ".join(f"({x})" for x in conds)
    tag_counts = {}
    for (js,) in c.execute(f"SELECT tags FROM tweets{where}", args):
        try:
            arr = json.loads(js)
        except Exception:
            continue
        for name in arr if isinstance(arr, list) else []:
            name = str(name).strip()
            if name:
                tag_counts[name] = tag_counts.get(name, 0) + 1
    tags = [{"name": k, "count": v} for k, v in
            sorted(tag_counts.items(), key=lambda x: (-x[1], x[0]))[:60]]
    return {"countries": _fac("geo_country", "country"),
            "industries": _fac("industry", "industry"),
            "categories": _fac("category", "category"), "tags": tags}


def category_facets():
    """情报分类去重计数（供筛选下拉）"""
    rows = get_conn().execute(
        "SELECT category AS v, COUNT(*) AS n FROM tweets"
        " WHERE category!='' GROUP BY category ORDER BY n DESC").fetchall()
    return [{"name": r["v"], "count": r["n"]} for r in rows]


def tweets_missing_category(limit_n):
    """未打情报分类标签的推文（按新到旧）"""
    return [_row_tweet(r) for r in get_conn().execute(
        "SELECT * FROM tweets WHERE category=''"
        " ORDER BY COALESCE(NULLIF(published_at,''), created_at) DESC, id DESC"
        " LIMIT ?", (int(limit_n),)).fetchall()]


def tweets_missing_geo(limit_n):
    """已研判成功但缺地区标签的推文（供 AI 补齐）"""
    return [_row_tweet(r) for r in get_conn().execute(
        "SELECT * FROM tweets WHERE ai_status='ok' AND (geo_country='' OR geo_country IS NULL)"
        " ORDER BY id DESC LIMIT ?", (int(limit_n),)).fetchall()]


def tweets_pending_push(limit_n=200):
    """待推送：未推送且未忽略（是否满足 AI 门控由 engine 判定）"""
    return [_row_tweet(r) for r in get_conn().execute(
        "SELECT * FROM tweets WHERE pushed=0 AND ignored=0"
        " ORDER BY COALESCE(NULLIF(published_at,''), created_at) DESC, id DESC LIMIT ?",
        (int(limit_n),)).fetchall()]


def mark_pushed(ids):
    if not ids:
        return
    ids = [int(i) for i in ids][:500]
    ph = ",".join("?" * len(ids))
    with _lock:
        get_conn().execute(f"UPDATE tweets SET pushed=1 WHERE id IN ({ph})", ids)
        get_conn().commit()


def tweets_pending_ai(limit_n):
    """未研判 + 首败待重试的推文（error 且 retries<2 闭合重试环；≥2 次由推送门控附说明放行）"""
    return [_row_tweet(r) for r in get_conn().execute(
        "SELECT * FROM tweets WHERE ai_status='' OR (ai_status='error' AND ai_retries<2)"
        " ORDER BY id DESC LIMIT ?", (int(limit_n),)).fetchall()]


def tweets_recent(limit_n=8):
    return [_row_tweet(r) for r in get_conn().execute(
        "SELECT * FROM tweets ORDER BY id DESC LIMIT ?", (int(limit_n),)).fetchall()]


def update_ai(tweet_id, fields):
    """fields: ai_status/ai_error/ai_model/ai_severity/ai_type/ai_actors/ai_iocs/
    ai_assessment/ai_recommendations/ai_confidence/ai_analyzed_at/title_zh/text_zh/translated_at"""
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    args = list(fields.values()) + [int(tweet_id)]
    with _lock:
        get_conn().execute(f"UPDATE tweets SET {sets} WHERE id=?", args)
        get_conn().commit()


def get_analysis_version(tid):
    r = get_conn().execute("SELECT analysis_version FROM tweets WHERE id=?", (int(tid),)).fetchone()
    return int((r["analysis_version"] if r else 0) or 0)


def bump_analysis_version(tid, bump_modified=False):
    """原子推进版本（人工修订/原文更新调用）"""
    with _lock:
        get_conn().execute(
            "UPDATE tweets SET analysis_version=COALESCE(analysis_version,0)+1"
            + (", ai_modified=1" if bump_modified else "")
            + " WHERE id=?", (int(tid),))
        get_conn().commit()
    return get_analysis_version(tid)


def manual_override(tid, fields):
    """人工修订：字段更新 + ai_modified=1 + 版本 +1，全部在同一事务内原子完成"""
    with _lock:
        sets = ", ".join(f"{k}=?" for k in fields)
        args = list(fields.values()) + [int(tid)]
        cur = get_conn().execute(
            f"UPDATE tweets SET {sets}, ai_modified=1,"
            " analysis_version=COALESCE(analysis_version,0)+1 WHERE id=?", args)
        get_conn().commit()
        if cur.rowcount == 0:
            return None
    return get_analysis_version(tid)


def apply_ai_result(tid, fields, base_version, source="ai"):
    """原子应用 AI 结果。返回 (applied, reason, current_version)。

    base_version 必须是调用模型前读取的版本。版本已被推进（人工改过/原文变了/其它
    AI 先提交）时：不覆盖、写冲突审计、返回 applied=False。
    """
    with _lock:
        row = get_conn().execute(
            "SELECT analysis_version, ai_modified FROM tweets WHERE id=?", (int(tid),)).fetchone()
        if not row:
            return False, "tweet_not_found", 0
        cur_ver = int((row["analysis_version"] or 0))
        if int(base_version) != cur_ver:
            # 冲突：保留审计，不动当前有效值
            import json as _json
            try:
                payload = _json.dumps(fields, ensure_ascii=False, default=str)[:4000]
            except Exception:
                payload = "{}"
            reason = "manual_modified" if int(row["ai_modified"] or 0) == 1 else "version_changed"
            get_conn().execute(
                "INSERT INTO ai_result_conflicts (tweet_id, base_version, current_version,"
                " reason, result_json, created_at) VALUES (?,?,?,?,?,?)",
                (int(tid), int(base_version), cur_ver, reason, payload, now()))
            get_conn().commit()
            return False, reason, cur_ver
        if int(row["ai_modified"] or 0) == 1:
            # 人工保护：整条保护（不做字段级合并，避免语义拼接）
            import json as _json
            get_conn().execute(
                "INSERT INTO ai_result_conflicts (tweet_id, base_version, current_version,"
                " reason, result_json, created_at) VALUES (?,?,?,?,?,?)",
                (int(tid), int(base_version), cur_ver, "manual_protected",
                 _json.dumps(fields, ensure_ascii=False, default=str)[:4000], now()))
            get_conn().commit()
            return False, "manual_protected", cur_ver
        sets = ", ".join(f"{k}=?" for k in fields)
        args = list(fields.values()) + [int(tid), int(base_version)]
        # 关键：成功应用后必须推进版本——否则两个同 base 的结果都会"成功"，
        # 后到者会静默覆盖先到者（乱序竞争漏洞，2026-11-08 修复）
        cur = get_conn().execute(
            f"UPDATE tweets SET {sets},"
            " analysis_version=COALESCE(analysis_version,0)+1"
            " WHERE id=? AND analysis_version=?", args)
        get_conn().commit()
        if cur.rowcount == 0:
            return False, "race_lost", get_analysis_version(tid)
        return True, "applied", get_analysis_version(tid)


def clear_manual_protection(tid):
    """显式重新分析：解除人工保护并推进版本（人工值将成为历史版本）"""
    with _lock:
        cur = get_conn().execute(
            "UPDATE tweets SET ai_modified=0,"
            " analysis_version=COALESCE(analysis_version,0)+1 WHERE id=? AND ai_modified=1",
            (int(tid),))
        get_conn().commit()
        return cur.rowcount > 0


def bind_review(tid, base_version, review="manual"):
    """人工审核绑定：原子检查版本——审核只能绑定用户实际审阅的版本。

    请求处理期间内容版本被推进（AI 结果先落地/人工修订/原文更新）→ 拒绝绑定，
    返回 None（调用方应要求刷新后基于新版本重新审核）。
    成功：ai_review/ai_review_at/ai_review_version 原子写入，返回绑定版本。"""
    with _lock:
        cur = get_conn().execute(
            "UPDATE tweets SET ai_review=?, ai_review_at=?, ai_review_version=?"
            " WHERE id=? AND analysis_version=?",
            (review, now(), int(base_version), int(tid), int(base_version)))
        get_conn().commit()
        if cur.rowcount == 0:
            return None
        return get_analysis_version(tid)


def update_ai_guarded(tid, fields, base_version):
    """失败/状态类写回的权威路径：原子检查版本与人工保护，不推进版本。

    与 apply_ai_result 的区别：这是状态写回（ai_status/ai_error 等），不推进版本；
    但同样必须在提交时原子确认「版本未变且未被人工接管」——迟到失败不得覆盖
    期间新增的人工状态或更新的成功结果（2026-11-09 缺陷 B 修复）。
    返回 (applied, reason)。
    """
    if not fields:
        return True, "applied"
    with _lock:
        row = get_conn().execute(
            "SELECT analysis_version, ai_modified FROM tweets WHERE id=?", (int(tid),)).fetchone()
        if not row:
            return False, "tweet_not_found"
        cur_ver = int(row["analysis_version"] or 0)
        if int(row["ai_modified"] or 0) == 1 or int(base_version) != cur_ver:
            reason = ("status_manual_protected" if int(row["ai_modified"] or 0) == 1
                      else "status_version_changed")
            try:
                payload = json.dumps(fields, ensure_ascii=False, default=str)[:4000]
            except Exception:
                payload = "{}"
            get_conn().execute(
                "INSERT INTO ai_result_conflicts (tweet_id, base_version, current_version,"
                " reason, result_json, created_at) VALUES (?,?,?,?,?,?)",
                (int(tid), int(base_version), cur_ver, reason, payload, now()))
            get_conn().commit()
            return False, reason
        sets = ", ".join(f"{k}=?" for k in fields)
        args = list(fields.values()) + [int(tid), cur_ver]
        # 注意：ai_modified 为 TEXT 列（默认 ''），不能用 =0 比较（台账 B47 同源坑）
        not_modified = "(ai_modified IS NULL OR ai_modified='' OR ai_modified='0')"
        cur = get_conn().execute(
            f"UPDATE tweets SET {sets} WHERE id=? AND analysis_version=? AND {not_modified}",
            args)
        get_conn().commit()
        if cur.rowcount == 0:
            return False, "race_lost"
        return True, "applied"


def list_conflicts(limit=50):
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM ai_result_conflicts ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()]


def ai_stats():
    c = get_conn()
    analyzed = c.execute("SELECT COUNT(*) AS n FROM tweets WHERE ai_status='ok'").fetchone()["n"]
    translated = c.execute("SELECT COUNT(*) AS n FROM tweets WHERE text_zh!=''").fetchone()["n"]
    today_analyzed = c.execute(
        "SELECT COUNT(*) AS n FROM tweets WHERE ai_status='ok' AND date(ai_analyzed_at)=date('now','localtime')"
    ).fetchone()["n"]
    return {"analyzed": analyzed, "translated": translated, "today_analyzed": today_analyzed}


# ---------- X 账号 ----------

def list_x_accounts(enabled_only=False):
    sql = "SELECT * FROM x_accounts"
    if enabled_only:
        sql += " WHERE enabled=1"
    return [dict(r) for r in get_conn().execute(sql + " ORDER BY id").fetchall()]


def add_x_account(screen_name, display=""):
    with _lock:
        cur = get_conn().execute(
            "INSERT OR IGNORE INTO x_accounts (screen_name, display, enabled, created_at) VALUES (?,?,1,?)",
            (screen_name, display or screen_name, now()))
        get_conn().commit()
        return cur.rowcount > 0


def update_x_account(acc_id, screen_name=None, display=None, enabled=None, fetch_minutes=None):
    sets, args = [], []
    if screen_name is not None:
        sets.append("screen_name=?"); args.append(screen_name)
    if display is not None:
        sets.append("display=?"); args.append(display)
    if enabled is not None:
        sets.append("enabled=?"); args.append(int(enabled))
    if fetch_minutes is not None:
        sets.append("fetch_minutes=?"); args.append(max(0, int(fetch_minutes)))
    if not sets:
        return False
    args.append(int(acc_id))
    with _lock:
        get_conn().execute(f"UPDATE x_accounts SET {', '.join(sets)} WHERE id=?", args)
        get_conn().commit()
    return True


def delete_x_account(acc_id):
    with _lock:
        cur = get_conn().execute("DELETE FROM x_accounts WHERE id=?", (int(acc_id),))
        get_conn().commit()
        return cur.rowcount > 0


def ensure_default_x_accounts():
    if get_kv("x_accounts_seeded") != "1":
        add_x_account("DailyDarkWeb", "Dark Web Intelligence")
        set_kv("x_accounts_seeded", "1")


# ---------- RSS 源 ----------

def list_rss_sources(enabled_only=False):
    sql = "SELECT * FROM rss_sources"
    if enabled_only:
        sql += " WHERE enabled=1"
    return [dict(r) for r in get_conn().execute(sql + " ORDER BY id").fetchall()]


def add_rss_source(name, url):
    with _lock:
        cur = get_conn().execute(
            "INSERT OR IGNORE INTO rss_sources (name, url, enabled, created_at) VALUES (?,?,1,?)",
            (name, url, now()))
        get_conn().commit()
        return cur.rowcount > 0


def update_rss_source(src_id, name=None, url=None, enabled=None, fetch_minutes=None):
    sets, args = [], []
    if name is not None:
        sets.append("name=?"); args.append(name)
    if url is not None:
        sets.append("url=?"); args.append(url)
    if enabled is not None:
        sets.append("enabled=?"); args.append(int(enabled))
    if fetch_minutes is not None:
        sets.append("fetch_minutes=?"); args.append(int(fetch_minutes))
    if not sets:
        return True
    with _lock:
        get_conn().execute(f"UPDATE rss_sources SET {', '.join(sets)} WHERE id=?", args + [int(src_id)])
        get_conn().commit()
    return True


def delete_rss_source(src_id):
    with _lock:
        cur = get_conn().execute("DELETE FROM rss_sources WHERE id=?", (int(src_id),))
        get_conn().commit()
        return cur.rowcount > 0


# ---------- 推送 ----------

def add_push_log(channel, kind, status, detail="", content=""):
    with _lock:
        get_conn().execute(
            "INSERT INTO push_logs (channel, kind, status, detail, content, created_at) VALUES (?,?,?,?,?,?)",
            (channel, kind, status, detail, content[:6000], now()))
        get_conn().commit()


def list_push_logs(limit=20):
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM push_logs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()]


def last_push_for(channel):
    r = get_conn().execute(
        "SELECT kind, status, created_at FROM push_logs WHERE channel=? ORDER BY id DESC LIMIT 1",
        (channel,)).fetchone()
    return dict(r) if r else None


def cnt24_by_source(source_id):
    return get_conn().execute(
        "SELECT COUNT(*) AS n FROM tweets WHERE source_id=?"
        " AND created_at>=datetime('now','-24 hours','localtime')",
        (str(source_id),)).fetchone()["n"]


def push_stats_today():
    """今日推送统计（2026-11-09 J：真实/模拟分列，禁止模拟冒充真实成功率）。

    口径（单位=逻辑投递，非重试次数）：
      total      今日全部推送日志行
      ok         成功+模拟成功（兼容旧调用）
      sim        模拟次数（单列）
      real_ok    真实成功（= ok - sim）
      real_total 真实尝试（= total - sim）
      real_rate  真实成功率（real_total==0 → None：无真实样本，不得显示 100%）
    "今日"=本地自然日窗口，与"近24小时"是不同窗口，不强行要求相等。"""
    row = get_conn().execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN status IN ('成功','模拟成功') THEN 1 ELSE 0 END) AS ok,"
        " SUM(CASE WHEN status='模拟成功' THEN 1 ELSE 0 END) AS sim"
        " FROM push_logs WHERE date(created_at)=date('now','localtime')").fetchone()
    total = int(row["total"] or 0)
    ok = int(row["ok"] or 0)
    sim = int(row["sim"] or 0)
    real_total = total - sim
    real_ok = ok - sim
    return {"total": total, "ok": ok, "sim": sim,
            "real_ok": real_ok, "real_total": real_total,
            "real_rate": (round(real_ok / real_total * 1000) / 10) if real_total > 0 else None}


# ---------- kv ----------

def set_kv(k, v):
    with _lock:
        get_conn().execute("INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        get_conn().commit()


def get_kv(k):
    r = get_conn().execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


# ---------- 逐实例投递（第二批） ----------
# 状态：消息=待发送/发送中/成功/等待重试/永久失败/结果不明/模拟成功/部分成功/已取消
#       逻辑投递 deliveries=待发送/已过滤/成功/模拟成功/永久失败/结果不明/已取消


def delivery_candidates(iid, boundary, cap=500):
    """某实例尚未建投递记录的候选（ignored=0、入库时间≥边界、AI 门控由调用方过滤）。
    按 id 升序 = 最旧优先，配合持久记录天然分页，不饥饿；游标即 deliveries 表本身。"""
    return [_row_tweet(r) for r in get_conn().execute(
        "SELECT t.* FROM tweets t WHERE t.ignored=0 AND t.created_at>=?"
        " AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.tweet_id=t.id AND d.instance_id=?)"
        " ORDER BY t.id ASC LIMIT ?", (boundary, iid, int(cap))).fetchall()]


def create_delivery(tweet_id, iid, ckey, rule_id, snap, state, reason="", msg_id=None):
    with _lock:
        cur = get_conn().execute(
            "INSERT OR IGNORE INTO deliveries (tweet_id, instance_id, channel_key, state, msg_id,"
            " rule_id, rule_snapshot, reason, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (int(tweet_id), iid, ckey, state, msg_id, rule_id, snap, reason, now(), now()))
        get_conn().commit()
        return cur.rowcount > 0


def deliveries_pending(iid, limit=200):
    """待发送的逻辑投递（联推文取等级用于摘要排序/即时筛选）"""
    return [_row_tweet(r) for r in get_conn().execute(
        "SELECT d.id AS delivery_id, t.* FROM deliveries d JOIN tweets t ON t.id=d.tweet_id"
        " WHERE d.instance_id=? AND d.state='待发送'"
        " ORDER BY CASE t.ai_severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1"
        " WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, t.id ASC LIMIT ?",
        (iid, int(limit))).fetchall()]


def create_msg(iid, ckey, mtype, rule_id, snap, title, content, tweet_ids=None,
               report_task_id=None):
    with _lock:
        cur = get_conn().execute(
            "INSERT INTO delivery_msgs (instance_id, channel_key, msg_type, report_task_id,"
            " title, state, rule_id, rule_snapshot, content, created_at, updated_at)"
            " VALUES (?,?,?,?,?,'待发送',?,?,?,?,?)",
            (iid, ckey, mtype, report_task_id, title, rule_id, snap, content, now(), now()))
        mid = cur.lastrowid
        for tid in (tweet_ids or []):
            get_conn().execute("INSERT OR IGNORE INTO delivery_items (msg_id, tweet_id) VALUES (?,?)",
                               (mid, int(tid)))
        get_conn().commit()
        return mid


def link_deliveries(iid, tweet_ids, msg_id):
    """逻辑投递 ↔ 消息 关联（消息创建后回填 msg_id，终态镜像依赖它）"""
    with _lock:
        for tid in tweet_ids:
            get_conn().execute(
                "UPDATE deliveries SET msg_id=? WHERE tweet_id=? AND instance_id=?",
                (int(msg_id), int(tid), iid))
        get_conn().commit()


def claim_msg(msg_id, lease_seconds=120):
    """原子领取：仅 待发送/等待重试 可领取；领取后 发送中 + 租约 + owner 令牌。
    返回令牌（成功）或 None（已被其他工作者领取/状态不允许）。完成写回必须携带该令牌。"""
    token = uuid.uuid4().hex
    with _lock:
        cur = get_conn().execute(
            "UPDATE delivery_msgs SET state='发送中', lease_until=?, attempts=attempts+1,"
            " claim_token=?, updated_at=?"
            " WHERE id=? AND state IN ('待发送','等待重试')",
            (time.time() + lease_seconds, token, now(), int(msg_id)))
        if cur.rowcount == 0:
            get_conn().commit()
            return None
        get_conn().execute(
            "INSERT INTO delivery_attempts (msg_id, attempt_no, started_at)"
            " SELECT ?, attempts, ? FROM delivery_msgs WHERE id=?",
            (int(msg_id), now(), int(msg_id)))
        get_conn().commit()
    return token


def finish_msg(msg_id, state, detail="", mirror=True, token=None, next_attempt_at=None):
    """终态/退避写回。令牌规则：消息处于 发送中 时，必须匹配 claim 令牌，
    旧工作者（租约过期后被接管）写回会被拒绝（返回 False，不覆盖新状态）。"""
    with _lock:
        row = get_conn().execute(
            "SELECT state, claim_token, lease_until FROM delivery_msgs WHERE id=?", (int(msg_id),)).fetchone()
        if not row:
            return False
        if row["state"] == "发送中" and (
            (row["lease_until"] is not None and row["lease_until"] > time.time())
        ) and (token or "") != (row["claim_token"] or ""):
            return False  # 租约仍有效 → 仅持令牌者可写回；租约已过期 → 允许 sweep 收尸（结果不明）  # 令牌不匹配：旧工作者或并发写回，拒绝覆盖
        if row["state"] in ("成功", "模拟成功", "部分成功", "永久失败", "已取消"):
            return False  # 已终态，不覆盖
        get_conn().execute(
            "UPDATE delivery_msgs SET state=?, detail=?, lease_until=NULL, updated_at=? WHERE id=?",
            (state, detail[:400], now(), int(msg_id)))
        get_conn().execute(
            "UPDATE delivery_attempts SET finished_at=?, result=?, detail=?, next_attempt_at=?"
            " WHERE msg_id=? AND finished_at IS NULL",
            (now(), "simulated" if state == "模拟成功" else
             ("success" if state == "成功" else
              ("retry" if state == "等待重试" else
               ("permanent" if state == "永久失败" else
                ("unknown" if state == "结果不明" else state)))), detail[:200],
             next_attempt_at, int(msg_id)))
        if mirror and state in ("成功", "模拟成功", "永久失败", "结果不明"):
            get_conn().execute(
                "UPDATE deliveries SET state=?, msg_id=?, updated_at=? WHERE msg_id=?",
                (state, int(msg_id), now(), int(msg_id)))
        get_conn().commit()
        return True


def msg_retry_wait(msg_id, next_ts, detail="", token=None):
    with _lock:
        row = get_conn().execute(
            "SELECT state, claim_token FROM delivery_msgs WHERE id=?", (int(msg_id),)).fetchone()
        if not row or (row["state"] == "发送中" and (token or "") != (row["claim_token"] or "")):
            return False
        get_conn().execute(
            "UPDATE delivery_msgs SET state='等待重试', next_attempt_at=?, detail=?, lease_until=NULL,"
            " updated_at=? WHERE id=?",
            (next_ts, detail[:400], now(), int(msg_id)))
        get_conn().execute(
            "UPDATE delivery_attempts SET finished_at=?, result='retry', detail=?, next_attempt_at=?"
            " WHERE msg_id=? AND finished_at IS NULL",
            (now(), detail[:200], next_ts, int(msg_id)))
        get_conn().commit()
        return True


def msg_attempt_text(msg_id, text):
    with _lock:
        get_conn().execute("UPDATE delivery_msgs SET detail=?, updated_at=? WHERE id=?",
                           (text[:400], now(), int(msg_id)))
        get_conn().commit()


def msg_by_id(msg_id):
    r = get_conn().execute("SELECT * FROM delivery_msgs WHERE id=?", (int(msg_id),)).fetchone()
    return dict(r) if r else None


def msg_members(msg_id):
    return [r["tweet_id"] for r in get_conn().execute(
        "SELECT tweet_id FROM delivery_items WHERE msg_id=?", (int(msg_id),)).fetchall()]


def msgs_due(now_ts, limit=50):
    """到期重试（等待重试且到点）+ 租约过期的发送中（→结果不明，由调用方处理）"""
    due = [r["id"] for r in get_conn().execute(
        "SELECT id FROM delivery_msgs WHERE state='等待重试' AND next_attempt_at<=?"
        " ORDER BY id ASC LIMIT ?", (now_ts, int(limit))).fetchall()]
    expired = [r["id"] for r in get_conn().execute(
        "SELECT id FROM delivery_msgs WHERE state='发送中' AND (lease_until IS NULL OR lease_until<?)"
        " ORDER BY id ASC LIMIT ?", (now_ts, int(limit))).fetchall()]
    return due, expired


def msgs_report(report_task_id):
    """报告任务的全部投递消息（每实例一条）"""
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM delivery_msgs WHERE report_task_id=? ORDER BY id",
        (int(report_task_id),)).fetchall()]


def reset_sim_deliveries(iid):
    """模拟转真实：该实例 模拟成功 的逻辑投递重置为 待发送（真实资格仍在；受每轮上限限速）"""
    with _lock:
        cur = get_conn().execute(
            "UPDATE deliveries SET state='待发送', updated_at=? WHERE instance_id=? AND state='模拟成功'",
            (now(), iid))
        get_conn().commit()
        return cur.rowcount


def sync_pushed_compat(tweet_ids):
    """兼容回写：推文存在任一终态投递（成功/模拟成功/永久失败/结果不明/已过滤）→ pushed=1。
    仅用于旧代码回退防重发与历史展示，不再决定实例投递资格。"""
    if not tweet_ids:
        return
    ph = ",".join("?" * len(tweet_ids))
    with _lock:
        get_conn().execute(
            f"UPDATE tweets SET pushed=1 WHERE pushed=0 AND id IN ({ph})"
            " AND EXISTS (SELECT 1 FROM deliveries d WHERE d.tweet_id=tweets.id"
            " AND d.state IN ('成功','模拟成功','永久失败','结果不明','已过滤'))",
            [int(x) for x in tweet_ids])
        get_conn().commit()


def protected_tweet_ids():
    """删除入口保护集：收藏 + 报告引用 + 事件引用 + 在途投递成员 + 活跃写作候选引用"""
    rows = get_conn().execute(
        "SELECT id FROM tweets WHERE starred=1"
        " UNION SELECT tweet_id FROM report_refs"
        " UNION SELECT tweet_id FROM event_evidence"
        " UNION SELECT di.tweet_id FROM delivery_items di"
        " JOIN delivery_msgs m ON m.id=di.msg_id WHERE m.state IN ('待发送','发送中','等待重试')"
        " UNION SELECT j.value FROM writing_candidates,"
        " json_each(CASE WHEN substr(writing_candidates.tweet_ids,1,1)='['"
        " THEN writing_candidates.tweet_ids ELSE '[]' END) j"
        " WHERE writing_candidates.status IN ('待处理','写作中','暂缓','已转报告')"
    ).fetchall()
    return {r["id"] for r in rows}

# ---------- 后台 AI 任务（第三批） ----------


def create_ai_task(kind, params, total=0):
    with _lock:
        cur = get_conn().execute(
            "INSERT INTO ai_tasks (kind, params, total, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (kind, json.dumps(params or {}, ensure_ascii=False), int(total), "待处理", now(), now()))
        get_conn().commit()
        return cur.lastrowid


def claim_ai_task(task_id, lease_minutes=10):
    """原子领取 + 租约（处理中 10 分钟陈旧可被重新领取——重启恢复）"""
    with _lock:
        cur = get_conn().execute(
            "UPDATE ai_tasks SET status='处理中', claimed_at=?, lease_until=?, updated_at=?"
            " WHERE id=? AND (status='待处理'"
            "  OR (status='处理中' AND lease_until < ?))",
            (now(), time.time() + lease_minutes * 60, now(), int(task_id), time.time()))
        get_conn().commit()
        return cur.rowcount > 0


def finish_ai_task(task_id, status, done=0, fail=0, skip=0, note=""):
    with _lock:
        get_conn().execute(
            "UPDATE ai_tasks SET status=?, done=done+?, fail=fail+?, skip=skip+?,"
            " note=?, lease_until=NULL, updated_at=? WHERE id=?",
            (status, int(done), int(fail), int(skip), note[:200], now(), int(task_id)))
        get_conn().commit()


def list_ai_tasks(limit=20):
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM ai_tasks ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()]


def ai_task_items(task_id, kind, limit):
    """任务候选集：analyze_backlog=未研判+首败待重试；retry_failed=研判失败积压。均排除人工修订。"""
    # ai_modified 为 TEXT 列（默认 ''），必须兼容空串——`ai_modified=0` 会永久匹配不到（2026-11-08 修复）
    not_modified = "(ai_modified IS NULL OR ai_modified='' OR ai_modified='0')"
    if kind == "retry_failed":
        rows = get_conn().execute(
            f"SELECT id FROM tweets WHERE ai_status='error' AND {not_modified}"
            " ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    else:
        rows = get_conn().execute(
            f"SELECT id FROM tweets WHERE (ai_status='' OR (ai_status='error' AND ai_retries<2))"
            f" AND {not_modified} ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [r["id"] for r in rows]


def reevaluate_filtered():
    """规则/过滤变更后，已过滤投递重新进入待评估（下轮按新规则重新判定，不丢失）"""
    with _lock:
        cur = get_conn().execute(
            "UPDATE deliveries SET state='待发送', reason='规则变更重新评估', updated_at=?"
            " WHERE state='已过滤'", (now(),))
        get_conn().commit()
        return cur.rowcount

# ---------- 事件聚合（第六批） ----------

EVENT_BASIS_RANK = {"manual": 3, "official_same": 2, "victim_same": 1, "shared_indicator": 0}


def create_event(title, summary="", severity="", operator="system", tweet_ids=None,
                 basis="manual"):
    """创建事件并可选关联证据。返回 (event_uid, version)。
    建事件/证据/审计/指纹在同事务内（2026-11-09 E）。"""
    import secrets as _secrets
    uid = "evt_" + _secrets.token_hex(6)
    with _lock:
        c = get_conn()
        c.execute(
            "INSERT INTO events (event_uid, title, summary, status, version, severity,"
            " first_seen, last_seen, created_at, updated_at) VALUES (?,?,?,'候选',1,?,?,?,?,?)",
            (uid, title[:200], summary[:1000], severity[:20],
             now(), now(), now(), now()))
        for tid in (tweet_ids or []):
            c.execute("INSERT OR IGNORE INTO event_evidence"
                      " (event_uid, tweet_id, basis, source_kind, note, created_at)"
                      " VALUES (?,?,?,'original','',?)", (uid, int(tid), basis, now()))
        c.execute("INSERT INTO event_revisions (event_uid, version, action, detail, operator,"
                  " created_at) VALUES (?,1,'create',?,?,?)",
                  (uid, f"title={title[:80]} basis={basis} n={len(tweet_ids or [])}",
                   operator[:60], now()))
        _refresh_event_times(uid)
        c.execute("UPDATE events SET evidence_fingerprint=? WHERE event_uid=?",
                  (evidence_fingerprint(uid), uid))
        c.commit()
    return uid, 1


def get_event(uid, conn=None):
    r = (conn or get_conn()).execute("SELECT * FROM events WHERE event_uid=?", (uid,)).fetchone()
    return dict(r) if r else None


def list_events(status="", q="", limit=50, offset=0):
    conds, args = [], []
    if status and status != "all":
        conds.append("e.status=?")
        args.append(status)
    if q:
        conds.append("(e.title LIKE ? OR e.summary LIKE ?)")
        args += [f"%{q}%"] * 2
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    c = get_conn()
    total = c.execute("SELECT COUNT(*) n FROM events e" + where, args).fetchone()["n"]
    rows = c.execute(
        "SELECT e.*, (SELECT COUNT(*) FROM event_evidence v WHERE v.event_uid=e.event_uid) n_evidence,"
        " (SELECT COUNT(*) FROM event_evidence v WHERE v.event_uid=e.event_uid"
        "   AND v.source_kind='original') n_original"
        " FROM events e" + where + " ORDER BY e.updated_at DESC, e.id DESC LIMIT ? OFFSET ?",
        args + [int(limit), int(offset)]).fetchall()
    return total, [dict(r) for r in rows]


def event_evidence(uid, conn=None):
    """证据列表（联推文内容 + 完整性分级），用于详情页。

    身份契约（2026-11-09 缺陷 D 修复）：
      - evidence_id = event_evidence 关联表行 id（仅审计用途）
      - tweet_id    = tweets.id（解除/拆分/跳转/导出必须使用这一身份）
    历史 SQL 用 v.* 导致关联表 id 以 "id" 键泄漏、created_at 同名歧义——
    前端与导出误用关联表 id（推文 4091/4092 显示为证据 1/2），解除/拆分/导出
    全部引用错误。现改为全列唯一别名。"""
    rows = (conn or get_conn()).execute(
        "SELECT v.id AS evidence_id, v.event_uid, v.basis, v.source_kind,"
        " v.note, v.created_at AS linked_at,"
        " t.id AS tweet_id, t.screen_name, t.text, t.title_zh, t.text_zh, t.url,"
        " t.published_at, t.created_at AS collected_at, t.text_hash, t.ai_severity, t.ai_type,"
        " t.category, t.geo_country, t.geo_province, t.geo_city, t.industry, t.ai_integrity,"
        " t.ai_review, t.ai_review_version, t.ai_review_at, t.analysis_version,"
        " t.org, t.tags, t.ai_iocs, t.ai_assessment,"
        " t.ai_confidence, t.ai_repair"
        " FROM event_evidence v JOIN tweets t ON t.id=v.tweet_id"
        " WHERE v.event_uid=? ORDER BY v.id", (uid,)).fetchall()
    return [_row_tweet(r) for r in rows]


def evidence_stats(uid, conn=None):
    """独立证据统计：original 才算独立；repost 单列（转载不冒充独立证据）"""
    rows = (conn or get_conn()).execute(
        "SELECT source_kind, COUNT(*) n FROM event_evidence WHERE event_uid=?"
        " GROUP BY source_kind", (uid,)).fetchall()
    d = {r["source_kind"]: r["n"] for r in rows}
    return {"original": d.get("original", 0), "repost": d.get("repost", 0),
            "translation": d.get("translation", 0), "total": sum(d.values())}


def _bump_event(uid, action, detail, operator):
    with _lock:
        return _bump_event_locked(uid, action, detail, operator)


def _bump_event_locked(uid, action, detail, operator):
    """版本推进 + 审计（**调用方必须已持有 _lock**；与内容修改同一事务提交）。"""
    c = get_conn()
    c.execute("UPDATE events SET version=version+1, updated_at=? WHERE event_uid=?",
              (now(), uid))
    v = c.execute("SELECT version FROM events WHERE event_uid=?", (uid,)).fetchone()["version"]
    c.execute("INSERT INTO event_revisions (event_uid, version, action, detail, operator,"
              " created_at) VALUES (?,?,?,?,?,?)",
              (uid, v, action, detail[:500], operator[:60], now()))
    return v


def evidence_fingerprint(uid, conn=None):
    """事件证据内容指纹：成员集合 + 每条证据当前 analysis_version。

    底层情报（AI 结果落地/人工修订/原文更新都会推进 analysis_version）一经变化，
    指纹随之变化——事件审核与版本据此判定"内容已变化"，旧审核自动成为历史决定。"""
    c = conn or get_conn()
    rows = c.execute(
        "SELECT v.tweet_id, COALESCE(t.analysis_version,0) av, v.basis, v.source_kind"
        " FROM event_evidence v LEFT JOIN tweets t ON t.id=v.tweet_id"
        " WHERE v.event_uid=? ORDER BY v.tweet_id", (uid,)).fetchall()
    import hashlib as _hashlib
    h = _hashlib.sha1()
    for r in rows:
        h.update(f"{r['tweet_id']}:{r['av']}:{r['basis']}:{r['source_kind']};".encode())
    return h.hexdigest()


def ensure_event_freshness(uid, operator="system"):
    """底层情报变化检测：指纹不符 → 推进事件版本 + 留痕 + 刷新存储指纹。

    幂等：指纹一致时不做任何写。读路径（详情/导出）调用后，旧审核
    （绑定旧 event_version）自动失效为历史决定。返回当前版本。"""
    with _lock:
        ev = get_conn().execute("SELECT version, evidence_fingerprint FROM events"
                                " WHERE event_uid=?", (uid,)).fetchone()
        if not ev:
            return 0
        cur_fp = evidence_fingerprint(uid)
        if (ev["evidence_fingerprint"] or "") == cur_fp:
            return int(ev["version"] or 0)
        v = _bump_event_locked(uid, "evidence_changed",
                               f"底层情报内容变化（指纹 {cur_fp[:12]}）", operator)
        get_conn().execute("UPDATE events SET evidence_fingerprint=? WHERE event_uid=?",
                           (cur_fp, uid))
        get_conn().commit()
        return v


def _refresh_event_times(uid):
    """重建事件首末时间。**调用方必须已持有 _lock**（_lock 不可重入，此处不再加锁）。"""
    r = get_conn().execute(
        "SELECT MIN(t.published_at) a, MAX(t.published_at) b FROM event_evidence v"
        " JOIN tweets t ON t.id=v.tweet_id WHERE v.event_uid=?", (uid,)).fetchone()
    if r and r["a"]:
        get_conn().execute("UPDATE events SET first_seen=?, last_seen=? WHERE event_uid=?",
                           (r["a"] or "", r["b"] or "", uid))
        get_conn().commit()


def add_evidence(uid, tweet_id, basis="manual", source_kind="original", note="",
                 operator="system"):
    """添加证据。返回 (ok, event_version)。共享标识依据会被强制降级为候选不可自动合并。"""
    if basis not in EVENT_BASIS_RANK:
        return False, 0
    if not get_event(uid) or not get_tweet(tweet_id):
        return False, 0
    with _lock:
        cur = get_conn().execute(
            "INSERT OR IGNORE INTO event_evidence"
            " (event_uid, tweet_id, basis, source_kind, note, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (uid, int(tweet_id), basis, source_kind, note[:200], now()))
        if cur.rowcount == 0:
            get_conn().commit()
            return False, get_event(uid)["version"]
        v = _bump_event_locked(uid, "update", f"add_evidence tweet={tweet_id} basis={basis}"
                                               f" kind={source_kind}", operator)
        _refresh_event_times(uid)
        get_conn().execute("UPDATE events SET evidence_fingerprint=? WHERE event_uid=?",
                           (evidence_fingerprint(uid), uid))
        get_conn().commit()
    return True, v


def remove_evidence(uid, tweet_id, operator="system"):
    with _lock:
        cur = get_conn().execute("DELETE FROM event_evidence WHERE event_uid=? AND tweet_id=?",
                                 (uid, int(tweet_id)))
        if cur.rowcount == 0:
            get_conn().commit()
            return False, get_event(uid)["version"] if get_event(uid) else 0
        v = _bump_event_locked(uid, "unlink", f"remove_evidence tweet={tweet_id}", operator)
        _refresh_event_times(uid)
        get_conn().execute("UPDATE events SET evidence_fingerprint=? WHERE event_uid=?",
                           (evidence_fingerprint(uid), uid))
        get_conn().commit()
    return True, v


def merge_events(src_uid, dst_uid, operator="system", reason=""):
    """人工合并：把 src 的证据并入 dst（重复 tweet 保留 dst 的记录），src 置已归档"""
    if src_uid == dst_uid:
        return False, ""
    src, dst = get_event(src_uid), get_event(dst_uid)
    if not src or not dst:
        return False, ""
    # 单事务（2026-11-09 E）：证据并入 + dst 版本推进 + src 归档 + 双向指纹/时间刷新
    with _lock:
        c = get_conn()
        for r in c.execute("SELECT tweet_id, basis, source_kind, note FROM event_evidence"
                           " WHERE event_uid=?", (src_uid,)).fetchall():
            c.execute("INSERT OR IGNORE INTO event_evidence"
                      " (event_uid, tweet_id, basis, source_kind, note, created_at)"
                      " VALUES (?,?,?,?,?,?)",
                      (dst_uid, r["tweet_id"], r["basis"], r["source_kind"],
                       (r["note"] or "") + f" [merged from {src_uid}]", now()))
        _bump_event_locked(dst_uid, "merge", f"merge {src_uid} -> {dst_uid}: {reason}", operator)
        c.execute(
            "UPDATE events SET status='已归档', updated_at=? WHERE event_uid=?",
            (now(), src_uid))
        _bump_event_locked(src_uid, "merge_out", f"merged into {dst_uid}: {reason}", operator)
        _refresh_event_times(dst_uid)
        for u in (dst_uid, src_uid):
            c.execute("UPDATE events SET evidence_fingerprint=? WHERE event_uid=?",
                      (evidence_fingerprint(u), u))
        c.commit()
    return True, dst_uid


def split_event(uid, tweet_ids, new_title, operator="system"):
    """人工拆分：指定证据移出到新事件。

    原子语义（2026-11-09 D/E 修复）：校验、建事件、移动、双向版本推进、
    指纹刷新全部在**同一事务**内——零有效成员直接失败不建空事件；
    中途任何一步失败整体不落库（不留半拆分状态）。传入必须是本事件的
    真实 tweet_id（历史上前端误传关联表 id → 零命中 → 却已建空事件报成功）。"""
    ev = get_event(uid)
    if not ev:
        return False, ""
    import secrets as _secrets
    new_uid = "evt_" + _secrets.token_hex(6)
    with _lock:
        c = get_conn()
        valid_ids = []
        for tid in (tweet_ids or []):
            try:
                tid = int(tid)
            except Exception:
                continue
            r = c.execute("SELECT 1 FROM event_evidence WHERE event_uid=? AND tweet_id=?",
                          (uid, tid)).fetchone()
            if r:
                valid_ids.append(tid)
        if not valid_ids:
            return False, ""
        # 建目标事件（同事务，不经 create_event——其内部加锁会死锁）
        c.execute(
            "INSERT INTO events (event_uid, title, summary, status, version, severity,"
            " first_seen, last_seen, created_at, updated_at) VALUES (?,?,?,'候选',1,?,?,?,?,?)",
            (new_uid, (new_title or (ev["title"] + "（拆分）"))[:200], ev.get("summary", "")[:1000],
             ev.get("severity", "")[:20], now(), now(), now(), now()))
        c.execute("INSERT INTO event_revisions (event_uid, version, action, detail, operator,"
                  " created_at) VALUES (?,1,'create',?,?,?)",
                  (new_uid, f"split from {uid}", operator[:60], now()))
        moved = 0
        for tid in valid_ids:
            r = c.execute("SELECT * FROM event_evidence WHERE event_uid=? AND tweet_id=?",
                          (uid, int(tid))).fetchone()
            if not r:  # 锁内复验（校验与移动之间无窗口）
                continue
            c.execute("INSERT OR IGNORE INTO event_evidence"
                      " (event_uid, tweet_id, basis, source_kind, note, created_at)"
                      " VALUES (?,?,?,?,?,?)",
                      (new_uid, int(tid), r["basis"], r["source_kind"],
                       (r["note"] or "") + f" [split from {uid}]", now()))
            c.execute("DELETE FROM event_evidence WHERE event_uid=? AND tweet_id=?",
                      (uid, int(tid)))
            moved += 1
        if moved == 0:  # 双保险：不产生空事件
            c.execute("DELETE FROM events WHERE event_uid=?", (new_uid,))
            c.commit()
            return False, ""
        _bump_event_locked(uid, "split", f"split out {moved} evidence -> {new_uid}", operator)
        _bump_event_locked(new_uid, "split", f"split in {moved} evidence from {uid}", operator)
        _refresh_event_times(uid)
        _refresh_event_times(new_uid)
        for u in (uid, new_uid):
            c.execute("UPDATE events SET evidence_fingerprint=? WHERE event_uid=?",
                      (evidence_fingerprint(u), u))
        c.commit()
    return True, new_uid


def update_event(uid, title=None, summary=None, severity=None, status=None, operator="system",
                 expected_version=None):
    """更新事件元数据。expected_version 提供时做乐观锁。

    原子语义（2026-11-09 E 修复）：版本检查、内容修改、版本推进、审计
    在**同一事务**内完成——历史上检查在锁外读、UPDATE 无版本条件，
    两个相同 expected_version 的并发修改都会"成功"并互相覆盖。"""
    sets, args = [], []
    for k, v in (("title", title), ("summary", summary), ("severity", severity),
                 ("status", status)):
        if v is not None:
            sets.append(f"{k}=?" if k != "status" else "status=?")
            args.append(str(v)[:1000])
    if not sets:
        ev = get_event(uid)
        return False, int(ev["version"]) if ev else 0
    with _lock:
        c = get_conn()
        if expected_version is not None:
            cond = (f"UPDATE events SET {{}}, version=version+1, updated_at=?"
                    " WHERE event_uid=? AND version=?")
            cur = c.execute(cond.format(", ".join(sets)),
                            args + [now(), uid, int(expected_version)])
            if cur.rowcount == 0:
                c.commit()
                ev = get_event(uid)
                return False, int(ev["version"]) if ev else 0
            v = int(expected_version) + 1
        else:
            c.execute(f"UPDATE events SET {{}}, version=version+1, updated_at=?"
                      " WHERE event_uid=?".format(", ".join(sets)),
                      args + [now(), uid])
            v = c.execute("SELECT version FROM events WHERE event_uid=?",
                          (uid,)).fetchone()["version"]
        c.execute("INSERT INTO event_revisions (event_uid, version, action, detail, operator,"
                  " created_at) VALUES (?,?,?,?,?,?)",
                  (uid, v, "update", "fields=" + ",".join(x.split("=")[0] for x in sets),
                   operator[:60], now()))
        c.commit()
    return True, v


def event_revisions(uid, conn=None):
    return [dict(r) for r in (conn or get_conn()).execute(
        "SELECT * FROM event_revisions WHERE event_uid=? ORDER BY version DESC, id DESC",
        (uid,)).fetchall()]


def candidate_events(tweet_id, limit=10):
    """候选关联（**不自动合并**）：仅共享 CVE/IP/组织名/受害单位等弱信号。
    返回 [{event_uid,title,reasons}]，由人工决定是否关联。"""
    t = get_tweet(tweet_id)
    if not t:
        return []
    import json as _json
    def _arr(v):
        try:
            return [str(x) for x in _json.loads(v or "[]")]
        except Exception:
            return []
    my_iocs = set(_arr(t.get("ai_iocs")))
    my_orgs = set(_arr(t.get("org")))
    my_actors = set(_arr(t.get("ai_actors")))
    out = []
    for ev in get_conn().execute(
            "SELECT * FROM events WHERE status!='已归档' ORDER BY updated_at DESC LIMIT 100").fetchall():
        ev = dict(ev)
        reasons = []
        ev_tweets = [r["tweet_id"] for r in get_conn().execute(
            "SELECT tweet_id FROM event_evidence WHERE event_uid=?", (ev["event_uid"],)).fetchall()]
        if tweet_id in ev_tweets:
            continue   # 已在本事件中
        if ev_tweets:
            rows = get_conn().execute(
                f"SELECT ai_iocs, org, ai_actors FROM tweets WHERE id IN"
                f" ({','.join('?' * len(ev_tweets))})", ev_tweets).fetchall()
            ev_iocs, ev_orgs, ev_actors = set(), set(), set()
            for r in rows:
                ev_iocs |= set(_arr(r["ai_iocs"]))
                ev_orgs |= set(_arr(r["org"]))
                ev_actors |= set(_arr(r["ai_actors"]))
            common_ioc = my_iocs & ev_iocs
            common_org = my_orgs & ev_orgs
            common_actor = my_actors & ev_actors
            if common_ioc:
                reasons.append("共享指标：" + "、".join(sorted(common_ioc)[:3]))
            if common_org:
                reasons.append("共享单位：" + "、".join(sorted(common_org)[:3]))
            if common_actor:
                reasons.append("共享行为体：" + "、".join(sorted(common_actor)[:3]))
        if reasons:
            out.append({"event_uid": ev["event_uid"], "title": ev["title"],
                        "reasons": reasons,
                        "basis": "shared_indicator",
                        "auto_merge": False})   # 明确：共享标识不得自动合并
    return out[:limit]

def review_event(uid, event_version, decision, note="", operator="system"):
    """审核绑定事件版本 + 证据指纹。返回 (ok, reason)。

    规则（2026-11-09 E 收紧）：
      - 版本检查在锁内复验（读-判-写原子）：旧版本不能批准新内容
      - 同时绑定当前证据指纹——底层情报再变（ensure_event_freshness 推进版本）
        后旧决定自动成为历史，不得冒充当前批准
      - 同一版本只能有一条决定（UNIQUE(event_uid,event_version) 幂等）
    """
    if decision not in ("approved", "rejected"):
        return False, "bad_decision"
    with _lock:
        c = get_conn()
        ev = c.execute("SELECT version FROM events WHERE event_uid=?", (uid,)).fetchone()
        if not ev:
            return False, "event_not_found"
        if int(event_version) != int(ev["version"]):
            return False, f"version_mismatch(current={ev['version']})"
        fp = evidence_fingerprint(uid)
        try:
            c.execute(
                "INSERT INTO event_reviews (event_uid, event_version, decision,"
                " evidence_fingerprint, note, operator, created_at) VALUES (?,?,?,?,?,?,?)",
                (uid, int(event_version), decision, fp, note[:500], operator[:60], now()))
            c.commit()
        except Exception:
            return False, "already_reviewed"
    return True, "ok"


def event_review_state(uid, conn=None):
    """事件审核有效性三态（导出/详情统一口径）。

    返回 dict：{state: valid|stale|none, review: 最新决定或 None,
                current_version, reviewed_version, fingerprint_matched}
    valid = 决定绑定当前版本且证据指纹一致；否则 stale（历史决定）。"""
    c = conn or get_conn()
    ev = c.execute("SELECT version, evidence_fingerprint FROM events WHERE event_uid=?",
                   (uid,)).fetchone()
    if not ev:
        return {"state": "none", "review": None, "current_version": 0,
                "reviewed_version": None, "fingerprint_matched": False}
    cur_ver = int(ev["version"] or 0)
    cur_fp = ev["evidence_fingerprint"] or ""
    rev = c.execute("SELECT * FROM event_reviews WHERE event_uid=?"
                    " ORDER BY event_version DESC, id DESC LIMIT 1", (uid,)).fetchone()
    if not rev:
        return {"state": "none", "review": None, "current_version": cur_ver,
                "reviewed_version": None, "fingerprint_matched": cur_fp == ""}
    rev_v = int(rev["event_version"] or 0)
    # 保守原则：事件已指纹化但审核未绑定指纹（历史数据）→ 无法证明审的是当前内容 → stale
    fp_match = (not cur_fp) or (rev["evidence_fingerprint"] or "") == cur_fp
    state = "valid" if (rev_v == cur_ver and fp_match) else "stale"
    return {"state": state, "review": dict(rev), "current_version": cur_ver,
            "reviewed_version": rev_v, "fingerprint_matched": bool(fp_match)}


def event_review_latest(uid, conn=None):
    r = (conn or get_conn()).execute(
        "SELECT * FROM event_reviews WHERE event_uid=? ORDER BY event_version DESC, id DESC LIMIT 1",
        (uid,)).fetchone()
    return dict(r) if r else None


def event_reviews(uid):
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM event_reviews WHERE event_uid=? ORDER BY event_version DESC, id DESC",
        (uid,)).fetchall()]


def review_queue(status="", limit=50):
    """审核队列：待审核（无当前版本审核决定）+ 未归档事件"""
    rows = get_conn().execute(
        "SELECT e.*, (SELECT COUNT(*) FROM event_evidence v WHERE v.event_uid=e.event_uid) n_evidence,"
        " (SELECT decision FROM event_reviews r WHERE r.event_uid=e.event_uid"
        "   AND r.event_version=e.version LIMIT 1) current_decision"
        " FROM events e WHERE e.status!='已归档'"
        " ORDER BY e.updated_at DESC LIMIT ?", (int(limit),)).fetchall()
    items = [dict(r) for r in rows]
    if status == "pending":
        items = [x for x in items if not x.get("current_decision")]
    elif status == "approved":
        items = [x for x in items if x.get("current_decision") == "approved"]
    return items


def event_events_for_tweet(tweet_id):
    """某情报所属事件（删除保护用）"""
    return [r["event_uid"] for r in get_conn().execute(
        "SELECT event_uid FROM event_evidence WHERE tweet_id=?", (int(tweet_id),)).fetchall()]
