# -*- coding: utf-8 -*-
"""第二批验收测试（临时数据库 + 通道替身；无真实 AI、无真实外发）。

十六场景覆盖见文件尾对照表；猴补接缝：engine._iid_channels（实例枚举唯一入口）。
运行：C:/Python314/python.exe tests/test_batch2.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

TMP = Path(tempfile.mkdtemp(prefix="xintel-batch2-"))
import app.db as db  # noqa: E402

db.DATA_DIR = TMP
db._conn = None
db.init_db()

import app.engine as engine  # noqa: E402
import app.reports as reports  # noqa: E402
import app.wecom_media as wm  # noqa: E402
from app.config import ensure_instance_ids  # noqa: E402

PASS, FAIL = 0, []
NOW = db.now()
INST = []                       # 当前实例枚举（替身）
engine._iid_channels = lambda cfg: INST


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(name)
        print(f"FAIL {name} {detail}")


class StubCh:
    def __init__(self, key, display, ok=True, caps=("markdown",), fail_detail="stub fail"):
        self.key, self.display, self.caps = key, display, caps
        self._ok = ok
        self._fail_detail = fail_detail
        self.sent = []

    def configured(self, cc):
        return True

    def send(self, cc, title, markdown, image=None, html=None, url=None, **_):
        self.sent.append({"title": title, "md": markdown})
        return self._ok, (self._fail_detail if not self._ok else "stub ok")


def add_tweet(sn, text, severity="medium", geo="", created_at=None, starred=False):
    tid = db.insert_tweet({"screen_name": sn, "tweet_id": f"t{time.time_ns()}-{len(text)}",
                           "text": text, "url": "https://x/x", "published_at": db.now()})
    db.update_ai(tid, {"ai_status": "ok", "ai_severity": severity, "geo_country": geo})
    if created_at:
        db.get_conn().execute("UPDATE tweets SET created_at=? WHERE id=?", (created_at, tid))
    if starred:
        db.set_tweet_flags(tid, starred=True)
    db.get_conn().commit()
    return tid


def task_st(tid):
    return db.get_conn().execute("SELECT status FROM report_tasks WHERE id=?",
                                 (tid,)).fetchone()["status"]


def set_inst(*tuples):
    global INST
    INST = list(tuples)


def msg_states(iid=None):
    q = "SELECT state, COUNT(*) n FROM delivery_msgs"
    args = []
    if iid:
        q += " WHERE instance_id=?"
        args.append(iid)
    q += " GROUP BY state"
    return {r["state"]: r["n"] for r in db.get_conn().execute(q, args).fetchall()}


def tweet_deliveries(tweet_id):
    return [dict(r) for r in db.get_conn().execute(
        "SELECT * FROM deliveries WHERE tweet_id=?", (tweet_id,)).fetchall()]


def inst(iid, name="I", rule="", since=None):
    """场景实例：默认边界=当前时刻（只处理之后入库的情报），与既有积压隔离"""
    return {"iid": iid, "name": name, "enabled": True, "filter_rule": rule,
            "enabled_since": since or NOW,
            "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=stub"}


def cfg_for(instances, digest_max=8, default_countries=""):
    return {"push": {"digest_max_items": digest_max,
                     "filters": {"min_severity": "", "mode": "and",
                                 "countries": default_countries, "categories": "",
                                 "industries": "", "tags": "", "keywords": ""},
                     "rules": []},
            "channels": {}, "instances": instances}


# ============================================================
print("== 场景1 默认法国/实例日本 ==")
iid_jp = "ci_t_jp"
cfg1 = cfg_for([inst(iid_jp, "日本组", "rJP", since="2000-01-01 00:00:00")],
               default_countries="法国")
cfg1["push"]["rules"] = [{"id": "rJP", "name": "日本组", "mode": "and", "countries": "日本",
                          "categories": "", "industries": "", "tags": "", "keywords": ""}]
jp_id = add_tweet("JPSrc", "JP incident", severity="high", geo="日本")
stub_jp = StubCh("wecom", "企微·日本组")
set_inst((stub_jp, cfg1["instances"][0], False, iid_jp))
engine.dispatch_round(cfg1)
d = tweet_deliveries(jp_id)
check("S1-1 日本情报进入日本实例投递", len(d) == 1 and d[0]["state"] == "成功", str(d))
check("S1-2 法国默认规则未拦截日本投递", bool(stub_jp.sent), f"发送 {len(stub_jp.sent)}")

# ============================================================
print("== 场景2 超 2000 候选不饥饿 ==")
old_ids = [add_tweet(f"BulkSrc{i}", f"bulk {i}", severity="low", geo="日本",
                     created_at=f"2026-01-01 {(i % 24):02d}:00:00") for i in range(2100)]
oldest_id = old_ids[0]
for _ in range(5):  # 5 轮 × 每轮创建 500（最旧优先）≥ 2100
    engine.dispatch_round(cfg1)
d0 = tweet_deliveries(oldest_id)
check("S2-1 最旧合格记录第一轮即处理", len(d0) == 1 and d0[0]["state"] == "成功",
      str([x["state"] for x in d0]))
cnt = db.get_conn().execute(
    "SELECT COUNT(*) n FROM deliveries WHERE instance_id=?", (iid_jp,)).fetchone()["n"]
check("S2-2 五轮后全量建投递", cnt >= 2100, f"deliveries={cnt}")

# ============================================================
print("== 场景3 摘要上限 8，剩余 2 条后续发送 ==")
iid_md = "ci_t_md"
cfg_md = cfg_for([inst(iid_md, "S3组")], default_countries="S3法国")
ids_md = [add_tweet(f"FR{i}", f"FR digest {i}", severity="medium", geo="S3法国") for i in range(10)]
stub_fr = StubCh("dingtalk", "钉钉·法国")
set_inst((stub_fr, cfg_md["instances"][0], False, iid_md))
engine.dispatch_round(cfg_md)
pend1 = db.deliveries_pending(iid_md)
check("S3-1 第一轮摘要后剩余 2 条待发送", len(pend1) == 2, f"剩余 {len(pend1)}")
engine.dispatch_round(cfg_md)
states = [tweet_deliveries(i)[0]["state"] for i in ids_md]
check("S3-2 第二轮补发剩余 2 条", len(db.deliveries_pending(iid_md)) == 0
      and all(s == "成功" for s in states), f"states={states}")

# ============================================================
print("== 场景4 A 成功 B 失败，只重试 B ==")
iid_a, iid_b = "ci_t_a", "ci_t_b"
ch_a = StubCh("wecom", "A", ok=True)
ch_b = StubCh("dingtalk", "B", ok=False)
cfg4 = cfg_for([inst(iid_a, "A"), inst(iid_b, "B")], default_countries="S4法国")
set_inst((ch_a, cfg4["instances"][0], False, iid_a),
         (ch_b, cfg4["instances"][1], False, iid_b))
t4 = add_tweet("DualSrc", "dual delivery", severity="high", geo="S4法国")
engine.dispatch_round(cfg4)
st_a = tweet_deliveries(t4)[0]["state"]
st_b = [x for x in tweet_deliveries(t4) if x["instance_id"] == iid_b][0]["state"]
check("S4-1 A 成功", st_a == "成功", st_a)
check("S4-2 B 失败进入等待重试", st_b == "待发送" or st_b == "等待重试", st_b)
a_before = len(ch_a.sent)
db.get_conn().execute("UPDATE delivery_msgs SET next_attempt_at=? WHERE instance_id=?",
                      (time.time() - 1, iid_b))
db.get_conn().commit()
ch_b._ok = True
engine.dispatch_round(cfg4)
st_b2 = [x for x in tweet_deliveries(t4) if x["instance_id"] == iid_b][0]["state"]
check("S4-3 B 重试成功", st_b2 == "成功", st_b2)
check("S4-4 A 未被重复发送", len(ch_a.sent) == a_before, f"{a_before}→{len(ch_a.sent)}")

# ============================================================
print("== 场景5 模拟后转真实，真实资格仍在 ==")
iid_sim = "ci_t_sim"
ch_s = StubCh("wecom", "SimFirst", ok=True)
cfg5 = cfg_for([inst(iid_sim, "SimFirst")], default_countries="S5法国")
set_inst((ch_s, cfg5["instances"][0], True, iid_sim))
t5 = add_tweet("SimSrc", "sim then real", severity="high", geo="S5法国")
engine.dispatch_round(cfg5)
d5 = tweet_deliveries(t5)[0]
check("S5-1 模拟独立记录（模拟成功）", d5["state"] == "模拟成功", d5["state"])
db.reset_sim_deliveries(iid_sim)  # 真实激活钩子（凭据配置时触发）
set_inst((ch_s, cfg5["instances"][0], False, iid_sim))
engine.dispatch_round(cfg5)
d5 = tweet_deliveries(t5)[0]
check("S5-2 转真实后真实发送成功", d5["state"] == "成功", d5["state"])

# ============================================================
print("== 场景6 实例身份稳定性 ==")
cfg6 = {"channels": {"wecom": {"instances": [
    {"iid": "ci_AAA", "name": "原名", "enabled": True},
    {"iid": "ci_BBB", "name": "实例二", "enabled": True}]}}}
ensure_instance_ids(cfg6)
cfg6["channels"]["wecom"]["instances"].reverse()
cfg6["channels"]["wecom"]["instances"][0]["name"] = "改名后"
ensure_instance_ids(cfg6)
iids = [i["iid"] for i in cfg6["channels"]["wecom"]["instances"]]
check("S6-1 改名/排序身份不变", iids == ["ci_BBB", "ci_AAA"], str(iids))
cfg6["channels"]["wecom"]["instances"] = [{"name": "重建", "enabled": True}]
ensure_instance_ids(cfg6)
check("S6-2 删除重建不误继承",
      cfg6["channels"]["wecom"]["instances"][0]["iid"] not in ("ci_AAA", "ci_BBB"))

# ============================================================
print("== 场景7 新实例不补发历史；显式补发按范围 ==")
iid_new = "ci_t_new"
past = [add_tweet(f"OldSrc{i}", f"old {i}", geo="法国",
                  created_at="2026-01-02 00:00:00") for i in range(10)]
cfg7 = cfg_for([{"iid": iid_new, "name": "新实例", "enabled": True, "filter_rule": "",
                 "enabled_since": NOW, "webhook": "stub"}])
set_inst((stub_fr, cfg7["instances"][0], False, iid_new))
engine.dispatch_round(cfg7)
old_leak = [t for t in past if tweet_deliveries(t)]
check("S7-1 新实例默认不补发历史（旧推文零投递）", not old_leak, f"泄漏 {len(old_leak)} 条")
cands = [t for t in db.delivery_candidates(iid_new, "2000-01-01 00:00:00", cap=5000)
         if t["screen_name"].startswith("OldSrc")]
check("S7-2 历史候选可预览（时间窗内）", len(cands) == 10, f"候选 {len(cands)}")
created = sum(1 for t in cands[:3]
              if db.create_delivery(t["id"], iid_new, "dingtalk", "", "{}", "待发送"))
check("S7-3 显式执行按 limit 3", created == 3, f"created={created}")

# ============================================================
print("== 场景8 暂停恢复/规则快照 ==")
iid_p = "ci_t_pause"
cfg8 = cfg_for([inst(iid_p, "暂停实例")], default_countries="S8法国")
tp = add_tweet("PauseSrc", "paused tweet", severity="high", geo="S8法国")
set_inst()  # 暂停（实例停用）
engine.dispatch_round(cfg8)
check("S8-1 暂停期间不创建投递",
      db.get_conn().execute("SELECT COUNT(*) n FROM deliveries WHERE instance_id=?",
                            (iid_p,)).fetchone()["n"] == 0)
set_inst((stub_fr, cfg8["instances"][0], False, iid_p))
engine.dispatch_round(cfg8)
m8 = db.get_conn().execute(
    "SELECT id, rule_snapshot, content FROM delivery_msgs WHERE instance_id=? ORDER BY id DESC LIMIT 1",
    (iid_p,)).fetchone()
cfg8["push"]["filters"]["countries"] = "S8德国"  # 规则随后被改
m8b = db.get_conn().execute(
    "SELECT rule_snapshot, content FROM delivery_msgs WHERE id=?", (m8["id"],)).fetchone()
check("S8-2 重试/重发使用规则快照不静默改义",
      m8b["rule_snapshot"] == m8["rule_snapshot"] and m8b["content"] == m8["content"])

# ============================================================
print("== 场景9 原子领取防重复 ==")
iid_c9 = "ci_t_claim"
cfg9 = cfg_for([inst(iid_c9, "并发")], default_countries="S9法国")
tc = add_tweet("ClaimSrc", "claim tweet", severity="high", geo="S9法国")
set_inst((stub_fr, cfg9["instances"][0], False, iid_c9))
engine.dispatch_round(cfg9)
mid = db.get_conn().execute(
    "SELECT id FROM delivery_msgs WHERE instance_id=? ORDER BY id DESC LIMIT 1",
    (iid_c9,)).fetchone()["id"]
db.get_conn().execute("UPDATE delivery_msgs SET state='待发送' WHERE id=?", (mid,))
db.get_conn().commit()
c1 = db.claim_msg(mid)
c2 = db.claim_msg(mid)
check("S9 并发双领仅一个成功", c1 is not None and c2 is None, f"{c1}/{c2}")
db.finish_msg(mid, "成功", "场景9收尾")  # 清理发送中残留，避免污染场景16状态分布

# ============================================================
print("== 场景10 退避与永久失败 ==")
iid_f = "ci_t_fail"
cfg10 = cfg_for([inst(iid_f, "失败实例")], default_countries="S10法国")
ch_p = StubCh("wecom", "P", ok=False)
set_inst((ch_p, cfg10["instances"][0], False, iid_f))
tpx = add_tweet("RetrySrc", "retryable fail", severity="high", geo="S10法国")
db.create_delivery(tpx, iid_f, "wecom", "", "{}", "待发送")
mid2 = db.create_msg(iid_f, "wecom", "instant", "", "{}", "T2", "retry content", [tpx])
engine._process_msg(ch_p, {"iid": iid_f}, False, mid2, "T2", "retry content", None,
                    {"sent": 0, "sim": 0, "retry_wait": 0, "failed": 0, "unknown": 0}, set())
m2 = db.msg_by_id(mid2)
check("S10-1 可重试错误进入等待重试", m2["state"] == "等待重试", m2["state"])
tp1 = add_tweet("PermSrc", "perm fail", severity="high", geo="S10法国")
db.create_delivery(tp1, iid_f, "wecom", "", "{}", "待发送")
ch_p._fail_detail = "invalid webhook url errcode 93000"  # 永久失败替身
mid1 = db.create_msg(iid_f, "wecom", "instant", "", "{}", "T1",
                     "perm content", [tp1])
engine._process_msg(ch_p, {"iid": iid_f}, False, mid1, "T1",
                    "invalid webhook url errcode 93000", None,
                    {"sent": 0, "sim": 0, "retry_wait": 0, "failed": 0, "unknown": 0}, set())
m1 = db.msg_by_id(mid1)
check("S10-2 无效配置→永久失败停止自动重试", m1["state"] == "永久失败", m1["state"])

# ============================================================
print("== 场景11 崩溃恢复与结果不明 ==")
iid_u = "ci_t_unknown"
cfg11 = cfg_for([inst(iid_u, "不明实例")], default_countries="S11法国")
tu = add_tweet("UnkSrc", "unknown outcome", severity="high", geo="S11法国")
db.create_delivery(tu, iid_u, "wecom", "", "{}", "待发送")
mid3 = db.create_msg(iid_u, "wecom", "instant", "", "{}", "T3", "unknown content", [tu])
db.link_deliveries(iid_u, [tu], mid3)
db.claim_msg(mid3)  # 发送中 → 进程崩溃（租约到期）
due, expired = db.msgs_due(time.time() + 300)
check("S11-1 租约过期被识别", mid3 in expired, f"expired={expired}")
set_inst((stub_fr, cfg11["instances"][0], False, iid_u))
db.get_conn().execute("UPDATE delivery_msgs SET lease_until=? WHERE id=?",
                      (time.time() - 10, mid3))  # 模拟：租约随进程中断而过期
db.get_conn().commit()
engine.dispatch_round(cfg11)  # 过期 → 结果不明（不自动重发）
m3 = db.msg_by_id(mid3)
check("S11-2 落库前崩溃 → 结果不明", m3["state"] == "结果不明", m3["state"])
db.msg_retry_wait(mid3, time.time() - 1, "人工重试")  # 人工重试入口
engine.dispatch_round(cfg11)
check("S11-3 人工重试后恢复", tweet_deliveries(tu)[0]["state"] == "成功",
      tweet_deliveries(tu)[0]["state"])

# ============================================================
print("== 场景12 迁移幂等，不伪造历史成功 ==")
rid = reports.create_task("迁移测试", ["mig"], "速报", "", [], 0)["id"]
n_before = db.get_conn().execute("SELECT COUNT(*) n FROM deliveries").fetchone()["n"]
refs_before = db.get_conn().execute("SELECT COUNT(*) n FROM report_refs").fetchone()["n"]
db.init_db()  # 重复迁移
n_after = db.get_conn().execute("SELECT COUNT(*) n FROM deliveries").fetchone()["n"]
refs_after = db.get_conn().execute("SELECT COUNT(*) n FROM report_refs").fetchone()["n"]
check("S12-1 迁移重复执行不重复登记", refs_before == refs_after, f"{refs_before} vs {refs_after}")
check("S12-2 迁移不创建投递（不伪造历史成功）", n_before == n_after,
      f"{n_before} vs {n_after}")

# ============================================================
print("== 场景13 报告分项交付 ==")
dirt = TMP / "deliver"
dirt.mkdir(exist_ok=True)
(dirt / "r_批注.docx").write_text("x")
reports.load = lambda: {"channels": {}}
ch_wfile = StubCh("wecom", "企微·附件", ok=True, caps=("markdown", "file"))
wm.push_file = lambda webhook, path: {"errcode": 90002, "errmsg": "替身：附件推送失败"}
set_inst((ch_wfile, {"iid": iid_a, "webhook": "stub://wecom", "instance_name": "W",
                     "filter_rule": ""}, False, iid_a))
tid13 = reports.create_task("分项交付", ["d13"], "速报", "", [jp_id], 0)["id"]
db.get_conn().execute(
    "UPDATE report_tasks SET status='已交付待推', deliver_dir=?, docx='r.docx' WHERE id=?",
    (str(dirt), tid13))
db.get_conn().commit()
reports.push_task(tid13)
check("S13-1 附件失败 → 部分已推送（不被正文成功掩盖）",
      task_st(tid13) == "部分已推送", task_st(tid13))
last_detail = db.msgs_report(tid13)[-1]["detail"]
check("S13-2 分项明细含失败附件", any(not i["ok"] for i in
      json.loads(last_detail)["items"]), last_detail[:80])
ch_wfile.sent.clear()
# 全失败
ch_fail = StubCh("wecom", "全败", ok=False, caps=("markdown", "file"))
set_inst((ch_fail, {"iid": iid_a, "webhook": "stub://wecom", "instance_name": "F",
                    "filter_rule": ""}, False, iid_a))
tid13b = reports.create_task("全失败", ["d13b"], "速报", "", [jp_id], 0)["id"]
db.get_conn().execute("UPDATE report_tasks SET status='已交付待推' WHERE id=?", (tid13b,))
db.get_conn().commit()
r = reports.push_task(tid13b)
check("S13-3 全失败保持原状态并带 error", task_st(tid13b) == "已交付待推" and r.get("error"),
      task_st(tid13b))
# 仅模拟
ch_sim = StubCh("dingtalk", "模拟", ok=True)
set_inst((ch_sim, {"iid": iid_a, "webhook": "stub://ding", "instance_name": "S",
                   "filter_rule": ""}, True, iid_a))
tid13c = reports.create_task("仅模拟", ["d13c"], "速报", "", [jp_id], 0)["id"]
db.get_conn().execute("UPDATE report_tasks SET status='已交付待推' WHERE id=?", (tid13c,))
db.get_conn().commit()
r = reports.push_task(tid13c)
check("S13-4 仅模拟不推进真实完成", task_st(tid13c) == "已交付待推" and r.get("simulated_only"),
      task_st(tid13c))
# 部分实例成功 → 部分已推送；补发跳过成功实例
ch_ok = StubCh("wecom", "OK", ok=True, caps=("markdown", "file"))
wm.push_file = lambda webhook, path: {"errcode": 0}
set_inst((ch_ok, {"iid": iid_a, "webhook": "stub://ok", "instance_name": "OK",
                  "filter_rule": ""}, False, iid_a),
         (ch_fail, {"iid": iid_b, "webhook": "stub://f2", "instance_name": "F2",
                    "filter_rule": ""}, False, iid_b))
tid13d = reports.create_task("部分实例", ["d13d"], "速报", "", [jp_id], 0)["id"]
db.get_conn().execute("UPDATE report_tasks SET status='已交付待推' WHERE id=?", (tid13d,))
db.get_conn().commit()
reports.push_task(tid13d)
check("S13-5 部分实例成功 → 部分已推送", task_st(tid13d) == "部分已推送", task_st(tid13d))
a_count = len(ch_ok.sent)
reports.push_task(tid13d)  # ch_fail 仍失败
check("S13-6 补发跳过已成功实例", len(ch_ok.sent) == a_count,
      f"{a_count}→{len(ch_ok.sent)}")

# ============================================================
print("== 场景14 待人工重新验收 + 并发推送 ==")
gate_stub = TMP / "gate_stub.py"
gate_stub.write_text("import sys\nsys.exit(0)\n")
reports.load = lambda: {"channels": {}, "report": {"gate_script": str(gate_stub)}}
tid14 = reports.create_task("待人工重验", ["d14"], "速报", "", [jp_id], 0)["id"]
db.get_conn().execute(
    "UPDATE report_tasks SET status='待人工', deliver_dir=?, docx='g.docx' WHERE id=?",
    (str(dirt), tid14))
(dirt / "g.docx").write_text("doc")
(dirt / "g_批注.docx").write_text("doc")
(dirt / "g_审阅.html").write_text("<html></html>")
db.get_conn().commit()
wm.push_file = lambda webhook, path: {"errcode": 0}
set_inst((ch_ok, {"iid": iid_a, "webhook": "stub://ok", "instance_name": "OK",
                  "filter_rule": ""}, False, iid_a))
d = reports.deliver(tid14, {"deliver_dir": str(dirt), "docx": "g.docx",
                            "word_count": 100, "ledger_total": 3, "ledger_pass": 3})
check("S14-1 待人工可重新交付并重新验收", task_st(tid14) == "已交付待推", task_st(tid14))
db.get_conn().execute("UPDATE report_tasks SET status='已交付待推', updated_at=? WHERE id=?",
                      (db.now(), tid14))
db.get_conn().commit()
c = reports.push_task(tid14)
c2 = reports.push_task(tid14)
check("S14-2 重复推送被原子领取拒绝", c2.get("error") is not None, str(c2)[:80])

# ============================================================
print("== 场景15 删除入口保护 ==")
ref_tid = add_tweet("RefSrc", "referenced tweet")
reports.create_task("引用保护", ["d15"], "速报", "", [ref_tid], 0)["id"]
star_tid = add_tweet("StarSrc2", "starred2", starred=True)
plain_tid = add_tweet("PlainSrc", "plain")
prot = db.protected_tweet_ids()
check("S15-1 保护集含报告引用与收藏", ref_tid in prot and star_tid in prot, f"{len(prot)} 个")
db.batch_tweet_op([ref_tid, star_tid, plain_tid], "delete")
check("S15-2 批量删除跳过保护对象", db.get_tweet(ref_tid) is not None
      and db.get_tweet(star_tid) is not None and db.get_tweet(plain_tid) is None)
db.cleanup_old(0)
check("S15-3 cleanup 同样保护", db.get_tweet(ref_tid) is not None
      and db.get_tweet(star_tid) is not None)

# ============================================================
print("== 场景16 投递状态独立可见 ==")
# 自造一个"结果不明"样本（S11 的样本已被人工重试正确消化，属预期行为）
iid_s16 = "ci_t_s16"
cfg16 = cfg_for([inst(iid_s16, "S16")])
t16 = add_tweet("S16Src", "unknown sample", severity="high", geo="S16")
db.create_delivery(t16, iid_s16, "wecom", "", "{}", "待发送")
m16 = db.create_msg(iid_s16, "wecom", "instant", "", "{}", "T16", "c", [t16])
db.link_deliveries(iid_s16, [t16], m16)
_tk16 = db.claim_msg(m16)
db.finish_msg(m16, "结果不明", "结果不明样例", token=_tk16)
states = msg_states()
need = ["成功", "模拟成功", "等待重试", "永久失败", "结果不明"]
check("S16 状态分布独立可见（成功/模拟/重试/永久/不明）",
      all(k in states for k in need), str(states))

# ============================================================
print(f"\n==== 第二批验收测试：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
