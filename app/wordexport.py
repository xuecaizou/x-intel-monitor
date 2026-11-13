# -*- coding: utf-8 -*-
"""一键下载 Word：本地 pandoc 转换，产出真实可编辑 OOXML .docx。

边界（详见 docs/worklogs/collaborative-writing/findings.md）：
  - 原子捕获不可变修订（doccollab._save_revision kind=export），转换期间他人可继续编辑。
  - subprocess list 传参（无 shell 拼接）；用户内容不控制过滤器/脚本/转换参数/服务端路径。
  - 转换器只读取受控临时目录与该文档的已归档素材（data/doc_assets/<doc_uid>/）；
    远程图片一律不抓取，替换为明确缺图提示（不静默丢失）。
  - 超时/大小上限/并发信号量/临时目录清理；产物校验 ZIP+OOXML 部件+关键文本。
  - 下载不改变审核/交付/推送状态；与旧报告门禁（spec_gate）完全独立。
"""
import hashlib
import json
import re
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path

from . import db
from .config import load

_concurrency = threading.Semaphore(2)
_threads = set()
_threads_lock = threading.Lock()

ASSET_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")

STYLE_VERSION = "xintel-ref-zh-1"   # reference.docx 版本标识（变更时同步 scripts/make_reference_docx.py）


def _reference_docx() -> Path:
    p = Path(__file__).resolve().parent / "assets" / "reference.docx"
    if not p.exists():
        raise RuntimeError("reference.docx 缺失（app/assets/reference.docx）")
    return p


def pandoc_path(cfg=None):
    cfg = cfg or load()
    return (cfg.get("word") or {}).get("pandoc_path") or ""


def pandoc_version(path: str) -> str:
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", timeout=15)
        first = (r.stdout or "").splitlines()
        return first[0].strip()[:60] if first else ""
    except Exception:
        return ""


def _assets_dir(doc_uid) -> Path:
    return Path(db.DATA_DIR) / "doc_assets" / doc_uid


def resolve_images(doc_uid: str, md: str, tmp: Path):
    """受控素材解析：attachment: 引用复制进临时目录；远程/缺失 → 明确占位（缺图提示）。"""
    missing = []
    copied = []

    def _sub(m):
        alt, src = m.group(1), m.group(2).strip()
        if src.startswith("attachment:"):
            name = src[len("attachment:"):].strip()
            srcp = (_assets_dir(doc_uid) / name).resolve()
            try:
                srcp.relative_to(_assets_dir(doc_uid).resolve())
            except Exception:
                missing.append(src)
                return f"`[图片未嵌入：非法路径 {name}]`"
            if srcp.exists() and srcp.stat().st_size <= 20 * 1024 * 1024:
                dst = tmp / f"asset_{len(copied)}_{srcp.name}"
                shutil.copyfile(srcp, dst)
                copied.append(name)
                return f"![{alt}]({dst.name})"
            missing.append(src)
            return f"`[图片缺失：{name}]`"
        # 远程 http(s)/其他协议：不抓取，明确提示（保留可读链接文本）
        missing.append(src)
        return f"`[图片未嵌入（远程地址不自动抓取）：{alt or src}]`（{src}）"

    return ASSET_RE.sub(_sub, md), copied, missing


def _verify_docx(p: Path, expect_title: str) -> str:
    """结构校验：合法 ZIP + 必备 OOXML 部件 + 标题文本存在。返回错误信息（空=通过）。"""
    try:
        z = zipfile.ZipFile(p)
    except Exception as e:
        return f"产物不是合法 ZIP/OOXML：{type(e).__name__}"
    names = set(z.namelist())
    for need in ("word/document.xml", "word/styles.xml", "[Content_Types].xml"):
        if need not in names:
            return f"OOXML 缺少部件 {need}"
    try:
        doc = z.read("word/document.xml").decode("utf-8", "ignore")
    except Exception as e:
        return f"document.xml 读取失败：{type(e).__name__}"
    probe = (expect_title or "").strip()[:20]
    if probe and probe not in doc:
        return "产物中未找到文档标题（转换可能截断）"
    return ""


def create_export(doc_uid, operator, rev=None) -> int:
    """登记导出任务并启动后台转换线程。返回 export id。"""
    cfg = load()
    pp = pandoc_path(cfg)
    if not pp:
        raise RuntimeError("Word 导出未配置：请在配置 word.pandoc_path 指向 pandoc 可执行文件")
    if not Path(pp).exists():
        raise RuntimeError(f"pandoc 不存在：{pp}（部署说明见 docs/ops-runbook.md）")
    with db._lock:
        c = db.get_conn()
        cur = c.execute(
            "INSERT INTO doc_exports (doc_uid, rev, status, created_by, created_at)"
            " VALUES (?,?,'pending',?,?)", (doc_uid, rev if rev else -1, operator, db.now()))
        eid = cur.lastrowid
        c.commit()
    t = threading.Thread(target=_worker, args=(eid, doc_uid, rev), name=f"wordexport-{eid}",
                         daemon=True)
    with _threads_lock:
        _threads.add(t)
    t.start()
    return eid


def _worker(eid: int, doc_uid: str, rev):
    from . import doccollab
    with _concurrency:
        try:
            _run_export(eid, doc_uid, rev)
        except Exception as e:
            _fail(eid, f"{type(e).__name__}: {e}")
        finally:
            with _threads_lock:
                _threads.discard(threading.current_thread())


def _fail(eid, err):
    with db._lock:
        db.get_conn().execute("UPDATE doc_exports SET status='failed', error=?, finished_at=?"
                              " WHERE id=?", (str(err)[:500], db.now(), eid))
        db.get_conn().commit()


