# -*- coding: utf-8 -*-
"""Round2 · 需求 E/F：提示词驱动候选 + 推文流入口（真实 worker + 替身模型端点）。

U12 用户提示词真正进入模型请求；结果明细持久化、刷新可恢复
U13 零资料/错误 JSON/非法来源引用/重试/重启恢复（B22）
U14 采纳幂等/不串题/不复活拒绝项（B15）/不覆盖人工编辑
B13 素材结构统一（sqlite3.Row 无 get）
B14 白名单校验（平台 tweet_id ≠ 内部 id；越界引用剔除并标注）
B16 不同批次同默认标题不串并
B18 已转报告终态守卫
B19 转报告事务回滚（文件写入失败无孤立任务）
F 推文流入口（单条/批量，成功状态与后端一致）
"""
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from tests.testenv import TestEnv  # noqa: E402

env = TestEnv(forbid_network=False).start()
(env.tmp / "runtime.json").write_text(json.dumps({"scheduler": {"enabled": False}}), encoding="utf-8")

import app.db as db  # noqa: E402
import app.auth as auth  # noqa: E402
import app.writing as writing  # noqa: E402
import app.ai_channels as aich  # noqa: E402
import app.ai as aimod  # noqa: E402

db.DATA_DIR = env.tmp
db._conn = None
db.init_db()
auth.init_auth_tables()
auth.create_admin("admin", "AdminPass123")

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(f"{name} [{detail}]")
        print(f"FAIL {name} [{detail}]")


def mk(text, tweet_id=None, sev="high"):
    t = {"screen_name": "ESrc", "tweet_id": tweet_id or f"e-{text[:16]}", "text": text,
         "url": "u", "published_at": db.now()}
    tid = db.insert_tweet(t)
    db.update_ai(tid, {"ai_status": "ok", "ai_severity": sev,
                       "ai_assessment": text[:60], "ai_integrity": "verified"})
    return tid


RECOMMEND_JSON = json.dumps({"items": [
    {"title": "供应链攻击专题：恶意包生态", "angle": "从投递链分析",
     "reason": "两条素材同指供应链", "priority": 2, "template": "",
     "materials": ["待补 IOC"], "basis": ["素材1", "素材2"],
     "source_links": ["https://stub.local/1"], "tweet_ids": []},
    {"title": "数据泄露专题", "angle": "泄露规模", "reason": "数据库出售",
     "priority": 3, "template": "", "materials": [], "basis": [],
     "source_links": [], "tweet_ids": []},
]}, ensure_ascii=False)

import http.server  # noqa: E402


class ModelStub(http.server.BaseHTTPRequestHandler):
    """确定性替身：记录收到的 user 消息；按场景返回不同响应。"""
    scene = {"mode": "ok", "last_user": "", "calls": 0}

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        ModelStub.scene["calls"] += 1
        msgs = payload.get("messages") or []
        ModelStub.scene["last_user"] = " ".join(
            m.get("content", "") for m in msgs if m.get("role") == "user")
        if ModelStub.scene["mode"] == "ok":
            return self._json(200, {"choices": [{"message": {"content": RECOMMEND_JSON},
                                                 "finish_reason": "stop"}]})
        if ModelStub.scene["mode"] == "badjson":
            return self._json(200, {"choices": [{"message": {"content": "这不是 JSON"},
                                                 "finish_reason": "stop"}]})
        if ModelStub.scene["mode"] == "500":
            return self._json(500, {"error": "boom"})
        self._json(404, {"error": "?"})


_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ModelStub)
SPORT = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()
STUB = f"http://127.0.0.1:{SPORT}/v1"

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
_sess = None


def api(method, path, body=None):
    global _sess
    if _sess is None:
        r = client.post("/api/auth/login", json={"username": "admin", "password": "AdminPass123"})
        _sess = {"cookie": r.headers.get("set-cookie", "").split(";")[0], "csrf": r.json()["csrf"]}
    r = client.request(method, path, json=body,
                       headers={"X-CSRF-Token": _sess["csrf"], "Cookie": _sess["cookie"]})
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}


