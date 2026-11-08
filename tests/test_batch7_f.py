# -*- coding: utf-8 -*-
"""F 组：IOC 导出（2026-11-09 集中修复）。

历史缺陷（基线 b4c720d 复核）：
  F-1 _classify_ioc 默认 role=attack——无角色证据的指标全部塞进攻击分组。
  F-2 组织名称子串匹配直接判 victim（企业名误标受害资产）。
  F-3 "verified" 由结构完整性推导（is_verified）冒充事实核验。
  F-4 去重按 value 全局去重——丢失其他来源（违反"保留全部来源"）。
  F-5 invalid AI 派生指标混入攻击分组。

运行：python tests/test_batch7_f.py
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

env = TestEnv(forbid_network=True).start()
import app.db as db  # noqa: E402

db.DATA_DIR = env.tmp
db._conn = None
db.init_db()

import app.integrity as integ  # noqa: E402
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


def mk(tweet_id, text, orgs=None, iocs=None, **fields):
    with db._lock:
        db.get_conn().execute(
            "INSERT INTO tweets (id, screen_name, tweet_id, text, url, published_at,"
            " created_at, org, ai_iocs) VALUES (?,?,?,?,?,?,?,?,?)",
            (int(tweet_id), "FSrc", f"ext-{tweet_id}", text, f"u{tweet_id}",
             db.now(), db.now(),
             json.dumps(orgs or [], ensure_ascii=False), json.dumps(iocs or [], ensure_ascii=False)))
        db.get_conn().commit()
    if fields:
        db.update_ai(tweet_id, fields)
    return int(tweet_id)


# ============================================================
print("== F1 缺角色证据默认 unknown（绝不默认 attack） ==")
t1 = mk(601, "普通引用 1.2.3.4 与 evil.example.com", orgs=[],
        iocs=["1.2.3.4", "evil.example.com"], ai_integrity="verified",
        ai_assessment="完整研判", ai_repair="")
uid, _ = db.create_event("F1 事件", tweet_ids=[t1], operator="f")
data = eventexport.export_event_json(uid)
check("F1-1 ip/domain 无角色证据 → reference 桶",
      len(data["iocs"]["reference"]) == 2 and len(data["iocs"]["attack"]) == 0,
      str({k: len(v) for k, v in data["iocs"].items()}))
check("F1-2 role=unknown（不是 attack）",
      all(r["role"] == "unknown" for r in data["iocs"]["reference"]),
      str([r["role"] for r in data["iocs"]["reference"]]))

# ============================================================
print("== F2 组织名子串不再决定 victim（只留 hint） ==")
# 复现历史误判场景：短企业名 "ACME" 与域名子串重合——旧实现直接判 victim
t2 = mk(602, "ACME 集团披露 acme-cdn.com 遭滥用", orgs=["ACME"],
        iocs=["acme-cdn.com"], ai_integrity="verified",
        ai_assessment="完整研判", ai_repair="")
uid2, _ = db.create_event("F2 事件", tweet_ids=[t2], operator="f")
data2 = eventexport.export_event_json(uid2)
check("F2-1 组织名子串命中也不进 victim 桶",
      len(data2["iocs"]["victim"]) == 0 and len(data2["iocs"]["reference"]) == 1,
      str({k: len(v) for k, v in data2["iocs"].items()}))
rec2 = data2["iocs"]["reference"][0]
check("F2-2 org_overlap 仅作人工复核 hint", rec2["hints"].get("org_overlap") is True,
      str(rec2["hints"]))

# ============================================================
print("== F3 CVE 独立 vulnerability ==")
t3 = mk(603, "CVE-2026-12345 利用披露", orgs=[], iocs=["CVE-2026-12345"],
        ai_integrity="verified", ai_assessment="完整", ai_repair="")
uid3, _ = db.create_event("F3 事件", tweet_ids=[t3], operator="f")
data3 = eventexport.export_event_json(uid3)
check("F3-1 CVE 进 cve 桶且 role=vulnerability",
      len(data3["iocs"]["cve"]) == 1 and data3["iocs"]["cve"][0]["role"] == "vulnerability",
      str(data3["iocs"]["cve"]))

# ============================================================
print("== F4 事实核验不由结构完整性推导 ==")
rec4 = data3["iocs"]["cve"][0]
check("F4-1 结构完整但未人工审核 → verified=false（核验≠完整）",
      rec4["integrity"] == "verified" and rec4["verified"] is False,
      f"integrity={rec4['integrity']} verified={rec4['verified']}")
# 人工审核绑定当前版本 → verified=true
ver4 = int(db.get_tweet(t3)["analysis_version"] or 0)
db.bind_review(t3, ver4)
data3b = eventexport.export_event_json(uid3)
rec4b = data3b["iocs"]["cve"][0]
check("F4-2 人工审核绑定当前版本 → verified=true",
      rec4b["verified"] is True and rec4b["integrity"] == "verified",
      f"verified={rec4b['verified']}")
# 版本推进 → 审核失效 → verified 回落 false
db.bump_analysis_version(t3)
db.ensure_event_freshness(uid3)
data3c = eventexport.export_event_json(uid3)
rec4c = data3c["iocs"]["cve"][0]
check("F4-3 内容变化后核验状态回落 false", rec4c["verified"] is False,
      f"verified={rec4c['verified']}")

# ============================================================
print("== F5 invalid 独立桶 + partial 逐条标注 ==")
t5 = mk(605, "invalid 来源的指标 5.6.7.8", orgs=[], iocs=["5.6.7.8"],
        ai_integrity="invalid", ai_assessment="", ai_repair="缺少 assessment",
        ai_status="error")
t5b = mk(606, "partial 来源的指标 9.10.11.12", orgs=[], iocs=["9.10.11.12"],
         ai_integrity="partial", ai_assessment="部分结果", ai_repair="JSON 经修复解析（repaired）")
uid5, _ = db.create_event("F5 事件", tweet_ids=[t5, t5b], operator="f")
data5 = eventexport.export_event_json(uid5)
check("F5-1 invalid 来源指标 → invalid 桶（不进 reference/attack）",
      len(data5["iocs"]["invalid"]) == 1 and len(data5["iocs"]["reference"]) == 1,
      str({k: len(v) for k, v in data5["iocs"].items()}))
rec5 = data5["iocs"]["invalid"][0]
check("F5-2 invalid 桶带独立标注", rec5["invalid_reason"] != "" and
      rec5["usable_as_conclusion"] is False, str(rec5.get("invalid_reason")))
rec5b = data5["iocs"]["reference"][0]
check("F5-3 partial 逐条标注：不可作结论 + 须标注未完成",
      rec5b["integrity"] == "partial" and rec5b["usable_as_conclusion"] is False
      and rec5b["must_label_unfinished"] is True,
      f"{rec5b['integrity']}/{rec5b['usable_as_conclusion']}")

# ============================================================
print("== F6 去重保留全部来源与角色冲突 ==")
t6a = mk(607, "来源A 报告 13.14.15.16", orgs=[], iocs=["13.14.15.16"],
         ai_integrity="verified", ai_assessment="A 完整", ai_repair="")
t6b = mk(608, "来源B 报告同一指标 13.14.15.16（invalid）", orgs=[],
         iocs=["13.14.15.16"], ai_integrity="invalid", ai_assessment="",
         ai_repair="缺少 assessment", ai_status="error")
uid6, _ = db.create_event("F6 事件", tweet_ids=[t6a, t6b], operator="f")
data6 = eventexport.export_event_json(uid6)
ref6 = data6["iocs"]["reference"]
inv6 = data6["iocs"]["invalid"]
check("F6-1 同一 value 来自两条来源全部保留（不全局去重丢源）",
      len(ref6) == 1 and len(inv6) == 1, str({k: len(v) for k, v in data6["iocs"].items()}))
check("F6-2 source_count=2", ref6[0]["source_count"] == 2, str(ref6[0]["source_count"]))
check("F6-3 角色冲突标注（reference/invalid 两桶）",
      ref6[0]["role_conflict"] is True and inv6[0]["role_conflict"] is True,
      f"{ref6[0]['role_conflict']}/{inv6[0]['role_conflict']}")
# 同一条 tweet 内重复 value → 去重
t6c = mk(609, "同源重复 13.14.15.16 与 13.14.15.16", orgs=[],
         iocs=["13.14.15.16", "13.14.15.16"], ai_integrity="verified",
         ai_assessment="C 完整", ai_repair="")
db.add_evidence(uid6, t6c)
data6b = eventexport.export_event_json(uid6)
same_src = [r for r in data6b["iocs"]["reference"] if r["tweet_id"] == t6c]
check("F6-4 同源同值只保留一条", len(same_src) == 1, str(len(same_src)))

# ============================================================
print("== F7 JSON/CSV 一致 + 公式注入防护 ==")
csv_text = eventexport.export_event_ioc_csv(uid6)
rows = list(csv.reader(io.StringIO(csv_text)))
total_recs = sum(len(v) for v in data6b["iocs"].values())
check("F7-1 CSV 数据行数 = JSON 各桶总数", len(rows) - 1 == total_recs,
      f"{len(rows)-1} vs {total_recs}")
header = rows[0]
check("F7-2 CSV 列与新口径一致", "structural_integrity" in header and
      "usable_as_conclusion" in header and "role_conflict" in header, str(header))
# 公式注入：构造 = 开头的"指标"（kind=other → unknown 桶）
t7 = mk(610, "可疑字符串 =1+1", orgs=[], iocs=["=1+1"], ai_integrity="verified",
        ai_assessment="完整", ai_repair="")
uid7, _ = db.create_event("F7 事件", tweet_ids=[t7], operator="f")
csv7 = eventexport.export_event_ioc_csv(uid7)
row7 = [r for r in csv.reader(io.StringIO(csv7)) if r and r[2].endswith("1+1")]
check("F7-3 CSV 公式注入被转义（前导 '）", row7 and row7[0][2].startswith("'="),
      str(row7[:1]))

print(f"\n==== F 组 IOC 导出：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
