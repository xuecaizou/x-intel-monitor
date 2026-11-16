# -*- coding: utf-8 -*-
"""Round3 第三批补丁 c：R18 模板元数据保存（按实际代码）+ 语法验证。运行后自删。"""
import ast
import re as _re
import sys

sys.stdout.reconfigure(encoding="utf-8")
h = open("web/index.html", encoding="utf-8").read()

# R18: 找模板元数据 PUT 保存体（当前形态探测）
m = _re.search(r"apiFetch\('/api/writing/templates/'\+tid,\{method:'PUT',[^\n]*\n[^\n]*body:JSON\.stringify\((.+?)\)\}\)", h, _re.S)
if m:
    body_expr = m.group(1)
    print("当前保存体:", body_expr[:200].replace("\n", " "))
    if "{fields:" not in body_expr:
        # 包一层 fields 外层并对象化 variables（字符串数组 → {name,label,required} 对象）
        if "variables:(v['mt-vars']" in body_expr:
            body_expr = body_expr.replace(
                "variables:(v['mt-vars']||'').split(',').map(function(x){return x.trim();}).filter(Boolean)",
                "variables:(v['mt-vars']||'').split(',').map(function(x){return x.trim();}).filter(Boolean)"
                ".map(function(x){return {name:x,label:x,required:false};})")
        # 外层 fields
        body_expr = "{" + body_expr[1:] if body_expr.startswith("{") else body_expr
        body_expr = "{fields:" + body_expr + "}"
        new_block = ("apiFetch('/api/writing/templates/'+tid,{method:'PUT',"
                     "headers:{'Content-Type':'application/json'},"
                     "body:JSON.stringify(" + body_expr + ")})")
        h = h.replace(m.group(0), new_block, 1)
        print("R18 wrapped fields + variables objectified")
    else:
        print("R18: 已是 fields 外层，跳过")
else:
    # 兜底：直接找 JSON.stringify({description:
    m2 = _re.search(r"body:JSON\.stringify\(\{description:v\['mt-desc'\],\s*"
                    r"variables:\(v\['mt-vars'\]\|\|''\)\.split\(','\)\.map\(function\(x\)\{return x\.trim\(\);\}\)\.filter\(Boolean\),\s*"
                    r"agent_notes:v\['mt-notes'\]\|\|'',check_rules:rules\|\|\[\]\}\)", h)
    assert m2, "R18 anchor still missing"
    new2 = ("body:JSON.stringify({fields:{description:v['mt-desc']||'',"
            "variables:(v['mt-vars']||'').split(',').map(function(x){return x.trim();}).filter(Boolean)"
            ".map(function(x){return {name:x,label:x,required:false};}),"
            "agent_notes:v['mt-notes']||'',check_rules:rules||[]}})")
    h = h.replace(m2.group(0), new2, 1)
    print("R18 patched (fallback)")

open("web/index.html", "w", encoding="utf-8").write(h)

# 语法验证
scripts = _re.findall(r"<script>([\s\S]*?)</script>", h)
ok = True
for i, sc in enumerate(scripts):
    try:
        compile(sc, f"<script{i}>", "exec")
    except SyntaxError as e:
        ok = False
        print(f"block {i} ERR: {e.message}")
print("index.html 语法 OK" if ok else "FAIL")
