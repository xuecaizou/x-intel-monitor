# -*- coding: utf-8 -*-
"""第六批验收：事件聚合 / 审核 / 导出（隔离库，含 7 条固定流程对应断言）。

运行：C:/Python314/python.exe tests/test_batch6_events.py
"""
import csv
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from tests.testenv import TestEnv

env = TestEnv(forbid_network=False).start()
import app.db as db  # noqa: E402
import app.eventexport as ex  # noqa: E402
import app.integrity as integ  # noqa: E402

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


def mk(text, iocs=None, org=None, actors=None, integrity="verified", **kw):
    tid = db.insert_tweet({"screen_name": "EvSrc", "tweet_id": f"ev-{text}",
                           "text": text, "url": f"https://x/{text}", "published_at": db.now()})
    db.update_ai(tid, {"ai_status": "ok", "ai_severity": "high", "category": "数据泄露线索",
                       "ai_iocs": json.dumps(iocs or [], ensure_ascii=False),
                       "org": json.dumps(org or [], ensure_ascii=False),
                       "ai_actors": json.dumps(actors or [], ensure_ascii=False),
                       "ai_integrity": integrity, **kw})
    return tid


# ============================================================
print("== E1 事件创建：稳定 ID / 版本 / 证据关联 ==")
t1 = mk("orig1", ["CVE-2026-1111", "1.2.3.4"], ["Acme Corp"], ["Qilin"])
t2 = mk("orig2", ["5.6.7.8"], ["Acme Corp"], ["Qilin"])
uid, v = db.create_event("Acme 泄露", "摘要", "high", "tester", [t1], "manual")
check("E1-1 稳定事件 ID（evt_ 前缀）", uid.startswith("evt_") and len(uid) == 16, uid)
check("E1-2 初始版本 v1", v == 1)
ev = db.get_event(uid)
check("E1-3 事件可查", ev is not None and ev["title"] == "Acme 泄露")
check("E1-4 证据关联", len(db.event_evidence(uid)) == 1)
check("E1-5 版本历史含 create", any(r["action"] == "create" for r in db.event_revisions(uid)))

# ============================================================
print("== E2 共享标识仅作候选，不自动合并 ==")
cands = db.candidate_events(t2)
cand_uids = [c["event_uid"] for c in cands]
check("E2-1 共享 CVE/IP/组织名 → 候选", uid in cand_uids, str(cand_uids))
check("E2-2 候选明确 auto_merge=False",
      all(c["auto_merge"] is False for c in cands))
check("E2-3 候选未自动写入证据", len(db.event_evidence(uid)) == 1)

# ============================================================
print("== E3 转载不冒充独立证据 ==")
ok, v2 = db.add_evidence(uid, t2, "shared_indicator", "repost", "转载自 Acme 公告", "tester")
check("E3-1 证据添加成功且版本推进", ok is True and v2 == 2, f"{ok}/{v2}")
st = db.evidence_stats(uid)
check("E3-2 独立证据仍为 1（转载不计数）", st["original"] == 1 and st["repost"] == 1, str(st))

# 重复添加幂等
ok2, v3 = db.add_evidence(uid, t2, "manual", "original", "", "tester")
check("E3-3 同推文重复关联被拒", ok2 is False and v3 == v2, f"{ok2}/{v3}")

# ============================================================
print("== E4 合并 / 拆分 / 解除（可追溯） ==")
t3 = mk("other1", ["9.9.9.9"], ["Beta LLC"], ["LockBit"])
uid2, _ = db.create_event("Beta 事件", "", "medium", "tester", [t3], "manual")
ok, dst = db.merge_events(uid, uid2, "tester", "同一事件两家报道")
check("E4-1 合并成功", ok is True and dst == uid2)
check("E4-2 源事件归档", db.get_event(uid)["status"] == "已归档")
n_src = len(db.event_evidence(uid))
check("E4-3 目标事件含源事件全部证据",
      len(db.event_evidence(uid2)) == 1 + n_src, f"{len(db.event_evidence(uid2))} vs {1+n_src}")
check("E4-4 合并留痕", any(r["action"] == "merge" for r in db.event_revisions(uid2)))

# 拆分
ok, new_uid = db.split_event(uid2, [t3], "Beta 拆分事件", "tester")
check("E4-5 拆分生成新事件", ok is True and new_uid.startswith("evt_"))
check("E4-6 拆分后原事件减少 1 条", len(db.event_evidence(uid2)) == n_src, f"{len(db.event_evidence(uid2))} vs {n_src}")
check("E4-7 新事件有 1 条", len(db.event_evidence(new_uid)) == 1)

# 解除
ok, v4 = db.remove_evidence(new_uid, t3, "tester")
check("E4-8 解除关联成功", ok is True and len(db.event_evidence(new_uid)) == 0)
check("E4-9 解除留痕", any(r["action"] == "unlink" for r in db.event_revisions(new_uid)))

