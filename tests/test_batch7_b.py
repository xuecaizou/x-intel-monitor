# -*- coding: utf-8 -*-
"""B 组：AI 写入 / 人工保护 / 配额停止（2026-11-09 集中修复）。

历史缺陷（基线 b4c720d 独立复核）：
  B-1 process_tweet() 直接 update_ai() 绕过版本保护；后台路径"先写入再检查冲突"。
  B-2 失败状态写回无保护，可覆盖期间新增的人工状态。
  B-3 429/额度耗尽后被通用异常吞掉，本轮继续烧钱调用（含翻译、批量、分类、补标）。
  B-4 统计把模型失败计成成功（process_ai_tasks done 计数）。

替身位置：requests.post（HTTP/模型适配边界）——_chat/_chat_call/analyze_tweet/
process_tweet/run_ai_stage/process_ai_tasks/batch_process/classify_batch 全部真实。

运行：python tests/test_batch7_b.py
"""
import json
import sys
import threading
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

import app.ai as ai  # noqa: E402
import app.engine as engine  # noqa: E402

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(f"{name} [{detail}]")
        print(f"FAIL {name} [{detail}]")


def mk(text, **fields):
    tid = db.insert_tweet({"screen_name": "BSrc", "tweet_id": f"b-{text[:20]}-{db.now()}",
                           "text": text, "url": "u", "published_at": db.now()})
    if fields:
        db.update_ai(tid, fields)
    return tid


GOOD_JSON = json.dumps({
    "threat_type": "勒索软件", "actors": ["EvilCorp"], "organizations": ["ACME Corp"],
    "industry": "医疗", "country": "美国", "province": "", "city": "",
    "iocs": ["1.2.3.4"], "tags": ["RAT（远程访问木马）"], "severity": "high",
    "assessment": "测试研判结论", "recommendations": ["隔离主机"],
    "confidence": 90, "intel_category": "预警情报"}, ensure_ascii=False)


class _Resp:
    def __init__(self, status=200, text=GOOD_JSON, payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is not None:
            return self._payload
        return {"choices": [{"message": {"content": self.text}, "finish_reason": "stop"}]}


class HTTPStub:
    """HTTP 边界替身：按脚本顺序返回响应；记录调用次数。"""

    def __init__(self, script):
        self.script = list(script)   # 每次消费一个 _Resp 或 异常实例
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, url, **kw):
        with self.lock:
            self.calls += 1
            idx = min(self.calls - 1, len(self.script) - 1)
        item = self.script[idx]
        if isinstance(item, Exception):
            raise item
        return item


def install_http(script, target=ai.requests):
    stub = HTTPStub(script)
    orig = target.post
    target.post = stub
    return stub, orig


ACFG = {"enabled": True, "api_key": "stub-key", "model": "stub-model",
        "base_url": "https://stub.local/v1",
        "auto_analyze": True, "auto_limit": 10, "max_tokens": 100, "proxy": ""}

# ============================================================
print("== B1 计算与提交职责分离：process_tweet 不再直接写库 ==")
tid1 = mk("b1-process-no-write", ai_status="", ai_retries=0)
stub, orig_post = install_http([_Resp(text=GOOD_JSON), _Resp(text=GOOD_JSON)])
try:
    t1 = db.get_tweet(tid1)
    fields, error, quota = ai.process_tweet(t1, ACFG)
    row = db.get_tweet(tid1)
    check("B1-1 process_tweet 调用后库内未被写入（提交职责归调用方）",
          row["ai_status"] == "" and (row["ai_assessment"] or "") == "",
          f"ai_status={row['ai_status']!r}")
    check("B1-2 返回待提交字段", fields.get("ai_status") == "ok" and not error, str(fields)[:60])
    check("B1-3 无配额标志", quota is False, str(quota))
finally:
    ai.requests.post = orig_post

