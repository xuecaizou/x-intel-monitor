# X-Intel 架构与业务关联说明（实际实现，非计划稿）

> 更新：2026-11-07。本文件描述**当前实际运行**的模块职责、数据流、规则含义与统计口径。与 /arch 页面互为补充：/arch 面向总览图，本文件面向关联语义与易错点。

## 1. 模块职责（实际）

| 模块 | 职责 | 不负责 |
|------|------|--------|
| app/config.py | DEFAULTS + config.yaml + runtime.json 三层深合并；**运行时配置唯一入口** | 不缓存（每次 load() 读文件） |
| app/db.py | SQLite 单连接+线程锁；全部表读写；facets 统计；push_stats_today | 业务判定 |
| app/twitter.py | X 抓取三级降级（syndication→Nitter→Cookie）；代理读取 x.proxy | 入库（engine 调 db） |
| app/rssfetch.py | RSS/Atom 抓取；_norm_time 时间归一；guid 增量短路 | 入库（同上） |
| app/ai.py | 研判/翻译/分类/模型列表；analyze_tweet 内含地理规则兜底（F7） | 推送 |
| app/geo_rules.py | 确定性地理判定三层（国旗→强地理→企业名弱线索默认不采信） | 写库 |
| app/engine.py | 调度编排：run_fetch（采集→AI→推送）、dispatch（摘要+即时）、pass_filter（六维+模式）、报告任务编排 | 不直连外部（经 notify/ai） |
| app/notify/ | 四通道 send 抽象；企微 _post 退避+节奏+图片直传+errcode 提示 | 过滤判定（engine 做） |
| app/reports.py | 报告任务状态机、线索包快照、门禁脚本复跑、按通道能力分发推送 | 情报采集 |
| api/routes.py | 全部 REST 端点；PUSH_FILTER_FIELDS 是过滤字段唯一事实源 | 业务逻辑（薄层） |
| backfill_geo.py / backfill_tags.py | 离线存量补标工具（自动提交短事务，可断点续跑） | 实时链路 |

## 2. 核心数据流（实测确认）

```
监控源(x_accounts/rss_sources) → run_fetch(15min 调度/手动, _running 互斥防重叠)
  → twitter/rssfetch 抓取 → insert_tweet(UNIQUE(screen_name,tweet_id) 幂等)
  → run_ai_stage(新推文全量研判+翻译；analyze_tweet: AI→geo_rules 兜底→tags≤10)
  → pending = tweets_pending_push(200)（pushed=0 AND ignored=0，统一排序键）
      ∧ pass_filter(全局六维规则) ∧ _pushable_by_ai(门控)
  → dispatch：每启用实例 → 绑定命名规则(替代全局) → pass_filter(实例规则)
      → 摘要(≤8条) + critical/high 即时单推(企微附图)
      → push_logs 逐实例逐条记录（成功/失败/模拟成功）
  → mark_pushed(本轮全部进入推送流程的推文)  ← 语义见 §4
```

## 3. 关键语义（逐项核实过）

### 推送规则
- **六维**：min_severity（等级阈值）/ categories（精确）/ countries（子串）/ industries（子串）/ tags（精确）/ keywords（全文子串）。
- **模式 mode**：and（默认，全部满足）/ or（任一非空维度满足）。**维度内多值恒为或**。空维度不参与；全空=放行。
- **实例绑定**：filter_rule 指向命名规则 id → **替代**全局规则（非叠加）；未绑定或规则已删 → 回退全局。删除命名规则时自动清空实例绑定。
- **即时 vs 摘要**：同一实例、同一实例规则，摘要取排序后前 max_items(8) 条聚合一条消息，critical/high 另发即时单推（企微附图）。
- **配置生效时机**：runtime.json 每次 load() 即时读 → 保存后**下一轮 dispatch 即生效**（本轮已开始的 dispatch 不受影响）。
- **过滤轮记日志**：本轮全被过滤 / 实例全被过滤都会写 push_logs（"推送过滤"/实例名），便于对账。

