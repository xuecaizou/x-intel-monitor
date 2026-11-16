# -*- coding: utf-8 -*-
"""多 LLM 通道管理（2026-11-16 round2 需求 B/C）。

职责：
  - 通道列表的持久化（runtime.json `ai_channels`，密钥仅存服务端，与现有
    推送通道凭据同一安全边界）；
  - 旧单份 `ai` 配置的幂等迁移（legacy → 默认通道，旧字段保留兼容）；
  - 统一解析服务 resolve_cfg()：全部 AI 调用点（研判/翻译/分类/候选推荐/文档 AI/
    草稿测试/模型拉取）经此取得生效配置——禁止后台各自读旧全局配置造成串用。

数据结构（runtime.json）：
  ai_channels: {
    "list": [ {id, name, base_url, api_key, model, enabled, max_tokens, proxy,
               timeout, models[], last_test{...}} ],
    "default": "<id>",
    "task_overrides": { "doc_ai": "<id>", "writing_recommend": "<id>",
                        "analyze": "<id>", "translate": "<id>", "classify": "<id>" }
  }

语义：
  - 草稿测试/拉取模型接收完整草稿参数，服务端一次请求内使用，**不写 runtime**；
  - 脱敏星号（含 ****）不代表密钥——凭据引用回退到通道已存密钥；
  - 新通道不继承其他通道密钥；删除被引用通道被阻止（先改默认/覆盖）；
  - 任务级覆盖 > 默认通道；任务开始时形成配置快照（channel_id/model 记录）。
"""
import secrets

from .config import load, save_runtime_exact

TASKS = ("doc_ai", "writing_recommend", "analyze", "translate", "classify")


def _new_id():
    return "aich_" + secrets.token_hex(5)


def _normalize(ch):
    """通道字段规范化（写入前）。"""
    return {
        "id": ch.get("id") or _new_id(),
        "name": (ch.get("name") or "").strip()[:60] or "未命名通道",
        "base_url": (ch.get("base_url") or "").strip(),
        "api_key": (ch.get("api_key") or "").strip(),
        "model": (ch.get("model") or "").strip(),
        "enabled": bool(ch.get("enabled", True)),
        "max_tokens": int(ch.get("max_tokens") or 4096),
        "proxy": (ch.get("proxy") or "").strip(),
        "timeout": int(ch.get("timeout") or 90),
        "models": [str(m) for m in (ch.get("models") or [])][:200],
        "last_test": ch.get("last_test") or {},
    }


def _store():
    return load().get("ai_channels") or {}


def list_channels():
    st = _store()
    return [dict(c) for c in (st.get("list") or [])]


def get_channel(cid):
    for c in list_channels():
        if c["id"] == cid:
            return c
    return None


def default_channel_id():
    st = _store()
    d = st.get("default") or ""
    lst = st.get("list") or []
    if d and any(c["id"] == d for c in lst):
        return d
    for c in lst:
        if c.get("enabled"):
            return c["id"]
    return lst[0]["id"] if lst else None


def save_channels(channels, default=None, task_overrides=None):
    """全量保存（管理端）。校验默认/覆盖引用存在；id 缺失自动补。"""
    norm = [_normalize(c) for c in (channels or [])]
    ids = {c["id"] for c in norm}
    d = default or (norm[0]["id"] if norm else "")
    if d and d not in ids:
        raise ValueError(f"默认通道 {d} 不在通道列表中")
    ov = {k: v for k, v in (task_overrides or {}).items()
          if k in TASKS and v in ids}
    save_runtime_exact({"ai_channels": {"list": norm, "default": d, "task_overrides": ov}})
    return {"list": norm, "default": d, "task_overrides": ov}


def upsert_channel(ch):
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
        lst.append(norm)
    d = st.get("default") or (norm["id"] if len(lst) == 1 else "")
    save_runtime_exact({"ai_channels": {"list": lst, "default": d,
                                  "task_overrides": st.get("task_overrides") or {}}})
    return norm


def delete_channel(cid):
    """删除通道。被默认/任务覆盖引用时阻止（要求先替换引用）——不留悬空引用。"""
    st = _store()
    lst = [c for c in (st.get("list") or []) if c["id"] != cid]
    if len(lst) == len(st.get("list") or []):
        raise ValueError("通道不存在")
    refs = []
    if st.get("default") == cid:
        refs.append("默认通道")
    for k, v in (st.get("task_overrides") or {}).items():
        if v == cid:
            refs.append(f"任务覆盖 {k}")
    if refs:
        raise ValueError("通道仍被引用（" + "、".join(refs) + "）：请先更换引用再删除")
    d = st.get("default") or (lst[0]["id"] if lst else "")
    save_runtime_exact({"ai_channels": {"list": lst, "default": d,
                                  "task_overrides": st.get("task_overrides") or {}}})
    return True


def set_default(cid):
    st = _store()
    if not get_channel(cid):
        raise ValueError("通道不存在")
    save_runtime_exact({"ai_channels": {"list": st.get("list") or [], "default": cid,
                                  "task_overrides": st.get("task_overrides") or {}}})
    return True


def set_task_override(task, cid):
    """task 通道覆盖；cid 为空 = 清除覆盖（回落默认）。"""
    if task not in TASKS:
        raise ValueError(f"未知任务 {task}")
    st = _store()
    ov = dict(st.get("task_overrides") or {})
    if cid:
        if not get_channel(cid):
            raise ValueError("通道不存在")
        ov[task] = cid
    else:
        ov.pop(task, None)
    save_runtime_exact({"ai_channels": {"list": st.get("list") or [],
                                  "default": st.get("default") or "",
                                  "task_overrides": ov}})
    return True


