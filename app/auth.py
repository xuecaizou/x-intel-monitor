# -*- coding: utf-8 -*-
"""认证与授权（第四批）。

两种凭据：
  1. 浏览器会话：登录后签发 HttpOnly Cookie（SameSite=Lax，修改请求需 CSRF token）
  2. API Token：Authorization: Bearer xintel_xxx —— 供脚本/Agent 调用

权限分级：
  read  只读：查询推文/统计/日志/规则/任务/报告线索包
  write 修改：所有 PUT/POST/DELETE（含配置、推送、补发、删除）
  admin 管理：Token 管理、迁移、备份

安全要点：
  - Token 仅存 SHA-256 摘要，明文只在创建时返回一次
  - 密码用 PBKDF2-HMAC-SHA256 + 随机盐
  - 首次初始化：通过本地文件（data/admin_init.txt，仅本机可读）设置，无匿名远程初始化
  - 认证失败一律拒绝，不回退匿名
  - 错误响应不含密钥/路径/堆栈
"""
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, Request

from . import db
from .config import DATA_DIR

COOKIE_NAME = "xintel_sess"
SESSION_HOURS = 12
_PBKDF2_ROUNDS = 200_000


# ---------- 密码 ----------

def hash_password(password: str, salt: str = "") -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ROUNDS)
    return f"pbkdf2${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, _ = stored.split("$", 2)
    except Exception:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------- 存储（SQLite：auth_users / auth_tokens / auth_sessions） ----------

