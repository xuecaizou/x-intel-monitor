# -*- coding: utf-8 -*-
"""协作文档内核：pycrdt(Yjs) + ASGI WebSocket + SQLite 持久化 + 业务修订。

架构（详见 docs/worklogs/collaborative-writing/findings.md）：
  - 协作正文权威来源 = doc_updates CRDT 增量日志（逐更新落主库）；
    Markdown 文件/修订快照是读取或导出结果，不存在彼此独立的整篇覆盖通道。
  - 房间按需构建：回放 doc_updates；仅当日志为空时从 initial_md 初始化一次。
  - 持久化确认：YStore.write 落库后推进房间 awareness 的 saved_seq，前端"已保存"以此为据。
  - 业务修订：周期对比文本哈希 → doc_revisions（save）；restore/agent_edit/suggest_apply/
    submit_review/export 均为不可变修订；恢复历史=新修订，不破坏历史与审核记录。
  - 服务端只读约束：包装 Channel 丢弃只读连接的 SYNC_STEP2/SYNC_UPDATE（服务端永不应用）。
  - 索引语义：pycrdt Text 索引 = UTF-8 字节偏移；所有替换自算字节边界（锚点必须唯一匹配）。
  - 单进程约束：所有房间操作必须在事件循环线程；REST 线程经 run_coroutine_threadsafe 桥接。
"""
import asyncio
from pathlib import Path
import collections
import contextvars
import hashlib
import json
import secrets
import threading
import time
from contextlib import suppress

from pycrdt import Doc, Text, YMessageType, YSyncMessageType
from pycrdt.store.base import BaseYStore
from pycrdt.websocket import ASGIServer, YRoom, WebsocketServer

from . import db

TEXT_FIELD = "content"

_loop = None                    # 主事件循环（REST 线程桥接用）
_server: WebsocketServer = None  # 模块底部构造
_asgi: ASGIServer = None  # 模块底部构造
_server_task = None
_room_by_uid: dict = {}         # doc_uid -> YRoom（与 _server.rooms 同步维护）
_build_locks: dict = {}         # doc_uid -> asyncio.Lock（loop 侧）
_doc_mutexes: dict = {}         # doc_uid -> threading.Lock（跨线程互斥）
_rev_tasks: dict = {}           # doc_uid -> asyncio.Task（修订快照循环）
_conn_registry: dict = {}       # doc_uid -> {id(inner_channel): info}
_saved_seq_cache: dict = {}     # doc_uid -> 已落库最大 seq
_stopping = False
_CLEANUP_MIN_AGE = 30   # 连接注册最小复核间隔（测试可置 0）

_ws_perm: contextvars.ContextVar = contextvars.ContextVar("xintel_ws_perm", default=None)


class DocError(Exception):
    pass


class AnchorError(DocError):
    """锚点零匹配/多匹配——明确失败，不猜测位置"""

    def __init__(self, matches, msg=""):
        self.matches = matches
        super().__init__(msg or f"锚点匹配 {matches} 处（要求恰好 1 处）")


class RevisionConflict(DocError):
    def __init__(self, current, msg=""):
        self.current = current
        super().__init__(msg or f"版本冲突：base_rev 与当前修订 {current} 不一致")


def _uid(prefix):
    return prefix + secrets.token_hex(6)


def _mutex(doc_uid) -> threading.Lock:
    m = _doc_mutexes.get(doc_uid)
    if m is None:
        m = _doc_mutexes[doc_uid] = threading.Lock()
    return m


def _hash_md(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:32]


# ============================================================
# DB 层：文档 / 修订 / 建议 / 工单
# ============================================================

def _assets_dir(doc_uid):
    """受控素材目录（2026-11-16 B10：此前路由引用不存在的函数导致素材接口全 500）。
    目录锁定在 data/doc_assets/<doc_uid>，路径由调用方经 safe 校验使用。"""
    from .config import DATA_DIR
    d = Path(DATA_DIR) / "doc_assets" / doc_uid
    d = d.resolve()
    root = (Path(DATA_DIR) / "doc_assets").resolve()
    if root not in d.parents:
        raise DocError("素材目录非法")
    return d


def get_doc(doc_uid):
    r = db.get_conn().execute("SELECT * FROM doc_documents WHERE doc_uid=?", (doc_uid,)).fetchone()
    return dict(r) if r else None


def list_docs(kind="", status="", q="", limit=50, offset=0):
    where, args = [], []
    if kind:
        where.append("d.kind=?")
        args.append(kind)
    if status:
        where.append("d.status=?")
        args.append(status)
    if q:
        where.append("d.title LIKE ?")
        args.append(f"%{q}%")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    c = db.get_conn()
    total = c.execute(f"SELECT COUNT(*) n FROM doc_documents d{w}", args).fetchone()["n"]
    rows = c.execute(
        f"SELECT d.*, t.name template_name, t.current_version template_current"
        f" FROM doc_documents d LEFT JOIN doc_templates t ON t.id=d.template_id{w}"
        f" ORDER BY d.updated_at DESC, d.doc_uid LIMIT ? OFFSET ?",
        args + [int(limit), int(offset)]).fetchall()
    return total, [dict(r) for r in rows]


def create_doc(title, template_id=None, use_version=None, operator="system", kind="report",
               initial_md="", candidate_uid="", default_permission="read"):
    """新建报告文档：固定模板发布版本 + 初始修订。"""
    tver = None
    tpl = None
    if template_id:
        tpl = db.get_conn().execute("SELECT * FROM doc_templates WHERE id=?",
                                    (int(template_id),)).fetchone()
        if not tpl:
            raise ValueError("模板不存在")
        if not tpl["current_version"]:
            raise ValueError("模板尚未发布任何版本")
        ver = int(use_version) if use_version else int(tpl["current_version"])
        tver = db.get_conn().execute(
            "SELECT * FROM template_versions WHERE template_id=? AND version=?",
            (tpl["id"], ver)).fetchone()
        if not tver:
            raise ValueError("模板版本不存在")
    init_md = tver["body_md"] if tver else (initial_md or "")
    uid = _uid("doc_")
    with db._lock:
        c = db.get_conn()
        c.execute(
            "INSERT INTO doc_documents (doc_uid, title, kind, status, template_id, template_version,"
            " initial_md, initialized, default_permission, candidate_uid, created_by, created_at, updated_at)"
            " VALUES (?,?,?,'编辑中',?,?,?,?,?,?,?,?,?)",
            (uid, title, kind, tpl["id"] if tpl else None, tver["version"] if tver else None,
             init_md, 1 if init_md else 0, default_permission, candidate_uid,
             operator, db.now(), db.now()))
        if init_md:
            c.execute("INSERT INTO doc_revisions (doc_uid, rev, title, body_md, revision_kind,"
                      " content_hash, created_by, created_at) VALUES (?,1,?,?,'init',?,?,?)",
                      (uid, title, init_md, _hash_md(init_md), operator, db.now()))
        c.execute("INSERT OR IGNORE INTO doc_acl (doc_uid, principal, permission, created_at)"
                  " VALUES (?,?,'write',?)", (uid, operator, db.now()))
        c.commit()
    return uid


