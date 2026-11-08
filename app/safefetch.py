# -*- coding: utf-8 -*-
"""文件访问与出站请求安全（第四批）。

A. 报告文件路径限制（app/safepath.py 语义在此实现）
   - 所有读写/下载/删除统一经 safe_join()：必须落在允许根目录内
   - 拒绝：绝对路径、盘符、UNC、..、符号链接逃逸、URL 编码变体
B. 出站 URL 校验（SSRF）
   - 低信任 URL（RSS/媒体）仅 http(s)，解析并校验 IP（禁止回环/私网/链路本地/保留段）
   - 每次重定向重新校验；限制重定向次数、超时、响应大小
   - 管理员显式配置的内网 AI 网关/代理走白名单例外（按用途授权）
"""
import ipaddress
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

from .config import DATA_DIR


class UnsafePathError(ValueError):
    """路径越界/非法"""


class UnsafeUrlError(ValueError):
    """出站 URL 未通过校验"""


# ============ 文件路径 ============

def _allowed_roots():
    """允许的文件根目录：data/（含 report_tasks 与备份）与项目 web/ 只读"""
    roots = [Path(DATA_DIR).resolve()]
    proj = Path(__file__).resolve().parent.parent
    roots.append((proj / "web").resolve())
    return roots


def safe_join(root: Path, *parts: str) -> Path:
    """拼接并校验：结果必须位于 root 内；拒绝绝对路径/盘符/UNC/../URL 编码变体"""
    from urllib.parse import unquote
    root = Path(root).resolve()
    checked = []
    for p in parts:
        if p is None:
            continue
        sp = unquote(str(p))   # 编码变体（%2e%2e%2f、%5c）先解码再校验
        if not sp:
            continue
        # 盘符（C:）、UNC（\\server）、以 / 或 \ 开头
        if re.match(r"^[A-Za-z]:", sp) or sp.startswith(("\\\\", "//", "/", "\\")):
            raise UnsafePathError(f"不允许绝对路径/UNC/盘符：{sp[:40]}")
        if ".." in sp.replace("\\", "/").split("/"):
            raise UnsafePathError(f"不允许路径穿越：{sp[:40]}")
        checked.append(sp)
    target = root.joinpath(*checked).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise UnsafePathError(f"路径越出允许目录：{target}")
    return target


def safe_report_path(deliver_dir: str, filename: str) -> Path:
    """报告交付文件：deliver_dir 必须是已批准根目录下的路径，filename 仅文件名"""
    if os.path.isabs(deliver_dir) or re.match(r"^[A-Za-z]:", deliver_dir or ""):
        raise UnsafePathError("deliver_dir 不允许绝对路径（请使用任务登记的交付目录）")
    base = safe_join(Path(DATA_DIR) / "report_tasks", deliver_dir)
    if os.path.basename(filename) != filename:
        raise UnsafePathError("filename 必须是纯文件名")
    target = safe_join(base, filename)
    # 符号链接逃逸检查
    if target.is_symlink():
        raise UnsafePathError("符号链接不允许")
    return target


# ============ 出站 URL ============

_DEFAULT_PORTS = {"http": 80, "https": 443}
MAX_REDIRECTS = 3
MAX_BYTES = 8 * 1024 * 1024


def _ip_blocked(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved
            or a.is_multicast or a.is_unspecified)


def _resolve_ips(host: str):
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return []
    ips = []
    for family, _, _, _, sockaddr in infos:
        ips.append(sockaddr[0])
    return ips


# 按用途允许的私网目标（host:port 精确匹配）；只有管理员显式配置的用途才能进入
def _allowed_private_targets(purpose: str):
    try:
        from .config import load
        cfg = (load().get("outbound") or {}).get("allow_private") or {}
        vals = cfg.get(purpose) or []
        return {str(v).strip().lower() for v in vals if str(v).strip()}
    except Exception:
        return set()


def _hostport(u) -> str:
    port = u.port or _DEFAULT_PORTS.get(u.scheme, 0)
    return f"{u.hostname.lower()}:{port}"


