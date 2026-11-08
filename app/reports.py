"""报告任务模块：任务 CRUD + 线索包生成 + claim 原子领取 + 门禁脚本复跑验收 + 推送。

与外部 ZCode report-writer Agent 桥接（桥接而非内置）：
- 系统建任务/发线索包/复跑门禁验收/推送，Agent 按内部规范写作
- Agent 交付回写不信任自报结果，系统必须自己复跑门禁脚本（runtime.json report.gate_script 配置）以系统结果为准
"""
import hashlib
import json
import os
import subprocess
import time
from urllib.parse import quote

from . import db
from . import integrity as _integ
from .config import load

DATA_SUBDIR = "report_tasks"


def _gate_script():
    """门禁脚本绝对路径：runtime.json report.gate_script 配置（未配置/文件不存在返回空，交付转待人工）"""
    p = ((load().get("report") or {}).get("gate_script") or "").strip()
    if p and os.path.isfile(p):
        return os.path.normpath(p)
    return ""

STATUSES = ["待写", "写作中", "已交付待推", "已推送", "待人工", "已取消"]


def _tasks_dir():
    d = os.path.join(db.DATA_DIR, DATA_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


# ---------- 线索包 ----------

def _tweet_package(t):
    """单条推文 → 线索包条目（AI 字段标注仅供参考）"""
    def j(v):
        try:
            return json.loads(v or "[]")
        except Exception:
            return []
    return {
        "tweet_id": t.get("tweet_id"),
        "screen_name": t.get("screen_name"),
        "created_at": t.get("created_at"),
        "url": t.get("url"),
        "text": t.get("text"),
        "title_zh": t.get("title_zh", ""),
        "text_zh": t.get("text_zh", ""),
        "ai": {
            "severity": t.get("ai_severity", ""),
            "type": t.get("ai_type", ""),
            "assessment": t.get("ai_assessment", ""),
            "recommendations": j(t.get("ai_recommendations")),
            "iocs": j(t.get("ai_iocs")),
            "confidence": t.get("ai_confidence", 0),
            "tags": j(t.get("tags")),
            # L2：完整性分级——Agent 必须据此决定是否可自动引用为结论
            "integrity": _integ.effective_grade(t),
            "integrity_label": _integ.label(t),
            "may_auto_downstream": _integ.may_auto_downstream(t)[0],
            "must_label_unfinished": _integ.may_auto_downstream(t)[1],
            "repair_notes": t.get("ai_repair", ""),
        },
        "geo": {
            "country": t.get("geo_country", ""),
            "province": t.get("geo_province", ""),
            "city": t.get("geo_city", ""),
            "organizations": j(t.get("org")),
            "industry": t.get("industry", ""),
        },
        "media": [t["media_url"]] if t.get("media_url") else [],
    }


DISCLAIMER = ("tweets内AI字段（title_zh/text_zh/ai/geo）由系统预研判生成，仅供参考——"
              "使用前必须回推文原文核措辞；本包不替代 Agent 的外部检索与溯源核查步骤")


def create_task(title, keywords, tier, notes, tweet_ids, auto_push=0, cfg=None):
    """建任务：从 tweets 表按 id 取数生成线索包落盘，入库 status=待写"""
    cfg = cfg or load()
    tier = tier if tier in ("速报", "完整") else "速报"
    ids = [int(i) for i in (tweet_ids or [])][:50]
    tweets = []
    for tid in ids:
        t = db.get_tweet(tid)
        if t:
            tweets.append(_tweet_package(t))
    partial_n = sum(1 for x in tweets if x["ai"]["integrity"] in ("partial", "invalid"))
    package = {
        "title": title, "keywords": keywords or [], "tier": tier,
        "notes": notes or "", "tweets": tweets, "disclaimer": DISCLAIMER,
        "integrity_summary": {
            "total": len(tweets),
            "partial_or_invalid": partial_n,
            "note": ("包含部分/无效 AI 结果，不得直接作为已核验结论引用"
                     if partial_n else "全部为完整研判"),
        },
    }
    with db._lock:
        cur = db.get_conn().execute(
            "INSERT INTO report_tasks (title, keywords, tier, notes, tweet_ids,"
            " auto_push, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (title, json.dumps(keywords or [], ensure_ascii=False), tier, notes or "",
             json.dumps(ids, ensure_ascii=False), int(auto_push), db.now(), db.now()))
        task_id = cur.lastrowid
        pdir = _tasks_dir()
        package["task_id"] = task_id
        ppath = os.path.join(pdir, f"task_{task_id}.json")
        with open(ppath, "w", encoding="utf-8") as f:
            json.dump(package, f, ensure_ascii=False, indent=1)
        db.get_conn().execute("UPDATE report_tasks SET package_path=? WHERE id=?",
                              (ppath, task_id))
        db.get_conn().commit()
    _register_refs(task_id, ids)
    return {"id": task_id, "status": "待写", "package_path": ppath}