def latest_revision(doc_uid):
    r = db.get_conn().execute(
        "SELECT * FROM doc_revisions WHERE doc_uid=? ORDER BY rev DESC LIMIT 1", (doc_uid,)).fetchone()
    return dict(r) if r else None


def list_revisions(doc_uid, limit=100):
    return [dict(r) for r in db.get_conn().execute(
        "SELECT id, doc_uid, rev, title, revision_kind, base_rev, content_hash, created_by,"
        " created_at, LENGTH(body_md) size FROM doc_revisions WHERE doc_uid=?"
        " ORDER BY rev DESC LIMIT ?", (doc_uid, int(limit))).fetchall()]


def get_revision(doc_uid, rev):
    r = db.get_conn().execute("SELECT * FROM doc_revisions WHERE doc_uid=? AND rev=?",
                              (doc_uid, int(rev))).fetchone()
    return dict(r) if r else None


def list_acl(doc_uid):
    return [dict(r) for r in db.get_conn().execute(
        "SELECT principal, permission, created_at FROM doc_acl WHERE doc_uid=?"
        " ORDER BY principal", (doc_uid,)).fetchall()]


def set_acl(doc_uid, principal, permission, operator):
    """显式授权/改权/收回（permission='' 删除）。仅 admin（路由层把关）。"""
    with db._lock:
        c = db.get_conn()
        if permission == "":
            c.execute("DELETE FROM doc_acl WHERE doc_uid=? AND principal=?", (doc_uid, principal))
        else:
            if permission not in ("read", "write"):
                raise ValueError("permission 须为 read/write")
            c.execute("INSERT INTO doc_acl (doc_uid, principal, permission, created_at)"
                      " VALUES (?,?,?,?) ON CONFLICT(doc_uid, principal)"
                      " DO UPDATE SET permission=excluded.permission",
                      (doc_uid, principal, permission, db.now()))
        c.commit()
    return True


def doc_permission(doc, principal):
    """计算用户对文档的权限：None=不可访问；read/write。
    规则：admin 恒 write；模板正文仅 admin 可写；显式 ACL 优先；其余取 default_permission。
    Agent/Agent Token（role=agent）按 scope 精确判定（2026-11-16 B02：WS 路径保留完整
    agent 身份，跨文档/越界一律 None——与 REST _principal_from_scope 同一判定语义）。"""
    if doc is None or principal is None:
        return None
    kind = principal.get("kind")
    role = principal.get("role")
    if kind == "agent" or (kind == "token" and role == "agent"):
        if principal.get("agent_scope_all"):
            return "write"
        return "write" if doc["doc_uid"] in (principal.get("agent_docs") or []) else None
    if role == "admin" and kind in ("session", "token"):
        return "write"
    if doc.get("kind") == "template":
        return "read"
    acl = db.get_conn().execute("SELECT permission FROM doc_acl WHERE doc_uid=? AND principal=?",
                                (doc["doc_uid"], principal.get("username", ""))).fetchone()
    # R08：全局角色是权限上限——全局降为 read 的成员，即使有 doc_acl=write 也立即失去写权
    rank_write = role in ("write", "admin")
    if acl and acl["permission"] == "write":
        return "write" if rank_write else "read"
    if acl:
        return "read"
    return doc.get("default_permission") or "read"


# ============================================================
# CRDT 持久化（SQLite YStore）
# ============================================================

def _next_seq(doc_uid):
    return db.get_conn().execute(
        "SELECT COALESCE(MAX(seq),0)+1 s FROM doc_updates WHERE doc_uid=?", (doc_uid,)).fetchone()["s"]


def _store_update_sync(doc_uid, data: bytes):
    """落一条 CRDT 增量。返回 (seq, sha256hex)；sha 供前端按更新精确确认（2026-11-16 B05）。"""
    sha = hashlib.sha256(data).hexdigest()
    with db._lock:
        seq = _next_seq(doc_uid)
        db.get_conn().execute("INSERT INTO doc_updates (doc_uid, seq, update_blob, update_sha, created_at)"
                              " VALUES (?,?,?,?,?)", (doc_uid, seq, data, sha, db.now()))
        db.get_conn().commit()
    return seq, sha


