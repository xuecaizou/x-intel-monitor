# x-intel-monitor：第二轮独立复核与第三轮修复任务

复核日期：2026-11-16（用户时区 Asia/Shanghai）  
项目：https://github.com/xuecaizou/x-intel-monitor  
复核提交：c0068af0bf0a1c1c6cfcd4209fcab1e1c0cdf82a  
第二轮基线：3407c448787b68cefd9b42a5b8d5bc36e4e5d96d

## 一、验收结论

**第二轮有实际改进，但当前提交未达到“六项需求和 B01–B22 全部完成”的验收条件。需要继续整改，不能按原报告直接关闭问题。**

本次拉取的 origin/main 与报告 SHA 一致。审查只读仓库，所有动态验证在临时数据库、测试账号、合成资料和本机服务中完成；没有修改项目代码、推送提交或操作生产数据。

关键事实：

- 完整离线入口实跑：29 套件中 28 通过、1 失败，739 PASS / 1 FAIL，exit 1，用时约 219 秒。
- 失败发生于 test_round2_collab_security 的 B06，真实请求出现 HTTP 500；堆栈为 sqlite3.InterfaceError: bad parameter or other API misuse。
- 单独执行该安全套件曾得到 31 PASS，证明失败并非每次出现；后一次通过不能覆盖已捕获的失败。
- 本次 Word 测试安装并实际使用 Pandoc，工作台套件为 80 PASS / 0 FAIL / SKIP 0；仍有独立复现的 Word 缺陷，说明已有用例覆盖不足。
- 选区 AI 的真实接口直接 HTTP 500；多通道的旧翻译入口仍请求旧配置；多个报告标“已修”的缺陷仍能复现。
- 7de42b1 的提交说明写有 B08/B11/B12 修复，但实际只修改 app/backup.py 与 tests/test_round2_rss.py。最终 wordexport.py 相对基线仅增加 Pandoc PATH 探测和未实际改变正文选择的 review_rev 赋值。

证据包中的脚本与结果是审查复现材料，不应直接当成完整 CI 套件。开发者需将明确场景转为稳定回归测试。

### 实测边界

1. 真实 HTTP / WS 验证使用正常鉴权，关闭认证 bypass。没有使用真实用户凭据。
2. LLM 调用使用本机受控端点或固定模型输出替身；没有真实厂商模型联调。
3. 部分并发使用受控交错，持久化使用故障注入。
4. ACK、通道页面和版本弹窗执行了原始 JavaScript 逻辑，DOM/传输使用替身；这不是完整浏览器验收。
5. 本次浏览器连接尝试超时，未完成真实浏览器交互、截图和双浏览器验收。
6. Word 做了真实 Pandoc 转换、API 下载和 ZIP/OOXML 内容检查；本轮未进行 Microsoft Word/WPS/LibreOffice 视觉打开或渲染验收。
7. RSS 推送回归使用模拟发送，实际外发次数为 0；用户部署的 THN 实际配置与日志仍未获取。

## 二、已经确认有效的修复

| 项目 | 独立验证结果 |
| --- | --- |
| B01 跨文档票据 | A 的票据连接 B 被拒绝。 |
| B02 Agent 范围 | 越界文档 REST 403，WebSocket 也拒绝。 |
| B03 会话删除子场景 | 在应用正确事件循环执行清理后，旧会话连接关闭。全局降级及旧票据仍有缺陷，见 R08。 |
| B04 一次持久化故障 | FIFO 补写后 pending=0；live 与数据库重放均为 BASE FIRST SECOND。 |
| B10 素材接口 | 上传、列表、下载均可通过真实鉴权 API 返回 200。 |
| B09 文件复制部分 | doc_assets、word_exports 已进入备份及恢复目录；路径迁移见 R16。 |
| B07 普通编辑部分 | Agent 编辑后状态能回落编辑中，并可重新送审；恢复历史与默认导出仍有缺陷。 |
| 候选基本流程 | Row 转 dict、用户提示词进入模型、推荐结果持久化、情报集合去重均有代码和执行进展；完整流程仍见 R20–R24。 |
| Pandoc 定位 | 已有 PATH 探测，摆脱原个人 D 盘路径。 |

