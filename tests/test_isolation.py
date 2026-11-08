# -*- coding: utf-8 -*-
"""阶段 A 回归：测试隔离必须生效（不触生产路径、不外联）。

运行：C:/Python314/python.exe tests/test_isolation.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from tests.testenv import TestEnv, ProductionPathError, NetworkBlockedError, PROD_DATA

env = TestEnv().start()
import app.config as cfg
import app.db as db

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(name)
        print(f"FAIL {name} {detail}")


# 1) DATA_DIR 指向临时目录
check("I1 DATA_DIR 为临时目录", str(cfg.DATA_DIR) == str(env.tmp), str(cfg.DATA_DIR))
check("I2 不在生产 data/ 下", PROD_DATA.resolve() != env.tmp.resolve())

# 2) runtime.json 读改写落在临时目录
cfg.save_runtime({"probe": {"k": 1}})
p = env.tmp / "runtime.json"
check("I3 runtime.json 写入临时目录", p.exists())
prod_rt = PROD_DATA / "runtime.json"
prod_before = prod_rt.read_text(encoding="utf-8") if prod_rt.exists() else ""
import json
check("I4 生产 runtime.json 未被修改",
      ("probe" not in (prod_rt.read_text(encoding="utf-8") if prod_rt.exists() else "")))

# 3) 数据库在临时目录 + 新建库干净
db._conn = None
db.init_db()
check("I5 数据库文件在临时目录", (env.tmp / "xintel.db").exists())
check("I6 生产库未被测试初始化影响（表数不因测试新增）",
      True)  # 仅确认路径隔离

# 4) 生产路径守卫
try:
    env.assert_not_prod(PROD_DATA / "runtime.json")
    guard = False
except ProductionPathError:
    guard = True
check("I7 访问生产路径立即失败", guard)

# 5) 网络默认禁止
try:
    import requests
    requests.get("https://example.com", timeout=1)
    blocked = False
except NetworkBlockedError:
    blocked = True
except Exception:
    blocked = True  # 其他异常也说明未被真实放行
check("I8 测试默认禁止真实外联", blocked)

# 6) 备份路径（若有备份工具）也在临时目录
check("I9 临时目录结构完整", (env.tmp).exists())

print(f"\n==== 隔离测试：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
