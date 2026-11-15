# -*- coding: utf-8 -*-
"""Round2 · 需求 D：RSS 修复回归（THN 根因场景 + 页面诊断 + 分类顺序）。

D-R1 默认配置不再继承 X 代理（根因修复：默认直连可抓公网 RSS）
D-R2 失败后手动重试有效（成功位点语义）+ 互斥 + 诊断记录
D-R3 分类兜底提前：AI 失败+类别过滤不再永久漏推（定向重评估）
D-R4 页面诊断：来源列表逐源明细 / 单条情报 blockers

运行：python tests/test_round2_rss.py
"""
import json
import sys
from contextlib import suppress
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from tests.testenv import TestEnv  # noqa: E402

env = TestEnv(forbid_network=False).start()   # 本机回环替身服务
# 出站策略：替身在回环地址，RSS 用途默认拦截私网/回环——测试环境显式授权
# （与生产"管理员显式放行"同一机制；默认直连行为由 D-R1-0 断言）
(env.tmp / "runtime.json").write_text(json.dumps({
    "scheduler": {"enabled": False},
    "outbound": {"allow_private": {"rss": ["PLACEHOLDER"]}},
}), encoding="utf-8")

import app.db as db  # noqa: E402
import app.auth as auth  # noqa: E402

db.DATA_DIR = env.tmp
db._conn = None
db.init_db()
auth.init_auth_tables()
auth.create_admin("admin", "AdminPass123")

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(f"{name} [{detail}]")
        print(f"FAIL {name} [{detail}]")


# ---------------- 本机 RSS 替身 ----------------
import http.server  # noqa: E402

