# -*- coding: utf-8 -*-
"""Round2 · B01–B07 协作内核安全/持久化/审核回归（真实 uvicorn + 真实 WebSocket）。

对应任务书 §10.1（权限、持久化和审核——优先修复）：
  B01 跨文档 ticket 复用被拒（A 文档 write ticket 连 B 文档 → 至多 read，写被丢弃）
  B02 Agent Token WS 保留完整 scope 身份——未授权文档拒绝，授权文档可写
  B03 会话删除/权限降级/Token 吊销 → 连接周期复核真实关闭；写连接降级即断
  B04 持久化故障注入：写失败不推进 saved_seq，恢复后按序补齐且内容完整
  B05 服务端 per-update sha 契约（doc_updates.update_sha + awareness.saved_shas）
  B06 同 client_id 并发 apply-edit：只应用一次，无重复正文
  B07 审核通过后内容修改：状态回落编辑中、可重新审阅、默认导出=批准版本

运行：python tests/test_round2_collab_security.py
"""
import json
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from tests.testenv import TestEnv  # noqa: E402

env = TestEnv(forbid_network=True).start()
(env.tmp / "runtime.json").write_text(json.dumps({"scheduler": {"enabled": False}}), encoding="utf-8")

import app.db as db  # noqa: E402
import app.auth as auth  # noqa: E402
import app.writing as writing  # noqa: E402
import app.doccollab as dc  # noqa: E402

db.DATA_DIR = env.tmp
db._conn = None
db.init_db()
auth.init_auth_tables()
auth.create_admin("boss", "BossPass12345")
auth.create_user("writer1", "WriterPass123", "write")

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL.append(name)
        print(f"FAIL {name} {detail}")


import websockets  # noqa: E402
from pycrdt import (Doc, Text, YMessageType, create_sync_message,
                    handle_sync_message, create_update_message)  # noqa: E402

import uvicorn  # noqa: E402
from app.main import app  # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# 2026-11-16：端口/启动重试（离线连跑时偶发绑定竞态导致整组静默失败的加固）
_server = _th = None
for attempt in range(3):
    PORT = _free_port()
    BASE = f"http://127.0.0.1:{PORT}"
    WSBASE = f"ws://127.0.0.1:{PORT}"
    _server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT,
                                            log_level="warning", lifespan="on"))
    _th = threading.Thread(target=_server.run, daemon=True)
    _th.start()
    ready = False
    for _ in range(60):
        try:
            with urllib.request.urlopen(BASE + "/api/healthz", timeout=1) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.4)
    if ready:
        print(f"服务就绪：{BASE}", flush=True)
        break
    _server.should_exit = True
    _th.join(timeout=5)
    print(f"[retry] 端口 {PORT} 启动失败，换端口重试（第 {attempt+1} 次）", flush=True)
else:
    print("服务未就绪（3 次重试失败）", flush=True)
    sys.exit(1)


def http(method, path, body=None, headers=None, cookies=""):
    req = urllib.request.Request(BASE + path, method=method)
    if cookies:
        req.add_header("Cookie", cookies)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8")), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8")), dict(e.headers)
        except Exception:
            return e.code, {}, dict(e.headers)


def login(username, password):
    s, d, h = http("POST", "/api/auth/login", {"username": username, "password": password})
    assert s == 200, d
    setc = h.get("set-cookie") or h.get("Set-Cookie") or ""
    sid = setc.split(";")[0]
    return {"cookie": sid, "csrf": d["csrf"], "username": username}


def api(who, method, path, body=None):
    return http(method, path, body, headers={"X-CSRF-Token": who["csrf"]}, cookies=who["cookie"])


import asyncio  # noqa: E402