class _DocYStore(BaseYStore):
    """逐更新落主库 doc_updates；落库成功后推进房间 awareness.saved_seq。

    2026-11-16 B04：写失败不再被吞——更新进入**有序待刷队列**，由后台 flusher
    按 FIFO + 指数退避重试；saved_seq / saved_shas 仅在实际落库成功后推进。
    恢复一致性前前端不会收到"已保存"确认。"""

    def __init__(self, doc_uid, log=None):
        super().__init__(path=doc_uid, log=log)
        self.doc_uid = doc_uid
        self._pending = collections.deque()
        self._flush_ev = None      # asyncio.Event（loop 侧懒建）
        self._flush_task = None
        self._saved_shas = collections.deque(maxlen=64)

    def _ensure_flusher(self):
        if self._flush_task is None or self._flush_task.done():
            self._flush_ev = asyncio.Event()
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def write(self, data: bytes) -> None:
        self._pending.append(bytes(data))
        self._ensure_flusher()
        self._flush_ev.set()

    async def _flush_loop(self):
        delay = 0.2
        while True:
            if not self._pending:
                try:
                    await asyncio.wait_for(self._flush_ev.wait(), timeout=30)
                except asyncio.TimeoutError:
                    continue
                finally:
                    self._flush_ev.clear()
                continue
            data = self._pending[0]
            try:
                seq, sha = await asyncio.to_thread(_store_update_sync, self.doc_uid, data)
            except Exception:
                delay = min(delay * 2, 5.0)   # 指数退避，上限 5s；更新保留在队首
                await asyncio.sleep(delay)
                continue
            delay = 0.2
            self._pending.popleft()
            self._saved_shas.append(sha)
            _saved_seq_cache[self.doc_uid] = max(seq, _saved_seq_cache.get(self.doc_uid, 0))
            room = _room_by_uid.get(self.doc_uid)
            if room is not None and _loop is not None:
                with suppress(Exception):
                    room.awareness.set_local_state_field("saved_seq", seq)
                    room.awareness.set_local_state_field("saved_shas", list(self._saved_shas))

    async def flush_pending(self, timeout=10):
        """房间销毁前尽力把待刷更新落库（失败则放弃——不阻塞销毁，不虚报成功）。"""
        deadline = time.time() + timeout
        while self._pending and time.time() < deadline:
            try:
                seq, sha = await asyncio.to_thread(_store_update_sync, self.doc_uid,
                                                   self._pending[0])
            except Exception:
                await asyncio.sleep(0.3)
                continue
            self._pending.popleft()
            self._saved_shas.append(sha)
            _saved_seq_cache[self.doc_uid] = max(seq, _saved_seq_cache.get(self.doc_uid, 0))

    async def read(self):
        rows = await asyncio.to_thread(
            lambda: db.get_conn().execute(
                "SELECT update_blob FROM doc_updates WHERE doc_uid=? ORDER BY seq",
                (self.doc_uid,)).fetchall())
        for r in rows:
            yield r["update_blob"], None, time.time()


def _replay_doc(doc_uid, initial_md=""):
    d = Doc()
    rows = db.get_conn().execute(
        "SELECT update_blob FROM doc_updates WHERE doc_uid=? ORDER BY seq", (doc_uid,)).fetchall()
    for r in rows:
        d.apply_update(r["update_blob"])
    t = d.get(TEXT_FIELD, type=Text)
    if not rows and initial_md:
        t += initial_md
        upd = d.get_update()
        _store_update_sync(doc_uid, upd)   # 含 update_sha（B05：每行都有对账依据）
    return d


def _replay_text(doc_uid):
    doc = get_doc(doc_uid)
    if doc is None:
        raise DocError("文档不存在")
    d = _replay_doc(doc_uid, doc.get("initial_md") or "")
    return str(d.get(TEXT_FIELD, type=Text))


# ============================================================
# 房间构建 / WS 鉴权
# ============================================================

class _ReadOnlyChannel:
    """只读连接包装：丢弃其 SYNC_STEP2/SYNC_UPDATE（服务端永不应用只读用户的更新）。"""

    def __init__(self, inner):
        self._inner = inner
        self._closed = False

    @property
    def path(self):
        return self._inner.path

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            if self._closed:
                raise StopAsyncIteration()
            msg = await self._inner.recv()
            if msg and len(msg) >= 2 and msg[0] == int(YMessageType.SYNC) \
                    and msg[1] in (int(YSyncMessageType.SYNC_STEP2), int(YSyncMessageType.SYNC_UPDATE)):
                continue
            return msg

    async def send(self, message: bytes) -> None:
        await self._inner.send(message)

    async def recv(self) -> bytes:
        return await self.__anext__()

    async def close(self):
        self._closed = True
        inner = self._inner
        send = getattr(inner, "_send", None)
        if send:
            with suppress(Exception):
                await send({"type": "websocket.close", "code": 4403, "reason": "permission revoked"})


async def _do_force_close(ch):
    """实际关闭动作（必须已在服务事件循环上执行）。"""
    target = getattr(ch, "_inner", ch)
    if getattr(target, "_xintel_closed", False):
        return
    try:
        target._xintel_closed = True
    except Exception:
        pass
    send = getattr(target, "_send", None)
    if send is not None:
        with suppress(Exception):
            await send({"type": "websocket.close", "code": 4403, "reason": "permission revoked"})
    rc = getattr(ch, "close", None)
    if rc is not None:
        with suppress(Exception):
            await rc()


def _force_close_channel(ch):
    """R11：强制关闭必须提交到连接所属事件循环（uvicorn 主 loop）执行——
    跨线程直接对同一 IOCP socket 写 close 帧会触发 WinError 995 完成通知竞争，
    令 asyncio selector 抛 InvalidStateError 并杀死事件循环线程（Windows 实证：
    服务静默停摆、后续全部连接拒绝）。幂等：同一连接只调度一次。"""
    target = getattr(ch, "_inner", ch)
    if getattr(target, "_xintel_closed", False):
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is _loop:
        # 已在服务事件循环：直接调度（同 loop 内串行，无跨线程竞争）
        asyncio.ensure_future(_do_force_close(ch))
        return
    if _loop is not None and _loop.is_running():
        # 其余一切情形（测试 loop / 工作线程）：一律调度回服务 loop——
        # 跨 loop 直接操作同一 socket 是 R11 崩溃根因
        _loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_do_force_close(ch)))
        return
    # 服务 loop 不在（停机中）：就地尽力关闭
    with suppress(Exception):
        asyncio.run(_do_force_close(ch))


class _XServer(WebsocketServer):
    """按连接权限包装 channel；连接注册表供周期复核（退出/停用后 ≤30s 生效）。
    房间被自动清理时同步清理本模块映射（陈旧引用会让重连拿到已停止的房间）。"""

    async def serve(self, websocket):
        perm = _ws_perm.get() or "read"
        info = _ws_info.get()
        if info:
            # 2026-11-16 B03 修复：注册表键统一用 doc_uid（原先误用完整 path，
            # 周期复核 get_doc(完整路径) 恒 None——MIN_AGE=0 时会误关所有连接）
            uid = (websocket.path or "").rstrip("/").rsplit("/", 1)[-1]
            reg = _conn_registry.setdefault(uid, {})
            reg[id(websocket)] = {**info, "perm": perm, "channel": websocket, "at": time.time()}
        wrapped = websocket if perm == "write" else _ReadOnlyChannel(websocket)
        await super().serve(wrapped)

    async def delete_room(self, *, name=None, room=None):
        key = name
        if key is None and room is not None:
            key = next((k for k, v in self.rooms.items() if v is room), None)
        uid = key.rstrip("/").rsplit("/", 1)[-1] if key else None
        await super().delete_room(name=name, room=room)
        if uid:
            _room_by_uid.pop(uid, None)
            _rev_tasks.pop(uid, None)
        if key:
            _conn_registry.pop(key, None)


