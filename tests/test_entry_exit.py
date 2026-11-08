# -*- coding: utf-8 -*-
"""入口失败传递自测（缺陷 A 反例固化，端到端）。

历史缺陷：offline_test.py 收集 failed 列表但从未据此退出非零——
12 个套件全部退出 1，入口仍退出 0；CI 双平台 1 FAIL 仍标 success（run 42482898535）。

本测试在临时目录构造受控假套件，对入口做端到端验证：
  E1 全部通过 → 入口退出 0，输出含套件级汇总；
  E2 任一套件退出 1 → 入口必须退出非零；
  E3 全部套件退出 1 → 入口必须退出非零（原始反例）；
  E4 必需套件缺失 → 入口必须退出非零（不得静默跳过）。
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import offline_test as entry  # noqa: E402

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(f"{name}  [{detail}]")
        print(f"FAIL {name}  [{detail}]")


def make_fake_repo(tmp, fail_list=(), missing_list=()):
    """构造假离线入口仓库：offline_test.py 原样复制，套件按参数生成 exit 0/1 或缺失。"""
    (tmp / "tests").mkdir()
    src = (ROOT / "offline_test.py").read_text(encoding="utf-8")
    (tmp / "offline_test.py").write_text(src, encoding="utf-8")
    for s in entry.SUITES:
        if s in missing_list:
            continue
        body = ("import sys\nprint('fake suite')\nsys.exit(1)\n" if s in fail_list
                else "import sys\nprint('fake suite')\nsys.exit(0)\n")
        (tmp / s).write_text(body, encoding="utf-8")


def run_entry(tmp):
    return subprocess.run([sys.executable, str(tmp / "offline_test.py")],
                          cwd=str(tmp), capture_output=True, text=True,
                          encoding="utf-8", errors="ignore", timeout=300)


def main():
    # E1 全部通过 → 退出 0
    tmp = Path(tempfile.mkdtemp(prefix="entry-ok-"))
    try:
        make_fake_repo(tmp)
        r = run_entry(tmp)
        check("E1 全部套件通过 → 入口退出 0", r.returncode == 0, f"exit={r.returncode}")
        check("E1b 输出含套件级汇总", "套件级汇总" in (r.stdout or ""), "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # E2 单套件失败 → 退出非零
    tmp = Path(tempfile.mkdtemp(prefix="entry-fail1-"))
    try:
        make_fake_repo(tmp, fail_list=(entry.SUITES[0],))
        r = run_entry(tmp)
        check("E2 单套件退出 1 → 入口必须退出非零", r.returncode != 0, f"exit={r.returncode}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # E3 全部套件退出 1 → 退出非零（原始反例）
    tmp = Path(tempfile.mkdtemp(prefix="entry-allfail-"))
    try:
        make_fake_repo(tmp, fail_list=tuple(entry.SUITES))
        r = run_entry(tmp)
        check("E3 全部套件退出 1 → 入口必须退出非零", r.returncode != 0, f"exit={r.returncode}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # E4 必需套件缺失 → 退出非零，不得静默跳过
    tmp = Path(tempfile.mkdtemp(prefix="entry-missing-"))
    try:
        make_fake_repo(tmp, missing_list=(entry.SUITES[-1],))
        r = run_entry(tmp)
        check("E4 必需套件缺失 → 入口必须退出非零", r.returncode != 0, f"exit={r.returncode}")
        check("E4b 缺失套件在汇总中显式标 FAIL", "FAIL" in (r.stdout or ""), "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n==== 入口失败传递自测：{PASS} PASS / {len(FAIL)} FAIL ====")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
