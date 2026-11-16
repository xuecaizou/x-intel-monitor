# -*- coding: utf-8 -*-
"""Round3 补丁：R04 AI 调用统一路由 / R05 草稿凭据泄漏 / R06 规范化与 fail-closed。运行后自删。"""
import ast
import sys

sys.stdout.reconfigure(encoding="utf-8")

# ---------- app/ai_channels.py ----------
s = open("app/ai_channels.py", encoding="utf-8").read()

# R06a: upsert_channel 与既有通道字段合并（页面普通保存只带部分字段时不得重置其余）
old = '''def upsert_channel(ch):
    """新增/更新单通道（页面保存入口）。返回规范化后的通道。"""
    st = _store()
    lst = [dict(c) for c in (st.get("list") or [])]
    norm = _normalize(ch)
    replaced = False
    for i, c in enumerate(lst):
        if c["id"] == norm["id"]:
            # 密钥语义：空/星号 = 保留旧密钥（凭据引用）；"__CLEAR__" = 清除
            if norm["api_key"] in ("", "****") or "****" in norm["api_key"]:
                norm["api_key"] = c.get("api_key") or ""
            lst[i] = norm
            replaced = True
            break
    if not replaced:
        lst.append(norm)'''
new = '''def upsert_channel(ch):
    """新增/更新单通道（页面保存入口）。返回规范化后的通道。

    R06：更新时**与既有通道逐字段合并**——页面普通保存只提交部分字段，
    缺失字段沿用旧值（enabled/max_tokens/timeout/proxy 不被默认值悄悄重置）。
    密钥语义：空/含**** = 凭据引用（沿用已存密钥）；"__CLEAR__" = 真实清除。"""
    st = _store()
    lst = [dict(c) for c in (st.get("list") or [])]
    replaced = False
    merged = dict(ch or {})
    for c in lst:
        if c.get("id") and c["id"] == (ch or {}).get("id"):
            for k, v in c.items():
                if k == "api_key":
                    continue
                if merged.get(k) in (None, ""):
                    merged[k] = v
            replaced = True
            break
    norm = _normalize(merged)
    key_in = (ch or {}).get("api_key")
    for i, c in enumerate(lst):
        if c["id"] == norm["id"]:
            if key_in == "__CLEAR__":
                norm["api_key"] = ""                       # R06：显式清除密钥
            elif norm["api_key"] in ("", "****") or "****" in norm["api_key"]:
                norm["api_key"] = c.get("api_key") or ""   # 凭据引用
            lst[i] = norm
            replaced = True
            break
    if not replaced:
        lst.append(norm)'''
assert old in s, "upsert anchor"
s = s.replace(old, new, 1)

# R06b: resolve_cfg 显式引用不存在 → fail closed（不悄悄回落旧全局配置）
old2 = '''    legacy = dict(load().get("ai") or {})
    if ch is None:
        out = dict(legacy)
        out["enabled"] = bool(legacy.get("enabled", False))
        out["channel_id"] = ""
        out["channel_name"] = ""
        out["channel_missing"] = bool(cid)   # 显式引用但不存在：调用方给出明确状态
        return out'''
new2 = '''    legacy = dict(load().get("ai") or {})
    if ch is None:
        out = dict(legacy)
        # R06：显式引用的通道不存在 → fail closed（enabled=False），
        # 调用方给出明确状态，不得悄悄启用旧全局配置。
        out["enabled"] = False
        out["channel_id"] = ""
        out["channel_name"] = ""
        out["channel_missing"] = bool(cid)
        return out'''
assert old2 in s, "resolve anchor"
s = s.replace(old2, new2, 1)

# R05: 草稿凭据引用只在本通道（或 base_url 一致）时生效
old3 = '''    base = resolve_cfg(channel_id) if channel_id else {}
    ref_key = (base.get("api_key") or "") if channel_id else ""
    key = (draft.get("api_key") or "").strip()
    if not key or "****" in key:
        key = ref_key'''
new3 = '''    base = resolve_cfg(channel_id) if channel_id else {}
    ref_key = (base.get("api_key") or "") if channel_id else ""
    key = (draft.get("api_key") or "").strip()
    if not key or "****" in key:
        # R05：凭据引用仅当草稿地址与引用通道一致（或草稿未填地址）——
        # 换了 endpoint 还留空 Key 时不得把 A 通道的密钥发往 B。
        d_url = (draft.get("base_url") or "").strip()
        if d_url and ref_key and d_url != (base.get("base_url") or ""):
            key = ""
        else:
            key = ref_key'''
