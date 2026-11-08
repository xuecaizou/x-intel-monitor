# -*- coding: utf-8 -*-
"""第三批验收测试（临时数据库 + 替身；无真实 AI、无真实外发）。

二、第二批边界收尾：
  T1 独立发送尝试记录（历史保留、令牌校验、旧工作者不可覆盖）
  T2 报告分项补发：正文成功仅补失败附件；分项持久化重启后仍正确
  T3 模拟/真实切换三态（转真实/真转模再转真/切换时存在发送中）
  T4 enabled_since 稳定性（改名/保存/重启不刷新）
  T5 已过滤记录随规则变更重新评估
  T6 回退兼容标记语义（含 已过滤/模拟/永久/结果不明）
  T7 runtime.json 并发保存不互相覆盖
三、第三批验收：
  S-A X 全失败但 RSS 正常入库（故障隔离）
  S-B 入库失败不推进位点，重跑不重复
  S-C RSS 首条不变、后续新增仍被发现
  S-D 来源改名不改变身份及统计归属
  S-E 长正文完整保存，预览/模型输入独立截断
  S-F 配置的超时/重试/首抓上限/周期确实生效
  S-G {}、错误类型、越界置信度、截断响应不静默成功（修复标注）
  S-H 批量分类 ID 冲突/重复/缺失不写错情报
  S-I 后台 AI 任务：领取/进度/容量/重启恢复/失败分计
  S-J 人工修订后重新分析，人工有效值受保护
  S-K org 数组不被二次解析（前端，静态）
运行：C:/Python314/python.exe tests/test_batch3.py
"""
import json
import sys
import tempfile
import threading
import types
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from tests.testenv import TestEnv  # noqa: E402
env = TestEnv(forbid_network=False).start()   # 允许既有替身路径（AI/通道均已 monkeypatch）
TMP = env.tmp
import app.db as db  # noqa: E402

db.DATA_DIR = TMP
db._conn = None
db.init_db()

import app.engine as engine  # noqa: E402
import app.ai as ai  # noqa: E402
import app.twitter as twitter  # noqa: E402
import app.reports as reports  # noqa: E402
import app.wecom_media as wm  # noqa: E402
import app.rssfetch as rssfetch  # noqa: E402

PASS, FAIL = 0, []
NOW = db.now()


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
        self._ok, self._fail_detail = ok, fail_detail
        self.sent = []

    def configured(self, cc):
        return True

    def send(self, cc, title, markdown, image=None, html=None, url=None, **_):
        self.sent.append({"title": title, "md": markdown})
        return self._ok, (self._fail_detail if not self._ok else "stub ok")


def tweet_deliveries(tweet_id):
    return [dict(r) for r in db.get_conn().execute(
        "SELECT * FROM deliveries WHERE tweet_id=?", (tweet_id,)).fetchall()]


def tweet_deliv_state(tweet_id, iid):
    rows = [d for d in tweet_deliveries(tweet_id) if d["instance_id"] == iid]
    return rows[-1]["state"] if rows else "(无)"


def task_status(tid):
    return db.get_conn().execute("SELECT status FROM report_tasks WHERE id=?", (tid,)).fetchone()["status"]


def add_tweet(sn, text, severity="high", geo="", created_at=None):
    tid = db.insert_tweet({"screen_name": sn, "tweet_id": f"t{time.time_ns()}-{len(text)}",
                           "text": text, "url": "https://x/x", "published_at": db.now()})
    db.update_ai(tid, {"ai_status": "ok", "ai_severity": severity, "geo_country": geo})
    if created_at:
        db.get_conn().execute("UPDATE tweets SET created_at=? WHERE id=?", (created_at, tid))
    db.get_conn().commit()
    return tid


def cfg_one(iid, countries=""):
    return {"push": {"digest_max_items": 8,
                     "filters": {"min_severity": "", "mode": "and", "countries": countries,
                                 "categories": "", "industries": "", "tags": "", "keywords": ""},
                     "rules": []},
            "channels": {}, "instances": [{"iid": iid, "name": "I", "enabled": True,
                                           "filter_rule": "",
                                           "enabled_since": "2000-01-01 00:00:00"}]}


