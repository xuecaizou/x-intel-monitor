# X-Intel · X(Twitter) 情报监控台

监控双源威胁情报（01 X/Twitter 账号经代理三级降级抓取 + 02 RSS 订阅源）→ AI 智能研判 / 智能翻译 / 地理行业标签 → 推送企业微信 / 钉钉 / PushPlus / WxPusher（每通道多实例+实例级筛选）。亮色中文控制台，监听 `0.0.0.0:8083` 局域网可访问，配套 HTML 帮助文档内置于系统（`/help`）。

- 技术栈：Python 3.14 + FastAPI + SQLite + APScheduler（前端原生 JS/CSS，零 CDN 依赖）
- 仓库位置：`D:\AI-work\黑客组织报告编写\x-intel-monitor\`
- 关联系统：ThreatEye（`../threat-intel-monitor/`，17 源开源情报监控，X 监控已剥离至本系统）

## 功能特性

### 采集
- **三级降级抓取**：① X syndication 官方接口（免登录）→ ② Nitter RSS（可用实例缓存 + twiiit.com 轮换器自动发现）→ ③ X 登录 Cookie 直连官方 GraphQL（实验性，配 auth_token+ct0 后启用）
- **必须走代理**：X 相关域名直连被 DNS 污染（实测解析到 Facebook IP 段），默认 `socks5://127.0.0.1:10808`，支持 http 代理；代理故障时如实报错、恢复自动续抓
- **每账号独立监控频率**：跟随全局 / 5 / 10 / 15 / 30 分钟 / 1 / 2 小时，按账号自身到期时间调度
- **增量抓取**：记录每账号置顶推文 id（X 雪花 ID 全局时间有序），未更新的推文直接短路，不重复入库、不消耗 AI 额度、不触发推送
- **AI 实时开关**：推文流标题栏一键启停自动研判/翻译（关闭后手动按钮不受影响）
- **抓取日志**：每轮持久化分层追踪明细（syndication✗429 → nitter✓20条/2.4s），`GET /api/fetch/logs` 可查，保留 500 轮

### 推文管理
- **双视图**：卡片（默认）/ 列表，切换记忆在 localStorage
- **推文截图**：带图推文在卡片内展示原图，服务端 `/api/media` 经抓取代理拉取 X 图片 CDN（客户端无需直连 X），点击图打开推文原文
- **收藏 / 已读 / 忽略**：卡片一键操作；状态筛选（全部/未读/已读/已忽略/收藏），默认视图隐藏已忽略
- **勾选批量**：批量已读 / 忽略 / 删除 / 批量 AI 翻译 / 批量 AI 研判（批量 AI 每次最多 20 条串行执行）
- **一键清理**：保留近 1/2/3 天、1 周、30 天可选，★ 收藏的推文永久保留
- **排序与时区**：按 X 雪花 ID 排序（全局时间有序，补抓批次内不乱序）；时间统一转本地时区显示

### AI 能力
- **厂商预设一键填充**：DeepSeek / 智谱 GLM / Kimi / 阿里通义 Qwen / OpenAI / 本地 Ollama / 自定义（任意 OpenAI 兼容接口）
- **AI 智能研判**：威胁类型、建议等级（critical~info）、态势评估、处置建议、IOC 列表、置信度；prompt 携带原文 + 中文参考译文的中英对照
- **AI 智能翻译**：标题式中文摘要 + 完整中文翻译（专有名词 / IOC / CVE 不译）
- **关键词标签**：AI 研判时提取 ≤10 个情报要素标签（威胁手法/技术要素/恶意家族/威胁组织/涉事企业/攻防双方国家），英文缩写译成「英文（中文说明）」如 C2（命令与控制服务器）；`tags` JSON 列 + `/api/tweets?tag=` 筛选 + facets 标签统计 + 卡片 chip 点击筛选 + 首页 TOP TAGS 卡；存量用 `python backfill_tags.py` 回填（顺带补行业/单位缺口）
- **地理行业标签**：国家 → 省/州 → 城市 三级地区（未提及如实留空）、涉事单位全称、所属行业；无地理信息标「未知」不重复消耗额度。判定分三层（国旗 → 强地理词 → 企业名仅作弱线索），**企业总部所在地不视为涉事国家**、FBI/CISA 等披露机构不参与判定，判定依据记入 `geo_basis`；`python backfill_geo.py --audit [--fix]` 可复核/修正存量标签
- **情报分类（16 类）**：预警/参考/舆论/政策/动态/态势/主体情报 + 木马/钓鱼网站/钓鱼邮件/水坑/暴破扫描/僵尸网络/蠕虫/网页篡改/数据泄露线索，AI 自动分类 + 脚本规则兜底，支持分类筛选
- **模型列表拉取**：「拉取模型」按钮调厂商 `/models`（OpenAI 兼容端点），结果填入 model 输入框下拉（datalist），仍可手输
- **触发**：新推文自动处理（每轮限 auto_limit 条控成本）/ 推文卡按钮手动 / 「补齐地区标签」批量补标