## 三、仍需修复的问题登记表

优先级说明：P1 表示本轮应优先阻断交付的问题，P2 表示功能正确性与可靠性问题；这不是 CVSS 评分。

| 编号 | 优先级 | 问题、实际结果与定位 | 证据类型 |
| --- | --- | --- | --- |
| R01 | P1 | 文档选区 AI 接口每次在模型调用前 500：api/writing_routes.py 716–717 从 api.ai / api.ai_channels 导入，实际模块在 app。 | 真实已鉴权 API + 异常定位 |
| R02 | P2 | 选中第二处重复文本无法应用改写：前端只提交 old_text，没有选区稳定定位；建议接口返回 422“多处匹配”。web/editor.html 344–352、473–480。 | 按页面请求契约的真实 API + 源码 |
| R03 | P1 | 多通道“新建”重新选回默认旧通道，随后保存携带旧 id，可能覆盖旧配置。web/index.html 1236–1241、1291–1295、1310–1313。 | 原始前端函数执行，DOM/网络替身 |
| R04 | P1 | 全部 AI 调用并未统一路由：默认通道为 A，POST /api/ai/translate 实际仍请求 OLD 端点和旧模型。api/routes.py 779–786、app/ai.py 208–209；批量入口和 engine 手动入口亦需审计。 | 真实 API + 本机 HTTP 替身捕获 |
| R05 | P1 | 草稿把 endpoint 从 A 改为 B、Key 留空，自动把 A 的已存 Key 发往 B。app/ai_channels.py 240–267。 | 真实 API + 合成 Key + 本机端点 |
| R06 | P2 | 页面普通保存缺 enabled/max_tokens/timeout/proxy，后端整对象规范化会把停用恢复启用、777 改 4096、17 秒改 90 秒、代理清空；__CLEAR__ 被存为字面密钥；显式不存在通道仍返回启用的旧配置。app/ai_channels.py 37–54、100–121、215–227、240–267。 | 真实保存 API + 组件验证 |
| R07 | P2 | 测试后改模型没有使通过标志失效；loadAiConfigKeep 用 60ms 定时恢复草稿，响应晚于此时会被旧值覆盖。web/index.html 1246–1247、1332–1381。 | 原始前端函数执行，130ms 受控延迟 |
| R08 | P1 | 全局降为 read 后 REST 403，但新旧 WS 均可写；管理员签发票据后降级并注销，未消费旧票据仍可写。doccollab.py 210–243、486–495、990–1003。 | 真实 HTTP/WS，当前角色与会话均核查 |
| R09 | P2 | 前端把 Uint8Array 用 TextEncoder 编成逗号数字字符串再求 SHA，后端对原始 bytes 求 SHA，真实 ACK 永不匹配，界面停留 syncing。editor/src/main.js 95–99。 | 原始事件代码 + 原生 WebCrypto |
| R10 | P1 | 两个不同 client_id、相同 base_rev=1 的请求都成功应用，分别 rev2/rev3，版本检查仍在修改锁外。doccollab.py 729–757。 | 真实 Agent HTTP + 受控交错 |
| R11 | P1 | 完整回归捕获 B06 并发 HTTP 500 / sqlite3.InterfaceError；失败在 claim 之前 get_doc SELECT。共享连接、锁外查询/提交是待调查方向，不能直接归因 Linux WS。db.py 12–32、doccollab.py 99–101、writing_routes.py 922。 | 原始完整套件日志 |
| R12 | P1 | 已批准 rev3 后恢复 rev1 得 rev4，仍“已通过”且不能重审；默认 Word 导出仍取当前正文，可包含未批准内容。doccollab.py 620–624、694–718；wordexport.py 173–185。 | 真实 API、正文与 DOCX 内容核验 |
| R13 | P1 | B08 仍在：引用式/带 title 图片绕过原正则；普通写作者可将素材目录外合成文本打包进 DOCX，本机 HTTP 服务也收到转换器请求。wordexport.py 28、65–91、215–225。 | 真实 API 下载 + 隔离资源/网络夹具 |
| R14 | P2 | B11 仍在：标题 A & B、Markdown 加粗标题生成有效 DOCX 后被产品误判失败。wordexport.py 94–110、232–239。 | 真实 API + Pandoc 返回码及 OOXML |
| R15 | P2 | B12 仍在：同名附件从红图替换蓝图，正文不变，再导出返回同路径同 SHA，仍是红图。wordexport.py 186–203；writing_routes.py 642–662。 | 真实素材上传及 Word 下载 |
| R16 | P2 | B09 部分修复：恢复到另一目录，DOCX 文件存在，但数据库仍指向旧绝对路径，下载 410。backup.py 152–208；writing_routes.py 784–786。 | 真实备份、换目录恢复、重新登录下载 |
| R17 | P2 | B17 未修：固定 V1 仍读草稿 variables 和 agent_notes；新 V1 文档出现字面 {{title}}，旧 V1 Agent 得到未发布指令。writing.py 881–884；writing_routes.py 879–884。 | 真实模板/文档服务及数据库快照 |
| R18 | P2 | 新模板元数据 UI 无法保存：请求缺 fields 外层为 422；补外层后变量字符串数组仍 400，后台要求含 name 的对象。web/index.html 2685–2698；writing_routes.py 317–318、364–371。 | 按页面实际 JSON 调用真实 API |
| R19 | P2 | B21 未修：恢复待处理仍发空 decision 返回 400；版本弹窗的拼接优先级使关闭按钮缺失，仍直接 null.addEventListener。web/index.html 2400–2403、2675–2681。 | API + 原始 HTML 表达式执行 |
| R20 | P2 | 推荐“全部近期情报”提交 scope_type=all，后台只允许 manual/tweet/event/backlog，返回 400。web/index.html 2418、2431；writing.py 309–310。 | 按页面实际请求调用真实 API |
| R21 | P2 | 新推荐流程以 rec:task_id:index 做选题去重，不同任务同题同来源仍建重复候选，先前不采用状态不能命中。writing.py 357–389。 | 真实 worker + 两轮推荐/采纳 |
| R22 | P2 | 仅含非法引用的推荐被剔除全部 ID 后仍 done，可采纳并转成零来源报告；模型声称的依据和链接仍保留，候选丢失 rejected_refs 警示。writing.py 563–607、375–389。 | 固定模型输出 + 真实 worker/转换 |
| R23 | P2 | B22 不覆盖快速重启：启动时 running 未满 10min 不回收，此后普通 worker 只取 queued，无再次租约回收，任务持续 running。writing.py 399–430；main.py 31–35。 | 控制任务时间验证，无实际长等待 |
| R24 | P2 | 推荐结果虽然入库，页面没有刷新后历史任务入口；轮询只取最新 5 条，目标被挤出后直接 return，超时分支也走不到。web/index.html 2448–2453 及 /recommend/tasks 调用点。 | 静态调用链检查，浏览器待补 |
| R25 | P1 | RSS 分类重评估直接将已过滤改待发送，下游不重查规则；仍不匹配规则的旧情报生成两条模拟消息。engine.py 597–612、483–511；db.py 1592–1594。 | 历史条目夹具 + 实际分类/投递服务 + 模拟发送 |