_ws_info: contextvars.ContextVar = contextvars.ContextVar("xintel_ws_info", default=None)


def _headers(scope):
    out = {}
    for k, v in scope.get("headers") or []:
        try:
            out[k.decode("latin-1").lower()] = v.decode("latin-1")
        except Exception:
            pass
    return out


def _origin_ok(scope, hdrs):
    origin = hdrs.get("origin")
    if not origin:
        return True  # 非浏览器客户端（Agent 走 REST，不依赖 WS）
    host = hdrs.get("host", "")
    try:
        from urllib.parse import urlparse
        o = urlparse(origin)
        oh = o.netloc
    except Exception:
        return False
    return (not oh) or oh == host


def _principal_from_ws(scope, hdrs, doc_uid=None):
    """WS 鉴权：会话 Cookie > 一次性 ticket > Bearer Token。返回 (principal, info, perm_override)。
    2026-11-16 B01：ticket 消费绑定目标文档（跨文档票据直接失效）。"""
    from . import auth
    # 1) 会话
    cookies = {}
    for part in (hdrs.get("cookie") or "").split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            cookies[k.strip()] = v.strip()
    sid = cookies.get(auth.COOKIE_NAME, "")
    s = auth.get_session(sid)
    if s:
        role = auth.user_role(s["username"])
        return ({"username": s["username"], "role": role, "kind": "session"},
                {"via": "session", "sid": sid, "username": s["username"]}, None)
    # 2) 一次性 ticket（query 参数）
    try:
        from urllib.parse import parse_qs
        qs = parse_qs(scope.get("query_string", b"").decode("utf-8", "ignore"))
        ticket = (qs.get("ticket") or [""])[0]
    except Exception:
        ticket = ""
    if ticket:
        # B01：消费时绑定目标文档——A 文档的票据连 B 文档直接失效
        t = consume_ticket(ticket, doc_uid)
        if t:
            p = t.get("principal") or {}
            # R08：ticket 只作一次性身份凭证——签发后降级/注销/吊销必须即时失效：
            # 会话票据须 sid 仍有效；Token 票据须未吊销；角色一律按 auth_users 现算，
            # 不使用签发时的 role 快照。
            sid = p.get("sid") or ""
            token_id = p.get("token_id")
            kind = p.get("kind", "session")
            username = p.get("username", "")
            if kind == "token" and token_id is not None:
                row = db.get_conn().execute(
                    "SELECT name, role FROM auth_tokens WHERE id=? AND revoked=0",
                    (int(token_id),)).fetchone()
                if not row:
                    return None, None, None
                username = row["name"]
            elif kind == "session" and sid:
                if not auth.get_session(sid):
                    return None, None, None
            role = auth.user_role(username) or ""
            if not role:
                return None, None, None
            principal = {"username": username, "role": role, "kind": kind}
            return (principal,
                    {"via": "ticket", "username": username},
                    None)
    # 3) Bearer（便于测试与内部工具）——2026-11-16 B02：保留完整 agent 身份与 token id，
    #    权限仍由 doc_permission 在连接时按当前 ACL/scope 计算（不再透传快照权限）。
    a = hdrs.get("authorization", "")
    if a.lower().startswith("bearer "):
        p = auth.verify_token(a[7:].strip())
        if p:
            principal = {"username": p["name"], "role": p["role"], "kind": "token"}
            for k in ("agent_scope", "agent_scope_all", "agent_docs"):
                if k in p:
                    principal[k] = p[k]
            info = {"via": "token", "username": p["name"], "token_name": p["name"],
                    "token_id": p.get("token_id")}
            if "agent_docs" in p:
                info["agent_docs"] = list(p["agent_docs"])
                info["agent_scope_all"] = bool(p.get("agent_scope_all"))
            return principal, info, None
    return None, None, None


async def _on_connect(msg, scope) -> bool:
    """True = 拒绝连接。鉴权 + 权限计算 + 房间构建（全部通过才 accept）。"""
    import os as _os
    _dbg = _os.environ.get("XINTEL_WS_DEBUG") == "1"
    if _dbg:
        print("[ws] on_connect path=", scope.get("path"), flush=True)
    if _stopping or _server is None or not _server.running:
        if _dbg:
            print("[ws] reject: server not running", flush=True)
        return True
    # Starlette Mount 传完整路径（/ws/docs/<uid>）；直接驱动 ASGI 时为 /<uid>——统一取末段
    doc_uid = (scope.get("path") or "").rstrip("/").rsplit("/", 1)[-1]
    hdrs = _headers(scope)
    if not doc_uid or not _origin_ok(scope, hdrs):
        if _dbg:
            print("[ws] reject: path/origin", doc_uid, flush=True)
        return True
    doc = get_doc(doc_uid)
    if doc is None:
        print("[ws] reject: no doc", doc_uid, flush=True)
        return True
    principal, info, perm_override = _principal_from_ws(scope, hdrs, doc_uid)
    if principal is None:
        print("[ws] reject: no principal（无会话/ticket/token）", flush=True)
        return True
    # 2026-11-16 B01/B03：权限一律在连接时按当前身份与 ACL 现算——
    # ticket 只作身份凭证（一次性、绑定目标文档），不再透传签发时的权限快照。
    perm = doc_permission(doc, principal)
    if not perm:
        print("[ws] reject: no perm", principal.get("username"), flush=True)
        return True
    info = dict(info or {})
    info["doc_uid"] = doc_uid
    info["principal"] = principal
    info["connected_perm"] = perm
    try:
        await _ensure_room(doc_uid, doc, scope_path=scope.get("path"))
    except Exception:
        import traceback
        traceback.print_exc()   # 服务端拒绝必须留痕（2026-11-14：静默拒绝导致排障困难）
        return True
    print("[ws] accept", doc_uid, perm, principal.get("username"), flush=True)
    _ws_perm.set(perm)
    _ws_info.set(info)
    return False  # accept


