# -*- coding: utf-8 -*-
"""Round3 第二批补丁：R02 选区上下文定位 / R12 restore 回落+导出绑定批准版本 /
   R14 标题探针 OOXML 可见文本 / R15 素材进缓存键 / R16 恢复后路径重写 /
   R17 模板发布快照读取。运行后自删。"""
import ast
import sys

sys.stdout.reconfigure(encoding="utf-8")

# ================= app/doccollab.py =================
s = open("app/doccollab.py", encoding="utf-8").read()

# R02: 上下文消歧锚点查找
old = '''def _find_unique(s: str, anchor: str) -> int:
    if not anchor:
        raise AnchorError(0, "锚点为空")
    first = s.find(anchor)
    if first < 0:
        raise AnchorError(0, "锚点在当前正文中零匹配")
    if s.find(anchor, first + 1) >= 0:
        raise AnchorError(2, "锚点在当前正文中多处匹配，拒绝猜测")
    return first'''
new = '''def _find_unique(s: str, anchor: str, before: str = "", after: str = "") -> int:
    """唯一锚点定位；多处匹配时用前后文消歧（R02：两段相同文字选第二段）。
    消歧规则：对每个出现位置计算"前文匹配长度+后文匹配长度"，
    仅当存在唯一最大且严格大于其他候选时采用；否则明确失败拒绝猜测。"""
    if not anchor:
        raise AnchorError(0, "锚点为空")
    idxs = []
    i = s.find(anchor)
    while i >= 0:
        idxs.append(i)
        i = s.find(anchor, i + 1)
    if not idxs:
        raise AnchorError(0, "锚点在当前正文中零匹配")
    if len(idxs) == 1:
        return idxs[0]
    if not (before or after):
        raise AnchorError(len(idxs), "锚点在当前正文中多处匹配，拒绝猜测")

    def _score(i):
        b = 0
        seg = s[max(0, i - len(before)):i]
        if before.endswith(seg):
            b = len(seg)
        e = 0
        seg2 = s[i + len(anchor): i + len(anchor) + len(after)]
        if after.startswith(seg2):
            e = len(seg2)
        return b + e

    scores = [_score(i) for i in idxs]
    best = max(scores)
    if best > 0 and scores.count(best) == 1:
        return idxs[scores.index(best)]
    raise AnchorError(len(idxs),
                      f"锚点在当前正文中多处匹配（{len(idxs)} 处），且前后文不足以唯一定位——"
                      "请扩大选区范围或直接手动修改")'''
assert old in s, "find_unique anchor"
s = s.replace(old, new, 1)

# create_suggestion 支持 before/after 上下文
old1b = '''def create_suggestion(doc_uid, agent, old_text, new_text, rationale="", sources=None,
                      target="", patch_kind="replace", client_id="", base_rev=None,
                      operator=None):
    """修改建议：创建时即校验锚点唯一匹配与版本；client_id 幂等。"""
    doc = get_doc(doc_uid)
    if not doc:
        raise DocError("文档不存在")
    if client_id:
        exist = db.get_conn().execute(
            "SELECT * FROM doc_suggestions WHERE doc_uid=? AND client_id=?",
            (doc_uid, client_id)).fetchone()
        if exist:
            return dict(exist), True
    latest = latest_revision(doc_uid)
    cur_rev = latest["rev"] if latest else 0
    if base_rev is None:
        base_rev = cur_rev
    if int(base_rev) != int(cur_rev):
        raise RevisionConflict(cur_rev)
    text = current_text(doc_uid)
    _find_unique(text, old_text)   # 零/多匹配直接失败'''
new1b = '''def create_suggestion(doc_uid, agent, old_text, new_text, rationale="", sources=None,
                      target="", patch_kind="replace", client_id="", base_rev=None,
                      operator=None, context_before="", context_after=""):
    """修改建议：创建时即校验锚点唯一匹配（支持前后文消歧，R02）与版本；client_id 幂等。"""
    doc = get_doc(doc_uid)
    if not doc:
        raise DocError("文档不存在")
    if client_id:
        exist = db.get_conn().execute(
            "SELECT * FROM doc_suggestions WHERE doc_uid=? AND client_id=?",
            (doc_uid, client_id)).fetchone()
        if exist:
            return dict(exist), True
    latest = latest_revision(doc_uid)
    cur_rev = latest["rev"] if latest else 0
    if base_rev is None:
        base_rev = cur_rev
    if int(base_rev) != int(cur_rev):
        raise RevisionConflict(cur_rev)
    text = current_text(doc_uid)
    _find_unique(text, old_text, context_before or "", context_after or "")   # 零/多匹配直接失败'''
