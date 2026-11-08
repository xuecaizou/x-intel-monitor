# -*- coding: utf-8 -*-
"""CI 守卫：确认仓库未提交秘密/数据库/备份。"""
import re
import subprocess
import sys

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
                       encoding="utf-8", errors="ignore").stdout.splitlines()
for f in files:
    if f.startswith("data/"):
        bad.append(f"禁止提交 data/：{f}")
    if any(f.endswith(x) for x in FORBIDDEN_FILES):
        bad.append(f"禁止提交敏感文件：{f}")
for f in files:
    try:
        txt = open(f, encoding="utf-8", errors="ignore").read()
    except Exception:
        continue
    for pat, name in PATTERNS:
        if re.search(pat, txt):
            bad.append(f"{f}: 发现 {name}")
if bad:
    print("发现禁止内容：")
    for x in bad[:20]:
        print("  -", x)
    sys.exit(1)
print(f"检查通过：{len(files)} 个文件无秘密/数据库")