class YClient:
    def __init__(self, doc_uid, cookie=None, bearer=None, expect_fail=False):
        self.doc = Doc()
        self.text = self.doc.get("content", type=Text)
        self.q = []
        self.ev = threading.Event()
        self._stop = False
        self.loop = asyncio.new_event_loop()
        self._th = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._th.start()
        headers = {}
        if cookie:
            headers["Cookie"] = cookie
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        self.fail_reason = None
        self.ws = None
        self.closed = False
        fut = asyncio.run_coroutine_threadsafe(
            self._connect(f"{WSBASE}/ws/docs/{doc_uid}", headers), self.loop)
        try:
            fut.result(timeout=10)
        except Exception as e:
            self.fail_reason = f"{type(e).__name__}: {e}"[:140]
            if not expect_fail:
                raise
        if self.ws is not None:
            asyncio.run_coroutine_threadsafe(self._reader(), self.loop)
            self.send(bytes(create_sync_message(self.doc)))
            self.pump(1.5)

    async def _connect(self, url, headers):
        self._ctx = websockets.connect(url, additional_headers=headers,
                                       max_size=16 * 1024 * 1024)
        self.ws = await self._ctx.__aenter__()

    async def _reader(self):
        while not self._stop:
            try:
                msg = await asyncio.wait_for(self.ws.recv(), timeout=0.5)
            except TimeoutError:
                continue
            except Exception:
                self.closed = True
                break
            if msg is not None:
                self.q.append(msg if isinstance(msg, bytes) else msg.encode())
                self.ev.set()

    def send(self, b):
        asyncio.run_coroutine_threadsafe(self.ws.send(b), self.loop).result(5)

    def pump(self, timeout=2.0):
        n = 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ev.wait(0.15)
            while self.q:
                msg = self.q.pop(0)
                if msg and msg[0] == int(YMessageType.SYNC):
                    reply = handle_sync_message(msg[1:], self.doc)
                    if reply:
                        self.send(bytes(reply))
                    n += 1
                self.ev.clear()
            if not self.q and n > 0:
                break
        return n

    def local_edit(self, s):
        self.text += s
        self.send(bytes(create_update_message(self.doc.get_update())))

    def close(self):
        self._stop = True
        if self.ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._ctx.__aexit__(None, None, None),
                                                 self.loop).result(5)
            except Exception:
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)


# ============================================================
boss = login("boss", "BossPass12345")
writer1 = login("writer1", "WriterPass123")
print("== 准备：模板 + 文档 A/B + ACL ==")
tpl = writing.create_template("R2 模板", body_md="# R2\n\n共享首段。\n", variables=[], operator="boss")
writing.publish_template(tpl["id"], "v1", "boss")
doc_a = dc.create_doc("文档A", template_id=tpl["id"], operator="boss")
doc_b = dc.create_doc("文档B", template_id=tpl["id"], operator="boss")
s, d, _ = api(boss, "PUT", f"/api/writing/docs/{doc_a}/acl",
              {"principal": "writer1", "permission": "write"})
check("P0-1 ACL 接口可用（writer1→A write）", s == 200, f"{s} {str(d)[:80]}")

# ============================================================
print("== B01 跨文档 ticket 复用被拒 ==")
# writer1 对 A 有 write；取 A 的写 ticket 后去连 B（对 B 仅默认 read）
s, d, _ = api(writer1, "POST", f"/api/writing/docs/{doc_a}/ws-ticket", {})
check("B01-0 ticket 签发", s == 200 and d.get("ticket"), f"{s} {str(d)[:80]}")
ticket_a = d["ticket"]

async def _ticket_connect():
    ctx = websockets.connect(f"{WSBASE}/ws/docs/{doc_b}?ticket={ticket_a}",
                             additional_headers={}, max_size=16 * 1024 * 1024)
    return await ctx.__aenter__()
loop_b = asyncio.new_event_loop()
th_b = threading.Thread(target=loop_b.run_forever, daemon=True)
th_b.start()
fut = asyncio.run_coroutine_threadsafe(_ticket_connect(), loop_b)
ticket_ws = None
try:
    ticket_ws = fut.result(timeout=8)
except Exception as e:
    pass
if ticket_ws is None:
    check("B01-1 A 的 ticket 连 B 被直接拒绝", True, "连接层拒绝")