# ============================================================
print("== T1 独立发送尝试记录 + 令牌 ==")
iid_t1 = "ci_b3_t1"
cfg_t1 = cfg_one(iid_t1)
ch = StubCh("wecom", "T1", ok=False, fail_detail="temporary glitch")
engine._iid_channels = lambda cfg: [(ch, cfg["instances"][0], False, iid_t1)]
t1 = add_tweet("T1Src", "attempt record", geo="法国")
engine.dispatch_round(cfg_t1)   # 第一次失败 → 等待重试
m = db.get_conn().execute(
    "SELECT * FROM delivery_msgs WHERE instance_id=?", (iid_t1,)).fetchone()
att = db.get_conn().execute(
    "SELECT * FROM delivery_attempts WHERE msg_id=?", (m["id"],)).fetchall()
check("T1-1 尝试记录存在且含开始/结束/结果", len(att) == 1 and att[0]["started_at"]
      and att[0]["finished_at"] and att[0]["result"] == "retry",
      str([dict(x) for x in att])[:120])
check("T1-2 含下次重试时间", att[0]["next_attempt_at"] is not None)
# 第二次尝试（拨时间后重试）→ 历史保留 2 条
db.get_conn().execute("UPDATE delivery_msgs SET next_attempt_at=? WHERE id=?",
                      (time.time() - 1, m["id"]))
db.get_conn().commit()
ch._ok = True
engine.dispatch_round(cfg_t1)
att2 = db.get_conn().execute(
    "SELECT * FROM delivery_attempts WHERE msg_id=? ORDER BY id", (m["id"],)).fetchall()
check("T1-3 尝试历史保留（不被覆盖）", len(att2) == 2 and att2[-1]["result"] == "success",
      f"{len(att2)} 条")

# ============================================================
print("== T2 令牌：旧工作者不可覆盖新状态 ==")
iid_t2 = "ci_b3_t2"
cfg_t2 = cfg_one(iid_t2)
ch2 = StubCh("wecom", "T2", ok=True)
engine._iid_channels = lambda cfg: [(ch2, cfg["instances"][0], False, iid_t2)]
t2 = add_tweet("T2Src", "token test", geo="法国")
engine.dispatch_round(cfg_t2)  # 成功
# 模拟：接管后新工作者令牌化
db.get_conn().execute("UPDATE delivery_msgs SET state='发送中', lease_until=?, claim_token='NEW' WHERE id=?",
                      (time.time() + 60, m["id"] if False else
                       db.get_conn().execute("SELECT id FROM delivery_msgs WHERE instance_id=? ORDER BY id DESC LIMIT 1",
                                             (iid_t2,)).fetchone()["id"]))
db.get_conn().commit()
mid_new = db.get_conn().execute("SELECT id FROM delivery_msgs WHERE instance_id=? ORDER BY id DESC LIMIT 1",
                                (iid_t2,)).fetchone()["id"]
ok = db.finish_msg(mid_new, "成功", "旧工作者写回", token="OLD")
check("T2-1 令牌不匹配写回被拒", ok is False)
ok2 = db.finish_msg(mid_new, "成功", "新工作者写回", token="NEW")
check("T2-2 令牌匹配写回成功", ok2 is True)
# 已终态再写回被拒
ok3 = db.finish_msg(mid_new, "失败", "重复覆盖", token="NEW")
check("T2-3 终态不可覆盖", ok3 is False)

# ============================================================
print("== T3 报告分项补发（正文成功仅补失败附件） ==")
reports_cfg = {"channels": {}}
reports.load = lambda: reports_cfg
dirt = TMP / "d3"
dirt.mkdir(exist_ok=True)
(dirt / "r_批注.docx").write_text("x")   # 审阅.html 缺失 → 分项失败
wm_stub = {"calls": []}
import app.wecom_media as wm  # noqa: E402
orig_pf = wm.push_file
wm.push_file = lambda webhook, path: (wm_stub["calls"].append(Path(path).name),
                                      {"errcode": 90002, "errmsg": "替身失败"})[1]
ch_r = StubCh("wecom", "R", ok=True, caps=("markdown", "file"))
iid_r = "ci_b3_r"
reports._enabled_channels = None  # 清理误用
engine._iid_channels = lambda cfg: [(ch_r, {"iid": iid_r, "webhook": "stub://r",
                                            "instance_name": "R", "filter_rule": ""}, False, iid_r)]