## 四、关键复现场景与验收要求

### 4.1 选区 AI：导入修复之后还必须验证选区身份

真实请求返回：

```text
POST /api/writing/docs/{uid}/ai
HTTP 500
ModuleNotFoundError: No module named 'api.ai'
```

修改导入只能解决第一层阻断。当前 AISEL 记录的 from/to 没有送入建议服务，实际仅传 old_text。两段相同文字选第二段，正常建议接口会拒绝多处匹配。

下一轮必须：保留稳定选区与必要版本/内容校验；无关段落协作修改保留，同段变化明确冲突；问答不改正文，接受建议才改正文；中文、emoji、多轮问答、取消重试及权限撤销均验证。没有真实模型 Key 仍可用本机替身完成浏览器链路测试。

### 4.2 AI 配置：草稿和正式配置要有明确身份

通道页面的“新建态”不能用空 id 与默认选择混在一起。测试已执行原始函数：新建后 cur 又变为 saved-A，保存新地址时提交 id=saved-A。

修复后验收：

1. 已有 A，新建 B；A 的地址、凭据、模型、开关与高级参数保持原值，列表增加 B。
2. 保存 A、表单草稿 B；模型列表和测试实际请求 B，正式运行配置仍 A；显式保存才生效。
3. 测试通过后修改任一影响请求的参数，结果立即失效；慢响应不能覆盖新输入，取消/切换通道也不能串回旧表单。
4. 改 endpoint 不得无提示携带旧服务凭据；所有测试只用合成 Key。
5. 普通编辑保留未修改字段；清除密钥具有真实语义。
6. 显式缺失/停用通道 fail closed；不得悄悄请求旧全局配置。
7. 遍历研判、翻译、批量操作、手动操作、分类、候选与文档 AI 的真实入口，验证实际 HTTP 端点和模型。仅测试 resolve_cfg 函数不够。

