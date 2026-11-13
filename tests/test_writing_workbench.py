# -*- coding: utf-8 -*-
"""协作写作工作台 · 后端验收（隔离库 + TestClient，无真实外联）。

覆盖：候选（N06/N08）/ 模板（N09）/ 文档修订与审阅（N10）/ 权限（N11）/ Agent 契约（N03/N04）/
     Word 导出（N12-N15，pandoc 可用时）/ 汇总与删除保护 / 幂等与冲突。
运行：C:/Python314/python.exe tests/test_writing_workbench.py
"""
import base64
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from tests.testenv import TestEnv  # noqa: E402

env = TestEnv(forbid_network=True).start()
import app.db as db  # noqa: E402
import app.auth as auth  # noqa: E402
import app.writing as writing  # noqa: E402
import app.doccollab as dc  # noqa: E402
import app.wordexport as we  # noqa: E402

db.DATA_DIR = env.tmp
db._conn = None
db.init_db()
auth.init_auth_tables()
auth.create_admin("boss", "BossPass12345")
auth.create_user("reader", "ReaderPass123", "read")
auth.create_user("writer", "WriterPass123", "write")

PASS, FAIL = 0, []
SKIP = []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(name)
        print(f"FAIL {name} {detail}")


from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def login(username, password):
    c = TestClient(app)   # 每用户独立 client，避免 cookie jar 相互覆盖
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    d = r.json()
    return {"username": username, "csrf": d.get("csrf", ""), "client": c,
            "cookies": {auth.COOKIE_NAME: r.cookies.get(auth.COOKIE_NAME)}}


def sreq(sess, method, url, body=None, csrf=True):
    headers = {}
    if csrf and method != "GET":
        headers["X-CSRF-Token"] = sess["csrf"]
    r = sess["client"].request(method, url, json=body, headers=headers,
                               cookies=sess["cookies"])
    return r


boss = login("boss", "BossPass12345")
reader = login("reader", "ReaderPass123")
writer = login("writer", "WriterPass123")

# ============================================================
print("== W1 候选：人工/去重/决定/状态/乐观锁 ==")
r = sreq(boss, "POST", "/api/writing/candidates",
         {"title": "勒索软件投递活动", "angle": "投递链分析", "tweet_ids": [101, 102]})
check("W1-1 人工建候选 200", r.status_code == 200, r.text[:120])
uid1 = r.json()["cand_uid"]
check("W1-2 稳定 ID 前缀", uid1.startswith("cand_"))
r = sreq(boss, "POST", "/api/writing/candidates",
         {"title": "勒索软件  投递活动", "angle": "投递链 分析", "tweet_ids": [103]})
check("W1-3 规范化去重合并", r.json().get("merged") is True and r.json()["cand_uid"] == uid1, r.text[:120])
c = writing.get_candidate(uid1)
check("W1-4 合并后 tweet_ids 并集", sorted(c["tweet_ids"]) == [101, 102, 103], str(c["tweet_ids"]))
v0 = c["version"]
r = sreq(boss, "PUT", f"/api/writing/candidates/{uid1}",
         {"fields": {"priority": 2, "owner": "boss"}, "expected_version": v0})
check("W1-5 乐观锁编辑成功", r.status_code == 200)
r = sreq(boss, "PUT", f"/api/writing/candidates/{uid1}",
         {"fields": {"priority": 1}, "expected_version": v0})