def _build_doc_sync(doc_uid, doc_row):
    """线程池内执行：回放/初始化 + 返回 (ydoc, store)。持 _doc_mutex。"""
    with _mutex(doc_uid):
        d = _replay_doc(doc_uid, doc_row.get("initial_md") or "")
        store = _DocYStore(doc_uid)
        rows_n = db.get_conn().execute("SELECT COUNT(*) n FROM doc_updates WHERE doc_uid=?",
                                       (doc_uid,)).fetchone()["n"]
        _saved_seq_cache[doc_uid] = max(_saved_seq_cache.get(doc_uid, 0), rows_n)
        return d, store


async def _ensure_room(doc_uid, doc_row, scope_path=None):
    if doc_uid in _room_by_uid:
        return _room_by_uid[doc_uid]
    lock = _build_locks.setdefault(doc_uid, asyncio.Lock())
    async with lock:
        if doc_uid in _room_by_uid:
            return _room_by_uid[doc_uid]
        ydoc, store = await asyncio.to_thread(_build_doc_sync, doc_uid, doc_row)
        room = YRoom(ydoc=ydoc, ystore=store)
        with _mutex(doc_uid):
            if doc_uid in _room_by_uid:      # 与离线受控修改竞态时以已注册者为准
                return _room_by_uid[doc_uid]
            _room_by_uid[doc_uid] = room
            # WebsocketServer 以 scope 的完整 path 查房（/ws/docs/<uid>）——键必须一致
            _server.rooms[scope_path or doc_uid] = room
        await _server.start_room(room)
        seq = _saved_seq_cache.get(doc_uid, 0)
        with suppress(Exception):
            room.awareness.set_local_state({"saved_seq": seq, "role": "server"})
        _rev_tasks[doc_uid] = asyncio.create_task(_revision_loop(doc_uid, room))
        return room


def invalidate_room(doc_uid):
    """模板/文档初始化变更后调用：无活动房间则无操作；有房间时其内容以 CRDT 为准。"""
    return doc_uid in _room_by_uid


# ============================================================
# 修订快照
# ============================================================

def _save_revision_sync(doc_uid, body, kind="save", created_by="system", base_rev=None,
                        skip_same=True):
    with db._lock:
        c = db.get_conn()
        last = c.execute("SELECT rev, content_hash FROM doc_revisions WHERE doc_uid=?"
                         " ORDER BY rev DESC LIMIT 1", (doc_uid,)).fetchone()
        h = _hash_md(body)
        if skip_same and last is not None and last["content_hash"] == h and kind == "save":
            rev = last["rev"]
        else:
            rev = (last["rev"] if last else 0) + 1
            c.execute("INSERT INTO doc_revisions (doc_uid, rev, title, body_md, revision_kind,"
                      " base_rev, content_hash, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      (doc_uid, rev, (get_doc(doc_uid) or {}).get("title", ""), body, kind,
                       base_rev, h, created_by, db.now()))
            # 2026-11-16 B07：内容变化使既有"已通过"失效——回落编辑中（可重新提交审阅）；
            # review_* 字段保留为历史决定。submit_review/export 等非内容类修订不触发。
            if kind in ("save", "agent_edit", "suggest_apply"):
                c.execute("UPDATE doc_documents SET status='编辑中' WHERE doc_uid=? AND status='已通过'",
                          (doc_uid,))
        c.execute("UPDATE doc_documents SET updated_at=? WHERE doc_uid=?", (db.now(), doc_uid))
        c.commit()
    if kind == "save":
        from . import writing
        with suppress(Exception):
            writing.record_candidate_status_change_by_doc(doc_uid, "写作中", "system")
    return rev


async def _revision_loop(doc_uid, room):
    last_hash = None
    try:
        while not _stopping and _room_by_uid.get(doc_uid) is room \
                and room in _server.rooms.values():
            await asyncio.sleep(3)
            try:
                text = str(room.ydoc.get(TEXT_FIELD, type=Text))
            except Exception:
                continue
            h = _hash_md(text)
            if h != last_hash:
                last_hash = h
                await asyncio.to_thread(_save_revision_sync, doc_uid, text, "save", "system")
    finally:
        # 房间移除后做最终快照（尽力而为；更新日志本身已逐条落库）
        if not _stopping and _room_by_uid.get(doc_uid) is not room:
            with suppress(Exception):
                text = str(room.ydoc.get(TEXT_FIELD, type=Text))
                await asyncio.to_thread(_save_revision_sync, doc_uid, text, "save", "system")


# ============================================================
# 跨线程读取 / 受控修改
# ============================================================

def current_text(doc_uid) -> str:
    """当前协作正文（房间活动→loop 线程读；否则离线回放）。REST 线程可调用。"""
    room = _room_by_uid.get(doc_uid)
    if room is not None and _loop is not None and _loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(_read_text(room), _loop)
        return fut.result(timeout=15)
    return _replay_text(doc_uid)


async def _read_text(room) -> str:
    return str(room.ydoc.get(TEXT_FIELD, type=Text))


def _find_unique(s: str, anchor: str) -> int:
    if not anchor:
        raise AnchorError(0, "锚点为空")
    first = s.find(anchor)
    if first < 0:
        raise AnchorError(0, "锚点在当前正文中零匹配")
    if s.find(anchor, first + 1) >= 0:
        raise AnchorError(2, "锚点在当前正文中多处匹配，拒绝猜测")
    return first


def _apply_on_text(doc: Doc, t: Text, s: str, i: int, old: str, new: str) -> str:
    """字节边界精确替换（pycrdt Text 索引 = UTF-8 字节偏移）。"""
    b_i = len(s[:i].encode("utf-8"))
    b_j = b_i + len(old.encode("utf-8"))
    with doc.transaction():
        del t[b_i:b_j]
        t.insert(b_i, new)
    return str(t)


