# -*- coding: utf-8 -*-
"""Round3 第三批补丁 b：按实际代码锚点（R19/R20/R21/R22/R23/R24/R18）。运行后自删。"""
import ast
import sys

sys.stdout.reconfigure(encoding="utf-8")

# ================= app/writing.py =================
g = open("app/writing.py", encoding="utf-8").read()

old = '''    if scope_type not in ("manual", "tweet", "event", "backlog"):
        raise ValueError("scope_type 非法")'''
new = '''    if scope_type not in ("manual", "tweet", "event", "backlog", "all"):
        raise ValueError("scope_type 非法")   # R20：页面"all（全部近期情报）"范围放行'''
assert old in g, "scope anchor"
g = g.replace(old, new, 1)

old3 = '''        ai_run=row["updated_at"] or db.now(), rec_task_id=int(task_id),
        dedup_scope=f"rec:{task_id}:{index}")'''
new3 = '''        ai_run=row["updated_at"] or db.now(), rec_task_id=int(task_id),
        # R21：去重身份回归内容本身（标题+角度）——跨任务同题命中同一候选；
        # "不采用"的旧候选抑制新推荐复活；单次结果幂等由 it["adopted"] 标记保障
        dedup_scope="")'''
assert old3 in g, "dedup anchor"
g = g.replace(old3, new3, 1)

# R22 + R21 注释：adopt 主体（按当前文件实际内容替换）
old4 = '''    uid, merged = create_candidate(
        title=it.get("title", ""), angle=(it.get("angle") or "")[:200],
        tweet_ids=it.get("tweet_ids") or [], source_type="ai",
        recommend_reason=(it.get("reason") or "")[:500],
        ai_suggestion=_j({"suggestion": {"angle": it.get("angle") or "",
                                         "reason": it.get("reason") or "",
                                         "materials": it.get("materials") or [],
                                         "basis": it.get("basis") or [],
                                         "source_links": it.get("source_links") or []},
                          "priority": it.get("priority"),
                          "note": f"推荐任务 #{task_id} 采纳"}),'''
new4 = '''    # R22：模型引用越界被剔除后零合法来源 → 不冒充有依据（materials 首条强制提示，
    # 推荐理由前缀标注，rejected_refs 进建议 JSON）；采纳仍允许（选题可取、来源人工补）
    zero_source = not (it.get("tweet_ids") or [])
    materials = list(it.get("materials") or [])
    reason = (it.get("reason") or "")[:500]
    if zero_source:
        materials = ["⚠ 模型引用的情报ID全部越界/无效，来源待人工补充"] + materials
        reason = "【来源待确认——模型引用无有效情报】" + reason
    uid, merged = create_candidate(
        title=it.get("title", ""), angle=(it.get("angle") or "")[:200],
        tweet_ids=it.get("tweet_ids") or [], source_type="ai",
        recommend_reason=reason,
        ai_suggestion=_j({"suggestion": {"angle": it.get("angle") or "",
                                         "reason": it.get("reason") or "",
                                         "materials": it.get("materials") or [],
                                         "basis": it.get("basis") or [],
                                         "source_links": it.get("source_links") or [],
                                         "rejected_refs": (it.get("rejected_refs") or [])[:10],
                                         "zero_source": zero_source},
                          "priority": it.get("priority"),
                          "note": f"推荐任务 #{task_id} 采纳"}),'''
assert old4 in g, "adopt anchor"
g = g.replace(old4, new4, 1)

old5 = '''def _recommend_worker():
    while True:
        task = None'''
new5 = '''def _recommend_worker():
    while True:
        try:
            recover_recommend_tasks(10)   # R23：每轮回收超租约 running 任务（快速重启场景）
        except Exception:
            pass
        task = None'''
assert old5 in g, "worker anchor"
g = g.replace(old5, new5, 1)

open("app/writing.py", "w", encoding="utf-8").write(g)
ast.parse(g)
print("writing batch3 done")

# ================= api/writing_routes.py：R24 单任务查询 =================
w = open("api/writing_routes.py", encoding="utf-8").read()
old6 = '''@router.get("/api/writing/recommend-tasks", dependencies=[Depends(auth.require("read"))])
def api_recommend_list(limit: int = 20):
    return {"items": writing.list_recommend_tasks(min(limit, 100))}'''
new6 = '''@router.get("/api/writing/recommend-tasks", dependencies=[Depends(auth.require("read"))])
def api_recommend_list(limit: int = 20):
    return {"items": writing.list_recommend_tasks(min(limit, 100))}


@router.get("/api/writing/recommend-tasks/{task_id}", dependencies=[Depends(auth.require("read"))])
def api_recommend_get(task_id: int):
    """R24：按 id 查任务（轮询与历史入口不依赖最新 5 条列表）。"""
    row = db.get_conn().execute("SELECT * FROM ai_recommend_tasks WHERE id=?",
                                (int(task_id),)).fetchone()
    if not row:
        raise HTTPException(404, "任务不存在")
    d = dict(row)
    try:
        d["result_detail"] = json.loads(d.pop("result_json") or "[]")
    except Exception:
        d["result_detail"] = []
    return d'''
assert old6 in w, "recommend list anchor"
w = w.replace(old6, new6, 1)
open("api/writing_routes.py", "w", encoding="utf-8").write(w)
ast.parse(w)
print("routes single-task done")

# ================= web/index.html =================
h = open("web/index.html", encoding="utf-8").read()

# R19a: restore 走状态更新
old7 = """  var map={adopt:['采纳','采纳','待处理'],hold:['暂缓','暂缓','暂缓'],reject:['不采用','不采用','不采用'],restore:['采纳','','待处理']};
  var m=map[act];
  apiFetch('/api/writing/candidates/'+uid+'/decision',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({decision:m[1],note:''})}).then(function(r){return r.json();})
    .then(function(d){snack(d.ok?'已'+m[0]:('操作失败：'+(d.detail||'')));loadCands();});"""
new7 = """  if(act==='restore'){
    /* R19：恢复待处理走状态更新（空 decision 后端 400） */
    apiFetch('/api/writing/candidates/'+uid,{method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({fields:{status:'待处理'}})}).then(function(r){return r.json();})
      .then(function(d){snack(d.ok?'已恢复待处理':('操作失败：'+(d.detail||'')));loadCands();});
    return;
  }
  var map={adopt:['采纳','采纳','待处理'],hold:['暂缓','暂缓','暂缓'],reject:['不采用','不采用','不采用']};
  var m=map[act];
  apiFetch('/api/writing/candidates/'+uid+'/decision',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({decision:m[1],note:''})}).then(function(r){return r.json();})
    .then(function(d){snack(d.ok?'已'+m[0]:('操作失败：'+(d.detail||'')));loadCands();});"""
assert old7 in h, "restore anchor"
h = h.replace(old7, new7, 1)

# R18: 模板元数据保存外层 fields + 变量对象化（检查当前实现形态）
import re as _re2
m18 = _re2.search(r"body:JSON\.stringify\(\{description:[^)]*\)\}", h)
if not m18:
    print("R18: 未找到旧形态（可能已是 fields 外层）")
elif "{fields:" in m18.group(0):
    print("R18: 已是 fields 外层")
else:
import re as _re2
mm = _re2.search(r"body:JSON\.stringify\(\{description:([^\n]*?)\)\}\)", h)
print("R18 实际保存体片段:", (mm.group(0)[:200] if mm else "未找到（可能已是 fields 外层）"))

open("web/index.html", "w", encoding="utf-8").write(h)
print("frontend restore fix applied")