def get_task(tid):
    r = db.get_conn().execute("SELECT * FROM report_tasks WHERE id=?", (int(tid),)).fetchone()
    if not r:
        return None
    d = dict(r)
    for col in ("keywords", "tweet_ids", "warnings"):
        try:
            d[col] = json.loads(d.get(col) or "[]")
        except Exception:
            d[col] = []
    return d


def list_tasks(status="", limit=20, offset=0):
    conds, args = [], []
    if status:
        conds.append("status=?")
        args.append(status)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    c = db.get_conn()
    total = c.execute("SELECT COUNT(*) AS n FROM report_tasks" + where, args).fetchone()["n"]
    rows = c.execute("SELECT * FROM report_tasks" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                     args + [int(limit), int(offset)]).fetchall()
    return total, [_normalize_task(dict(r)) for r in rows]


def _normalize_task(d):
    for col in ("keywords", "tweet_ids", "warnings"):
        try:
            d[col] = json.loads(d.get(col) or "[]")
        except Exception:
            d[col] = []
    return d


# ---------- 状态机（严格按交接文档：待写→写作中→已交付待推→已推送；待人工分支） ----------

def claim(tid, assignee):
    """原子领取：待写→写作中；并发双领只有一个成功"""
    with db._lock:
        cur = db.get_conn().execute(
            "UPDATE report_tasks SET status='写作中', assignee=?, updated_at=?"
            " WHERE id=? AND status='待写'",
            (assignee or "report-writer@unknown", db.now(), int(tid)))
        db.get_conn().commit()
        return cur.rowcount > 0


def heartbeat(tid, assignee, note=""):
    with db._lock:
        cur = db.get_conn().execute(
            "UPDATE report_tasks SET assignee=?, updated_at=? WHERE id=? AND status='写作中'",
            (assignee or "report-writer@unknown", db.now(), int(tid)))
        db.get_conn().commit()
        return cur.rowcount > 0


def abort(tid, note=""):
    with db._lock:
        cur = db.get_conn().execute(
            "UPDATE report_tasks SET status='待写', assignee='', updated_at=?, notes=notes||? "
            " WHERE id=? AND status IN ('写作中','待人工')",
            (db.now(), (chr(10) + "退回: " + note) if note else "", int(tid)))
        db.get_conn().commit()
        return cur.rowcount > 0


# ---------- 系统复跑门禁脚本验收 ----------