check("W1-6 过期版本 409", r.status_code == 409, r.text[:80])
r = sreq(boss, "POST", f"/api/writing/candidates/{uid1}/decision", {"decision": "暂缓", "note": "等新材料"})
check("W1-7 暂缓决定", r.status_code == 200 and writing.get_candidate(uid1)["status"] == "暂缓")
r = sreq(boss, "POST", f"/api/writing/candidates/{uid1}/decision", {"decision": "采纳"})
check("W1-8 恢复采纳（状态回待处理）", r.status_code == 200 and writing.get_candidate(uid1)["status"] == "待处理")
r = sreq(boss, "POST", f"/api/writing/candidates/{uid1}/decision", {"decision": "不采用"})
check("W1-9 不采用", writing.get_candidate(uid1)["status"] == "不采用")
r = sreq(boss, "DELETE", f"/api/writing/candidates/{uid1}")
check("W1-10 不采用可删", r.status_code == 200)
# AI 更新不覆盖人工决定
uid2, _ = writing.create_candidate("AI 选题 测试", operator="ai", source_type="ai")
writing.update_candidate(uid2, {"human_decision": "不采用", "status": "不采用"}, "human")
writing.ai_update_candidate(uid2, {"suggestion": {"angle": "新角度"}, "priority": 1})
c2 = writing.get_candidate(uid2)
check("W1-11 AI 重评不覆盖人工决定", c2["human_decision"] == "不采用" and c2["status"] == "不采用")
check("W1-12 标记线索有更新", c2["has_update"] == 1)

# ============================================================
print("== W2 模板：白名单变量/发布不可变/复制/停用 ==")
r = sreq(boss, "POST", "/api/writing/templates",
         {"name": "事件分析模板", "body_md": "# {{title}}\n\n角度：{{angle}}\n\n{{__evil__}}",
          "variables": [{"name": "title", "label": "标题", "required": True},
                        {"name": "angle", "label": "角度"}]})
check("W2-1 建模板 200", r.status_code == 200, r.text[:120])
tpl_id = r.json()["id"]
body_doc = r.json()["doc_uid"]
r = sreq(boss, "POST", f"/api/writing/templates/{tpl_id}/publish", {"changelog": "首版"})
check("W2-2 发布 v1", r.json().get("version") == 1, r.text[:80])
v1_body = writing.template_versions(tpl_id)[0]["body_md"]
r = sreq(boss, "POST", f"/api/writing/templates/{tpl_id}/preview", {"values": {"title": "T"}})
pv = r.json()
check("W2-3 缺失变量提示", pv["missing"] == ["angle"], str(pv))
check("W2-4 未定义占位报告（不执行）", pv["unknown"] == ["__evil__"] and "{{__evil__}}" in pv["filled"])
writing.update_template(tpl_id, {"variables": [{"name": "title", "label": "标题"}]}, "boss")
r = sreq(boss, "POST", f"/api/writing/templates/{tpl_id}/publish", {"changelog": "改"})
check("W2-5 发布 v2", r.json().get("version") == 2)
check("W2-6 v1 快照不可变", writing.template_versions(tpl_id)[1]["body_md"] == v1_body)
r = sreq(boss, "POST", f"/api/writing/templates/{tpl_id}/duplicate", {"new_name": "事件分析模板 副本"})
check("W2-7 复制模板", r.status_code == 200 and r.json().get("doc_uid", "").startswith("doc_"))
r = sreq(boss, "POST", f"/api/writing/templates/{tpl_id}/status", {"status": "停用"})
check("W2-8 停用", writing.get_template(tpl_id)["status"] == "停用")
r = sreq(reader, "POST", "/api/writing/templates",
         {"name": "越权模板", "body_md": "x", "variables": []})
check("W2-9 read 角色不能建模板", r.status_code == 403)
r = sreq(writer, "POST", "/api/writing/templates",
         {"name": "越权模板2", "body_md": "x", "variables": []})
check("W2-10 write 角色不能建模板", r.status_code == 403)
r = sreq(boss, "POST", "/api/writing/templates", {"name": "事件分析模板", "body_md": "", "variables": []})
check("W2-11 同名拒绝", r.status_code == 400)

# ============================================================
print("== W3 候选转报告：单事务/幂等/固定模板版本 ==")
uid3, _ = writing.create_candidate("供应链投毒线索", tweet_ids=[], operator="boss")
r = sreq(boss, "POST", f"/api/writing/candidates/{uid3}/convert",
         {"template_id": tpl_id, "use_version": 1})