assert old1b in s, "create_suggestion anchor"
s = s.replace(old1b, new1b, 1)

# apply_text_replace / _apply_replace_live / offline 支持 before/after（R02 改写接受路径经 apply）
old2 = '''def apply_text_replace(doc_uid, old_text, new_text, kind="agent_edit", operator="system",
                       base_rev_expected=None):'''
new2 = '''def apply_text_replace(doc_uid, old_text, new_text, kind="agent_edit", operator="system",
                       base_rev_expected=None, context_before="", context_after=""):'''
assert old2 in s, "atr anchor"
s = s.replace(old2, new2, 1)
s = s.replace(
    "_apply_replace_live(room, doc_uid, old_text, new_text, base_rev_expected, kind, operator),",
    "_apply_replace_live(room, doc_uid, old_text, new_text, base_rev_expected, kind, operator,\n                                context_before, context_after),")
s = s.replace(
    "async def _apply_replace_live(room, doc_uid, old_text, new_text, base_rev_expected, kind, operator):",
    "async def _apply_replace_live(room, doc_uid, old_text, new_text, base_rev_expected, kind, operator,\n                              context_before=\"\", context_after=\"\"):")

# _apply_replace_live：锁内两处 _find_unique 带上下文
old3 = '''        s_txt = str(t)
        i = _find_unique(s_txt, old_text)
        b_i = len(s_txt[:i].encode("utf-8"))
        b_j = b_i + len(old_text.encode("utf-8"))
        sv = room.ydoc.get_state()'''
new3 = '''        s_txt = str(t)
        i = _find_unique(s_txt, old_text, context_before or "", context_after or "")
        b_i = len(s_txt[:i].encode("utf-8"))
        b_j = b_i + len(old_text.encode("utf-8"))
        sv = room.ydoc.get_state()'''
assert old3 in s, "live find anchor"
s = s.replace(old3, new3, 1)

# offline 分支：find 位置前移到锁内需带上下文（当前在锁外 find）——把 find 移进锁内
old4 = '''        d = _replay_doc(doc_uid, (get_doc(doc_uid) or {}).get("initial_md") or "")
        t = d.get(TEXT_FIELD, type=Text)
        s_txt = str(t)
        i = _find_unique(s_txt, old_text)
        with db._lock:
            latest = db.get_conn().execute(
                "SELECT rev FROM doc_revisions WHERE doc_uid=? ORDER BY rev DESC LIMIT 1",
                (doc_uid,)).fetchone()
            cur = latest["rev"] if latest else 0
            if base_rev_expected is not None and int(base_rev_expected) != int(cur):
                raise RevisionConflict(cur)
            b_i = len(s_txt[:i].encode("utf-8"))'''
new4 = '''        d = _replay_doc(doc_uid, (get_doc(doc_uid) or {}).get("initial_md") or "")
        t = d.get(TEXT_FIELD, type=Text)
        s_txt = str(t)
        with db._lock:
            latest = db.get_conn().execute(
                "SELECT rev FROM doc_revisions WHERE doc_uid=? ORDER BY rev DESC LIMIT 1",
                (doc_uid,)).fetchone()
            cur = latest["rev"] if latest else 0
            if base_rev_expected is not None and int(base_rev_expected) != int(cur):
                raise RevisionConflict(cur)
            i = _find_unique(s_txt, old_text, context_before or "", context_after or "")
            b_i = len(s_txt[:i].encode("utf-8"))'''
assert old4 in s, "offline find anchor"
s = s.replace(old4, new4, 1)