def _run_gate(docx_path, tier, word_limit=None):
    """复跑机械门禁，返回 (exit_code, gate_summary)"""
    cmd = ["py", "-3.13", _gate_script(), docx_path, "--档位", tier]
    if word_limit:
        cmd += ["--字数上限", str(word_limit)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", timeout=180)
        exit_code = r.returncode
        lines = [ln for ln in (r.stdout or "").splitlines()
                 if ln.startswith("GATE_RESULT") or "❌" in ln or "⚠️" in ln]
        summary = chr(10).join(lines[-12:]) if lines else f"exit {exit_code}"
    except subprocess.TimeoutExpired:
        exit_code, summary = 999, "门禁脚本超时（>180s）"
    except FileNotFoundError as e:
        exit_code, summary = 999, f"py launcher 不可用: {e}"
    except Exception as e:
        exit_code, summary = 999, f"复跑异常: {type(e).__name__}: {str(e)[:120]}"
    return exit_code, summary


def deliver(tid, payload):
    """交付回写 + 系统复跑门禁脚本验收（本模块的灵魂，不信任 agent_gate）"""
    t = get_task(tid)
    if not t:
        return None
    if t["status"] not in ("写作中", "待人工"):
        return {"conflict": True, "status": t["status"]}

    deliver_dir = payload.get("deliver_dir", "")
    docx = payload.get("docx", "")
    docx_abs = os.path.join(deliver_dir, docx) if deliver_dir and docx else ""
    exists = bool(docx_abs and os.path.isfile(docx_abs))

    updates = {
        "deliver_dir": deliver_dir, "docx": docx,
        "word_count": payload.get("word_count"),
        "ledger_total": payload.get("ledger_total"),
        "ledger_pass": payload.get("ledger_pass"),
        "warnings": json.dumps(payload.get("warnings") or [], ensure_ascii=False),
        "agent_gate": payload.get("agent_gate", ""),
        "updated_at": db.now(),
    }
    auto_push = bool(t.get("auto_push")) or bool(payload.get("auto_push"))

    if not exists:
        # 交付文件不存在 → 宁可拦住不可放过
        updates.update({"status": "待人工",
                        "gate_exit": 999,
                        "gate_summary": "交付文件不存在: " + docx_abs})
    else:
        gate_exit, gate_summary = _run_gate(
            docx_abs, t.get("tier", "速报"), payload.get("word_limit"))
        updates.update({"gate_exit": gate_exit, "gate_summary": gate_summary,
                        "status": "已交付待推" if gate_exit == 0 else "待人工"})

    with db._lock:
        sets = ", ".join(f"{k}=?" for k in updates)
        db.get_conn().execute(f"UPDATE report_tasks SET {sets} WHERE id=?",
                              list(updates.values()) + [int(tid)])
        db.get_conn().commit()

    # auto_push=1 且验收 PASS → 立即推送
    if updates.get("status") == "已交付待推" and auto_push:
        push_task(tid)
        updates["status"] = get_task(tid)["status"]

    return {"id": int(tid), "status": updates.get("status"),
            "gate_exit": updates.get("gate_exit"),
            "gate_summary": updates.get("gate_summary", "")}


def push_task(tid):
    """人工确认推送（仅'已交付待推/部分已推送'）。

    原子领取：置'推送中'（10 分钟陈旧自动回收，防并发双推/进程中断卡死）。
    分项交付：每实例一条 delivery_msgs（msg_type=report），正文/附件分项记入 detail，
    附件失败不得被正文成功掩盖（状态=部分成功）。
    补发：'部分已推送' 再推送时跳过上次已成功实例，仅补失败实例。
    状态语义：全部真实实例成功→已推送；混合→部分已推送；仅模拟/全失败→保持原状态并带说明。
    """
    t = get_task(tid)
    if not t:
        return None
    if not (load().get("outbound") or {}).get("enable", True):
        return {"error": "外发已禁用（outbound.enable=false）：恢复/演练模式不允许推送报告"}
    prev_status = t["status"]
    with db._lock:
        cur = db.get_conn().execute(
            "UPDATE report_tasks SET status='推送中', updated_at=? WHERE id=? AND ("
            " status IN ('已交付待推','部分已推送')"
            " OR (status='推送中' AND updated_at < datetime('now','localtime','-10 minutes')))",
            (db.now(), int(tid)))
        db.get_conn().commit()
        if cur.rowcount == 0:
            cur2 = db.get_conn().execute(
                "SELECT status FROM report_tasks WHERE id=?", (int(tid),)).fetchone()
            return {"error": f"任务正在推送或状态不允许（当前：{cur2['status'] if cur2 else '?'}）"}

    def _restore():
        with db._lock:
            db.get_conn().execute(
                "UPDATE report_tasks SET status=?, updated_at=? WHERE id=? AND status='推送中'",
                (prev_status, db.now(), int(tid)))
            db.get_conn().commit()

    try:
        cfg = load()
        from .notify.base import image_base
        base = image_base()
        title_h = f"X-Intel 报告交付：{t['title'][:30]}"
        md = (f"**【报告交付】{t['title']}**" + chr(10)
              + f"> 档位：{t.get('tier','速报')} ｜ 字数：{t.get('word_count') or '—'}"
              + f" ｜ 门禁：{t.get('ledger_pass') or 0}/{t.get('ledger_total') or 0} PASS" + chr(10)
              + f"> gate：{t.get('gate_summary','').split(chr(10))[0] if t.get('gate_summary') else '—'}" + chr(10)
              + f"> 交付目录：{t.get('deliver_dir','')}")
        md_ext = md + (chr(10) + f"> 📎 报告交付附件：内网监控台 {base} 报告任务面板获取")
        import html as _h

        def _e(x):
            return _h.escape(str(x if x is not None else ""), quote=False)
        html_body = (f"<b>【报告交付】{_e(t['title'])}</b><br/>"
                     f"档位：{_e(t.get('tier','速报'))} ｜ 字数：{_e(t.get('word_count') or '—')}"
                     f" ｜ 门禁：{_e(t.get('ledger_pass') or 0)}/{_e(t.get('ledger_total') or 0)} PASS<br/>"
                     f"gate：{_e((t.get('gate_summary') or '—').split(chr(10))[0])}<br/>"
                     f"交付目录：{_e(t.get('deliver_dir',''))}")
        pkg_url = f"{base}/api/reports/tasks/{tid}/package"

        # 上次各实例结果：部分补发跳过已成功实例
        prev_msgs = db.msgs_report(tid)
        prev = {m["instance_id"]: m["state"] for m in prev_msgs}
        # 分项明细：实例→{item: ok}（部分补发时跳过已成功分项）
        prev_items = {}
        for m in prev_msgs:
            try:
                det = json.loads(m.get("detail") or "{}")
                prev_items[m["instance_id"]] = {i["item"]: i["ok"] for i in det.get("items") or []}
            except Exception:
                prev_items[m["instance_id"]] = {}
        results, any_fail = [], False
        sim_any = False
        from .engine import _iid_channels
        for ch, cc, sim, iid in _iid_channels(cfg):
            if prev.get(iid) == "成功":
                results.append({"channel": ch.display, "status": "跳过（上次已成功）", "iid": iid})
                continue
            items, attach_note = [], ""
            prev_it = prev_items.get(iid) or {}
            skip_body = prev_it.get("正文") is True and prev.get(iid) == "部分成功"
            try:
                if sim:
                    ok, detail = True, "模拟模式（未配置凭据）"
                elif skip_body:
                    ok, detail = True, "跳过（上次正文已成功）"
                elif ch.key == "pushplus":
                    ok, detail = ch.send(cc, title_h, md_ext, html=html_body)
                elif ch.key == "wxpusher":
                    ok, detail = ch.send(cc, title_h, md_ext, url=pkg_url)
                else:
                    ok, detail = ch.send(cc, title_h, md_ext if "file" not in ch.caps else md)
            except Exception as e:
                ok, detail = False, f"{type(e).__name__}: {e}"[:200]
            items.append({"item": "正文", "ok": bool(ok),
                          "detail": ("跳过（上次已成功）" if skip_body else
                                     str(detail)[:120] if not ok else "")})
            # 附件分项（仅企微真实模式；模块缺失/单附件失败→分项失败，不得被正文成功掩盖）
            if ch.key == "wecom" and "file" in ch.caps and not sim and t.get("deliver_dir") and t.get("docx"):
                _push_file = None
                try:
                    from .wecom_media import push_file as _push_file
                except Exception as e:
                    items.append({"item": "附件模块", "ok": False, "detail": str(e)[:120]})
                if _push_file is not None:
                    for suffix_name in (t["docx"].rsplit(".", 1)[0] + "_批注.docx",
                                        t["docx"].rsplit(".", 1)[0] + "_审阅.html"):
                        fp = os.path.join(t["deliver_dir"], suffix_name)
                        if prev_it.get(suffix_name) is True:
                            items.append({"item": suffix_name, "ok": True,
                                          "detail": "跳过（上次已成功）"})
                            attach_note += f" +{suffix_name}:跳过"
                            continue
                        if not os.path.isfile(fp):
                            items.append({"item": suffix_name, "ok": False, "detail": "交付目录中不存在"})
                            attach_note += f" +{suffix_name}:缺失"
                            continue
                        if os.path.getsize(fp) > 20 * 1024 * 1024:
                            items.append({"item": suffix_name, "ok": False, "detail": "超过 20MB"})
                            attach_note += f" +{suffix_name}:超限"
                            continue
                        try:
                            r_ = _push_file(cc["webhook"], fp)
                            iok = r_.get("errcode") == 0
                            items.append({"item": suffix_name, "ok": iok, "detail": str(r_)[:120]})
                            attach_note += f" +{suffix_name}:{r_.get('errcode')}"
                        except Exception as e:
                            items.append({"item": suffix_name, "ok": False, "detail": str(e)[:120]})
                            attach_note += f" +{suffix_name}:FAIL"
            if sim and ok:
                status_i = "模拟成功"
                sim_any = True
            elif ok and all(i["ok"] for i in items):
                status_i = "成功"
            elif any(i["ok"] for i in items):
                status_i = "部分成功"
                any_fail = True
            else:
                status_i = "失败"
                any_fail = True
            mid = db.create_msg(cc.get("iid") or "", ch.key, "report", "", "", title_h, md,
                                None, report_task_id=tid)
            db.finish_msg(mid, status_i, json.dumps({"items": items}, ensure_ascii=False)[:400])
            db.add_push_log(ch.display, f"报告交付：{t['title'][:22]} (任务{tid})",
                            status_i, detail + attach_note, content=md)
            results.append({"channel": ch.display, "status": status_i, "iid": iid})

        real_ok = any(r["status"] == "成功" for r in results)
        real_partial = any(r["status"] == "部分成功" for r in results)
        sim_only = (not real_ok) and (not real_partial) and sim_any             and not any(r["status"] == "失败" for r in results)
        if real_ok and not any_fail:
            final = "已推送"
        elif real_ok or real_partial:
            final = "部分已推送"  # 有真实进展但存在失败分项/实例；可再推送补失败部分
        elif sim_only:
            final = prev_status  # 仅模拟：不推进真实完成
        else:
            final = prev_status  # 全失败/全跳过：保持
        if final in ("已推送", "部分已推送"):
            with db._lock:
                db.get_conn().execute(
                    "UPDATE report_tasks SET status=?, updated_at=? WHERE id=? AND status='推送中'",
                    (final, db.now(), int(tid)))
                db.get_conn().commit()
        else:
            _restore()
        out = {"id": int(tid), "status": final, "pushed": final in ("已推送", "部分已推送"),
               "channels": results}
        if final == prev_status and not (real_ok or sim_any):
            out["error"] = "全部通道发送失败，任务保持原状态"
        if sim_only:
            out["simulated_only"] = True
            out["error"] = "仅模拟成功（实例未配置真实凭据），不推进真实完成状态"
        return out
    except Exception as e:
        _restore()
        return {"error": f"推送异常已回滚状态：{type(e).__name__}: {e}"[:200]}


def _register_refs(report_id, tweet_ids):
    """报告→原始情报引用登记（可靠查询入口；UNIQUE 幂等）。删除任务后保留（保守保护）。"""
    for tid0 in (tweet_ids or []):
        try:
            with db._lock:
                db.get_conn().execute(
                    "INSERT OR IGNORE INTO report_refs (report_id, tweet_id, created_at) VALUES (?,?,?)",
                    (int(report_id), int(tid0), db.now()))
            db.get_conn().commit()
        except Exception:
            continue


def refs_for(report_id):
    return [r["tweet_id"] for r in db.get_conn().execute(
        "SELECT tweet_id FROM report_refs WHERE report_id=? ORDER BY tweet_id",
        (int(report_id),)).fetchall()]


def delete_task(tid):
    t = get_task(tid)
    if not t:
        return False
    if t["status"] == "写作中":
        return False  # 409 由路由层抛
    with db._lock:
        db.get_conn().execute("DELETE FROM report_tasks WHERE id=?", (int(tid),))
        db.get_conn().commit()
    return True
