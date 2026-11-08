# -*- coding: utf-8 -*-
"""安全防护单元测试：SSRF / 路径越界 / 报告文件。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from tests.testenv import TestEnv

env = TestEnv(forbid_network=False).start()   # 需要真实 socket 解析以验证校验逻辑
import app.safefetch as sf  # noqa: E402

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(name)
        print(f"FAIL {name} {detail}")


def expect_block(label, fn):
    try:
        fn()
        check(label, False, "未拦截")
    except (sf.UnsafeUrlError, sf.UnsafePathError) as e:
        check(label, True)


print("== SSRF 目标校验 ==")
expect_block("回环 IPv4 拦截", lambda: sf.validate_url("http://127.0.0.1/x"))
expect_block("私网 IPv4 拦截", lambda: sf.validate_url("http://192.168.1.1/x"))
expect_block("IPv6 回环拦截", lambda: sf.validate_url("http://[::1]/x"))
expect_block("链路本地（云元数据）拦截",
             lambda: sf.validate_url("http://169.254.169.254/latest/meta-data"))
expect_block("file 协议拦截", lambda: sf.validate_url("file:///etc/passwd"))
expect_block("gopher 协议拦截", lambda: sf.validate_url("gopher://127.0.0.1:6379/"))
expect_block("IPv4 映射 IPv6 拦截", lambda: sf.validate_url("http://[::ffff:127.0.0.1]/x"))
# 管理员按用途白名单例外（第五批收敛：不再有全局 allow_private）
import app.config as _cfgmod  # noqa: E402
_real_load = _cfgmod.load
_cfgmod.load = lambda: {"outbound": {"allow_private": {
    "ai_gateway": ["192.168.1.53:20128"]}}}
try:
    sf.validate_url("http://192.168.1.53:20128/v1", purpose="ai_gateway")
    check("管理员白名单例外（内网 AI 网关）", True)
except sf.UnsafeUrlError as e:
    check("管理员白名单例外（内网 AI 网关）", False, str(e)[:60])
try:
    sf.validate_url("http://192.168.1.53:20128/v1", purpose="rss")
    check("同一地址在 RSS 用途仍被拒（例外不共享）", False, "未拦截")
except sf.UnsafeUrlError:
    check("同一地址在 RSS 用途仍被拒（例外不共享）", True)
_cfgmod.load = _real_load

print("\n== 路径越界 ==")
data_root = Path(env.tmp)
cases = [
    ("上跳穿越", "../../etc/passwd"),
    ("盘符", "C:\\Windows\\win.ini"),
    ("绝对路径", "/etc/passwd"),
    ("UNC", "\\\\server\\share\\x"),
    ("编码变体(..%2f)", "..%2f..%2fetc"),
]
for label, bad in cases:
    expect_block(f"路径拦截：{label}", lambda b=bad: sf.safe_join(data_root, b))
# 合法相对路径可解析
ok = sf.safe_join(data_root, "report_tasks", "task_1.json")
# 2026-11-09：改用解析后归属比较——Windows CI 上 tempfile 返回的原始路径与
# .resolve() 展开形式可能不同（8.3 短名/大小写），字符串前缀比较会假失败
# （CI run 42508927840 Windows job 实证；safe_join 本身用 resolved 相对比较，无此问题）
check("合法相对路径通过",
      Path(str(ok)).resolve().is_relative_to(Path(data_root).resolve()), str(ok))

print("\n== 报告文件（纯文件名约束） ==")
try:
    sf.safe_report_path("taskdir", "a.docx")
    check("合法交付文件解析", True)
except Exception as e:
    check("合法交付文件解析", False, str(e)[:60])
for bad in ("../evil.docx", "C:\\evil.docx", "sub/dir.docx"):
    try:
        sf.safe_report_path("taskdir", bad)
        check(f"文件名拦截：{bad[:16]}", False, "未拦截")
    except sf.UnsafePathError:
        check(f"文件名拦截：{bad[:16]}", True)

print("\n== 重定向逐跳校验（禁自动跟随） ==")


class FakeResp:
    def __init__(self, status, headers=None, body=b"ok"):
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    def iter_content(self, n):
        yield self._body


class FakeSession:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, url, **kw):
        assert kw.get("allow_redirects") is False, "必须禁用自动重定向"
        return self.mapping[url]


sess = FakeSession({
    "http://example.com/a": FakeResp(302, {"location": "http://127.0.0.1/evil"}),
})
try:
    sf.fetch_limited("http://example.com/a", session=sess)
    check("重定向到内网被拦截", False, "未拦截")
except sf.UnsafeUrlError as e:
    check("重定向到内网被拦截", True, str(e)[:50])

# 响应大小限制
big = FakeSession({"http://example.com/big": FakeResp(200, {}, b"x" * 200)})
try:
    sf.fetch_limited("http://example.com/big", max_bytes=100, session=big)
    check("响应大小限制", False, "未限制")
except sf.UnsafeUrlError:
    check("响应大小限制", True)

print(f"\n==== 安全防护测试：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