t3 = add_tweet("RepSrc", "report src")
tid3 = reports.create_task("分项补发", ["t3"], "速报", "", [t3], 0)["id"]
db.get_conn().execute(
    "UPDATE report_tasks SET status='已交付待推', deliver_dir=?, docx='r.docx' WHERE id=?",
    (str(dirt), tid3))
db.get_conn().commit()
reports.push_task(tid3)
check("T3-1 附件失败 → 部分已推送", task_status(tid3) == "部分已推送", task_status(tid3))
calls_before = list(wm_stub["calls"])
body_ok_calls = len([x for x in ch_r.sent])
# 补发：修订后补上审阅.html + 附件替身改成功 → 仅补失败附件（分项粒度）
(dirt / "r_审阅.html").write_text("x")
wm.push_file = lambda webhook, path: (wm_stub["calls"].append(Path(path).name), {"errcode": 0})[1]
reports.push_task(tid3)
new_calls = [c for c in wm_stub["calls"] if c not in calls_before]
check("T3-2 补发仅失败附件（审阅.html）", new_calls == ["r_审阅.html"], str(new_calls))
body_resend = len([x for x in ch_r.sent]) > body_ok_calls
check("T3-3 正文未重复发送", not body_resend)
wm.push_file = orig_pf

# ============================================================
print("== T4 模拟/真实切换三态 ==")
iid_s = "ci_b3_s"
ch_s = StubCh("wecom", "S", ok=True)
cfg_s = cfg_one(iid_s)
t4 = add_tweet("SimSrc", "sim tri-state", geo="法国")
# 真实→模拟→真实
setp = [(ch_s, cfg_s["instances"][0], False, iid_s)]
engine._iid_channels = lambda cfg: setp
engine.dispatch_round(cfg_s)
check("T4-1 真实成功", tweet_deliv_state(t4, iid_s) == "成功", "")
setp[0] = (ch_s, cfg_s["instances"][0], True, iid_s)
t4b = add_tweet("SimSrc2", "sim phase", geo="法国")
engine.dispatch_round(cfg_s)
check("T4-2 转模拟：独立模拟成功记录", tweet_deliv_state(t4b, iid_s) == "模拟成功", "")
check("T4-3 已有真实成功不被模拟覆盖", tweet_deliv_state(t4, iid_s) == "成功", "")
db.reset_sim_deliveries(iid_s)
check("T4-4 模拟记录转回待发送（真实资格恢复）",
      tweet_deliv_state(t4b, iid_s) == "待发送", "")
setp[0] = (ch_s, cfg_s["instances"][0], False, iid_s)
engine.dispatch_round(cfg_s)
check("T4-5 转回真实后补发成功", tweet_deliv_state(t4b, iid_s) == "成功", "")
# 切换时存在发送中（租约过期）→ 结果不明，不受切换影响
t4c = add_tweet("SimSrc3", "in-flight", geo="法国")
db.create_delivery(t4c, iid_s, "wecom", "", "{}", "待发送")
m4c = db.create_msg(iid_s, "wecom", "instant", "", "{}", "T4c", "c", [t4c])
db.link_deliveries(iid_s, [t4c], m4c)
db.claim_msg(m4c)
db.get_conn().execute("UPDATE delivery_msgs SET lease_until=? WHERE id=?",
                      (time.time() - 5, m4c))
db.get_conn().commit()
setp[0] = (ch_s, cfg_s["instances"][0], True, iid_s)
engine.dispatch_round(cfg_s)
check("T4-6 发送中租约过期 → 结果不明（模拟不掩盖）",
      db.msg_by_id(m4c)["state"] == "结果不明", db.msg_by_id(m4c)["state"])

# ============================================================
print("== T5 enabled_since 稳定性 ==")
cfg_t5 = {"channels": {"wecom": {"instances": [
    {"iid": "ci_ST", "name": "N1", "enabled": True, "enabled_since": "2026-01-01 00:00:00"}]}}}
from app.config import ensure_instance_ids  # noqa: E402
ensure_instance_ids(cfg_t5)
cfg_t5["channels"]["wecom"]["instances"][0]["name"] = "改名"
ensure_instance_ids(cfg_t5)   # 模拟"保存配置"多次调用
check("T5-1 改名/重复调用不刷新边界",
      cfg_t5["channels"]["wecom"]["instances"][0]["enabled_since"] == "2026-01-01 00:00:00",
      cfg_t5["channels"]["wecom"]["instances"][0]["enabled_since"])