def validate_url(url: str, allow_private: bool = False, purpose: str = "rss") -> str:
    """校验出站 URL。

    私网放行规则（第五批收敛）：
      - 仅当 purpose 在 outbound.allow_private 白名单中且 host:port 精确匹配时允许
      - allow_private=True 参数已废弃（保留兼容：会被忽略，避免调用方误放行）
      - RSS/媒体用途不配置白名单 → 永不允许私网
    """
    if not url or not isinstance(url, str):
        raise UnsafeUrlError("URL 为空")
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https"):
        raise UnsafeUrlError(f"仅允许 http/https：{u.scheme}")
    if not u.hostname:
        raise UnsafeUrlError("URL 缺少主机名")
    host = u.hostname
    # 直接 IP 或解析后校验（防 DNS 检查与连接目标不一致：连接时再次解析校验由调用方保证）
    ips = [host] if _is_ip(host) else _resolve_ips(host)
    if not ips:
        raise UnsafeUrlError(f"主机无法解析：{host[:60]}")
    allowed = _allowed_private_targets(purpose)
    hp = _hostport(u)
    explicit_ok = hp in allowed or host.lower() in allowed
    for ip in ips:
        if _ip_blocked(ip) and not explicit_ok:
            raise UnsafeUrlError(
                f"目标地址不允许（{ip}）：私网/回环/链路本地/保留地址。"
                f"用途 {purpose} 未获管理员显式授权（outbound.allow_private.{purpose}）")
    return u.geturl()


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def check_proxy_trust(purpose: str, proxy: str):
    """代理边界（第五批）：低信任来源走代理时，代理侧解析/连接目标无法由本应用验证。

    要求管理员显式声明 outbound.trusted_proxy=true（受控出口策略已落实）；
    否则拒绝该低信任来源使用代理路径，避免"代理绕过内网防护"。
    """
    if not proxy:
        return
    if purpose in ("ai_gateway",):
        return   # AI 网关属管理员受信配置
    try:
        from .config import load
        trusted = bool((load().get("outbound") or {}).get("trusted_proxy"))
    except Exception:
        trusted = False
    if not trusted:
        raise UnsafeUrlError(
            f"用途 {purpose} 配置了代理但未声明受控出口（outbound.trusted_proxy=true），"
            f"代理侧目标不可验证，已拒绝")


def fetch_limited(url: str, *, timeout: int = 20, max_bytes: int = MAX_BYTES,
                  allow_private: bool = False, session=None, purpose: str = "rss",
                  proxies=None, headers=None):
    """受控 GET：禁用自动重定向（**逐跳** validate_url）、限制大小、限超时。

    2026-11-09 H：新增 purpose/proxies/headers——业务调用链（RSS/媒体）从
    "默认自动重定向+整包读取"的裸 requests 迁移到本函数；代理场景下本地
    IP 校验是尽力而为（代理侧解析不可见），但 scheme 白名单、逐跳校验、
    重定向上限、大小上限、超时仍在应用层实际生效。
    session 可为 requests.Session 或兼容替身（测试用）。
    返回 (status_code, content_bytes, final_url)。
    """
    import requests

    cur = url
    for hop in range(MAX_REDIRECTS + 1):
        validate_url(cur, allow_private=allow_private, purpose=purpose)
        r = (session or requests).get(cur, timeout=timeout, allow_redirects=False,
                                      stream=True, proxies=proxies,
                                      headers=headers or {})
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("location") or r.headers.get("Location")
            if not loc:
                raise UnsafeUrlError("重定向缺少 Location")
            cur = loc if loc.startswith("http") else str(requests.compat.urljoin(cur, loc))
            if hop >= MAX_REDIRECTS:
                raise UnsafeUrlError(f"重定向超过 {MAX_REDIRECTS} 次")
            continue
        chunks, total = [], 0
        for chunk in r.iter_content(65536):
            total += len(chunk)
            if total > max_bytes:
                raise UnsafeUrlError(f"响应超过上限 {max_bytes} 字节")
            chunks.append(chunk)
        return r.status_code, b"".join(chunks), cur
    raise UnsafeUrlError("重定向处理失败")
