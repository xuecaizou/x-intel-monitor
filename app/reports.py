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
    package = {
        "title": title, "keywords": keywords or [], "tier": tier,
        "notes": notes or "", "tweets": tweets, "disclaimer": DISCLAIMER,
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
            " WHERE id=? AND status='写作中'",
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
    if t["status"] != "写作中":
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
    """人工确认推送：仅'已交付待推'；按通道能力分发——
    企微 markdown+附件两步法；钉钉 markdown（附件以目录提示替代）；
    PushPlus html 富文本；WxPusher markdown+线索包跳转卡片"""
    t = get_task(tid)
    if not t:
        return None
    if t["status"] != "已交付待推":
        return {"error": f"状态为 {t['status']}，仅'已交付待推'可推"}

    cfg = load()
    from .notify.base import image_base
    base = image_base()
    title_h = f"X-Intel 报告交付：{t['title'][:30]}"
    md = (f"**【报告交付】{t['title']}**" + chr(10)
          + f"> 档位：{t.get('tier','速报')} ｜ 字数：{t.get('word_count') or '—'}"
          + f" ｜ 门禁：{t.get('ledger_pass') or 0}/{t.get('ledger_total') or 0} PASS" + chr(10)
          + f"> gate：{t.get('gate_summary','').split(chr(10))[0] if t.get('gate_summary') else '—'}" + chr(10)
          + f"> 交付目录：{t.get('deliver_dir','')}")
    # 无文件能力的通道：补一条附件获取提示
    md_ext = md + (chr(10) + f"> 📎 报告交付附件：内网监控台 {base} 报告任务面板获取")
    # PushPlus 富文本版
    import html as _h
    def _e(s):
        return _h.escape(str(s if s is not None else ""), quote=False)
    html_body = (f"<b>【报告交付】{_e(t['title'])}</b><br/>"
                 f"档位：{_e(t.get('tier','速报'))} ｜ 字数：{_e(t.get('word_count') or '—')}"
                 f" ｜ 门禁：{_e(t.get('ledger_pass') or 0)}/{_e(t.get('ledger_total') or 0)} PASS<br/>"
                 f"gate：{_e((t.get('gate_summary') or '—').split(chr(10))[0])}<br/>"
                 f"交付目录：{_e(t.get('deliver_dir',''))}")
    # WxPusher 卡片跳转：线索包下载
    pkg_url = f"{base}/api/reports/tasks/{tid}/package"

    sent, results = 0, []
    from .engine import _enabled_channels
    for ch, cc, sim in _enabled_channels(cfg):
        # 附件（仅企微支持文件两步法，且真实模式）
        attach_note = ""
        if ch.key == "wecom" and "file" in ch.caps and not sim and t.get("deliver_dir") and t.get("docx"):
            for suffix_name in (t["docx"].rsplit(".", 1)[0] + "_批注.docx",
                                t["docx"].rsplit(".", 1)[0] + "_审阅.html"):
                fp = os.path.join(t["deliver_dir"], suffix_name)
                if os.path.isfile(fp) and os.path.getsize(fp) <= 20 * 1024 * 1024:
                    try:
                        from .wecom_media import push_file
                        r_ = push_file(cc["webhook"], fp)
                        attach_note += f" +{suffix_name}:{r_.get('errcode')}"
                    except Exception as e:
                        attach_note += f" +{suffix_name}:FAIL"
        try:
            if sim:
                ok, detail = True, "模拟模式（未配置凭据）"
            elif ch.key == "pushplus":
                ok, detail = ch.send(cc, title_h, md_ext, html=html_body)
            elif ch.key == "wxpusher":
                ok, detail = ch.send(cc, title_h, md_ext, url=pkg_url)
            else:
                # 企微等有 file 能力的通道：附件直推，消息不带附件提示行
                ok, detail = ch.send(cc, title_h, md_ext if "file" not in ch.caps else md)
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"[:200]
        status = "模拟成功" if (sim and ok) else ("成功" if ok else "失败")
        db.add_push_log(ch.display, f"报告交付：{t['title'][:22]} (任务{tid})",
                        status, detail + attach_note, content=md)
        results.append({"channel": ch.display, "status": status})
        sent += 1 if ok else 0

    # 结果语义（2026-11-08 修复）：真实成功才推进已推送；仅模拟不改变状态；全失败带错误返回
    real_ok = any(r["status"] == "成功" for r in results)
    sim_ok = any(r["status"] == "模拟成功" for r in results)
    if real_ok:
        with db._lock:
            db.get_conn().execute(
                "UPDATE report_tasks SET status='已推送', updated_at=? WHERE id=?",
                (db.now(), int(tid)))
            db.get_conn().commit()
        return {"id": int(tid), "status": "已推送", "pushed": True, "channels": results}
    if sim_ok:
        return {"id": int(tid), "status": "已交付待推", "pushed": False,
                "simulated_only": True,
                "error": "仅模拟成功（实例未配置真实凭据），不推进真实完成状态",
                "channels": results}
    return {"id": int(tid), "status": "已交付待推", "pushed": False,
            "error": "全部通道发送失败，任务保持已交付待推", "channels": results}


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