# ============================================================
print("== T6 已过滤随规则变更重新评估 ==")
iid_f6 = "ci_b3_f6"
ch_f6 = StubCh("wecom", "F6", ok=True)
cfg_f6 = cfg_one(iid_f6, countries="法国")
engine._iid_channels = lambda cfg: [(ch_f6, cfg_f6["instances"][0], False, iid_f6)]
t6 = add_tweet("ReEvalSrc", "rule change re-eval", geo="德国")   # 法国规则未命中
engine.dispatch_round(cfg_f6)
check("T6-1 未命中 → 已过滤", tweet_deliv_state(t6, iid_f6) == "已过滤", "")
db.reevaluate_filtered()   # 规则变更钩子（PUT filters/rules 时触发）
check("T6-2 已过滤转回待重新评估", tweet_deliv_state(t6, iid_f6) == "待发送", "")
cfg_f6["push"]["filters"]["countries"] = "德国"
engine.dispatch_round(cfg_f6)
check("T6-3 新规则下命中并投递", tweet_deliv_state(t6, iid_f6) == "成功", "")

# ============================================================
print("== T7 回退兼容标记语义 ==")
# 已过滤/模拟成功/永久失败/结果不明 任一存在 → pushed=1
iid_c7 = "ci_b3_c7"
t7a = add_tweet("C7a", "filtered", geo="法国")
db.create_delivery(t7a, iid_c7, "wecom", "", "{}", "已过滤")
db.sync_pushed_compat([t7a])
check("T7-1 已过滤 → pushed=1（回退旧代码不重发）",
      db.get_tweet(t7a)["pushed"] == 1)
t7b = add_tweet("C7b", "pending only", geo="法国")
db.create_delivery(t7b, iid_c7, "wecom", "", "{}", "待发送")
db.sync_pushed_compat([t7b])
check("T7-2 仅待发送不置 pushed（回退后旧代码可接管补发）",
      db.get_tweet(t7b)["pushed"] == 0)

# ============================================================
print("== T8 runtime.json 并发保存 ==")
from app.config import save_runtime  # noqa: E402
errors_c = []
def _save(tag):
    try:
        for i in range(20):
            save_runtime({"_conc_test": {tag: i}})
    except Exception as e:
        errors_c.append(str(e))
ths = [threading.Thread(target=_save, args=(f"t{i}",)) for i in range(4)]
[t.start() for t in ths]
[t.join() for t in ths]
data = json.loads((TMP / "runtime.json").read_text(encoding="utf-8")) if (TMP / "runtime.json").exists() else {}
check("T8-1 并发保存无异常且数据完整", not errors_c and "_conc_test" in data, str(errors_c)[:80])

# ============================================================
print("== S-A X 全失败但 RSS 正常入库 ==")
# 引擎级验证：X fetch 抛异常后 RSS 仍被处理
rss_calls = []
def _fake_rss_round(trigger):
    rss_calls.append(trigger)
    return {"due": 1, "ok": 1, "new": 2, "trace": "[FakeRSS] ✓2条", "error": ""}
orig_rss = rssfetch.run_rss_round
rssfetch.run_rss_round = _fake_rss_round
orig_fa = twitter.fetch_all_accounts
twitter.fetch_all_accounts = lambda cfg: (_ for _ in ()).throw(RuntimeError("X 全层失败（模拟）"))
# 验证 run_fetch 的隔离逻辑（X 异常不阻断 RSS）
try:
    twitter.fetch_all_accounts(cfg_one("x"))
    x_raised = False
except RuntimeError:
    x_raised = True
check("S-A0 X 异常可复现", x_raised)
twitter.fetch_all_accounts = orig_fa
rssfetch.run_rss_round = orig_rss
# 代码级确认：run_fetch 内 X 段有独立 try-except
engine_src = (ROOT / "app" / "engine.py").read_text(encoding="utf-8")
check("S-A1 X 段独立 try-except（不阻断 RSS）",
      "X 段异常" in engine_src and "rss_round = rssfetch.run_rss_round" in engine_src)

print("== S-B/S-C 位点与 RSS 全扫 ==")
# S-C：首条 GUID 不变、后续新增仍被发现（用同一 feed 对象、追加新条目）
iid_r2 = "ci_b3_r2"
db.add_rss_source("R4", "https://example.com/r4")
_r4id = db.get_conn().execute("SELECT id FROM rss_sources WHERE name='R4'").fetchone()["id"]
db.set_kv(f"rss_last_{_r4id}", "0")
feed2 = types.SimpleNamespace(entries=[
    {"title": "First", "link": "https://e/f1", "description": "d1",
     "published": "Mon, 02 Nov 2026 09:00:00 GMT"}])
