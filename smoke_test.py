# -*- coding: utf-8 -*-
"""X-Intel 系统冒烟测试：七组链路一次跑完，输出 PASS/FAIL 汇总。
用法：C:/Python314/python.exe smoke_test.py [--nolive]（--nolive 跳过真实抓取/推送等外发动作）
必须用与服务相同解释器（Python 3.14）：G6 单元断言 from app import engine 连带导入 feedparser，py -3.13 会 ModuleNotFoundError
G1 服务与页面 | G2 API 全端点（复用 healthcheck）| G3 数据完整性
G4 采集链路 | G5 AI 链路 | G6 推送链路 | G7 报告任务闭环
"""
import io
import json
import subprocess
import sys
import time
import urllib.request
from urllib.parse import quote

sys.stdout.reconfigure(encoding='utf-8')
BASE = 'http://127.0.0.1:8083'
NOLIVE = '--nolive' in sys.argv
GROUP, PASS, FAIL = '', 0, []


def get(path, timeout=15):
    with urllib.request.urlopen(BASE + quote(path, safe='/?&=_'), timeout=timeout) as r:
        return r.status, json.loads(r.read().decode('utf-8'))


def post(path, payload=None, timeout=60):
    data = json.dumps(payload or {}).encode('utf-8')
    req = urllib.request.Request(BASE + quote(path, safe='/?&=_'), data=data, method='POST',
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def delete(path, timeout=15):
    req = urllib.request.Request(BASE + quote(path, safe='/?&=_'), method='DELETE')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def check(name, cond, detail=''):
    global PASS
    tag = f'[{GROUP}] ' if GROUP else ''
    if cond:
        PASS += 1
        print(f'  PASS {name}')
    else:
        FAIL.append(tag + name)
        print(f'  FAIL {name}  {detail}')


def group(name):
    global GROUP
    GROUP = name
    print(f'\n———— {name} ————')


# ============ G1 服务与页面 ============
group('G1 服务与页面')
import socket
s = socket.socket()
try:
    s.settimeout(3)
    s.connect(('127.0.0.1', 8083))
    check('8083 端口监听', True)
except Exception as e:
    check('8083 端口监听', False, str(e))
finally:
    s.close()

for path, kw in [('/', 'X-Intel'), ('/help', '报告任务与 report-writer'), ('/arch', 'report_tasks'),
                 ('/readme.md', '监控源'), ('/docs', 'swagger')]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            body = r.read().decode('utf-8')
            check(f'页面 {path}', kw.lower() in body.lower(), f'缺关键字 {kw}')
    except Exception as e:
        check(f'页面 {path}', False, str(e)[:80])

try:
    with urllib.request.urlopen(BASE + '/', timeout=10) as r:
        check('no-cache 头', 'no-cache' in (r.headers.get('Cache-Control') or '').lower())
except Exception as e:
    check('no-cache 头', False, str(e)[:80])

# ============ G2 API 全端点 ============
group('G2 API 全端点（healthcheck 35 项）')
r = subprocess.run([sys.executable, 'healthcheck.py'], capture_output=True, text=True,
                   encoding='utf-8', errors='ignore', timeout=180)
tail = (r.stdout or '').strip().splitlines()[-1] if r.stdout else ''
check('healthcheck 全通过', r.returncode == 0, tail)
print('  ' + tail)

# ============ G3 数据完整性 ============
group('G3 数据完整性')
sys.path.insert(0, '.')
from app import db  # noqa: E402

total = db.get_conn().execute('SELECT COUNT(*) AS n FROM tweets').fetchone()['n']
check('推文总量 > 0', total > 0, f'{total}')
dup = db.get_conn().execute(
    'SELECT COUNT(*) AS n FROM (SELECT screen_name, tweet_id FROM tweets'
    ' GROUP BY screen_name, tweet_id HAVING COUNT(*) > 1)').fetchone()['n']
check('UNIQUE 无重复入库', dup == 0, f'{dup} 组重复')
# 统一排序键回归（Bug 2026-11-06）：不能再用 CAST(tweet_id AS INTEGER)，RSS 的 md5 id 会恒为 0
bad_ts = db.get_conn().execute(
    "SELECT COUNT(*) AS n FROM tweets WHERE published_at != '' AND published_at IS NOT NULL"
    " AND NOT (LENGTH(published_at)=16 AND substr(published_at,5,1)='-'"
    " AND substr(published_at,11,1)=' ' AND substr(published_at,14,1)=':')").fetchone()['n']
check('published_at 格式统一（可字符串排序）', bad_ts == 0, f'{bad_ts} 条非 YYYY-MM-DD HH:MM')
_p1 = get('/api/tweets?limit=30&offset=0')[1]
keys = [(t['published_at'] or '', t['id']) for t in _p1['items']]
check('首屏按时间降序', all(keys[i] >= keys[i + 1] for i in range(len(keys) - 1)),
      '存在逆序')
# RSS 可见性用确定性不变量：独立筛选非空且时间降序（首屏占比取决于各源相对到达时间，不是排序不变量）
_rss = get('/api/tweets?screen_name=RSS:TheHackersNews&limit=30')[1]
_rt = [t['published_at'] for t in _rss['items']]
check('RSS 源独立筛选可见且降序', _rss['total'] > 0 and all(_rt[i] >= _rt[i+1] for i in range(len(_rt)-1)),
      f"total={_rss['total']}")
# 跨页连续性：翻 4 页不得重复、不得逆序
_ids, _keys = [], []
for _off in range(0, 120, 30):
    for _t in get(f'/api/tweets?limit=30&offset={_off}')[1]['items']:
        _ids.append(_t['id'])
        _keys.append((_t['published_at'] or '', _t['id']))
check('分页无重复', len(_ids) == len(set(_ids)), f'{len(_ids) - len(set(_ids))} 条重复')
check('分页时序连续（无逆序）',
      all(_keys[i] >= _keys[i + 1] for i in range(len(_keys) - 1)),
      f"{sum(1 for i in range(len(_keys)-1) if _keys[i] < _keys[i+1])} 处逆序")
ai_err = db.get_conn().execute("SELECT COUNT(*) AS n FROM tweets WHERE ai_status='error'").fetchone()['n']
check('AI 研判零积压', ai_err == 0, f'{ai_err} 条 error')
rss_n = db.get_conn().execute("SELECT COUNT(*) AS n FROM rss_sources").fetchone()['n']
check('RSS 源表可读', rss_n >= 1, f'{rss_n} 源')
rt_n = db.get_conn().execute('SELECT COUNT(*) AS n FROM report_tasks').fetchone()['n']
check('报告任务表可读', rt_n >= 0, f'{rt_n} 任务')
al_n = db.get_conn().execute('SELECT COUNT(*) AS n FROM ai_logs').fetchone()['n']
check('AI 日志表可读', al_n >= 0, f'{al_n} 条')

# ============ G4 采集链路 ============
group('G4 采集链路')
if NOLIVE:
    print('  （--nolive 跳过真实抓取）')
else:
    before = db.get_conn().execute('SELECT COUNT(*) AS n FROM tweets').fetchone()['n']
    r = post('/api/fetch/run', timeout=20)
    check('触发抓取任务', r.get('ok') is True, str(r)[:80])
    # 等待最近一轮落库（最多 150s）
    ok, waited = False, 0
    at0 = (db.get_conn().execute('SELECT at FROM fetch_logs ORDER BY id DESC LIMIT 1').fetchone() or {'at': ''})['at']
    while waited < 150:
        time.sleep(10)
        waited += 10
        row = db.get_conn().execute('SELECT at, accounts_ok, new, ai, error FROM fetch_logs ORDER BY id DESC LIMIT 1').fetchone()
        if row and row['at'] != at0:
            ok = True
            break
    check('新一轮抓取完成（≤150s）', ok, f'等待 {waited}s')
    if ok:
        d = dict(row)
        check('抓取无错误', not d['error'], str(d['error'])[:60])
        check('AI 阶段执行', d['ai'] >= 0)
        after = db.get_conn().execute('SELECT COUNT(*) AS n FROM tweets').fetchone()['n']
        check('增量正常（无重复爆量）', after - before == d['new'], f'+{after - before} vs 记录 {d["new"]}')
        print(f'  （本轮：到期账号 {d["accounts_ok"]} · 新增 {d["new"]} · AI {d["ai"]}）')

# ============ G5 AI 链路 ============
group('G5 AI 链路')
r = post('/api/ai/test', timeout=180)
check('AI 连通测试', r.get('ok') is True, str(r.get('error', ''))[:80])
r = get('/api/ai/models', timeout=60)
check('模型列表拉取', r[1].get('ok') and len(r[1].get('models', [])) > 0, str(r[1])[:80])
time.sleep(1)
al = get('/api/ai/logs?limit=3')[1]
check('AI 日志落库', len(al.get('items', [])) >= 1 and al['items'][0]['action'] == '测试',
      str(al.get('stats'))[:80])
# 地理规则兜底（可控替身：拦截 _chat 强制 AI country 为空，验证强地理词兜底分支，2026-11-07）
from app import ai as _ai
_orig_chat = _ai._chat
_ai._chat = lambda *a, **k: {"severity": "low", "intel_category": "参考情报", "country": "", "tags": []}
try:
    _f = _ai.analyze_tweet({"id": -1, "screen_name": "stub", "text": "Spanish developers have been gatekeeping this"},
                           {"enabled": True, "api_key": "stub", "model": "stub"})
except Exception as e:
    _f = {"geo_country": "", "geo_basis": f"stub_error:{type(e).__name__}"}
finally:
    _ai._chat = _orig_chat
check('AI漏判-强地理规则兜底', _f.get('geo_country') == '西班牙' and _f.get('geo_basis') == 'geo',
      f"{_f.get('geo_country')}/{_f.get('geo_basis')}")
# AI 判出国家时不兜底、basis=ai
_ai._chat = lambda *a, **k: {"severity": "low", "intel_category": "参考情报", "country": "日本", "tags": []}
try:
    _f2 = _ai.analyze_tweet({"id": -1, "screen_name": "stub", "text": "totally unrelated English text"},
                            {"enabled": True, "api_key": "stub", "model": "stub"})
except Exception as e:
    _f2 = {"geo_country": "", "geo_basis": f"stub_error:{type(e).__name__}"}
finally:
    _ai._chat = _orig_chat
check('AI已判-不覆盖且basis=ai', _f2.get('geo_country') == '日本' and _f2.get('geo_basis') == 'ai',
      f"{_f2.get('geo_country')}/{_f2.get('geo_basis')}")

# ============ G6 推送链路 ============
group('G6 推送链路')
if NOLIVE:
    print('  （--nolive 跳过真实推送）')
else:
    r = post('/api/channels/wecom/instances/0/test', timeout=90)
    check('企微实例真实推送', r.get('ok') and 'errcode' in r.get('detail', ''), str(r.get('detail'))[:60])
r = post('/api/channels/dingtalk/instances/0/test', timeout=60)
check('钉钉模拟推送', r.get('ok') and r.get('simulated'), str(r)[:80])
pl = get('/api/push/logs?limit=3')[1]['items']
check('推送日志留档', len(pl) >= 1 and pl[0]['status'] in ('成功', '模拟成功'), str(pl[0])[:60] if pl else '无')
# 六维过滤引擎单元断言（分类/标签为 2026-11-07 新增维度，防回归）
from app import engine as _eng
_flt = {"min_severity": "", "categories": ["数据泄露线索"], "countries": ["美国"],
        "industries": [], "tags": ["勒索软件"], "keywords": []}
_t_hit = {"category": "数据泄露线索", "geo_country": "美国", "industry": "金融",
          "tags": ["勒索软件", "LockBit（勒索软件家族）"], "text": "ransomware hit"}
check('推送过滤-六维全命中', _eng.pass_filter(_t_hit, _flt) is True)
check('推送过滤-分类不符拦截', _eng.pass_filter({**_t_hit, 'category': '参考情报'}, _flt) is False)
check('推送过滤-标签不符拦截', _eng.pass_filter({**_t_hit, 'tags': ['钓鱼邮件']}, _flt) is False)
check('推送过滤-国家不符拦截', _eng.pass_filter({**_t_hit, 'geo_country': '日本'}, _flt) is False)
check('推送过滤-标签JSON串兼容', _eng.pass_filter({**_t_hit, 'tags': '["勒索软件"]'}, _flt) is True)
# 组合模式：or=任一满足即推送（空维度不参与）；and=全部满足（默认，缺 mode 键兼容）
_flt_or = {"min_severity": "", "categories": ["参考情报"], "countries": ["日本"],
           "industries": [], "tags": [], "keywords": [], "mode": "or"}
check('推送过滤-OR任一命中放行', _eng.pass_filter(
    {**_t_hit, 'category': '参考情报', 'geo_country': '法国'}, _flt_or) is True)
check('推送过滤-OR全不命中拦截', _eng.pass_filter(
    {'category': '其他', 'geo_country': '法国', 'tags': ['钓鱼邮件'], 'text': 'x'}, _flt_or) is False)
check('推送过滤-OR仅等级命中放行', _eng.pass_filter(
    {'category': '其他', 'geo_country': '法国', 'tags': [], 'text': 'x',
     'ai_severity': 'critical'}, {**_flt_or, 'min_severity': 'high'}) is True)
check('推送过滤-缺mode键默认AND', _eng.pass_filter({**_t_hit, 'category': '参考情报'}, _flt) is False)

# ============ G7 报告任务闭环 ============
group('G7 报告任务闭环')
tid0 = db.get_conn().execute('SELECT id FROM tweets ORDER BY id DESC LIMIT 1').fetchone()['id']
r = post('/api/reports/tasks', {'title': f'冒烟测试任务 {time.strftime("%H:%M:%S")}',
                                'keywords': ['smoke-a', 'smoke-b', 'smoke-c'],
                                'tweet_ids': [tid0], 'tier': '速报', 'notes': '冒烟自动创建'}, timeout=20)
check('建任务', r.get('id') is not None, str(r)[:80])
smoke_tid = r.get('id')
if smoke_tid:
    s, d = get(f'/api/reports/tasks/{smoke_tid}/package')
    check('线索包生成', s == 200 and d.get('disclaimer') and len(d.get('tweets', [])) == 1)
    s2, d2 = get('/api/reports/tasks?limit=5')
    check('任务列表可见', any(t['id'] == smoke_tid for t in d2.get('tasks', [])))
    # 并发 claim ×2 → 一 200 一 409
    import threading
    results = []
    def _claim():
        try:
            post(f'/api/reports/tasks/{smoke_tid}/status', {'action': 'claim', 'assignee': 'smoke'})
            results.append(200)
        except Exception as e:
            results.append(409 if '409' in str(e) else str(e)[:30])
    t1, t2 = threading.Thread(target=_claim), threading.Thread(target=_claim)
    t1.start(); t2.start(); t1.join(); t2.join()
    check('原子领取防双领（200+409）', sorted(results) == [200, 409], str(results))
    # 写作中删除应被拒（状态机保护）
    try:
        delete(f'/api/reports/tasks/{smoke_tid}')
        check('写作中删除被拒（409）', False, '竞然删掉了')
    except Exception as e:
        check('写作中删除被拒（409）', '409' in str(e), str(e)[:60])
    # abort 释放 → 待写 → 可删
    r = post(f'/api/reports/tasks/{smoke_tid}/status', {'action': 'abort', 'note': '冒烟清理'})
    check('abort 释放回待写', r.get('status') == '待写', str(r)[:60])
    r = delete(f'/api/reports/tasks/{smoke_tid}')
    check('清理冒烟任务', r.get('ok') is True)
    try:
        get(f'/api/reports/tasks/{smoke_tid}/package')
        check('删除后不可取', False, '仍可访问')
    except Exception as e:
        check('删除后不可取', '404' in str(e) or 'Not Found' in str(e))

# ============ G8 筛选联动回归 ============
# 防"下拉显示 N 实际 0 条"复发：facet 计数必须恒等于同条件实际条数（2026-11-07 用户报告）
group('G8 筛选联动回归')
_, f = get('/api/facets')
cats, tglist = f.get('categories', []), f.get('tags', [])
check('facets 四维度齐全', all(k in f for k in ('countries', 'industries', 'categories', 'tags')),
      str(list(f.keys())))
tot = get('/api/tweets?limit=1')[1]['total']
cat_sum = sum(x['count'] for x in cats)
check('分类计数总和=推文总量', cat_sum == tot, f'{cat_sum} vs {tot}')
if cats:
    t2 = get(f'/api/tweets?category={cats[0]["name"]}&limit=1')[1]['total']
    check('Top 分类计数=实际条数', cats[0]['count'] == t2, f'{cats[0]["count"]} vs {t2}')
if tglist:
    t3 = get(f'/api/tweets?tag={tglist[0]["name"]}&limit=1')[1]['total']
    check('Top 标签计数=实际条数', tglist[0]['count'] == t3, f'{tglist[0]["count"]} vs {t3}')
# severity×分类交叉：每个分类的联动计数必须等于实际条数（用户 0 条场景）
_, f2 = get('/api/facets?severity=critical')
bad = []
for x in f2.get('categories', []):
    t4 = get(f'/api/tweets?severity=critical&category={x["name"]}&limit=1')[1]['total']
    if t4 != x['count']:
        bad.append(f'{x["name"]}: facet {x["count"]} vs 实际 {t4}')
check('severity×分类交叉计数一致（全部分类）', not bad, '；'.join(bad[:4]))
# severity×标签交叉（前 5）
bad = []
for x in f2.get('tags', [])[:5]:
    t5 = get(f'/api/tweets?severity=critical&tag={x["name"]}&limit=1')[1]['total']
    if t5 != x['count']:
        bad.append(f'{x["name"]}: facet {x["count"]} vs 实际 {t5}')
check('severity×标签交叉计数一致（Top5）', not bad, '；'.join(bad[:4]))
# 组合筛选结果必须真带标签
if tglist and cats:
    c0 = cats[1]['name'] if len(cats) > 1 else cats[0]['name']
    d6 = get(f'/api/tweets?tag={tglist[0]["name"]}&category={c0}&limit=5')[1]
    ok = all(tglist[0]['name'].lower() in json.dumps(t.get('tags', []), ensure_ascii=False).lower()
             for t in d6.get('items', []))
    check('组合筛选条目确实带标签', ok or d6.get('total', 0) == 0, f"total={d6.get('total')}")

# ============ 汇总 ============
print(f'\n================ 冒烟测试汇总：{PASS} PASS / {len(FAIL)} FAIL ================')
if FAIL:
    print('失败项：')
    for f in FAIL:
        print('  -', f)
    sys.exit(1)
print('系统核心链路全部可用 ✅')
