# -*- coding: utf-8 -*-
"""Round2 · 需求 B/C：AI 草稿测试不落库 + 多 LLM 通道（真实 HTTP 双替身端点）。

U06 草稿配置测试：保存 A、草稿 B，实际测试 B 而运行配置仍 A；失败不污染 A
U07 草稿拉取模型：不保存即可枚举（无隐式 PUT）；改配置后旧成功状态失效（指纹）
U08 多通道：两独立端点并发，任务按指定通道调用；默认/停用/删除/迁移正确
"""
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from tests.testenv import TestEnv  # noqa: E402

env = TestEnv(forbid_network=False).start()   # 允许本机回环（替身 HTTP 服务）
(env.tmp / "runtime.json").write_text(json.dumps({
    "scheduler": {"enabled": False},
    "ai": {"enabled": True, "base_url": "http://127.0.0.1:1/v1", "api_key": "legacy-key",
           "model": "legacy-model", "auto_analyze": True, "max_tokens": 4096},
}), encoding="utf-8")

import app.db as db  # noqa: E402
import app.auth as auth  # noqa: E402
import app.ai_channels as aich  # noqa: E402
import app.ai as aimod  # noqa: E402

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


# ---------------- 本机双替身 OpenAI 兼容端点 ----------------
import http.server  # noqa: E402