# R12: restore 也使批准回落（纳入内容类修订）
old5 = '''        if kind in ("save", "agent_edit", "suggest_apply"):
            db.get_conn().execute(
                "UPDATE doc_documents SET status='编辑中' WHERE doc_uid=? AND status='已通过'",
                (doc_uid,))
        db.get_conn().execute("UPDATE doc_documents SET updated_at=? WHERE doc_uid=?",
                              (db.now(), doc_uid))
        db.get_conn().commit()'''
new5 = '''        if kind in ("save", "agent_edit", "suggest_apply", "restore"):
            db.get_conn().execute(
                "UPDATE doc_documents SET status='编辑中' WHERE doc_uid=? AND status='已通过'",
                (doc_uid,))
        db.get_conn().execute("UPDATE doc_documents SET updated_at=? WHERE doc_uid=?",
                              (db.now(), doc_uid))
        db.get_conn().commit()'''
assert old5 in s, "restore fallback anchor"
s = s.replace(old5, new5, 1)

open("app/doccollab.py", "w", encoding="utf-8").write(s)
ast.parse(s)
print("doccollab R02/R12 done")

# ================= api/writing_routes.py =================
w = open("api/writing_routes.py", encoding="utf-8").read()

# R02: 建议/agent 建议端点透传上下文
oldw = '''class SuggestionBody(BaseModel):
    old_text: str
    new_text: str
    rationale: str = ""
    sources: list = []
    target: str = ""
    client_id: str = ""
    base_rev: int | None = None'''
neww = '''class SuggestionBody(BaseModel):
    old_text: str
    new_text: str
    rationale: str = ""
    sources: list = []
    target: str = ""
    client_id: str = ""
    base_rev: int | None = None
    context_before: str = ""
    context_after: str = ""'''
assert oldw in w, "SuggestionBody anchor"
w = w.replace(oldw, neww, 1)

oldw2 = '''    try:
        row, existed = doccollab.create_suggestion(
            uid, agent=ctx["principal"].get("username", "user"), old_text=body.old_text,
            new_text=body.new_text, rationale=body.rationale, sources=body.sources,
            target=body.target, client_id=body.client_id, base_rev=body.base_rev)'''
neww2 = '''    try:
        row, existed = doccollab.create_suggestion(
            uid, agent=ctx["principal"].get("username", "user"), old_text=body.old_text,
            new_text=body.new_text, rationale=body.rationale, sources=body.sources,
            target=body.target, client_id=body.client_id, base_rev=body.base_rev,
            context_before=body.context_before, context_after=body.context_after)'''
assert oldw2 in w, "sug create anchor"
w = w.replace(oldw2, neww2, 1)

# AgentSuggestBody 同步
oldw3 = '''class AgentSuggestBody(BaseModel):
    old_text: str
    new_text: str
    rationale: str = ""
    sources: list = []
    target: str = ""
    client_id: str = ""
    base_rev: int | None = None'''
neww3 = '''class AgentSuggestBody(BaseModel):
    old_text: str
    new_text: str
    rationale: str = ""
    sources: list = []
    target: str = ""
    client_id: str = ""
    base_rev: int | None = None
    context_before: str = ""
    context_after: str = ""'''
assert oldw3 in w, "AgentSuggestBody anchor"
w = w.replace(oldw3, neww3, 1)

oldw4 = '''        row, existed = doccollab.create_suggestion(
            uid, agent=f"agent:{agent['name']}", old_text=body.old_text,
            new_text=body.new_text, rationale=body.rationale, sources=body.sources,
            target=body.target, client_id=body.client_id, base_rev=body.base_rev)'''
neww4 = '''        row, existed = doccollab.create_suggestion(
            uid, agent=f"agent:{agent['name']}", old_text=body.old_text,
            new_text=body.new_text, rationale=body.rationale, sources=body.sources,
            target=body.target, client_id=body.client_id, base_rev=body.base_rev,
            context_before=body.context_before, context_after=body.context_after)'''
assert oldw4 in w, "agent sug anchor"
w = w.replace(oldw4, neww4, 1)

# apply-edit 路由透传上下文
oldw5 = '''class AgentEditBody(BaseModel):
    base_rev: int
    old_text: str
    new_text: str
    rationale: str = ""
    client_id: str
    target: str = ""'''
neww5 = '''class AgentEditBody(BaseModel):
    base_rev: int
    old_text: str
    new_text: str
    rationale: str = ""
    client_id: str
    target: str = ""
    context_before: str = ""
    context_after: str = ""'''
