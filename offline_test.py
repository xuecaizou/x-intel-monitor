# -*- coding: utf-8 -*-
"""离线测试入口（不依赖运行服务、不联网、不写生产）。

与 smoke_test.py 的区别：
  smoke_test.py        → 对运行实例做集成验收，G5 真实调用 AI、G6 模拟外发、G7 增删生产任务
  offline_test.py      → 全部在临时库+替身中执行，零外联、零生产写入，可用于 CI

运行：python offline_test.py
分组：tests/test_batch1/2/3 + test_isolation + test_batch4_* + test_entry_exit
退出码：任一必需套件失败/缺失/异常退出 → 整体非零退出（套件级汇总见末尾）。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
SUITES = [
    "tests/test_isolation.py",
    "tests/test_entry_exit.py",
    "tests/test_batch7_b.py",
    "tests/test_batch7_c.py",
    "tests/test_batch7_d.py",
    "tests/test_batch7_e.py",
    "tests/test_batch7_f.py",
    "tests/test_batch1.py",
    "tests/test_batch2.py",
    "tests/test_batch3.py",
    "tests/test_t7_matrix.py",
    "tests/test_batch4_ai_task.py",
    "tests/test_batch4_security.py",
    "tests/test_batch4_auth.py",
    "tests/test_batch4_backup.py",
    "tests/test_batch5.py",
    "tests/test_batch5_restore.py",
    "tests/test_batch6_events.py",
]


def run_suites(suites, root, py):
    """逐套件运行子进程，返回 (失败列表, 结果明细列表)。

    缺失、无法启动、退出码非零一律视为失败，不静默跳过。
    """
    failed = []
    detail = []
    for s in suites:
        p = root / s
        if not p.exists():
            print(f"FAIL {s}（套件缺失——必需套件不得静默跳过）")
            failed.append(s)
            detail.append((s, "缺失"))
            continue
        print(f"\n===== {s} =====")
        try:
            r = subprocess.run([py, str(p)], cwd=str(root))
        except OSError as e:
            print(f"FAIL {s}（无法启动解释器：{e}）")
            failed.append(s)
            detail.append((s, "启动失败"))
            continue
        if r.returncode != 0:
            print(f"FAIL {s}（退出码 {r.returncode}）")
            failed.append(s)
            detail.append((s, f"exit {r.returncode}"))
        else:
            detail.append((s, "ok"))
    return failed, detail


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass  # 非常规控制台（重定向/无 reconfigure 能力）不阻塞测试
    failed, detail = run_suites(SUITES, ROOT, PY)
    print("\n" + "=" * 60)
    print("套件级汇总：")
    for s, st in detail:
        print(f"  [{'PASS' if st == 'ok' else 'FAIL'}] {s}" + ("" if st == "ok" else f"（{st}）"))
    total = len(detail)
    print(f"\n共 {total} 个套件：{total - len(failed)} 通过 / {len(failed)} 失败")
    if failed:
        print(f"失败套件：{', '.join(failed)}")
        return 1
    print("全部套件通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