def restore_text(doc_uid, new_text, kind="restore", operator="system"):
    """历史恢复专用：整体替换当前正文（含空正文场景——B20：apply_text_replace 的
    锚点为空 422 问题）。CRDT 事务内 del+insert，修订留痕 kind=restore。"""
    room = _room_by_uid.get(doc_uid)
    if room is not None and _loop is not None and _loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(
            _restore_live(room, doc_uid, new_text), _loop)
        full = fut.result(timeout=30)
    else:
        with _mutex(doc_uid):
            if _room_by_uid.get(doc_uid) is not None and _loop is not None and _loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(
                    _restore_live(_room_by_uid[doc_uid], doc_uid, new_text), _loop)
                full = fut.result(timeout=30)
            else:
                d = _replay_doc(doc_uid, (get_doc(doc_uid) or {}).get("initial_md") or "")
                t = d.get(TEXT_FIELD, type=Text)
                with d.transaction():
                    del t[0:len(str(t).encode("utf-8"))]
                    t.insert(0, new_text)
                upd = d.get_update()
                _store_update_sync(doc_uid, upd)
                full = str(t)
    rev = _save_revision_sync(doc_uid, full, kind, operator, skip_same=False)
    return {"rev": rev, "text": full}


async def _restore_live(room, doc_uid, new_text):
    t = room.ydoc.get(TEXT_FIELD, type=Text)
    with room.ydoc.transaction():
        del t[0:len(str(t).encode("utf-8"))]
        t.insert(0, new_text)
    return str(t)


def apply_text_replace(doc_uid, old_text, new_text, kind="agent_edit", operator="system",
                       base_rev_expected=None):
    """受控局部修改：锚点唯一匹配 + base_rev 乐观锁 + 修订留痕。线程安全。

    R10：版本检查、锚点匹配、CRDT 变更、update 落库、修订写入在**同一临界区**
    原子完成（loop 协程内全程同步、或离线路径持 _mutex + db._lock）——
    两个不同 client_id 共用旧 base_rev 的并发请求，只有一个成功，另一个明确 409。"""
    room = _room_by_uid.get(doc_uid)
    if room is not None and _loop is not None and _loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(
            _apply_replace_live(room, doc_uid, old_text, new_text, base_rev_expected, kind, operator),
            _loop)
        return fut.result(timeout=30)
    with _mutex(doc_uid):
        room = _room_by_uid.get(doc_uid)
        if room is not None and _loop is not None and _loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(
                _apply_replace_live(room, doc_uid, old_text, new_text, base_rev_expected, kind, operator),
                _loop)
            return fut.result(timeout=30)
        # 离线路径：先回放（_replay_doc 的 seed 分支内部会取 db._lock——必须在进入
        # 本函数的 db._lock 临界区**之前**完成，否则非重入死锁，workbench W4 实证）；
        # 随后 冲突检查+变更+落库 在 db._lock 内原子完成。
        d = _replay_doc(doc_uid, (get_doc(doc_uid) or {}).get("initial_md") or "")
        t = d.get(TEXT_FIELD, type=Text)
        s_txt = str(t)
        i = _find_unique(s_txt, old_text)
        with db._lock:
            latest = db.get_conn().execute(
                "SELECT rev FROM doc_revisions WHERE doc_uid=? ORDER BY rev DESC LIMIT 1",
                (doc_uid,)).fetchone()
            cur = latest["rev"] if latest else 0
            if base_rev_expected is not None and int(base_rev_expected) != int(cur):
                raise RevisionConflict(cur)
            b_i = len(s_txt[:i].encode("utf-8"))
            b_j = b_i + len(old_text.encode("utf-8"))
            with d.transaction():
                del t[b_i:b_j]
                t.insert(b_i, new_text)
            upd = d.get_update()
            seq = db.get_conn().execute(
                "SELECT COALESCE(MAX(seq),0)+1 s FROM doc_updates WHERE doc_uid=?",
                (doc_uid,)).fetchone()["s"]
            sha = hashlib.sha256(upd).hexdigest()
            db.get_conn().execute(
                "INSERT INTO doc_updates (doc_uid, seq, update_blob, update_sha, created_at)"
                " VALUES (?,?,?,?,?)", (doc_uid, seq, upd, sha, db.now()))
            rev = cur + 1
            full = str(t)
            db.get_conn().execute(
                "INSERT INTO doc_revisions (doc_uid, rev, title, body_md, revision_kind,"
                " base_rev, content_hash, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (doc_uid, rev, (get_doc(doc_uid) or {}).get("title", ""), full, kind,
                 base_rev_expected, _hash_md(full), operator, db.now()))
            if kind in ("save", "agent_edit", "suggest_apply"):
                db.get_conn().execute(
                    "UPDATE doc_documents SET status='编辑中' WHERE doc_uid=? AND status='已通过'",
                    (doc_uid,))
            db.get_conn().execute("UPDATE doc_documents SET updated_at=? WHERE doc_uid=?",
                                  (db.now(), doc_uid))
            db.get_conn().commit()
        if kind == "save":
            from . import writing
            with suppress(Exception):
                writing.record_candidate_status_change_by_doc(doc_uid, "写作中", "system")
        return {"rev": rev, "text": full}


async def _apply_replace_live(room, doc_uid, old_text, new_text, base_rev_expected, kind, operator):
    """R10：loop 协程内全程同步（无 await）——与 WS 写、修订循环天然串行；
    再持 db._lock 覆盖修订循环的跨线程写入 → 版本检查与应用真正原子。"""
    t = room.ydoc.get(TEXT_FIELD, type=Text)
    with db._lock:
        latest = db.get_conn().execute(
            "SELECT rev FROM doc_revisions WHERE doc_uid=? ORDER BY rev DESC LIMIT 1",
            (doc_uid,)).fetchone()
        cur = latest["rev"] if latest else 0
        if base_rev_expected is not None and int(base_rev_expected) != int(cur):
            raise RevisionConflict(cur)
        s_txt = str(t)
        i = _find_unique(s_txt, old_text)
        b_i = len(s_txt[:i].encode("utf-8"))
        b_j = b_i + len(old_text.encode("utf-8"))
        sv = room.ydoc.get_state()
        with room.ydoc.transaction():
            del t[b_i:b_j]
            t.insert(b_i, new_text)
        upd = room.ydoc.get_update(sv)
        full = str(t)
        seq = db.get_conn().execute(
            "SELECT COALESCE(MAX(seq),0)+1 s FROM doc_updates WHERE doc_uid=?",
            (doc_uid,)).fetchone()["s"]
        sha = hashlib.sha256(upd).hexdigest()
        db.get_conn().execute(
            "INSERT INTO doc_updates (doc_uid, seq, update_blob, update_sha, created_at)"
            " VALUES (?,?,?,?,?)", (doc_uid, seq, upd, sha, db.now()))
        rev = cur + 1
        db.get_conn().execute(
            "INSERT INTO doc_revisions (doc_uid, rev, title, body_md, revision_kind,"
            " base_rev, content_hash, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (doc_uid, rev, (get_doc(doc_uid) or {}).get("title", ""), full, kind,
             base_rev_expected, _hash_md(full), operator, db.now()))
        # B07：内容变化使既有"已通过"失效——回落编辑中（review_* 保留为历史决定）
        if kind in ("save", "agent_edit", "suggest_apply"):
            db.get_conn().execute(
                "UPDATE doc_documents SET status='编辑中' WHERE doc_uid=? AND status='已通过'",
                (doc_uid,))
        db.get_conn().execute("UPDATE doc_documents SET updated_at=? WHERE doc_uid=?",
                              (db.now(), doc_uid))
        db.get_conn().commit()
    if kind == "save":
        from . import writing
        with suppress(Exception):
            writing.record_candidate_status_change_by_doc(doc_uid, "写作中", "system")
    # 落库成功后推进确认游标（与 YStore 相同语义）
    _saved_seq_cache[doc_uid] = max(seq, _saved_seq_cache.get(doc_uid, 0))
    store = getattr(room, "ystore", None)
    if store is not None and hasattr(store, "_saved_shas"):
        store._saved_shas.append(sha)
    with suppress(Exception):
        room.awareness.set_local_state_field("saved_seq", seq)
        room.awareness.set_local_state_field("saved_shas", list(store._saved_shas))
    return {"rev": rev, "text": full}


