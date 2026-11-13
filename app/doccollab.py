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
    规则：admin 恒 write；模板正文仅 admin 可写；显式 ACL 优先；其余取 default_permission。"""
    if doc is None or principal is None:
        return None
    if principal.get("kind") == "agent":
        return "write" if principal.get("agent_scope_all") or doc["doc_uid"] in (principal.get("agent_docs") or []) else None
    if principal.get("role") == "admin":
        return "write"
    if doc.get("kind") == "template":
        return "read"
    acl = db.get_conn().execute("SELECT permission FROM doc_acl WHERE doc_uid=? AND principal=?",
                                (doc["doc_uid"], principal.get("username", ""))).fetchone()
    if acl:
        return acl["permission"]
    return doc.get("default_permission") or "read"


# ============================================================
# CRDT 持久化（SQLite YStore）
# ============================================================

def _next_seq(doc_uid):
    return db.get_conn().execute(
        "SELECT COALESCE(MAX(seq),0)+1 s FROM doc_updates WHERE doc_uid=?", (doc_uid,)).fetchone()["s"]


def _store_update_sync(doc_uid, data: bytes) -> int:
    with db._lock:
        seq = _next_seq(doc_uid)
        db.get_conn().execute("INSERT INTO doc_updates (doc_uid, seq, update_blob, created_at)"
                              " VALUES (?,?,?,?)", (doc_uid, seq, data, db.now()))
        db.get_conn().commit()
    return seq


class _DocYStore(BaseYStore):
    """逐更新落主库 doc_updates；落库成功后推进房间 awareness.saved_seq。"""

    def __init__(self, doc_uid, log=None):
        super().__init__(path=doc_uid, log=log)
        self.doc_uid = doc_uid

    async def write(self, data: bytes) -> None:
        try:
            seq = await asyncio.to_thread(_store_update_sync, self.doc_uid, data)
        except Exception:
            return
        _saved_seq_cache[self.doc_uid] = max(seq, _saved_seq_cache.get(self.doc_uid, 0))
        room = _room_by_uid.get(self.doc_uid)
        if room is not None and _loop is not None:
            with suppress(Exception):
                room.awareness.set_local_state_field("saved_seq", seq)

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
        with db._lock:
            db.get_conn().execute("INSERT INTO doc_updates (doc_uid, seq, update_blob, created_at)"
                                  " VALUES (?,1,?,?)", (doc_uid, upd, db.now()))
            db.get_conn().commit()
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

    @property
    def path(self):
        return self._inner.path

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
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
        inner = self._inner
        send = getattr(inner, "_send", None)
        if send:
            with suppress(Exception):
                await send({"type": "websocket.close", "code": 4403, "reason": "permission revoked"})


class _XServer(WebsocketServer):
    """按连接权限包装 channel；连接注册表供周期复核（退出/停用后 ≤30s 生效）。
    房间被自动清理时同步清理本模块映射（陈旧引用会让重连拿到已停止的房间）。"""

    async def serve(self, websocket):
        perm = _ws_perm.get() or "read"
        info = _ws_info.get()
        if info:
            reg = _conn_registry.setdefault(websocket.path, {})
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


def _principal_from_ws(scope, hdrs):
    """WS 鉴权：会话 Cookie > 一次性 ticket > Bearer Token。返回 (principal, info, perm_override)。"""
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
        t = consume_ticket(ticket)
        if t:
            return (t["principal"], {"via": "ticket", "username": t.get("principal_name", "")},
                    t["permission"])
    # 3) Bearer（便于测试与内部工具）
    a = hdrs.get("authorization", "")
    if a.lower().startswith("bearer "):
        p = auth.verify_token(a[7:].strip())
        if p:
            return ({"username": p["name"], "role": p["role"], "kind": "token"},
                    {"via": "token", "username": p["name"]}, None)
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
    principal, info, perm_override = _principal_from_ws(scope, hdrs)
    if principal is None:
        print("[ws] reject: no principal（无会话/ticket/token）", flush=True)
        return True
    perm = perm_override or doc_permission(doc, principal)
    if not perm:
        print("[ws] reject: no perm", principal.get("username"), flush=True)
        return True
    info = dict(info or {})
    info["doc_uid"] = doc_uid
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


def apply_text_replace(doc_uid, old_text, new_text, kind="agent_edit", operator="system",
                       base_rev_expected=None):
    """受控局部修改：锚点唯一匹配 + base_rev 乐观锁 + 修订留痕。线程安全。"""
    if base_rev_expected is not None:
        latest = latest_revision(doc_uid)
        cur = latest["rev"] if latest else 0
        if int(base_rev_expected) != int(cur):
            raise RevisionConflict(cur)
    room = _room_by_uid.get(doc_uid)
    if room is not None and _loop is not None and _loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(
            _apply_replace_live(room, doc_uid, old_text, new_text), _loop)
        full, matched = fut.result(timeout=30)
    else:
        with _mutex(doc_uid):
            if _room_by_uid.get(doc_uid) is not None and _loop is not None and _loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(
                    _apply_replace_live(_room_by_uid[doc_uid], doc_uid, old_text, new_text), _loop)
                full, matched = fut.result(timeout=30)
            else:
                d = _replay_doc(doc_uid, (get_doc(doc_uid) or {}).get("initial_md") or "")
                t = d.get(TEXT_FIELD, type=Text)
                s = str(t)
                i = _find_unique(s, old_text)
                full = _apply_on_text(d, t, s, i, old_text, new_text)
                upd = d.get_update()
                _store_update_sync(doc_uid, upd)
                matched = True
    rev = _save_revision_sync(doc_uid, full, kind, operator)
    return {"rev": rev, "text": full}


async def _apply_replace_live(room, doc_uid, old_text, new_text):
    t = room.ydoc.get(TEXT_FIELD, type=Text)
    s = str(t)
    i = _find_unique(s, old_text)
    full = _apply_on_text(room.ydoc, t, s, i, old_text, new_text)
    return full, True


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
    """principal 为认证主体 dict；返回一次性明文 ticket（60s）。"""
    info = {"principal": principal, "principal_name": principal.get("username", ""),
            "permission": permission}
    tk = secrets.token_urlsafe(24)
    with db._lock:
        db.get_conn().execute(
            "INSERT INTO ws_tickets (ticket, doc_uid, principal, permission, used, expires_at, created_at)"
            " VALUES (?,?,?,?,0,?,?)",
            (tk, doc_uid, json.dumps(info, ensure_ascii=False), permission,
             time.time() + ttl, db.now()))
        db.get_conn().commit()
    return tk


def consume_ticket(ticket):
    with db._lock:
        r = db.get_conn().execute("SELECT * FROM ws_tickets WHERE ticket=?", (ticket,)).fetchone()
        if not r or r["used"] or float(r["expires_at"] or 0) < time.time():
            return None
        db.get_conn().execute("UPDATE ws_tickets SET used=1 WHERE ticket=?", (ticket,))
        db.get_conn().commit()
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
            await asyncio.to_thread(
                lambda: db.get_conn().execute("DELETE FROM ws_tickets WHERE expires_at<?",
                                              (time.time() - 3600,)) and db.get_conn().commit())
        except Exception:
            pass
        for doc_uid, reg in list(_conn_registry.items()):
            for cid, info in list(reg.items()):
                if time.time() - info.get("at", 0) < 30:
                    continue
                info = {**info, "at": time.time()}
                reg[cid] = info
                from . import auth
                p = info.get("principal") or {}
                ok = False
                if info.get("via") == "session" and p.get("username"):
                    role = auth.user_role(p["username"])
                    ok = bool(role)
                    doc = get_doc(doc_uid)
                    ok = ok and bool(doc) and bool(doc_permission(doc, {"username": p["username"], "role": role, "kind": "session"}))
                elif info.get("via") == "token":
                    ok = True  # token 已在连接时验证；吊销由重启/重连生效（记录在文档限制）
                if not ok:
                    ch = info.get("channel")
                    if ch is not None:
                        with suppress(Exception):
                            await ch.close()
                    reg.pop(cid, None)


# 供 main.py 挂载的 ASGI 应用（模块导入末尾构造，保证 import 时可用）
_server = _XServer(exception_handler=_exception_handler)
_asgi = ASGIServer(_server, on_connect=_on_connect)


def asgi_app():
    return _asgi
