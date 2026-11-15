# Round2 整改计划（plan.md）

> 基线：origin/main @ 3407c44（第一轮协作写作工作台 + AI 网关修复）。
> 分支：fix/round2-repair。开始时间：2026-11-16。
> 任务书：x-intel-monitor-round2-repair-development-prompt.md（475 行）。

## 环境

- Python 3.12.14 / Node v24.14.0 / Pandoc 3.12.1
- 依赖补装：httpx2（starlette 新版 testclient 强制要求，requirements/ci 未含 → F-01）
- 离线基线：25 套件 531 断言全绿（/tmp/r2_baseline3.log）

## 工作包（按依赖排序）

| WP | 内容 | 依赖 | 状态 |
|----|------|------|------|
| WP1 | 基线+工作记录+分支 | - | 进行中 |
| WP2 | B01–B07 协作权限/持久化/审核（doccollab.py） | WP1 | 待开始 |
| WP3 | 需求 B 草稿测试不落库 + 需求 C 多 LLM 通道（ai_channels.py + 迁移 + 全调用点接入） | WP1 | 待开始 |
| WP4 | 需求 D RSS 根因/诊断（rssfetch/engine/前端） | WP1 | 待开始 |
| WP5 | 需求 E 提示词候选（writing.py 重构）+ 需求 F 推文流入口 | WP3（通道） | 待开始 |
| WP6 | 需求 A 编辑器配色/MD/选区 AI（editor.html+src+bundle 构建） | WP3（通道供 AI 调用） | 待开始 |
| WP7 | B08–B12 Word/素材/备份 + B13–B22 writing/事务/UI | WP5 | 待开始 |
| WP8 | 模板在线维护页面（10.4） | WP7 | 待开始 |
| WP9 | 测试体系修复（§11.1–8） | 各 WP | 待开始 |
| WP10 | 全量回归 + 双用户浏览器验收 + 交付 | 全部 | 待开始 |

## 需求 → 验收映射（关键）

- A → U01–U05；B → U06–U07；C → U08；D → U09–U11；E → U12–U14；F → U15
- B01–B07 → Q01–Q06；B08/B11/B12 → Q07–Q10；B09 → Q11；B17 → Q12；B18/B19 → Q13；B20/B21 → Q14；全链路 → Q15

## 缺陷清单处置（B01–B22 + RSS 三路径）

状态枚举：待核对 / 已修（提交） / 已被 3407c44 修复（验证中） / 受阻
详细处置表在 findings.md 持续更新。

## 下一步

WP2：读 doccollab.py 全文 → B01–B07 逐项写回归 → 修复。