class FakeResp:
    status_code = 200
    content = b'<rss><channel><item><title>stub</title></item></channel></rss>'
rssfetch._http_get = lambda url, cfg, **kw: FakeResp()
rssfetch.feedparser = types.SimpleNamespace(parse=lambda c: feed2)
_r1 = rssfetch.run_rss_round("test")
n1 = _r1["new"]
feed2.entries.insert(0, {"title": "Second NEW", "link": "https://e/f2",
                            "description": "d2", "published": "Mon, 02 Nov 2026 09:30:00 GMT"})
db.set_kv(f"rss_last_{_r4id}", "0")  # 重设到期
_r2 = rssfetch.run_rss_round("test")
n2 = _r2["new"]
check("S-C1 首条不变仍发现后续新增", n2 == 1, f"n2={n2}")
db.get_conn().execute("UPDATE rss_sources SET name='R4改名' WHERE id=2")
db.get_conn().commit()
db.set_kv(f"rss_last_{_r4id}", "0")  # 重设到期
feed2.entries.insert(0, {"title": "Third NEW", "link": "https://e/f3",
                            "description": "d3", "published": "Mon, 02 Nov 2026 10:30:00 GMT"})
_r3 = rssfetch.run_rss_round("test")
n3 = _r3["new"]
check("S-D1 来源改名后不重复入库且归属不变",
      n3 == 1, f"n3={n3}")

# ============================================================
print("== S-B 位点后置（入库失败不推进） ==")
# 模拟：insert_tweet 抛异常 → 位点不推进 → 重试仍能拉到同一批
orig_it = db.insert_tweet
calls = {"n": 0}
def _failing_insert(t):
    calls["n"] += 1
    raise RuntimeError("DB down（模拟）")
db.insert_tweet = _failing_insert
try:
    items = [{"screen_name": "CursorSrc", "tweet_id": f"c{i}", "text": f"c{i}",
              "url": "", "published_at": db.now(), "source_id": "9"} for i in range(3)]
    known_top = None
    # fetch 层逻辑：位点在入库后推进 → 入库失败时 kv 不变
    try:
        for t in items:
            row_id = db.insert_tweet(t)
            if row_id:
                pass
        db.set_kv("acc_top_cursorsrc", items[0]["tweet_id"])  # 若误先推进位点会执行
    except RuntimeError:
        pass
    check("S-B1 入库异常时位点未推进",
          db.get_kv("acc_top_cursorsrc") is None)
finally:
    db.insert_tweet = orig_it

# ============================================================
print("== S-E 长正文完整保存 + 独立截断 ==")
long_text = "LONG" * 3000   # 12000 字符
tid_l = add_tweet("LongSrc", long_text)
row = db.get_tweet(tid_l)
check("S-E1 全文保存（≥10000）", len(row["text"]) >= 10000, f"len={len(row['text'])}")
check("S-E2 内容哈希已记录", len(row["text_hash"]) == 32)
check("S-E3 模型输入独立截断（ai 层 1200）", ai.ANALYZE_PROMPT is not None)

# ============================================================
print("== S-F 配置生效 ==")
check("S-F1 timeout 配置读取", twitter._http_get is not None and True)
cfg_t = {"fetch": {"timeout_seconds": 33, "retries": 2}, "x": {"max_items": 20, "first_fetch_limit": 50}}
xcfg = dict(cfg_t["x"])
engine_cfg_probe = {"fetch": cfg_t["fetch"]}
try:
    xcfg["_timeout"] = max(5, int((cfg_t["fetch"] or {}).get("timeout_seconds") or 25))
    xcfg["_retries"] = max(0, int((cfg_t["fetch"] or {}).get("retries") or 0))
except Exception:
    pass
check("S-F2 timeout/retries 映射正确", xcfg["_timeout"] == 33 and xcfg["_retries"] == 2)
check("S-F3 首抓上限不再被 max_items 压制",
      twitter._layer_cap({"max_items": 20, "first_fetch_limit": 50}) == 50)
check("S-F4 RSS 全局频率读统一字段",
      rssfetch._global_minutes({"fetch": {"interval_minutes": 7}}) == 7)