### 4.3 权限、持久化和并发

不能以新票据绑定了 doc_uid 就认为身份撤销完整。票据消费需重新核查签发主体当前角色和原始会话/Token 有效性。REST、WS、Agent 使用一致的权限上限。

保存 ACK 对实际更新 bytes 建立一致协议。除普通编辑外，验证断线重连、同步合并、远端并发和超过滚动窗口，不允许永远 syncing 或错误 saved。

Agent 版本检查、定位、CRDT 修改、修订与幂等结果需处于统一文档串行边界。相同请求重试与不同请求使用旧 base_rev 是两类用例，均要验证。

保留本次完整测试中捕获的 500。增加连接/线程/SQL 阶段诊断，查清共享 SQLite 连接及事务边界；不能随意在所有函数加普通 Lock 造成重入死锁。真实 WS 清理应提交到 uvicorn 所属事件循环，先纠正测试跨循环操作，再定位 401 与 500；不以改 mock 或加重试消除失败信号。

### 4.4 Word、审核与备份

- 任意内容变更（含 restore）都正确处理批准状态；历史批准保留，新内容不能沿用旧批准。
- 最终 Word 与批准修订明确绑定，草稿导出为明确选项。必须实际读取目标修订正文，不能只赋值 rev 变量。
- Markdown 资源解析覆盖完整语法；Pandoc 子进程落实文件、网络访问范围，不把 --resource-path 当隔离机制。
- 测试只读取隔离夹具、自建端点；不访问真实秘密或生产网络。
- 标题检查解析 OOXML 可见文本，兼容 XML 实体和 Markdown 标记。
- 附件不可变版本/清单绑定文档修订，缓存含素材与样式等实际依赖。
- 恢复到新目录/新实例后实际登录并下载旧产物；不得只验证目录存在或表数量。
- 对 Word 样例真实下载、打开或渲染，包含中文、&、强调、列表、表格、代码、图片和引用。只验证 ZIP 结构不等于视觉验收。

### 4.5 模板、候选、RSS

模板发布快照要同时固定正文、变量、写作要求与检查规则。不能只断言 template_version 数字没有变化。

模板元数据、版本历史、候选恢复必须从真实界面走完整请求。按界面发送的 payload 写集成用例，不允许后台测试另外使用一套正确参数而掩盖前端错误。

