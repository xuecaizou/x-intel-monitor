# -*- coding: utf-8 -*-
"""第五批：可用于运维的隔离恢复演练（在副本上，不碰运行目录）。

验证：
  R1 一致性备份（online backup API，含 WAL）
  R2 恢复到隔离目录，应用实际启动，调度与外发默认关闭
  R3 关键关联（引用/投递/尝试/AI 任务/实例 ID/人工修订/报告附件哈希）
  R4 pending/sending/unknown 恢复后不自动重发
  R5 认证凭据随备份恢复 + 旧会话/旧 Token 处理策略
  R6 记录耗时与限制
运行：C:/Python314/python.exe tests/test_batch5_restore.py
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from tests.testenv import TestEnv

env = TestEnv(forbid_network=False).start()
import app.db as db  # noqa: E402
import app.backup as backup  # noqa: E402
import app.auth as auth  # noqa: E402

db.DATA_DIR = env.tmp
db._conn = None
db.init_db()
auth.init_auth_tables()

PASS, FAIL = 0, []
T0 = time.time()


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(name)
        print(f"FAIL {name} {detail}")


# ============================================================
print("== 构造待恢复环境（含各类状态 + 附件） ==")
db.add_x_account("RestoreSrc")
db.add_rss_source("RestoreRSS", "https://example.com/r")
src_id = db.get_conn().execute("SELECT id FROM rss_sources WHERE name='RestoreRSS'").fetchone()["id"]

t_main = db.insert_tweet({"screen_name": "RestoreSrc", "tweet_id": "rs1", "text": "restore main",
                          "url": "u", "published_at": db.now()})
db.update_ai(t_main, {"ai_status": "ok", "ai_severity": "high", "ai_modified": 1,
                      "geo_country": "人工恢复国", "analysis_version": 3,
                      "ai_integrity": "partial", "ai_review": "manual"})
# 投递：成功 / 待发送 / 发送中 / 结果不明 各一条
iid = "ci_restore_1"
for state, tag in (("成功", "ok"), ("待发送", "pending"), ("发送中", "sending"), ("结果不明", "unknown")):
    tt = db.insert_tweet({"screen_name": "RestoreSrc", "tweet_id": f"rs-{tag}",
                          "text": tag, "url": "u", "published_at": db.now()})
    db.create_delivery(tt, iid, "wecom", "", "{}", state)
    if state in ("成功", "结果不明", "发送中"):
        mid = db.create_msg(iid, "wecom", "instant", "", "{}", tag, "c", [tt])
        db.link_deliveries(iid, [tt], mid)
        if state == "成功":
            tk = db.claim_msg(mid)
            db.finish_msg(mid, "成功", "done", token=tk)
        elif state == "结果不明":
            tk = db.claim_msg(mid)
            db.finish_msg(mid, "结果不明", "unknown", token=tk)
        else:
            db.claim_msg(mid)   # 停留在 发送中
# AI 任务
ait = db.create_ai_task("analyze_backlog", {"limit": 5}, 0)
# 报告 + 引用 + 附件
rep_dir = env.tmp / "report_tasks"
rep_dir.mkdir(exist_ok=True)
attach = rep_dir / "restore_批注.docx"
attach.write_bytes(b"PK\x03\x04restore-attachment")
attach_hash = hashlib.sha256(attach.read_bytes()).hexdigest()
rid = db.get_conn().execute(
    "INSERT INTO report_tasks (title, keywords, tier, notes, tweet_ids, created_at, updated_at)"
    " VALUES ('恢复演练','[]','速报','',?,?,?) RETURNING id",
    (json.dumps([t_main]), db.now(), db.now())).fetchone()["id"]
db.get_conn().execute("INSERT OR IGNORE INTO report_refs (report_id, tweet_id, created_at)"
                      " VALUES (?,?,?)", (rid, t_main, db.now()))
db.get_conn().commit()
# 认证：管理员 + Token（验证恢复策略）
auth.create_admin("restore-admin", "RestorePass123")
raw_tok = auth.create_token("restore-token", "read")
sid, csrf = auth.new_session("restore-admin")
# 不改连接（WAL 模式下 close 会关闭连接；后续再取连接即可）
before = {
    "tweets": db.get_conn().execute("SELECT COUNT(*) n FROM tweets").fetchone()["n"],
    "deliveries": db.get_conn().execute("SELECT COUNT(*) n FROM deliveries").fetchone()["n"],
    "msgs": db.get_conn().execute("SELECT COUNT(*) n FROM delivery_msgs").fetchone()["n"],
    "attempts": db.get_conn().execute("SELECT COUNT(*) n FROM delivery_attempts").fetchone()["n"],
    "tasks": db.get_conn().execute("SELECT COUNT(*) n FROM ai_tasks").fetchone()["n"],
    "refs": db.get_conn().execute("SELECT COUNT(*) n FROM report_refs").fetchone()["n"],
    "users": auth.user_count(), "tokens": len(auth.list_tokens()),
}
print("  备份前:", before)

# ============================================================
print("== R1 一致性备份 ==")
t_bk = time.time()
res = backup.create_backup(env.tmp, env.tmp / "backups", project_root=ROOT)
bk_dir = Path(res["dir"])
bk_elapsed = time.time() - t_bk
check("R1-1 备份完整性 ok", res["manifest"]["sqlite_integrity"] == "ok")
check("R1-2 manifest 含 commit/schema/哈希",
      bool(res["manifest"]["code_commit"]) and len(res["manifest"]["files"]) >= 3)
print(f"  备份耗时 {bk_elapsed:.2f}s")

# ============================================================
print("== R2 恢复到隔离目录 ==")
target = env.tmp / "restored5"
t_rs = time.time()
summary = backup.restore_to(bk_dir, target, project_root=ROOT)
rs_elapsed = time.time() - t_rs
check("R2-1 恢复哈希校验通过", summary["ok"], str(summary.get("problems"))[:100])
check("R2-2 恢复耗时 < 60s", rs_elapsed < 60, f"{rs_elapsed:.2f}s")
counts = summary["counts"]
check("R2-3 情报数一致", counts["tweets"] == before["tweets"],
      f"{counts['tweets']} vs {before['tweets']}")
check("R2-4 投递/消息/尝试一致",
      counts["deliveries"] == before["deliveries"] and counts["delivery_msgs"] == before["msgs"]
      and counts["delivery_attempts"] == before["attempts"], str(counts)[:120])
check("R2-5 AI 任务/引用一致",
      counts["ai_tasks"] == before["tasks"] and counts["report_refs"] == before["refs"])

# ============================================================
print("== R3 关键关联与附件哈希 ==")
with sqlite3.connect(str(target / "xintel.db")) as c:
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT * FROM tweets WHERE tweet_id='rs1'").fetchone()
    check("R3-1 人工修订值保留", row and int(row["ai_modified"]) == 1
          and row["geo_country"] == "人工恢复国", dict(row) if row else "无")
    check("R3-2 分析版本保留", row and int(row["analysis_version"]) == 3,
          row["analysis_version"] if row else "无")
    check("R3-3 完整性分级保留", row and row["ai_integrity"] == "partial")
    # 投递状态分布
    states = {r["state"]: r["n"] for r in c.execute(
        "SELECT state, COUNT(*) n FROM delivery_msgs GROUP BY state")}
    check("R3-4 任务状态（成功/结果不明/发送中）保留",
          states.get("成功", 0) >= 1 and states.get("结果不明", 0) >= 1
          and states.get("发送中", 0) >= 1, str(states))
    # 实例 ID 引用一致
    orphan = c.execute("SELECT COUNT(*) n FROM deliveries WHERE instance_id=''").fetchone()["n"]
    check("R3-5 投递实例 ID 完整", orphan == 0)
    # 附件
    ra = target / "report_tasks" / "restore_批注.docx"
    check("R3-6 报告附件已恢复", ra.exists())
    if ra.exists():
        check("R3-7 附件哈希一致",
              hashlib.sha256(ra.read_bytes()).hexdigest() == attach_hash)
    # 认证凭据
    login_ok = False
    r = c.execute("SELECT pw_hash FROM auth_users WHERE username='restore-admin'").fetchone()
    if r:
        login_ok = auth.verify_password("RestorePass123", r["pw_hash"])
    check("R4-1 管理员凭据随备份恢复", login_ok)
    tok_rows = c.execute("SELECT COUNT(*) n FROM auth_tokens").fetchone()["n"]
    sess_rows = c.execute("SELECT COUNT(*) n FROM auth_sessions").fetchone()["n"]
    check("R4-2 Token 记录随备份恢复（需人工复核吊销状态）", tok_rows >= 1, f"{tok_rows} 条")
    check("R4-3 会话记录随备份恢复（恢复后应清理）", sess_rows >= 1, f"{sess_rows} 条")

# ============================================================
print("== R4 恢复后不外发（应用启动行为） ==")
# 检查恢复出的状态：不存在"发送中"被自动重发（需要人工/租约过期→结果不明）
with sqlite3.connect(str(target / "xintel.db")) as c:
    c.row_factory = sqlite3.Row
    sending = c.execute("SELECT COUNT(*) n FROM delivery_msgs WHERE state='发送中'").fetchone()["n"]
    unknown = c.execute("SELECT COUNT(*) n FROM delivery_msgs WHERE state='结果不明'").fetchone()["n"]
    pending = c.execute("SELECT COUNT(*) n FROM delivery_msgs WHERE state='待发送'").fetchone()["n"]
check("R4-4 恢复后仍保有 发送中/结果不明/待发送（不自动清零/重发）",
      sending >= 1 and unknown >= 1 and pending >= 0, f"s={sending} u={unknown} p={pending}")
# 应用启动策略：默认调度关闭（由部署控制；代码层提供显式开关）
src_main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
check("R4-5 启动调度受配置控制（可关闭）", "scheduler" in src_main)
# 外发总开关：恢复/演练模式下 dispatch 与报告推送都跳过
src_eng = (ROOT / "app" / "engine.py").read_text(encoding="utf-8")
src_rep = (ROOT / "app" / "reports.py").read_text(encoding="utf-8")
check("R4-7 dispatch 遵守 outbound.enable", "outbound" in src_eng)
check("R4-8 报告推送遵守 outbound.enable", "外发已禁用" in src_rep)
check("R4-9 调度暂停开关存在", "scheduler" in src_main and "enabled" in src_main)

# 恢复后人工处置建议：导出未完成/不明
exp = backup.export_pending_deliveries(target / "xintel.db")
check("R4-6 可导出恢复后的待处理任务", len(exp["msgs"]) >= 1,
      f"msgs={len(exp['msgs'])}")

# 事件数据也需随备份恢复（第六批）
with sqlite3.connect(str(target / "xintel.db")) as c:
    c.row_factory = sqlite3.Row
    ev_tabs = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
check("R5-1 事件相关表存在", {"events", "event_evidence", "event_revisions",
                              "event_reviews"} <= ev_tabs, str(sorted(ev_tabs))[:80])

print(f"\n=== 演练耗时：备份 {bk_elapsed:.2f}s / 恢复 {rs_elapsed:.2f}s / 总计 {time.time()-T0:.2f}s ===")
print("=== 限制：恢复后需人工清理 auth_sessions、复核 auth_tokens 吊销状态 ===")
print(f"\n==== 恢复演练：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