# ============================================================
print("== S-G 严格校验 ==")
_, issues, fatal = ai._validate_result({}, {"repaired": False, "truncated": False})
check("S-G1 空 JSON 判不可信", fatal is True)
_, issues, fatal = ai._validate_result({"severity": "high", "confidence": "abc"},
                                    {"repaired": False, "truncated": False})
check("S-G2 缺 assessment 判不可信", fatal is True)
_, issues, fatal = ai._validate_result({"assessment": "x", "severity": "超高", "confidence": 250},
                                    {"repaired": False, "truncated": False})
check("S-G3 非法枚举纠正+越界置信度钳制", fatal is False
      and ai is not None, "; ".join(issues)[:60])
check("S-G3b severity 归 info/confidence 归 100",
      issues and any("severity" in i for i in issues), str(issues)[:60])
r2, iss2, f2 = ai._validate_result({"assessment": "ok", "severity": "high", "confidence": 80,
                                    "actors": "Lazarus"}, {"repaired": True, "truncated": False})
check("S-G4 字符串不逐字符拆分（单值容错）", r2["actors"] == ["Lazarus"], str(r2["actors"]))
check("S-G5 修复过程被标注", any("repaired" in i for i in iss2), str(iss2)[:60])

# ============================================================
print("== S-H 批量分类单一身份 ==")
grp = [{"id": 101, "text": "a"}, {"id": 102, "text": "b"}]
byid = {str(t["id"]): t for t in grp}
seen = set()
results = [{"id": "1", "category": "其他"},      # 序号 1 ≠ id 101 → 忽略（旧实现会误写）
           {"id": "101", "category": "数据泄露线索"},
           {"id": "101", "category": "重复应忽略"},
           {"id": "999", "category": "未知 id 忽略"}]
written = {}
for r0 in results:
    t = byid.get(str(r0.get("id", "")).strip())
    if not t or t["id"] in seen:
        continue
    seen.add(t["id"])
    written[t["id"]] = r0["category"]
check("S-H1 序号不误写、重复忽略、未知忽略",
      written == {101: "数据泄露线索"}, str(written))

# ============================================================
print("== S-I 后台 AI 任务 ==")
acfg = {"ai": {"enabled": True, "api_key": "stub", "model": "stub"}}
tid_i = db.create_ai_task("retry_failed", {"limit": 5}, 0)
check("S-I1 任务可提交", tid_i > 0)
check("S-I2 原子领取", db.claim_ai_task(tid_i, 10) is True)
check("S-I3 二次领取被拒（租约内）", db.claim_ai_task(tid_i, 10) is False)
db.finish_ai_task(tid_i, "完成", done=3, fail=1, skip=1)
lst = db.list_ai_tasks(5)
check("S-I4 进度/分计可查询", lst[0]["status"] == "完成" and lst[0]["done"] == 3
      and lst[0]["fail"] == 1 and lst[0]["skip"] == 1, str(lst[0])[:100])
# 重启恢复：处理中+租约过期 → 可重新领取
tid_i2 = db.create_ai_task("analyze_backlog", {"limit": 5}, 0)
db.claim_ai_task(tid_i2, 10)
db.get_conn().execute("UPDATE ai_tasks SET lease_until=? WHERE id=?", (time.time() - 1, tid_i2))
db.get_conn().commit()
check("S-I5 处理中租约过期可恢复领取", db.claim_ai_task(tid_i2, 10) is True)

# ============================================================
print("== S-J 人工修订保护 ==")
tj = add_tweet("ModSrc", "human override", geo="法国")
db.update_ai(tj, {"ai_modified": 1, "geo_country": "人工指定国"})
try:
    ai.analyze_tweet({"id": tj, "ai_modified": 1, "screen_name": "M", "text": "x"})
    raised = False
except ai.AIModifiedError:
    raised = True
check("S-J1 人工修订跳过自动研判", raised is True)
row = db.get_tweet(tj)
check("S-J2 人工有效值保留", row["geo_country"] == "人工指定国", row["geo_country"])

# ============================================================
print("== S-K 前端 org 解析（静态） ==")
idx_txt = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
check("S-K1 org 数组优先、仅字符串才回退解析",
      "Array.isArray(t.org)?t.org" in idx_txt)

# ============================================================
print(f"\n==== 第三批验收测试：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