else:
    # 连接被接受也必须只是 read（B 默认 read）：写必须被服务端丢弃
    q = []
    ev = threading.Event()

    async def _reader():
        while True:
            try:
                msg = await asyncio.wait_for(ticket_ws.recv(), timeout=0.5)
            except TimeoutError:
                continue
            except Exception:
                break
            if msg:
                q.append(msg if isinstance(msg, bytes) else msg.encode())
                ev.set()

    asyncio.run_coroutine_threadsafe(_reader(), loop_b)
    tdoc = Doc()
    ttext = tdoc.get("content", type=Text)
    async def _sync():
        sm = bytes(create_sync_message(tdoc))
        await ticket_ws.send(sm)
    asyncio.run_coroutine_threadsafe(_sync(), loop_b).result(5)
    deadline = time.time() + 2
    while time.time() < deadline:
        ev.wait(0.15)
        while q:
            msg = q.pop(0)
            if msg and msg[0] == int(YMessageType.SYNC):
                reply = handle_sync_message(msg[1:], tdoc)
                if reply:
                    asyncio.run_coroutine_threadsafe(ticket_ws.send(bytes(reply)), loop_b).result(5)
            ev.clear()
        if q == [] and ttext.__len__() >= 0:
            break
    before_b = dc.current_text(doc_b)
    ttext += "越权写入——ticket 跨文档。"
    upd = tdoc.get_update()
    async def _send_upd():
        await ticket_ws.send(bytes(create_update_message(upd)))
    asyncio.run_coroutine_threadsafe(_send_upd(), loop_b).result(5)
    time.sleep(2.0)
    after_b = dc.current_text(doc_b)
    check("B01-2 跨文档 ticket 在 B 上无写权限（写被丢弃）",
          after_b == before_b and "越权写入" not in after_b, repr(after_b[-40:]))
if ticket_ws is not None:
    async def _close():
        await ticket_ws.close()
    asyncio.run_coroutine_threadsafe(_close(), loop_b).result(5)
loop_b.call_soon_threadsafe(loop_b.stop)

# ============================================================
print("== B02 Agent Token WS scope ==")
s, d, _ = api(boss, "POST", "/api/agent/tokens", {"name": "r2-agent", "docs": doc_a})
check("B02-0 agent token 创建", s == 200 and d.get("token"), f"{s} {str(d)[:80]}")
atok = d["token"]
AGENT_B = YClient(doc_b, bearer=atok, expect_fail=True)
check("B02-1 未授权文档的 Agent WS 连接被拒", AGENT_B.fail_reason is not None,
      str(AGENT_B.fail_reason))
AGENT_A = YClient(doc_a, bearer=atok)
AGENT_A.pump(2.0)
check("B02-2 授权文档 Agent WS 可连", str(AGENT_A.text) != "", repr(str(AGENT_A.text))[:60])
AGENT_A.local_edit("Agent 经 WS 追加。")
AGENT_A.pump(1.0)
time.sleep(0.6)
check("B02-3 授权文档内 Agent WS 写生效", "Agent 经 WS 追加。" in dc.current_text(doc_a),
      repr(dc.current_text(doc_a))[-50:])

# ============================================================
print("== B03 会话删除/降级/Token 吊销 → 连接关闭 ==")
w2 = login("writer1", "WriterPass123")   # writer1 对 A 有 write（会话连接）
W = YClient(doc_a, cookie=w2["cookie"])
W.pump(2.0)
# 降级：A 上 writer1 → read
dc.set_acl(doc_a, "writer1", "read", "boss")
time.sleep(0.3)
import asyncio as _aio
dc._CLEANUP_MIN_AGE = 0
_aio.run(dc._cleanup_once())
time.sleep(1.5)
check("B03-1 写连接降级后被服务端关闭", W.closed or W.fail_reason is not None,
      f"closed={W.closed}")
before = dc.current_text(doc_a)
try:
    W.local_edit("降级后仍尝试写入。")
    W.pump(1.0)
except Exception:
    pass
time.sleep(0.8)
check("B03-2 降级连接的写入未落服务端", dc.current_text(doc_a) == before,
      repr(dc.current_text(doc_a))[-40:])

# 会话删除：writer1 重新登录拿新会话并连接，然后删除会话
w3 = login("writer1", "WriterPass123")
dc.set_acl(doc_a, "writer1", "write", "boss")
W3 = YClient(doc_a, cookie=w3["cookie"])
W3.pump(2.0)
check("B03-3 恢复写权限后可连可写（前置）", str(W3.text) != "", "")
sid3 = w3["cookie"].split("=")[1]
auth.drop_session(sid3)
_aio.run(dc._cleanup_once())
time.sleep(1.5)
check("B03-4 会话删除后连接被关闭", W3.closed or W3.fail_reason is not None,
      f"closed={W3.closed}")
before3 = dc.current_text(doc_a)
try:
    W3.local_edit("会话删除后写入。")
    W3.pump(1.0)
