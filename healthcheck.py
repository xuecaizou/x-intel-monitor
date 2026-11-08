# -*- coding: utf-8 -*-
"""X-Intel 全端点回归检查：36 个 API + 6 个页面路由，一次性体检"""
import io
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')
BASE = 'http://127.0.0.1:8083'
PASS, FAIL = 0, []


import os as _os


def _req(path):
    from urllib.parse import quote
    safe = quote(path, safe='/?&=')
    req = urllib.request.Request(BASE + safe)
    tok = _os.environ.get("XINTEL_TOKEN", "")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    return req


def get(path):
    """单端点故障（超时/拒绝/非 JSON）返回 0 状态标记 FAIL，不炸整个套件。
    认证（第四批）：需设置环境变量 XINTEL_TOKEN=<API Token>（admin 角色）。"""
    try:
        with urllib.request.urlopen(_req(path), timeout=30) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return 0, {"_error": f"{type(e).__name__}: {str(e)[:90]}"}


def page(path, kw):
    try:
        with urllib.request.urlopen(_req(path), timeout=15) as r:
            body = r.read().decode('utf-8')
    except Exception as e:
        return f'请求失败:{type(e).__name__}'
    return ('OK' if kw in body else f'缺关键字:{kw}')


def check(name, cond, detail=''):
    global PASS
    if cond:
        PASS += 1
        print(f'PASS {name}')
    else:
        FAIL.append(name)
        print(f'FAIL {name} {detail}')


# ---- 推文与管理 ----
s, d = get('/api/tweets?limit=3')
check('GET /api/tweets', s == 200 and 'items' in d and len(d['items']) > 0)
t0 = d['items'][0] if d.get('items') else {}
check('推文字段齐全', all(k in t0 for k in ('text', 'ai_severity', 'geo_country', 'category', 'media_url')))
s, d = get('/api/tweets?category=数据泄露线索&limit=3')
check('分类筛选', s == 200 and d.get('total', 0) > 0, f"total={d.get('total')}")
s, d = get('/api/tweets?status=starred&limit=3')
check('状态筛选', s == 200)
s, f = get('/api/facets')
check('GET /api/facets', s == 200 and 'countries' in f)
check('facets 含关键词标签统计', s == 200 and isinstance(f.get('tags'), list))
_cat = {x['name']: x['count'] for x in f.get('categories', [])}.get('数据泄露线索')
_d2 = get('/api/tweets?category=数据泄露线索&limit=1')[1]
check('分类联动计数=实际条数', _cat == _d2.get('total'), f"facets={_cat} api={_d2.get('total')}")
s, d = get('/api/categories')
check('GET /api/categories', s == 200)
s, d = get('/api/stats')
check('GET /api/stats', s == 200 and 'today' in d and 'ai_auto_on' in d)

# ---- 账号 ----
s, d = get('/api/x/accounts')
check('GET /api/x/accounts', s == 200 and isinstance(d.get('items'), list) and len(d['items']) > 0)
s, d = get('/api/x/settings')
check('GET /api/x/settings', s == 200)

# ---- AI ----
s, d = get('/api/ai/config')
check('GET /api/ai/config', s == 200 and 'values' in d)
s, d = get('/api/ai/models')
check('GET /api/ai/models', s == 200 and d.get('ok') and len(d.get('models', [])) > 5,
      f"models={len(d.get('models', []))}")
s, d = get('/api/ai/logs?limit=10')
check('GET /api/ai/logs', s == 200 and 'items' in d and 'stats' in d)

# ---- 通道/过滤/日志 ----
s, d = get('/api/channels')
check('GET /api/channels', s == 200)
s, d = get('/api/push/filters')
check('GET /api/push/filters', s == 200)
check('推送过滤六维字段齐全', s == 200 and all(k in d.get('values', {}) for k in
      ('min_severity', 'categories', 'countries', 'industries', 'tags', 'keywords')),
      str(list(d.get('values', {}).keys())))
s, d = get('/api/push/logs?limit=3')
check('GET /api/push/logs', s == 200)
s, d = get('/api/fetch/logs?limit=3')
check('GET /api/fetch/logs', s == 200)

# ---- RSS 源 ----
s, d = get('/api/rss/sources')
check('GET /api/rss/sources', s == 200 and 'items' in d)

# ---- 通道多实例 ----
s, d = get('/api/channels')
check('GET /api/channels(实例化)', s == 200 and all('instances' in c for c in d.get('items', [])))
wecom = next((c for c in d.get('items', []) if c['key'] == 'wecom'), None)
check('企微旧配置自动迁移', wecom is not None and len(wecom.get('instances', [])) >= 1,
      f'instances={len((wecom or {}).get("instances", []))}')

# ---- 报告任务 ----
s, d = get('/api/reports/tasks?limit=5')
check('GET /api/reports/tasks', s == 200 and 'tasks' in d and 'total' in d)
if d.get('tasks'):
    tid = d['tasks'][0]['id']
    s2, d2 = get(f'/api/reports/tasks/{tid}/package')
    check('GET …/{id}/package', s2 == 200 and 'disclaimer' in d2 and 'tweets' in d2)
    check('线索包字段', all(k in d2 for k in ('task_id', 'title', 'tier', 'keywords')))

# ---- Swagger ----
s, d = get('/openapi.json')
paths = list(d.get('paths', {}).keys())
check('OpenAPI 端点数 ≥ 30', len(paths) >= 30, f'实际 {len(paths)}')
check('报告端点进 Swagger', any('/api/reports/tasks' in p for p in paths))
check('模型端点进 Swagger', '/api/ai/models' in paths)

# ---- 页面 ----
check('页面 /', page('/', 'X-Intel') == 'OK')
check('页面 / 含报告任务面板', page('/', 'task-panel') == 'OK')
check('页面 / 含日志审计二级菜单', page('/', 'sb-subitem') == 'OK')
check('页面 / 含 AI 日志容器', page('/', 'ailogs') == 'OK')
check('页面 / 含拉取模型', page('/', 'ai-fetch-models') == 'OK')
check('页面 / 含生成报告任务', page('/', 'selreport') == 'OK')
check('页面 / 含标签筛选下拉', page('/', 'tagsel') == 'OK')
check('页面 / 含关键词标签卡', page('/', 'hc-tags') == 'OK')
check('页面 /help', page('/help', '报告任务与 report-writer') == 'OK')
check('help 含 models 端点', page('/help', '/api/ai/models') == 'OK')
check('页面 /arch', page('/arch', 'report_tasks') == 'OK')
check('页面 /readme', page('/readme', '报告任务') == 'OK')

print(f'\n==== 回归结果：{PASS} PASS / {len(FAIL)} FAIL ====')
if FAIL:
    print('失败项:', FAIL)
    sys.exit(1)
