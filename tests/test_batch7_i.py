# -*- coding: utf-8 -*-
"""I 组：RSS 漏采（2026-11-09 集中修复）。

历史缺陷（基线 b4c720d 复核，台账 B33 声称已修但代码未生效）：
  fetch_source 仍按"首条 GUID 未变 → 整源跳过"短路——首位是置顶条目、
  或新条目插入非首位时 → 漏采。原测试把新条目插到首位（首条变化），
  不能证明该场景。

必须验证（任务书要求）：
  - 第一条保持不变，只在第二条新增（旧实现必失败的固定反例）；
  - 入库失败不推进位点、重跑补齐且不重复；
  - 重跑整轮幂等；改名不重复入库且归属不变（真实夹具，非空洞 WHERE id=2）。

运行：python tests/test_batch7_i.py
"""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from tests.testenv import TestEnv

env = TestEnv(forbid_network=False).start()
import app.db as db  # noqa: E402

db.DATA_DIR = env.tmp
db._conn = None
db.init_db()

import app.rssfetch as rssfetch  # noqa: E402
import app.safefetch as sf  # noqa: E402
import app.config as cfgmod  # noqa: E402
import requests as requests_mod  # noqa: E402

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(f"{name} [{detail}]")
        print(f"FAIL {name} [{detail}]")


class Resp:
    status_code = 200
    content = b"<rss/>"
    headers = {}

    def iter_content(self, n=65536):
        yield self.content


def E(title, link, published="Mon, 02 Nov 2026 09:00:00 GMT"):
    return {"title": title, "link": link, "description": f"desc {title}",
            "published": published}


# 环境替身：HTTP 边界 requests.get + validate 放行假主机（真实逐跳校验在 H 组已验）
class HttpStub:
    def __init__(self):
        self.calls = 0

    def __call__(self, url, **kw):
        self.calls += 1
        return Resp()


_orig_get = requests_mod.get
_orig_validate = sf.validate_url
_orig_load = cfgmod.load
http = HttpStub()
requests_mod.get = http
sf.validate_url = lambda url, **kw: url  # 测试假主机免 DNS；逐跳逻辑由 H 组验证
cfgmod.load = lambda: {"outbound": {"trusted_proxy": True}, "fetch": {}, "x": {}, "network": {}}
feed = types.SimpleNamespace(entries=[])


def set_feed(entries):
    feed.entries = list(entries)


rssfetch.feedparser = types.SimpleNamespace(parse=lambda c: feed)


def add_source(name):
    db.add_rss_source(name, f"https://stub.local/{name}")
    return db.get_conn().execute("SELECT id FROM rss_sources WHERE name=?",
                                 (name,)).fetchone()["id"]


def mark_due(sid):
    db.set_kv(f"rss_last_{sid}", "0")