# ============================================================
# 审阅 / 建议 / 工单
# ============================================================

def submit_review(doc_uid, operator):
    doc = get_doc(doc_uid)
    if not doc:
        raise DocError("文档不存在")
    if doc["kind"] != "report":
        raise DocError("模板正文无审阅流程")
    if doc["status"] not in ("编辑中",):
        raise ValueError(f"当前状态 {doc['status']} 不可提交审阅")
    text = current_text(doc_uid)
    rev = _save_revision_sync(doc_uid, text, "submit_review", operator, skip_same=False)
    with db._lock:
        db.get_conn().execute(
            "UPDATE doc_documents SET status='待审阅', review_rev=?, updated_at=? WHERE doc_uid=?",
            (rev, db.now(), doc_uid))
        db.get_conn().commit()
    return rev


def review_doc(doc_uid, decision, note, operator):
    doc = get_doc(doc_uid)
    if not doc:
        raise DocError("文档不存在")
    if doc["status"] != "待审阅":
        raise ValueError("文档不在待审阅状态")
    if decision not in ("approved", "rejected"):
        raise ValueError("decision 须为 approved/rejected")
    with db._lock:
        db.get_conn().execute(
            "UPDATE doc_documents SET status=?, review_decision=?, review_note=?, review_by=?,"
            " review_at=?, updated_at=? WHERE doc_uid=?",
            ("已通过" if decision == "approved" else "编辑中", decision, note or "",
             operator, db.now(), db.now(), doc_uid))
        db.get_conn().commit()
    return True


def create_suggestion(doc_uid, agent, old_text, new_text, rationale="", sources=None,
                      target="", patch_kind="replace", client_id="", base_rev=None,
                      operator=None):
    """修改建议：创建时即校验锚点唯一匹配与版本；client_id 幂等。"""
    doc = get_doc(doc_uid)
    if not doc:
        raise DocError("文档不存在")
    if client_id:
        exist = db.get_conn().execute(
            "SELECT * FROM doc_suggestions WHERE doc_uid=? AND client_id=?",
            (doc_uid, client_id)).fetchone()
        if exist:
            return dict(exist), True
    latest = latest_revision(doc_uid)
    cur_rev = latest["rev"] if latest else 0
    if base_rev is None:
        base_rev = cur_rev
    if int(base_rev) != int(cur_rev):
        raise RevisionConflict(cur_rev)
    text = current_text(doc_uid)
    _find_unique(text, old_text)   # 零/多匹配直接失败
    uid = _uid("sug_")
    with db._lock:
        c = db.get_conn()
        c.execute(
            "INSERT INTO doc_suggestions (doc_uid, suggestion_uid, base_rev, agent, target,"
            " rationale, sources, patch_kind, anchor, old_text, new_text, client_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_uid, uid, int(base_rev), agent, target or "", rationale or "",
             json.dumps(sources or [], ensure_ascii=False), patch_kind,
             old_text, old_text, new_text, client_id, db.now()))
        c.commit()
        row = c.execute("SELECT * FROM doc_suggestions WHERE suggestion_uid=?", (uid,)).fetchone()
    return dict(row), False


def list_suggestions(doc_uid, status=""):
    w, args = " WHERE doc_uid=?", [doc_uid]
    if status:
        w += " AND status=?"
        args.append(status)
    return [dict(r) for r in db.get_conn().execute(
        f"SELECT * FROM doc_suggestions{w} ORDER BY id DESC LIMIT 200", args).fetchall()]


def resolve_suggestion(doc_uid, sid, decision, operator):
    """接受=锚点复检+应用（kind=suggest_apply）；拒绝=仅标记。冲突时标记 conflict。"""
    row = db.get_conn().execute("SELECT * FROM doc_suggestions WHERE id=? AND doc_uid=?",
                                (int(sid), doc_uid)).fetchone()
    if not row:
        raise DocError("建议不存在")
    if row["status"] != "pending":
        raise ValueError(f"建议已处理（{row['status']}）")
    if decision == "rejected":
        with db._lock:
            db.get_conn().execute(
                "UPDATE doc_suggestions SET status='rejected', reviewed_by=?, reviewed_at=? WHERE id=?",
                (operator, db.now(), int(sid)))
            db.get_conn().commit()
        return {"status": "rejected"}
    try:
        r = apply_text_replace(doc_uid, row["old_text"], row["new_text"],
                               kind="suggest_apply", operator=operator,
                               base_rev_expected=row["base_rev"])
    except AnchorError as e:
        with db._lock:
            db.get_conn().execute(
                "UPDATE doc_suggestions SET status='conflict', reviewed_by=?, reviewed_at=? WHERE id=?",
                (operator, db.now(), int(sid)))
            db.get_conn().commit()
        raise DocError(f"建议应用冲突：{e}") from e
    with db._lock:
        db.get_conn().execute(
            "UPDATE doc_suggestions SET status='accepted', reviewed_by=?, reviewed_at=? WHERE id=?",
            (operator, db.now(), int(sid)))
        db.get_conn().commit()
    return {"status": "accepted", "rev": r["rev"]}


