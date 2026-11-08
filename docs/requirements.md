# X-Intel 需求追踪表

> 维护约定：每条需求含 **ID｜内容｜来源与确认依据｜验收条件｜实现位置｜关联模块｜验证用例｜状态**。
> 状态取值：✅已实现且已验证 / 🟡已实现未验证 / 🔶部分实现 / 📋已确认未实现 / ⚠曾实现后丢失 / 💭待确认想法 / ❌已取消或被替代。
> 验证用例列指向 healthcheck.py / smoke_test.py 的具体断言或浏览器实测记录。
> 本表由 2026-11-07 系统性盘点建立，来源：项目记忆库（x-intel-monitor.md 全版本记录）、/help 与 /arch 内建文档、代码实读、本会话验证证据。

| ID | 需求内容 | 来源与依据 | 验收条件 | 实现位置 | 关联模块 | 验证用例 | 状态 |
|----|---------|-----------|---------|---------|---------|---------|------|
| R1 | 双源监控：01 X 账号（syndication→Nitter→Cookie 三级降级、每账号独立频率、置顶 id 增量短路）+ 02 RSS（guid 增量短路、RSS: 前缀隔离） | 2026-11-05 v9 用户明确需求；/help 监控源章节 | 新增源即采集入库；停用源不再采集；日志逐源可追踪 | app/twitter.py、app/rssfetch.py、app/engine.py run_fetch | engine/db/scheduler | smoke G4、healthcheck RSS 组、fetch_logs trace | ✅ |
| R2 | AI 研判/翻译/16 类分类（OpenAI 兼容、四层 JSON 兼容） | 2026-10-26 起持续需求 | 新推文自动研判+翻译；失败不阻塞推送门控另有规则（R13） | app/ai.py、app/engine.py run_ai_stage | ai/engine/db | smoke G5、healthcheck AI 组 | ✅ |
| R3 | 地理标签三层判定（国旗→强地理词→企业名仅弱线索不采信）；企业总部≠受害国；披露机构不参与 | 2026-11-06 Bug2 修复确认（用户明确需求） | 企业名不直接定国家；geo_basis 记录依据 | app/geo_rules.py、backfill_geo.py、app/ai.py（入库兜底 F7） | ai/geo_rules/backfill | smoke G5 兜底双断言、B6 抽查 | ✅ |
| R4 | 统一排序键 `COALESCE(NULLIF(published_at,''),created_at) DESC, id DESC`；禁用 CAST(tweet_id)（RSS md5 恒 0） | 2026-11-06 Bug1 用户明确需求 | RSS 进首屏、分页时序连续 | app/db.py 三处排序点 | db/前端 | smoke G3 四条排序断言 | ✅ |
| R5 | RSS published_at 归一 GMT `YYYY-MM-DD HH:MM`（RFC2822 不可字符串排序） | 2026-11-06 Bug1 配套确认 | 库内无 RFC2822 原串 | app/rssfetch.py _norm_time | rssfetch/db | smoke G3 published_at 格式断言 | ✅ |
| R6 | 推送四通道（企微/钉钉/PushPlus/WxPusher）多实例；实例 filter_rule 绑定命名规则（**替代**全局，非叠加）；解绑回退默认 | 2026-11-05 v9 + v9.7 用户明确需求 | 同一情报按各实例规则独立判定；删规则自动解绑 | app/engine.py dispatch/_enabled_channels、api/routes.py 实例端点 | engine/notify/routes | smoke G6、代码实读（本盘点关联验证） | ✅ |
| R7 | 推送过滤六维（等级/分类/国家/行业/标签/关键词）+ **且/或组合模式**（默认且）；维度内多值恒为或 | 2026-11-07 用户明确需求（"需要或的关系"） | 且=全部满足才推；或=任一非空维度满足即推；空维度不参与 | app/engine.py pass_filter、api/routes.py、前端 pf-panel | engine/routes/前端 | smoke G6 九条 pass_filter 断言 | ✅ |
| R8 | 白名单输入为标记式控件（facets 常用值下拉 + 自定义回车 + chips 可删去重） | 2026-11-07 用户明确需求 | 下拉选值/自定义输入均正确序列化为顿号串 | web/index.html tokenFieldHtml/bindTokenField | 前端/routes | 浏览器实测（下拉/自定义/去重/删除/保存回读） | ✅ |
| R9 | 企微图片直传（markdown 后紧跟 image 消息 base64 直传）+ 45009 退避（15s/30s×3）+ 4s 发送节奏 | 2026-11-01 用户实测需求；v9.1 修复 | 图片在群里直接显示；限频自动恢复 | app/notify/channels.py WeComChannel | notify/engine | smoke G6 企微真实推送；push_logs | ✅ |
| R10 | 凭据掩码不回写：编辑表单留空=不变；含 ****/… 的值一律丢弃 | v9.1/v9.3 两次修复（用户反复反馈） | 留空提交不变；粘贴完整新地址即换 | api/routes.py api_channel_update clean 过滤、前端 instFormHtml | routes/前端 | **B3 回归六步实测全 PASS（本轮）** | ✅ |
| R11 | 报告任务六状态机（待写→写作中→已交付待推→已推送；待人工/已取消）；原子领取；系统复跑配置的门禁脚本，不信任 Agent 自报 | 2026-11-05 v8 交接文档 | 并发 claim 409；gate≠0 → 待人工；写作中不可删 | app/reports.py、api/routes.py 报告端点 | reports/engine/db | smoke G7 八条 | ✅ |
| R12 | 首页=系统信息汇聚（长期约定：每加功能同步首页展示卡） | 2026-11-05 用户明确约定 | 新功能必同步首页；首页不跟随其它视图显示 | web/index.html home-grid/loadHome | 前端/routes | healthcheck 页面断言；浏览器实测 | ✅ |
| R13 | 推送门控：AI 实时开启时仅 ai_status=ok 可推送（error 且 retries≥2 附说明放行） | 2026-11-01 v6.2 确认 | 未研判推文不推送 | app/engine.py _pushable_by_ai | engine | 代码实读 + ai error 零积压佐证 | 🟡（逻辑确认，场景级端到端未单独测） |
| R14 | 混合分类：rule_classify 关键词优先（零成本）+ AI 批量兜底；AI 研判顺带 intel_category | 2026-11-01 v6.2 确认 | 新推文 100% 有分类 | app/ai.py rule_classify、app/engine.py | ai/engine | 本盘点实测：分类覆盖 100%，规则兜底 0 触发（AI 先行） | ✅ |
| R15 | 数据清理 keep_days（1/2/3/7/30），starred 永久保留 | 2026-11-01 v6 确认 | 清理后收藏保留；计数返回 | app/db.py cleanup_old | db/前端 | 代码实读；**未在真实库执行**（破坏性） | 🟡 |
| R16 | AI 模型列表拉取（/models → datalist 下拉，仍可手输） | 2026-11-05 v8 确认 | 保存配置后拉取；失败保留手输 | app/ai.py list_models、前端 ai-fetch-models | ai/前端 | smoke G5 模型列表断言 | ✅ |
| R17 | facets 计数随筛选上下文联动（各维度排除自身条件）；选中项 0 计数保留显示 | 2026-11-07 Bug 修复确认（"显示 36 实际 0"） | 下拉数字=选它能得到的真实条数 | app/db.py facets、api/routes.py、前端 loadFacets | db/routes/前端 | smoke G8 七条、浏览器场景实测 | ✅ |
| R18 | 关键词标签 ≤10 个/条；英文缩写译成「英文（中文说明）」；入库时 AI 漏判由强地理规则兜底（F7） | 2026-11-06/07 用户明确需求 | 新推文自动打标；标签精确匹配筛选 | app/ai.py analyze_tweet、app/geo_rules.py | ai/geo_rules | smoke G5 兜底双断言（可控替身） | ✅ |
| R19 | 日志审计三页签（抓取/推送/AI 日志，AI 日志留存 500 条） | 2026-11-05 v9.2 确认 | 三类日志可查、AI 失败红色可见 | app/db.py ai_logs、前端 logs-panel | db/前端 | smoke G5 AI 日志落库断言 | ✅ |
| R20 | 统计口径拆分：推送成功/模拟分列（KPI 显示"含模拟 N"） | 2026-11-07 本盘点 F3 修复（口径不清晰问题） | stats 提供 push_sim；KPI 区分真实/模拟 | app/db.py push_stats_today、前端 KPI | db/routes/前端 | 本轮实测 stats 三键 + 浏览器 KPI | ✅ |