class StubHandler(http.server.BaseHTTPRequestHandler):
    hits = {"A_chat": 0, "A_models": 0, "B_chat": 0, "B_models": 0}

    def log_message(self, *a):  # 静默
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}") if n else {}
        authz = self.headers.get("Authorization", "")
        if self.path == "/A/v1/chat/completions":
            StubHandler.hits["A_chat"] += 1
            if "keyA" not in authz:
                return self._json(401, {"error": {"message": "bad key"}})
            return self._json(200, {"choices": [{"message": {"content": "链路正常"},
                                                 "finish_reason": "stop"}]})
        if self.path == "/B/v1/chat/completions":
            StubHandler.hits["B_chat"] += 1
            if "keyB" not in authz:
                return self._json(401, {"error": {"message": "bad key"}})
            return self._json(200, {"choices": [{"message": {"content": "B 链路正常"},
                                                 "finish_reason": "stop"}]})
        self._json(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/A/v1/models":
            StubHandler.hits["A_models"] += 1
            if "keyA" not in self.headers.get("Authorization", ""):
                return self._json(401, {"error": "bad key"})
            return self._json(200, {"data": [{"id": "model-a1"}, {"id": "model-a2"}]})
        if self.path == "/B/v1/models":
            StubHandler.hits["B_models"] += 1
            if "keyB" not in self.headers.get("Authorization", ""):
                return self._json(401, {"error": "bad key"})
            return self._json(200, {"data": [{"id": "model-b1"}]})
        self._json(404, {"error": "not found"})


_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
SPORT = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()
A_BASE = f"http://127.0.0.1:{SPORT}/A/v1"
B_BASE = f"http://127.0.0.1:{SPORT}/B/v1"

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


_sess = None

def _login():
    global _sess
    if _sess is None:
        r = client.post("/api/auth/login", json={"username": "admin", "password": "AdminPass123"})
        assert r.status_code == 200, r.text
        cookie = r.headers.get("set-cookie", "").split(";")[0]
        _sess = {"cookie": cookie, "csrf": r.json()["csrf"]}
    return _sess

def api(method, path, body=None):
    sess = _login()
    r = client.request(method, path, json=body,
                       headers={"X-CSRF-Token": sess["csrf"], "Cookie": sess["cookie"]})
    return r.status_code, r.json()


# ============================================================
print("== U08-迁移：旧单份 ai 配置 → 默认通道（幂等） ==")
aich.migrate_legacy()
chans = aich.list_channels()
check("U08-1 迁移生成默认通道", len(chans) == 1 and chans[0]["id"] == "aich_legacy"
      and chans[0]["api_key"] == "legacy-key" and chans[0]["model"] == "legacy-model",
      str(chans)[:120])
check("U08-2 default 指向迁移通道", aich.default_channel_id() == "aich_legacy")
aich.migrate_legacy()
check("U08-3 幂等（不重复迁移）", len(aich.list_channels()) == 1)
cfg = aich.resolve_cfg()
check("U08-4 resolve 走迁移通道（任务记录快照字段）",
      cfg.get("channel_id") == "aich_legacy" and cfg.get("api_key") == "legacy-key",
      str(cfg)[:100])

# ============================================================
print("== U08-多通道 CRUD 与引用保护 ==")
s, d = api("POST", "/api/ai/channels", {"channel": {
    "name": "通道A", "base_url": A_BASE, "api_key": "keyA", "model": "model-a1",
    "enabled": True, "max_tokens": 100}})
check("U08-5 创建通道A", s == 200 and d["channel"]["id"], str(d)[:80])
cid_a = d["channel"]["id"]
s, d = api("POST", "/api/ai/channels", {"channel": {
    "name": "通道B", "base_url": B_BASE, "api_key": "keyB", "model": "model-b1",
    "enabled": True}})
cid_b = d["channel"]["id"]
check("U08-6 创建通道B", s == 200 and cid_b != cid_a)
s, d = api("POST", f"/api/ai/channels/{cid_a}/default", {})
check("U08-7 设默认", s == 200 and aich.default_channel_id() == cid_a)
s, d = api("POST", f"/api/ai/channels/{cid_a}/task-override",
           {"task": "writing_recommend", "channel_id": cid_b})
check("U08-8 任务覆盖设置", s == 200
      and aich.resolve_cfg(task="writing_recommend").get("channel_id") == cid_b)
s, d = api("DELETE", f"/api/ai/channels/{cid_a}")
check("U08-9 删除被引用通道被阻止（不留悬空引用）", s == 400, f"{s} {str(d)[:80]}")
s, d = api("POST", f"/api/ai/channels/{cid_b}/task-override",
           {"task": "writing_recommend", "channel_id": ""})
check("U08-10 清除任务覆盖", s == 200 and
      aich.resolve_cfg(task="writing_recommend").get("channel_id") == cid_a)
s, d = api("DELETE", f"/api/ai/channels/{cid_b}")
check("U08-11 清除引用后可删", s == 200)

# ============================================================
print("== U06 草稿测试：实际测 B 而运行配置仍 A；失败不污染 ==")
api("POST", "/api/ai/channels", {"channel": {
    "name": "通道B", "base_url": B_BASE, "api_key": "keyB", "model": "model-b1",
    "enabled": True}})
s, d = api("POST", "/api/ai/test",
           {"draft": {"base_url": B_BASE, "api_key": "keyB", "model": "model-b1"}})
check("U06-1 草稿 B 测试成功（实际打到 B 端点）",
      d.get("ok") is True and StubHandler.hits["B_chat"] >= 1, str(d)[:120])
check("U06-2 返回实际 endpoint/model/耗时/指纹",
      d.get("endpoint_host", "").endswith(str(SPORT)) and d.get("model") == "model-b1"
      and isinstance(d.get("latency_ms"), int) and d.get("fingerprint"), str(d)[:140])
runtime_ai = json.loads((env.tmp / "runtime.json").read_text(encoding="utf-8")).get("ai_channels")
legacy_ch = [c for c in runtime_ai["list"] if c["id"] == "aich_legacy"][0]
check("U06-3 运行配置未被草稿污染", legacy_ch["base_url"] == "http://127.0.0.1:1/v1",
      str(legacy_ch)[:100])
# 失败草稿：错 Key → 401 分类，且不影响已存配置
s, d = api("POST", "/api/ai/test",
           {"draft": {"base_url": B_BASE, "api_key": "WRONG", "model": "model-b1"}})
check("U06-4 凭据错误分类为 auth", d.get("ok") is False and d.get("error_class") == "auth",
      str(d)[:120])
legacy_ch = [c for c in json.loads((env.tmp / "runtime.json").read_text(
    encoding="utf-8"))["ai_channels"]["list"] if c["id"] == "aich_legacy"][0]
check("U06-5 失败也不改运行配置", legacy_ch["api_key"] == "legacy-key")

# ============================================================
print("== U07 草稿拉模型：无隐式保存 + 凭据引用 ==")
before_hits = StubHandler.hits["A_models"]
s, d = api("POST", "/api/ai/models",
           {"draft": {"base_url": A_BASE, "api_key": "", "model": ""},
            "channel_id": cid_a})
check("U07-1 草稿拉模型成功且凭据引用通道密钥（空 Key+通道id→keyA）",
      d.get("ok") is True and d.get("models") == ["model-a1", "model-a2"], str(d)[:120])
legacy_ch = [c for c in json.loads((env.tmp / "runtime.json").read_text(
    encoding="utf-8"))["ai_channels"]["list"] if c["id"] == "aich_legacy"][0]
check("U07-2 无隐式保存（通道配置原样）", legacy_ch["base_url"] == "http://127.0.0.1:1/v1"
      and legacy_ch["model"] == "legacy-model", str(legacy_ch)[:100])
# 星号脱敏引用
s, d = api("POST", "/api/ai/models",
           {"draft": {"base_url": A_BASE, "api_key": "****", "model": ""},
            "channel_id": cid_a})
check("U07-3 脱敏星号不是密钥（引用已存密钥）", d.get("ok") is True, str(d)[:100])
# 测试通过绑定指纹：改模型后旧指纹失效
fp1 = aich.fingerprint({"base_url": B_BASE, "api_key": "keyB", "model": "model-b1"})
aich.record_test_result("aich_legacy", {"ok": True, "model": "legacy-model",
                                        "fingerprint": fp1, "at": db.now()})
ch = aich.get_channel("aich_legacy")
fp_now = aich.fingerprint({"base_url": ch["base_url"], "api_key": "legacy-key",
                           "model": ch["model"], "channel_id": "aich_legacy"})
check("U07-4 改配置后旧通过状态失效（指纹不同）", fp_now != fp1,
      f"{fp_now} vs {fp1}")

# ============================================================
print("== U08-并发：两通道并发调用不串用配置/凭据 ==")
# U08-11 删除过 B——重建（并发段需要两个独立端点）
s, d = api("POST", "/api/ai/channels", {"channel": {
    "name": "通道B", "base_url": B_BASE, "api_key": "keyB", "model": "model-b1",
    "enabled": True}})
cid_b = d["channel"]["id"]
results = {"A": [], "B": []}
def _call_a(n):
    r = aimod.test_ai(aich.resolve_cfg(cid_a))
    results["A"].append(r.get("reply"))
def _call_b(n):
    r = aimod.test_ai(aich.resolve_cfg(cid_b))
    results["B"].append(r.get("reply"))
ths = [threading.Thread(target=_call_a, args=(i,)) for i in range(5)] + \
      [threading.Thread(target=_call_b, args=(i,)) for i in range(5)]
[t.start() for t in ths]
[t.join() for t in ths]
check("U08-12 A 通道 5 次全部命中 A 替身", len(results["A"]) == 5
      and all(x == "链路正常" for x in results["A"]), str(results["A"]))
check("U08-13 B 通道 5 次全部命中 B 替身", len(results["B"]) == 5
      and all(x == "B 链路正常" for x in results["B"]), str(results["B"]))
check("U08-14 无串用（A 凭据不会打到 B，反之亦然）",
      StubHandler.hits["A_chat"] >= 5 and StubHandler.hits["B_chat"] >= 5,
      str(StubHandler.hits))

# ============================================================
print("== U08-任务覆盖 > 默认；停用通道给出明确状态 ==")
s, d = api("POST", f"/api/ai/channels/{cid_b}/task-override",
           {"task": "writing_recommend", "channel_id": cid_b})
cfg_rec = aich.resolve_cfg(task="writing_recommend")
check("U08-15 推荐任务走覆盖通道 B", cfg_rec.get("channel_id") == cid_b, str(cfg_rec)[:80])
cfg_def = aich.resolve_cfg(task="doc_ai")
check("U08-16 文档 AI 无覆盖 → 默认通道 A", cfg_def.get("channel_id") == cid_a,
      str(cfg_def)[:80])
# 停用默认通道 → resolve 报告不可用（不悄悄用错）
aich.upsert_channel({"id": cid_a, "name": "通道A", "base_url": A_BASE,
                     "api_key": "keyA", "model": "model-a1", "enabled": False})
cfg_off = aich.resolve_cfg()
check("U08-17 默认通道停用 → enabled=False 明确状态", cfg_off.get("enabled") is False,
      str(cfg_off)[:80])
aich.upsert_channel({"id": cid_a, "name": "通道A", "base_url": A_BASE,
                     "api_key": "keyA", "model": "model-a1", "enabled": True})
# 凭据不回显
s, d = api("GET", "/api/ai/channels")
keys = [c.get("api_key") for c in d.get("items", [])]
check("U08-18 列表不回显完整密钥（**** 前缀或空）",
      all((not k) or k.startswith("****") for k in keys), str(keys))

print(f"\n==== Round2 B/C 通道与草稿测试：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
