# -*- coding: utf-8 -*-
"""C 组：审核版本绑定与完整性三表达（2026-11-09 集中修复）。

历史缺陷（基线 b4c720d 复核）：
  C-1 effective_grade() 只看 ai_review=manual，未验证 ai_review_version——
      内容变化（版本推进）后旧审核仍冒充当前批准。
  C-2 审核绑定经 update_ai 直接写库，无原子版本检查（审的不是所写版本）。
  C-3 结构完整性、人工认可、核验状态混在一个字段里表达，
      人工审核"抹掉"原始 partial/invalid。

运行：python tests/test_batch7_c.py
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


def mk(text, **fields):
    tid = db.insert_tweet({"screen_name": "CSrc", "tweet_id": f"c-{text[:20]}-{db.now()}",
                           "text": text, "url": "u", "published_at": db.now()})
    if fields:
        db.update_ai(tid, fields)
    return tid


# ============================================================
print("== C1 审核绑定当前版本 → 人工认可有效 ==")
tid1 = mk("c1-review-valid", ai_integrity="partial", ai_repair="JSON 经修复解析（repaired）",
          ai_assessment="部分研判")
base1 = int(db.get_tweet(tid1)["analysis_version"] or 0)
bound = db.bind_review(tid1, base1)
row1 = db.get_tweet(tid1)
state, rev_v, cur_v = integ.review_state(row1)
check("C1-1 原子绑定成功且记录版本", bound == base1
      and int(row1["ai_review_version"] or 0) == base1,
      f"bound={bound}")
check("C1-2 review_state=valid", state == "valid", f"{state}/{rev_v}/{cur_v}")
check("C1-3 有效等级升级 verified（人工认可）", integ.effective_grade(row1) == "verified",
      integ.effective_grade(row1))
check("C1-4 原始结构分级 partial 保留不被抹掉", integ.structural_grade(row1) == "partial",
      integ.structural_grade(row1))
check("C1-5 修复痕迹保留", bool(row1["ai_repair"]), str(row1["ai_repair"]))

# ============================================================
print("== C2 版本推进后旧审核自动失效（历史决定不冒充当前批准） ==")
db.bump_analysis_version(tid1)  # 内容变化（AI 结果落地/原文更新/人工修订都会推进）
row2 = db.get_tweet(tid1)
state2, rev2, cur2 = integ.review_state(row2)
check("C2-1 review_state=stale", state2 == "stale", f"{state2}/{rev2}/{cur2}")
check("C2-2 有效等级回落结构分级 partial",
      integ.effective_grade(row2) == "partial", integ.effective_grade(row2))
allowed2, must_label2, reason2 = integ.may_auto_downstream(row2)
check("C2-3 下游恢复「须标注未完成」门控", allowed2 is True and must_label2 is True, reason2)
check("C2-4 旧审核记录保留（历史决定可查）",
      str(row2["ai_review"]) == "manual" and int(row2["ai_review_version"] or 0) == base1,
      f"{row2['ai_review']}/{row2['ai_review_version']}")

# ============================================================
print("== C3 审核原子绑定竞争：处理期间版本被推进 → 拒绝 ==")
tid3 = mk("c3-review-race", ai_integrity="verified")
stale_base = int(db.get_tweet(tid3)["analysis_version"] or 0) - 1  # 构造"读到旧版本"
bound3 = db.bind_review(tid3, stale_base)
check("C3-1 版本不符绑定被拒", bound3 is None, str(bound3))
row3 = db.get_tweet(tid3)
check("C3-2 未留下错误绑定", not row3["ai_review_version"], str(row3["ai_review_version"]))

# ============================================================
print("== C4 无版本记录的历史审核数据保守按 stale ==")
tid4 = mk("c4-legacy-review", ai_integrity="partial")
db.update_ai(tid4, {"ai_review": "manual"})  # 模拟历史数据：无 ai_review_version
state4, _, _ = integ.review_state(db.get_tweet(tid4))
check("C4-1 未记录绑定版本 → stale（不得冒充当前批准）", state4 == "stale", state4)
check("C4-2 有效等级不被无版本审核升级",
      integ.effective_grade(db.get_tweet(tid4)) == "partial",
      integ.effective_grade(db.get_tweet(tid4)))

# ============================================================
print("== C5 导出三表达一致（证据含结构/有效/审核三块） ==")
ev5 = eventexport._evidence_item(db.get_tweet(tid1))  # 已推进版本：stale
ai5 = ev5["ai"]
check("C5-1 导出含原始结构分级", ai5["structural_integrity"] == "partial",
      str(ai5["structural_integrity"]))
check("C5-2 导出审核块为三态结构", ai5["review"]["state"] == "stale"
      and ai5["review"]["reviewed_version"] == base1, str(ai5["review"]))
check("C5-3 stale 审核 → 不可作为已核验结论",
      ai5["usable_as_conclusion"] is False and ai5["must_label_unfinished"] is True,
      f"{ai5['usable_as_conclusion']}/{ai5['must_label_unfinished']}")
ev5b = eventexport._evidence_item(db.get_tweet(tid3))  # 未审核 verified
ai5b = ev5b["ai"]
check("C5-4 未审核完整研判：none + 可作结论",
      ai5b["review"]["state"] == "none" and ai5b["usable_as_conclusion"] is True,
      str(ai5b["review"]))

# ============================================================
print("== C6 API 端点：绑定竞争返回 409，成功返回绑定版本 ==")
from fastapi.testclient import TestClient  # noqa: E402
import app.auth as auth  # noqa: E402
from app.main import app  # noqa: E402

auth.init_auth_tables()
auth.create_admin("admin", "AdminPass123")
raw_admin = auth.create_token("c6-admin", "admin")
H = {"Authorization": f"Bearer {raw_admin}"}
client = TestClient(app)  # 不进入 lifespan：不启动调度器


def _mk_api(text, **fields):
    tid = db.insert_tweet({"screen_name": "CSrcApi", "tweet_id": f"cap-{text[:16]}-{db.now()}",
                           "text": text, "url": "u", "published_at": db.now()})
    if fields:
        db.update_ai(tid, fields)
    return tid


tid6 = _mk_api("c6-api-review", ai_integrity="normalized")
r6 = client.post(f"/api/tweets/{tid6}/review", json={"note": "c6"}, headers=H)
check("C6-1 审核成功返回绑定版本", r6.status_code == 200 and
      r6.json().get("reviewed_version") == int(db.get_tweet(tid6)["analysis_version"] or 0),
      f"{r6.status_code}/{r6.text[:80]}")
# 竞争：端点内部是 读版本→绑定 两步；monkeypatch get_tweet 返回旧快照，
# 复现"两步之间内容被推进"窗口 → 端点必须 409 且不留下错误绑定
import api.routes as routes  # noqa: E402
tid6b = _mk_api("c6-api-race", ai_integrity="verified")
orig_get = routes.db.get_tweet
snap = dict(orig_get(tid6b))
db.bump_analysis_version(tid6b)  # 两步之间内容变化


def _stale_get(x):
    if x == tid6b:
        return snap  # 返回读到的旧快照（旧版本）
    return orig_get(x)


routes.db.get_tweet = _stale_get
try:
    r6b = client.post(f"/api/tweets/{tid6b}/review", json={"note": "race"}, headers=H)
finally:
    routes.db.get_tweet = orig_get
check("C6-2 版本竞争 → 409 且提示重新审核", r6b.status_code == 409, f"{r6b.status_code}/{r6b.text[:80]}")
row6b = db.get_tweet(tid6b)
check("C6-3 竞争后未留下绑定", not row6b["ai_review_version"], str(row6b["ai_review_version"]))

print(f"\n==== C 组审核版本与完整性：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