### 监控源 02 · RSS
- 「监控源配置」面板 02 区添加 RSS/Atom 源（内置 TheHackersNews feedburner 实测）；「测试」按钮先验证连通（返回条目数+样例标题）
- 条目带 `RSS:` 前缀复用 tweets 管道——AI 研判/翻译/分类/地理/推送/筛选全通用；guid 增量短路（首条未变 0 消耗）；每源独立频率（默认跟随全局）

### 推送
- **四通道 × 多实例**：企业微信群机器人 / 钉钉机器人（加签）/ PushPlus / WxPusher，每通道可加 N 个实例（如 5 个企微群各一份）；**实例级推送筛选**（等级/国家/行业/关键词，仅对该实例生效，叠加全局过滤）；凭据页面内联编辑（脱敏回显），留空 = 模拟模式；企微 45009 限频自动退避重试
- **推送策略**：摘要合并（≤8 条）+ AI 判为 critical/high 的推文即时单推（含地区、中英对照、研判、建议）；企业微信通道即时单推自动附**截图原图**（image 消息 base64 直传，无需公网）
- **推送过滤六维+模式**：最低等级 / 情报分类（精确）/ 国家 / 行业 / 关键词标签（与卡片橙标精确一致）/ 关键词（顿号分隔）；**组合方式可选「且（全部满足）/ 或（任一满足）」，默认且**；对摘要与即时推送同时生效；命名规则同构供实例绑定；被过滤轮次记入推送日志
- **按通道能力分发**：报告交付类消息按通道能力路由——企微收 markdown + 报告交付附件（upload_media 两步法）；钉钉收 markdown；PushPlus 收 HTML 富文本；WxPusher 收摘要+线索包跳转卡片

### 报告任务（写作桥接）
- **定位**：系统不自己写报告——建任务/发线索包/复跑门禁验收/推送；正文由外部 ZCode `report-writer` Agent 按内部报告编写规范产出（规范不在本仓库内）
- **流程**：勾选推文「生成报告任务」→ 线索包落盘（原文+AI 预研判+geo+免责声明）→ Agent 原子领取（并发双领 409）→ 取料写作 → 交付回写 → **系统复跑配置的机械门禁脚本**（不信任 Agent 自报：exit 0 → 已交付待推；≠0 → 待人工，宁拦住不放过）→ 人工确认推送
- **状态机**：待写 / 写作中 / 已交付待推 / 已推送 / 待人工 / 已取消；`auto_push=1` 可验收 PASS 自动推送（默认人工确认）

### 控制台与文档
- 亮色玻璃拟态主题；左侧菜单视图切换（推文流/监控账号/AI/报告任务/推送过滤/通道/抓取日志/推送记录，48px 图标条 ⇄ 220px 展开，均记忆）；KPI（今日新增 / AI 研判 / 地区覆盖 / 账号 / 推送成功率）
- 筛选：等级 / 国家 / 行业 / 分类 / 关键词标签 / 账号 / 状态 / 全文检索（覆盖原文、译文、研判、地理、单位、行业、标签）；下拉计数随其它筛选动态联动（数字=选它能得到的真实条数），卡片标签 chip 点击即筛
- 推送记录时间轴；抓取倒计时（进度环）+「立即抓取」
- 内建帮助文档 `/help`：功能说明、REST API 参考、架构图、推荐 Prompt 模板、FAQ；Swagger `/docs`、OpenAPI `/openapi.json`

