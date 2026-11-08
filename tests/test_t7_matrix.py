# -*- coding: utf-8 -*-
"""阶段 A：T7 回退语义固定场景矩阵（隔离库）。

对五类终态分别观察：
  - 新代码：pushed 值 + 实例级投递状态
  - 旧代码候选查询（tweets WHERE pushed=0 AND ignored=0）会选中谁
输出实际漏发/重复/停止处理判定，供降级流程使用。

运行：C:/Python314/python.exe tests/test_t7_matrix.py
"""
import sys
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

PASS, FAIL = 0, []
ROWS = []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(name)
        print(f"FAIL {name} {detail}")


class StubCh:
    key = "wecom"
    display = "测试"
    caps = ("markdown",)

    def __init__(self, ok=True):
        self._ok = ok

    def configured(self, cc):
        return True

    def send(self, cc, title, md, image=None, html=None, url=None, **_):
        return self._ok, ("ok" if self._ok else "stub fail")


def mk(text):
    tid = db.insert_tweet({"screen_name": "T7Src", "tweet_id": f"t7-{text}",
                           "text": text, "url": "u", "published_at": db.now()})
    db.update_ai(tid, {"ai_status": "ok", "ai_severity": "high", "geo_country": "法国"})
    return tid


def old_candidates():
    """旧版本候选查询（ec92eba 的语义）：未推送且未忽略"""
    return [r["id"] for r in db.get_conn().execute(
        "SELECT id FROM tweets WHERE pushed=0 AND ignored=0 ORDER BY id DESC").fetchall()]


def scenario(name, state_a, state_b=None, sync=True):
    """构造两实例场景，观察 pushed 与旧查询命中"""
    iid_a, iid_b = f"ci_t7_{name}_a", f"ci_t7_{name}_b"
    t = mk(f"scenario-{name}")
    db.create_delivery(t, iid_a, "wecom", "", "{}", state_a)
    if state_b:
        db.create_delivery(t, iid_b, "wecom", "", "{}", state_b)
    if sync:
        db.sync_pushed_compat([t])
    pushed = db.get_tweet(t)["pushed"]
    hit = t in old_candidates()
    ROWS.append((name, state_a, state_b, pushed, hit))
    return t, pushed, hit


# 1) A 成功、B 待重试
t1, p1, h1 = scenario("A成功B待重试", "成功", "待发送")
check("M1 A成功B待重试 → pushed=1（旧代码不会重发）", p1 == 1, f"pushed={p1}")
check("M1b 旧查询不选中（B 的待重试在降级后丢失）", h1 is False, f"hit={h1}")

# 2) 仅模拟成功
t2, p2, h2 = scenario("仅模拟", "模拟成功")
check("M2 仅模拟 → pushed=1（旧代码不会补发真实）", p2 == 1, f"pushed={p2}")
check("M2b 旧查询不选中（真实资格在降级后丢失）", h2 is False)

# 3) 全部过滤
t3, p3, h3 = scenario("全部过滤", "已过滤", "已过滤")
check("M3 全部过滤 → pushed=1（旧代码不会重发）", p3 == 1, f"pushed={p3}")
check("M3b 旧查询不选中", h3 is False)

# 4) 永久失败
t4, p4, h4 = scenario("永久失败", "永久失败")
check("M4 永久失败 → pushed=1", p4 == 1, f"pushed={p4}")
check("M4b 旧查询不选中（失败不再重试）", h4 is False)

# 5) 结果不明
t5, p5, h5 = scenario("结果不明", "结果不明")
check("M5 结果不明 → pushed=1", p5 == 1, f"pushed={p5}")
check("M5b 旧查询不选中（不明不自动重发）", h5 is False)

# 6) 对照：仅待发送（未产生终态）
t6, p6, h6 = scenario("仅待发送", "待发送", sync=True)
check("M6 仅待发送 → pushed=0（旧代码可接管补发）", p6 == 0, f"pushed={p6}")
check("M6b 旧查询选中该条", h6 is True)

print("\n=== 场景矩阵 ===")
print(f"{'场景':<16}{'A状态':<10}{'B状态':<10}{'pushed':<8}{'旧查询命中'}")
for r in ROWS:
    print(f"{r[0]:<16}{r[1]:<10}{str(r[2] or '-'):<10}{r[3]:<8}{r[4]}")

print("""
=== 降级结论（实测） ===
- 旧版本以 tweets.pushed 为唯一门控：
  · 任一终态（成功/模拟/永久失败/结果不明/已过滤）→ pushed=1 → 旧代码**不会重发**；
  · 仅"待发送"（尚无终态）→ pushed=0 → 旧代码会接管补发。
- 因此降级后的实际行为：
  · A 成功、B 待重试：B **丢失**（不会重发）——这是降级损失，不是无损；
  · 仅模拟成功：真实资格 **丢失**；
  · 永久失败/结果不明：**停止处理**（符合预期，但旧代码无"人工重试"入口）；
  · 全部过滤：行为一致（不应重发）。
- 结论修正：此前"B 会被旧代码重发"的说法错误——实际是不重发（丢失）。
  降级前必须导出未完成/不明任务，按 §降级流程人工处理。
""")

print(f"==== T7 矩阵：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    sys.exit(1)
