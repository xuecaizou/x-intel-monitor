# -*- coding: utf-8 -*-
"""外部 Agent 协作示例（可运行）：读取文档 → 提交建议 / 受控修改。

前置（管理员在 UI 或 API 完成）：
  1. 创建 Agent Token：POST /api/agent/tokens {"name":"demo-agent","docs":"all"}
     （或 docs="doc_xxx,doc_yyy" 限定范围；明文仅返回一次）
  2. Agent 携带 `Authorization: Bearer agent_xxx` 调用下列接口。

用法：
  python scripts/agent_demo.py --base http://127.0.0.1:8083 --token agent_xxx --doc doc_xxxxxxxx [--edit]

行为：
  - 默认：读取文档 → 在文末追加一节【建议】（锚点=最后一个非空行，唯一匹配才可提交）。
  - --edit：走受控直接修改（base_rev 乐观锁 + client_id 幂等；冲突时明确报 409）。
"""
import argparse
import hashlib
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")


def http(base, path, token, method="GET", body=None):
    req = urllib.request.Request(base.rstrip("/") + path, method=method)
    req.add_header("Authorization", "Bearer " + token)
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8083")
    ap.add_argument("--token", required=True)
    ap.add_argument("--doc", required=True)
    ap.add_argument("--edit", action="store_true", help="受控直接修改（默认提交建议）")
    args = ap.parse_args()

    s, doc = http(args.base, f"/api/agent/docs/{args.doc}", args.token)
    if s != 200:
        print("读取失败", s, doc)
        sys.exit(1)
    text = doc["body_md"]
    rev = doc["rev"]
    print(f"文档：{doc['title']}  状态：{doc['status']}  修订：v{rev}")
    print("Agent 写作要求：", (doc.get("agent_notes") or "（无）")[:200])
    print("正文长度：", len(text), "字符")

    lines = [x for x in text.splitlines() if x.strip()]
    anchor = lines[-1] if lines else text.strip()
    addition = f"\n\n## 协作补充（demo-agent）\n\n本节由外部 Agent 于本轮协作写入；依据见建议来源。\n"
    client_id = "demo-" + hashlib.sha1((args.doc + text[:200]).encode()).hexdigest()[:12]

    if args.edit:
        s, r = http(args.base, f"/api/agent/docs/{args.doc}/apply-edit", args.token, "POST", {
            "base_rev": rev, "old_text": anchor, "new_text": anchor + addition,
            "client_id": client_id, "rationale": "示例脚本受控追加", "target": "文末"})
        print("受控修改：", s, json.dumps(r, ensure_ascii=False)[:200])
        if s == 200 and r.get("existed"):
            print("（幂等命中：同一 client_id 未重复插入）")
        sys.exit(0 if s == 200 else 1)

    s, r = http(args.base, f"/api/agent/docs/{args.doc}/suggestions", args.token, "POST", {
        "old_text": anchor, "new_text": anchor + addition,
        "rationale": "示例脚本建议：追加协作补充节",
        "sources": ["scripts/agent_demo.py 内置示例"],
        "client_id": client_id, "base_rev": rev})
    print("提交建议：", s, json.dumps(r, ensure_ascii=False)[:200])
    sys.exit(0 if s in (200, 409) and (s != 200 or not r.get("existed") or True) else 1)


if __name__ == "__main__":
    main()
