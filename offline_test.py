# -*- coding: utf-8 -*-
"""离线测试入口（不依赖运行服务、不联网、不写生产）。

与 smoke_test.py 的区别：
  smoke_test.py        → 对运行实例做集成验收，G5 真实调用 AI、G6 模拟外发、G7 增删生产任务
  offline_test.py      → 全部在临时库+替身中执行，零外联、零生产写入，可用于 CI

运行：C:/Python314/python.exe offline_test.py
分组：tests/test_batch1/2/3 + test_isolation + test_batch4_*
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
SUITES = [
    "tests/test_isolation.py",
    "tests/test_batch1.py",
    "tests/test_batch2.py",
    "tests/test_batch3.py",
    "tests/test_t7_matrix.py",
    "tests/test_batch4_ai_task.py",
    "tests/test_batch4_security.py",
    "tests/test_batch4_auth.py",
    "tests/test_batch4_backup.py",
]

sys.stdout.reconfigure(encoding="utf-8")
failed = []
for s in SUITES:
    p = ROOT / s
    if not p.exists():
        print(f"SKIP {s}（不存在）")
        continue
    print(f"\n===== {s} =====")
    r = subprocess.run([PY, str(p)], cwd=str(ROOT))
    if r.returncode != 0:
        failed.append(s)
