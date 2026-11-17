# state.md（round4 稳定性治理 · 唯一进度入口）

> 本文件是本轮唯一实时进度入口。旧 round2 内容已归档至 git 历史（fix/round2-repair 分支记录）。
> 规则：每解决一个根因更新一次；包含实际 SHA、已完成流程、失败证据、未验证项、下一步。

## 环境快照（本机实测 2026-11-17）

- 项目实际路径：D:\AI-work\黑客组织报告编写\x-intel-monitor（任务书路径 D:\AI-to-tools\18-X\… 在本机不存在，已实际搜索定位）
- 本地 HEAD：43e596a（main，与远端一致）· 工作分支：fix/round4-stability
- 远端：github.com/xuecaizou/x-intel-monitor main=43e596a（git ls-remote 实查）
- Python 3.14.6（C:\Python314）· Node v24.17.0 · **Pandoc 缺失**（线索 E：W7/B07-8 跳过根因）
- 关键依赖已装（fastapi 0.139/pycrdt 0.14.13/httpx2 2.16/pytest 9.1.1）
- 运行实例：**8083 端口生产实例在跑（PID 3848）——本轮不改生产**，测试一律隔离实例
- 测试架构：29 个脚本式套件（非 pytest 可收集，pytest 收集会执行模块级代码→INTERNALERROR，唯一合法入口 offline_test.py）

## 基线（offline_test.py @43e596a）

- 首跑（有并发干扰）：28/29，test_round2_candidates 零输出 exit 1 → 复盘为我手动并发跑 batch3 所致
- 干净重跑：进行中（logs/baseline_round4.log）

## 已确认缺陷（当前 HEAD 复现）

- **A** loadAiConfigKeep：60ms setTimeout 守卫无法区分"用户编辑 vs 异步渲染覆盖"——loadAiConfig 的 fetch 渲染必覆盖草稿，恢复逻辑不可靠（10ms/200ms 均复现丢草稿）
- **B** cand-rechist：`h.querySelectorAll` h 未定义 → ReferenceError 被空 catch 吞 → 弹窗"查看结果/重试"按钮全死；pollRecommend 的 interval 关闭弹窗不清理 → 僵尸轮询写新弹窗（多击堆叠）
- **C** 审核原子性：submit_review 三段加锁（状态读/正文读/修订写分离）+ review_doc 无 review_rev 校验无 CAS + **待审期间正文变化不回落编辑中**（B07 只对已通过）→ 旧版通过显示为当前草稿通过
- **E** Pandoc 缺失 → W7/B07-8 跳过、Word 渲染验收未完成

## 检查点记录

- [2026-11-17 01:56] 分支 fix/round4-stability 创建（自 43e596a）。A/B/C 已在当前 HEAD 确认存在（代码证据已读）。
- [2026-11-17 02:20] 基线确认：干净重跑 29/29 全绿（logs/baseline_round4.log，EXIT=0）。首跑 candidates 失败系我并发干扰所致，已复盘并记录（教训：基线期间不得并发手动跑套件）。
- [2026-11-17 02:40] 三个独立提交完成：
  * 03ac742 fix(web) 线索 A：loadAiConfigKeep 草稿状态+渲染归属（零定时器）
  * 6e7356b fix(web) 线索 B：cand-rechist 事件绑定 + 空 catch 反馈 + 轮询生命周期
  * 42cf8ca fix(collab) 线索 C：审核快照契约 + CAS + 待审变动回落（统一 _demote_doc_status_locked 收口 3 个写入点）
  测试：tests/test_round4_review.py 新增 17 项全过；writing_workbench 73/0（W4-4/5/6 按新契约改写）；collab_security 30/0；writing_collab 21/0。
  JS 语法 node --check 通过（A/B 各自中间态+最终态）。
- [2026-11-18 00:10] 线索 E 验收缺口补齐：Pandoc 3.14 安装（用户级 AppData\Local\Pandoc）→ W7 真跑 7/7（OOXML/中文/中文名/缓存）、B07-8 真跑通过（默认导出=批准快照 rev，端到端验证 C 契约）；offline_test.py 纳入 test_round4_review（30 套件，b538bda）。全量 30/30 全绿（logs/baseline_round4_pandoc.log）。
- [2026-11-18 00:25] Word 渲染级验收 18/18 全过（scripts/word_render_check.py，提交见下）：三层独立验证（OOXML/python-docx/pandoc DOCX→HTML）+ Word COM 实际打开渲染成功（round3 三次被模态阻塞项，DispatchEx+DisplayAlerts=0 组合通过）。产物 logs/round4-artifacts/render_check.docx + render_evidence.json。
- 过程修复：render 脚本初版误用 /restore 的 body_md 参数（RestoreBody 只有 rev——该端点语义是恢复到指定历史修订）；改走真实业务入口 dc.restore_text。非产品缺陷，已如实记录。
- [2026-11-18 00:50] 浏览器实测（隔离实例 8990 + 真实页面交互）全部通过：
  * A：注入 300ms /api/ai/* 延迟 → 点测试 → 草稿 model/url/key 全保留（修复前必丢）；切换通道草稿正确放弃
  * B：推荐历史弹窗按钮绑定恢复（查看结果→轮询弹窗打开）；关闭弹窗后 5s 轮询请求=0（修复前僵尸轮询）；重开/关闭两轮均正常
  * 新发现并修复：index.html 缺失 ME 定义（AI 面板对所有人 ReferenceError）→ 681fe8a；ME 加错 IIFE 作用域 → c0c3a44 修正至 block0
- 下一步：全量回归（30 套件）→ 推送分支 → 报告。
