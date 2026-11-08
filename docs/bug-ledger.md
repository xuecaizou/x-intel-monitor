# X-Intel 历史 Bug 复验台账

> 建立时间：2026-11-07。来源：项目记忆库历史记录 + 本会话用户反馈。
> 状态取值：待复现 / 已复现 / 原因待确认 / 已修改待验证 / **已验证通过** / 受阻。
> 原则：只有依据明确预期行为完成验证才标"已验证通过"；验证环境（本机 8083 实测）与生产环境为同一环境（单机部署），已注明。

| Bug ID | 问题 | 来源 | 复现条件 | 预期行为 | 实际行为 | 历次修复 | 当前状态 | 验证证据（2026-11-07） |
|--------|------|------|---------|---------|---------|---------|---------|----------------------|
| B1 | AI 研判 JSONDecodeError（思维链模型把思维链写进 content / 截断） | 用户反馈（推文流报错提示） | 思维链模型 hy4-preview + response_format=json_object | 研判成功落库，失败有重试兜底 | 修复前 0 成功 38 败 | 四层 _chat 兼容 + 切 cbcn/deepseek-v4.1-flash | **已验证通过** | /api/ai/test ok；今日 19 调用 0 失败；推文 error 积压 0 |
| B2 | 企微 45009 限频导致推送失败 | 用户反馈（收不到推送） | 0.3s 间隔连发 markdown+图片双消息 | 限频自动退避，最终成功 | 修复前 12 次失败 | _post 退避 15s/30s×3 + _pace 4s + engine 4.5s/条 | **已验证通过** | 全部 12 次 45009 均发生于 2026-11-05 修复前；修复后 6 天零复发 |
| B3 | 凭据掩码被当真值写回（编辑后 webhook 失效） | 用户反复反馈两次（v9.1、v9.3） | 编辑实例表单预填掩码 → 提交 | 留空=不变；粘贴完整新地址=更换 | 修复前 webhook 变成掩码串 | _mask 统一 host+"/****" + PUT 过滤 ****/… + 表单留空模式 | **已验证通过（本轮六步回归）** | API 往返：留空不变 ✓、**** 丢弃 ✓、… 丢弃 ✓、完整新地址更换 ✓（runtime.json 直核）；首轮 FAIL 是测试断言读了掩码回显（教训已记） |
| B4 | webhook"填入了不生效" | 用户反馈（2026-11-07） | 新建实例「123456」填 webhook | 推送到新群 | 企微返回 errcode 93000 invalid webhook url | 非系统缺陷（保存/读取/发送链路全部正常，同刻另一实例 errcode=0） | **已验证通过（根因在企微侧）** | 结构比对（host/path/key 长度/字符集一致）+ 日志复现 + 系统侧新增 93000/45009 中文处置提示 |
| B5 | RSS 条目"抓到但看不见"（CAST(tweet_id) 排序恒 0） | 用户明确需求单 | RSS 入库后列表沉底 | RSS 按时间进首屏、分页连续 | 修复前 RSS 排 801-829/830 | 三处排序点统一时间键 + _norm_time 归一 | **已验证通过** | smoke G3 四条排序断言 + RSS 首屏 4/5/7/28 位实测 |
| B6 | 企业总部被当受害国（Apple 警告德国议员→美国 等） | 用户明确需求单（Bug2） | 企业名关键词命中 | 按受害方/强地理判定 | 修复前 40 条系统性误标 | app/geo_rules.py 三层重构 + 存量审计修正 68 条 | **已验证通过** | 抽查 #3242=德国、#3139=朝鲜、#3248=中国、#3130=全球 全 PASS |
| B7 | AI 研判漏写 geo/org/industry 字段（入库恒空） | 盘点发现（Bug2 顺带） | 2026-11-06 前所有入库推文 | 研判即写地理/单位/行业 | 修复前字段被丢弃 | analyze_tweet 补映射 + _geo_text 归一 | **已验证通过** | 新推文（#3957-3960）geo/org 齐全；#3956 本轮修复后补判（见 F7） |
| B8 | 分类筛选"显示 36 实际 0 条" | 用户反馈（2026-11-07） | 分类下拉计数为静态全量数，与其它筛选交集为 0 | 下拉数字=选它能得到的真实条数 | 修复前数字撒谎 | facets 动态联动（排除自身维度）+ 0 计数保留 + 空结果提示区分 | **已验证通过** | smoke G8 七条 + 浏览器三场景（36→0 如实显示→放宽恢复） |
| B9 | 标签筛选计数与实际不符（LIKE 子串 vs 精确计数） | 盘点发现（B8 同族） | 标签含包含关系时计数偏差 | 筛选与计数同语义 | 修复前 facet 21 vs 命中 23 | tag 筛选改精确匹配 | **已验证通过** | smoke G8 交叉计数断言 + 组合 3=3 实测 |
| B10 | 侧栏收起/展开按钮"不生效" | 用户反馈（v8.1，两次） | 点击 .sb-top 区域 | 宽度 48↔220 切换并持久化 | v8.1 曾修复（hover 强制展开根因）；本轮复验 | #sb-toggle-btn 绑定 + body.sb-expanded + localStorage | **已验证通过** | 浏览器实测：点击切换 true→false→true、localStorage 更新、无双绑定（注意：状态类在 body 上，探针曾点错 .sb-top 容器） |
| B11 | X 采集未走代理（DEFAULTS 缺 proxy 键） | 盘点记录（2026-11-01 致命坑） | 只读 raw runtime.json 判断 | x.proxy=socks5://127.0.0.1:10808 生效 | raw 文件 x 块无 proxy 键 | DEFAULTS 提供 x.proxy + twitter 回退 network.proxy | **已验证通过（非回归）** | app.config.load() 合并结果 x.proxy/network.proxy 均为 socks5://127.0.0.1:10808；fetch_logs nitter✓ 正常 |
| B12 | 菜单按钮无效/视图切换 | 用户反馈（v8.1） | 点击侧栏菜单 | 视图切换 + localStorage 记忆 | 修复：视图切换制 | showView + data-view 全量匹配 | **已验证通过** | 浏览器实测：AI 设置/监控源/首页切换 + xintel_view_cur 持久化 |
| B13 | AI 研判失败重试超时（20 条/批超 HTTP 时窗） | 用户反馈 | 批量 AI ≤20 条同步 | 批量可用 | 修复：5 条/批 | 文档与提示 | 🟡 已修复未单独复验（本轮未触发该场景；smoke G5 单条链路通过） |
| B14 | 跨通道 pushed 标记粒度（全局 per-tweet） | 本盘点关联核查（prompt 明确列为检查项） | 实例 A 发过 → pushed=1 → 之后新启用实例不补发历史；同轮内所有启用实例正常收到 | 待业务确认（需求证据不足） | 现行为如实描述 | 未改动 | ⚠ **待确认决策 C1**（详见表下说明） |
| B15 | 模拟发送占用 pushed 标记 | 本盘点关联核查（prompt 明确列为检查项） | 未配置凭据的实例"模拟发送"后推文标记 pushed=1 | 待业务确认（与 B14 同一决策） | 现行为如实描述 | 未改动 | ⚠ **待确认决策 C2** |
| B16 | 发送失败批次无补发（标记 pushed 后不重试） | 本盘点关联核查 | 实例发送失败 → pushed=1 → 该批永久丢失 | 待业务确认 | 现行为如实描述 | 未改动 | ⚠ **待确认决策 C3** |
| B17 | cleanup 不保护报告任务引用的推文 | 本盘点关联核查 | cleanup 后 report_tasks.tweet_ids 悬空 | 线索包为创建时快照不受影响；原始推文引用失效 | 现行为如实描述 | 未改动 | ⚠ **待确认决策 C4** |

