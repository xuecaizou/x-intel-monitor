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
        """)

        # agent token 范围列（幂等）
        try:
            c.execute("ALTER TABLE auth_tokens ADD COLUMN scope TEXT DEFAULT ''")
        except Exception:
            pass

        c.executescript("""
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


def user_role(username: str) -> Optional[str]:
    r = db.get_conn().execute("SELECT role FROM auth_users WHERE username=?",
                              (username,)).fetchone()
    return r["role"] if r else None


def create_user(username: str, password: str, role: str = "read") -> dict:
    """创建团队成员（admin 端点）。用户名唯一。"""
    username = (username or "").strip()
    if not username or len(username) > 64:
        raise ValueError("用户名非法")
    if role not in ("read", "write", "admin"):
        raise ValueError("role 仅支持 read/write/admin")
    if len(password or "") < 8:
        raise ValueError("密码至少 8 位")
    with db._lock:
        c = db.get_conn()
        if c.execute("SELECT 1 FROM auth_users WHERE username=?", (username,)).fetchone():
            raise ValueError("用户名已存在")
        c.execute("INSERT INTO auth_users (username, pw_hash, role, created_at) VALUES (?,?,?,?)",
                  (username, hash_password(password), role, db.now()))
        c.commit()
    return {"username": username, "role": role}


def list_users() -> list:
    return [dict(r) for r in db.get_conn().execute(
        "SELECT username, role, created_at FROM auth_users ORDER BY username").fetchall()]


def set_user_role(username: str, role: str) -> bool:
    if role not in ("read", "write", "admin"):
        raise ValueError("role 仅支持 read/write/admin")
    with db._lock:
        cur = db.get_conn().execute("UPDATE auth_users SET role=? WHERE username=?",
                                    (role, username))
        db.get_conn().commit()
        return cur.rowcount > 0


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

def create_token(name: str, role: str = "read", scope: str = "") -> str:
    if role not in ("read", "write", "admin", "agent"):
        raise ValueError("role 仅支持 read/write/admin/agent")
    raw = ("agent_" if role == "agent" else "xintel_") + secrets.token_urlsafe(32)
    with db._lock:
        db.get_conn().execute(
            "INSERT INTO auth_tokens (name, digest, role, scope, created_at) VALUES (?,?,?,?,?)",
            (name, token_digest(raw), role, scope or "", db.now()))
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
    p = {"name": r["name"], "role": r["role"], "kind": "token"}
    if r["role"] == "agent":
        p["agent_scope"] = r["scope"] or ""
        p["agent_scope_all"] = (r["scope"] or "") == "all"
        p["agent_docs"] = [] if p["agent_scope_all"] else [
            x for x in (r["scope"] or "").split(",") if x]
    return p


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


def parse_init_content(raw: str):
    """解析初始化文件内容。返回 (username, password, 诊断信息)。

    容错（2026-11-08 实测问题）：
      - 冒号 或 斜杠 均可用作分隔符（用户实际写了 admin/密码）
      - 去 BOM / 首尾空白 / Windows 换行
      - 密码含 @ . 等字符原样保留
    仍无法解析时返回 (None, None, 原因)。
    """
    txt = (raw or "").lstrip("﻿").strip()
    if not txt:
        return None, None, "文件为空"
    sep = ":" if ":" in txt else ("/" if "/" in txt else None)
    if sep is None:
        return None, None, "缺少分隔符（需 用户名:密码 或 用户名/密码）"
    username, _, password = txt.partition(sep)
    username, password = username.strip(), password.strip()
    if not username:
        return None, None, "用户名为空"
    if len(password) < 8:
        return None, None, f"密码长度 {len(password)} < 8"
    return username, password, ""


def find_init_files():
    """查找初始化文件。同时接受 单点/双点 命名（admin_init.txt / admin_init..txt），
    以及项目根目录的误放位置——返回存在的路径列表。"""
    base = Path(DATA_DIR)
    candidates = [base / INIT_FILE, base / "admin_init..txt", base.parent / INIT_FILE]
    return [p for p in candidates if p.exists()]


def try_consume_init_file() -> Optional[str]:
    """服务启动时处理初始化文件（存在即处理；已有管理员时改为"重置该用户密码"）。
    格式：用户名:密码（也兼容 用户名/密码）。处理成功后删除文件。"""
    files = find_init_files()
    if not files:
        return None
    p = files[0]
    try:
        raw = p.read_text(encoding="utf-8-sig")
    except Exception as e:
        return f"初始化文件读取失败：{type(e).__name__}"
    username, password, why = parse_init_content(raw)
    if not username:
        return f"admin_init 解析失败（{why}）：文件保留在 {p}，请修正后重启"
    try:
        if user_count() == 0:
            create_admin(username, password)
            msg = f"已创建管理员：{username}"
        else:
            # 已有管理员：按文件意图重置该用户密码（运维恢复场景）
            set_password(username, password)
            msg = f"已重置管理员密码：{username}"
        for f in files:
            try:
                f.unlink()
            except Exception:
                pass
        return msg + f"（初始化文件已删除，共 {len(files)} 个）"
    except Exception as e:
        return f"初始化失败：{type(e).__name__}: {e}"[:160]


def set_password(username: str, password: str) -> bool:
    """设置指定用户密码（不存在返回 False）"""
    with db._lock:
        cur = db.get_conn().execute(
            "UPDATE auth_users SET pw_hash=? WHERE username=?",
            (hash_password(password), username))
        db.get_conn().commit()
        return cur.rowcount > 0


# ---------- 依赖注入 ----------

ROLE_RANK = {"read": 1, "write": 2, "admin": 3}


def _is_test_mode() -> bool:
    """测试模式必须显式声明（环境变量），不依赖当前目录/浏览器来源"""
    return os.environ.get("XINTEL_TEST") == "1"


def _auth_disabled_allowed() -> bool:
    """auth.enabled=false 仅在测试模式下允许；生产配置关闭会被拒绝并回退为启用。"""
    try:
        from .config import load
        enabled = (load().get("auth") or {}).get("enabled", True)
    except Exception:
        enabled = True
    if enabled:
        return False
    if _is_test_mode():
        return True
    # 生产误配：记录一次告警（不静默公开），按启用处理
    if not getattr(_auth_disabled_allowed, "_warned", False):
        print("[auth] 拒绝 auth.enabled=false：非测试模式（需 XINTEL_TEST=1），认证保持启用")
        _auth_disabled_allowed._warned = True
    return False


def _extract_principal(request: Request) -> Optional[dict]:
    # 1) Bearer Token
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return verify_token(auth[7:].strip())
    # 2) 会话 Cookie（角色以 auth_users 为准——修正历史缺陷：不再固定 admin）
    sid = request.cookies.get(COOKIE_NAME, "")
    s = get_session(sid)
    if s:
        role = user_role(s["username"]) or "read"
        return {"username": s["username"], "role": role, "kind": "session",
                "csrf": s["csrf"]}
    return None


def require(role: str = "read"):
    """FastAPI 依赖：要求指定权限；写请求校验 CSRF（会话登录时）"""
    async def _dep(request: Request):
        from .config import load
        if _auth_disabled_allowed():
            return {"username": "anonymous", "role": "admin", "kind": "test_bypass"}
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