候选需区分“单次结果采纳幂等”与“跨任务选题身份”。保留人工不采用、暂缓、已转报告和人工修改。无合法来源时明确提示待补材料，不能按有依据推荐正常推进；链接来自受校验的资料，不将模型编造链接当作已核验证据。

快速重启后任务应在租约策略下最终恢复，不能只在启动时检查一次。页面提供任务历史/恢复入口，按 task id 查询进度，不依赖最新五条列表。

RSS 复现中：

```text
规则：只允许“高危漏洞”
实际条目分类：数据泄露线索
pass_filter：false
最终：digest + instant 两条“模拟成功”消息
真实外发：0
```

必须逐推送实例重新判定规则、人工忽略、历史边界和 AI 完整性，再决定状态；不能用“已过滤→待发送”冒充重评估。

THN 结论应区分代码层默认配置复现和用户现场：尚无用户部署配置与日志，不能写“实际根因完全确定”。旧代码是周期内立即手动重试跳过，不是永久不会再次调度。

## 五、测试报告为什么不能作为当前验收依据

| 原报告依据 | 具体缺口 |
| --- | --- |
| U01–U05 指向 31 条协作安全断言 | 这些用例没有调用新文档 AI 入口，也没有验证页面配色、选区对话和交互。 |
| U07 通过指纹断言 | 只证明两个对象算出的指纹不同，没有证明 UI 修改时会使状态失效，也没覆盖慢响应。 |
| U08 多通道双端点 | 直接调用 test_ai(resolve_cfg(cid))，不能证明旧翻译/批量入口已接入。 |
| B05 saved_shas 存在 | 未验证前端实际 hash 与后端一致。 |
| B06 相同 client_id 并发 | 未覆盖不同 client_id 共用旧 base_rev；此次还出现真实 500。 |
| B07 第二次审核后导出 | 当前正文恰好等于批准版本，始终导出当前稿也能通过。 |
| Q07–Q10 Word 8 条断言 | 不含引用图片越界、特殊标题、同名素材变更和视觉验收。 |
| Q11 M2 备份 | 无真实附件/导出产物的换目录恢复下载。 |
| B17 template_version=1 | 没检查变量渲染结果与 Agent 指令来源。 |
| B15 辅助函数去重 | 没经过新推荐任务→结果采纳入口，跨任务身份问题未覆盖。 |
| B22 stale_minutes=0 | 不是默认十分钟策略；断言还允许仍为 running，无法证明恢复完成。 |
| Windows 通过、Ubuntu 抖动 | 本次 Linux 完整入口实际失败；不能忽略与唯一生产目标未核对的跨平台失败。 |

本轮观察证明“测试能运行”与“断言能检出需求缺陷”需要分别审查。不得删除失败用例、扩大通过条件、捕获测试异常后判成功，或用新增更多简单断言抵消关键场景缺失。

## 六、可直接交给开发 AI 的第三轮执行指令

以下为完整执行要求；先阅读本文件的问题表和证据说明，再开始修改。

### 6.1 目标与连续执行

你负责 x-intel-monitor 的第三轮修复与验收闭环。以当前最新代码为准，记录实际 SHA。不要盲信第二轮交付报告、提交标题、注释或测试名称。

用户六项业务目标继续有效：

1. 编辑器配色统一，Markdown 可用，选区 AI 问答/改写及多人协作正确。
2. 未保存的 AI 草稿可选模型并测试，通过后用户明确保存才生效。
3. 多 LLM 通道完整管理，实际业务调用按指定通道，兼容旧配置。
4. 查清 THN RSS 现场问题，采集与投递有诊断，修复不能引入误推或历史群发。
5. 用户提示词生成可阅读、可恢复、可采纳的候选文章列表，来源可靠。
6. X/RSS 推文流各入口可加入已有候选模块，失败不假成功。

保留多模板实时在线维护、发布快照、历史恢复、审核与一键 Word 下载，并通过旧功能回归。