except Exception:
    pass
time.sleep(0.8)
check("B03-5 会话删除后写入未落服务端", dc.current_text(doc_a) == before3,
      repr(dc.current_text(doc_a))[-40:])

# Token 吊销
s, d, _ = api(boss, "POST", "/api/agent/tokens", {"name": "r2-agent-revoke", "docs": doc_a})
atok2 = d["token"]
tok_id = d.get("id") or d.get("token_id")
A3 = YClient(doc_a, bearer=atok2)
A3.pump(2.0)
auth.revoke_token(int(tok_id)) if tok_id else auth.revoke_token(
    db.get_conn().execute("SELECT id FROM auth_tokens WHERE name='r2-agent-revoke'").fetchone()["id"])
_aio.run(dc._cleanup_once())
time.sleep(1.5)
check("B03-6 Token 吊销后连接被关闭", A3.closed or A3.fail_reason is not None,
      f"closed={A3.closed}")

# ============================================================
print("== B04 持久化故障注入：失败不确认、恢复后按序补齐 ==")
doc_c = dc.create_doc("故障注入文档", template_id=tpl["id"], operator="boss")
C = YClient(doc_c, cookie=boss["cookie"])
C.pump(2.0)
room_c = None
store_c = None
for _ in range(20):   # 房间构建是异步的——等待 store 就绪（非确定性修复）
    room_c = dc._room_by_uid.get(doc_c)
    store_c = getattr(room_c, "ystore", None) if room_c else None
    if store_c is not None:
        break
    time.sleep(0.3)
check("B04-0 房间 store 可达", store_c is not None, "")

orig = dc._store_update_sync
state = {"fail": True, "calls": 0}
def flaky(doc_uid, data):
    state["calls"] += 1
    if state["fail"]:
        raise RuntimeError("注入：SQLite 写失败")
    return orig(doc_uid, data)
dc._store_update_sync = flaky
C.local_edit("故障注入编辑一。")
C.local_edit("故障注入编辑二。")
time.sleep(2.0)
ls = room_c.awareness.get_local_state() or {}
n_rows_mid = db.get_conn().execute("SELECT COUNT(*) n FROM doc_updates WHERE doc_uid=?",
                                   (doc_c,)).fetchone()["n"]
ls_seq = (ls or {}).get("saved_seq", 0)
check("B04-1 故障期间不虚报保存（pending 保留 + saved_seq 不推进）",
      state["calls"] >= 2 and len(store_c._pending) >= 1 and ls_seq < 3,
      f"calls={state['calls']} pending={len(store_c._pending)} rows={n_rows_mid} saved_seq={ls_seq}")
state["fail"] = False
store_c._flush_ev.set()
time.sleep(4.0)
time.sleep(4.0)
n_rows_after = db.get_conn().execute("SELECT COUNT(*) n FROM doc_updates WHERE doc_uid=?",
                                     (doc_c,)).fetchone()["n"]
final_c = dc.current_text(doc_c)
check("B04-2 恢复后按序补齐且内容完整",
      "故障注入编辑一。" in final_c and "故障注入编辑二。" in final_c
      and final_c == str(C.text), repr(final_c[-60:]))
check("B04-3 待刷队列清空", len(store_c._pending) == 0, f"pending={len(store_c._pending)}")
ls2 = room_c.awareness.get_local_state() or {}
check("B04-4 saved_shas 滚动窗口存在（B05 契约）",
      isinstance(ls2.get("saved_shas"), list) and len(ls2["saved_shas"]) >= 1,
      str(ls2.get("saved_shas"))[:80])
sha_rows = db.get_conn().execute(
    "SELECT COUNT(*) n FROM doc_updates WHERE doc_uid=? AND update_sha!=''",
    (doc_c,)).fetchone()["n"]
check("B05-1 每条更新落库带 sha（服务端对账依据）", sha_rows == n_rows_after,
      f"sha_rows={sha_rows} rows={n_rows_after}")

# ============================================================
print("== B06 同 client_id 并发 apply-edit：只应用一次 ==")
base_rev = dc.latest_revision(doc_a)["rev"]
results = []
def _fire(i):
    s, d, _ = http("POST", f"/api/agent/docs/{doc_a}/apply-edit",
                   {"base_rev": base_rev,
                    "old_text": "共享首段。", "new_text": f"共享首段（并发{i}）。",
                    "client_id": "r2-dup", "rationale": "并发幂等"},
                   headers={"Authorization": f"Bearer {atok}"})
    results.append((s, d))
