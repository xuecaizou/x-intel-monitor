# -*- coding: utf-8 -*-
"""标准化导出（第六批）：事件 JSON / 时间线 CSV / IOC JSON+CSV。

约束：
  - schema_version 显式；含事件版本、来源、时间、核验状态
  - 一致性快照：导出函数在**单个 SQLite 事务**内读取（BEGIN → 读 → COMMIT）
  - CVE / 受害资产 / 攻击指标 / 普通引用 四类分离
  - partial 逐条标注；invalid 不混入可用 AI 结论
  - CSV 防公式注入（= + - @ 开头加 '）；JSON 保留类型
  - 不输出凭据/内部路径/无关配置
"""
import csv
import io
import json
import re
import sqlite3

from . import db
from . import integrity as integ

SCHEMA_VERSION = "xintel-event-1"

# CVE / 私网 / 域名 / IP / 哈希 识别
def _arr(v):
    """数组字段读取：db._row_tweet 已解析为 list，历史数据可能是 JSON 字符串。
    兼容两者，避免"数组再解析"导致空（与 org 双重解析同类缺陷）。"""
    if isinstance(v, list):
        return v
    if not v:
        return []
    try:
        parsed = json.loads(v)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


_CVE = re.compile(r"CVE-\d{4}-\d{4,7}", re.I)
_IP = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_HASH = re.compile(r"^[a-fA-F0-9]{32,64}$")
_DOMAIN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,}$")


def _classify_ioc(value, tweet):
    """返回 (kind, role)。kind: cve/ip/domain/hash/other；role: attack/victim/unknown"""
    v = str(value).strip()
    if _CVE.match(v):
        return "cve", "vulnerability"
    if _IP.match(v):
        kind = "ip"
    elif _HASH.match(v):
        kind = "hash"
    elif _DOMAIN.match(v):
        kind = "domain"
    else:
        kind = "other"
    # 受害资产判定：与涉事单位域名重合 → victim
    role = "attack"
    orgs = _arr(tweet.get("org"))
    low = v.lower()
    for o in orgs:
        o = str(o).lower().strip()
        if o and (o in low or low in o):
            role = "victim"
            break
    return kind, role


def _csv_safe(v):
    """CSV 公式注入防护：= + - @ 开头加前导单引号"""
    s = "" if v is None else str(v)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def _snapshot(uid):
    """一致性快照读取（2026-11-09 E 修订）。

    先做新鲜度检查（写连接：底层情报变化 → 推进版本留痕），再开
    **独立只读连接**（WAL 快照）完成全部读取——与写请求完全隔离：
    导出与写交错不再出现 'cannot commit - no transaction is active'
    （共享连接 BEGIN 被写入隐式提交/回收的历史缺陷），且多次查询
    看到同一版本的数据。"""
    if not db.ensure_event_freshness(uid):
        return None  # 事件不存在
    with db.snapshot_conn() as ro:
        ev = db.get_event(uid, conn=ro)
        if not ev:
            return None
        return {"event": ev,
                "evidence": db.event_evidence(uid, conn=ro),
                "stats": db.evidence_stats(uid, conn=ro),
                "review": db.event_review_latest(uid, conn=ro),
                "review_state": db.event_review_state(uid, conn=ro),
                "revisions": db.event_revisions(uid, conn=ro)}


def _evidence_item(t):
    """单条证据（结构完整性 / 人工认可 / 有效性三表达，互不抹除）"""
    grade = integ.effective_grade(t)
    structural = integ.structural_grade(t)
    allowed, must_label, reason = integ.may_auto_downstream(t)
    rev_state, rev_ver, cur_ver = integ.review_state(t)
    iocs = _arr(t.get("ai_iocs"))
    orgs = _arr(t.get("org"))
    tags = _arr(t.get("tags"))
    return {
        # 身份契约（2026-11-09 缺陷 D）：tweet_id=tweets.id；evidence_id 仅审计
        "tweet_id": int(t.get("tweet_id") or 0),
        "evidence_id": int(t.get("evidence_id") or 0),
        "source": t.get("screen_name", ""),
        "url": t.get("url", ""),
        "published_at": t.get("published_at", ""),
        "collected_at": t.get("collected_at", ""),
        "text_hash": t.get("text_hash", ""),
        "title_zh": t.get("title_zh", ""),
        "text": t.get("text", ""),
        "ai": {
            "severity": t.get("ai_severity", ""),
            "type": t.get("ai_type", ""),
            "assessment": t.get("ai_assessment", ""),
            "confidence": int(t.get("ai_confidence") or 0),
            # 有效完整性（人工认可绑定当前版本时可升级为 verified）
            "integrity": grade,
            # 原始结构分级（partial/invalid 与修复痕迹不被人工审核抹掉）
            "structural_integrity": structural,
            "integrity_label": integ.label(t),
            "usable_as_conclusion": bool(allowed and not must_label),
            "must_label_unfinished": bool(must_label),
            # 人工认可三态：valid=绑定当前版本；stale=历史决定；none=未审核
            "review": {"state": rev_state,
                       "reviewed_version": rev_ver,
                       "current_version": cur_ver},
            "repair_notes": t.get("ai_repair", ""),
        },
        "geo": {"country": t.get("geo_country", ""), "province": t.get("geo_province", ""),
                "city": t.get("geo_city", ""), "industry": t.get("industry", "")},
        "organizations": orgs,
        "tags": tags,
        "iocs_raw": iocs,
        "evidence_kind": t.get("source_kind", "original"),
        "basis": t.get("basis", ""),
        "analysis_version": int(t.get("analysis_version") or 0),
    }


