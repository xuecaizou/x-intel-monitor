# findings.md（round2 事实与证据）

## F-01 requirements/ci 缺 httpx2（基线实证）

- 现象：全新环境 `pip install -r requirements.txt` 后 18 个套件 ImportError：
  `starlette.testclient requires the httpx2 package`
- 证据：/tmp/r2_baseline.log（EXIT=1，18 套件失败）→ 安装 httpx2 后 /tmp/r2_baseline3.log 全绿
- 结论：远端"25 套件全绿"依赖开发者本机已装 httpx2；requirements.txt 与 ci.yml（只装 httpx）
  均未声明。处置：加入 requirements.txt（测试依赖，starlette>=新版本强制）。

## F-02 远端提交盘点（034bf65..3407c44，10 个）

- 091e6df 报告编写工作台（候选/模板/协作/Word/Agent）— 第一轮交付主体
- 6b95421/1c18b2b 依赖补齐（pycrdt/pycrdt-websocket/websockets）
- 25d7adc 文档同步（help §8/arch 数据模型）
- add1236/aa4db1e/236d638/c557e48 AI 网关兼容三连（SSE 尾巴容错/UTF-8/替身兼容/友好报错）
- cb4e401/3407c44 回归证据与文档
- feat/collab-writing 分支无领先提交（工作已并入 main）

## F-03 工具链

- Python 3.12.14；Node v24.14.0；Pandoc 3.12.1（任务书审查环境 3.1.11，本地更新版本，Word 转换需回归验证）
- pycrdt：requirements 锁定小版本安装成功

## F-04 基线测试规模

- 25 套件；断言总数见 /tmp/r2_baseline3.log（含 SKIP 1 = Word 组内单跳过项，对应任务书 §11.1
  "Pandoc 路径硬编码/整组跳过"问题——本机有 pandoc 3.12.1 仍跳过 1 项，待查 test_writing_workbench）

## F-05 The Hacker News RSS 根因（round2 需求 D，已取证）

- **根因链**（默认部署复现，证据 tests/test_round2_rss.py D-R5）：
  1. RSS 继承 X/network 的 `socks5://127.0.0.1:10808` 代理 + `outbound.trusted_proxy=false`
     → 出站校验直接拒绝：`URL 拒绝（出站策略）：用途 rss 配置了代理但未声明受控出口`
     → 0 条入库、0 推送（此前页面无任何可见诊断）。
  2. 失败发生在 fetch_source 内部——但 `run_rss_round` 在抓取**前**写 `rss_last`
     → 手动重试 due=0，不发请求 → 永不再试（除非等 interval）。
- **修复**：
  1. DEFAULTS 新增 `rss{proxy:"",max_items:30,timeout_seconds:25}`——RSS 默认直连
     （feedburner 公网可抓）；显式配置 rss.proxy 才走代理（仍受 trusted_proxy 约束）；
  2. 节流位点改为**成功时间** rss_ok_{id}（失败不节流）；新增 fetch_now/force_ids
     （"立即抓取此源"，保留互斥）；互斥标志 rss_busy 修复（原 finally 写当前时间=600s 误锁）；
  3. 诊断落 kv rss_diag_{id}（attempt/ok/http_status/parsed/scanned/new/dup/error/
     error_class/via_proxy），/api/rss/sources 返回明细；前端来源行显示成功/失败/计数+立即抓取按钮；
  4. 分类顺序：确定性兜底分类**提前到 dispatch 之前** + 存量未分类有界扫描（≤50/轮，
     db.tweets_pending_classify）；dispatch 对"等待分类"采用不同语义（不写永久已过滤，
     情报留队列等分类补齐）；分类由空变非空→定向 reevaluate_filtered（不做无差别重置）。
- **现场假设待确认**（无法读用户部署日志）：用户现场是否配置了代理、来源是否启用。
  页面诊断（来源行 + 单条情报 /api/tweets/{id}/diagnose）已可分层定位。

## F-06 引擎通道接线（需求 C）

- run_ai_stage/process_ai_tasks 增加 ai_cfg 显式参数（任务快照/测试替身优先），
  未提供时走 ai_channels.resolve_cfg(task)；run_single_ai/backfill/classify_recent 同步接入。
- 坑：第一轮 B 组测试用 {"ai": ACFG} 注入替身——新语义下必须走 ai_cfg 参数
  （已适配调用点，断言未动）。

## F-07 测试体系

- test_writing_workbench W7 硬编码开发者 D 盘 pandoc 路径 → we.pandoc_path()（配置→PATH），
  本机 pandoc 3.12.1 实测 Word 8 断言全过、SKIP 清零；
- test_round2_collab_security 启动加 3 次换端口重试（离线连跑偶发绑定竞态）。