## B14/B15/B16 说明（同一设计决策，建议一起决定）

`dispatch()` 的 `pushed` 标记是**推文级全局标志**（tweets.pushed=1）。当前语义：
- 同一轮内，所有启用实例都会收到同一条推文（各实例按各自规则独立过滤、独立记录日志）——**无串用、无误跳**；
- 该轮结束后统一 `mark_pushed`，无论该实例成功/失败/模拟；
- 后续轮次的 `tweets_pending_push` 只取 pushed=0 —— 因此：新启用实例收不到历史、失败实例不重试、模拟发送"消费"了推文。

这三个行为是同一标记设计的三个侧面。修复需要 per-(tweet, instance) 发送记录表（schema 变更 + dispatch 逻辑重写），属业务语义变更，按工作原则（业务行为变化必须有依据）列为决策项，不在结构整理中悄悄改。

**建议选项**：
1. 维持现状（明确记录语义）——最稳，接受"后启用不补发/失败不重试/模拟即消费"。
2. per-(tweet,instance) 记录表 + 失败重推 + 模拟不消费——功能最完整，改动最大。
3. 折中：仅增加"失败重推"（失败实例的批次进入下轮 pending），模拟保持消费语义。

## 探针误差记录（避免误判为系统 Bug）

- B11：读 raw runtime.json 判断 proxy 丢失 → 实际 load() 深合并后正常。**验证配置必须用 app.config.load()**。
- B10：探针点击 `.sb-top` 容器（无绑定）→ 实际按钮是 `#sb-toggle-btn`。
- B3 首轮：断言读 GET /config 掩码回显 → 掩码恒以 **** 结尾，无法反映存储值，须直核 runtime.json。