def export_event_json(uid):
    snap = _snapshot(uid)
    if not snap:
        return None
    ev = snap["event"]
    items = [_evidence_item(t) for t in snap["evidence"]]
    # IOC 分类（含 CVE 单列、受害资产、攻击指标、普通引用）
    iocs = {"cve": [], "attack": [], "victim": [], "other": []}
    seen = set()
    for t in snap["evidence"]:
        raw = _arr(t.get("ai_iocs"))
        for v in raw:
            key = (str(v), int(t.get("tweet_id") or 0))
            if str(v).strip() in seen:
                continue
            seen.add(str(v).strip())
            kind, role = _classify_ioc(v, t)
            rec = {"value": str(v), "kind": kind, "role": role,
                   "tweet_id": int(t.get("tweet_id") or 0),
                   "evidence_id": int(t.get("evidence_id") or 0),
                   "source": t.get("screen_name", ""),
                   "verified": integ.is_verified(t),
                   "integrity": integ.effective_grade(t)}
            if kind == "cve":
                iocs["cve"].append(rec)
            elif role == "victim":
                iocs["victim"].append(rec)
            elif role == "attack":
                iocs["attack"].append(rec)
            else:
                iocs["other"].append(rec)
    return {
        "schema_version": SCHEMA_VERSION,
        "exported_at": db.now(),
        "event": {
            "event_uid": ev["event_uid"],
            "version": int(ev["version"]),
            "title": ev["title"],
            "summary": ev["summary"],
            "status": ev["status"],
            "severity": ev["severity"],
            "first_seen": ev["first_seen"],
            "last_seen": ev["last_seen"],
            "created_at": ev["created_at"],
            "updated_at": ev["updated_at"],
        },
        "evidence_stats": snap["stats"],
        "review": ({"decision": snap["review"]["decision"],
                    "event_version": snap["review"]["event_version"],
                    "operator": snap["review"]["operator"],
                    "note": snap["review"]["note"],
                    "created_at": snap["review"]["created_at"],
                    # 历史决定标注：state=stale 时该决定只反映旧版本，不得当作当前批准
                    "state": snap["review_state"]["state"],
                    "current_version": snap["review_state"]["current_version"]}
                   if snap["review"] else None),
        "evidence": items,
        "iocs": iocs,
        "revisions": [{"version": r["version"], "action": r["action"],
                       "detail": r["detail"], "operator": r["operator"],
                       "created_at": r["created_at"]} for r in snap["revisions"]],
        "disclaimer": ("AI 字段为系统预研判，integrity=partial 表示研判未完成；"
                       "usable_as_conclusion=false 的条目不得作为已核验结论引用。"),
    }


def export_event_timeline_csv(uid):
    snap = _snapshot(uid)
    if not snap:
        return None
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["event_uid", "event_version", "published_at", "collected_at", "source",
                "evidence_kind", "basis", "title_zh", "severity", "category",
                "integrity", "usable_as_conclusion", "url"])
    ev = snap["event"]
    for t in snap["evidence"]:
        grade = integ.effective_grade(t)
        allowed, must_label, _ = integ.may_auto_downstream(t)
        w.writerow([_csv_safe(ev["event_uid"]), int(ev["version"]),
                    _csv_safe(t.get("published_at", "")), _csv_safe(t.get("created_at", "")),
                    _csv_safe(t.get("screen_name", "")), _csv_safe(t.get("source_kind", "")),
                    _csv_safe(t.get("basis", "")), _csv_safe(t.get("title_zh", "")),
                    _csv_safe(t.get("ai_severity", "")), _csv_safe(t.get("category", "")),
                    grade, "true" if (allowed and not must_label) else "false",
                    _csv_safe(t.get("url", ""))])
    return buf.getvalue()


def export_event_ioc_csv(uid):
    """IOC CSV（复用 JSON 的分类结果，保证口径一致）"""
    data = export_event_json(uid)
    if not data:
        return None
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["event_uid", "event_version", "value", "kind", "role", "tweet_id",
                "source", "verified", "integrity"])
    for group in ("cve", "attack", "victim", "other"):
        for rec in data["iocs"][group]:
            w.writerow([_csv_safe(data["event"]["event_uid"]), data["event"]["version"],
                        _csv_safe(rec["value"]), rec["kind"], rec["role"],
                        rec["tweet_id"], _csv_safe(rec["source"]),
                        "true" if rec["verified"] else "false", rec["integrity"]])
    return buf.getvalue()


def export_event_ioc_json(uid):
    data = export_event_json(uid)
    if not data:
        return None
    return {"schema_version": SCHEMA_VERSION, "exported_at": data["exported_at"],
            "event": data["event"], "iocs": data["iocs"],
            "disclaimer": data["disclaimer"]}