### 推送标记 pushed（⚠ 三个已记录的语义边界，见 docs/bug-ledger.md B14-B16）
- pushed 是**推文级全局标记**：本轮所有启用实例共享；轮后统一标记（无论成败、无论真实/模拟）。
- 推论 1：实例 A 发过的推文，之后新启用的实例不会补发（同轮内不受影响）。
- 推论 2：实例发送失败不重试（下一轮只处理新推文）。
- 推论 3：模拟发送也会"消费"推文（防止未配置通道每轮重发模拟；代价是之后配置真实凭据不补发历史）。
- 以上为**现状如实记录**，是否变更见 requirements.md C1-C3。

### AI 与标签
- aiquick 开关（推文页）与 AI 设置面板的 auto_analyze **是同一配置字段**（两处 UI 写同一键），ai_auto_on=enabled∧auto_analyze∧api_key。
- tags：AI 输出 ≤10 个，截断去重；**精确匹配**用于筛选与推送。
- geo：AI 判定优先；AI 为空时 geo_rules 强线索（flag/geo）兜底，geo_basis 如实记录（ai/flag/geo）。
- 重新研判会覆盖 ai_* 与 geo/tags 字段（AI 优先），geo_basis 如实反映本次来源。
- AI 失败：ai_status=error + ai_retries 累计，≥2 次后附说明放行推送；批量重试用 /api/tweets/batch op=ai_analyze（**5 条/批**，20 条会超 HTTP 时窗）。

### 统计口径
- push_stats_today：total=今日全部推送日志行；ok=成功+模拟成功；**sim 单列模拟次数**。KPI 显示"今日推送 N 次 · 含模拟 M"。
- AI 日志 stats：calls/ok/error/avg_ms（一次调用含重试计一条）。
- geo_tagged=geo_country 非空数；categorized=category 非空数。

### 数据清理与历史引用
- cleanup_old：按 created_at 删 keep_days 前推文，starred 豁免。**不检查 report_tasks 引用**（线索包是快照，报告不受影响；原始推文引用会悬空）——见 C4。
- ai_logs.fetch_logs 滚动保留 500 条，与推文删除无关。

### 前端易错点
- 侧栏开关按钮是 `#sb-toggle-btn`（.sb-top 是容器无绑定）；状态类在 `body.sb-expanded`。
- GET /api/channels/.../config 恒返回掩码——验证存储值必须读 runtime.json 或看 errcode。
- facets 下拉计数是"在当前其它筛选下选它能得到的条数"（联动式）。

## 4. 配置生效矩阵

| 配置 | 持久化 | 生效时机 |
|------|--------|---------|
| 监控源增删/启停/频率 | runtime.json / db | 下一轮调度（或手动抓取）立即 |
| AI 设置（含 aiquick） | runtime.json | 下一轮 run_ai_stage / 下次调用 |
| 推送过滤六维+模式 | runtime.json | 下一轮 dispatch |
| 通道实例凭据/绑定规则 | runtime.json | 下一轮 dispatch；测试按钮即时 |
| 报告任务 | db + 线索包文件 | 即时 |

## 5. 已知历史坑（详见 docs/bug-ledger.md 探针误差记录）

1. 验证配置必须用 app.config.load()（三层合并），不能读 raw runtime.json。
2. GET /config 类端点恒掩码，验证存储值须直核 runtime.json。
3. 冒烟测试必须用 Python 3.14（G6 import engine → feedparser）。
4. smoke 的 get() 内部已 URL 编码，测试代码不得预编码中文参数。
5. 多标签页浏览器自动化必须按 URL 匹配标签，tabs.list()[0] 未必是目标。

## 6. 认证与访问控制（第四批）

```
浏览器 → /login → POST /api/auth/login → HttpOnly Cookie(SameSite=Lax,12h) + CSRF token
脚本/Agent → Authorization: Bearer xintel_xxx → 角色 read/write/admin
```

- 所有 API 路由在装饰器上挂 `dependencies=[Depends(auth.require("<role>"))]`：
  read 20 个 / write 40 个 / admin 1 个；认证端点 3 个免依赖。