## 第一批复核边界（2026-11-08 复核修正，第二批输入）

- **A4 候选饥饿仅缓解未根治**：cap 200→2000 仍存在截断；第二批以逐实例投递记录（NOT EXISTS 游标）根治。
- **A2 失败重试边界**：全局 pushed 在"A 成功、B 失败"时 B 仍永久失去资格；即时单推失败改为不标记后，存在对已成功实例的重复发送风险——第二批按实例投递闭合。
- **模拟隔离**：模拟发送仍会标记全局 pushed（消费真实资格）——第二批修复（验收场景：模拟后转真实，真实资格仍在）。
- **smoke 竞态加固的性质**：AI 积压"超 15 分钟"窗口与分类"10 分钟在途容差"是**有依据的时间窗不变量**（能证明：窗口内不误报、窗口外真实丢失会暴露），不能证明"第一次失败确为竞态"——根因标**待确认**（已缓解）。


## 第三批已实现（2026-11-08）

| ID | 需求 | 实现位置 | 验证 |
|----|------|---------|------|
| R21 | 来源故障隔离：X 未配置/全失败不阻断 RSS | engine.run_fetch（X 段独立 try-except） | tests/test_batch3 S-A |
| R22 | 采集位点后置：入库成功后才推进（中断不漏抓） | twitter.fetch_accounts（insert 后再 set_kv） | S-B（入库异常时位点未推进） |
| R23 | RSS 逐条扫描去重（首条 GUID 不变仍发现后续新增） | rssfetch.fetch_source（移除整源短路） | S-C（首条不变仍发现新增） |
| R24 | 稳定来源身份 source_id（改名不丢归属/不重复入库） | db 迁移 + rssfetch/twitter 注入 + 局部唯一索引 | S-D、实测 874/928 回填 |
| R25 | 原文完整保存（上限 10000；预览/模型输入独立裁剪）+ 内容哈希 | twitter.\_clean(10000) + db.insert_tweet(text_hash) | S-E |
| R26 | 配置实际生效：timeout/retries/first_fetch_limit/单源与全局周期 | twitter.fetch（\_timeout/\_retries/\_layer_cap）+ engine.effective_interval_minutes + scheduler.reschedule | S-F1~F4 |
| R27 | 严格 AI Schema 校验（{}、错误类型、越界枚举、缺必需字段） | ai.\_validate_result + AIResultError | S-G1~G5 |
| R28 | 修复/截断标注 ai_repair（不把语法可解析当完整研判） | ai.analyze_tweet（LAST_PARSE_META→ai_repair） | S-G5 |
| R29 | 批量分类单一身份（仅库内 id，序号不撞号） | ai.classify_batch | S-H1 |
| R30 | 持久后台 AI 任务（提交/查询/原子领取/租约/进度/分计） | db.ai_tasks + engine.process_ai_tasks + /api/ai/tasks | S-I1~I5 |
| R31 | 人工修订保护（ai_modified；自动重分析不覆盖） | db / ai.AIModifiedError / route /api/tweets/{id}/manual | S-J1~J2 |
| R32 | 规则变更后已过滤投递重新评估（不永久跳过） | db.reevaluate_filtered + PUT filters/rules 钩子 | T6-1~T6-3 |
| R33 | 独立发送尝试记录（开始/结束/结果/下次重试，历史保留） | delivery_attempts 表 | T1-1~T1-3 |
| R34 | owner 令牌：完成写回必须匹配；租约过期后旧工作者不可覆盖 | db.claim_msg/finish_msg/msg_retry_wait | T2-1~T2-3 |
| R35 | 报告分项补发（正文成功仅补失败附件） | reports.push_task（prev_items 分项跳过） | T3-1~T3-3 |
| R36 | 模拟/真实切换三态正确（不抹审计、不覆盖真实成功） | db.reset_sim_deliveries + finish_msg 终态保护 | T4-1~T4-6 |
| R37 | enabled_since 稳定（改名/保存/重启不刷新） | config.ensure_instance_ids | T5-1 |
| R38 | runtime.json 并发安全（完整读-改-写加锁 + 原子替换） | config.save_runtime（\_rt_lock + os.replace + 校验 + .bak） | T8-1 |
| R39 | 回退兼容标记语义明确（已过滤/模拟/永久/不明 → pushed=1；仅待发送不置） | db.sync_pushed_compat | T7-1~T7-2 |