延续整夜持续工作安排，允许充分使用分析、并行与测试资源；约 5 亿 token 是资源预估，不是必须消耗目标。正常开发、测试和修复无需等待“继续”。遇单项阻塞先推进其余工作，平台硬中断时保存检查点并说明状态，不假称后台运行。

### 6.2 第一个阶段：让证据对应当前代码

1. 阅读 AGENTS.md 和工作记录，检查工作区，建立任务分支/独立工作树。
2. 记录本文件审查 SHA 与当前 SHA 的差异，保持用户修改。
3. 阅读 evidence/root/offline.log 及各领域 JSON，先复现 R01、R03、R04、R08、R11、R13、R17、R25 等关键项。
4. 原始复现脚本含审查机器路径；在隔离副本更正路径，审阅夹具和副作用后运行。不要将测试指向生产库或真实推送。
5. 将每项转为能检出原缺陷的自动化/浏览器验收，证明旧版本失败，再修复到通过。
6. 为已被更新提交修好的项提供验证证据后关闭，不重复无用改动。

### 6.3 开发次序

- 第一批：文档 AI 500、通道误覆盖/错误路由、权限降级、Word 资源边界、RSS 误投递、数据库并发 500。
- 第二批：选区定位、ACK、原子版本检查、审核/恢复/导出版本、配置草稿及凭据语义。
- 第三批：模板元数据与版本快照、候选结果/身份/来源/重启恢复、Word 标题/附件/备份迁移、页面历史和错误反馈。
- 最后：集成、真实双浏览器链路、Word 视觉验收、旧功能完整回归及部署恢复。

支持并行时按模块划分，公共数据库/鉴权/通道协议指定唯一负责人；主 Agent 验证最终集成，不接受子任务口头“已完成”。

### 6.4 不得降低的完成门槛

- R01–R25 均有处置、验证和证据；静态项补真实行为验收。
- 浏览器完整链路：登录→新建第二通道→草稿测试→保存→抓取隔离 RSS→推文流加入候选→提示词推荐→采纳→编辑模板元数据并发布→创建文档→两个会话协作→选区问答/改写→审阅→下载 Word。
- 本机受控模型替身可用于上述浏览器流程；缺真实厂商 Key 不构成跳过整个浏览器验收的理由。浏览器能力确实不可用时准确标“未完成”，不能给勾。
- 鉴权保持开启，真实 WS 并发仍保留；测试清理在正确事件循环执行。
- Word 实际生成、下载并打开/渲染；依赖缺失明确补齐或记录硬阻塞，不能静默 SKIP 后宣布核心功能通过。
- 旧 AI 手动/自动/批量、X/RSS、搜索、推送规则/重试/去重、报告、事件、登录/Token/CSRF、备份恢复均回归。
- 修复最后一次代码后再运行完整套件，保留首次失败及修复后的日志。当前确有 Linux 500，需有针对性确定性并发测试和受控重复验证；不允许重跑到绿就删去失败记录。
- CI 的目标平台全部满足要求，或提供真实环境限制并标未完成。不能自行决定失败平台不阻塞。
- 交付报告中的每个“已修/已验证”必须能追溯到当前 HEAD 的代码、实际命令、日志/截图和观察结果。

### 6.5 持续记录与最终交付

持续维护 plan.md、state.md、findings.md，记录需求/缺陷状态、进程、依赖、提交、证据和可直接执行的下一步。

交付：

1. 基线/最终 SHA、分支、提交与推送状态。
2. R01–R25 逐项修复表及六项业务目标入口。
3. 原问题在修复前失败、修复后通过的证据。
4. 完整回归、真实浏览器、双用户、Word 样例与渲染证据。
5. 数据迁移、配置兼容、备份恢复及回滚说明。
6. THN 用户现场根因或明确待核事实。
7. 已修的其他问题、未验证内容和剩余限制。