assert oldw5 in w, "AgentEditBody anchor"
w = w.replace(oldw5, neww5, 1)

oldw6 = '''        r = doccollab.apply_text_replace(uid, body.old_text, body.new_text,
                                         kind="agent_edit", operator=f"agent:{agent['name']}",
                                         base_rev_expected=body.base_rev)'''
neww6 = '''        r = doccollab.apply_text_replace(uid, body.old_text, body.new_text,
                                         kind="agent_edit", operator=f"agent:{agent['name']}",
                                         base_rev_expected=body.base_rev,
                                         context_before=body.context_before,
                                         context_after=body.context_after)'''
assert oldw6 in w, "agent apply anchor"
w = w.replace(oldw6, neww6, 1)

# R17: agent_notes 读文档固定版本的快照
oldw7 = '''def _tpl_agent_notes(doc):
    if doc.get("template_id"):
        t = writing.get_template(doc["template_id"])
        if t:
            return t.get("agent_notes") or ""
    return ""'''
neww7 = '''def _tpl_agent_notes(doc):
    """R17：Agent 指令取文档固定模板版本的快照——模板后续未发布修改不生效。"""
    if doc.get("template_id"):
        ver = doc.get("template_version")
        if ver:
            row = db.get_conn().execute(
                "SELECT agent_notes FROM template_versions WHERE template_id=? AND version=?",
                (doc["template_id"], int(ver))).fetchone()
            if row is not None:
                return row["agent_notes"] or ""
        t = writing.get_template(doc["template_id"])
        if t:
            return t.get("agent_notes") or ""
    return ""'''
assert oldw7 in w, "agent notes anchor"
w = w.replace(oldw7, neww7, 1)

open("api/writing_routes.py", "w", encoding="utf-8").write(w)
ast.parse(w)
print("writing_routes R02/R17 done")

# ================= app/writing.py：R17 convert 用版本快照变量 =================
g = open("app/writing.py", encoding="utf-8").read()
oldg = '''        if tver and tpl:
            filled, _missing, _unknown = apply_variables(
                tver["body_md"], _jl(tpl["variables"], []),'''
newg = '''        if tver and tpl:
            # R17：变量定义取发布版本快照（tver.variables）——模板后续未发布的
            # 变量修改不得影响既有发布版本实例化
            filled, _missing, _unknown = apply_variables(
                tver["body_md"], _jl(tver["variables"], []),'''
assert oldg in g, "convert vars anchor"
g = g.replace(oldg, newg, 1)
open("app/writing.py", "w", encoding="utf-8").write(g)
ast.parse(g)
print("writing R17 done")

# ================= app/wordexport.py：R14 探针 + R12 默认导出绑批准版本 + R15 素材缓存键 =================
x = open("app/wordexport.py", encoding="utf-8").read()

# R14：探针用 OOXML 可见文本（去标签+XML 实体反转义），并剥离 Markdown 强调标记
oldx = '''def _verify_docx(p: Path, expect_title: str) -> str:
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
    return ""'''
newx = '''def _ooxml_visible_text(doc_xml: str) -> str:
    """R14：OOXML 可见文本（去标签 + XML 实体反转义）——标题含 & 等实体、
    Markdown 强调标记时不再误判失败。"""
    import html as _html
    return _html.unescape(re.sub(r"<[^>]+>", "", doc_xml))


def _probe_from(md_text: str) -> str:
    """探针文本：正文首个非空行，剥离 Markdown 前缀与强调标记。"""
    import re as _re
    for ln in (md_text or "").splitlines():
        ln = ln.strip()
        ln = _re.sub(r"^(#{1,6}\\s+)", "", ln).strip()
        ln = ln.replace("**", "").replace("*", "").replace("`", "").strip()
        if ln:
            return ln[:20]
    return ""


def _verify_docx(p: Path, expect_title: str) -> str:
    """结构校验：合法 ZIP + 必备 OOXML 部件 + 探针文本存在（R14 可见文本口径）。"""
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
    if probe and probe not in _ooxml_visible_text(doc):
        return "产物中未找到文档标题（转换可能截断）"
    return ""'''
