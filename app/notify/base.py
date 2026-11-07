"""Markdown 消息模板与推送通道基类"""
import json
from urllib.parse import quote

SEV_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}


def image_base():
    """推送内嵌图片的基址：优先 push.image_base 配置，否则自动探测本机局域网地址"""
    from ..config import load
    cfg = load()
    base = ((cfg.get("push") or {}).get("image_base") or "").rstrip("/")
    if base:
        return base
    port = int((cfg.get("server") or {}).get("port", 8083))
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return f"http://{ip}:{port}"
    except Exception:
        return f"http://127.0.0.1:{port}"


def media_md(t):
    """推文截图的内嵌 Markdown（经 /api/media 代理，适用于支持图片渲染的通道）"""
    u = t.get("media_url")
    if not u:
        return ""
    return f"![推文截图]({image_base()}/api/media?url={quote(u, safe='')})"


_IMG_CACHE = {}


def prepare_image(t):
    """下载推文截图并转 base64（供企业微信 image 消息直传，无需公网 URL）。

    结果按 media_url 缓存；失败/无图/超过 2MB 返回 None。"""
    import base64
    import hashlib
    u = t.get("media_url")
    if not u:
        return None
    if u in _IMG_CACHE:
        return _IMG_CACHE[u]
    try:
        from ..twitter import _http_get
        from ..config import load
        xcfg = dict(load().get("x", {}))
        if not xcfg.get("proxy"):
            xcfg["proxy"] = (load().get("network") or {}).get("proxy") or ""
        r = _http_get(u, xcfg, timeout=20)
        ct = r.headers.get("content-type", "")
        if not ct.startswith("image/") or len(r.content) > 2 * 1024 * 1024:
            _IMG_CACHE[u] = None
            return None
        img = {"base64": base64.b64encode(r.content).decode(),
               "md5": hashlib.md5(r.content).hexdigest()}
        if len(_IMG_CACHE) > 50:
            _IMG_CACHE.clear()
        _IMG_CACHE[u] = img
        return img
    except Exception:
        return None


def geo_line(t):
    """地区/单位/行业摘要行"""
    from .. import db as _db  # 延迟导入避免环
    geo = " ".join(x for x in [t.get("geo_country") or "", t.get("geo_province") or "",
                               t.get("geo_city") or ""] if x)
    parts = []
    if geo:
        parts.append(f"📍{geo}")
    try:
        orgs = json.loads(t.get("org") or "[]")
    except Exception:
        orgs = []
    if orgs:
        parts.append("🏢" + "、".join(orgs[:2]))
    if t.get("industry") and t["industry"] != "未知":
        parts.append(f"🏭{t['industry']}")
    return " ｜ ".join(parts)
SEV_NAME = {"critical": "严重", "high": "高危", "medium": "中危", "low": "低危", "info": "通报"}


class Channel:
    key = ""
    display = ""

    def configured(self, cc):
        return False

    def send(self, cc, title, markdown):
        raise NotImplementedError


def _clip(s, n=200):
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"


def build_single(t):
    """单条推文即时推送：截图 + 地区/单位/行业 + 原文 + 译文 + AI 研判"""
    sev = t.get("ai_severity") or "info"
    lines = [f"{SEV_EMOJI.get(sev, '⚪')} **[{SEV_NAME.get(sev, sev)}]** @{t['screen_name']} 推文"]
    g = geo_line(t)
    if g:
        lines.append(f"> {g}")
    lines.append(f"> {_clip(t.get('text', ''), 180)}")
    if t.get("text_zh"):
        lines.append(f"> **译**：{_clip(t['text_zh'], 180)}")
    if t.get("ai_type"):
        lines.append(f"> 研判：{t['ai_type']}（置信度 {t.get('ai_confidence', 0)}%）")
    if t.get("ai_assessment"):
        lines.append(f"> 评估：{_clip(t['ai_assessment'], 160)}")
    recs = t.get("ai_recommendations") or []
    if recs:
        lines.append("> 建议：" + "；".join(recs[:2]))
    iocs = t.get("ai_iocs") or []
    if iocs:
        lines.append("> IOC：" + "、".join(iocs[:6]))
    if t.get("ai_status") == "error":
        lines.append(f"> ⚠️ AI 研判失败（已重试）：{_clip(t.get('ai_error', ''), 80)}")
    md = media_md(t)
    if md:
        lines.append("")
        lines.append(md)
    if t.get("url"):
        lines.append(f"\n[查看原文]({t['url']})")
    lines.append("\n—— X-Intel 监控台")
    return "\n".join(lines)


def build_digest(items):
    """每轮新推文摘要（含国家标注、中文译文与截图）"""
    head = f"**X-Intel 推文摘要**（本轮 {len(items)} 条）\n"
    body = []
    for i, t in enumerate(items, 1):
        title = t.get("title_zh") or _clip(t.get("text", ""), 60).replace("\n", " ")
        sev = t.get("ai_severity") or "info"
        geo = t.get("geo_country") or ""
        line = (f"\n{i}. {SEV_EMOJI.get(sev, '⚪')} @{t['screen_name']}"
                + (f"（{geo}）" if geo else "")
                + f"：{title}")
        if t.get("text_zh"):
            line += f"\n   译：{_clip(t['text_zh'], 100)}"
        md = media_md(t)
        if md:
            line += f"\n   {md}"
        body.append(line)
    return head + "\n".join(body) + "\n\n—— 由 X-Intel 监控台自动推送"


def build_test():
    return "**X-Intel 测试消息**\n> 推送通道工作正常 ✅\n> 若您看到本消息，说明该通道配置成功。"