try:
    # ============================================================
    print("== I1 固定反例：首条保持不变，只在第二条新增 ==")
    sid1 = add_source("I1")
    set_feed([E("A", "https://e/a"), E("B", "https://e/b")])
    mark_due(sid1)
    r1 = rssfetch.run_rss_round("test")
    check("I1-1 首轮入库 2 条", r1["new"] == 2, str(r1))
    # 新条目插到第二位（首位 A 保持不变）——旧实现：首条 guid 未变 → 整源跳过
    set_feed([E("A", "https://e/a"), E("NEW", "https://e/new"), E("B", "https://e/b")])
    mark_due(sid1)
    r2 = rssfetch.run_rss_round("test")
    check("I1-2 首条不变+第二条新增 → 仍发现新条目（反例闭合）", r2["new"] == 1, str(r2))
    rows = db.get_conn().execute(
        "SELECT text FROM tweets WHERE screen_name='RSS:I1'").fetchall()
    texts = [dict(x)["text"] for x in rows]
    check("I1-3 A 未重复入库（仍 1 条）",
          sum(1 for t in texts if "A" in t) == 1, str(texts))
    check("I1-4 NEW 已入库", any("NEW" in t for t in texts), str(texts))

    # ============================================================
    print("== I2 置顶条目在首位（真实 RSS 场景） ==")
    sid2 = add_source("I2")
    set_feed([E("置顶公告", "https://e/pin"), E("条目1", "https://e/i1")])
    mark_due(sid2)
    rssfetch.run_rss_round("test")
    set_feed([E("置顶公告", "https://e/pin"), E("条目2-新", "https://e/i2"),
              E("条目1", "https://e/i1")])
    mark_due(sid2)
    r_i2 = rssfetch.run_rss_round("test")
    check("I2-1 置顶未变 + 中部新增 → 不漏采", r_i2["new"] == 1, str(r_i2))

    # ============================================================
    print("== I3 入库失败不推进位点，重跑补齐不重复 ==")
    sid3 = add_source("I3")
    set_feed([E("X1", "https://e/x1"), E("X2", "https://e/x2")])
    mark_due(sid3)
    orig_insert = db.insert_tweet
    calls3 = {"n": 0}

    def _flaky_insert(t):
        calls3["n"] += 1
        if calls3["n"] == 1:
            raise RuntimeError("模拟入库失败")
        return orig_insert(t)

    db.insert_tweet = _flaky_insert
    try:
        r3 = rssfetch.run_rss_round("test")
    finally:
        db.insert_tweet = orig_insert
    top3_after_fail = db.get_kv(f"rss_top_{sid3}")
    check("I3-1 入库异常时位点未推进", top3_after_fail is None, str(top3_after_fail))
    mark_due(sid3)
    r3b = rssfetch.run_rss_round("test")
    rows3 = db.get_conn().execute(
        "SELECT COUNT(*) n FROM tweets WHERE screen_name='RSS:I3'").fetchone()["n"]
    check("I3-2 重跑补齐（X1+X2 共 2 条）", rows3 == 2, f"rows={rows3}")
    check("I3-3 位点在成功轮推进", db.get_kv(f"rss_top_{sid3}") is not None, "")

    # ============================================================
    print("== I4 重跑整轮幂等（零重复） ==")
    mark_due(sid3)
    r4 = rssfetch.run_rss_round("test")
    check("I4-1 相同 feed 重跑 → 0 新增", r4["new"] == 0, str(r4))
    mark_due(sid3)
    r4b = rssfetch.run_rss_round("test")
    rows4 = db.get_conn().execute(
        "SELECT COUNT(*) n FROM tweets WHERE screen_name='RSS:I3'").fetchone()["n"]
    check("I4-2 数据不重复", rows4 == 2, f"rows={rows4}")

    # ============================================================
    print("== I5 来源改名：不重复入库且归属不变（真实 sid 夹具） ==")
    sid5 = add_source("I5原")
    set_feed([E("M1", "https://e/m1"), E("M2", "https://e/m2")])
    mark_due(sid5)
    rssfetch.run_rss_round("test")
    db.get_conn().execute("UPDATE rss_sources SET name='I5改名' WHERE id=?", (sid5,))
    db.get_conn().commit()
    set_feed([E("M3-新", "https://e/m3"), E("M1", "https://e/m1"), E("M2", "https://e/m2")])
    mark_due(sid5)
    r5 = rssfetch.run_rss_round("test")
    check("I5-1 改名后仅新增条目入库（旧条目不重复）", r5["new"] == 1, str(r5))
    total5 = db.get_conn().execute(
        "SELECT COUNT(*) n FROM tweets WHERE source_id=?", (f"rss:{sid5}",)).fetchone()["n"]
    check("I5-2 归属完整（source_id 下 3 条）", total5 == 3, f"n={total5}")
    dup5 = db.get_conn().execute(
        "SELECT COUNT(*) n FROM (SELECT tweet_id FROM tweets WHERE source_id=?"
        " GROUP BY tweet_id HAVING COUNT(*)>1)", (f"rss:{sid5}",)).fetchone()["n"]
    check("I5-3 无重复条目", dup5 == 0, f"dup={dup5}")

    # ============================================================
    print("== I6 新条目在首位（原测试场景仍有效） ==")
    sid6 = add_source("I6")
    set_feed([E("N1", "https://e/n1")])
    mark_due(sid6)
    rssfetch.run_rss_round("test")
    set_feed([E("N2-最新", "https://e/n2"), E("N1", "https://e/n1")])
    mark_due(sid6)
    r6 = rssfetch.run_rss_round("test")
    check("I6-1 首位新增 → 正常入库", r6["new"] == 1, str(r6))

finally:
    requests_mod.get = _orig_get
    sf.validate_url = _orig_validate
    cfgmod.load = _orig_load

print(f"\n==== I 组 RSS 漏采：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
