# -*- coding: utf-8 -*-
"""H 组：安全辅助函数接入实际业务（2026-11-09 集中修复）。

历史缺陷（基线 b4c720d 复核）：
  H-1 来源测试 test_source 裸走 _http_get（无出站校验）——可当 SSRF 探针。
  H-2 RSS 抓取/媒体代理走默认自动重定向+整包读取的裸 requests——
      上游 302 到内网会被静默跟随、响应无大小上限。
  H-3 报告交付 deliver 接受任意绝对路径（agent payload 可读任意文件）。
  H-4 企微附件 webhook 无出站校验。

验证原则：通过**业务入口**验证拒绝行为（不直接单测辅助函数了事）；
不访问真实云元数据、不扫描外部网络（全部替身在 requests 边界）。

运行：python tests/test_batch7_h.py
"""
import io
import json
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

import app.safefetch as sf  # noqa: E402
import app.rssfetch as rssfetch  # noqa: E402
import app.reports as reports  # noqa: E402
import app.wecom_media as wm  # noqa: E402
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


class HttpCounter:
    """requests 边界替身 + 调用计数（业务入口拒绝时计数必须为 0）"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append(str(url)[:80])
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        return item


class Resp:
    def __init__(self, status=200, content=b"", headers=None, chunks=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self._chunks = chunks

    def iter_content(self, chunk_size=65536):
        if self._chunks is not None:
            yield from self._chunks
        else:
            yield self.content

    def json(self):
        return json.loads(self.content.decode("utf-8"))


# 选择性放行替身：仅放行测试假主机（跳过 DNS），其余交还真实校验——
# 保证"逐跳校验""用途隔离"等真实逻辑仍被验证
from urllib.parse import urlparse as _urlparse  # noqa: E402

_real_validate = sf.validate_url
_FAKE_HOSTS = {"stub.local", "pbs.twimg.com"}


def _selective_validate(url, **kw):
    host = (_urlparse(url).hostname or "").lower()
    if host in _FAKE_HOSTS:
        return url
    return _real_validate(url, **kw)

# ============================================================
print("== H1 来源测试入口：私网 URL 被业务入口拒绝（零 HTTP 调用） ==")
hc = HttpCounter([Resp()])
_orig_get = requests_mod.get
requests_mod.get = hc
try:
    r1 = rssfetch.test_source("http://127.0.0.1:8080/feed.xml")
    check("H1-1 业务入口拒绝回环地址", r1["ok"] is False and "URL 拒绝" in r1["error"],
          str(r1))
    check("H1-2 拒绝路径零 HTTP 调用", len(hc.calls) == 0, str(hc.calls))
    r1b = rssfetch.test_source("http://169.254.169.254/latest/meta-data/")
    check("H1-3 云元数据地址被拒绝（不真实访问）",
          r1b["ok"] is False and "URL 拒绝" in r1b["error"], str(r1b))
finally:
    requests_mod.get = _orig_get

# ============================================================
print("== H2 RSS 抓取入口：私网 URL 拒绝 ==")
hc2 = HttpCounter([Resp()])
requests_mod.get = hc2
try:
    new, trace, err = rssfetch.fetch_source(
        {"id": 1, "name": "H2", "url": "http://localhost/rss"}, {"x": {}, "network": {}})
    check("H2-1 抓取入口拒绝回环", err != "" and "URL 拒绝" in err, f"{err}")
    check("H2-2 零 HTTP 调用", len(hc2.calls) == 0, str(hc2.calls))
finally:
    requests_mod.get = _orig_get

# ============================================================
print("== H3 受控抓取：重定向逐跳校验 + 大小上限 ==")
# 302 → 内网：逐跳校验必须拦截（历史：裸 requests 默认自动跟随）
hc3 = HttpCounter([Resp(status=302, headers={"Location": "http://127.0.0.1/secret"}),
                   Resp(content=b"secret")])
requests_mod.get = hc3
sf.validate_url = _selective_validate  # 放行 stub.local（免 DNS），内网目标仍走真实校验
try:
    try:
        sf.fetch_limited("http://stub.local/start", purpose="rss")
        redirected = False
    except sf.UnsafeUrlError as ue:
        redirected = True
    check("H3-1 重定向到内网被逐跳校验拦截", redirected, "")
    check("H3-2 拦截时未发起第二跳请求", len(hc3.calls) == 1, str(hc3.calls))
finally:
    requests_mod.get = _orig_get

# 大小上限：>8MB 拒绝
big = Resp(chunks=[b"A" * (1024 * 1024) for _ in range(9)])  # 9MB
hc3b = HttpCounter([big])
requests_mod.get = hc3b
try:
    try:
        sf.fetch_limited("http://stub.local/big", purpose="rss")
        too_big = False
    except sf.UnsafeUrlError as ue:
        too_big = "上限" in str(ue)
    check("H3-3 响应超 8MB 被拒绝", too_big, "")
finally:
    requests_mod.get = _orig_get

# ============================================================
print("== H4 用途授权隔离：AI 网关白名单不扩大到 RSS/媒体 ==")
import app.config as cfgmod  # noqa: E402
_real_load = cfgmod.load
cfgmod.load = lambda: {"outbound": {"allow_private": {"ai_gateway": ["127.0.0.1:443"]}}}
try:
    try:
        sf.validate_url("http://127.0.0.1/x", purpose="rss")
        leaked = True
    except sf.UnsafeUrlError:
        leaked = False
    check("H4-1 ai_gateway 白名单不放行 rss 用途", leaked is False, "")
    ok_ai = False
    try:
        sf.validate_url("https://127.0.0.1/v1", purpose="ai_gateway")
        ok_ai = True
    except sf.UnsafeUrlError:
        ok_ai = False
    check("H4-2 ai_gateway 用途按显式授权放行（合法内网网关）", ok_ai, "")
finally:
    cfgmod.load = _real_load

# ============================================================
print("== H5 媒体代理入口：白名单外拒绝 + 重定向内网拒绝 ==")
from fastapi.testclient import TestClient  # noqa: E402
import app.auth as auth  # noqa: E402
from app.main import app  # noqa: E402

auth.init_auth_tables()
auth.create_admin("admin", "AdminPass123")
H = {"Authorization": f"Bearer {auth.create_token('h-admin', 'read')}"}
client = TestClient(app)

r5 = client.get("/api/media", params={"url": "https://evil.example.com/a.jpg"}, headers=H)
check("H5-1 白名单外主机 → 400", r5.status_code == 400, f"{r5.status_code}")
r5b = client.get("/api/media", params={"url": "http://127.0.0.1:8080/a.jpg"}, headers=H)
check("H5-2 私网地址 → 400（域名白名单+IP 校验双重防护）", r5b.status_code == 400,
      f"{r5b.status_code}")

# 白名单内主机 302 → 内网：受控抓取逐跳拦截
hc5 = HttpCounter([Resp(status=302, headers={"Location": "http://127.0.0.1:9/s"}),
                   Resp(content=b"x")])
requests_mod.get = hc5
sf.validate_url = _selective_validate
try:
    r5c = client.get("/api/media", params={"url": "https://pbs.twimg.com/media/x.jpg"},
                     headers=H)
    check("H5-3 twimg 302 到内网 → 拒绝（不跟随）", r5c.status_code in (400, 502),
          f"{r5c.status_code}/{str(r5c.text)[:60]}")
    check("H5-4 第二跳内网请求未发出", len(hc5.calls) == 1, str(hc5.calls))
finally:
    requests_mod.get = _orig_get

# ============================================================
print("== H6 报告交付入口：受控目录（拒绝绝对路径/穿越） ==")
cfg_backup_load = reports.load
reports.load = lambda: {"channels": {}, "report": {}}
try:
    tid6 = db.insert_tweet({"screen_name": "H6", "tweet_id": "h6-1", "text": "报告源",
                            "url": "u", "published_at": db.now()})
    tid6r = reports.create_task("H6 任务", [""], "速报", "", [tid6], 0)["id"]
    db.get_conn().execute("UPDATE report_tasks SET status='待人工' WHERE id=?", (tid6r,))
    db.get_conn().commit()
    # 绝对路径（历史缺陷：任意绝对目录被接受）
    d6 = reports.deliver(tid6r, {"deliver_dir": r"C:\\Windows\\Temp", "docx": "evil.docx"})
    check("H6-1 绝对路径 → 待人工 + 拒绝说明",
          d6["status"] == "待人工" and "路径不合法" in d6["gate_summary"], str(d6)[:120])
    # 穿越
    d6b = reports.deliver(tid6r, {"deliver_dir": "../outside", "docx": "x.docx"})
    check("H6-2 相对路径穿越 → 拒绝", d6b["status"] == "待人工"
          and "路径不合法" in d6b["gate_summary"], str(d6b)[:120])
    # 受控相对路径 + 文件存在 → 正常走门禁（替身门禁 exit 0 → 已交付待推）
    rel = "h6-deliver"
    d6d = env.tmp / "report_tasks" / rel
    d6d.mkdir(parents=True, exist_ok=True)
    (d6d / "ok.docx").write_text("doc")
    gate = env.tmp / "gate_ok.py"
    gate.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    reports.load = lambda: {"channels": {}, "report": {"gate_script": str(gate)}}
    d6c = reports.deliver(tid6r, {"deliver_dir": rel, "docx": "ok.docx",
                                  "word_count": 100, "ledger_total": 1, "ledger_pass": 1})
    check("H6-3 受控相对路径 → 正常交付流程", d6c["status"] == "已交付待推", str(d6c)[:120])
finally:
    reports.load = cfg_backup_load

# ============================================================
print("== H7 企微附件 webhook 出站校验 ==")
hc7 = HttpCounter([Resp(content=json.dumps({"errcode": 0, "media_id": "m1"}))])
requests_mod.get = hc7
requests_mod.post = hc7
try:
    f7 = env.tmp / "h7.docx"
    f7.write_bytes(b"PK\x03\x04fake")
    r7 = wm.push_file("http://127.0.0.1:9000/cgi-bin/webhook/upload_media?key=x", str(f7))
    check("H7-1 私网 webhook → 业务拒绝（errcode 90002）", r7.get("errcode") == 90002,
          str(r7)[:100])
    check("H7-2 拒绝路径零 HTTP 调用", len(hc7.calls) == 0, str(hc7.calls))
    r7b = wm.push_file("ftp://qyapi.example.com/hook", str(f7))
    check("H7-3 非 https webhook → 拒绝", r7b.get("errcode") == 90002, str(r7b)[:80])
finally:
    requests_mod.get = _orig_get
    requests_mod.post = requests_mod.post  # post 恢复交给下段

# ============================================================
print("== H8 代理边界：低信任用途配代理须显式受控出口 ==")
cfgmod.load = lambda: {"outbound": {"trusted_proxy": False}}
try:
    try:
        sf.check_proxy_trust("rss", "socks5://127.0.0.1:10808")
        allowed = True
    except sf.UnsafeUrlError:
        allowed = False
    check("H8-1 未声明 trusted_proxy → 代理路径拒绝", allowed is False, "")
    cfgmod.load = lambda: {"outbound": {"trusted_proxy": True}}
    try:
        sf.check_proxy_trust("rss", "socks5://127.0.0.1:10808")
        ok8 = True
    except sf.UnsafeUrlError:
        ok8 = False
    check("H8-2 显式受控出口 → 放行（代理侧策略由管理员负责，文档已注明局限）", ok8, "")
finally:
    cfgmod.load = _real_load
requests_mod.post = requests_mod.post

print(f"\n==== H 组安全辅助函数接入实际业务：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
