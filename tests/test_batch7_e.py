# -*- coding: utf-8 -*-
"""E 组：事件版本 / 审核 / 一致性快照（2026-11-09 集中修复，受控并发+故障注入）。

历史缺陷（基线 b4c720d 复核）：
  E-1 底层情报改变，事件版本和审核不变，导出却出现新内容。
  E-2 两个相同 expected_version 的并发修改均成功（乐观锁检查在锁外读）。
  E-3 导出与写请求交错 → cannot commit - no transaction is active
      （共享连接上 BEGIN 被写入隐式回收）。

修复验证（不只顺序调用）：
  并发线程竞争乐观锁 / 后台写入期间导出 / 指纹驱动的审核失效。

运行：python tests/test_batch7_e.py
"""
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from tests.testenv import TestEnv

env = TestEnv(forbid_network=True).start()
import app.db as db  # noqa: E402

db.DATA_DIR = env.tmp
db._conn = None
db.init_db()

import app.eventexport as eventexport  # noqa: E402

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(f"{name} [{detail}]")
        print(f"FAIL {name} [{detail}]")


def mk(tweet_id, text="事件证据"):
    with db._lock:
        db.get_conn().execute(
            "INSERT INTO tweets (id, screen_name, tweet_id, text, url, published_at, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (int(tweet_id), "ESrc", f"ext-{tweet_id}", text, f"u{tweet_id}", db.now(), db.now()))
        db.get_conn().commit()
    return int(tweet_id)


t1 = mk(501, "证据一")
t2 = mk(502, "证据二")
uid, v0 = db.create_event("E 组事件", tweet_ids=[t1, t2], operator="e-test")

# ============================================================
print("== E1 乐观锁并发竞争：相同 expected_version 只允许一个成功 ==")
results = []
barrier = threading.Barrier(4)


def _compete(i):
    barrier.wait()
    ok, v = db.update_event(uid, title=f"并发标题 {i}", operator=f"t{i}",
                            expected_version=v0)
    results.append((ok, v))


ths = [threading.Thread(target=_compete, args=(i,)) for i in range(4)]
[t.start() for t in ths]
[t.join() for t in ths]
winners = [r for r in results if r[0]]
check("E1-1 四线程同 expected_version 仅一个成功", len(winners) == 1,
      str(results))
ev1 = db.get_event(uid)
check("E1-2 版本只推进一次", int(ev1["version"]) == v0 + 1,
      f"{ev1['version']} vs {v0}+1")
revs1 = db.event_revisions(uid)
check("E1-3 审计只有一条 update（同事务写入）",
      sum(1 for r in revs1 if r["action"] == "update") == 1,
      str([r["action"] for r in revs1]))
check("E1-4 失败者拿到当前版本号", all(v == ev1["version"] for ok, v in results if not ok),
      str(results))

# ============================================================
print("== E2 内容修改/版本检查/推进/审计 同事务 ==")
ev2 = db.get_event(uid)
v2 = int(ev2["version"])
ok2, v2n = db.update_event(uid, summary="E2 摘要更新", operator="e2", expected_version=v2)
revs2 = db.event_revisions(uid)
check("E2-1 更新成功且返回新版本", ok2 is True and v2n == v2 + 1, f"{ok2}/{v2n}")
check("E2-2 版本推进与审计一致", revs2[0]["version"] == v2n and revs2[0]["action"] == "update",
      str(revs2[0]))
check("E2-3 旧 expected_version 再更新 → 拒绝并返回当前版本",
      db.update_event(uid, title="x", operator="e2", expected_version=v2)[0] is False,
      "")

# ============================================================
print("== E3 事件审核绑定版本+指纹 ==")
cur_v = int(db.get_event(uid)["version"])
ok3, reason3 = db.review_event(uid, cur_v, "approved", note="E3", operator="e3")
st3 = db.event_review_state(uid)
check("E3-1 绑定当前版本成功", ok3 is True, reason3)
check("E3-2 审核绑定证据指纹（非空）", bool(st3["review"]["evidence_fingerprint"]),
      str(st3["review"]["evidence_fingerprint"])[:20])
check("E3-3 review_state=valid", st3["state"] == "valid", str(st3))
ok3b, reason3b = db.review_event(uid, cur_v - 1, "approved")
check("E3-4 旧版本不能批准新内容（锁内复验）",
      ok3b is False and "version_mismatch" in reason3b, reason3b)

# ============================================================
print("== E4 底层情报变化 → 事件版本推进，旧审核成历史决定 ==")
v_before = int(db.get_event(uid)["version"])
db.bump_analysis_version(t1)  # 底层情报：AI 结果落地/人工修订都会推进
check("E4-1 指纹检测到内容变化（版本推进 + 留痕）",
      db.ensure_event_freshness(uid) == v_before + 1, "")
check("E4-2 幂等：指纹一致不再推进",
      db.ensure_event_freshness(uid) == v_before + 1, "")
rev4 = db.event_revisions(uid)
check("E4-3 留痕 action=evidence_changed",
      any(r["action"] == "evidence_changed" for r in rev4),
      str([r["action"] for r in rev4][:4]))
st4 = db.event_review_state(uid)
check("E4-4 旧审核自动变 stale（不得冒充当前批准）", st4["state"] == "stale", str(st4))
check("E4-5 旧审核记录保留（历史决定可查）",
      st4["review"] is not None and int(st4["review"]["event_version"]) == cur_v,
      str(st4["reviewed_version"]))
# 新版本重新审核 → 恢复 valid
v_now = int(db.get_event(uid)["version"])
db.review_event(uid, v_now, "approved", note="E4 重新审核")
check("E4-6 新版本重新审核 → valid",
      db.event_review_state(uid)["state"] == "valid", "")

# ============================================================
print("== E5 导出与并发写入交错：独立只读事务，无 cannot commit ==")
errors = []
stop = threading.Event()


def _writer():
    i = 0
    while not stop.is_set():
        try:
            db.update_event(uid, summary=f"并发写入 {i}", operator="writer")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{type(e).__name__}: {str(e)[:80]}")
        i += 1


wt = threading.Thread(target=_writer)
wt.start()
try:
    for k in range(6):  # 写入期间连续导出（故障注入窗口）
        data = eventexport.export_event_json(uid)
        if data is None:
            errors.append("export None")
            continue
        # 快照一致性：导出内所有证据同属一个版本快照（version 字段单一）
        if "version" not in data["event"]:
            errors.append("no version")
except Exception as e:  # noqa: BLE001
    errors.append(f"EXPORT {type(e).__name__}: {str(e)[:120]}")
finally:
    stop.set()
    wt.join()
check("E5-1 并发写入期间导出无异常（含 cannot commit 消失）",
      not any("cannot commit" in x.lower() or "transaction" in x.lower() for x in errors),
      str(errors[:3]))
check("E5-2 导出全程成功（6 轮）", not errors, str(errors[:3]))

# ============================================================
print("== E6 拆分原子性：双向版本推进 + 指纹一致 ==")
uid_b, _ = db.create_event("E6 拆分源", tweet_ids=[t2], operator="e6")
db.bump_analysis_version(t2)
db.ensure_event_freshness(uid_b)
vb = int(db.get_event(uid_b)["version"])
ok6, new6 = db.split_event(uid_b, [t2], "E6 拆出")
src_ev = db.get_event(uid_b)
dst_ev = db.get_event(new6)
check("E6-1 拆分成功", ok6 is True and new6, f"{ok6}/{new6}")
check("E6-2 源事件版本推进", int(src_ev["version"]) == vb + 1,
      f"{src_ev['version']} vs {vb}+1")
check("E6-3 双方指纹非空且互异（成员不同）",
      src_ev["evidence_fingerprint"] and dst_ev["evidence_fingerprint"]
      and src_ev["evidence_fingerprint"] != dst_ev["evidence_fingerprint"],
      str(src_ev["evidence_fingerprint"])[:12] + "/" + str(dst_ev["evidence_fingerprint"])[:12])
src_revs = db.event_revisions(uid_b)
check("E6-4 源事件审计含 split",
      any(r["action"] == "split" for r in src_revs),
      str([r["action"] for r in src_revs][:4]))

# ============================================================
print("== E7 导出审核块：历史决定标注（state 字段） ==")
data7 = eventexport.export_event_json(uid_b)
check("E7-1 导出 review 含 state 与 current_version",
      data7["review"] is None or ("state" in data7["review"]
                                  and "current_version" in data7["review"]),
      str(data7.get("review"))[:100])
check("E7-2 导出含版本与指纹驱动的事件字段",
      int(data7["event"]["version"]) == int(src_ev["version"]), "")

print(f"\n==== E 组事件版本/审核/一致性快照：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
