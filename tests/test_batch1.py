# -*- coding: utf-8 -*-
"""第一批回归测试（第二批后适配版）。

A1/A2/A4/D2 已升级为 tests/test_batch2.py 的逐实例投递语义（本文件不再重复）。
保留仍独立有效的断言：C1 AI 重试集合 / E1 收藏保护 / F1 来源统计键 / B6 RSS 频率字段 / C9 IOC 建议。

运行：C:/Python314/python.exe tests/test_batch1.py
"""
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

TMP = Path(tempfile.mkdtemp(prefix="xintel-batch1-"))
import app.db as db  # noqa: E402

db.DATA_DIR = TMP
db._conn = None
db.init_db()

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


def add_tweet(sn, text, severity="medium", geo="", category="数据泄露线索", starred=0):
    tid = db.insert_tweet({"screen_name": sn, "tweet_id": f"t{time.time_ns()}",
                           "text": text, "url": "https://x/x", "published_at": db.now()})
    db.update_ai(tid, {"ai_status": "ok", "ai_severity": severity,
                       "geo_country": geo, "category": category})
    if starred:
        db.set_tweet_flags(tid, starred=True)
    return tid


print("== C1 AI 首败进入可重试集合 ==")
err_id = add_tweet("ErrSrc", "ai failed once")
db.update_ai(err_id, {"ai_status": "error", "ai_retries": 1})
pend = db.tweets_pending_ai(50)
check("C1-1 error且retries<2 可重试", any(t["id"] == err_id for t in pend),
      f"待AI {len(pend)} 条")

print("== E1 批量删除保护收藏 ==")
s_id = add_tweet("StarSrc", "starred tweet", starred=True)
n_id = add_tweet("NormSrc", "normal tweet")
affected = db.batch_tweet_op([s_id, n_id], "delete")
check("E1-1 收藏不被批量删除", db.get_tweet(s_id) is not None, f"affected={affected}")
check("E1-2 非收藏正常删除", db.get_tweet(n_id) is None)

print("== F1 来源统计键 ==")
db.add_x_account("DailyDarkWeb")
db.add_rss_source("TheHackersNews", "https://example.com/rss")
add_tweet("DailyDarkWeb", "x source tweet")
add_tweet("RSS:TheHackersNews", "rss source tweet")
db.add_fetch_log("schedule", 1, 1, 2, 0, "",
                 "@DailyDarkWeb: nitter✓20条/1.0s ｜ [TheHackersNews] ✗5条/10扫描")
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

print("== B6 RSS 跟随全局读统一字段 ==")
import app.rssfetch as rssfetch  # noqa: E402
check("B6-1 读取 fetch.interval_minutes", rssfetch._global_minutes({"fetch": {"interval_minutes": 5}}) == 5)
check("B6-2 无 fetch 字段回退 15", rssfetch._global_minutes({"x": {"interval_minutes": 15}}) == 15)
check("B6-3 空配置回退 15", rssfetch._global_minutes({}) == 15)

print("== C9 IOC 导出建议（静态） ==")
help_txt = (ROOT / "web" / "help.html").read_text(encoding="utf-8")
check("C9-1 导出模板不含'优先封禁'建议", "建议优先封禁" not in help_txt)
check("C9-2 导出模板区分 CVE 与封禁指标", "不与封禁指标混用" in help_txt or "独立列出" in help_txt)

print(f"\n==== 第一批回归测试：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