# ============================================================
print("== B2 权威提交：迟到结果不覆盖人工修改 ==")
tid2 = mk("b2-manual-race", ai_status="ok")
db.update_ai(tid2, {"ai_modified": 1, "ai_assessment": "人工修改结论"})
t2 = db.get_tweet(tid2)
base_v0 = int(t2["analysis_version"] or 0)
db.bump_analysis_version(tid2, bump_modified=True)  # 人工修订推进版本
applied, reason, cur = db.apply_ai_result(tid2, {"ai_assessment": "AI 迟到结果",
                                                 "ai_status": "ok"}, base_v0)
row = db.get_tweet(tid2)
check("B2-1 版本已变 → 拒绝应用", applied is False and reason in
      ("manual_modified", "version_changed"), f"{applied}/{reason}")
check("B2-2 人工值保留", row["ai_assessment"] == "人工修改结论", str(row["ai_assessment"]))
conf = db.list_conflicts(3)
check("B2-3 冲突留审计", any(c["tweet_id"] == tid2 for c in conf), str(conf)[:80])

# ============================================================
print("== B3 失败状态写回保护（update_ai_guarded） ==")
tid3 = mk("b3-fail-writeback", ai_status="ok")
base3 = int(db.get_tweet(tid3)["analysis_version"] or 0)
db.bump_analysis_version(tid3, bump_modified=True)  # 期间被人工接管
ok3, reason3 = db.update_ai_guarded(tid3, {"ai_status": "error", "ai_error": "迟到失败"}, base3)
row3 = db.get_tweet(tid3)
check("B3-1 迟到失败不覆盖人工接管状态", ok3 is False and reason3 == "status_manual_protected",
      f"{ok3}/{reason3}")
check("B3-2 人工期间状态未被改写", row3["ai_status"] == "ok", str(row3["ai_status"]))
conf3 = db.list_conflicts(2)
check("B3-3 失败写回冲突也留审计", any(c["tweet_id"] == tid3 and
      c["reason"] == "status_manual_protected" for c in conf3), str(conf3)[:100])

tid3b = mk("b3b-guarded-ok", ai_status="")
base3b = int(db.get_tweet(tid3b)["analysis_version"] or 0)
ok3b, reason3b = db.update_ai_guarded(tid3b, {"ai_status": "error", "ai_error": "首败"}, base3b)
check("B3-4 版本未变且无人工保护 → 允许写回", ok3b is True and reason3b == "applied",
      f"{ok3b}/{reason3b}")
check("B3-5 状态写回不推进版本",
      int(db.get_tweet(tid3b)["analysis_version"] or 0) == base3b, "版本被意外推进")

# ============================================================
print("== B4 run_ai_stage：429 停止整轮模型调用，统计不记成功 ==")
# 清理候选干扰
db.get_conn().execute("UPDATE tweets SET ai_status='ok' WHERE ai_status=''")
db.get_conn().commit()
n_before = stub.calls if False else 0
tids4 = [mk(f"b4-429-{i}", ai_status="") for i in range(3)]
stub4, orig4 = install_http([_Resp(status=429, text="rate limit exceeded")])
try:
    news = [db.get_tweet(x) for x in tids4]
    done4 = engine.run_ai_stage(news, {"ai": dict(ACFG)})
finally:
    ai.requests.post = orig4
row4a = db.get_tweet(tids4[0])
check("B4-1 首条即 429 → ai_status=error 且带配额标注",
      row4a["ai_status"] == "error" and "AIQuotaError" in (row4a["ai_error"] or ""),
      f"{row4a['ai_status']}/{row4a['ai_error']}")
check("B4-2 整轮仅 1 次 HTTP 调用（后续条目不再烧钱）", stub4.calls == 1, f"calls={stub4.calls}")
check("B4-3 成功计数为 0（统计不记成功）", done4 == 0, f"done={done4}")
row4b = db.get_tweet(tids4[2])
check("B4-4 第三条未被处理（保持未研判，等下轮配额恢复）",
      row4b["ai_status"] == "" and int(row4b["ai_retries"] or 0) == 0,
      f"{row4b['ai_status']}/{row4b['ai_retries']}")

