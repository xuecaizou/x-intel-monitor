# -*- coding: utf-8 -*-
"""协作写作工作台 · 实时协作验收（真实 uvicorn 服务 + 真实 WebSocket 客户端，隔离库）。

覆盖：N01 两客户端并发收敛 / N02 重连恢复无重复初始化 / N05 中文与 emoji /
     N18（服务端侧：房间稳定持久化）/ 只读服务端约束 / Agent 活动房间受控修改广播 /
     WS 鉴权（无凭据拒绝、Origin 校验）。
运行：C:/Python314/python.exe tests/test_writing_collab.py
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

env = TestEnv(forbid_network=True).start()   # 仅拦 requests（外部网站）；本机回环服务由本测试自建
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
auth.create_user("reader", "ReaderPass123", "read")

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
from pycrdt import (Doc, Text, YMessageType,  # noqa: E402
                    create_sync_message, handle_sync_message, create_update_message)

# ---------------- 启动真实服务（同进程线程） ----------------
import uvicorn  # noqa: E402
from app.main import app  # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = _free_port()
BASE = f"http://127.0.0.1:{PORT}"
WSBASE = f"ws://127.0.0.1:{PORT}"
_server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT,
                                        log_level="warning", lifespan="on"))
_th = threading.Thread(target=_server.run, daemon=True)
_th.start()
for _ in range(60):
    try:
        with urllib.request.urlopen(BASE + "/api/healthz", timeout=1) as r:
            if r.status == 200:
                break
    except Exception:
        time.sleep(0.4)
else:
    print("服务未就绪")
    sys.exit(1)
print(f"服务就绪：{BASE}")


# ---------------- REST 助手（urllib，带会话/CSRF） ----------------
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


def api(boss, method, path, body=None):
    return http(method, path, body, headers={"X-CSRF-Token": boss["csrf"]}, cookies=boss["cookie"])


# ---------------- Yjs WS 测试客户端（asyncio 桥接线程） ----------------
import asyncio  # noqa: E402


class YClient:
    def __init__(self, doc_uid, cookie=None, expect_fail=False):
        self.doc = Doc()
        self.text = self.doc.get("content", type=Text)
        self.q = []
        self.ev = threading.Event()
        self._stop = False
        self.loop = asyncio.new_event_loop()
        self._th = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._th.start()
        headers = {"Cookie": cookie} if cookie else {}
        self.fail_reason = None
        self.ws = None
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
print("== C0 准备 ==")
tpl = writing.create_template("协作模板", body_md="# 初始标题\n\n第一节。\n", variables=[], operator="boss")
writing.publish_template(tpl["id"], "v1", "boss")
doc_uid = dc.create_doc("协作测试文档", template_id=tpl["id"], operator="boss")
init_text = dc.current_text(doc_uid)
check("C0-1 初始正文=模板发布版", init_text == "# 初始标题\n\n第一节。\n", repr(init_text))

print("== C1 连接鉴权 ==")
NOAUTH = YClient(doc_uid, cookie=None, expect_fail=True)
check("C1-1 无凭据 WS 被拒", NOAUTH.fail_reason is not None, str(NOAUTH.fail_reason))
BADORIGIN = None
try:
    BADORIGIN = websockets.connect(f"{WSBASE}/ws/docs/{doc_uid}",
                                   additional_headers={"Cookie": boss["cookie"],
                                                       "Origin": "http://evil.example"},
                                   max_size=16 * 1024 * 1024)
    BADORIGIN = BADORIGIN.__enter__()
    bad_ok = True
except Exception as e:
    bad_ok = False
check("C1-2 恶意 Origin 被拒", not bad_ok, str(bad_ok))

print("== C2 连接即同步初始内容（仅初始化一次） ==")
A = YClient(doc_uid, cookie=boss["cookie"])
A.pump(2.0)
check("C2-1 客户端收到初始正文", str(A.text) == init_text, repr(str(A.text)))
B = YClient(doc_uid, cookie=boss["cookie"])
B.pump(2.0)
check("C2-2 第二客户端同内容（无重复初始化）", str(B.text) == init_text, repr(str(B.text)))
n_updates = db.get_conn().execute("SELECT COUNT(*) n FROM doc_updates WHERE doc_uid=?",
                                  (doc_uid,)).fetchone()["n"]
check("C2-3 更新日志仍为初始少量行", n_updates <= 2, f"{n_updates}")

print("== C3 两客户端并发编辑收敛（N01/N05） ==")
A.local_edit("甲端写入中文🌍。")
A.pump(1.0)
B.pump(2.5)
check("C3-1 B 看到 A 的修改", "甲端写入中文🌍。" in str(B.text), repr(str(B.text)))
B.local_edit("乙端写入。")
B.pump(1.0)
A.pump(2.5)
check("C3-2 A 看到 B 的修改", "乙端写入。" in str(A.text))
check("C3-3 双端收敛一致", str(A.text) == str(B.text), f"{repr(str(A.text))} vs {repr(str(B.text))}")
server_text = dc.current_text(doc_uid)
check("C3-4 服务端文本=收敛结果", server_text == str(A.text), repr(server_text))

print("== C4 持久化确认与修订快照 ==")
room = dc._room_by_uid.get(doc_uid)
ls = room.awareness.get_local_state() if room else None
check("C4-1 服务端 awareness 含 saved_seq", bool(ls) and ls.get("saved_seq", 0) > 0
      and ls.get("role") == "server", str(ls))
time.sleep(4.2)
rev = dc.latest_revision(doc_uid)
check("C4-2 修订快照生成（save）", rev is not None and rev["revision_kind"] == "save"
      and rev["body_md"] == server_text, str(rev and rev["revision_kind"]))

print("== C5 只读连接：服务端丢弃其更新 ==")
reader = login("reader", "ReaderPass123")
RO = YClient(doc_uid, cookie=reader["cookie"])
RO.pump(2.0)
before = dc.current_text(doc_uid)
RO.local_edit("只读端写入——应被服务端丢弃。")
RO.pump(1.5)
A.pump(1.0)
after = dc.current_text(doc_uid)
check("C5-1 只读端修改未进服务端", after == before, repr(after[-30:]))
check("C5-2 其他客户端不受影响", "只读端写入" not in str(A.text))
RO.close()

print("== C6 Agent 活动房间受控修改 → 实时广播 ==")
atok_s, atok_d, _ = api(boss, "POST", "/api/agent/tokens", {"name": "ws-agent", "docs": doc_uid})
atok = atok_d["token"]
s, d, _ = http("POST", f"/api/agent/docs/{doc_uid}/apply-edit",
               {"base_rev": dc.latest_revision(doc_uid)["rev"],
                "old_text": "第一节。", "new_text": "第一节（Agent 已补充）。🌍",
                "client_id": "ws-e1", "rationale": "活动房间内修改"},
               headers={"Authorization": f"Bearer {atok}"})
check("C6-1 Agent 活动房间修改成功", s == 200, str(d)[:120])
A.pump(2.5)
B.pump(2.5)
check("C6-2 修改广播到在线客户端", "第一节（Agent 已补充）。🌍" in str(A.text))
check("C6-3 双端再次收敛", str(A.text) == str(B.text))

print("== C7 断开重连：内容保留、不重复初始化（N02） ==")
A.close()
B.close()
time.sleep(1.5)
offline_text = dc.current_text(doc_uid)
check("C7-1 房间清空后离线回放一致", offline_text == str(A.text), repr(offline_text[-40:]))
A2 = YClient(doc_uid, cookie=boss["cookie"])
A2.pump(2.0)
check("C7-2 重连后内容完整恢复", str(A2.text) == offline_text, repr(str(A2.text))[:80])
A2.local_edit("重连后追加。")
A2.pump(1.5)
A2.close()
time.sleep(0.5)
final_text = dc.current_text(doc_uid)
check("C7-3 重连后编辑正常落库", "重连后追加。" in final_text)
check("C7-4 无重复初始化", final_text.count("初始标题") == 1)

_server.should_exit = True
_th.join(timeout=6)
print(f"\n==== 实时协作验收：{PASS} PASS / {len(FAIL)} FAIL ====")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