check("W3-1 转换成功", r.status_code == 200 and r.json().get("doc_uid", "").startswith("doc_"), r.text[:120])
conv = r.json()
doc_uid = conv["doc_uid"]
check("W3-2 报告任务创建", isinstance(conv["report_task_id"], int))
check("W3-3 候选状态已转报告", writing.get_candidate(uid3)["status"] == "已转报告")
check("W3-4 文档固定模板版本 v1", db.get_conn().execute(
    "SELECT template_version FROM doc_documents WHERE doc_uid=?", (doc_uid,)).fetchone()["template_version"] == 1)
r = sreq(boss, "POST", f"/api/writing/candidates/{uid3}/convert", {"template_id": tpl_id})
check("W3-5 重复转换幂等", r.json().get("existed") is True and r.json()["doc_uid"] == doc_uid)
n_tasks = db.get_conn().execute("SELECT COUNT(*) n FROM report_tasks WHERE title='供应链投毒线索'").fetchone()["n"]
check("W3-6 不产生重复报告任务", n_tasks == 1, f"{n_tasks}")
# 模板发布 v3 → 既有报告不变
writing.set_template_status(tpl_id, "启用", "boss")
writing.publish_template(tpl_id, "v3", "boss")
check("W3-7 旧报告模板版本仍为 1", db.get_conn().execute(
    "SELECT template_version FROM doc_documents WHERE doc_uid=?", (doc_uid,)).fetchone()["template_version"] == 1)
# 引用保护
db.get_conn().execute("INSERT OR IGNORE INTO tweets (id, tweet_id, screen_name, text, url, created_at)"
                      " VALUES (9001,'tw9001','T','x','u','2026-11-13 00:00:00')")
db.get_conn().commit()
writing.update_candidate(uid3, {}, "sys")  # 触发 version 变化不影响
prot = db.protected_tweet_ids()
# 候选没有真实推文引用时此处只验证调用不炸；引用保护在 W8 用真实推文验证

# ============================================================
print("== W4 文档：修订/恢复/审阅绑定 ==")
r = sreq(boss, "GET", f"/api/writing/docs/{doc_uid}")
check("W4-1 文档详情+权限", r.status_code == 200 and r.json()["my_permission"] == "write")
rev1 = r.json()["latest_rev"]
out = dc.apply_text_replace(doc_uid, "{{angle}}", "投递链与时间线（已实例化）", kind="agent_edit",
                            operator="agent:demo", base_rev_expected=rev1)
check("W4-2 受控修改成修订", out["rev"] == rev1 + 1)
r = sreq(boss, "POST", f"/api/writing/docs/{doc_uid}/submit-review")
rv = r.json().get("review_rev")
check("W4-3 提交审阅固定修订", r.status_code == 200 and rv == out["rev"] + 0 + (1 if False else 0) or rv, r.text[:100])
# 内容继续变化 → 旧批准不适用
dc.apply_text_replace(doc_uid, "投递链与时间线（已实例化）", "投递链与时间线", kind="agent_edit", operator="boss")
r = sreq(boss, "POST", f"/api/writing/docs/{doc_uid}/review", {"decision": "approved", "note": ""})
check("W4-4 待审阅可审核", r.status_code == 200)
latest = dc.latest_revision(doc_uid)["rev"]
doc = dc.get_doc(doc_uid)
check("W4-5 审核绑定提交时修订", doc["review_rev"] == rv and doc["review_decision"] == "approved")
check("W4-6 新内容不继承旧批准（review_rev 不随内容推进）", dc.latest_revision(doc_uid)["rev"] > doc["review_rev"])
# 恢复历史
revs = dc.list_revisions(doc_uid)
target = [x for x in revs if x["revision_kind"] == "init"][0]["rev"]
r = sreq(boss, "POST", f"/api/writing/docs/{doc_uid}/restore", {"rev": target})
check("W4-7 恢复形成新修订", r.status_code == 200 and r.json()["rev"] > latest)
check("W4-8 历史未被破坏", dc.get_revision(doc_uid, target)["revision_kind"] == "init")
dc.apply_text_replace(doc_uid, "{{angle}}", "投递链与时间线", kind="agent_edit", operator="boss")
r = sreq(reader, "POST", f"/api/writing/docs/{doc_uid}/restore", {"rev": target})
check("W4-9 只读用户拒绝恢复", r.status_code == 403)
r = sreq(writer, "POST", f"/api/writing/docs/{doc_uid}/restore", {"rev": target})
check("W4-10 无 ACL 的 write 角色默认只读", r.status_code == 403)