# ============================================================
print("== B5 run_ai_stage：人工修改与迟到结果竞争（成功路径也走权威提交） ==")
db.get_conn().execute("UPDATE tweets SET ai_status='ok' WHERE ai_status=''")
db.get_conn().commit()
tid5 = mk("b5-late-success", ai_status="")
# 模拟"模型调用期间"的人工修订：先取快照（引擎视角，ai_modified=0），
# 在 HTTP 替身返回前人工推进版本——提交时才暴露冲突
t5 = db.get_tweet(tid5)
db.bump_analysis_version(tid5, bump_modified=True)
stub5, orig5 = install_http([_Resp(text=GOOD_JSON)])
try:
    done5 = engine.run_ai_stage([t5], {"ai": dict(ACFG)})
finally:
    ai.requests.post = orig5
row5 = db.get_tweet(tid5)
check("B5-1 人工保护版本下 AI 成功结果不覆盖",
      row5["ai_assessment"] is None or row5["ai_assessment"] != "测试研判结论",
      f"assess={row5['ai_assessment']!r}")
check("B5-2 冲突被审计", any(c["tweet_id"] == tid5 and c["reason"] in
      ("manual_modified", "version_changed", "manual_protected") for c in db.list_conflicts(5)),
      str(db.list_conflicts(3))[:120])

# ============================================================
print("== B6 process_ai_tasks：429 停止 + 任务标记配额失败 + 统计不虚记 ==")
db.get_conn().execute("UPDATE tweets SET ai_status='ok' WHERE ai_status=''")
db.get_conn().commit()
db.get_conn().execute("UPDATE ai_tasks SET status='已取消' WHERE status IN ('待处理','处理中')")
db.get_conn().commit()
tids6 = [mk(f"b6-task-{i}", ai_status="") for i in range(3)]
task6 = db.create_ai_task("analyze_backlog", {"limit": 3}, total=0)
stub6, orig6 = install_http([_Resp(status=429, text="quota exhausted")])
try:
    note6 = engine.process_ai_tasks({"ai": dict(ACFG)})
finally:
    ai.requests.post = orig6
row_task = db.get_conn().execute("SELECT * FROM ai_tasks WHERE id=?", (task6,)).fetchone()
check("B6-1 任务状态标注配额失败", "配额" in (row_task["status"] or "") or
      "配额" in (row_task["note"] or ""), f"{row_task['status']}/{row_task['note']}")
check("B6-2 后台路径也仅 1 次 HTTP 调用", stub6.calls == 1, f"calls={stub6.calls}")
check("B6-3 done=0（失败不计成功）", int(row_task["done"] or 0) == 0,
      f"done={row_task['done']}")
check("B6-4 进度说明含剩余未处理", "剩余" in (note6 or ""), str(note6))

# ============================================================
print("== B7 batch_process：429 停止本轮且 quota_stopped 计数 ==")
db.get_conn().execute("UPDATE tweets SET ai_status='ok' WHERE ai_status=''")
db.get_conn().commit()
tids7 = [mk(f"b7-batch-{i}", ai_status="") for i in range(4)]
stub7, orig7 = install_http([_Resp(status=429, text="rate limit")])
try:
    r7 = ai.batch_process(tids7, "analyze", ACFG)
finally:
    ai.requests.post = orig7
check("B7-1 返回 quota_stopped", r7.get("quota_stopped") == 4, str(r7))
check("B7-2 仅 1 次调用（不逐条烧钱）", stub7.calls == 1, f"calls={stub7.calls}")
check("B7-3 done=0", r7["done"] == 0, str(r7["done"]))

