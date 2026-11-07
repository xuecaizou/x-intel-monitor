# -*- coding: utf-8 -*-
"""第一批复核测试（临时数据库 + 可控通道替身，无真实外发、无真实 AI）。

覆盖（对应审查清单）：
  A1 实例规则替代语义（默认规则预过滤不得阻断实例规则）
  A2 摘要分批仅确认实际发送成员（超上限的中低危不丢失、不误标）
  A4 待推送不因先取 200 条而饥饿
  C1 AI 首败进入可重试集合（error+retries<2）
  E1 批量删除保护收藏
  F1 来源统计键（X 无 @ 前缀、RSS 带 RSS: 前缀）
  D2 报告推送：全失败/仅模拟不推进真实完成；真实成功才推进
  C9 IOC 导出建议不含"优先封禁"（静态断言）

运行：C:/Python314/python.exe tests/test_batch1.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

# ---------- 隔离环境：临时数据目录 ----------
TMP = Path(tempfile.mkdtemp(prefix="xintel-batch1-"))
import app.db as db  # noqa: E402

db.DATA_DIR = TMP          # 测试隔离（db 模块内引用该名）
db._conn = None
db.init_db()

import app.engine as engine  # noqa: E402
import app.reports as reports  # noqa: E402
from api import routes  # noqa: E402

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(name)
        print(f"FAIL {name} {detail}")


# ---------- 通道替身 ----------
class StubCh:
    def __init__(self, key, display, ok=True, caps=("markdown",)):
        self.key, self.display, self.caps = key, display, caps
        self._ok = ok
        self.sent = []

    def configured(self, cc):
        return True

    def send(self, cc, title, markdown, image=None, html=None, url=None, **_):
        self.sent.append({"title": title, "md": markdown})
        return self._ok, "stub ok"


def add_tweet(sn, text, severity="medium", geo="", category="数据泄露线索", starred=0):
    tid = db.insert_tweet({"screen_name": sn, "tweet_id": f"t{time.time_ns()}",
                           "text": text, "url": "https://x/x", "published_at": db.now()})
    db.update_ai(tid, {"ai_status": "ok", "ai_severity": severity,
                       "geo_country": geo, "category": category})
    if starred:
        db.set_tweet_flags(tid, starred=True)
    return tid


def pushed_count():
    return db.get_conn().execute("SELECT COUNT(*) n FROM tweets WHERE pushed=1").fetchone()["n"]


# ============================================================
print("== A1 实例规则替代语义 ==")
jp_id = add_tweet("SecTrackerJP", "JP incident 1", severity="high", geo="日本")
fr_id = add_tweet("SecTrackerFR", "FR incident 1", severity="high", geo="法国")
CFG_A1 = {"push": {"digest_max_items": 8,
                   "filters": {"min_severity": "", "mode": "and", "countries": "法国",
                               "categories": "", "industries": "", "tags": "", "keywords": ""},
                   "rules": [{"id": "rJP", "name": "日本组", "mode": "and", "countries": "日本",
                              "categories": "", "industries": "", "tags": "", "keywords": ""}]},
          "channels": {}}
stub_jp = StubCh("wecom", "企微·日本组")
engine._enabled_channels = lambda cfg: [(stub_jp, {"instance_name": "日本组", "filter_rule": "rJP"}, False)]
# 期望：默认规则(法国)不得阻断实例规则(日本)——日本情报应送达实例
cands = engine._collect_pending(CFG_A1, True)
check("A1-1 候选不被默认规则预过滤", any(t["id"] == jp_id for t in cands),
      f"候选 {len(cands)} 条")
n = engine.dispatch(cands, CFG_A1)
check("A1-2 日本情报送达日本实例", any("JP incident 1" in x["md"] for x in stub_jp.sent),
      f"发送 {len(stub_jp.sent)} 条")
check("A1-3 法国情报不送日本实例", not any("FR incident" in x["md"] for x in stub_jp.sent))

# ============================================================
print("== A2 摘要分批仅确认实际发送成员 ==")
fr_ids = [add_tweet(f"FR{i}", f"FR medium {i}", severity="medium", geo="法国") for i in range(10)]
CFG_A2 = {"push": {"digest_max_items": 8,
                   "filters": {"min_severity": "", "mode": "and", "countries": "法国",
                               "categories": "", "industries": "", "tags": "", "keywords": ""},
                   "rules": []},
          "channels": {}}
stub_fr = StubCh("dingtalk", "钉钉·法国")
engine._enabled_channels = lambda cfg: [(stub_fr, {"instance_name": "法国", "filter_rule": ""}, False)]
cands = [t for t in engine._collect_pending(CFG_A2, True) if t["id"] in set(fr_ids)]
n = engine.dispatch(cands, CFG_A2)
digests = [x for x in stub_fr.sent if x["title"] == "X-Intel 推文摘要"]
sent_members = sum(x["md"].count("FR medium") for x in digests)
marked = [db.get_tweet(i)["pushed"] for i in fr_ids]
check("A2-1 摘要仅发送上限 8 条", sent_members == 8, f"实际 {sent_members}")
check("A2-2 被截断的 2 条不误标 pushed", sum(marked) == 8, f"marked={sum(marked)}")
check("A2-3 未发送成员保留待推送资格",
      all(db.get_tweet(i)["pushed"] == 0 for i in fr_ids if not db.get_tweet(i)["pushed"]) and
      sum(1 for i in fr_ids if db.get_tweet(i)["pushed"] == 0) == 2,
      "剩余 " + str(sum(1 for i in fr_ids if db.get_tweet(i)["pushed"] == 0)))

# ============================================================
print("== A4 待推送不因预截断饥饿 ==")
many_ids = [add_tweet(f"FR{i}", f"FR bulk {i}", severity="low", geo="法国") for i in range(300)]
cands = engine._collect_pending(CFG_A2, True)
check("A4-1 全量候选不被 200 截断", len(cands) >= 300, f"候选 {len(cands)}")

# ============================================================
print("== C1 AI 首败进入可重试集合 ==")
err_id = add_tweet("ErrSrc", "ai failed once")
db.update_ai(err_id, {"ai_status": "error", "ai_retries": 1})
pend = db.tweets_pending_ai(50)
check("C1-1 error且retries<2 可重试", any(t["id"] == err_id for t in pend),
      f"待AI {len(pend)} 条")

# ============================================================
print("== E1 批量删除保护收藏 ==")
s_id = add_tweet("StarSrc", "starred tweet", starred=True)
n_id = add_tweet("NormSrc", "normal tweet")
affected = db.batch_tweet_op([s_id, n_id], "delete")
check("E1-1 收藏不被批量删除", db.get_tweet(s_id) is not None, f"affected={affected}")
check("E1-2 非收藏正常删除", db.get_tweet(n_id) is None)

# ============================================================
print("== F1 来源统计键 ==")
db.add_x_account("DailyDarkWeb")
db.add_rss_source("TheHackersNews", "https://example.com/rss")
add_tweet("DailyDarkWeb", "x source tweet")
add_tweet("RSS:TheHackersNews", "rss source tweet")
db.add_fetch_log("schedule", 1, 1, 2, 0, "",
                 "@DailyDarkWeb: nitter✓20条/1.0s ｜ [TheHackersNews] ✓5条/10扫描")
db.set_kv("acc_last_dailydarkweb", str(time.time()))
db.set_kv("rss_last_1", str(time.time()))
accounts = db.list_x_accounts()
src = routes._sources_health(accounts)
x0 = src["x"]["items"][0]
r0 = src["rss"]["items"][0]
check("F1-1 X 源 24h 计数（无@前缀）", x0["cnt24"] == 1, f"cnt24={x0['cnt24']}")
check("F1-2 RSS 源 24h 计数（含RSS:前缀）", r0["cnt24"] == 1, f"cnt24={r0['cnt24']}")
check("F1-3 总来源数含 X 与 RSS", src["x"]["total"] + src["rss"]["total"] >= 2,
      f"x={src['x']['total']} rss={src['rss']['total']}")

# ============================================================
print("== D2 报告推送结果语义 ==")
reports.load = lambda: {"channels": {}}   # 隔离：不读生产 runtime


def make_ready_task(title):
    r = reports.create_task(title, ["audit"], "速报", "", [], 0)
    tid = r["id"]
    db.get_conn().execute("UPDATE report_tasks SET status='已交付待推' WHERE id=?", (tid,))
    db.get_conn().commit()
    return tid


def task_status(tid):
    return db.get_conn().execute("SELECT status FROM report_tasks WHERE id=?", (tid,)).fetchone()["status"]


# D2-a 全部失败
ch_fail = StubCh("wecom", "企微·全败", ok=False)
engine._enabled_channels = lambda cfg: [(ch_fail, {"instance_name": "X", "filter_rule": ""}, False)]
tid_a = make_ready_task("D2 全部失败")
r = reports.push_task(tid_a)
check("D2-a1 全失败不推进已推送", task_status(tid_a) == "已交付待推",
      f"status={task_status(tid_a)}")
check("D2-a2 全失败响应带错误且不再自称已推送", r.get("error") and r.get("status") != "已推送",
      str(r)[:80])
# D2-b 仅模拟
ch_sim = StubCh("dingtalk", "钉钉·模拟", ok=True)
engine._enabled_channels = lambda cfg: [(ch_sim, {"instance_name": "D", "filter_rule": ""}, True)]
tid_b = make_ready_task("D2 仅模拟")
r = reports.push_task(tid_b)
check("D2-b1 仅模拟不推进真实完成", task_status(tid_b) == "已交付待推",
      f"status={task_status(tid_b)}")
check("D2-b2 仅模拟响应明确标注", r.get("simulated_only") is True, str(r)[:80])
# D2-c 真实成功
ch_ok = StubCh("wecom", "企微·真实", ok=True)
engine._enabled_channels = lambda cfg: [(ch_ok, {"instance_name": "W", "filter_rule": ""}, False)]
tid_c = make_ready_task("D2 真实成功")
r = reports.push_task(tid_c)
check("D2-c1 真实成功推进已推送", task_status(tid_c) == "已推送" and r.get("pushed") is True,
      f"status={task_status(tid_c)}")

# ============================================================
print("== C9 IOC 导出建议（静态） ==")
help_txt = (ROOT / "web" / "help.html").read_text(encoding="utf-8")
check("C9-1 导出模板不含'优先封禁'建议", "建议优先封禁" not in help_txt)
check("C9-2 导出模板区分 CVE 与封禁指标", "不与封禁指标混用" in help_txt or "独立列出" in help_txt)

# ============================================================
print(f"\n==== 第一批复核测试：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