### 报告编写工作台（协作写作 · 2026-11-14）
- **待编写线索候选**：情报卡片「+待编写」/批量「加入待编写」/事件详情「创建写作候选」/手工新建；规范化去重合并、优先级/负责人/状态机（待处理→写作中→已转报告/暂缓/不采用）、AI 选题推荐（任务链：排队→运行→完成/失败→重试，重评不覆盖人工决定，标记"线索有更新"）
- **多模板管理**：新建/复制/停用/发布不可变版本/版本历史/变量白名单预览（缺失变量与未定义占位明确提示）；模板正文支持多人实时协作；报告固定发布版本，模板后续修改不影响既有报告
- **Markdown 实时协作**：独立编辑器页 `/editor?doc=…`（CodeMirror 6 + Yjs CRDT）；两浏览器实时同步、远程光标/成员、断线重连、撤销重做、版本历史与恢复、提交审阅（绑定版本，批准不继承新内容）；"已同步"=服务端落库确认（awareness saved_seq）
- **Agent 协作**：可吊销、范围明确的 agent_ 令牌；读取正文/来源快照、提交修改建议（锚点精确匹配，零/多匹配明确失败）、受控直接修改（base_rev 乐观锁 + client_id 幂等）；示例 `scripts/agent_demo.py`
- **一键下载 Word**：pandoc 本地转换真实可编辑 .docx（中文宋体/黑体样式、标题/列表/表格/代码/受控本地图片），固定版本捕获、内容哈希缓存、超时与缺依赖明确提示；导出不改审核状态、与报告门禁独立
- **权限**：成员管理（read/write/admin）+ 文档级 ACL（只读由服务端 WS 层强制）+ 审核仅管理员；详情见 `docs/ops-runbook.md` §9

## 快速开始

```bash
pip install -r requirements.txt
python run.py
# 控制台 http://本机IP:8083（默认监听 0.0.0.0，局域网可访问）
# 帮助文档 http://本机IP:8083/help   Swagger http://本机IP:8083/docs
```

首次启动自动建库并预置 @DailyDarkWeb 监控账号；代理与 AI 配置见下。

## 系统架构

```
┌──────────────────┐   ┌───────────────────────────┐   ┌─────────────────┐   ┌──────────────────┐
│  采集层（经代理）  │   │  处理管道                  │   │  SQLite 存储     │   │  输出层           │
│                  │   │                           │   │                 │   │                  │
│ A syndication    │──▶│ 到期账号调度 + 置顶 id     │──▶│ tweets          │──▶│ Web 控制台（亮色） │
│   官方接口        │   │ 增量短路                   │   │  （AI+地理标签）  │   │ REST API+Swagger │
│ B Nitter RSS     │   │ UNIQUE 去重入库            │   │ x_accounts      │   │ 推送四通道        │
│   实例缓存+轮换   │   │ AI：研判/翻译/地理标签      │   │ push_logs / kv  │   │ 外部工具/AI Agent │
│ C Cookie 直连     │   │ 推送过滤（等级/国家/        │   │ runtime.json    │   │ （独立 AI API）   │
│   GraphQL        │   │  行业/关键词）              │   │                 │   │                  │
└──────────────────┘   └───────────────────────────┘   └─────────────────┘   └──────────────────┘
     ▲ 必须走本地代理（X 域名被 DNS 污染）        ▲ AI 为 OpenAI 兼容接口（厂商可换）        ▲ 模拟模式兜底
```

抓取周期默认 15 分钟（`fetch.interval_minutes` 可配）；调度器 APScheduler，不可用时自动降级 threading。

## 配置说明（config.yaml，页面改动优先存 data/runtime.json）

```yaml
server:   { host: 0.0.0.0, port: 8083 }        # 0.0.0.0 = 局域网可访问
fetch:    { interval_minutes: 15, timeout_seconds: 25, retries: 1 }
network:  { proxy: "socks5://127.0.0.1:10808" } # 全局代理（X 源必须）
x:
  strategy: "auto"                # auto = syndication → Nitter → Cookie 直连
  show_replies: true
  max_items: 20                   # 每账号每轮最多入库条数
  nitter_instances: ["nitter.meowing.monster"]   # 兜底实例（会随时间变化）
  auth_token: ""                  # 可选：X 登录 Cookie（第三级直连）
  ct0: ""
ai:
  enabled: true
  base_url: "https://api.deepseek.com/v1"   # OpenAI 兼容；页面有厂商预设
  api_key: ""
  model: "deepseek-chat"
  auto_analyze: true              # 新推文自动研判+翻译
  auto_limit: 10                  # 每轮自动处理上限（控成本）
  max_tokens: 1200
  proxy: ""                       # AI 接口可选代理
channels:
  wecom:    { enabled: true,  webhook: "" }
  dingtalk: { enabled: true,  webhook: "", secret: "" }
  pushplus: { enabled: false, token: "" }
  wxpusher: { enabled: false, app_token: "", uid: "" }
push:
  digest_max_items: 8
  filters: { mode: "and", min_severity: "", categories: "", countries: "", industries: "", tags: "", keywords: "" }  # 也可页面配置
```

