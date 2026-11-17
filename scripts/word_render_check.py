# -*- coding: utf-8 -*-
"""round4 · Word 交付渲染级验收（隔离实例，零生产写入、零真实外联）。

真实协议链路：管理员登录（username/password → cookie+CSRF）→ 模板发布 →
建报告（initial_md 承载长中文/标题/列表/表格/图片/链接/特殊字符）→
上传 PNG 素材 → 提交审阅（快照契约）→ 批准 → 导出 DOCX → 三层独立验证：
  1) zipfile OOXML 部件与 media 图片
  2) python-docx 独立解析（标题/表格/段落/图片）
  3) pandoc DOCX→HTML 独立渲染（内容标记核对）
附加：Word COM 打开验证（尝试一次，被模态阻塞则如实记录受阻）。

运行：python scripts/word_render_check.py
产物：logs/round4-artifacts/render_check.docx + render_evidence.json
"""
import base64
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from tests.testenv import TestEnv  # noqa: E402

env = TestEnv(forbid_network=True).start()
(env.tmp / "runtime.json").write_text(json.dumps({
    "scheduler": {"enabled": False},
}), encoding="utf-8")

import app.db as db  # noqa: E402
import app.auth as auth  # noqa: E402

db.DATA_DIR = env.tmp
db._conn = None
db.init_db()
auth.init_auth_tables()
auth.create_admin("boss", "RenderPass123")

import uvicorn  # noqa: E402
from app.main import app  # noqa: E402

PASS, FAIL, BLOCKED = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f" | {str(detail)[:140]}"))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = _free_port()
BASE = f"http://127.0.0.1:{PORT}"
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(40):
    try:
        urllib.request.urlopen(BASE + "/api/auth/status" if False else BASE + "/", timeout=1)
        break
    except Exception:
        time.sleep(0.3)

cj = __import__("http.cookiejar", fromlist=["CookieJar"]).CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
SESSION = {"cookie": "", "csrf": ""}


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json",
                 "Cookie": SESSION["cookie"],
                 "X-CSRF-Token": SESSION["csrf"]},
        method=method)
    resp = op.open(r, timeout=25)
    return json.loads(resp.read())


def raw(path):
    r = urllib.request.Request(BASE + path, headers={"Cookie": SESSION["cookie"]})
    return op.open(r, timeout=30)


# 1) 登录（真实协议：username/password → cookie + csrf）
login_body = {"username": "boss", "password": "RenderPass123"}
lr = urllib.request.Request(BASE + "/api/auth/login", data=json.dumps(login_body).encode(),
                            headers={"Content-Type": "application/json"}, method="POST")
resp = op.open(lr, timeout=20)
ld = json.loads(resp.read())
sc = resp.headers.get("Set-Cookie", "")
SESSION["cookie"] = sc.split(";")[0]
SESSION["csrf"] = ld["csrf"]
check("L-1 管理员登录（cookie+csrf）", ld.get("ok") is True and SESSION["csrf"] != "")

# 2) 模板发布 → 报告文档（initial_md 承载全部渲染要素）
tpl = req("POST", "/api/writing/templates", {
    "name": "渲染验收模板",
    "body_md": "# 渲染验收\n\n{{angle}}\n", "variables": [], "agent_notes": "", "check_rules": []})
jreq_pub = req("POST", f"/api/writing/templates/{tpl['id']}/publish", {"changelog": "v1"})
doc = req("POST", "/api/writing/docs", {"title": "渲染验收报告（长中文标题）", "template_id": tpl["id"]})
UID = doc["doc_uid"]
check("L-2 模板发布+报告创建", bool(UID))

# 3) PNG 素材（真实 PNG 字节）
PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
req("POST", f"/api/writing/docs/{UID}/assets", {"name": "evidence.png", "data_b64": PNG_B64})

# 4) 正文重写（apply-replace 无 REST 端点——正文走 WS；此处用 restore 承载完整正文，
#    restore 是真实恢复路径且形成新修订，等价于作者粘贴全文）
FULL_MD = ("# 渲染验收报告（长中文标题）\n\n" + "中文长段落。" * 60 + "\n\n"
           "## 一、时间线\n\n- 2026-11-17 08:00 首次告警\n- 2026-11-17 08:05 确认失陷\n"
           "- 特殊字符 <script>&\"' ｜ 竖线|测试 ｜ emoji 🌍✅\n\n"
           "## 二、IOC 表格\n\n| 类型 | 值 | 备注 |\n|---|---|---|\n"
           "| IP | 203.0.113.41 | 攻击源 |\n| 域名 | evil.example.com | C2 |\n"
           "| 哈希 | d41d8cd98f00b204e9800998ecf8427e | 样本 |\n\n"
           "## 三、证据截图\n\n![证据截图](attachment:evidence.png)\n\n"
           "## 四、参考\n\n[MITRE ATT&CK](https://attack.mitre.org/techniques/T1505/003/)\n")
# 正文写入走真实业务入口 dc.restore_text（与 round2 B20 套件同路径；WS 编辑链路由
# writing_collab 套件与浏览器验收覆盖）。REST /restore 的语义是"恢复到指定历史修订"。
import app.doccollab as dc  # noqa: E402

rr = dc.restore_text(UID, FULL_MD, kind="restore", operator="boss")
check("L-3 正文重写（restore_text 真实入口）", rr.get("rev", 0) >= 1 and "IOC 表格" in rr.get("text", ""), rr.get("rev"))

# 5) 提交审阅（快照契约）→ 批准 → 导出
sr = req("POST", f"/api/writing/docs/{UID}/submit-review", {})
rev = sr["review_rev"]
req("POST", f"/api/writing/docs/{UID}/review",
    {"decision": "approved", "note": "渲染验收", "review_rev": rev})
