# -*- coding: utf-8 -*-
"""CI 守卫：确认仓库未提交秘密/数据库/备份。"""
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PATTERNS = [
    (r"qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[a-f0-9]{8}-", "企微 webhook"),
    (r"sk-[A-Za-z0-9]{20,}", "OpenAI key"),
    (r"gho_[A-Za-z0-9]{20,}", "GitHub token"),
    (r"xintel_[A-Za-z0-9_-]{30,}", "X-Intel API Token"),
    (r'"api_key"\s*:\s*"[^"]{16,}"', "AI api_key"),
]
FORBIDDEN_FILES = (".db", ".db-wal", ".db-shm", "runtime.json", "admin_init.txt")

bad = []
files = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                       encoding="utf-8", errors="surrogateescape").stdout.splitlines()
for f in files:
    if f.startswith("data/"):
        bad.append(f"禁止提交 data/：{f}")
    if any(f.endswith(x) for x in FORBIDDEN_FILES):
        bad.append(f"禁止提交敏感文件：{f}")
for f in files:
    try:
        raw = open(f, "rb").read()
    except Exception:
        continue
    if bytes([0]) in raw[:4096]:
        continue   # 二进制文件跳过
    txt = raw.decode("utf-8", errors="ignore")
    for pat, name in PATTERNS:
        m = re.search(pat, txt)
        if m:
            bad.append(f"{f}: 发现 {name}（命中片段：{m.group(0)[:12]}…）")
if bad:
    print("发现禁止内容：", flush=True)
    for x in bad[:20]:
        print("  -", x, flush=True)
    sys.exit(1)
print(f"检查通过：{len(files)} 个文件无秘密/数据库", flush=True)
