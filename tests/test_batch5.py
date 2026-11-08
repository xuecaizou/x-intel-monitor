# -*- coding: utf-8 -*-
"""第五批验收：L1 版本并发、L2 完整性分级、出站例外、认证关闭约束、进程模型声明。

运行：C:/Python314/python.exe tests/test_batch5.py
"""
import json
import sys
import time
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

import app.integrity as integ  # noqa: E402

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(name)
        print(f"FAIL {name} {detail}")


def mk(text, **fields):
    tid = db.insert_tweet({"screen_name": "B5", "tweet_id": f"b5-{text}",
                           "text": text, "url": "u", "published_at": db.now()})
    if fields:
        db.update_ai(tid, fields)
    return tid


# ============================================================
print("== L1-1 AI 开始 → 用户修改 → AI 返回 ==")
t1 = mk("race1")
base = db.get_analysis_version(t1)
# 模拟：AI 调用中（已取 base），用户在此期间人工修改
db.manual_override(t1, {"geo_country": "人工国"})
ok, reason, cur = db.apply_ai_result(t1, {"geo_country": "迟到AI", "ai_status": "ok"}, base)
check("L1-1a 迟到结果不覆盖人工值", db.get_tweet(t1)["geo_country"] == "人工国",
      db.get_tweet(t1)["geo_country"])
check("L1-1b 返回未被应用+原因", ok is False and reason in ("manual_modified", "manual_protected"),
      f"{ok}/{reason}")
check("L1-1c 冲突写入审计", any(c["tweet_id"] == t1 for c in db.list_conflicts()))

# ============================================================
print("== L1-2 两个 AI 任务乱序返回 ==")
t2 = mk("race2")
v = db.get_analysis_version(t2)
okA, _, _ = db.apply_ai_result(t2, {"ai_assessment": "任务A结果", "ai_status": "ok"}, v)
okB, reasonB, _ = db.apply_ai_result(t2, {"ai_assessment": "任务B迟到", "ai_status": "ok"}, v)
check("L1-2a 先到结果生效", okA is True)
check("L1-2b 后到结果不覆盖", db.get_tweet(t2)["ai_assessment"] == "任务A结果",
      db.get_tweet(t2)["ai_assessment"])
check("L1-2c 后到结果被拒（版本已推进）", okB is False and reasonB in ("race_lost", "version_changed"), reasonB)

# ============================================================
print("== L1-3 原文更新后旧分析返回 ==")
t3 = mk("race3")
v3 = db.get_analysis_version(t3)
db.update_tweet_text(t3, "原文已更新")
ok3, reason3, _ = db.apply_ai_result(t3, {"ai_assessment": "基于旧原文", "ai_status": "ok"}, v3)
check("L1-3a 版本因原文更新推进", db.get_analysis_version(t3) > v3)
check("L1-3b 旧分析不应用", ok3 is False and reason3 == "version_changed", reason3)
check("L1-3c 当前有效值未被旧结果写入",
      (db.get_tweet(t3).get("ai_assessment") or "") != "基于旧原文")

# ============================================================
print("== L1-4 人工撤销覆盖（显式重新分析） ==")
t4 = mk("race4")
db.manual_override(t4, {"geo_country": "人工国"})
check("L1-4a 人工保护生效", int(db.get_tweet(t4)["ai_modified"] or 0) == 1)
cleared = db.clear_manual_protection(t4)
check("L1-4b 显式清除保护", cleared is True and int(db.get_tweet(t4)["ai_modified"] or 0) == 0)
v4 = db.get_analysis_version(t4)
ok4, _, _ = db.apply_ai_result(t4, {"geo_country": "重新分析国", "ai_status": "ok"}, v4)
check("L1-4c 清除后新结果可应用", ok4 is True and db.get_tweet(t4)["geo_country"] == "重新分析国")

# ============================================================
print("== L1-5 冲突不改变当前值且不自动重试 ==")
before = len(db.list_conflicts())
t5 = mk("race5")
v5 = db.get_analysis_version(t5)
db.manual_override(t5, {"geo_country": "人工A"})
db.apply_ai_result(t5, {"geo_country": "AI-B"}, v5)
db.apply_ai_result(t5, {"geo_country": "AI-B-again"}, v5)   # 同一 base 再提交
after = len(db.list_conflicts())
check("L1-5a 每次冲突都留审计（2 条）", after - before == 2, f"{before}->{after}")
check("L1-5b 当前值仍为人工值", db.get_tweet(t5)["geo_country"] == "人工A")

# ============================================================
print("== L2 完整性分级 ==")
g_verified = integ.grade_from_issues([], fatal=False)
g_norm = integ.grade_from_issues(["severity 非法(超高)→info"], fatal=False)
g_partial = integ.grade_from_issues([], fatal=False, repaired=True)
g_partial2 = integ.grade_from_issues([], fatal=False, truncated=True)
g_partial3 = integ.grade_from_issues(["缺少 assessment"], fatal=False)
g_invalid = integ.grade_from_issues([], fatal=True)
check("L2-1 严格有效 → verified", g_verified == "verified", g_verified)
check("L2-2 仅枚举纠正 → normalized", g_norm == "normalized", g_norm)
check("L2-3 修复/截断 → partial", g_partial == "partial" and g_partial2 == "partial")
check("L2-4 缺必需字段 → partial", g_partial3 == "partial", g_partial3)
check("L2-5 无效 → invalid", g_invalid == "invalid")