ex = req("POST", f"/api/writing/docs/{UID}/export/word", {})
eid = ex["export_id"]
file_path = None
for _ in range(120):
    es = req("GET", f"/api/writing/docs/{UID}/exports")
    e = next((x for x in es.get("items", []) if x["id"] == eid), None)
    if not e:
        time.sleep(0.5); continue
    if e["status"] == "done":
        # file_path 不对外暴露（安全设计）——走官方 download 路由取产物
        dl = op.open(urllib.request.Request(
            BASE + f"/api/writing/docs/{UID}/export/word/{eid}/download",
            headers={"Cookie": SESSION["cookie"], "X-CSRF-Token": SESSION["csrf"]}), timeout=60)
        file_path = "<downloaded>"
        break
    if e["status"] == "failed":
        check("L-4 导出完成", False, e.get("error", ""))
        break
    time.sleep(0.5)

art = ROOT / "logs" / "round4-artifacts"
art.mkdir(parents=True, exist_ok=True)

check("L-4 导出任务完成", file_path is not None, "未完成")
if not file_path:
    (art / "render_evidence.json").write_text(json.dumps(
        {"pass": PASS, "fail": FAIL, "blocked": BLOCKED}, ensure_ascii=False, indent=1), encoding="utf-8")
    server.should_exit = True
    sys.exit(1)

docx = art / "render_check.docx"
docx.write_bytes(dl.read())
check("L-5 DOCX 实物落盘", docx.exists() and docx.stat().st_size > 10000, docx.stat().st_size)

# 6) 验证 1：OOXML 部件
import zipfile  # noqa: E402

z = zipfile.ZipFile(docx)
names = z.namelist()
check("V-1 OOXML 部件完整", "word/document.xml" in names and "word/styles.xml" in names
      and "[Content_Types].xml" in names)
xml = z.read("word/document.xml").decode("utf-8")
media = [n for n in names if n.startswith("word/media/")]
check("V-2 图片嵌入 media 部件", len(media) >= 1, media)
check("V-3 标题文本入文", "渲染验收报告（长中文标题）" in xml)
check("V-4 特殊字符转义存在", "&amp;" in xml or "&lt;" in xml)

# 7) 验证 2：python-docx 独立解析
try:
    import docx as pydocx  # noqa: E402

    dd = pydocx.Document(str(docx))
    heads = [p.text for p in dd.paragraphs if p.style.name.startswith("Heading")]
    tables = dd.tables
    check("V-5 python-docx 解析标题", any("渲染验收报告" in h for h in heads), heads[:3])
    check("V-6 python-docx 表格", len(tables) >= 1 and len(tables[0].rows) == 4,
          len(tables) if not tables else len(tables[0].rows))
    check("V-7 python-docx 图片存在", len(dd.inline_shapes) >= 1, len(dd.inline_shapes))
    check("V-8 长中文段落完整", any("中文长段落。" * 5 in p.text for p in dd.paragraphs))
except ImportError:
    BLOCKED.append("python-docx 未安装，V5-V8 受阻")
    print("BLOCKED V5-V8（python-docx 未安装）")

# 8) 验证 3：pandoc 独立渲染 DOCX→HTML
import subprocess  # noqa: E402

from app.wordexport import pandoc_path  # noqa: E402

pp = pandoc_path()
if pp:
    html_out = art / "render_check.html"
    r = subprocess.run([pp, str(docx), "-f", "docx", "-t", "html", "-o", str(html_out)],
                       capture_output=True, text=True, timeout=90)
    check("V-9 pandoc DOCX→HTML 渲染成功", r.returncode == 0, r.stderr[:150])
    if r.returncode == 0:
        h = html_out.read_text(encoding="utf-8")
        check("V-10 渲染含标题", "渲染验收报告（长中文标题）" in h)
        check("V-11 渲染含表格", "<table" in h and "203.0.113.41" in h)
        check("V-12 渲染含图片", "<img" in h)
else:
    BLOCKED.append("V9-V12 受阻：pandoc 不可用")

# 9) Word COM 打开验证（尝试一次；模态/权限阻塞则如实记录）
com_status = "未尝试"
try:
    import win32com.client  # noqa: E402
    import pythoncom  # noqa: E402

    pythoncom.CoInitialize()
    word = win32com.client.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0
    try:
        doc_obj = word.Documents.Open(str(docx.resolve()), ReadOnly=True, AddToRecentFiles=False)
        pages = doc_obj.ComputeStatistics(2)  # wdStatisticPages
        words = doc_obj.ComputeStatistics(0)  # wdStatisticWords
        com_status = f"打开成功：{pages} 页 / {words} 词"
        check("V-13 Word COM 实际打开渲染", True, com_status)
        doc_obj.Close(False)
    finally:
        word.Quit()
except Exception as e:
    com_status = f"受阻：{str(e)[:120]}"
    BLOCKED.append("V-13 Word COM：" + com_status)
    print("BLOCKED V-13 Word COM：" + com_status)

# 10) 证据落盘
ev = {"docx": str(docx), "size": docx.stat().st_size, "review_rev": rev,
      "com": com_status, "pass": PASS, "fail": FAIL, "blocked": BLOCKED}
(art / "render_evidence.json").write_text(json.dumps(ev, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"\n==== 渲染级验收：{len(PASS)} PASS / {len(FAIL)} FAIL / {len(BLOCKED)} BLOCKED ====")
print(f"产物：{docx}")
server.should_exit = True
sys.exit(1 if FAIL else 0)
