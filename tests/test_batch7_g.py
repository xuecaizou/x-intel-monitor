# -*- coding: utf-8 -*-
"""G 组：恢复后安全状态（2026-11-09 集中修复）——含真实启动恢复实例。

历史缺陷（基线 b4c720d 复核）：
  G-1 restore_to() 原样复制配置，恢复成功后 scheduler/outbound 仍为 true。
  G-2 旧备份的 Token/会话被悄悄恢复（可能含已吊销凭据）。
  G-3 摘要未区分"备份原件哈希"与"恢复时安全配置变更"。

验证原则：**在隔离目录实际启动恢复实例**，用运行时行为证明
（stdout 调度暂停声明 + 旧 Token 被拒 + 健康检查），不以源码包含
开关字符串代替运行验证。

运行：python tests/test_batch7_g.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from tests.testenv import TestEnv

env = TestEnv(forbid_network=False).start()  # 允许回环 HTTP（不外联）
import app.db as db  # noqa: E402

db.DATA_DIR = env.tmp
db._conn = None
db.init_db()

import app.auth as auth  # noqa: E402
import app.backup as backup  # noqa: E402

auth.init_auth_tables()

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(f"{name} [{detail}]")
        print(f"FAIL {name} [{detail}]")


PORT = 18127  # 隔离实例回环端口（固定高位，CI 冲突概率低）

# ============================================================
print("== G1 准备生产态环境（调度/外发全开 + 管理员 Token） ==")
(env.tmp / "runtime.json").write_text(json.dumps(
    {"server": {"host": "127.0.0.1", "port": PORT},
     "scheduler": {"enabled": True},
     "outbound": {"enable": True}}, ensure_ascii=False), encoding="utf-8")
db.init_db()
auth.create_admin("g-admin", "GAdminPass123")
raw_token = auth.create_token("g-token", "admin")
db.insert_tweet({"screen_name": "GSrc", "tweet_id": "g-1", "text": "恢复演练推文",
                 "url": "u", "published_at": db.now()})
check("G1-1 生产态配置就绪", json.loads((env.tmp / "runtime.json").read_text(encoding="utf-8"))
      ["scheduler"]["enabled"] is True, "")

# ============================================================
print("== G2 一致性备份（含 WAL 未检查点数据） ==")
bk = backup.create_backup(env.tmp, env.tmp / "backups", project_root=ROOT)
bk_dir = Path(bk["dir"])
check("G2-1 备份 integrity ok", bk["manifest"]["sqlite_integrity"] == "ok", "")
# WAL 场景：备份后再插一条（主库 WAL 未 checkpoint）→ 重新备份 → 应包含
db.insert_tweet({"screen_name": "GSrc", "tweet_id": "g-2", "text": "WAL 后插入",
                 "url": "u", "published_at": db.now()})
bk2 = backup.create_backup(env.tmp, env.tmp / "backups", project_root=ROOT)
with sqlite3.connect(str(Path(bk2["dir"]) / "xintel.db")) as c:
    n = c.execute("SELECT COUNT(*) FROM tweets WHERE screen_name='GSrc'").fetchone()[0]
check("G2-2 备份含最近提交数据（backup API 非 cp 主库）", n == 2, f"n={n}")

# ============================================================
print("== G3 恢复到隔离目录：安全关闭 + 凭据清除 + 哈希区分 ==")
restore_dir = env.tmp / "restored-instance"
summary = backup.restore_to(bk2_dir := Path(bk2["dir"]), restore_dir, project_root=ROOT)
check("G3-1 原件一致性 verify_original ok",
      (summary.get("verify_original") or {}).get("ok") is True,
      str(summary.get("verify_original"))[:100])
rt = json.loads((restore_dir / "runtime.json").read_text(encoding="utf-8"))
check("G3-2 恢复副本 scheduler.enabled=false", rt["scheduler"]["enabled"] is False,
      str(rt["scheduler"]))
check("G3-3 恢复副本 outbound.enable=false", rt["outbound"]["enable"] is False,
      str(rt["outbound"]))
rt_orig = json.loads((bk2_dir / "runtime.json").read_text(encoding="utf-8"))
check("G3-4 备份原件配置未被改动（原件 scheduler 仍 true）",
      rt_orig["scheduler"]["enabled"] is True, "")
sc = summary["safety_changes"][0]
check("G3-5 摘要区分原件哈希与恢复变更哈希",
      sc["backup_original_sha256"] != sc["restored_modified_sha256"],
      str(sc)[:120])
ar = summary["auth_reset"]
check("G3-6 旧 Token/会话清除数量入摘要", ar["tokens"] >= 1 and ar["sessions"] == 0,
      str(ar)[:100])
with sqlite3.connect(str(restore_dir / "xintel.db")) as c:
    tok = c.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0]
    users = c.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0]
check("G3-7 恢复库 Token 表为空（不复活已吊销凭据）", tok == 0, f"tokens={tok}")
check("G3-8 管理员账号保留（需 admin_init 重置密码后登录）", users == 1, f"users={users}")

# ============================================================
print("== G4 实际启动恢复实例：运行时证明无调度/外发/旧凭据 ==")
runner = env.tmp / "g_run_restored.py"
runner.write_text(
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, r'" + str(ROOT).replace("'", "\\'") + "')\n"
    "import app.config as cfg\n"
    "import app.db as db\n"
    "cfg.DATA_DIR = Path(r'" + str(restore_dir).replace("'", "\\'") + "')\n"
    "db.DATA_DIR = cfg.DATA_DIR\n"
    "assert cfg.DATA_DIR == Path(r'" + str(restore_dir).replace("'", "\\'") + "')\n"
    "import uvicorn\n"
    "from app.main import app\n"
    "uvicorn.run(app, host='127.0.0.1', port=" + str(PORT) + ", log_level='warning')\n",
    encoding="utf-8")

proc = subprocess.Popen([sys.executable, str(runner)], cwd=str(ROOT),
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        env={**os.environ, "XINTEL_DATA_DIR": str(restore_dir),
                             "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
booted = False
health = None
try:
    deadline = time.time() + 25
    while time.time() < deadline:
        if proc.poll() is not None:
            break  # 提前退出
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/healthz", timeout=2) as r:
                health = r.status
                booted = True
                break
        except urllib.error.HTTPError as e:
            health = e.code
            booted = True
            break
        except Exception:
            time.sleep(0.4)
    check("G4-1 恢复实例真实启动（healthz 可达）", booted and health == 200,
          f"booted={booted} health={health}")
    # 旧 Token 必须被拒绝：恢复前签发的 admin Token 不复活
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/tweets",
                                 headers={"Authorization": f"Bearer {raw_token}"})
    code = None
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    check("G4-2 旧备份签发的 Token 被运行实例拒绝（401）", code == 401, f"code={code}")
finally:
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except Exception:
        proc.kill()
        out, _ = proc.communicate()
boot_log = (out or b"").decode("utf-8", errors="ignore")
check("G4-3 启动日志声明调度已按配置暂停（运行时证据，非源码字符串）",
      "已按配置暂停" in boot_log,
      boot_log[-300:] if boot_log else "(empty)")
check("G4-4 启动日志无调度启动痕迹", "Starting" not in boot_log or "已按配置暂停" in boot_log,
      boot_log[-200:])

# ============================================================
print("== G5 恢复实例不产生调度/外发痕迹（运行后复检） ==")
with sqlite3.connect(str(restore_dir / "xintel.db")) as c:
    fetch_logs = c.execute("SELECT COUNT(*) FROM fetch_logs").fetchone()[0]
    push_logs = c.execute("SELECT COUNT(*) FROM push_logs").fetchone()[0]
check("G5-1 启动期间 fetch_logs 空（无调度抓取）", fetch_logs == 0, f"fetch_logs={fetch_logs}")
check("G5-2 启动期间 push_logs 空（无外发）", push_logs == 0, f"push_logs={push_logs}")

print(f"\n==== G 组恢复后安全状态：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