不要只提交新的完成报告。现在开始核对实际代码、复现、修复、集成与验收，持续推进到满足上述完成标准或遇到真实硬性阻塞。

## 七、证据包与源码索引

证据包包含任务书、顶层复现脚本、JSON 和日志，未打包临时数据库、运行配置文件或测试产物目录。测试账号与 Key 均为合成数据，Key 在结果中清晰标识。

| 目录/文件 | 内容 |
| --- | --- |
| evidence/root/offline.log、offline-meta.json | 完整套件运行、首次失败堆栈、退出码。 |
| evidence/root/ai_api_results.json | 选区 AI 500、翻译旧路由、合成凭据跨 endpoint、普通保存重置、重复选区。 |
| evidence/root/config_ui_results.json | 原始前端代码的新通道身份、指纹状态、慢响应覆盖。 |
| evidence/realtime/ws-results.json | 权限、旧票据、恢复审核、不同请求旧版本并发。 |
| evidence/realtime/ack-results.json、fifo-results.json | 前端 hash 不一致；已修 FIFO 的正向验证。 |
| evidence/candidates/reproduce_remaining.json | 模板、候选入口/身份/来源、租约。 |
| evidence/candidates/reproduce_rss_reassessment.json | 规则不匹配仍进入模拟发送。 |
| evidence/export/api_export_results.json、export_edge_results.json | Word 资源边界、标题误判。 |
| evidence/export/review_and_cache_results.json | 审核版本、素材 API、旧图片缓存。 |
| evidence/export/backup_download_results.json | 换目录恢复后下载 410。 |

固定提交源码：

- [API 与文档 AI](https://github.com/xuecaizou/x-intel-monitor/blob/c0068af0bf0a1c1c6cfcd4209fcab1e1c0cdf82a/api/writing_routes.py#L697-L753)
- [AI 配置页面](https://github.com/xuecaizou/x-intel-monitor/blob/c0068af0bf0a1c1c6cfcd4209fcab1e1c0cdf82a/web/index.html#L1236-L1389)
- [AI 通道服务](https://github.com/xuecaizou/x-intel-monitor/blob/c0068af0bf0a1c1c6cfcd4209fcab1e1c0cdf82a/app/ai_channels.py)
- [旧 AI 默认配置入口](https://github.com/xuecaizou/x-intel-monitor/blob/c0068af0bf0a1c1c6cfcd4209fcab1e1c0cdf82a/app/ai.py#L208-L209)
- [协作内核](https://github.com/xuecaizou/x-intel-monitor/blob/c0068af0bf0a1c1c6cfcd4209fcab1e1c0cdf82a/app/doccollab.py)
- [前端 ACK](https://github.com/xuecaizou/x-intel-monitor/blob/c0068af0bf0a1c1c6cfcd4209fcab1e1c0cdf82a/editor/src/main.js#L83-L122)
- [Word 导出](https://github.com/xuecaizou/x-intel-monitor/blob/c0068af0bf0a1c1c6cfcd4209fcab1e1c0cdf82a/app/wordexport.py)
- [模板与候选](https://github.com/xuecaizou/x-intel-monitor/blob/c0068af0bf0a1c1c6cfcd4209fcab1e1c0cdf82a/app/writing.py)
- [模板页面](https://github.com/xuecaizou/x-intel-monitor/blob/c0068af0bf0a1c1c6cfcd4209fcab1e1c0cdf82a/web/index.html#L2673-L2703)
- [RSS 处理与投递](https://github.com/xuecaizou/x-intel-monitor/blob/c0068af0bf0a1c1c6cfcd4209fcab1e1c0cdf82a/app/engine.py#L597-L612)
- [重评估状态修改](https://github.com/xuecaizou/x-intel-monitor/blob/c0068af0bf0a1c1c6cfcd4209fcab1e1c0cdf82a/app/db.py#L1581-L1599)
- [7de42b1 实际提交](https://github.com/xuecaizou/x-intel-monitor/commit/7de42b1)

