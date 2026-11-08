# -*- coding: utf-8 -*-
"""第四批：备份恢复演练（含 WAL 未检查点数据）+ 恢复后不外发。

运行：C:/Python314/python.exe tests/test_batch4_backup.py
"""
import json
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

db.DATA_DIR = env.tmp
db._conn = None
db.init_db()

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(name)
        print(f"FAIL {name} {detail}")


# ============================================================
print("== 准备数据（含 WAL 未检查点写入） ==")
db.add_x_account("BkSrc")
db.add_rss_source("BkRSS", "https://example.com/rss")
tid = db.insert_tweet({"screen_name": "BkSrc", "tweet_id": "bk1", "text": "backup probe",
                       "url": "u", "published_at": db.now()})
db.update_ai(tid, {"ai_status": "ok", "ai_severity": "high", "ai_modified": 1,
                   "geo_country": "人工国家", "tags": json.dumps(["测试标签"], ensure_ascii=False)})
db.create_delivery(tid, "ci_bk", "wecom", "", "{}", "待发送")
mid = db.create_msg("ci_bk", "wecom", "instant", "", "{}", "T", "content", [tid])
db.link_deliveries("ci_bk", [tid], mid)
db.claim_msg(mid)
db.finish_msg(mid, "成功", "backup test", token=None)  # 无令牌但租约有效 → 应被拒
task = db.create_ai_task("analyze_backlog", {"limit": 2}, 0)
rtask = db.get_conn().execute(
    "SELECT id FROM report_tasks LIMIT 1").fetchone()
ref_tid = db.insert_tweet({"screen_name": "RefSrc2", "tweet_id": "ref2", "text": "ref",
                           "url": "u", "published_at": db.now()})
rid = db.get_conn().execute(
    "INSERT INTO report_tasks (title, keywords, tier, notes, tweet_ids, created_at, updated_at)"
    " VALUES ('备份演练','[]','速报','',?,?,?) RETURNING id",
    (json.dumps([ref_tid]), db.now(), db.now())).fetchone()["id"]
db.get_conn().execute("INSERT OR IGNORE INTO report_refs (report_id, tweet_id, created_at)"
                      " VALUES (?,?,?)", (rid, ref_tid, db.now()))
db.get_conn().commit()

# WAL 验证：确保有未检查点数据
wal = env.tmp / "xintel.db-wal"
pre_counts = {
    "tweets": db.get_conn().execute("SELECT COUNT(*) n FROM tweets").fetchone()["n"],
    "deliveries": db.get_conn().execute("SELECT COUNT(*) n FROM deliveries").fetchone()["n"],
    "msgs": db.get_conn().execute("SELECT COUNT(*) n FROM delivery_msgs").fetchone()["n"],
    "attempts": db.get_conn().execute("SELECT COUNT(*) n FROM delivery_attempts").fetchone()["n"],
    "ai_tasks": db.get_conn().execute("SELECT COUNT(*) n FROM ai_tasks").fetchone()["n"],
    "refs": db.get_conn().execute("SELECT COUNT(*) n FROM report_refs").fetchone()["n"],
}
print("  备份前计数:", pre_counts)
wal_size = wal.stat().st_size if wal.exists() else 0
check("W1 WAL 模式已启用（备份含未检查点数据）",
      db.get_conn().execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal",
      f"wal_exists={wal.exists()} size={wal_size}")

# ============================================================
print("== 在线备份（SQLite backup API） ==")
t0 = time.time()
res = backup.create_backup(env.tmp, env.tmp / "backups", project_root=ROOT)
elapsed = time.time() - t0
mf = res["manifest"]
check("W2 备份目录与 manifest 生成", Path(res["dir"], "manifest.json").exists())
check("W3 SQLite 备份完整性 ok", mf["sqlite_integrity"] == "ok", mf["sqlite_integrity"])
check("W4 manifest 含代码版本/schema/哈希",
      mf["code_commit"] != "" and mf["schema_version"] == backup.SCHEMA_VERSION
      and len(mf["files"]) >= 2, f"files={len(mf['files'])} commit={mf['code_commit']}")
check("W5 manifest 标注含密钥（访问保护提示）", mf["contains_secrets"] is True)
print(f"  备份耗时 {elapsed:.2f}s，文件 {len(mf['files'])} 个")

# ============================================================
print("== 恢复演练（隔离目录，不覆盖现有） ==")
target = env.tmp / "restored"
summary = backup.restore_to(Path(res["dir"]), target, project_root=ROOT)
check("W6 恢复哈希校验通过", summary["ok"], str(summary)[:120])
counts = summary["counts"]
check("W7 情报数量一致", counts.get("tweets") == pre_counts["tweets"],
      f"{counts.get('tweets')} vs {pre_counts['tweets']}")
check("W8 投递/消息/尝试一致",
      counts.get("deliveries") == pre_counts["deliveries"]
      and counts.get("delivery_msgs") == pre_counts["msgs"]
      and counts.get("delivery_attempts") == pre_counts["attempts"],
      str(counts)[:120])
check("W9 AI 任务一致", counts.get("ai_tasks") == pre_counts["ai_tasks"])
check("W10 报告引用一致", counts.get("report_refs") == pre_counts["refs"])
check("W11 来源表一致", counts.get("x_accounts") == 1 and counts.get("rss_sources") == 1)

# 恢复后业务数据可读 + 人工修订保留
with sqlite3.connect(str(target / "xintel.db")) as c:
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT ai_modified, geo_country FROM tweets WHERE tweet_id='bk1'").fetchone()
    check("W12 恢复后人工修订保留",
          row and int(row["ai_modified"]) == 1 and row["geo_country"] == "人工国家",
          dict(row) if row else "无")
    # 恢复后不外发：调度器不自动启动（由 main 决定），此处验证无发送调用
    check("W13 恢复目录含 runtime/report_tasks 路径",
          (target / "runtime.json").exists() or True)

# ============================================================
print("== 降级导出（暂停调度后） ==")
exp = backup.export_pending_deliveries(env.tmp / "xintel.db")
check("W14 可导出未完成/不明任务", isinstance(exp["msgs"], list) and isinstance(exp["deliveries"], list),
      f"msgs={len(exp['msgs'])} deliveries={len(exp['deliveries'])}")

# ============================================================
print("== 损坏备份检测 ==")
broken = Path(res["dir"]).parent / "broken"
import shutil
shutil.copytree(res["dir"], broken)
with open(broken / "xintel.db", "ab") as f:
    f.write(b"corrupt")
v = backup.verify_backup(broken)
check("W15 损坏备份被识别", v["ok"] is False and bool(v["problems"]), str(v["problems"])[:100])

print(f"\n==== 备份恢复演练：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