# ============================================================
# WS 工单（一次性）
# ============================================================

def create_ticket(doc_uid, principal, permission, ttl=60):
    """principal 为认证主体 dict；返回一次性明文 ticket（60s）。
    ticket 绑定目标 doc_uid——消费时校验匹配，禁止跨文档复用（2026-11-16 B01）。"""
    info = {"principal": principal, "principal_name": principal.get("username", ""),
            "permission": permission, "doc_uid": doc_uid}
    tk = secrets.token_urlsafe(24)
    with db._lock:
        db.get_conn().execute(
            "INSERT INTO ws_tickets (ticket, doc_uid, principal, permission, used, expires_at, created_at)"
            " VALUES (?,?,?,?,0,?,?)",
            (tk, doc_uid, json.dumps(info, ensure_ascii=False), permission,
             time.time() + ttl, db.now()))
        db.get_conn().commit()
    return tk


def consume_ticket(ticket, doc_uid=None):
    """消费一次性 ticket。doc_uid 提供时校验票据绑定文档——跨文档复用返回 None（B01）。
    返回的 info 不再携带可透传的权限（连接时由 doc_permission 现算，B03）。"""
    with db._lock:
        r = db.get_conn().execute("SELECT * FROM ws_tickets WHERE ticket=?", (ticket,)).fetchone()
        if not r or r["used"] or float(r["expires_at"] or 0) < time.time():
            return None
        db.get_conn().execute("UPDATE ws_tickets SET used=1 WHERE ticket=?", (ticket,))
        db.get_conn().commit()
    if doc_uid is not None and r["doc_uid"] and r["doc_uid"] != doc_uid:
        return None   # 票据签发给另一文档：拒绝（B01）
    try:
        info = json.loads(r["principal"])
    except Exception:
        return None
    info = dict(info)
    info["principal_name"] = info.get("principal", {}).get("username", "")
    return info


# ============================================================
# 生命周期
# ============================================================

def _exception_handler(exception, log):
    log.error("doccollab exception: %s: %s", type(exception).__name__, exception)
    return True


async def startup():
    """lifespan 启动：记录 loop、启动 WS 服务。"""
    global _loop, _server_task, _stopping
    _stopping = False
    _loop = asyncio.get_running_loop()
    _server_task = asyncio.create_task(_server.start())
    await _server.started.wait()
    asyncio.create_task(_cleanup_loop())


async def shutdown():
    global _stopping, _server_task
    _stopping = True
    if _server_task is not None:
        with suppress(Exception):
            await asyncio.wait_for(_server_task, timeout=5)
    _server_task = None


async def _cleanup_loop():
    """周期任务：过期 ticket 清理 + 连接权限复核（≤30s 生效）。"""
    while not _stopping:
        await asyncio.sleep(15)
        try:
            await _cleanup_once()
        except Exception:
            pass


async def _cleanup_once():
    """单轮清理（可测试直接调用）：过期 ticket 清理 + 连接权限复核。"""
    if True:
        try:
            await asyncio.to_thread(
                lambda: db.get_conn().execute("DELETE FROM ws_tickets WHERE expires_at<?",
                                              (time.time() - 3600,)) and db.get_conn().commit())
        except Exception:
            pass
        for doc_uid, reg in list(_conn_registry.items()):
            for cid, info in list(reg.items()):
                if time.time() - info.get("at", 0) < _CLEANUP_MIN_AGE:
                    continue
                info = {**info, "at": time.time()}
                reg[cid] = info
                from . import auth
                p = info.get("principal") or {}
                doc = get_doc(doc_uid)
                perm_now = None
                # 2026-11-16 B03：按连接方式重新验证身份与权限——
                #   session：sid 必须仍有效（登出/踢出即断）；token：按 id 复核 revoked；
                #   ticket：按用户名+当前 ACL 复核。权限复核统一走 doc_permission 现算。
                if doc is None:
                    ok = False
                elif info.get("via") == "session" and p.get("username"):
                    sid = info.get("sid") or ""
                    ok = bool(auth.get_session(sid))
                    if ok:
                        role = auth.user_role(p["username"]) or ""
                        perm_now = doc_permission(doc, {"username": p["username"], "role": role,
                                                        "kind": "session"})
                        ok = bool(perm_now)
                elif info.get("via") == "token" and p.get("username"):
                    tid = info.get("token_id")
                    if tid is not None:
                        # 不重放原始密钥——按 id 复核未吊销，再用存储的 scope 重算权限
                        row = db.get_conn().execute(
                            "SELECT name, role, scope FROM auth_tokens WHERE id=? AND revoked=0",
                            (int(tid),)).fetchone()
                        if not row:
                            ok = False
                        else:
                            principal = {"username": row["name"], "role": row["role"], "kind": "token"}
                            if row["role"] == "agent":
                                scope_all = (row["scope"] or "") == "all"
                                principal["agent_scope_all"] = scope_all
                                principal["agent_docs"] = [] if scope_all else [
                                    x for x in (row["scope"] or "").split(",") if x]
                            perm_now = doc_permission(doc, principal)
                            ok = bool(perm_now)
                    else:
                        ok = False
                else:
                    # ticket 身份：按用户名与当前 ACL 复核
                    ok = bool(auth.user_role(p.get("username", "")))
                    if ok:
                        role = auth.user_role(p.get("username", "")) or ""
                        perm_now = doc_permission(doc, {"username": p.get("username", ""), "role": role,
                                                        "kind": "session"})
                        ok = bool(perm_now)
                # 写连接降级/失效 → 真实关闭（读连接降读不中断阅读）
                if ok and info.get("connected_perm") == "write" and perm_now != "write":
                    ok = False
                if not ok:
                    ch = info.get("channel")
                    if ch is not None:
                        with suppress(Exception):
                            await _force_close_channel(ch)
                    reg.pop(cid, None)


# 供 main.py 挂载的 ASGI 应用（模块导入末尾构造，保证 import 时可用）
_server = _XServer(exception_handler=_exception_handler)
_asgi = ASGIServer(_server, on_connect=_on_connect)


def asgi_app():
    return _asgi
