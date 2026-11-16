# -*- coding: utf-8 -*-
"""Round3 第三批补丁：R19/R20/R21/R22/R23/R24。运行后自删。"""
import ast
import sys

sys.stdout.reconfigure(encoding="utf-8")

# ================= app/writing.py =================
g = open("app/writing.py", encoding="utf-8").read()

# R20: scope_type 支持 all（全部近期情报，按 filters.days 界定）
old = '''    if scope_type not in ("manual", "tweet", "event", "backlog"):
        raise ValueError("scope_type 非法")'''
new = '''    if scope_type not in ("manual", "tweet", "event", "backlog", "all"):
        raise ValueError("scope_type 非法")'''
assert old in g, "scope anchor"
g = g.replace(old, new, 1)

old2 = '''    if scope_type == "backlog":
        rows = db.get_conn().execute(
            "SELECT * FROM tweets WHERE pushed=0 AND ignored=0"
            " AND COALESCE(ai_severity,'') IN ('high','critical')"
            " ORDER BY id DESC LIMIT 15").fetchall()
        return [reports._tweet_package(r) for r in rows]
    return []'''
new2 = '''    if scope_type == "backlog":
        rows = db.get_conn().execute(
            "SELECT * FROM tweets WHERE pushed=0 AND ignored=0"
            " AND COALESCE(ai_severity,'') IN ('high','critical')"
            " ORDER BY id DESC LIMIT 15").fetchall()
        return [reports._tweet_package(r) for r in rows]
    if scope_type == "all":
        # R20：全部近期情报（页面"all（全部近期情报）"范围）——近 7 天、
        # 未忽略、有 AI 研判结果者优先，最多 30 条
        rows = db.get_conn().execute(
            "SELECT * FROM tweets WHERE ignored=0"
            " AND created_at >= datetime('now','localtime','-7 days')"
            " ORDER BY (CASE WHEN COALESCE(ai_status,'')='ok' THEN 0 ELSE 1 END), id DESC"
            " LIMIT 30").fetchall()
        return [reports._tweet_package(r) for r in rows]
    return []'''
assert old2 in g, "scope_tweets anchor"
g = g.replace(old2, new2, 1)

# R21: 推荐采纳去重改为内容身份（跨任务同题命中；不采用保留不复活）
old3 = '''        ai_run=row["updated_at"] or db.now(), rec_task_id=int(task_id),
        dedup_scope=f"rec:{task_id}:{index}")'''
new3 = '''        ai_run=row["updated_at"] or db.now(), rec_task_id=int(task_id),
        # R21：去重身份回归**内容本身**（标题+角度）——不同任务同题命中同一候选
        # （合并/抑制"不采用"复活）；单次结果幂等由 it["adopted"] 标记保障
        dedup_scope="")'''
assert old3 in g, "dedup anchor"
g = g.replace(old3, new3, 1)

# R22: 零合法来源的采纳 → 候选强制标注待补来源（不冒充有依据）
old4 = '''    it = detail[int(index)]
    if it.get("adopted") and it.get("cand_uid"):
        return {"cand_uid": it["cand_uid"], "adopted": True, "existed": True}
    uid, merged = create_candidate(
        title=it.get("title", ""), angle=(it.get("angle") or "")[:200],
        tweet_ids=it.get("tweet_ids") or [], source_type="ai",
        recommend_reason=(it.get("reason") or "")[:500],
        ai_suggestion=_j({"suggestion": {"angle": it.get("angle") or "",
                                         "reason": it.get("reason") or "",
                                         "materials": it.get("materials") or [],
                                         "basis": it.get("basis") or [],
                                         "source_links": it.get("source_links") or []},
                          "priority": it.get("priority"),
                          "note": f"推荐任务 #{task_id} 采纳"}),
        suggested_priority=it.get("priority"),
        materials=it.get("materials") or [], operator=operator,
        ai_run=row["updated_at"] or db.now(), rec_task_id=int(task_id),
        # R21：去重身份回归**内容本身**（标题+角度）——不同任务同题命中同一候选
        # （合并/抑制"不采用"复活）；单次结果幂等由 it["adopted"] 标记保障
        dedup_scope="")'''
