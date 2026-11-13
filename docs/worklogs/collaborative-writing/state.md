# state.md · 运行状态快照

更新：2026-11-13 23:35

- 分支：feat/collab-writing（自 main@034bf65 切出）
- 基线 SHA：034bf65a5ba016c4de7cb3ea1203e8fd4aeae762
- HEAD：feat/collab-writing（随实现推进，见 git log）
- 未提交改动：package.json/package-lock.json（前端构建依赖，已定版）、docs/worklogs/collaborative-writing/*（工作记录）
- 生产服务：8083 上有旧代码实例运行中（PID 13492，main@034bf65）——集成测试前须重启为新代码
- 离线基线：22 套件全 PASS，exit 0 → docs/worklogs/collaborative-writing/baseline-offline.log
- Python：3.14.6（C:/Python314/python.exe）；pip 已装 pycrdt 0.14.13 / pycrdt-websocket 0.16.9（用户 site，模块在 pycrdt/websocket 子包）
- Node：v24.17.0 + npm 11.13.0；node_modules 已装（yjs 13.6.33 / y-websocket 2.1.0 / y-codemirror.next 0.3.6 / codemirror 6.0.2 / marked 12.0.2 / esbuild 0.25.12）
- pandoc：3.6.3 便携版 → D:/AI-work/tools/pandoc/pandoc-3.6.3/pandoc.exe（仓库外，不入库）
- 阻塞：无
- 下一步：P1b pandoc 中文 DOCX 最小验证 → P1c bundle 构建 → P2 后端实现

## 最终状态（2026-11-14 01:00）

- 分支 feat/collab-writing；改动 39 文件 +9047 行（提交见 git log）
- 离线 25 套件全 PASS（full-offline-final.log）；healthcheck 37/38 + smoke 48/56（隔离实例，8 项环境限制如实记录）
- 生产 8083 已重启至最终代码（PID 见 netstat），生产库迁移完成（13 新表）
- Word 样例交付 docs/samples/collaborative-writing/；渲染级验证未完成（Word COM 被不可见模态阻塞，已如实记录）
- 遗留：无阻断缺陷；外部联调（真实 AI 推荐模型冒烟/生产 smoke）待凭据与配额