def record_test_result(cid, result):
    """记录最近测试结果（含草稿指纹——U07：改配置后旧通过状态失效）。"""
    st = _store()
    lst = [dict(c) for c in (st.get("list") or [])]
    for c in lst:
        if c["id"] == cid:
            c["last_test"] = result
            break
    save_runtime_exact({"ai_channels": {"list": lst, "default": st.get("default") or "",
                                  "task_overrides": st.get("task_overrides") or {}}})
    return True


def migrate_legacy():
    """旧单份 `ai` 配置 → 默认通道。幂等：已有任何通道时不重复迁移。"""
    st = _store()
    if st.get("list"):
        return False
    legacy = load().get("ai") or {}
    if not (legacy.get("base_url") or legacy.get("api_key") or legacy.get("model")):
        return False
    ch = _normalize({
        "id": "aich_legacy",
        "name": "默认通道（迁移自旧配置）",
        "base_url": legacy.get("base_url") or "",
        "api_key": legacy.get("api_key") or "",
        "model": legacy.get("model") or "",
        "enabled": bool(legacy.get("enabled", False)),
        "max_tokens": legacy.get("max_tokens") or 4096,
        "proxy": legacy.get("proxy") or "",
    })
    save_runtime_exact({"ai_channels": {"list": [ch], "default": ch["id"],
                                  "task_overrides": {}}})
    return True


def resolve_cfg(channel_id=None, task=None):
    """统一解析：返回 ai.py 兼容的生效配置 dict。

    优先级：显式 channel_id > 任务覆盖(task) > 默认通道 > 旧全局 ai 配置。
    附带 channel_id/channel_name 供任务快照记录。找不到可用通道时
    返回 enabled=False 的旧配置（调用方给出明确状态而非悄悄用错配置）。
    """
    st = _store()
    ch = None
    cid = channel_id
    if not cid and task and task in TASKS:
        cid = (st.get("task_overrides") or {}).get(task)
    if not cid:
        cid = default_channel_id()
    if cid:
        ch = get_channel(cid)
    legacy = dict(load().get("ai") or {})
    if ch is None:
        out = dict(legacy)
        # R06：显式引用的通道不存在 → fail closed（enabled=False），
        # 调用方给出明确状态，不得悄悄启用旧全局配置。
        out["enabled"] = False
        out["channel_id"] = ""
        out["channel_name"] = ""
        out["channel_missing"] = bool(cid)
        return out
    out = {
        "enabled": bool(ch.get("enabled")),
        "base_url": ch.get("base_url") or legacy.get("base_url") or "",
        "api_key": ch.get("api_key") or "",
        "model": ch.get("model") or legacy.get("model") or "",
        "max_tokens": ch.get("max_tokens") or 4096,
        "proxy": ch.get("proxy") or "",
        "timeout": ch.get("timeout") or 90,
        "auto_analyze": legacy.get("auto_analyze", True),
        "auto_limit": legacy.get("auto_limit", 10),
        "channel_id": ch["id"],
        "channel_name": ch.get("name") or "",
    }
    return out


def draft_cfg(draft, channel_id=None):
    """草稿配置合成（需求 B：测试/拉模型用，不落库）。

    凭据语义：
      draft.api_key 非空且不含 **** → 新密钥（本次请求使用，不落库）
      draft.api_key 空 / 含 **** 且提供 channel_id → 凭据引用：用通道已存密钥
      都没有 → 空（本地无鉴权网关允许空 Key）
    """
    base = resolve_cfg(channel_id) if channel_id else {}
    ref_key = (base.get("api_key") or "") if channel_id else ""
    key = (draft.get("api_key") or "").strip()
    if not key or "****" in key:
        # R05：凭据引用仅当草稿地址与引用通道一致（或草稿未填地址）——
        # 换了 endpoint 还留空 Key 时不得把 A 通道的密钥发往 B。
        d_url = (draft.get("base_url") or "").strip()
        if d_url and ref_key and d_url != (base.get("base_url") or ""):
            key = ""
        else:
            key = ref_key
    out = {
        "enabled": True,   # 草稿测试：显式动作即视为启用本次请求
        "base_url": (draft.get("base_url") or "").strip() or (base.get("base_url") or ""),
        "api_key": key,
        "model": (draft.get("model") or "").strip() or (base.get("model") or ""),
        "max_tokens": draft.get("max_tokens") or base.get("max_tokens") or 4096,
        "proxy": (draft.get("proxy") or "").strip() or (base.get("proxy") or ""),
        "timeout": draft.get("timeout") or base.get("timeout") or 90,
        "channel_id": channel_id or "",
        "channel_name": (get_channel(channel_id) or {}).get("name", "") if channel_id else "",
    }
    if not out["base_url"]:
        raise ValueError("缺少 base_url（草稿与引用通道均未提供）")
    return out


def fingerprint(draft):
    """草稿配置指纹（U07：测试通过绑定草稿，改参数后旧通过状态失效）。"""
    import hashlib
    import json as _json
    material = {k: draft.get(k) for k in
                ("base_url", "model", "api_key", "proxy", "max_tokens", "timeout", "channel_id")
                if draft.get(k) is not None}
    raw = _json.dumps(material, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def masked_list():
    """API 输出：密钥脱敏。"""
    out = []
    for c in list_channels():
        c = dict(c)
        key = c.get("api_key") or ""
        c["api_key"] = ("****" + key[-4:]) if key else ""
        c["has_key"] = bool(key)
        out.append(c)
    return out