## 第一批结论修正与第二批台账（2026-11-08）

### 第一批结论修正
- **A4**：cap 200→2000 属**缓解**（仍可饥饿），第二批以逐实例投递游标根治。
- **A2**：摘要遗漏已修复；但"A 成功、B 失败只重试 B"未闭合（全局 pushed 耦合），即时失败不标记带来对已成功实例的重复发送风险——均为第二批范围。
- **模拟消费**：模拟发送仍标记全局 pushed（ consumes 真实资格）——第二批修复项 B15。
- **冒烟竞态**：两次断言加固属时间窗不变量；第一次失败的根因**待确认**（无法复现并发证据）。

### 第二批问题台账
| ID | 问题 | 优先级 | 证据状态 | 位置 |
|----|------|--------|---------|------|
| B14 | 跨通道 pushed 全局耦合 | 高 | 已复现 | engine.dispatch |
| B15 | 模拟发送消费真实投递资格 | 高 | 静态确认 | engine.dispatch |
| B16 | 实例发送失败无补发 | 高 | 静态确认 | engine.dispatch |
| B18 | 实例无稳定 ID（数组下标/改名破坏身份） | 高 | 静态确认（runtime instances 无 iid） | config/runtime |
| B19 | app/wecom_media.py 缺失，附件推送必然 ModuleNotFoundError 且被 +FAIL 掩盖 | 高 | 静态确认（reports.py import 在 try 内） | app/reports.py:294 |
| B20 | 报告全部失败/仅模拟状态语义（第一批已修响应，库内部分分项与并发领取未修） | 高 | 部分修复 | reports.push_task |
| B21 | cleanup/批量删除不保护报告引用与在途投递 | 高 | 静态确认 | db.cleanup_old/batch_tweet_op |
| B22 | 报告推送无原子领取（并发双推） | 中 | 静态确认 | reports.push_task |
| B23 | 待人工无退回修改/重新交付路径 | 中 | 静态确认（deliver 仅写作中） | reports.deliver |
| B24 | runtime.json 写入非原子、无并发保护/写后校验 | 中 | 静态确认（直接 write_text） | config.save_runtime |
| B25 | 外部 URL 拼接进 onclick（index.html 媒体/原文 onclick） | 中 | 静态确认（待验证可利用性） | web/index.html |