assert oldx in x, "verify anchor"
x = x.replace(oldx, newx, 1)

# _run_export：探针改用 _probe_from；默认导出绑定批准版本（R12）；素材哈希进缓存键（R15）
oldx2 = '''    if rev:
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
                                                      skip_same=False)'''
newx2 = '''    draft_export = bool((body_draft or {}).get("draft")) if body_draft else False
    if rev:
        r = doccollab.get_revision(doc_uid, rev)
        if not r:
            return _fail(eid, f"修订 v{rev} 不存在")
        text, fixed_rev = r["body_md"], int(r["rev"])
    elif doc.get("status") == "已通过" and doc.get("review_rev") and not draft_export:
        # R12：已批准文档默认导出**批准时修订**——当前草稿可能包含未批准内容；
        # 明确要导出草稿须显式 draft=true（产物记录中标注）。
        r = doccollab.get_revision(doc_uid, int(doc["review_rev"]))
        if not r:
            return _fail(eid, f"批准修订 v{doc['review_rev']} 不存在")
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
                                                      skip_same=False)'''
assert oldx2 in x, "capture anchor"
x = x.replace(oldx2, newx2, 1)

# 函数签名加 body_draft 透传
oldx3 = '''def _worker(eid: int, doc_uid: str, rev):
    from . import doccollab
    with _concurrency:
        try:
            _run_export(eid, doc_uid, rev)'''
newx3 = '''def _worker(eid: int, doc_uid: str, rev, draft=False):
    from . import doccollab
    with _concurrency:
        try:
            _run_export(eid, doc_uid, rev, draft)'''
assert oldx3 in x, "worker anchor"
x = x.replace(oldx3, newx3, 1)
oldx4 = '''def _run_export(eid: int, doc_uid: str, rev):'''
newx4 = '''def _run_export(eid: int, doc_uid: str, rev, draft=False):'''
assert oldx4 in x, "runexport anchor"
x = x.replace(oldx4, newx4, 1)

# 探针调用点替换（原 first_line 逻辑改为 _probe_from(text)）
oldx5 = '''        # 校验探针：正文首个非空行（去除 Markdown 前缀）——模板骨架文档不含文档标题，不能拿 title 硬套
        first_line = ""
        for ln in text.splitlines():
            ln = ln.strip().lstrip("#").strip()
            if ln:
                first_line = ln[:20]
                break
        err = _verify_docx(out_docx, first_line)'''
newx5 = '''        # 校验探针：正文首个非空行（R14 可见文本口径，剥离 Markdown 标记）
        err = _verify_docx(out_docx, _probe_from(text))'''
assert oldx5 in x, "probe call anchor"
x = x.replace(oldx5, newx5, 1)

# R15：素材指纹进缓存键——resolve_images 移到缓存检查前，assets sha 并入 chash
oldx6 = '''    chash = doccollab._hash_md(text)
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
        md_final, copied, missing = resolve_images(doc_uid, text, tmp)'''
newx6 = '''    chash = doccollab._hash_md(text)
    if len(text.encode("utf-8")) > max_md:
        return _fail(eid, f"正文超过大小上限（{len(text.encode('utf-8'))} > {max_md} 字节）")
    # 3) 临时目录 + 受控素材（R15：先解析素材——素材内容参与缓存键，同名换图不再命中旧产物）
    tmp = Path(db.DATA_DIR) / "word_exports" / f"tmp_{eid}"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        md_final, copied, missing = resolve_images(doc_uid, text, tmp)
        import hashlib as _hl
        assets_sha = ""
        for name in sorted(copied):
            ap = tmp / f"asset_{copied.index(name)}_{name}"
            if ap.exists():
                assets_sha += _hl.sha256(ap.read_bytes()).hexdigest()[:16]
        chash = (chash + ":" + assets_sha) if assets_sha else chash
        # 2) 缓存命中：同 rev+同内容+同素材哈希+成功 → 复用产物（R15：素材变更不命中）
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
            return'''
assert oldx6 in x, "cache anchor"
x = x.replace(oldx6, newx6, 1)

open("app/wordexport.py", "w", encoding="utf-8").write(x)
ast.parse(x)
print("wordexport R12/R14/R15 done")