def init_auth_tables():
    with db._lock:
        c = db.get_conn()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS auth_users (
            username TEXT PRIMARY KEY,
            pw_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS auth_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            digest TEXT UNIQUE NOT NULL,
            role TEXT NOT NULL DEFAULT 'read',
            created_at TEXT,
            last_used_at TEXT,
            revoked INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS auth_sessions (
            sid TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            csrf TEXT NOT NULL,
            created_at TEXT,
            expires_at REAL
        );
        """)
        c.commit()


def user_count() -> int:
    return db.get_conn().execute("SELECT COUNT(*) n FROM auth_users").fetchone()["n"]


def create_admin(username: str, password: str):
    """创建管理员（仅当无任何用户；首次初始化走本机文件流程）"""
    if user_count() > 0:
        raise RuntimeError("管理员已存在")
    with db._lock:
        db.get_conn().execute(
            "INSERT INTO auth_users (username, pw_hash, role, created_at) VALUES (?,?,?,?)",
            (username, hash_password(password), "admin", db.now()))
        db.get_conn().commit()


def authenticate(username: str, password: str) -> Optional[dict]:
    r = db.get_conn().execute(
        "SELECT * FROM auth_users WHERE username=?", (username,)).fetchone()
    if not r or not verify_password(password, r["pw_hash"]):
        return None
    return {"username": r["username"], "role": r["role"]}


# ---------- 会话 ----------

def new_session(username: str) -> tuple:
    sid = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    with db._lock:
        db.get_conn().execute(
            "INSERT INTO auth_sessions (sid, username, csrf, created_at, expires_at)"
            " VALUES (?,?,?,?,?)",
            (sid, username, csrf, db.now(), time.time() + SESSION_HOURS * 3600))
        db.get_conn().commit()
    return sid, csrf


def get_session(sid: str) -> Optional[dict]:
    if not sid:
        return None
    r = db.get_conn().execute(
        "SELECT * FROM auth_sessions WHERE sid=?", (sid,)).fetchone()
    if not r:
        return None
    if float(r["expires_at"] or 0) < time.time():
        drop_session(sid)
        return None
    return dict(r)


def drop_session(sid: str):
    with db._lock:
        db.get_conn().execute("DELETE FROM auth_sessions WHERE sid=?", (sid,))
        db.get_conn().commit()


# ---------- Token ----------

def create_token(name: str, role: str = "read") -> str:
    if role not in ("read", "write", "admin"):
        raise ValueError("role 仅支持 read/write/admin")
    raw = "xintel_" + secrets.token_urlsafe(32)
    with db._lock:
        db.get_conn().execute(
            "INSERT INTO auth_tokens (name, digest, role, created_at) VALUES (?,?,?,?)",
            (name, token_digest(raw), role, db.now()))
        db.get_conn().commit()
    return raw  # 仅此一次返回明文


def verify_token(raw: str) -> Optional[dict]:
    if not raw:
        return None
    r = db.get_conn().execute(
        "SELECT * FROM auth_tokens WHERE digest=? AND revoked=0", (token_digest(raw),)).fetchone()
    if not r:
        return None
    with db._lock:
        db.get_conn().execute("UPDATE auth_tokens SET last_used_at=? WHERE id=?",
                              (db.now(), r["id"]))
        db.get_conn().commit()
    return {"name": r["name"], "role": r["role"], "kind": "token"}


def list_tokens():
    return [dict(r) for r in db.get_conn().execute(
        "SELECT id, name, role, created_at, last_used_at, revoked FROM auth_tokens"
        " ORDER BY id DESC").fetchall()]


def revoke_token(tid: int) -> bool:
    with db._lock:
        cur = db.get_conn().execute("UPDATE auth_tokens SET revoked=1 WHERE id=?", (int(tid),))
        db.get_conn().commit()
        return cur.rowcount > 0


# ---------- 首次初始化（本机文件，禁止匿名远程设置） ----------

INIT_FILE = "admin_init.txt"


def init_ready() -> bool:
    return user_count() > 0


def try_consume_init_file() -> Optional[str]:
    """服务启动时若存在 data/admin_init.txt，读取并创建管理员后删除文件。
    格式：用户名:密码（密码可含冒号）。文件权限建议仅本机用户可读。"""
    p = Path(DATA_DIR) / INIT_FILE
    if not p.exists() or user_count() > 0:
        return None
    try:
        raw = p.read_text(encoding="utf-8").strip()
        username, _, password = raw.partition(":")
        if not username or len(password) < 8:
            return "admin_init.txt 格式错误或密码少于 8 位"
        create_admin(username.strip(), password)
        try:
            p.unlink()
        except Exception:
            pass
        return f"已根据本机初始化文件创建管理员：{username.strip()}"
    except Exception as e:
        return f"初始化失败：{type(e).__name__}"


# ---------- 依赖注入 ----------

ROLE_RANK = {"read": 1, "write": 2, "admin": 3}


def _extract_principal(request: Request) -> Optional[dict]:
    # 1) Bearer Token
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return verify_token(auth[7:].strip())
    # 2) 会话 Cookie
    sid = request.cookies.get(COOKIE_NAME, "")
    s = get_session(sid)
    if s:
        return {"username": s["username"], "role": "admin", "kind": "session",
                "csrf": s["csrf"]}
    return None


def require(role: str = "read"):
    """FastAPI 依赖：要求指定权限；写请求校验 CSRF（会话登录时）"""
    async def _dep(request: Request):
        from .config import load
        acfg = (load().get("auth") or {})
        if not acfg.get("enabled", True):
            return {"username": "anonymous", "role": "admin", "kind": "disabled"}
        if not init_ready():
            raise HTTPException(503, "系统未初始化管理员：请在本机创建 data/admin_init.txt（用户名:密码）后重启")
        p = _extract_principal(request)
        if not p:
            raise HTTPException(401, "未认证")
        if ROLE_RANK.get(p.get("role", ""), 0) < ROLE_RANK.get(role, 1):
            raise HTTPException(403, "权限不足")
        # CSRF：Cookie 会话的写请求必须带 X-CSRF-Token
        if (request.method in ("POST", "PUT", "DELETE", "PATCH")
                and p.get("kind") == "session"):
            tok = request.headers.get("x-csrf-token", "")
            if not tok or not hmac.compare_digest(tok, p.get("csrf", "")):
                raise HTTPException(403, "CSRF 校验失败（缺少或错误的 X-CSRF-Token）")
        return p
    return _dep


def seed_initial_admin_if_needed():
    """便利：无管理员且存在环境变量时创建（CI/测试用；生产建议用本机文件）"""
    if user_count() > 0:
        return
    u = os.environ.get("XINTEL_ADMIN_USER")
    p = os.environ.get("XINTEL_ADMIN_PASSWORD")
    if u and p and len(p) >= 8:
        try:
            create_admin(u, p)
        except Exception:
            pass