def _run_export(eid: int, doc_uid: str, rev):
    from . import doccollab
    cfg = load()
    wcfg = cfg.get("word") or {}
    timeout = int(wcfg.get("timeout_seconds") or 120)
    max_md = int(wcfg.get("max_md_bytes") or 2_000_000)
    pp = pandoc_path(cfg)
    # 1) 原子捕获版本（rev 指定则用历史修订，否则捕获当前为 export 修订）
    doc = doccollab.get_doc(doc_uid)
    if doc is None:
        return _fail(eid, "文档不存在")
    if rev:
        r = doccollab.get_revision(doc_uid, rev)
        if not r:
            return _fail(eid, f"修订 v{rev} 不存在")
        text, fixed_rev = r["body_md"], int(r["rev"])
    else:
        text = doccollab.current_text(doc_uid)
        chash0 = doccollab._hash_md(text)
        latest = doccollab.latest_revision(doc_uid)
        # 内容未变则复用最近修订（同 rev+同哈希 → 命中产物缓存）
        if latest and latest["content_hash"] == chash0:
            fixed_rev = int(latest["rev"])
        else:
            fixed_rev = doccollab._save_revision_sync(doc_uid, text, "export", "system",
                                                      skip_same=False)
    chash = doccollab._hash_md(text)
    if len(text.encode("utf-8")) > max_md:
        return _fail(eid, f"正文超过大小上限（{len(text.encode('utf-8'))} > {max_md} 字节）")
    # 2) 缓存命中：同 rev+同内容哈希+成功 → 复用产物（防过期文件）
    prev = db.get_conn().execute(
        "SELECT * FROM doc_exports WHERE doc_uid=? AND rev=? AND content_hash=?"
        " AND status='done' ORDER BY id DESC LIMIT 1", (doc_uid, fixed_rev, chash)).fetchone()
    if prev and prev["file_path"] and Path(prev["file_path"]).exists():
        with db._lock:
            db.get_conn().execute(
                "UPDATE doc_exports SET status='done', rev=?, content_hash=?, file_path=?,"
                " file_name=?, sha256=?, size=?, template_id=?, template_version=?,"
                " style_version=?, pandoc_version=?, finished_at=?, error='' WHERE id=?",
                (fixed_rev, chash, prev["file_path"], prev["file_name"], prev["sha256"],
                 prev["size"], doc.get("template_id"), doc.get("template_version"),
                 prev["style_version"], prev["pandoc_version"], db.now(), eid))
            db.get_conn().commit()
        return
    # 3) 临时目录 + 受控素材
    tmp = Path(db.DATA_DIR) / "word_exports" / f"tmp_{eid}"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        md_final, copied, missing = resolve_images(doc_uid, text, tmp)
        src_md = tmp / "document.md"
        src_md.write_text(md_final, encoding="utf-8")
        out_docx = tmp / "out.docx"
        ref = _reference_docx()
        cmd = [pp, "-f", "markdown", "-t", "docx",
               "--reference-doc", str(ref),
               "--resource-path", str(tmp),
               "--wrap=none",
               "-V", "lang=zh-CN",
               "--metadata", "lang=zh-CN",
               "-o", str(out_docx),
               str(src_md)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="ignore", timeout=timeout, cwd=str(tmp))
            if r.returncode != 0:
                return _fail(eid, f"pandoc 退出码 {r.returncode}：{(r.stderr or r.stdout or '')[:300]}")
        except subprocess.TimeoutExpired:
            return _fail(eid, f"pandoc 超时（>{timeout}s）")
        if not out_docx.exists() or out_docx.stat().st_size < 2000:
            return _fail(eid, "pandoc 未产出有效文件")
        # 校验探针：正文首个非空行（去除 Markdown 前缀）——模板骨架文档不含文档标题，不能拿 title 硬套
        first_line = ""
        for ln in text.splitlines():
            ln = ln.strip().lstrip("#").strip()
            if ln:
                first_line = ln[:20]
                break
        err = _verify_docx(out_docx, first_line)
        if err:
            return _fail(eid, err)
        # 4) 落位最终文件
        safe_title = re.sub(r'[\\/:*?"<>|\r\n]+', "_", doc.get("title") or doc_uid)[:60]
        fname = f"{safe_title}_v{fixed_rev}.docx"
        final_dir = Path(db.DATA_DIR) / "word_exports" / str(eid)
        final_dir.mkdir(parents=True, exist_ok=True)
        final = final_dir / fname
        shutil.move(str(out_docx), str(final))
        sha = hashlib.sha256(final.read_bytes()).hexdigest()
        with db._lock:
            db.get_conn().execute(
                "UPDATE doc_exports SET status='done', rev=?, content_hash=?, file_path=?,"
                " file_name=?, sha256=?, size=?, template_id=?, template_version=?,"
                " style_version=?, pandoc_version=?, missing_images=?, finished_at=?, error=''"
                " WHERE id=?",
                (fixed_rev, chash, str(final), fname, sha, final.stat().st_size,
                 doc.get("template_id"), doc.get("template_version"), STYLE_VERSION,
                 pandoc_version(pp), json.dumps(missing, ensure_ascii=False)[:500],
                 db.now(), eid))
            db.get_conn().commit()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def get_export(eid: int):
    r = db.get_conn().execute("SELECT * FROM doc_exports WHERE id=?", (int(eid),)).fetchone()
    return dict(r) if r else None


def list_exports(doc_uid, limit=20):
    return [dict(r) for r in db.get_conn().execute(
        "SELECT id, doc_uid, rev, status, error, file_name, sha256, size, style_version,"
        " pandoc_version, missing_images, created_by, created_at, finished_at"
        " FROM doc_exports WHERE doc_uid=? ORDER BY id DESC LIMIT ?",
        (doc_uid, int(limit))).fetchall()]