assert old3 in s, "draft anchor"
s = s.replace(old3, new3, 1)

open("app/ai_channels.py", "w", encoding="utf-8").write(s)
ast.parse(s)
print("ai_channels patched")

# ---------- app/ai.py：R04 统一路由（默认解析进 ai_channels） ----------
a = open("app/ai.py", encoding="utf-8").read()

old4 = '''def analyze_tweet(tweet, cfg=None):
    cfg = cfg or _cfg()'''
new4 = '''def analyze_tweet(tweet, cfg=None):
    cfg = cfg or _routed_cfg("analyze")'''
assert old4 in a, "analyze anchor"
a = a.replace(old4, new4, 1)

old5 = '''def translate_tweet(tweet, cfg=None):
    cfg = cfg or _cfg()'''
new5 = '''def translate_tweet(tweet, cfg=None):
    cfg = cfg or _routed_cfg("translate")'''
assert old5 in a, "translate anchor"
a = a.replace(old5, new5, 1)

old6 = '''def classify_batch(tweets, cfg=None):
    cfg = cfg or _cfg()'''
new6 = '''def classify_batch(tweets, cfg=None):
    cfg = cfg or _routed_cfg("classify")'''
assert old6 in a, "classify anchor"
a = a.replace(old6, new6, 1)

old7 = '''def _cfg():
    return load().get("ai", {})'''
new7 = '''def _cfg():
    return load().get("ai", {})


def _routed_cfg(task):
    """R04：未显式传 cfg 的调用一律经 ai_channels 解析（任务覆盖>默认通道>旧全局），
    手动/批量/自动全入口同一路由——不再各自读旧全局配置。"""
    try:
        from . import ai_channels as _aich
        return _aich.resolve_cfg(task=task)
    except Exception:
        return _cfg()'''
assert old7 in a, "cfg anchor"
a = a.replace(old7, new7, 1)

open("app/ai.py", "w", encoding="utf-8").write(a)
ast.parse(a)
print("ai.py routed")

# ---------- api/routes.py：手动 analyze/translate/tag 端点显式路由 ----------
r = open("api/routes.py", encoding="utf-8").read()
old8 = '''        fields = aimod.analyze_tweet({"screen_name": body.screen_name, "text": body.text})
        return {"ok": True, "result": _normalize_ai_fields(fields)}'''
new8 = '''        fields = aimod.analyze_tweet({"screen_name": body.screen_name, "text": body.text},
                                     cfg=ai_channels.resolve_cfg(task="analyze"))
        return {"ok": True, "result": _normalize_ai_fields(fields)}'''
assert old8 in r, "route analyze anchor"
r = r.replace(old8, new8, 1)

old9 = '''        f = aimod.translate_tweet({"text": body.text})'''
new9 = '''        f = aimod.translate_tweet({"text": body.text},
                                  cfg=ai_channels.resolve_cfg(task="translate"))'''
assert old9 in r, "route translate anchor"
r = r.replace(old9, new9, 1)

old10 = '''        fields = aimod.analyze_tweet({"screen_name": body.screen_name, "text": body.text})
        r = _normalize_ai_fields(fields)
        return {"ok": True, "result": {k: r[k] for k in
                ("country", "province", "city", "organizations", "industry", "model")}}'''
new10 = '''        fields = aimod.analyze_tweet({"screen_name": body.screen_name, "text": body.text},
                                     cfg=ai_channels.resolve_cfg(task="classify"))
        r = _normalize_ai_fields(fields)
        return {"ok": True, "result": {k: r[k] for k in
                ("country", "province", "city", "organizations", "industry", "model")}}'''
assert old10 in r, "route tag anchor"
r = r.replace(old10, new10, 1)

# routes.py 顶部补 ai_channels 导入（若缺）
if "ai_channels" not in r.split("router = APIRouter()")[0]:
    r = r.replace("from app import db, engine, ai as aimod, twitter, reports, rssfetch, auth, eventexport, writing",
                  "from app import db, engine, ai as aimod, twitter, reports, rssfetch, auth, eventexport, writing, ai_channels")
open("api/routes.py", "w", encoding="utf-8").write(r)
ast.parse(r)
print("routes routed")