new4 = '''    it = detail[int(index)]
    if it.get("adopted") and it.get("cand_uid"):
        return {"cand_uid": it["cand_uid"], "adopted": True, "existed": True}
    # R22：模型引用越界被剔除后零合法来源 → 不冒充有依据：候选强制标注
    # "来源待人工补充"（materials 首条 + 推荐理由前缀），rejected_refs 保留进建议 JSON
    zero_source = not (it.get("tweet_ids") or [])
    materials = list(it.get("materials") or [])
    reason = (it.get("reason") or "")[:500]
    if zero_source:
        materials = ["⚠ 模型引用的情报ID全部越界/无效，来源待人工补充"] + materials
        reason = "【来源待确认——模型引用无有效情报】" + reason
    rej = it.get("rejected_refs") or []
    uid, merged = create_candidate(
        title=it.get("title", ""), angle=(it.get("angle") or "")[:200],
        tweet_ids=it.get("tweet_ids") or [], source_type="ai",
        recommend_reason=reason,
        ai_suggestion=_j({"suggestion": {"angle": it.get("angle") or "",
                                         "reason": it.get("reason") or "",
                                         "materials": it.get("materials") or [],
                                         "basis": it.get("basis") or [],
                                         "source_links": it.get("source_links") or [],
                                         "rejected_refs": rej[:10],
                                         "zero_source": zero_source},
                          "priority": it.get("priority"),
                          "note": f"推荐任务 #{task_id} 采纳"}),
        suggested_priority=it.get("priority"),
        materials=materials, operator=operator,
        ai_run=row["updated_at"] or db.now(), rec_task_id=int(task_id),
        # R21：去重身份回归**内容本身**（标题+角度）——不同任务同题命中同一候选
        # （合并/抑制"不采用"复活）；单次结果幂等由 it["adopted"] 标记保障
        dedup_scope="")'''
assert old4 in g, "adopt anchor"
g = g.replace(old4, new4, 1)

# R23: worker 每轮空转前回收超租约 running 任务（快速重启场景）
old5 = '''def _recommend_worker():
    while True:
        task = None'''
new5 = '''def _recommend_worker():
    while True:
        try:
            recover_recommend_tasks(10)   # R23：worker 每轮回收超租约任务（快速重启场景）
        except Exception:
            pass
        task = None'''
assert old5 in g, "worker anchor"
g = g.replace(old5, new5, 1)

open("app/writing.py", "w", encoding="utf-8").write(g)
ast.parse(g)
print("writing batch3 done")

# ================= api/writing_routes.py：R24 单任务查询端点 =================
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

# ================= web/index.html：R19/R24/R18 前端 =================
h = open("web/index.html", encoding="utf-8").read()

# R19a: 恢复待处理 → PUT 状态更新（不再发空 decision）
old7 = """  var map={adopt:['采纳','采纳','待处理'],hold:['暂缓','暂缓','暂缓'],reject:['不采用','不采用','不采用'],restore:['采纳','','待处理']};
  var m=map[act];
  apiFetch('/api/writing/candidates/'+uid+'/decision',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({decision:m[1],note:''})}).then(function(r){return r.json();})
    .then(function(d){snack(d.ok?'已'+m[0]:('操作失败：'+(d.detail||'')));loadCands();});"""