### 第二批修复结论（2026-11-08）
- **B14/B15/B16 已闭合**：逐实例投递模型（deliveries/delivery_msgs/delivery_items/delivery_attempts 语义）替代全局 pushed 调度职责；tweets.pushed 仅兼容回写（任一终态→1，供回退旧代码防重发）。验收：tests/test_batch2.py 39 断言（S1-S16）全绿，含"A 成功 B 失败只重试 B""模拟转真实资格仍在""迁移幂等不伪造历史""租约过期→结果不明→人工重试"。
- **B19 已闭合**：app/wecom_media.py 补齐（两步法+20MB 校验）；附件模块缺失/单附件失败→分项失败，状态=部分成功。
- **B21 已闭合**：cleanup/批量删除统一豁免 收藏+report_refs+在途投递（db.protected_tweet_ids）。
- **B22 已闭合**：push_task 原子领取（推送中+10 分钟陈旧回收）。
- **B23 已闭合**：待人工→重新交付（重跑门禁）/abort 退回修改。
- **B24 已闭合**：runtime.json 原子写（tmp→os.replace）+写后校验+.bak 备份+模块锁。
- **B18 已闭合**：实例 iid 稳定身份（迁移实测 3 实例赋码；改名/排序不变、删除重建新码——S6 断言）。
- 修复前证据：test_batch2 首轮跑出 7 项 FAIL（候选饥饿/模拟消费/身份缺失/分项缺失等），修复后 39/39。

### 第三批问题与修复（2026-11-08）

| ID | 问题 | 证据状态 | 修复 | 验证 |
|----|------|---------|------|------|
| B26 | 发送尝试记录缺失（重试覆盖历史） | 已复现 | delivery_attempts 表（尝试/结果/下次重试） | T1-1~T1-3 |
| B27 | 领取无 owner 令牌，旧工作者可覆盖新状态 | 已复现 | claim 返回 token；finish/retry 校验（租约有效时才强制） | T2-1~T2-3 |
| B28 | 报告补发仅按实例跳过（正文成功仍重发） | 已复现 | prev_items 分项粒度跳过 | T3-1~T3-3 |
| B29 | 模拟转真实不恢复真实资格 | 已复现 | reset_sim_deliveries + 终态保护（不覆盖真实成功） | T4-1~T4-6 |
| B30 | 已过滤记录永不重评估（规则变更后仍跳过） | 已复现 | reevaluate_filtered + 保存钩子 | T6-1~T6-3 |
| B31 | X 全部失败阻断 RSS | 静态确认→隔离复现 | X 段独立 try-except | S-A |
| B32 | 位点先于入库推进（中断漏抓） | 静态确认 | 入库成功后才 set_kv | S-B |
| B33 | RSS 首条 GUID 未变整源跳过（漏新增） | 静态确认→隔离复现 | 逐条扫描去重 | S-C |
| B34 | 无稳定 source_id（改名破坏归属/重复） | 静态确认 | source_id + 局部唯一索引 + 迁移回填 | S-D、实测 874/928 |
| B35 | 原文截断 400 字 | 静态确认 | \_clean 上限 10000 + text_hash | S-E1~E2 |
| B36 | fetch.timeout_seconds/retries 可配置但不读取 | 静态确认 | fetch 注入 \_timeout/\_retries | S-F2 |
| B37 | first_fetch_limit 被 max_items 静默压制 | 静态确认 | \_layer_cap = max(max_items, first_fetch_limit) | S-F3 |
| B38 | 来源 5 分钟频率受 15 分钟主调度限制 | 静态确认 | effective_interval_minutes + scheduler.reschedule | S-F4 |
| B39 | AI 结果 {} / 错误类型 / 越界枚举静默成功 | 静态确认→隔离复现 | \_validate_result 严格校验 + AIResultError | S-G1~G5 |
| B40 | JSON 修复/截断无标注 | 静态确认 | ai_repair 字段 | S-G5 |
| B41 | 批量分类序号与 id 撞号会写错情报 | 静态确认 | 仅库内 id + 重复忽略 | S-H1 |
| B42 | 长时间批量 AI 占用同步请求 | 设计缺陷 | ai_tasks 持久任务（提交/轮询/领取/租约/分计） | S-I1~I5 |
| B43 | 人工修订被自动重分析覆盖 | 静态确认 | ai_modified + AIModifiedError + /manual 端点 | S-J1~J2 |
| B44 | org 已是数组仍二次 JSON.parse（单位不显示） | 静态确认 | 前端 Array.isArray 优先 | S-K1 |