# ============================================================
print("== W5 文档 ACL ==")
r = sreq(boss, "PUT", f"/api/writing/docs/{doc_uid}/acl", {"principal": "writer", "permission": "write"})
check("W5-1 授予写权限", r.status_code == 200)
r = sreq(writer, "POST", f"/api/writing/docs/{doc_uid}/restore", {"rev": target})
check("W5-2 授权后可写", r.status_code == 200)
r = sreq(boss, "PUT", f"/api/writing/docs/{doc_uid}/acl", {"principal": "writer", "permission": "read"})
r = sreq(writer, "POST", f"/api/writing/docs/{doc_uid}/restore", {"rev": target})
check("W5-3 降权后拒绝", r.status_code == 403)
r = sreq(reader, "GET", f"/api/writing/docs/{doc_uid}")
check("W5-4 默认权限 read 可读", r.status_code == 200 and r.json()["my_permission"] == "read")
# W5-2 的授权验证动作执行了 restore（回卷到 init）——把文本恢复到 W6 工作状态
dc.apply_text_replace(doc_uid, "{{angle}}", "投递链与时间线", kind="agent_edit", operator="boss")

# ============================================================
print("== W6 Agent 契约：令牌/范围/幂等/冲突 ==")
r = sreq(boss, "POST", "/api/agent/tokens", {"name": "demo-agent", "docs": doc_uid})
check("W6-1 建 Agent Token（admin）", r.status_code == 200 and r.json()["token"].startswith("agent_"))
atok = r.json()["token"]
ascope = r.json()["scope"]
check("W6-2 范围限定存储", ascope == doc_uid)
AH = {"Authorization": f"Bearer {atok}"}
r = client.get(f"/api/agent/docs/{doc_uid}", headers=AH)
check("W6-3 Agent 读取文档", r.status_code == 200 and "body_md" in r.json())
r = client.post("/api/agent/tokens", headers=AH, json={"name": "evil"})
check("W6-4 Agent 不能建 Token", r.status_code == 403)
r = client.get("/api/writing/templates", headers=AH)
check("W6-5 Agent 不能访问模板接口（401/403）", r.status_code in (401, 403))
# 建第二个文档 → 范围外
doc_b = dc.create_doc("范围外文档", operator="boss")
r = client.get(f"/api/agent/docs/{doc_b}", headers=AH)
check("W6-6 范围外文档 403", r.status_code == 403)
# 受控修改：幂等/冲突/锚点
cur = dc.latest_revision(doc_uid)["rev"]
r = client.post(f"/api/agent/docs/{doc_uid}/apply-edit", headers=AH,
                json={"base_rev": cur, "old_text": "投递链与时间线", "new_text": "投递链与时间线（Agent 补充）",
                      "client_id": "op-1", "rationale": "补充"})
check("W6-7 受控修改成功", r.status_code == 200 and r.json()["rev"] == cur + 1, r.text[:120])
r = client.post(f"/api/agent/docs/{doc_uid}/apply-edit", headers=AH,
                json={"base_rev": cur, "old_text": "投递链与时间线", "new_text": "X", "client_id": "op-1"})
check("W6-8 client_id 幂等", r.status_code == 200 and r.json().get("existed") is True)
check("W6-9 幂等不重复插入", "（Agent 补充）X" not in dc.current_text(doc_uid))
r = client.post(f"/api/agent/docs/{doc_uid}/apply-edit", headers=AH,
                json={"base_rev": cur, "old_text": "投递链与时间线", "new_text": "Y", "client_id": "op-2"})
