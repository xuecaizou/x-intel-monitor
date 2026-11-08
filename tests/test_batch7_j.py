# -*- coding: utf-8 -*-
"""J 组：首页统计和来源健康（2026-11-09 集中修复）。

历史缺陷（基线 b4c720d 复核，线上真实场景）：
  3 X + 1 RSS 全部启用、RSS 抓取异常 → 首页显示 3/3（sources.x.total），
  配置页也显示 3 个；推送全部为模拟，真实成功率仍显示 100%。

统一口径（任务书）：
  来源总数 = X+RSS；启用数 = 两类启用之和；启用但异常仍计入启用；
  总数/启用/健康独立；停用/异常/未运行/未知分别显示；
  一轮 X 成功不能覆盖 RSS 已知失败；仅模拟时真实成功率="无真实样本"；
  请求失败不得显示假零或默认正常（启用但无抓取记录=unknown）。

运行：python tests/test_batch7_j.py
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

import api.routes as routes  # noqa: E402

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(f"{name} [{detail}]")
        print(f"FAIL {name} [{detail}]")


def add_x(name, enabled=True):
    db.add_x_account(name)
    if not enabled:
        db.get_conn().execute("UPDATE x_accounts SET enabled=0 WHERE screen_name=?", (name,))
        db.get_conn().commit()


# ============================================================
print("== J1 固定场景：3 X + 1 RSS 全启用，RSS 抓取异常 ==")
add_x("alpha")
add_x("beta")
add_x("gamma")
db.add_rss_source("rssA", "https://stub.local/rssA")
sid = db.get_conn().execute("SELECT id FROM rss_sources WHERE name='rssA'").fetchone()["id"]
# 一轮抓取 trace：X 三账号全部成功、RSS 异常（入库 3 条 X 情报使 24h 计数非零）
for nm in ("alpha", "beta", "gamma"):
    db.insert_tweet({"screen_name": nm, "tweet_id": f"j-{nm}", "text": "情报",
                     "url": "u", "published_at": db.now()})
db.add_fetch_log("schedule", 3, 3, 3, 0, "",
                 "@alpha: syndication✓1条/0.1s｜@beta: syndication✓1条/0.1s｜"
                 "@gamma: syndication✓1条/0.1s｜RSS:[rssA] ✗HTTP 500")
stats = routes.api_stats()
src = stats["sources"]
check("J1-1 来源总数 = X+RSS = 4", src["total"] == 4, f"total={src['total']}")
check("J1-2 启用数 = 4（异常源仍计入启用）", src["enabled"] == 4, f"enabled={src['enabled']}")
check("J1-3 健康状态 = partial_error（X 成功不掩盖 RSS 失败）",
      src["health"]["state"] == "partial_error", str(src["health"]))
check("J1-4 RSS 侧 error 计数 = 1", src["health"]["rss"]["error"] == 1, str(src["health"]["rss"]))
rss_item = [i for i in src["rss"]["items"] if "rssA" in i["name"]][0]
check("J1-5 RSS 逐源 state=error", rss_item["state"] == "error", str(rss_item))

# ============================================================
print("== J2 未运行源显示 unknown（不假正常） ==")
add_x("never-run")  # 启用但从未出现在 trace
stats2 = routes.api_stats()
x_item = [i for i in stats2["sources"]["x"]["items"] if "never-run" in i["name"]][0]
check("J2-1 启用但无抓取记录 → state=unknown", x_item["state"] == "unknown", str(x_item))
check("J2-2 unknown 不计入正常", stats2["sources"]["health"]["x"]["unknown"] == 1,
      str(stats2["sources"]["health"]))

# ============================================================
print("== J3 停用源：disabled，不计健康、不计启用 ==")
add_x("paused", enabled=False)
stats3 = routes.api_stats()
src3 = stats3["sources"]
p_item = [i for i in src3["x"]["items"] if "paused" in i["name"]][0]
check("J3-1 停用源 state=disabled", p_item["state"] == "disabled", str(p_item))
check("J3-2 停用不计入启用数（6 源 5 启用：3正常+1未运行+1停用 X +1 RSS）",
      src3["total"] == 6 and src3["enabled"] == 5,
      f"total={src3['total']} enabled={src3['enabled']}")
check("J3-3 disabled 独立计数", src3["health"]["x"]["disabled"] == 1, str(src3["health"]["x"]))

# ============================================================
print("== J4 全部异常 → error；未运行 → unknown 聚合 ==")
add_x("bad1")
add_x("bad2")
db.add_fetch_log("schedule", 2, 0, 0, 0, "@bad1 全层失败｜@bad2 全层失败",
                 "@bad1: syndication✗fail(0.1s)｜@bad2: syndication✗fail(0.1s)")
# 临时停用其余启用源 → 启用源 = bad1/bad2/rssA（三者全部异常）→ 聚合必须 error
db.get_conn().execute("UPDATE x_accounts SET enabled=0 WHERE screen_name IN"
                      " ('alpha','beta','gamma','never-run')")
db.get_conn().commit()
stats4 = routes.api_stats()
h4 = stats4["sources"]["health"]
check("J4-1 全部启用源异常 → error（启用 3 全失败）",
      h4["state"] == "error" and stats4["sources"]["enabled"] == 3, str(h4))
db.get_conn().execute("UPDATE x_accounts SET enabled=1 WHERE screen_name IN"
                      " ('alpha','beta','gamma','never-run')")
db.get_conn().commit()
# 再造一个部分异常场景（1 异常 + 其余正常/未知）
db.add_fetch_log("schedule", 3, 2, 0, 0, "@bad1 全层失败",
                 "@alpha: syndication✓1条/0.1s｜@beta: syndication✓1条/0.1s｜@bad1: ✗fail")
stats4b = routes.api_stats()
check("J4-2 部分异常 → partial_error（RSS 失败依旧被保留）",
      stats4b["sources"]["health"]["state"] == "partial_error",
      str(stats4b["sources"]["health"]["state"]))

# ============================================================
print("== J5 推送统计：真实/模拟分列，模拟不冒充成功率 ==")
db.add_push_log("企微", "测试", "模拟成功", "模拟模式")
db.add_push_log("企微", "测试", "模拟成功", "模拟模式")
ps = db.push_stats_today()
check("J5-1 仅模拟 → real_total=0", ps["real_total"] == 0, str(ps))
check("J5-2 仅模拟 → real_rate=None（无真实样本，不是 100%）", ps["real_rate"] is None, str(ps))
check("J5-3 模拟单列", ps["sim"] == 2 and ps["total"] == 2, str(ps))
db.add_push_log("企微", "推送", "成功", "ok")
db.add_push_log("钉钉", "推送", "失败", "err")
ps2 = db.push_stats_today()
check("J5-4 混合：真实分母=2（不含模拟）", ps2["real_total"] == 2, str(ps2))
check("J5-5 真实成功率按真实分母（1/2=50%）", ps2["real_rate"] == 50.0, str(ps2))
check("J5-6 ok 兼容字段仍含模拟（旧口径不变）", ps2["ok"] == 3, str(ps2))

# ============================================================
print("== J6 API 与内部口径一致 + 窗口独立 ==")
from fastapi.testclient import TestClient  # noqa: E402
import app.auth as auth  # noqa: E402
from app.main import app  # noqa: E402

auth.init_auth_tables()
auth.create_admin("admin", "AdminPass123")
H = {"Authorization": f"Bearer {auth.create_token('j-admin', 'read')}"}
client = TestClient(app)
r6 = client.get("/api/stats", headers=H)
api_stats = r6.json()
check("J6-1 API sources 与内部一致",
      api_stats["sources"]["total"] == routes.api_stats()["sources"]["total"]
      and api_stats["sources"]["health"]["state"] == "partial_error",
      str(api_stats["sources"]["health"]))
check("J6-2 API 含真实成功率字段",
      "push_real_rate" in api_stats and api_stats["push_real_rate"] == 50.0,
      str(api_stats.get("push_real_rate")))
check("J6-3 '今日'与'近24h'两个窗口独立存在（today 与 cnt24 不强行相等）",
      "today" in api_stats and "cnt24" in api_stats["sources"]["x"]["items"][0], "")
check("J6-4 前端契约字段齐备（total/enabled/health/items.state）",
      all(k in api_stats["sources"] for k in ("total", "enabled", "health"))
      and all("state" in i for i in api_stats["sources"]["x"]["items"]), "")

print(f"\n==== J 组首页统计和来源健康：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