- 会话写请求必须带 `X-CSRF-Token`（与登录返回的 csrf 比对）。
- 未初始化（无管理员）时一律 503；不存在"匿名兜底"。
- `db.update_ai()` 无版本检查（已知限制 L1）。

## 7. 出站与文件安全（第四批）

- `app/safefetch.py`：
  - `safe_join(root, *parts)`：拒绝绝对路径/盘符/UNC/../URL 编码变体/符号链接逃逸
  - `safe_report_path(deliver_dir, filename)`：deliver_dir 必须在 data/report_tasks 下，filename 必须纯文件名
  - `validate_url(url, allow_private)`：协议白名单 + DNS 解析 + 私网/回环/链路本地/保留段拦截
  - `fetch_limited`：禁自动重定向（逐跳校验）、≤3 次重定向、≤8MB
- RSS 抓取与媒体代理均接入；`fetch.allow_private_urls` 为管理员显式例外。
- **代理场景**：SOCKS 代理下 DNS 由代理解析，本层校验基于本地解析结果——
  已记录限制：代理解析目标可能与本地不同，管理员配代理时应确保代理侧出口策略。

## 8. 备份与恢复（第四批）

- SQLite `journal_mode=WAL`；备份用 `sqlite3.Connection.backup()`（含 WAL 未检查点事务）
- manifest：created_at / schema_version / code_commit / sqlite_integrity / files[sha256]
- 恢复：仅到空目录（拒绝覆盖）+ integrity_check + 关键表计数 + 人工修订保留校验
- 恢复后**不自动启动调度**（由部署脚本控制）；待发送/结果不明任务需人工处置

## 9. 进程模型与并发支持范围（第五批）

**当前仅支持单进程部署。**
- 调度器（APScheduler BackgroundScheduler / threading 回退）为**进程内单实例**；
  启动多个 uvicorn worker 或多个 `run_fetch` 实例会导致**重复调度**（同一轮抓取/推送被执行多次）。
- `app/db.py` 的 `_lock` 与 `config.py` 的 `_rt_lock` 均为**线程锁，不是跨进程锁**；
  投递消息的原子领取（`claim_msg`）依赖 SQLite 的 `UPDATE ... WHERE state IN (...)`，
  在单机多进程下**仍具备原子性**（SQLite 文件锁保证），但调度重复问题仍存在。
- 因此：
  - 禁止 `uvicorn --workers N`（N>1）、gunicorn 多 worker、`--reload` 开发模式长期运行；
  - 如需多实例，必须先引入外部分布式锁/队列（属后续架构变更，不在本批范围）；
  - 单进程下：调度互斥由 `engine._running` 保证（手动与定时不重叠）。
- 验证方式：`netstat` 确认单 PID 监听、`ps` 确认仅一个 `run.py`；
  代码层无跨进程锁声明（已核实）。

## 10. 事件聚合 / 审核 / 导出（最终收尾）

```
情报(tweets) ──event_evidence──> 事件(events)
                    │                  │
                    │                  ├─ event_revisions（每次变更留痕，version+1）
                    │                  └─ event_reviews（绑定 event_version 的审核）
                    │
                    └─ 导出：export.json / timeline.csv / iocs.json / iocs.csv（单事务快照）
```

**关联依据分级与自动合并边界**：
| basis | 含义 | 可自动合并 |
|-------|------|-----------|
| manual | 人工显式关联 | — |
| official_same | 同一官方公告/同一事件 ID | 否（需人工） |
| victim_same | 同一受害单位 | 否（需人工） |
| shared_indicator | 仅共享 CVE/IP/组织名 | **绝不允许**（仅候选） |

**证据独立性**：`source_kind=original` 才计入独立证据；`repost/translation` 单列，不冒充独立来源。

**审核语义**：审核对象是「事件 + 版本」；版本推进后旧审核自动失效（查询时按 `event_version=当前version` 匹配）。

**完整性 → 导出**：`verified/normalized` 可作结论；`partial` 逐条标注 `usable_as_conclusion=false`；`invalid` 不进入可用结论。