## 第四批已实现（2026-11-08）

| ID | 需求 | 实现位置 | 验证 |
|----|------|---------|------|
| R40 | 统一测试环境入口（临时目录/禁外联/生产路径守卫） | tests/testenv.py | test_isolation 9/9 |
| R41 | 真正离线测试入口（CI 可用） | offline_test.py | 9 套件 202 断言 |
| R42 | 认证：会话 + API Token + read/write/admin | app/auth.py、routes 依赖 | test_batch4_auth 32/32 |
| R43 | CSRF（会话写请求）+ HttpOnly Cookie + 会话过期 | auth.require、login.html | auth A5/A6 |
| R44 | 首次初始化仅本机文件（禁止匿名远程） | auth.try_consume_init_file | auth A1、实机验证 |
| R45 | 路由权限分级（敏感修改需 write/admin） | 64 路由 dependencies | auth A3 全矩阵 |
| R46 | 文件路径限制（穿越/盘符/UNC/编码/符号链接） | app/safefetch.safe_join | security 7 断言 |
| R47 | SSRF 防护（私网/回环/链路本地/IPv6/重定向） | app/safefetch.validate_url/fetch_limited | security 13 断言 |
| R48 | 管理员显式内网例外（AI 网关） | validate_url(allow_private) | security |
| R49 | 备份：SQLite online backup + manifest + 哈希 | app/backup.create_backup | backup 15/15 |
| R50 | 恢复演练（隔离目录 + 完整性 + 计数校验） | app/backup.restore_to/verify_backup | backup 15/15 |
| R51 | 降级导出（未完成/不明任务） | backup.export_pending_deliveries | T7/backup |
| R52 | 离线 CI（Ubuntu+Windows，秘密守卫） | .github/workflows/ci.yml、scripts/check_no_secrets.py | 本地脚本通过；远端未运行 |

