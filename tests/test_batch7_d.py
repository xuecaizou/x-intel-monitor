# -*- coding: utf-8 -*-
"""D 组：事件证据身份 / 解除 / 拆分（2026-11-09 集中修复）。

历史缺陷（基线 b4c720d 复核，线上现象：关联推文 4091/4092 显示证据 ID 1/2）：
  D-1 event_evidence() 用 v.* 导致关联表行 id 以 "id" 键泄漏、created_at 同名歧义；
      前端/导出误用关联表 id → 解除无效、拆分建空事件、导出引用错误。
  D-2 解除失败前端仍显示"已解除"（不看响应）。
  D-3 拆分先建事件后校验成员 → 零有效成员也创建空事件并报成功。

夹具契约：先灌 20 条无关推文，使 tweet_id（21+）与关联行 id（1+）明显不同。

运行：python tests/test_batch7_d.py
"""
import sys
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


def mk(tweet_id, text="情报内容"):
    """插入指定 tweet_id 的推文（主键显式，保证夹具身份可控）"""
    with db._lock:
        db.get_conn().execute(
            "INSERT INTO tweets (id, screen_name, tweet_id, text, url, published_at, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (int(tweet_id), "DSrc", f"ext-{tweet_id}", text, f"u{tweet_id}", db.now(), db.now()))
        db.get_conn().commit()
    return int(tweet_id)


# 夹具：20 条垫底推文（id 1-20，无事件）→ 目标推文 id 4091/4092（复现线上数量级）
for i in range(1, 21):
    mk(i, f"垫底推文 {i}")
t_a = mk(4091, "目标证据 A：勒索软件攻击 ACME 医疗集团")
t_b = mk(4092, "目标证据 B：同一事件后续通报")

uid, _ = db.create_event("D 组事件", tweet_ids=[t_a, t_b], operator="d-test")

# ============================================================
print("== D1 身份分离：evidence_id 与 tweet_id 明确不同 ==")
ev = db.event_evidence(uid)
check("D1-1 证据行含两个身份字段",
      all(("evidence_id" in r and "tweet_id" in r) for r in ev), str(list(ev[0].keys()))[:120])
check("D1-2 tweet_id 是真实推文身份（4091/4092）",
      sorted(r["tweet_id"] for r in ev) == [t_a, t_b], str([r["tweet_id"] for r in ev]))
check("D1-3 evidence_id 是关联表行 id（1/2，与推文 id 明显不同）",
      sorted(r["evidence_id"] for r in ev) == [1, 2], str([r["evidence_id"] for r in ev]))
check("D1-4 不再有泄漏的裸 id 键", all("id" not in r for r in ev),
      str([k for k in ev[0].keys() if k == "id"]))
check("D1-5 created_at 歧义消除：collected_at(推文) 与 linked_at(关联) 均唯一存在",
      all(("collected_at" in r and "linked_at" in r and "created_at" not in r) for r in ev),
      str([k for k in ev[0].keys() if "at" in k]))

# ============================================================
print("== D2 解除：使用正确身份 + 失败不报成功 ==")
ok, v = db.remove_evidence(uid, 1)  # 传关联表行 id 1（历史上前端行为）
check("D2-1 用关联行 id 解除 → 明确失败（不误删）", ok is False, f"ok={ok}")
check("D2-2 证据未被误删", len(db.event_evidence(uid)) == 2, len(db.event_evidence(uid)))
ok2, v2 = db.remove_evidence(uid, t_b)  # 传真实 tweet_id
check("D2-3 用 tweet_id 解除 → 成功", ok2 is True and len(db.event_evidence(uid)) == 1,
      f"ok={ok2}")
ok3, _ = db.remove_evidence(uid, 999999)
check("D2-4 不存在的 tweet_id 解除 → 失败", ok3 is False, f"ok={ok3}")

# ============================================================
print("== D3 拆分：零有效成员失败且不产生空事件 ==")
before, _ = db.list_events()
ok4, new_uid4 = db.split_event(uid, [1], "坏身份拆分")  # 关联行 id（历史前端行为）
check("D3-1 传关联行 id → 拆分失败", ok4 is False, f"ok={ok4}")
total_now, rows_now = db.list_events()
check("D3-2 未创建空事件（事件总数不变）", total_now == before, f"{total_now} vs {before}")
ok5, new_uid5 = db.split_event(uid, [4091], "正确身份拆分")
check("D3-3 传真实 tweet_id → 拆分成功", ok5 is True and new_uid5, f"ok={ok5}")
check("D3-4 新事件含被拆出的真实推文",
      [r["tweet_id"] for r in db.event_evidence(new_uid5)] == [t_a],
      str([r["tweet_id"] for r in db.event_evidence(new_uid5)]))
check("D3-5 原事件证据清空（t_b 已解除 + t_a 已拆出）",
      db.event_evidence(uid) == [],
      str([r["tweet_id"] for r in db.event_evidence(uid)]))

# API 层：零有效成员 → 400 且无空事件
from fastapi.testclient import TestClient  # noqa: E402
import app.auth as auth  # noqa: E402
from app.main import app  # noqa: E402

auth.init_auth_tables()
auth.create_admin("admin", "AdminPass123")
H = {"Authorization": f"Bearer {auth.create_token('d-admin', 'admin')}"}
client = TestClient(app)

uid_api, _ = db.create_event("D 组 API 事件", tweet_ids=[t_a], operator="d-test")
total_before, _ = db.list_events()
r6 = client.post(f"/api/events/{uid_api}/split",
                 json={"tweet_ids": [1], "new_title": "空事件尝试"}, headers=H)
check("D3-6 API 零有效成员 → 400", r6.status_code == 400, f"{r6.status_code}/{r6.text[:80]}")
total_after, _ = db.list_events()
check("D3-7 API 拆分失败不产生空事件", total_after == total_before,
      f"{total_after} vs {total_before}")
r7 = client.post(f"/api/events/{uid_api}/split",
                 json={"tweet_ids": [t_a], "new_title": "正确拆分"}, headers=H)
check("D3-8 API 正确身份拆分 → 200 + 新事件", r7.status_code == 200 and r7.json().get("new_event"),
      f"{r7.status_code}/{r7.text[:80]}")

# ============================================================
print("== D4 导出身份正确（tweet_id/evidence_id 双表达） ==")
uid_x, _ = db.create_event("D 组导出事件", tweet_ids=[t_a, t_b], operator="d-test")
evx = db.event_evidence(uid_x)
item = eventexport._evidence_item(evx[0])
check("D4-1 导出 tweet_id 为真实推文身份", item["tweet_id"] in (t_a, t_b), str(item["tweet_id"]))
check("D4-2 导出 evidence_id 为关联行 id（与推文身份互异）",
      item["evidence_id"] != item["tweet_id"] and item["evidence_id"] < 100
      and item["tweet_id"] >= 4091,
      f"evidence_id={item['evidence_id']} tweet_id={item['tweet_id']}")
check("D4-3 导出 collected_at 来自推文列", "collected_at" in item and item["collected_at"],
      str(item.get("collected_at"))[:40])

data = eventexport.export_event_json(uid_x)
check("D4-4 IOC 导出的 tweet_id 也是真实推文身份",
      all(rec["tweet_id"] in (t_a, t_b) for grp in data["iocs"].values() for rec in grp
          if rec["tweet_id"]),
      str([rec["tweet_id"] for grp in data["iocs"].values() for rec in grp]))
check("D4-5 事件 JSON 证据 tweet_id 正确",
      sorted(e["tweet_id"] for e in data["evidence"]) == sorted([t_a, t_b]),
      str([e["tweet_id"] for e in data["evidence"]]))

# ============================================================
print("== D5 详情接口返回正确身份（前端契约） ==")
uid_y, _ = db.create_event("D 组详情事件", tweet_ids=[t_b], operator="d-test")
r8 = client.get(f"/api/events/{uid_y}", headers=H)
evd = r8.json().get("evidence") or []
check("D5-1 详情接口证据含 tweet_id（不再有裸 id）",
      evd and all("tweet_id" in e and "id" not in e for e in evd),
      str(list(evd[0].keys()))[:120] if evd else "empty")
check("D5-2 详情接口 tweet_id=真实推文", evd and evd[0]["tweet_id"] == t_b,
      str(evd[0].get("tweet_id") if evd else None))

print(f"\n==== D 组事件证据身份/解除/拆分：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