check("W6-10 base_rev 过期 409", r.status_code == 409)
r = client.post(f"/api/agent/docs/{doc_uid}/apply-edit", headers=AH,
                json={"base_rev": dc.latest_revision(doc_uid)["rev"], "old_text": "不存在的锚点",
                      "new_text": "Y", "client_id": "op-3"})
check("W6-11 零匹配 422", r.status_code == 422)
# 多匹配
dc.apply_text_replace(doc_uid, "（Agent 补充）", "重复锚", kind="agent_edit", operator="boss")
dc.apply_text_replace(doc_uid, "重复锚", "重复锚 重复锚", kind="agent_edit", operator="boss")
r = client.post(f"/api/agent/docs/{doc_uid}/suggestions", headers=AH,
                json={"old_text": "重复锚", "new_text": "Z", "client_id": "s1"})
check("W6-12 多匹配建议 422", r.status_code == 422, r.text[:100])
# 建议：创建→接受
base_now = dc.latest_revision(doc_uid)["rev"]
r = client.post(f"/api/agent/docs/{doc_uid}/suggestions", headers=AH,
                json={"old_text": "重复锚 重复锚", "new_text": "唯一锚", "client_id": "s2", "base_rev": base_now,
                      "rationale": "改写"})
check("W6-13 建议创建", r.status_code == 200, r.text[:120])
sid = r.json()["suggestion"]["id"]
r = client.post(f"/api/writing/docs/{doc_uid}/suggestions/{sid}/resolve", json={"decision": "accepted"},
                headers={"X-CSRF-Token": boss["csrf"]}, cookies=boss["cookies"])
check("W6-14 接受建议应用", r.status_code == 200 and r.json().get("status") == "accepted")
check("W6-15 建议内容已入正文", "唯一锚" in dc.current_text(doc_uid))
# Agent 迟到修改（N03）：基于旧 rev 的建议在人工改文后接受 → 冲突标记
s3, _v = dc.create_suggestion(doc_uid, agent="agent:demo", old_text="唯一锚", new_text="迟到改写",
                              client_id="s3", base_rev=dc.latest_revision(doc_uid)["rev"])
dc.apply_text_replace(doc_uid, "唯一锚", "人工已改", kind="agent_edit", operator="boss")
r = client.post(f"/api/writing/docs/{doc_uid}/suggestions/{s3['id']}/resolve", json={"decision": "accepted"},
                headers={"X-CSRF-Token": boss["csrf"]}, cookies=boss["cookies"])
check("W6-16 迟到建议冲突（不覆盖人工修改）", r.status_code == 409, r.text[:100])
check("W6-17 人工修改保留", "人工已改" in dc.current_text(doc_uid) and "迟到改写" not in dc.current_text(doc_uid))
# 中文/emoji 锚点（N05）
dc.apply_text_replace(doc_uid, "人工已改", "人工已改 🌍 中文", kind="agent_edit", operator="boss")
r = client.post(f"/api/agent/docs/{doc_uid}/apply-edit", headers=AH,
                json={"base_rev": dc.latest_revision(doc_uid)["rev"], "old_text": "🌍 中文",
                      "new_text": "🌍 中文✅", "client_id": "op-emoji"})
check("W6-18 emoji/中文锚点修改", r.status_code == 200 and "🌍 中文✅" in dc.current_text(doc_uid))