## 待解决限制（如实记录）

| ID | 限制 | 影响 | 状态 |
|----|------|------|------|
| L1 | `db.update_ai()` 无版本检查/条件更新 | 迟到 AI 结果可直接覆盖人工值（绕过引擎路径） | 待第五批或专项 |
| L2 | `ai_repair` 非空未强制降级下游 | 修复/截断结果仍可能被当完整结果推送 | 待确认业务规则 |
| L3 | 跨进程并发未验证（模块锁仅单进程） | 多 worker 部署需外部锁 | 单进程部署下无影响 |
| L4 | 远端 CI 未实际运行 | 仅本地脚本验证 | 推送后待观察 |
| L5 | 54 条历史 source_id 为空 | 统计归属缺失（不猜测） | 保留待处理 |
| L6 | 真实 AI/群发送未在本轮执行 | 认证后需 Token 才能调用 | 按授权边界 |

## 待确认/候选需求（不擅自实现）

| ID | 内容 | 背景 | 状态 |
|----|------|------|------|
| C1 | 跨通道推送去重改为 per-(tweet,instance) 粒度 | ✅ 第二批已实现：deliveries 表 UNIQUE(tweet_id,instance_id)，实例独立状态机（engine.dispatch_round） |
| C2 | 模拟发送不消费真实资格 | ✅ 第二批已实现：模拟=独立"模拟成功"记录；真实激活（配置凭据）时 reset_sim_deliveries 把模拟记录转回待发送 | 
| C3 | 发送失败补发 | ✅ 第二批已实现：失败→等待重试（退避 2/5/15/60 分钟，上限 4 次）→永久失败；结果不明（超时/租约过期）独立状态+人工重试 |
| C4 | cleanup 是否保护被报告任务引用的推文 | ✅ 第二批已实现：cleanup/批量删除统一豁免 收藏+report_refs 引用+在途投递成员（db.protected_tweet_ids） |
| C5 | 企微群1 实例遗留 `filters` 垃圾字符串字段 | v9.7 改版前残留，引擎不读取，无功能影响；可在下次触碰该配置时顺手清理 | 💭 低优先级清理候选 |
| C6 | 推文流标签/单位 chip 点击筛选（已实现）扩展到 org chip | org chip 当前不可点击；若有需要再加 | 💭 待确认想法 |