ths = [threading.Thread(target=_fire, args=(i,)) for i in range(4)]
[t.start() for t in ths]
[t.join() for t in ths]
ok_results = [r for r in results if r[0] == 200]
conflict = [r for r in results if r[0] == 409]
check("B06-1 并发请求无 500", all(r[0] in (200, 409) for r in results), str(results)[:160])
text_a = dc.current_text(doc_a)
import re as _re
dup_hits = len(_re.findall("共享首段（并发", text_a))
check("B06-2 正文只应用一次", dup_hits == 1, f"hits={dup_hits} text={text_a[-80:]}")
n_edit_rows = db.get_conn().execute(
    "SELECT COUNT(*) n FROM doc_agent_edits WHERE doc_uid=? AND client_id='r2-dup'",
    (doc_a,)).fetchone()["n"]
check("B06-3 幂等记录恰一条", n_edit_rows == 1, f"rows={n_edit_rows}")

# ============================================================
print("== B07 审核后修改：状态回落 + 可重审 + 默认导出=批准版本 ==")
doc_d = dc.create_doc("审核流文档", template_id=tpl["id"], operator="boss")
d4 = dc.get_doc(doc_d)
dc._save_revision_sync(doc_d, dc.current_text(doc_d), "save", "boss", skip_same=False)
s, d, _ = api(boss, "POST", f"/api/writing/docs/{doc_d}/submit-review", {})
check("B07-0 提交审阅", s in (200, 404), f"{s} {str(d)[:80]}")
if s == 404:
    dc.submit_review(doc_d, "boss")
d4 = dc.get_doc(doc_d)
check("B07-1 状态=待审阅", d4["status"] == "待审阅", d4["status"])
review_rev = d4["review_rev"]
s, d, _ = api(boss, "POST", f"/api/writing/docs/{doc_d}/review",
              {"decision": "approved", "note": "r2"})
check("B07-2 审核通过", s in (200, 404) and (s == 404 or d.get("ok")), f"{s} {str(d)[:80]}")
if s == 404:
    dc.review_doc(doc_d, "approved", "r2", "boss")
d4 = dc.get_doc(doc_d)
check("B07-3 已通过", d4["status"] == "已通过", d4["status"])
# 内容修改（本地编辑→修订循环/直接 save）
dc.apply_text_replace(doc_d, "共享首段。", "共享首段（审核后修改）。", kind="save", operator="boss")
d4 = dc.get_doc(doc_d)
check("B07-4 内容修改后状态回落编辑中", d4["status"] == "编辑中", d4["status"])
check("B07-5 历史审核信息保留", bool(d4["review_decision"]), str(d4["review_decision"]))
# 可重新提交审阅
try:
    rev2 = dc.submit_review(doc_d, "boss")
    check("B07-6 可重新提交审阅", rev2 > review_rev, f"{rev2} vs {review_rev}")
except ValueError as e:
    check("B07-6 可重新提交审阅", False, str(e))
dc.review_doc(doc_d, "approved", "r2-2", "boss")
d4 = dc.get_doc(doc_d)
check("B07-7 二次通过", d4["status"] == "已通过" and d4["review_rev"] == rev2,
      f"{d4['status']}/{d4['review_rev']} vs {rev2}")
# 默认导出（rev=None）应选批准版本
from app import wordexport as we  # noqa: E402
pp = we.pandoc_path()
if pp:
    eid = we.create_export(doc_d, "boss", rev=None)
    for _ in range(60):
        e = we.get_export(eid)
        if e["status"] in ("done", "failed"):
            break
        time.sleep(0.5)
    e = we.get_export(eid)
    check("B07-8 默认导出=批准版本 rev", e["status"] == "done" and e["rev"] == rev2,
          f"status={e['status']} rev={e['rev']} expect={rev2} err={e['error'][:80]}")
else:
    print("SKIP B07-8（pandoc 不可用——Word 导出验收在具备 pandoc 的环境运行）")

_server.should_exit = True
_th.join(timeout=6)
print(f"\n==== Round2 B01-B07：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