### 第二批边界复核结论（本轮）
- T1/T2 新增：尝试记录 + 令牌（此前汇报未实现，本轮补齐）。
- T3 修正：报告补发从"实例粒度"细化到"分项粒度"。
- T4 覆盖：模拟→真实→模拟→真实、切换时发送中（不再被模拟掩盖）。
- T5 确认通过：enabled_since 不因改名/保存/重启刷新。
- T6 修复：已过滤记录随规则变更重新评估。
- T7 明确：回退兼容标记含 已过滤/模拟/永久/不明；仅待发送不置（旧代码可接管补发）；**降级限制：旧版本不具备逐实例语义，A 成功 B 待重试时回退，B 会依赖 pushed=0 被旧代码重发——这是旧代码的近似行为，不是无损降级**。
- T8 修复：runtime.json 完整读改写入锁 + 原子替换。
- 尚未验证：跨进程并发（模块锁仅单进程有效，已记录适用范围）。

### 第四批问题与修复（2026-11-08）

| ID | 问题 | 证据状态 | 修复 | 验证 |
|----|------|---------|------|------|
| B45 | 测试写入生产 runtime.json（T8 事故） | 已发生 | TestEnv 统一隔离 + 生产路径守卫 | isolation 9/9 |
| B46 | smoke 真实调用 AI 但被描述为离线 | 已实测（ai_logs 有 测试/研判 记录） | 文档更正 + offline_test 入口 | 实机日志 |
| B47 | **AI 后台任务候选集永远为空**（ai_modified=0 对 TEXT 空串） | 已复现 | `ai_task_items` 兼容 NULL/''/'0' | test_batch4_ai_task 15/15 |
| B48 | process_ai_tasks 忽略传入 cfg | 已复现 | 改用传入 cfg | A5 |
| B49 | T7 回退结论错误（称 B 会被重发） | 已实测更正 | 矩阵测试 + 降级流程 | T7 12/12 |
| B50 | manifest 未登记自身/恢复找不到 | 已复现 | manifest 自登记 + 校验跳过 | backup 15/15 |
| B51 | 未启用 WAL，备份无法覆盖未检查点数据 | 静态确认 | `PRAGMA journal_mode=WAL` | backup W1 |
| B52 | 路径编码变体未拦截 | 已复现 | unquote 后校验 | security |
| B53 | healthcheck/smoke 无认证支持 | 已复现（401） | XINTEL_TOKEN 支持 | 40/50 实机通过 |

### 最终收尾批次问题与修复（2026-11-08）

| ID | 问题 | 证据状态 | 修复 | 验证 |
|----|------|---------|------|------|
| B56 | 事件时间刷新函数重复加锁 → 死锁 | 已复现（冒烟超时） | 明确持锁约定 | 事件测试全绿 |
| B57 | event_evidence 查询漏选 ai_iocs/org/tags → 导出 IOC 为空 | 已复现 | SELECT 补全 | E6-7 |
| B58 | 导出快照无条件 BEGIN → 嵌套事务 | 静态确认 | 检查 in_transaction | 导出测试 |
| B59 | 备份校验未覆盖事件/认证表 | 已复现 | 补 7 表 | 恢复演练 24/24 |
| B60 | AI 429/额度耗尽被当普通错误反复重试 | 生产日志实测 | AIQuotaError + 中止本轮 | 代码审查 + 下次配额恢复可验 |

### 第七批：集中修复（2026-11-09，基线 b4c720d，分支 fix/central-repair）

> 来源：对新机器独立基线的源码与隔离复核（任务书固定范围 A–J）。
> 原则：先建立能失败的反例，再修复；替身位于 HTTP/模型边界，业务函数全真。