new7 = """  if(act==='restore'){
    /* R19：恢复待处理走状态更新（不发空 decision——后端 400） */
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

# R19b: 版本历史弹窗关闭按钮防 null（先判存在再绑定）
old8 = """      $('vm-close').addEventListener('click',closeModal);"""
new8 = """      var vmb=$('vm-close');if(vmb)vmb.addEventListener('click',closeModal);"""
assert old8 in h, "vm-close anchor"
h = h.replace(old8, new8, 1)

# R18: 模板元数据保存 → {fields:{...}} 外层 + variables 转 {name,label,required} 对象数组
old9 = """      apiFetch('/api/writing/templates/'+tid,{method:'PUT',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({description:v['mt-desc'],
          variables:(v['mt-vars']||'').split(',').map(function(x){return x.trim();}).filter(Boolean),
          agent_notes:v['mt-notes']||'',check_rules:rules||[]})})"""
new9 = """      apiFetch('/api/writing/templates/'+tid,{method:'PUT',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({fields:{
          description:v['mt-desc']||'',
          variables:(v['mt-vars']||'').split(',').map(function(x){return x.trim();}).filter(Boolean)
            .map(function(x){return {name:x,label:x,required:false};}),
          agent_notes:v['mt-notes']||'',check_rules:rules||[]}})})"""
assert old9 in h, "tplmeta anchor"
h = h.replace(old9, new9, 1)

# R24: pollRecommend 按 id 轮询（不再依赖最新 5 条）
old10 = """    jget('/api/writing/recommend/tasks?limit=5').then(function(d){
      var t=(d.items||[]).filter(function(x){return x.id===tid;})[0];
      if(!t)return;"""
new10 = """    jget('/api/writing/recommend-tasks/'+tid).then(function(d){
      var t=d;"""
assert old10 in h, "poll anchor"
h = h.replace(old10, new10, 1)

# R24: 候选面板头部加"推荐任务历史"入口 + 历史弹窗（刷新后可恢复查看）
old11 = """          <button class="btn btn-xs" id="cand-ai" type="button" title="用当前 AI 配置从积压高严重度情报推荐选题">AI 推荐</button>"""
new11 = """          <button class="btn btn-xs" id="cand-ai" type="button" title="用当前 AI 配置从积压高严重度情报推荐选题">AI 推荐</button>
          <button class="btn btn-xs" id="cand-rechist" type="button" title="历史推荐任务与结果（刷新后可恢复查看）">推荐历史</button>"""
assert old11 in h, "hist btn anchor"
h = h.replace(old11, new11, 1)

old12 = """$('cand-ai').addEventListener('click',function(){"""
new12 = """$('cand-rechist').addEventListener('click',function(){
  apiFetch('/api/writing/recommend-tasks?limit=50').then(function(r){return r.json();}).then(function(d){
    var items=d.items||[];
    var h='<div style="font-size:15px;font-weight:700;margin-bottom:10px">AI 推荐任务历史</div>'
      +(!(items.length)?'<div class="empty">暂无任务</div>':
        items.map(function(x){
          var n=0; try{ n=(JSON.parse(x.result_json||'[]')||[]).length; }catch(e){}
          return '<div class="rtitem"><div class="rttop"><span class="rttitle">#'+x.id+' '+esc(x.scope_type||'')+'</span>'
            +'<span class="hchip '+(x.status==='done'?'appr':x.status==='failed'?'':'pend')+'">'+esc(x.status)+'</span></div>'
            +'<div class="rtmeta">候选 '+n+' 项 · '+esc(x.updated_at||'')+(x.error?' · <span style="color:#be123c">'+esc(x.error.slice(0,50))+'</span>':'')+'</div>'
            +'<div style="margin-top:6px"><button class="btn btn-xs" data-recview="'+x.id+'">查看结果</button>'
            +(x.status==='failed'?' <button class="btn btn-xs" data-recretry="'+x.id+'">重试</button>':'')+'</div></div>';
        }).join(''))
      +'<div style="text-align:right;margin-top:8px"><button class="btn btn-xs" id="rh-close">关闭</button></div>';
    openModal(h);
    var rh=$('rh-close');if(rh)rh.addEventListener('click',closeModal);
    h.querySelectorAll('[data-recview]').forEach(function(b){
      b.addEventListener('click',function(){ pollRecommend(+b.getAttribute('data-recview')); });
    });
    h.querySelectorAll('[data-recretry]').forEach(function(b){
      b.addEventListener('click',function(){
        apiFetch('/api/writing/recommend/'+b.getAttribute('data-recretry')+'/retry',{method:'POST'})
          .then(function(){snack('已重排队');closeModal();});
      });
    });
  }).catch(function(){});
});
$('cand-ai').addEventListener('click',function(){"""
assert old12 in h, "hist handler anchor"
h = h.replace(old12, new12, 1)

open("web/index.html", "w", encoding="utf-8").write(h)
print("frontend batch3 done")
