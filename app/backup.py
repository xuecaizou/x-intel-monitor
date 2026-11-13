# -*- coding: utf-8 -*-
"""备份与恢复（第四批）。

备份：
  - SQLite 使用官方 backup API（online backup，含 WAL 未检查点数据），不在运行中复制主文件
  - 范围：数据库 + runtime.json + report_tasks 目录；生成 manifest（代码版本/schema 版本/时间/哈希）
恢复：
  - 恢复到指定目录（不覆盖现有数据），执行 integrity_check
  - 核验来源/情报/引用/投递/尝试/AI 任务/人工修订/实例身份
  - 恢复后默认暂停调度与外发（由启动参数/配置控制）
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 4


def _sha256_file(p: Path, chunk=1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _git_commit(project_root: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(project_root), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def create_backup(data_dir: Path, dest_dir: Path, project_root: Path | None = None):
    """在线一致性备份。返回 manifest dict。"""
    data_dir = Path(data_dir)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stamp_dir = dest_dir / f"xintel-backup-{ts}"
    stamp_dir.mkdir(parents=True, exist_ok=True)

    db_src = data_dir / "xintel.db"
    db_dst = stamp_dir / "xintel.db"
    # 在线备份 API：包含 WAL 中尚未 checkpoint 的事务
    with sqlite3.connect(str(db_src)) as src, sqlite3.connect(str(db_dst)) as dst:
        src.backup(dst)
    # 校验备份库
    with sqlite3.connect(str(db_dst)) as c:
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]

    # 配置（含密钥：仅保留在备份目录，权限由部署控制）
    rt = data_dir / "runtime.json"
    if rt.exists():
        shutil.copy2(rt, stamp_dir / "runtime.json")
    # 报告任务目录
    rtasks = data_dir / "report_tasks"
    if rtasks.exists():
        shutil.copytree(rtasks, stamp_dir / "report_tasks", dirs_exist_ok=True)

    files = []
    for p in sorted(stamp_dir.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            files.append({"path": str(p.relative_to(stamp_dir)), "size": p.stat().st_size,
                          "sha256": _sha256_file(p)})
    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "schema_version": SCHEMA_VERSION,
        "code_commit": _git_commit(project_root or Path(__file__).resolve().parent.parent),
        "sqlite_integrity": integrity,
        "contains_secrets": True,
        "note": "runtime.json 含 webhook/API Key；备份目录需按敏感数据保护",
        "files": files,
    }
    mpath = stamp_dir / "manifest.json"
    # manifest 自身也登记（自哈希留空，避免自引用）；恢复时按清单复制
    files.append({"path": "manifest.json", "size": 0, "sha256": ""})
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # 回填 manifest 自身大小
    manifest["files"] = files
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"dir": str(stamp_dir), "manifest": manifest}


def verify_backup(backup_dir: Path):
    """校验备份完整性（哈希 + SQLite integrity + 关键表存在）"""
    backup_dir = Path(backup_dir)
    mf = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    bad = []
    for f in mf["files"]:
        if not f.get("sha256"):
            continue   # manifest 自身
        p = backup_dir / f["path"]
        if not p.exists():
            bad.append(f"缺失 {f['path']}")
            continue
        if _sha256_file(p) != f["sha256"]:
            bad.append(f"哈希不符 {f['path']}")
    dbp = backup_dir / "xintel.db"
    checks = {}
    if dbp.exists():
        with sqlite3.connect(str(dbp)) as c:
            checks["integrity"] = c.execute("PRAGMA integrity_check").fetchone()[0]
            for t in ("tweets", "deliveries", "delivery_msgs", "delivery_attempts",
                      "ai_tasks", "report_refs", "report_tasks", "x_accounts", "rss_sources",
                      "events", "event_evidence", "event_revisions", "event_reviews",
                      "ai_result_conflicts", "auth_users", "auth_tokens",
                      "doc_templates", "template_versions", "writing_candidates",
                      "candidate_revisions", "ai_recommend_tasks", "doc_documents",
                      "doc_acl", "doc_updates", "doc_revisions", "doc_suggestions",
                      "doc_exports", "doc_agent_edits", "ws_tickets"):
                try:
                    checks[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except Exception as e:
                    checks[t] = f"ERR:{type(e).__name__}"
    return {"ok": not bad and checks.get("integrity") == "ok", "problems": bad, "counts": checks}


def restore_to(backup_dir: Path, target_dir: Path, project_root: Path | None = None,
               safe_mode: bool = True):
    """恢复到新目录（不覆盖现有数据）。返回恢复摘要 + 校验结果。

    安全语义（2026-11-09 G 修复，默认强制）：
      1. safe_mode=True：恢复后自动改写**恢复副本**的 runtime.json——
         scheduler.enabled=false、outbound.enable=false。备份原件不动；
         变更记录在摘要 safety_changes（含备份原件哈希与变更后哈希，
         两者明确区分——原件哈希不符≠恢复变更）。
      2. 旧会话与 Token 一律清除：旧备份可能包含已吊销 Token/过期会话，
         恢复绝不悄悄复活凭据；管理员用 data/admin_init.txt 重新取得访问
         （auth_reset 摘要记录清除数量）。
      3. verify_original：复制后立即按 manifest 全量校验（原件一致性）；
         安全变更发生在其后，最终校验只看 SQLite integrity。"""
    backup_dir = Path(backup_dir)
    target_dir = Path(target_dir)
    if target_dir.exists() and any(target_dir.iterdir()):
        raise RuntimeError(f"目标目录非空，拒绝覆盖：{target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)
    for f in json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))["files"]:
        src = backup_dir / f["path"]
        dst = target_dir / f["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    summary = {"safe_mode": bool(safe_mode), "safety_changes": [], "auth_reset": None}
    # 原件一致性：此时恢复副本与备份逐字节一致
    summary["verify_original"] = verify_backup(target_dir)
    if safe_mode:
        rt = target_dir / "runtime.json"
        if rt.exists():
            original_sha = _sha256_file(rt)
            data = {}
            try:
                data = json.loads(rt.read_text(encoding="utf-8") or "{}")
            except Exception:
                data = {}
            changed = {}
            sched = data.setdefault("scheduler", {})
            if sched.get("enabled", True) is not False:
                changed["scheduler.enabled"] = {"from": sched.get("enabled", True), "to": False}
            sched["enabled"] = False
            ob = data.setdefault("outbound", {})
            if ob.get("enable", True) is not False:
                changed["outbound.enable"] = {"from": ob.get("enable", True), "to": False}
            ob["enable"] = False
            rt.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            summary["safety_changes"].append({
                "file": "runtime.json",
                "backup_original_sha256": original_sha,      # 备份原件（复制后）哈希
                "restored_modified_sha256": _sha256_file(rt),  # 安全变更后哈希
                "changes": changed,
                "note": "恢复实例默认暂停调度、禁止外发；需恢复运行时人工显式开启并复核",
            })
        # 旧会话与 Token：明确清除，不复活已吊销凭据
        dbp = target_dir / "xintel.db"
        cleared = {"tokens": 0, "sessions": 0}
        if dbp.exists():
            try:
                with sqlite3.connect(str(dbp)) as c:
                    cleared["tokens"] = c.execute("DELETE FROM auth_tokens").rowcount or 0
                    cleared["sessions"] = c.execute("DELETE FROM auth_sessions").rowcount or 0
                    c.commit()
            except Exception as e:
                cleared["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        summary["auth_reset"] = {
            **cleared,
            "note": "旧备份可能含已吊销 Token/过期会话；恢复后一律清除，"
                    "管理员需通过 data/admin_init.txt 重新初始化密码",
        }
    # 最终校验：SQLite integrity（安全变更后）
    dbp = target_dir / "xintel.db"
    if dbp.exists():
        with sqlite3.connect(str(dbp)) as c:
            summary["integrity_final"] = c.execute("PRAGMA integrity_check").fetchone()[0]
    summary["ok"] = (summary["verify_original"].get("ok") is True
                     and summary.get("integrity_final", "ok") == "ok")
    return summary


def export_pending_deliveries(db_path: Path) -> dict:
    """降级流程用：导出未完成/结果不明的投递（暂停调度后执行）"""
    out = {"msgs": [], "deliveries": []}
    with sqlite3.connect(str(db_path)) as c:
        c.row_factory = sqlite3.Row
        for r in c.execute(
                "SELECT id, instance_id, channel_key, msg_type, state, attempts, detail,"
                " report_task_id FROM delivery_msgs WHERE state IN"
                " ('待发送','发送中','等待重试','结果不明')"):
            out["msgs"].append(dict(r))
        for r in c.execute(
                "SELECT id, tweet_id, instance_id, state, reason FROM deliveries"
                " WHERE state IN ('待发送','结果不明')"):
            out["deliveries"].append(dict(r))
    return out
