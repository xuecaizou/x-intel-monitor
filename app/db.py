"""SQLite 存储层：tweets(含 AI 结果列) / x_accounts / push_logs / kv / fetch_logs / report_tasks"""
import datetime
import json
import os
import sqlite3
import threading

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
        """)
        os.makedirs(os.path.join(DATA_DIR, "report_tasks"), exist_ok=True)
        # 早期建库可能缺列，逐列补齐（已存在则忽略报错）
        added_pushed = False
        for col in ("ai_status", "ai_error", "ai_model", "ai_severity", "ai_type",
                    "ai_actors", "ai_iocs", "ai_assessment", "ai_recommendations",
                    "ai_confidence", "ai_analyzed_at", "title_zh", "text_zh", "translated_at",
                    "geo_country", "geo_province", "geo_city", "org", "industry",
                    "geo_basis", "tags", "media_url", "starred", "read", "ignored", "pushed", "ai_retries",
                    "category"):
            try:
                get_conn().execute(f"ALTER TABLE tweets ADD COLUMN {col} "
                                   + ("INTEGER DEFAULT 0" if col in ("ai_confidence", "starred", "read", "ignored", "pushed", "ai_retries")
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
    """幂等入库；成功返回行 id，重复返回 None"""
    with _lock:
        cur = get_conn().execute(
            "INSERT OR IGNORE INTO tweets (screen_name, tweet_id, text, url, published_at, is_retweet, is_reply, media_url, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (t["screen_name"], t["tweet_id"], t.get("text", ""), t.get("url", ""),
             t.get("published_at", ""), int(t.get("is_retweet", 0)), int(t.get("is_reply", 0)),
             t.get("media_url", ""), now()))
        get_conn().commit()
        if cur.rowcount > 0:
            return cur.lastrowid
        return None


def get_tweet(tid):
    r = get_conn().execute("SELECT * FROM tweets WHERE id=?", (int(tid),)).fetchone()
    return dict(r) if r else None


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
           "delete": f"DELETE FROM tweets WHERE id IN ({ph})"}.get(op)
    if not sql:
        return 0
    with _lock:
        cur = get_conn().execute(sql, ids)
        get_conn().commit()
        return cur.rowcount


def cleanup_old(keep_days):
    """一键清理：删除 keep_days 天前的推文（收藏的永久保留），返回删除数"""
    with _lock:
        cur = get_conn().execute(
            "DELETE FROM tweets WHERE starred=0 AND created_at < datetime('now','localtime',?)",
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
    """未成功研判且未尝试过的推文（避免对反复失败的推文重复烧钱）"""
    return [_row_tweet(r) for r in get_conn().execute(
        "SELECT * FROM tweets WHERE ai_status='' ORDER BY id DESC LIMIT ?", (int(limit_n),)).fetchall()]


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


def push_stats_today():
    """今日推送统计（口径：ok=成功+模拟成功；sim 单列模拟次数，供首页区分真实/模拟口径）"""
    row = get_conn().execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN status IN ('成功','模拟成功') THEN 1 ELSE 0 END) AS ok,"
        " SUM(CASE WHEN status='模拟成功' THEN 1 ELSE 0 END) AS sim"
        " FROM push_logs WHERE date(created_at)=date('now','localtime')").fetchone()
    return int(row["ok"] or 0), int(row["total"] or 0), int(row["sim"] or 0)


# ---------- kv ----------

def set_kv(k, v):
    with _lock:
        get_conn().execute("INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        get_conn().commit()


def get_kv(k):
    r = get_conn().execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None