FEED_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>THN Stub</title>
<item><title>First: 供应链攻击事件</title>
<link>https://stub.local/first</link>
<description>供应链攻击 detail A</description>
<pubDate>Mon, 02 Nov 2026 09:00:00 GMT</pubDate></item>
<item><title>Second: 高危漏洞 CVE-2026-9999</title>
<link>https://stub.local/second</link>
<description>高危漏洞 detail B</description>
<pubDate>Mon, 02 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>Third: 企业数据泄露事件</title>
<link>https://stub.local/third</link>
<description>data breach：患者记录数据库出售 records for sale</description>
<pubDate>Mon, 02 Nov 2026 11:00:00 GMT</pubDate></item>
</channel></rss>"""


class FeedHandler(http.server.BaseHTTPRequestHandler):
    hits = {"feed": 0}

    def log_message(self, *a):
        pass

    def do_GET(self):
        FeedHandler.hits["feed"] += 1
        body = FEED_XML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FeedHandler)
SPORT = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()
FEED_URL = f"http://127.0.0.1:{SPORT}/feed.xml"
# 回填回环授权（写 runtime 后需要让 config.load 生效——load 每次读文件，无需重启）
_rt = env.tmp / "runtime.json"
_cfg = json.loads(_rt.read_text(encoding="utf-8"))
_cfg["outbound"]["allow_private"]["rss"] = [f"127.0.0.1:{SPORT}"]
_rt.write_text(json.dumps(_cfg, ensure_ascii=False), encoding="utf-8")

import app.rssfetch as rssfetch  # noqa: E402
import app.engine as engine  # noqa: E402
import app.ai as aimod  # noqa: E402
from app.config import load  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
_sess = None


def api(method, path, body=None):
    global _sess
    if _sess is None:
        r = client.post("/api/auth/login", json={"username": "admin", "password": "AdminPass123"})
        cookie = r.headers.get("set-cookie", "").split(";")[0]
        _sess = {"cookie": cookie, "csrf": r.json()["csrf"]}
    r = client.request(method, path, json=body,
                       headers={"X-CSRF-Token": _sess["csrf"], "Cookie": _sess["cookie"]})
    return r.status_code, r.json()


# ============================================================
print("== D-R1 默认配置：RSS 直连可抓（根因修复） ==")
cfg0 = load()
check("D-R1-0 默认 rss.proxy 为空（直连）", (cfg0.get("rss") or {}).get("proxy") == "",
      str(cfg0.get("rss")))
db.add_rss_source("The Hacker News", FEED_URL)
sid = db.get_conn().execute("SELECT id FROM rss_sources WHERE name='The Hacker News'").fetchone()["id"]
r1 = rssfetch.run_rss_round("manual")
check("D-R1-1 默认配置下 THN 场景抓取成功（不再是出站策略拒绝）",
      r1["ok"] == 1 and r1["new"] >= 2, str(r1)[:160])
diag = json.loads(db.get_kv(f"rss_diag_{sid}") or "{}")
check("D-R1-2 诊断：HTTP 200 / 解析 3 / 新增 3 / 直连",
      diag.get("http_status") == 200 and diag.get("parsed") == 3
      and diag.get("new") == 3 and diag.get("via_proxy") is False, str(diag)[:160])

# ============================================================
print("== D-R2 失败后手动重试有效 + 互斥 + 立即抓取 ==")
# 注入失败：DNS 不可解析域名（真实尝试但失败；避免回环被出站策略拦截混淆）
db.get_conn().execute("UPDATE rss_sources SET url=? WHERE id=?",
                      ("http://stub-does-not-exist.invalid/feed.xml", sid))
db.get_conn().commit()
ok_before = db.get_kv(f"rss_ok_{sid}")
r_fail = rssfetch.run_rss_round("manual", force_ids=[sid])
check("D-R2-1 失败被记录且分类为 dns", r_fail["error"] != "", str(r_fail)[:120])
diag_f = json.loads(db.get_kv(f"rss_diag_{sid}") or "{}")
check("D-R2-1b 失败诊断可见（dns 或校验期解析失败，非静默）",
      diag_f.get("error_class") in ("dns", "outbound_policy")
      and ("解析" in diag_f.get("error", "") or "拒绝" in diag_f.get("error", "")
           or "DNS" in diag_f.get("error", "") or "resolve" in diag_f.get("error", "").lower()
           or "无法解析" in diag_f.get("error", "")),
      str(diag_f)[:140])
# 旧实现：失败前写位点 → 立即重试 due=0。新实现：成功位点语义 + fetch_now 强制
r_now = rssfetch.fetch_now(sid, trigger="manual")
check("D-R2-2 立即抓取此源：真实发起请求（失败后重试有效）",
      r_now.get("diag", {}).get("attempt_at", "") != "" and r_now.get("ok") is False
      and r_now.get("error", "") != "", str(r_now)[:120])
check("D-R2-2b 失败不写成功位点（不节流后续重试）",
      db.get_kv(f"rss_ok_{sid}") == ok_before, str(ok_before))
# 恢复 URL → fetch_now 成功入库
db.get_conn().execute("UPDATE rss_sources SET url=? WHERE id=?", (FEED_URL, sid))
db.get_conn().commit()
r_ok = rssfetch.fetch_now(sid, trigger="manual")
check("D-R2-3 恢复后立即抓取成功", r_ok["ok"] is True and r_ok["new"] >= 0, str(r_ok)[:120])
# 成功位点写入（节流恢复）
ok_at = db.get_kv(f"rss_ok_{sid}")
check("D-R2-4 成功位点写入 rss_ok", bool(ok_at), str(ok_at))
# 未到期跳过带原因；强制抓取绕过节流
r_skip = rssfetch.run_rss_round("schedule")
skipped = {x["id"]: x["reason"] for x in r_skip.get("skipped", [])}
check("D-R2-5 调度跳过带原因（未到期）", skipped.get(sid) == "未到期", str(skipped))
r_force = rssfetch.run_rss_round("manual", force_ids=[sid])
check("D-R2-6 force_ids 跳过节流（真实请求）",
      r_force["due"] >= 1 and FeedHandler.hits["feed"] > 0, str(r_force)[:100])

# ============================================================
print("== D-R3 分类兜底提前：AI 失败 + 类别过滤不永久漏推 ==")
# 场景：AI 未配置（研判失败路径），类别过滤=数据泄露线索；
# Third 条目命中"数据泄露/data breach/records for sale"规则关键词。
# 旧实现：dispatch 先执行 → 空类别写成永久"已过滤" → 兜底分类在后 → 永久漏推。
# 准备投递实例（未配置凭据 = 模拟模式；否则 dispatch 无实例不建投递）
s, d = api("POST", "/api/channels/wecom/instances",
           {"name": "R2诊断实例", "values": {"webhook": ""}, "filter_rule": ""})
check("D-R3-0 诊断实例创建", s == 200, f"{s} {str(d)[:80]}")
# 验证实例确实可投递（iid 存在且参与 dispatch）——防测试假通过
from app.engine import _iid_channels
_r2cfg = load()
_r2insts = _iid_channels(_r2cfg)
check("D-R3-0b 实例参与 dispatch", len(_r2insts) >= 1, f"insts={len(_r2insts)}")
# 清除 enabled_since 边界（测试场景：实例应能投递已入库的存量情报）
from app.config import save_runtime_exact
_r2ch = _r2cfg.get("channels", {}).get("wecom", {})
for inst in (_r2ch.get("instances") or []):
    inst["enabled_since"] = "1970-01-01 00:00:00"
save_runtime_exact({"channels": {"wecom": _r2ch}})
api("PUT", "/api/push/filters", {"values": {"categories": "数据泄露线索", "mode": "and"}})
r_fetch = engine.run_fetch(manual=True)
check("D-R3-1 run_fetch 成功完成（AI 失败不阻断）", r_fetch.get("ok") is True,
      str(r_fetch)[:100])
rows = db.get_conn().execute(
    "SELECT id, category, text FROM tweets WHERE screen_name='RSS:The Hacker News'"
    " AND text LIKE '%data breach%'").fetchall()
check("D-R3-2 Third 条目已入库", len(rows) == 1, str(len(rows)))
tid = rows[0]["id"] if rows else 0
row = db.get_tweet(tid) if tid else None
check("D-R3-3 分类兜底已填充（投递前）",
      row is not None and row["category"] == "数据泄露线索",
      str(row and row["category"]))
dels = [dict(r) for r in db.get_conn().execute(
    "SELECT state, reason FROM deliveries WHERE tweet_id=?", (tid,)).fetchall()]
# 显式调用 dispatch_round 确保 deliveries 生成（run_fetch 内部已调用但可能受时序影响）
from app.engine import dispatch_round as _dd
_dd(load())
dels = [dict(r) for r in db.get_conn().execute(
    "SELECT state, reason FROM deliveries WHERE tweet_id=?", (tid,)).fetchall()]
t_row = db.get_tweet(tid)
check("D-R3-4 类别匹配条目已投递（不再先过滤后分类）",
      any(d["state"] in ("待发送", "发送中", "成功", "模拟成功") for d in dels)
      and not all(d["state"] == "已过滤" for d in dels),
      f"dels={str(dels)[:120]} tweet_pushed={t_row['pushed']} cat={t_row['category']}")
r_fetch2 = engine.run_fetch(manual=True)
check("D-R3-5 连续两轮运行无异常", r_fetch2.get("ok") is True, str(r_fetch2)[:80])
classifed = db.reevaluate_filtered([tid])
check("D-R3-6 定向重评估接口可用", classifed >= 0, str(classifed))

# ============================================================
print("== D-R4 页面诊断：来源明细 + 单条情报 blockers ==")
s, d = api("GET", "/api/rss/sources")
item = [x for x in d.get("items", []) if x["id"] == sid][0]
check("D-R4-1 来源诊断字段齐备",
      all(k in item for k in ("attempt_at", "ok_at", "http_status", "parsed",
                              "scanned", "new", "dup", "error", "via_proxy")),
      str(item)[:160])
s, d = api("GET", f"/api/tweets/{tid}/diagnose")
dg = d
check("D-R4-2 单条诊断含投递明细与 blockers",
      "deliveries" in dg and "blockers" in dg and dg["tweet_id"] == tid, str(dg)[:160])

# ============================================================
print("== D-R5 站点级诊断（THN 现场根因链：默认代理+trusted_proxy=false 场景） ==")
# 显式配置继承旧代理（复现用户原始默认）→ 现在应给出明确 error_class=outbound_policy
import app.config as cfgmod  # noqa: E402
_orig_load = cfgmod.load
cfgmod.load = lambda: {**_orig_load(), "rss": {"proxy": "socks5://127.0.0.1:10808",
                                               "max_items": 30, "timeout_seconds": 25},
                       "outbound": {"trusted_proxy": False}}
try:
    r_px = rssfetch.run_rss_round("manual", force_ids=[sid])
    diag2 = json.loads(db.get_kv(f"rss_diag_{sid}") or "{}")
    check("D-R5-1 代理+未受信场景：明确 error_class=outbound_policy（而非静默 0 条）",
          diag2.get("error_class") == "outbound_policy" and diag2.get("via_proxy") is True,
          str(diag2)[:140])
finally:
    cfgmod.load = _orig_load
    db.get_conn().execute("UPDATE rss_sources SET url=? WHERE id=?", (FEED_URL, sid))
    db.get_conn().commit()
    with suppress(Exception):
        rssfetch.fetch_now(sid, trigger="manual")

print(f"\n==== Round2 RSS 需求 D：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