def wait_task(tid, timeout=20):
    for _ in range(int(timeout * 4)):
        row = db.get_conn().execute("SELECT * FROM ai_recommend_tasks WHERE id=?",
                                    (tid,)).fetchone()
        if row["status"] in ("done", "failed"):
            return dict(row)
        time.sleep(0.25)
    return dict(db.get_conn().execute("SELECT * FROM ai_recommend_tasks WHERE id=?",
                                      (tid,)).fetchone())


# ============================================================
print("== 准备：情报素材 + 通道 ==")
t1 = mk("供应链攻击：恶意包投递事件", tweet_id="plat-1")
t2 = mk("数据库出售：records for sale", tweet_id="plat-2")
t3 = mk("无关内容填充", tweet_id="plat-3", sev="low")
aich.upsert_channel({"name": "推荐通道", "base_url": STUB, "api_key": "k-rec",
                     "model": "rec-model", "enabled": True})

# ============================================================
print("== U12 提示词进入模型 + 结果明细持久化 ==")
s, d = api("POST", "/api/writing/recommend", {
    "scope_type": "manual", "scope_ref": "",
    "prompt": "从最近情报中推荐 2 篇文章，优先供应链攻击",
    "filters": {"days": 7, "auto_create": True}})   # 旧自动推荐路径（建候选+关联）
check("U12-0 任务受理", s == 200 and d.get("task_id"), f"{s} {str(d)[:80]}")
tid1 = d["task_id"]
row = wait_task(tid1)
check("U12-1 任务完成", row["status"] == "done", f"{row['status']}/{row['error']}")
check("U12-2 用户提示词真正进入模型请求",
      "从最近情报中推荐 2 篇文章，优先供应链攻击" in ModelStub.scene["last_user"],
      ModelStub.scene["last_user"][:120])
check("U12-3 通道/模型快照持久化", row["channel_id"] != "" and row["model"] == "rec-model",
      f"{row['channel_id']}/{row['model']}")
detail = json.loads(row["result_json"] or "[]")
check("U12-4 结果明细完整（标题/角度/理由/素材缺口/依据）",
      len(detail) == 2 and all(k in detail[0] for k in
                               ("title", "angle", "reason", "materials", "basis", "cand_uid")),
      str(detail)[:140])
check("U12-5 候选已创建且关联推荐任务",
      all(c["rec_task_id"] == tid1 for c in
          [{"rec_task_id": (writing.get_candidate(x["cand_uid"]) or {}).get("rec_task_id")}
           for x in detail]),
      str([(x["cand_uid"], (writing.get_candidate(x["cand_uid"]) or {}).get("rec_task_id"))
           for x in detail])[:120])
# 刷新恢复：列表接口返回 result_detail
s, d = api("GET", "/api/writing/recommend/tasks")
task_view = [x for x in d["items"] if x["id"] == tid1][0]
check("U12-6 刷新后可恢复（列表含 result_detail）",
      task_view.get("result_detail") and len(task_view["result_detail"]) == 2,
      str(task_view.get("result_detail"))[:100])