| ID | 问题 | 根因 | 修复 | 验证 |
|----|------|------|------|------|
| A | CI 绿标掩盖真实失败：run 42482898535 两平台各 1 FAIL 仍 success | offline_test.py 收集 failed 后**没有非零退出**（文件在循环后直接结束）；缺失套件被静默 SKIP | 入口改为 run_suites()+main()：失败/缺失/异常退出→exit 1，套件级汇总；新增 tests/test_entry_exit.py 端到端固化退出码契约 | E1–E4（单套件失败/全失败/缺失/全绿 四场景退出码）；两平台 FAIL 修复见 A-S14/A-T8 |
| A-S14 | S14-1 待人工可重新交付 FAIL（两平台） | _run_gate 硬编码 ["py","-3.13"]——Ubuntu 无 py、Windows CI 无 3.13 注册 → 门禁恒 999 → 交付永远落回"待人工" | 门禁解释器默认 sys.executable（report.gate_python 可配置） | batch2 39/39；三平台本机/CI 均绿 |
| A-T8 | T8-1 并发保存 WinError 5（Windows） | os.replace 目标被 AV/索引服务瞬时占用，无重试；共享 .json.tmp | 唯一临时名（pid+tid）+ 有界重试（6×递增延时）；.bak 写入重试 | batch3 49/49（本机 Windows 复现→修复） |
| B | AI 写入绕过版本保护；后台路径"先写库再检查冲突"；429 被吞继续烧钱 | process_tweet 直接 update_ai（process_ai_tasks 双写形同虚设）；失败写回无保护；通用异常吞掉 AIQuotaError；done 统计把模型失败计成功 | process_tweet 纯计算 (fields,error,quota_hit)；新增 db.update_ai_guarded（失败写回权威路径，版本+人工保护原子检查、留审计、不推版本）；全部 AI 链路 429 即停本轮、剩余不计成功；WHERE 兼容 ai_modified TEXT 空串（B47 同源坑） | test_batch7_b 33 断言：迟到成功/迟到失败均不覆盖人工值且留审计、429 整轮仅 1 次 HTTP、落库失败不重复调用、classify done 计数修正 |
| C | effective_grade 只看 ai_review=manual，未验证版本——旧审核冒充当前批准；审核绑定无原子检查；三态混于单一字段 | 版本绑定字段存在但读取端未校验；update_ai 直接写 | integrity.review_state 三态（valid=绑定当前版本/stale=历史决定/none）；effective_grade 仅 valid 升级；structural_grade 保留原始分级（partial/invalid 不被抹掉）；db.bind_review 原子绑定（版本不符拒绝）；/api/tweets/{tid}/review 竞争→409 | test_batch7_c 20 断言：绑定/竞争409/版本推进失效/历史数据保守 stale/导出三块（integrity/structural_integrity/review{state,reviewed_version,current_version}）；L2-9 夹具按新契约修订 |
| D | 关联推文 4091/4092 显示证据 ID 1/2；解除无效、拆分建空事件、导出引用错误 | event_evidence 用 v.*——关联表行 id 以 "id" 键泄漏、created_at 同名歧义；前端/导出误用；split 先建事件后校验 | 全列唯一别名（evidence_id/tweet_id/collected_at/linked_at，补 ai_review_version）；split_event 单事务：先校验真实 tweet_id，零有效成员不建空事件（API→400）；解除/拆分前端按响应诚实反馈；导出 tweet_id+evidence_id 双表达 | test_batch7_d 24 断言（夹具垫底 20 条使 tweet_id=4091/4092 与证据行 id=1/2 明显不同）；test_batch4_ai_task A5 mock 适配新签名 |
| E | 底层情报改变事件版本/审核不变；两个同 expected_version 并发修改均成功；导出与写交错 cannot commit | update_event 乐观锁检查在锁外、UPDATE 无版本条件；无底层情报变化检测；共享连接 BEGIN 被写入隐式回收 | 事件证据指纹 evidence_fingerprint（成员+analysis_version SHA1）+ ensure_event_freshness（读路径检测→推进版本+留痕，幂等）；review_event 锁内复验+绑定指纹；event_review_state 统一 valid/stale/none；update_event/add/remove/split/merge 单事务（_bump_event_locked）；导出改**独立只读连接**（WAL 快照） | test_batch7_e 25 断言：4线程同版本竞争仅1胜、审计同事务、指纹驱动审核失效与恢复、写入期间 6 轮导出零异常、拆分双向版本/指纹一致 |
| F | IOC 默认 role=attack；组织名子串判 victim；verified 由结构完整性推导；全局去重丢源；invalid 混入攻击分组 | _classify_ioc 默认 attack + org 子串归类；is_verified 掺入人工语义 | role 缺证据默认 unknown（绝不默认 attack）；五桶 cve/attack/victim/reference/invalid（invalid 审计保留带标注）；org 重合只留 hints.org_overlap；verified=人工审核绑定当前版本（review_state）；去重按 (value,tweet_id) 保留全部来源+source_count+role_conflict；CSV 新列与 JSON 一致 | test_batch7_f 18 断言（含短名 ACME 历史误判场景复现） |
| G | restore_to 原样复制配置——恢复后调度/外发仍 true；旧 Token/会话悄悄复活 | 安全语义只写在文档未落实代码 | restore_to safe_mode（默认）：恢复副本强制 scheduler=false+outbound=false（原件不动）；safety_changes 区分原件哈希与恢复变更；Token/会话一律清除（auth_reset 摘要）；verify_original/integrity_final 分离 | test_batch7_g 17 断言——**隔离目录真实启动恢复实例**：healthz 200、备份前 Token 被运行实例 401、启动日志"调度已按配置暂停"、fetch_logs/push_logs 空、backup API 含最近提交数据；恢复演练测试适配新契约 30/30 |
| H | 来源测试裸走 _http_get（SSRF 探针）；RSS/媒体默认自动重定向+整包读取；报告交付接受任意绝对目录；附件 webhook 无校验 | safefetch 辅助函数存在但业务链路未接入 | fetch_limited 扩展 purpose/proxies；RSS 抓取+来源测试+api_media 全部受控（逐跳校验/≤8MB/显式超时）；deliver 与附件路径经 safe_report_path（非法路径→待人工+说明）；webhook 校验（https+公网，内网需显式 allow_private.webhook）；媒体代理逐跳复核 twimg 白名单 | test_batch7_h 22 断言（业务入口拒绝行为、零 HTTP 调用断言、302→内网第二跳未发出）；S13/S14/T3 夹具迁受控目录 |
| I | 首条 GUID 未变整源跳过（漏采）——B33 声称已修但代码未生效；S-D1 夹具 WHERE id=2 空洞通过；RSS 缺 source_id 改名重复入库 | 增量短路逻辑仍在；夹具硬编码 id | 逐条扫描去重（UNIQUE(source_id,tweet_id)）；条目带 source_id=rss:{id}；位点 rss_top 仅入库完成后推进（异常不推进）且只作追踪；S-D1 夹具改真实 sid | test_batch7_i 14 断言：固定反例（首条不变+第二条新增——旧实现必失败）、置顶场景、失败不推进+重跑补齐不重复、整轮幂等、改名归属完整 |
| J | 3X+1RSS 首页显示 3/3；全模拟推送显示 100% 真实成功率；启用但未运行的源默认正常 | 前端只读 sources.x.total；push_stats_today ok 含模拟；x_status 默认 True | _sources_health 统一口径：total/enabled=X+RSS、逐源四态（ok/error/unknown/disabled）、聚合 health.state（X 成功不掩盖 RSS 失败）；push_stats_today 真实/模拟分列（real_rate 无真实样本→None）；前端 KPI/圆点/副标题/配置页计数同步 | test_batch7_j 22 断言（4/4+partial_error 固定场景、全异常 error、仅模拟 real_rate=None、API 与内部一致）；浏览器实证（隔离实例真实数据 5/5·部分异常·无真实样本） |

**浏览器验收（2026-11-09，隔离实例 127.0.0.1:18200，回环监听）**：
登录（会话+CSRF）→首页（5/5 统一口径·部分异常·无真实样本）→监控源页（X4+RSS1 计数一致）
→事件详情（证据表显示推文ID 4018/4019/4020）→关联 4020（snack"证据已关联"，v2）
→解除 4019（confirm 对话框、"已解除"，v3）→拆分 4020（prompt 对话框、"已拆分"，双向版本推进）
→审核 approved@v4 →人工修订底层情报→**evidence_changed v5（指纹留痕）**→前端显示
"历史决定（v4）：approved——需重新审核"→重新审核 approved@v5→导出核对
（tweet_id/evidence_id 双表达、collected_at 唯一键、IOC role=unknown、verified 不由完整性推导、CSV 新列齐）。

**测试规模**：22 套件 514 PASS / 0 FAIL（本机 Windows 全量）；入口 exit 0 正确。