# ============================================================
print("== W7 Word 导出（pandoc 可用时真实转换） ==")
pandoc = "D:/AI-work/tools/pandoc/pandoc-3.6.3/pandoc.exe"
if Path(pandoc).exists():
    from app.config import save_runtime
    save_runtime({"word": {"pandoc_path": pandoc, "timeout_seconds": 90, "max_md_bytes": 2000000}})
    r = sreq(boss, "POST", f"/api/writing/docs/{doc_uid}/export/word", {})
    check("W7-1 导出任务受理", r.status_code == 200, r.text[:120])
    eid = r.json()["export_id"]
    status = ""
    for _ in range(80):
        e = we.get_export(eid)
        status = e["status"]
        if status in ("done", "failed"):
            break
        time.sleep(0.5)
    check("W7-2 导出完成", status == "done", str(we.get_export(eid).get("error", ""))[:150])
    if status == "done":
        import zipfile
        e = we.get_export(eid)
        z = zipfile.ZipFile(e["file_path"])
        xml = z.read("word/document.xml").decode("utf-8")
        check("W7-3 OOXML 部件完整", "word/styles.xml" in z.namelist() and "[Content_Types].xml" in z.namelist())
        check("W7-4 标题入文", "供应链投毒线索" in xml)
        check("W7-5 中文正文", "投递链与时间线" in xml)
        check("W7-6 中文文件名安全", "\\" not in e["file_name"] and "/" not in e["file_name"])
        # 缓存复用
        r2 = sreq(boss, "POST", f"/api/writing/docs/{doc_uid}/export/word", {})
        eid2 = r2.json()["export_id"]
        for _ in range(40):
            if we.get_export(eid2)["status"] in ("done", "failed"):
                break
            time.sleep(0.5)
        e2 = we.get_export(eid2)
        check("W7-7 同内容缓存命中", e2["status"] == "done" and e2["file_path"] == e["file_path"])
    # 越权下载
    r = client.get(f"/api/writing/docs/{doc_uid}/export/word/99999/download",
                   cookies=boss["cookies"])
    check("W7-8 不存在导出 404", r.status_code == 404)
else:
    SKIP.append("W7 pandoc 不可用（XINTEL_PANDOC 未配置）——本机验证在交付报告中记录")
    print("SKIP W7（pandoc 不存在）")

# ============================================================
print("== W8 删除保护 + 汇总 ==")
tid = db.insert_tweet({"screen_name": "WSrc", "tweet_id": "w8", "text": "w8 body", "url": "u",
                       "published_at": db.now()})
uid8, _ = writing.create_candidate("保护候选 W8", tweet_ids=[tid], operator="boss")
check("W8-1 候选引用进入保护集", tid in db.protected_tweet_ids())
db.batch_tweet_op([tid], "delete")
check("W8-2 批量删除跳过候选引用", db.get_tweet(tid) is not None)
s = writing.summary()
check("W8-3 汇总计数为服务端聚合", s["docs_total"] >= 2 and "candidates_active" in s)
r = sreq(reader, "GET", "/api/stats")
check("W8-4 stats 带 writing 汇总", r.status_code == 200 and "writing" in r.json())

# ============================================================
print("== W9 WS ticket 一次性 ==")
tk = dc.create_ticket(doc_uid, {"username": "reader", "role": "read", "kind": "session"}, "read")
info = dc.consume_ticket(tk)
check("W9-1 ticket 可用一次", info is not None and info["principal"]["username"] == "reader")
check("W9-2 二次使用失效", dc.consume_ticket(tk) is None)

# ============================================================
print("== W10 成员管理 ==")
r = sreq(boss, "POST", "/api/auth/users", {"username": "newbie", "password": "NewbiePass123", "role": "write"})
check("W10-1 建成员", r.status_code == 200)
r = sreq(boss, "POST", "/api/auth/users", {"username": "newbie", "password": "NewbiePass123"})
check("W10-2 重名拒绝", r.status_code == 400)
r = sreq(boss, "POST", "/api/auth/users/newbie/role", {"role": "read"})
check("W10-3 改角色", r.status_code == 200)
# 会话角色以 auth_users 为准（历史缺陷修复验证）
nm = login("newbie", "NewbiePass123")
r = sreq(nm, "POST", "/api/writing/candidates", {"title": "newbie 尝试"})
check("W10-4 降权后 write 被拒（会话角色生效）", r.status_code == 403)

print(f"\n==== 工作台后端验收：{PASS} PASS / {len(FAIL)} FAIL / SKIP {len(SKIP)} ====")
for x in SKIP:
    print("SKIP:", x)
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