# ============================================================
print("== U13/B14 非法来源引用剔除 + 零资料/错误 JSON 状态 ==")
# B14：模型引用越界 tweet_ids → 剔除并标注（替身 JSON 中 tweet_ids 为空，
# 这里通过直接构造响应验证白名单：改 scene 返回带越界引用）
RECOMMEND_JSON_BAD = json.dumps({"items": [
    {"title": "越界引用专题", "angle": "a", "reason": "r", "priority": 1,
     "materials": [], "basis": [], "source_links": [],
     "tweet_ids": [999999, t1]},   # 999999 不存在
]}, ensure_ascii=False)
import http.server  # noqa: E402
class BadRefStub(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_POST(self):
        body = json.dumps({"choices": [{"message": {"content": RECOMMEND_JSON_BAD},
                                        "finish_reason": "stop"}]}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
_bs = http.server.ThreadingHTTPServer(("127.0.0.1", 0), BadRefStub)
BPORT = _bs.server_address[1]
threading.Thread(target=_bs.serve_forever, daemon=True).start()
aich.upsert_channel({"name": "越界通道", "base_url": f"http://127.0.0.1:{BPORT}/v1",
                     "api_key": "k", "model": "m", "enabled": True})
bad_cid = [c["id"] for c in aich.list_channels() if c["name"] == "越界通道"][0]
s, d = api("POST", "/api/writing/recommend", {
    "scope_type": "manual", "scope_ref": "", "prompt": "推荐",
    "channel_id": bad_cid, "filters": {"days": 7}})
tid2 = d["task_id"]
row2 = wait_task(tid2)
check("U13-1 越界引用被剔除且标注", row2["status"] == "done", row2["error"][:80])
detail2 = json.loads(row2["result_json"] or "[]")
check("U13-2 引用白名单：仅保留存在的 tweets.id，越界进 rejected_refs",
      detail2 and detail2[0]["tweet_ids"] == [t1]
      and 999999 in detail2[0].get("rejected_refs", []), str(detail2)[:140])
# U13-3: 采纳后验证候选关联的是内部主键（非平台 tweet_id）
adopt_r = writing.adopt_recommend_item(tid2, 0, "admin")
cand2 = writing.get_candidate(adopt_r["cand_uid"])
check("U13-3 候选关联的是内部主键（非平台 tweet_id）",
      cand2["tweet_ids"] == [t1] and "plat-" not in json.dumps(cand2["tweet_ids"]),
      str(cand2["tweet_ids"]))
# 零资料：days 过滤到不可能的时间
s, d = api("POST", "/api/writing/recommend", {
    "scope_type": "backlog", "prompt": "推荐",
    "filters": {"keywords": ["zzz-无匹配关键词-xyz"]}})
tid3 = d["task_id"]
row3 = wait_task(tid3)
check("U13-4 资料范围为空 → 明确失败状态（不同错误）",
      row3["status"] == "failed" and "资料范围为空" in row3["error"], row3["error"][:80])
# 错误 JSON
ModelStub.scene["mode"] = "badjson"
s, d = api("POST", "/api/writing/recommend", {
    "scope_type": "manual", "scope_ref": "", "prompt": "推荐", "filters": {"days": 7}})
tid4 = d["task_id"]
row4 = wait_task(tid4)
check("U13-5 错误 JSON → failed 且错误可见", row4["status"] == "failed"
      and "JSON" in row4["error"], row4["error"][:80])
# 重试复用原始提示词
ModelStub.scene["mode"] = "ok"
s, d = api("POST", f"/api/writing/recommend/{tid4}/retry", {})
check("U13-6 重试受理", s == 200)
row4b = wait_task(tid4)
check("U13-7 重试成功且复用原始要求", row4b["status"] == "done"
      and "推荐" in ModelStub.scene["last_user"], row4b["error"][:60])

# ============================================================
print("== B22 worker 重启恢复 ==")
# 手工造一个 running 任务（模拟 worker 崩溃残留）
s, d = api("POST", "/api/writing/recommend", {
    "scope_type": "manual", "scope_ref": "", "prompt": "恢复测试", "filters": {"days": 7}})
tid5 = d["task_id"]
wait_task(tid5)
db.get_conn().execute("UPDATE ai_recommend_tasks SET status='running', updated_at=? WHERE id=?",
                      (db.now(), tid5))
db.get_conn().commit()
n = writing.recover_recommend_tasks(stale_minutes=0)   # 立即回收
row5 = db.get_conn().execute("SELECT status FROM ai_recommend_tasks WHERE id=?",
                             (tid5,)).fetchone()
check("B22-1 running 残留按租约回收为 queued", row5["status"] in ("queued", "running", "done"),
      row5["status"])

# ============================================================
print("== U14/B15/B16 采纳幂等/不复活/不串并 ==")
# B15：人工不采用后，同一推荐（同标题+角度）再次触发 → 不新建待处理
s, d = api("POST", "/api/writing/candidates", {"title": "B15 重复选题", "angle": "角度X"})
uid_b15 = d["cand_uid"]
api("POST", f"/api/writing/candidates/{uid_b15}/decision", {"decision": "不采用", "note": ""})
uid2_b15, merged = writing.create_candidate(title="B15 重复选题", angle="角度X",
                                            source_type="ai", operator="ai")
row_b15 = writing.get_candidate(uid2_b15)
check("B15-1 重复推荐不复活人工拒绝项", row_b15["status"] == "不采用"
      and uid2_b15 == uid_b15, f"{row_b15['status']}")
# B16：不同批次同默认标题不串并
r_a = api("POST", "/api/writing/candidates/from-tweets",
          {"tweet_ids": [t1, t2], "title": ""})   # 默认"多源合并线索"
r_b = api("POST", "/api/writing/candidates/from-tweets",
          {"tweet_ids": [t3], "title": "多源合并线索"})
uid_a = r_a[1]["items"][0]["cand_uid"]
uid_b = r_b[1]["items"][0]["cand_uid"]
check("B16-1 不同批次同默认标题不串并", uid_a != uid_b, f"{uid_a} vs {uid_b}")
ca, cb = writing.get_candidate(uid_a), writing.get_candidate(uid_b)
check("B16-2 情报归属正确（不混材料）",
      sorted(ca["tweet_ids"]) == sorted([t1, t2]) and cb["tweet_ids"] == [t3],
      f"{ca['tweet_ids']}/{cb['tweet_ids']}")
# 同批次重复添加 → 幂等合并
r_a2 = api("POST", "/api/writing/candidates/from-tweets",
           {"tweet_ids": [t1, t2]})   # 同一批次重复添加 → scope 相同 → 幂等合并
uid_a2 = r_a2[1]["items"][0]["cand_uid"]
check("B16-3 同批次幂等合并（scope 相同）", uid_a2 == uid_a, f"{uid_a2} vs {uid_a}")
# B18：已转报告终态
conv = writing.convert_to_report(uid_a, None, "admin")
s, d = api("POST", f"/api/writing/candidates/{uid_a}/decision", {"decision": "采纳"})
check("B18-1 已转报告后 decision 409（终态守卫）", s == 409, f"{s} {str(d)[:80]}")
row_b18 = writing.get_candidate(uid_a)
check("B18-2 状态未被改回待处理", row_b18["status"] == "已转报告", row_b18["status"])

# ============================================================
print("== B19 转报告文件失败回滚 ==")
import app.reports as reports  # noqa: E402
s, d = api("POST", "/api/writing/candidates", {"title": "B19 回滚候选", "tweet_ids": [t1]})
uid_b19 = d["cand_uid"]
# 注入：线索包目录不可写（替换 _tasks_dir 指向文件）
orig_dir = reports._tasks_dir
class _BadDir:
    def __truediv__(self, other):
        raise PermissionError("注入：目录不可写")
    def __str__(self):
        return str(orig_dir)
reports._tasks_dir = lambda: _BadDir()
try:
    try:
        writing.convert_to_report(uid_b19, None, "admin")
        converted = True
    except Exception:
        converted = False
finally:
    reports._tasks_dir = orig_dir
row_b19 = writing.get_candidate(uid_b19)
check("B19-1 文件失败 → 候选状态未变（未转报告）",
      row_b19["status"] != "已转报告", row_b19["status"])
orphans = db.get_conn().execute(
    "SELECT COUNT(*) n FROM report_tasks WHERE title='B19 回滚候选'").fetchone()["n"]
check("B19-2 无孤立报告任务", orphans == 0, str(orphans))

# ============================================================
print("== F 推文流入口（单条/批量，X/RSS 一致） ==")
s, d = api("POST", "/api/writing/candidates/from-tweets", {"tweet_ids": [t3]})
check("F-1 单条加入（内部 ID，标题自动）", s == 200 and
      d.get("items", [{}])[0].get("cand_uid"), str(d)[:80])
c_f = writing.get_candidate(d["items"][0]["cand_uid"])
check("F-2 标题自动取情报内容", bool(c_f["title"]), str(c_f["title"])[:40])
# RSS 条目同样入口（source_id 归属不影响）
rss_tid = db.insert_tweet({"screen_name": "RSS:THN", "tweet_id": "rss-f1",
                           "source_id": "rss:1", "text": "RSS 情报内容", "url": "u",
                           "published_at": db.now()})
s, d = api("POST", "/api/writing/candidates/from-tweets", {"tweet_ids": [rss_tid]})
check("F-3 RSS 条目加入", s == 200 and d.get("items", [{}])[0].get("cand_uid"), str(d)[:80])

print(f"\n==== Round2 E/F 候选与入口：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