## REST API 参考

交互式文档 `/docs` · OpenAPI `/openapi.json` · **全部接口需认证**（浏览器会话 Cookie + CSRF，或 `Authorization: Bearer xintel_…`）；未初始化管理员时一律 503，不存在匿名兜底。

### 报告编写工作台（协作写作）

| 方法 | 端点 | 说明 |
|---|---|---|
| GET/POST | `/api/writing/candidates` | 候选列表（status/q/source_type）/新建（去重合并返回 merged） |
| POST | `/api/writing/candidates/from-tweets` `/from-event` | 批量情报合并 / 事件建候选 |
| PUT/DELETE | `/api/writing/candidates/{uid}` | 编辑（乐观锁 expected_version）/删除（仅待处理·暂缓·不采用） |
| POST | `/api/writing/candidates/{uid}/decision` `/convert` | 采纳/暂缓/不采用 / 转为报告（单事务幂等，返回 doc_uid） |
| POST/GET | `/api/writing/recommend` · `/api/writing/recommend-tasks` | AI 推荐排队 / 任务状态；`/retry` 重试 |
| GET/POST/PUT/DELETE | `/api/writing/templates` · `/{tid}/publish` `/duplicate` `/status` `/versions` `/preview` | 模板 CRUD 与发布（维护=admin，预览=read） |
| GET/POST | `/api/writing/docs` `/api/writing/docs/{uid}` | 文档列表 / 详情（含 my_permission） |
| GET | `/api/writing/docs/{uid}/revisions[/{rev}]` | 版本历史 / 修订内容 |
| POST | `/api/writing/docs/{uid}/restore` `/submit-review` | 恢复历史（新修订）/ 提交审阅（固定版本） |
| POST | `/api/writing/docs/{uid}/review` | 审核批准/驳回（admin） |
| GET/POST | `/api/writing/docs/{uid}/suggestions` · `/{sid}/resolve` | 建议（读权限可提）/ 接受·拒绝（写权限） |
| GET | `/api/writing/docs/{uid}/export.md` `/export/word` `/export/word/{eid}/download` `/exports` | 下载 MD / 发起 Word 导出 / 下载 / 记录 |
| POST/GET | `/api/writing/docs/{uid}/assets` | 受控素材上传（base64 ≤8MB）/列表 |
| PUT | `/api/writing/docs/{uid}/acl` `/title` | ACL（admin）/ 标题 |
| POST | `/api/writing/docs/{uid}/ws-ticket` | WebSocket 一次性票据（60s） |
| GET | `/api/writing/summary` | 首页汇总（服务端全量聚合） |
| POST | `/api/auth/users` · GET `/api/auth/users` · POST `/{name}/role` | 成员管理（admin） |
| POST | `/api/agent/tokens`（admin）· `/api/agent/docs…`（agent） | Agent 令牌 / Agent 文档读取·建议·受控修改 |
| WS | `/ws/docs/{doc_uid}` | Yjs 实时协作（会话 Cookie 或 ticket；Origin 校验） |

### 推文数据与管理

| 方法 | 端点 | 说明 |
|---|---|---|
| GET | `/api/tweets` | 推文流。参数：`severity` / `screen_name` / `country` / `industry` / `status`（all·unread·read·ignored·starred）/ `q`（全文检索）/ `limit` / `offset` |
| GET | `/api/facets` | 国家与行业去重计数 |
| POST | `/api/tweets/{id}/flag` | 收藏/已读/忽略：`{"starred":true,"read":true,"ignored":false}` 任意组合 |
| POST | `/api/tweets/batch` | `{"ids":[…],"op":"read·unread·ignore·unignore·delete·ai_translate·ai_analyze"}` |
| POST | `/api/tweets/cleanup` | `{"keep_days":1\|2\|3\|7\|30}`，收藏永久保留，返回删除数 |
| GET | `/api/media?url=…` | 推文截图代理（仅 pbs/ton.twimg.com） |

