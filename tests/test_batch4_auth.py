# -*- coding: utf-8 -*-
"""第四批：认证与授权验收（临时库 + TestClient，无真实外联）。

覆盖：
  A1 未初始化时匿名访问被拒（503）
  A2 管理员 Token 创建/权限分级/吊销
  A3 只读 Token 不能修改（403）
  A4 无效/过期 Token 被拒（401）
  A5 登录/会话/CSRF
  A6 会话过期
  A7 登出后失效
  A8 健康检查匿名可访问且最小信息
  A9 认证失败不回退匿名
运行：C:/Python314/python.exe tests/test_batch4_auth.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from tests.testenv import TestEnv

env = TestEnv(forbid_network=True).start()   # 认证测试无需外联
import app.auth as auth  # noqa: E402
import app.db as db  # noqa: E402

db.DATA_DIR = env.tmp
db._conn = None
db.init_db()
auth.init_auth_tables()

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(name)
        print(f"FAIL {name} {detail}")


from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

# ============================================================
print("== A1 未初始化 ==")
r = client.get("/api/tweets")
check("A1-1 未初始化时接口返回 503", r.status_code == 503, f"{r.status_code} {r.text[:60]}")
check("A1-2 未初始化无匿名放行", "匿名" not in r.text)

# 初始化管理员
auth.create_admin("admin", "AdminPass123")
# 关闭调度副作用：本测试不启动 scheduler（TestClient 不触发 lifespan 的 start？会触发）
# 说明：lifespan 会初始化 DB/认证，但临时目录内无生产配置，安全。

# ============================================================
print("== A2 匿名访问被拒 ==")
r = client.get("/api/tweets")
check("A2-1 匿名读被拒 401", r.status_code == 401, str(r.status_code))
r = client.put("/api/push/filters", json={"values": {}})
check("A2-2 匿名写被拒 401", r.status_code == 401, str(r.status_code))

# ============================================================
print("== A3 管理员 Token ==")
raw_admin = auth.create_token("ci-admin", "admin")
h_admin = {"Authorization": f"Bearer {raw_admin}"}
r = client.get("/api/tweets", headers=h_admin)
check("A3-1 管理员 Token 可读", r.status_code == 200, str(r.status_code))
r = client.get("/api/auth/tokens", headers=h_admin)
check("A3-2 管理员可列 Token", r.status_code == 200, str(r.status_code))

# 只读 Token
raw_read = auth.create_token("ci-read", "read")
h_read = {"Authorization": f"Bearer {raw_read}"}
r = client.get("/api/tweets", headers=h_read)
check("A3-3 只读 Token 可读", r.status_code == 200, str(r.status_code))
r = client.put("/api/push/filters", json={"values": {"min_severity": "high"}}, headers=h_read)
check("A3-4 只读 Token 不能改配置（403）", r.status_code == 403, str(r.status_code))
r = client.post("/api/tweets/cleanup", json={"keep_days": 1}, headers=h_read)
check("A3-5 只读 Token 不能删除数据（403）", r.status_code == 403, str(r.status_code))
r = client.post("/api/channels/wecom/instances/0/test", headers=h_read)
check("A3-6 只读 Token 不能触发发送（403）", r.status_code == 403, str(r.status_code))
r = client.post("/api/channels/wecom/instances/0/backfill", json={"preview": True}, headers=h_read)
check("A3-7 只读 Token 不能补发（403）", r.status_code in (403, 404), str(r.status_code))
r = client.get("/api/auth/tokens", headers=h_read)
check("A3-8 只读 Token 不能管理 Token（403）", r.status_code == 403, str(r.status_code))

# 写 Token
raw_write = auth.create_token("ci-write", "write")
h_write = {"Authorization": f"Bearer {raw_write}"}
r = client.put("/api/push/filters", json={"values": {"min_severity": "high"}}, headers=h_write)
check("A3-9 写 Token 可改配置", r.status_code == 200, str(r.status_code))
r = client.get("/api/auth/tokens", headers=h_write)
check("A3-10 写 Token 不能管理 Token（403）", r.status_code == 403, str(r.status_code))

# ============================================================
print("== A4 无效/吊销 Token ==")
r = client.get("/api/tweets", headers={"Authorization": "Bearer xintel_invalid"})
check("A4-1 无效 Token 401", r.status_code == 401, str(r.status_code))
r = client.get("/api/tweets", headers={"Authorization": "Bearer gho_fake"})
check("A4-2 非本系统 Token 401", r.status_code == 401, str(r.status_code))
toks = auth.list_tokens()
rid = [t["id"] for t in toks if t["name"] == "ci-read"][0]
auth.revoke_token(rid)
r = client.get("/api/tweets", headers=h_read)
check("A4-3 吊销后 401", r.status_code == 401, str(r.status_code))

# ============================================================
print("== A5 登录 / CSRF / 会话 ==")
r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
check("A5-1 错误密码 401", r.status_code == 401, str(r.status_code))
r = client.post("/api/auth/login", json={"username": "admin", "password": "AdminPass123"})
check("A5-2 登录成功", r.status_code == 200, str(r.status_code))
csrf = r.json().get("csrf", "")
check("A5-3 登录返回 CSRF token", bool(csrf))
check("A5-4 Cookie 为 HttpOnly", "httponly" in r.headers.get("set-cookie", "").lower(),
      r.headers.get("set-cookie", "")[:80])
r = client.get("/api/auth/me")
check("A5-5 会话可查询身份", r.status_code == 200 and r.json().get("username") == "admin",
      str(r.json())[:80])
# CSRF：会话写请求无 token → 403
r = client.put("/api/push/filters", json={"values": {"min_severity": "low"}})
check("A5-6 会话写请求缺 CSRF → 403", r.status_code == 403, str(r.status_code))
r = client.put("/api/push/filters", json={"values": {"min_severity": "low"}},
                headers={"X-CSRF-Token": csrf})
check("A5-7 带正确 CSRF 可写", r.status_code == 200, str(r.status_code))
r = client.put("/api/push/filters", json={"values": {"min_severity": "low"}},
                headers={"X-CSRF-Token": "wrong"})
check("A5-8 错误 CSRF → 403", r.status_code == 403, str(r.status_code))

# ============================================================
print("== A6 会话过期 ==")
sid = client.cookies.get(auth.COOKIE_NAME)
db.get_conn().execute("UPDATE auth_sessions SET expires_at=? WHERE sid=?",
                      (time.time() - 10, sid))
db.get_conn().commit()
r = client.get("/api/auth/me")
check("A6-1 过期会话被拒 401", r.status_code == 401, str(r.status_code))

# ============================================================
print("== A7 登出 ==")
r = client.post("/api/auth/login", json={"username": "admin", "password": "AdminPass123"})
csrf2 = r.json()["csrf"]
r = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf2})
check("A7-1 登出成功", r.status_code == 200, str(r.status_code))
r = client.get("/api/auth/me")
check("A7-2 登出后会话失效", r.status_code == 401, str(r.status_code))

# ============================================================
print("== A8 健康检查 ==")
r = client.get("/api/healthz")
check("A8-1 匿名可访问健康检查", r.status_code == 200)
body = r.json()
check("A8-2 健康检查最小信息（不含路径/版本/配置）",
      set(body.keys()) <= {"ok"}, str(body))

# ============================================================
print("== A9 认证失败不回退匿名 ==")
r = client.get("/api/tweets", headers={"Authorization": "Bearer "})
check("A9-1 空 Bearer 仍 401", r.status_code == 401, str(r.status_code))
r = client.get("/api/tweets", cookies={auth.COOKIE_NAME: "forged-session"})
check("A9-2 伪造会话 401", r.status_code == 401, str(r.status_code))

print(f"\n==== 认证授权：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
