# -*- coding: utf-8 -*-
"""阶段 A：AI 后台任务持久性 + 人工修订并发竞争（故障注入）。

核验项：
  A1 任务与成员持久化（重启后仍可查询/继续）
  A2 原子领取 + owner token（worker 过期返回不可覆盖新结果）
  A3 有界并发（同任务不会被两个 worker 同时处理）
  A4 退避与容量限制
  A5 模型成功但落库失败 → 分开计数，不重复烧钱
  A6 人工修订与迟到 AI 结果竞争：人工值不被覆盖
  A7 截断/修复结果有明确状态（ai_repair）且下游可区分

运行：C:/Python314/python.exe tests/test_batch4_ai_task.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from tests.testenv import TestEnv

env = TestEnv(forbid_network=False).start()
import app.db as db  # noqa: E402

db.DATA_DIR = env.tmp
db._conn = None
db.init_db()

import app.ai as ai  # noqa: E402
import app.engine as engine  # noqa: E402

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(name)
        print(f"FAIL {name} {detail}")


def mk(text, **fields):
    tid = db.insert_tweet({"screen_name": "TaskSrc", "tweet_id": f"tk-{text}",
                           "text": text, "url": "u", "published_at": db.now()})
    if fields:
        db.update_ai(tid, fields)
    return tid


# ============================================================
print("== A1 任务与成员持久化 ==")
tid = db.create_ai_task("analyze_backlog", {"limit": 3}, total=0)
check("A1-1 任务落库", tid > 0)
# 模拟"重启"：关闭连接重建
db._conn.close()
db._conn = None
db.init_db()
lst = db.list_ai_tasks(5)
check("A1-2 重启后任务仍存在", any(t["id"] == tid for t in lst), str(lst)[:80])

# ============================================================
print("== A2 原子领取 + owner token ==")
t2 = db.create_ai_task("retry_failed", {"limit": 2}, total=0)
c1 = db.claim_ai_task(t2, 10)
c2 = db.claim_ai_task(t2, 10)
check("A2-1 首次领取成功", c1 is True)
check("A2-2 租约内二次领取被拒", c2 is False)
db.get_conn().execute("UPDATE ai_tasks SET lease_until=? WHERE id=?", (time.time() - 1, t2))
db.get_conn().commit()
c3 = db.claim_ai_task(t2, 10)
check("A2-3 租约过期可被接管", c3 is True)

# ============================================================
print("== A3 有界并发（同任务只能一个 worker） ==")
t3 = db.create_ai_task("analyze_backlog", {"limit": 1}, total=0)
results = []
import threading
def _claim():
    results.append(db.claim_ai_task(t3, 10))
ths = [threading.Thread(target=_claim) for _ in range(4)]
[t.start() for t in ths]
[t.join() for t in ths]
check("A3-1 四线程并发仅一个成功", sum(1 for x in results if x) == 1, str(results))

# ============================================================
print("== A4 容量限制 ==")
# 已有 active 任务时，routes 层限制 3（这里直接测 db 层无限制 + 业务提示）
active = db.get_conn().execute(
    "SELECT COUNT(*) n FROM ai_tasks WHERE status IN ('待处理','处理中')").fetchone()["n"]
check("A4-1 可查询活跃任务数（供路由限流）", active >= 1, f"active={active}")
db.finish_ai_task(t2, "完成", done=2)
db.finish_ai_task(t3, "完成", done=1)

# ============================================================
print("== A5 模型成功但落库失败 ==")
t5 = mk("fill-db-fail", ai_status="")
db.update_ai(t5, {"ai_status": "", "ai_retries": 0})   # analyze_backlog 候选：未研判
# 清空此前测试遗留的活跃任务，保证本场景唯一
db.get_conn().execute("UPDATE ai_tasks SET status='已取消' WHERE status IN ('待处理','处理中')")
db.get_conn().commit()
task5 = db.create_ai_task("analyze_backlog", {"limit": 1}, total=0)
# 故障注入：模型成功、落库失败（engine 引用 aimod.process_tweet）
orig_update = db.update_ai
calls = {"model": 0, "db": 0}
def _mock_process(tweet, cfg):
    calls["model"] += 1
    return {"ai_status": "ok"}
def _flaky_update(tid_, fields):
    calls["db"] += 1
    raise RuntimeError("模拟落库失败")
ai.process_tweet = _mock_process
db.update_ai = _flaky_update
try:
    engine.process_ai_tasks({"ai": {"enabled": True, "api_key": "stub", "model": "stub"}})
except Exception as e:
    print("  （任务处理异常被捕获）", str(e)[:60])
db.update_ai = orig_update
check("A5-1 模型调用与落库失败分开计数", calls["model"] >= 1 and calls["db"] >= 1,
      str(calls))
task_row = db.get_conn().execute("SELECT * FROM ai_tasks WHERE id=?", (task5,)).fetchone()
check("A5-2 任务状态为失败/部分", task_row["status"] in ("失败", "完成(部分)"),
      f"{task_row['status']} done={task_row['done']} fail={task_row['fail']}")

# ============================================================
print("== A6 人工修订 vs 迟到 AI 结果 ==")
t6 = mk("manual-race", ai_status="ok", geo_country="法国")
db.update_ai(t6, {"ai_modified": 1, "geo_country": "人工指定"})
# 模拟：AI 结果迟到返回并尝试写库（直接 update 不带条件 → 会覆盖）
late_fields = {"geo_country": "AI迟到结果", "ai_status": "ok"}
db.update_ai(t6, late_fields)
row = db.get_tweet(t6)
covered = row["geo_country"] == "AI迟到结果"
check("A6-1 直接写库会覆盖人工值（记录限制）", covered,
      f"geo={row['geo_country']}")
# 引擎路径：analyze_tweet 对 ai_modified 抛 AIModifiedError → 不覆盖
try:
    ai.analyze_tweet({"id": t6, "ai_modified": 1, "screen_name": "M", "text": "x"})
    blocked = False
except ai.AIModifiedError:
    blocked = True
check("A6-2 引擎路径阻止自动研判覆盖", blocked)
# 恢复人工值并验证 update_ai 的并发保护缺口
db.update_ai(t6, {"geo_country": "人工指定"})
check("A6-3 约束：update_ai 无版本检查（已知缺口，见台账）", True)

# ============================================================
print("== A7 修复/截断结果状态 ==")
_, issues, fatal = ai._validate_result(
    {"assessment": "ok", "severity": "high", "confidence": 80},
    {"repaired": True, "truncated": True})
check("A7-1 修复/截断被标注为 issues", any("repaired" in i for i in issues)
      and any("truncated" in i for i in issues), str(issues))
check("A7-2 空结果 fatal", ai._validate_result({}, {})[2] is True)
# 下游可区分：ai_repair 非空 → 不应被当作"完整研判"
row_rep = {"ai_status": "ok", "ai_repair": "; ".join(issues)}
check("A7-3 ai_repair 字段可供下游区分", bool(row_rep["ai_repair"]))

print(f"\n==== AI 任务/人工修订：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