### AI 能力（独立接口，不落库，供外部工具 / Agent 调用）

| 方法 | 端点 | 说明 |
|---|---|---|
| POST | `/api/ai/analyze` | `{"text":"…","screen_name":"可选"}` → 结构化研判（threat_type/severity/assessment/recommendations/iocs/actors/country/province/city/organizations/industry/confidence/model） |
| POST | `/api/ai/translate` | `{"text":"…"}` → `{title_zh, text_zh}` |
| POST | `/api/ai/tag` | `{"text":"…"}` → `{country, province, city, organizations[], industry}` |
| POST | `/api/ai/test` | 连通测试 · `POST /api/ai/backfill?limit=10` 批量补标 |
| GET/PUT | `/api/ai/config` | AI 设置（Key 脱敏回显） |
| GET | `/api/ai/models` | 拉取厂商 `/models` 模型列表（OpenAI 兼容端点，供页面下拉填充） |

### 监控账号

| 方法 | 端点 | 说明 |
|---|---|---|
| GET/POST | `/api/x/accounts` | 列表 / 新增 `{"screen_name":"…"}` |
| PUT | `/api/x/accounts/{id}` | 修改 `screen_name / display / fetch_minutes` |
| POST | `/api/x/accounts/{id}/toggle` | 启停 |
| DELETE | `/api/x/accounts/{id}` | 删除 |

### 抓取 / 统计 / 推送

| 方法 | 端点 | 说明 |
|---|---|---|
| POST | `/api/fetch/run` | 立即触发一轮（仅到期账号） |
| GET | `/api/fetch/logs` | 抓取日志：每轮触发方式/账号数/新增与 AI 数/分层追踪明细（保留 500 轮） |
| GET | `/api/stats` | 今日/累计/AI 研判/地区覆盖/账号/推送统计/倒计时/最近抓取结果 |
| GET/PUT | `/api/push/filters` | 推送过滤六维 |
| GET | `/api/channels` · `POST /api/channels/{key}/toggle` | 通道状态 / 启停 |
| GET/PUT | `/api/channels/{key}/config` | 通道凭据（GET 脱敏） |
| POST | `/api/channels/{key}/test` | 测试推送 |
| GET | `/api/push/logs` | 推送记录 |

### 报告任务（桥接外部 report-writer Agent）

| 方法 | 端点 | 说明 |
|---|---|---|
| POST | `/api/reports/tasks` | 建任务 `{"title","keywords":[≥3组],"tweet_ids":[≤50],"tier":"速报\|完整","notes","auto_push"}` → 线索包落盘 `data/report_tasks/task_<id>.json` |
| GET | `/api/reports/tasks` | 任务列表 `?status=&limit=&offset=`（全字段） |
| GET | `/api/reports/tasks/{id}/package` | **Agent 取料入口**：线索包 JSON（推文原文+AI 预研判+geo+免责声明） |
| POST | `/api/reports/tasks/{id}/status` | `{"action":"claim\|heartbeat\|abort"}`；claim 原子领取，并发双领 409 |
| POST | `/api/reports/tasks/{id}/deliver` | 交付回写+**系统复跑机械门禁验收**（可选 `word_limit`）；exit 0 → 已交付待推，否则待人工 |
| POST | `/api/reports/tasks/{id}/push` | 人工确认推送（仅"已交付待推"）：企微 markdown+附件；其余通道按能力分发 |
| DELETE | `/api/reports/tasks/{id}` | 取消（写作中 409） |

## Agent / 外部工具接入

系统在 `/help` 第 7、8 节提供接入指南与 **5 个可直接复制的 Prompt 模板**（系统接入 Prompt、每日威胁简报、定向监控分析、任意文本研判、IOC 汇总导出），模板内嵌 API 说明与字段含义，替换 `{BASE}` 即可粘贴给任何具备联网能力的 AI（Claude/GPT/Cursor/Dify/本地 Agent）。

典型链路：