# ============================================================
print("== E5 审核（版本绑定，旧版本不能批准新内容） ==")
v_now = db.get_event(uid2)["version"]
ok, reason = db.review_event(uid2, v_now, "approved", "确认", "reviewer")
check("E5-1 当前版本可审核", ok is True and reason == "ok", reason)
check("E5-2 审核记录含版本/操作者",
      db.event_review_latest(uid2)["event_version"] == v_now
      and db.event_review_latest(uid2)["operator"] == "reviewer")
# 内容变化 → 版本推进
db.update_event(uid2, title="Beta 事件（修订）", operator="editor")
v_new = db.get_event(uid2)["version"]
check("E5-3 内容变化推进版本", v_new > v_now, f"{v_now}->{v_new}")
ok, reason = db.review_event(uid2, v_now, "approved", "旧版本批准", "reviewer")
check("E5-4 旧版本不能批准新内容", ok is False and reason.startswith("version_mismatch"), reason)
ok, reason = db.review_event(uid2, v_new, "approved", "新版本确认", "reviewer")
check("E5-5 新版本可重新审核", ok is True)
ok, reason = db.review_event(uid2, v_new, "rejected", "重复", "reviewer")
check("E5-6 同版本重复审核被拒", ok is False and reason == "already_reviewed", reason)
q = db.review_queue("pending", 50)
check("E5-7 审核队列可查（不含已审当前版本）",
      all(x["event_uid"] != uid2 or not x.get("current_decision") for x in q))

# ============================================================
print("== E6 导出：JSON/CSV 结构、类型、公式注入、分级 ==")
# 给事件补充一条含 CVE 的完整证据（独立构造，不依赖前面拆分状态）
t_cve = mk("cve1", ["CVE-2026-1111", "1.2.3.4"], ["Acme Corp"], ["Qilin"])
db.add_evidence(uid2, t_cve, "manual", "original", "", "tester")
# 再补一条 partial 证据（含公式注入样例）
t4 = mk("partial1", ["=cmd|' /C calc'!A0", "10.0.0.1"], ["Acme Corp"], [], integrity="partial",
        ai_repair="JSON 经修复解析")
db.add_evidence(uid2, t4, "manual", "original", "", "tester")

data = ex.export_event_json(uid2)
check("E6-1 schema_version 存在", data.get("schema_version") == ex.SCHEMA_VERSION)
check("E6-2 含事件版本/状态/时间", data["event"]["version"] == db.get_event(uid2)["version"]
      and "first_seen" in data["event"])
check("E6-3 证据含来源与时间", all("source" in e and "published_at" in e
                                   for e in data["evidence"]))
check("E6-4 含核验/完整性状态", all("integrity" in e["ai"] for e in data["evidence"]))
check("E6-5 partial 逐条标注且不可作结论",
      any(e["ai"]["integrity"] == "partial" and e["ai"]["usable_as_conclusion"] is False
          for e in data["evidence"]))
check("E6-6 invalid 不混入（无 invalid 证据）",
      all(e["ai"]["integrity"] != "invalid" for e in data["evidence"]))
# IOC 分类
check("E6-7 CVE 独立分组", any(i["kind"] == "cve" for i in data["iocs"]["cve"]))
check("E6-8 受害资产与攻击指标分离",
      all(i["role"] == "attack" for i in data["iocs"]["attack"]))
check("E6-9 confidence 为整数类型",
      all(isinstance(e["ai"]["confidence"], int) for e in data["evidence"]))
# 不泄漏
blob = json.dumps(data, ensure_ascii=False)
check("E6-10 不泄漏凭据/路径", ("webhook" not in blob.lower())
      and ("C:\\\\" not in blob) and ("api_key" not in blob.lower()))

# CSV 公式注入
csv_text = ex.export_event_timeline_csv(uid2)
check("E6-11 时间线 CSV 有表头", csv_text.startswith("event_uid,"), csv_text[:40])
check("E6-12 CSV 公式注入被转义", "'=" in csv_text or "=cmd" not in csv_text.split("\n")[1:][0] if len(csv_text.split("\n"))>1 else True)
rows = list(csv.DictReader(io.StringIO(csv_text)))
check("E6-13 CSV 行数=证据数", len(rows) == len(db.event_evidence(uid2)), f"{len(rows)}")
check("E6-14 CSV 含完整性列", all("integrity" in r for r in rows))
ioc_csv = ex.export_event_ioc_csv(uid2)
check("E6-15 IOC CSV 含 kind/role", ioc_csv.startswith("event_uid,") and "kind" in ioc_csv[:200])
ioc_json = ex.export_event_ioc_json(uid2)
check("E6-16 IOC JSON 与事件 JSON 口径一致",
      ioc_json["iocs"] == data["iocs"])

# ============================================================
print("== E7 引用保护（事件证据不可删） ==")
prot = db.protected_tweet_ids()
check("E7-1 事件证据进保护集", t1 in prot and t2 in prot)
n_before = len(db.event_evidence(uid2))
db.batch_tweet_op([t4], "delete")
check("E7-2 批量删除跳过事件证据", db.get_tweet(t4) is not None)
db.cleanup_old(0)
check("E7-3 cleanup 同样保护", db.get_tweet(t4) is not None)

print(f"\n==== 第六批验收：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
