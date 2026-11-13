# -*- coding: utf-8 -*-
"""协作写作工作台 · 迁移/备份恢复/AI 推荐任务链验收（隔离库）。

覆盖：N16 旧库迁移可重复 / 备份恢复含新表 / N07 AI 推荐真实任务链（确定性模型替身）
运行：C:/Python314/python.exe tests/test_writing_migration.py
"""
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from tests.testenv import TestEnv  # noqa: E402

env = TestEnv(forbid_network=True).start()
import app.db as db  # noqa: E402
import app.auth as auth  # noqa: E402
import app.writing as writing  # noqa: E402
import app.doccollab as dc  # noqa: E402
import app.backup as backup  # noqa: E402
import app.ai as aimod  # noqa: E402

db.DATA_DIR = env.tmp
db._conn = None
db.init_db()
auth.init_auth_tables()
auth.create_admin("boss", "BossPass12345")

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(name)
        print(f"FAIL {name} {detail}")


NEW_TABLES = ["doc_templates", "template_versions", "writing_candidates",
              "candidate_revisions", "ai_recommend_tasks", "doc_documents",
              "doc_acl", "doc_updates", "doc_revisions", "doc_suggestions",
              "doc_exports", "doc_agent_edits", "ws_tickets"]

# ============================================================
print("== M1 旧库迁移（模拟升级前库）+ 可重复 ==")
db.insert_tweet({"screen_name": "M", "tweet_id": "m1", "text": "旧数据", "url": "u",
                 "published_at": db.now()})
n_tweets_before = db.get_conn().execute("SELECT COUNT(*) n FROM tweets").fetchone()["n"]
with db._lock:
    c = db.get_conn()
    for t in NEW_TABLES:
        c.execute(f"DROP TABLE IF EXISTS {t}")
    c.commit()
db.init_db()
rows = db.get_conn().execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
names = {r["name"] for r in rows}
check("M1-1 新表全部重建", set(NEW_TABLES) <= names, str(sorted(set(NEW_TABLES) - names))[:80])
check("M1-2 旧数据保留", db.get_conn().execute(
    "SELECT COUNT(*) n FROM tweets").fetchone()["n"] == n_tweets_before)
db.init_db()
db.init_db()
check("M1-3 重复迁移幂等（无异常）", True)

# ============================================================
print("== M2 备份恢复：新表数据进入备份 ==")
uid, _ = writing.create_candidate("备份候选", tweet_ids=[], operator="boss")
tpl = writing.create_template("备份模板", body_md="# 备份正文\n", variables=[], operator="boss")
writing.publish_template(tpl["id"], "v1", "boss")
doc_uid = dc.create_doc("备份文档", template_id=tpl["id"], operator="boss")
dc.apply_text_replace(doc_uid, "备份正文", "备份正文（已编辑）", kind="agent_edit", operator="boss")
res = backup.create_backup(env.tmp, env.tmp / "bk", project_root=ROOT)
check("M2-1 备份完整性 ok", res["manifest"]["sqlite_integrity"] == "ok")
with sqlite3.connect(str(Path(res["dir"]) / "xintel.db")) as c:
    counts = {}
    for t in NEW_TABLES:
        try:
            counts[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            counts[t] = "MISSING"
check("M2-2 备份含模板/候选/文档/CRDT 日志",
      counts["doc_templates"] >= 1 and counts["writing_candidates"] >= 1
      and counts["doc_documents"] >= 1 and counts["doc_updates"] >= 1, str(counts)[:160])
target = env.tmp / "restored_w"
summary = backup.restore_to(Path(res["dir"]), target, project_root=ROOT)
check("M2-3 恢复校验通过", summary["ok"], str(summary.get("problems"))[:120])
with sqlite3.connect(str(target / "xintel.db")) as c:
    c.row_factory = sqlite3.Row
    check("M2-4 恢复后候选/文档/更新一致",
          c.execute("SELECT COUNT(*) n FROM writing_candidates").fetchone()["n"] >= 1
          and c.execute("SELECT COUNT(*) n FROM doc_documents").fetchone()["n"] >= 1
          and c.execute("SELECT COUNT(*) n FROM doc_updates").fetchone()["n"] >= 1)
    check("M2-5 恢复后修订正文保留",
          "已编辑" in c.execute("SELECT body_md FROM doc_revisions WHERE doc_uid=?"
                                " ORDER BY rev DESC LIMIT 1", (doc_uid,)).fetchone()["body_md"])

# ============================================================
print("== M3 AI 推荐任务链（确定性模型替身，N07） ==")
CANNED = json.dumps({"items": [
    {"title": "推荐选题：某勒索团伙新样本", "angle": "样本与投递链", "reason": "高严重度集中",
     "priority": 2, "template": "", "materials": ["样本哈希"], "basis": ["素材1"], "tweet_ids": []},
]}, ensure_ascii=False)


def fake_chat(cfg, messages, json_mode=False, action="", tweet_id=None, ref=""):
    return CANNED


aimod._chat = fake_chat
tid = writing.trigger_recommend("backlog", "", "boss")
for _ in range(40):
    tasks = writing.list_recommend_tasks(5)
    row = [x for x in tasks if x["id"] == tid][0]
    if row["status"] in ("done", "failed"):
        break
    time.sleep(0.3)
check("M3-1 推荐任务完成", row["status"] == "done", str(row)[:140])
total, items = writing.list_candidates(status="", source_type="ai")
check("M3-2 AI 候选已创建", total >= 1 and any("推荐选题" in x["title"] for x in items),
      str(total))
# 重复推荐 → 去重合并不新建
tid2 = writing.trigger_recommend("backlog", "", "boss")
for _ in range(40):
    row2 = [x for x in writing.list_recommend_tasks(5) if x["id"] == tid2][0]
    if row2["status"] in ("done", "failed"):
        break
    time.sleep(0.3)
total2, _ = writing.list_candidates(source_type="ai")
check("M3-3 重复推荐去重（候选数不变）", total2 == total, f"{total}→{total2}")
# 失败→重试
def bad_chat(cfg, messages, json_mode=False, action="", tweet_id=None, ref=""):
    raise RuntimeError("配额限流模拟")


aimod._chat = bad_chat
tid3 = writing.trigger_recommend("backlog", "", "boss")
for _ in range(40):
    row3 = [x for x in writing.list_recommend_tasks(5) if x["id"] == tid3][0]
    if row3["status"] in ("done", "failed"):
        break
    time.sleep(0.3)
check("M3-4 模型失败→任务失败留痕", row3["status"] == "failed" and "配额" in row3["error"],
      str(row3)[:120])
aimod._chat = fake_chat
writing.retry_recommend_task(tid3)
for _ in range(40):
    row3 = [x for x in writing.list_recommend_tasks(5) if x["id"] == tid3][0]
    if row3["status"] in ("done", "failed"):
        break
    time.sleep(0.3)
check("M3-5 重试恢复", row3["status"] == "done", str(row3)[:100])

# ============================================================
print("== M4 汇总聚合（首页口径） ==")
s = writing.summary()
check("M4-1 候选/文档/模板计数来自全库", s["candidates_active"] >= 1 and s["docs_total"] >= 1
      and s["templates_active"] >= 1, str(s))

print(f"\n==== 迁移/备份/AI推荐验收：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