```
1. GET /api/tweets?severity=high&country=美国&limit=20   ← 拉取情报（自带中文翻译与地理标签）
2. GET /api/facets → 确认可选维度                          ← 按需分流
3. POST /api/ai/analyze {"text":"外部文章正文"}            ← 研判任意外部文本
4. 轮询 GET /api/stats 的 last_fetch.at 感知增量           ← 变化后拉 /api/tweets?limit=10
```

## 常见问题

- **抓取全失败？** 先确认本地代理可用（X 域名被 DNS 污染必须走代理）；代理出口节点切换后无需改配置。
- **AI 报 429 / 额度用尽？** 供应商侧额度问题，报错如实显示在推文卡上；换 Key 或充值后点卡上按钮重试，或用「补齐地区标签」批量补跑。
- **推文为什么是英文？** 等 AI 自动翻译完成即显示中文块；也可手动点「翻译」。
- **时间看不懂？** 页面时间已按本地时区渲染；数据库/API 原始值统一为 GMT。
- **安全提示**：系统已内建认证（会话 Cookie + CSRF / Bearer Token，角色 read·write·admin），但仍建议仅监听回环或内网；AI Key 明文存于 `data/runtime.json`，勿外传。

## 目录结构

```
x-intel-monitor/
├── run.py               # 启动入口
├── config.example.yaml  # 配置模板（页面改动优先存 data/runtime.json）
├── requirements.txt
├── seed_demo.py         # 离线演示数据
├── app/
│   ├── main.py          # FastAPI 入口（lifespan 初始化 + /help 路由）
│   ├── config.py        # 三层配置合并（默认←config.yaml←runtime.json）
│   ├── db.py            # SQLite：tweets(含AI/geo列) / x_accounts / push_logs / kv
│   ├── engine.py        # 管道：抓取→去重→AI→过滤→逐实例投递
│   ├── scheduler.py     # APScheduler（降级 threading）
│   ├── twitter.py       # 三级降级抓取 + 媒体提取 + 增量短路
│   ├── rssfetch.py      # RSS/Atom 采集：逐条扫描去重 + source_id 归属
│   ├── ai.py            # OpenAI 兼容研判/翻译/标签/分类（纯计算，提交归调用方）
│   ├── integrity.py     # 完整性分级 + 审核三态（valid/stale/none）
│   ├── eventexport.py   # 事件标准化导出（JSON/时间线 CSV/IOC JSON+CSV）
│   ├── auth.py          # 管理员/会话/CSRF/Token（read·write·admin）
│   ├── safefetch.py     # 出站 URL/路径安全（逐跳校验/大小上限/受控目录）
│   ├── backup.py        # 一致性备份与安全恢复（默认暂停调度与外发）
│   ├── wecom_media.py   # 企微附件两步法（upload_media→file）
│   └── notify/          # Markdown 模板 + 四通道
├── api/routes.py        # REST 路由
├── web/index.html       # 控制台（亮色）
├── web/help.html        # 内建帮助文档（/help）
└── data/                # xintel.db / runtime.json
```

- **事件聚合**：多来源情报归并为事件（稳定 `evt_` ID + 版本）；关联依据分级（人工/同一公告/同一受害单位/仅共享标识）；
  **共享 CVE/IP/组织名不得自动合并**，仅作候选；转载不计独立证据；支持人工合并/拆分/解除并全程留痕
- **人工审核**：审核绑定「事件版本 + 证据指纹」，旧版本/内容已变不能批准新内容（三态 valid/stale/none）；
  审核队列 + 操作者留痕；事件详情页对过期审核显示"历史决定（vN）… 需重新审核"
- **版本与并发**：底层情报变化（AI 结果落地/人工修订）经证据指纹检测→事件版本推进 + `evidence_changed` 留痕；
  内容修改/版本检查/审计同一事务（乐观锁 `expected_version` 并发只允许一个成功）；写事件、加/移证据、合并、拆分为单事务
- **标准化导出**：事件 JSON / 时间线 CSV / IOC JSON / IOC CSV（schema_version、事件版本、**独立只读事务一致性快照**、
  五桶分列 cve/attack/victim/reference/invalid、CVE 独立 vulnerability、缺角色证据默认 `role=unknown`、
  `verified` 仅来自人工审核绑定当前版本、partial 逐条标注、去重保留全部来源、CSV 公式注入防护）
- **报告任务**