# ============================================================
print("== B8 classify_batch：权威提交（人工分类不被覆盖）+ 429 停止 ==")
tid8 = mk("b8-classify-manual", ai_status="ok")
db.update_ai(tid8, {"ai_modified": 1, "category": "人工指定分类"})
tid8b = mk("b8-classify-auto", ai_status="ok")
stub8, orig8 = install_http([_Resp(text=json.dumps({"results": [
    {"id": tid8, "category": "木马攻击线索"},
    {"id": tid8b, "category": "预警情报"}]}, ensure_ascii=False))])
try:
    r8 = ai.classify_batch([db.get_tweet(tid8), db.get_tweet(tid8b)], ACFG)
finally:
    ai.requests.post = orig8
check("B8-1 人工分类不被 AI 批量覆盖",
      db.get_tweet(tid8)["category"] == "人工指定分类", str(db.get_tweet(tid8)["category"]))
check("B8-2 未被人工接管的条目正常写入",
      db.get_tweet(tid8b)["category"] == "预警情报", str(db.get_tweet(tid8b)["category"]))
check("B8-3 done 计数正确（原实现恒 0）", r8["done"] == 1, str(r8))

# 429 停止：第一组就 429
tid8c = mk("b8-classify-429", ai_status="ok")
stub8b, orig8b = install_http([_Resp(status=429, text="rate limit")])
try:
    r8b = ai.classify_batch([db.get_tweet(tid8c), db.get_tweet(tid8b)], ACFG)
finally:
    ai.requests.post = orig8b
check("B8-4 分类 429 → quota_stopped 含剩余条目", r8b.get("quota_stopped") == 2, str(r8b))
check("B8-5 分类 429 仅 1 次调用", stub8b.calls == 1, f"calls={stub8b.calls}")

# ============================================================
print("== B9 模型成功但落库失败：一次失败计一次，不重复调用收费模型 ==")
db.get_conn().execute("UPDATE tweets SET ai_status='ok' WHERE ai_status=''")
db.get_conn().commit()
db.get_conn().execute("UPDATE ai_tasks SET status='已取消' WHERE status IN ('待处理','处理中')")
db.get_conn().commit()
tid9 = mk("b9-dbfail", ai_status="")
task9 = db.create_ai_task("analyze_backlog", {"limit": 1}, total=0)
stub9, orig9 = install_http([_Resp(text=GOOD_JSON)])
orig_apply = db.apply_ai_result
_apply_calls = {"n": 0}

def _flaky_apply(tid_, fields, base, source="ai"):
    _apply_calls["n"] += 1
    raise RuntimeError("模拟落库失败")

db.apply_ai_result = _flaky_apply
try:
    engine.process_ai_tasks({"ai": dict(ACFG)})
finally:
    db.apply_ai_result = orig_apply
    ai.requests.post = orig9
check("B9-1 模型仅研判+翻译各一次（落库异常不引发重复调用，若重复会是 4 次）",
      stub9.calls == 2, f"calls={stub9.calls}")
check("B9-2 落库失败尝试 1 次", _apply_calls["n"] == 1, str(_apply_calls))
row9 = db.get_conn().execute("SELECT * FROM ai_tasks WHERE id=?", (task9,)).fetchone()
check("B9-3 任务标记失败而非卡处理中", row9["status"] == "失败", row9["status"])

# ============================================================
print("== B10 AIQuotaError 不被通用异常吞掉（分类正确传递到边界） ==")
stub10, orig10 = install_http([_Resp(status=429, text="too many requests")])
try:
    try:
        ai.analyze_tweet({"id": 1, "screen_name": "X", "text": "hello"}, ACFG)
        raised = "none"
    except ai.AIQuotaError:
        raised = "quota"
    except Exception as e:
        raised = type(e).__name__
finally:
    ai.requests.post = orig10
check("B10-1 429 → AIQuotaError（不被 RuntimeError 覆盖）", raised == "quota", raised)

print(f"\n==== B 组 AI写入/人工保护/配额停止：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