# 下游判断
t_v = {"ai_integrity": "verified"}
t_n = {"ai_integrity": "normalized"}
t_p = {"ai_integrity": "partial"}
t_i = {"ai_integrity": "invalid"}
check("L2-6 verified 允许且不标注", integ.may_auto_downstream(t_v) == (True, False, ""))
check("L2-7 partial 允许但必须标注", integ.may_auto_downstream(t_p)[:2] == (True, True))
check("L2-8 invalid 禁止下游", integ.may_auto_downstream(t_i)[0] is False)
# 人工审核 → verified
t_rev = {"ai_integrity": "partial", "ai_review": "manual"}
check("L2-9 人工审核后视为 verified", integ.effective_grade(t_rev) == "verified"
      and integ.may_auto_downstream(t_rev)[:2] == (True, False))

# ============================================================
print("== L2 下游一致性（推送/报告共用同一函数） ==")
import app.engine as engine  # noqa: E402
import app.reports as reports  # noqa: E402
src_engine = (ROOT / "app" / "engine.py").read_text(encoding="utf-8")
src_reports = (ROOT / "app" / "reports.py").read_text(encoding="utf-8")
check("L2-10 推送调用统一判断", "_integ.may_auto_downstream" in src_engine)
check("L2-11 报告线索包调用统一判断", "may_auto_downstream" in src_reports)

# 报告线索包字段
t_pkg = mk("pkg-partial", ai_status="ok", ai_integrity="partial", ai_repair="JSON 经修复解析")
pkg = reports._tweet_package(db.get_tweet(t_pkg))
check("L2-12 线索包含 integrity 分级", pkg["ai"]["integrity"] == "partial", str(pkg["ai"])[:80])
check("L2-13 线索包标注必须人工", pkg["ai"]["must_label_unfinished"] is True)

# ============================================================
print("== 出站例外：按用途白名单 ==")
import app.safefetch as sf  # noqa: E402
check("L2/OUT-1 RSS 用途默认禁止私网",
      not sf._allowed_private_targets("rss"))
# 模拟管理员配置：仅 AI 网关允许内网
import app.config as cfgmod  # noqa: E402
real_load = cfgmod.load
cfgmod.load = lambda: {"outbound": {"allow_private": {"ai_gateway": ["192.168.1.53:20128"]}}}
try:
    sf.validate_url("http://192.168.1.53:20128/v1/models", purpose="ai_gateway")
    ok_gw = True
except Exception as e:
    ok_gw = False
    print("  AI 网关例外失败:", str(e)[:60])
check("OUT-2 管理员授权的 AI 网关可访问", ok_gw)
try:
    sf.validate_url("http://192.168.1.53:20128/v1", purpose="rss")
    rss_blocked = False
except sf.UnsafeUrlError:
    rss_blocked = True
check("OUT-3 同一内网地址在 RSS 用途仍被拒（不共享例外）", rss_blocked)
try:
    sf.validate_url("http://192.168.1.99/x", purpose="rss")
    other_blocked = False
except sf.UnsafeUrlError:
    other_blocked = True
check("OUT-4 RSS 访问其它内网地址被拒", other_blocked)
# 代理信任
try:
    sf.check_proxy_trust("rss", "socks5h://127.0.0.1:10808")
    proxy_blocked = False
except sf.UnsafeUrlError:
    proxy_blocked = True
check("OUT-5 低信任用途走代理需 trusted_proxy 声明", proxy_blocked)
cfgmod.load = lambda: {"outbound": {"trusted_proxy": True,
                                    "allow_private": {"ai_gateway": ["192.168.1.53:20128"]}}}
try:
    sf.check_proxy_trust("rss", "socks5h://127.0.0.1:10808")
    proxy_ok = True
except Exception:
    proxy_ok = False
check("OUT-6 声明受控出口后允许", proxy_ok)
cfgmod.load = real_load

# ============================================================
print("== 认证关闭约束 ==")
import os
import app.auth as auth  # noqa: E402
cfgmod.load = lambda: {"auth": {"enabled": False}}
os.environ.pop("XINTEL_TEST", None)
check("AUTH-1 非测试模式拒绝关闭认证", auth._auth_disabled_allowed() is False)
os.environ["XINTEL_TEST"] = "1"
check("AUTH-2 测试模式允许关闭", auth._auth_disabled_allowed() is True)
os.environ.pop("XINTEL_TEST", None)
cfgmod.load = real_load

# 权限：write Token 不能管理 Token（由路由依赖保证，静态确认）
src_routes = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
check("AUTH-3 Token 管理仅 admin",
      '@router.get("/api/auth/tokens", dependencies=[Depends(auth.require("admin"))])' in src_routes
      and '@router.post("/api/auth/tokens", dependencies=[Depends(auth.require("admin"))])' in src_routes)

# ============================================================
print("== 进程模型声明 ==")
sched = (ROOT / "app" / "scheduler.py").read_text(encoding="utf-8")
check("PROC-1 调度为进程内单实例（无跨进程锁）",
      "BackgroundScheduler" in sched or "threading" in sched)
check("PROC-2 单进程支持范围已文档化",
      "单进程" in (ROOT / "docs" / "architecture-notes.md").read_text(encoding="utf-8"))

print(f"\n==== 第五批验收：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
