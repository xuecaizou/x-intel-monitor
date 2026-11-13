# 协作写作工作台 · 计划与验收追踪

开始时间：2026-11-13 23:10（本地） · 分支：feat/collab-writing · 基线：034bf65a5ba016c4de7cb3ea1203e8fd4aeae762（main）

## 需求 → 实现 → 测试 → 证据 追踪表

| # | 需求 | 实现位置 | 测试编号 | 证据 |
| --- | --- | --- | --- | --- |
| F1 | Markdown 多人实时协作 + Agent 协作 | app/doccollab.py + api/routes.py WS + web/editor.html + web/vendor/collab.bundle.js | N01-N05,N18 | 待填 |
| F2 | 待编写线索候选（人工/事件/AI 推荐/去重/转报告） | app/writing.py + db 写作表 + api/routes.py | N06-N08 | 待填 |
| F3 | 多模板管理（协作编辑/预览/发布版本/停用） | app/writing.py（模板）+ app/doccollab.py 复用 | N09 | 待填 |
| F4 | 一键下载 Word（本地 pandoc，真实 docx） | app/wordexport.py + reference.docx | N12-N15 | 待填 |
| F5 | 成员身份与文档权限（修正会话 role=admin 缺陷） | app/auth.py 修正 + 成员管理端点 + 文档 ACL | N11 | 待填 |
| F6 | 旧功能回归 R01-R10 | 既有 offline 22 套件 + 新增集成 | R 矩阵 | baseline-offline.log / final-offline.log |
| F7 | 备份恢复扩展（新表+正文+素材） | app/backup.py 扩展 | N16 | 待填 |
| F8 | 首页汇总（候选/编辑中/待审阅，服务端全量聚合） | api/routes.py stats + index.html | F8 | 待填 |

## 分阶段计划

- [x] P0 基线：仓库阅读 / 离线 22 套件基线（exit 0）/ 工作记录建立
- [x] P1a pycrdt 0.14.13 + pycrdt.websocket 0.16.9（Python 3.14.6）导入与 CRDT 合并验证（中文/emoji）
- [ ] P1b pandoc 3.6.3 中文 DOCX 最小验证 + reference.docx 生成
- [ ] P1c 前端 bundle 构建（yjs13.6.33 / y-websocket 2.1.0 / y-codemirror.next 0.3.6 / codemirror 6 / marked）
- [ ] P2 后端：数据模型/迁移、候选、模板、文档、权限、AI 推荐、备份扩展
- [ ] P3 前端：editor.html 独立页 + 主页面板 + 首页汇总
- [ ] P4 Agent API：读取/建议/受控修改 + 示例脚本
- [ ] P5 Word 导出：版本捕获、pandoc 子进程、边界与失败处理
- [ ] P6 测试：新增套件 + R01-R10 + N01-N20 + 浏览器 E2E
- [ ] P7 文档：部署/迁移/回滚/使用说明 + 最终报告 + 提交推送

## 验收清单（验收矩阵详表见 findings.md 证据区）

- [ ] N01 两用户并发编辑（真实 WS，两客户端收敛）
- [ ] N02 断线重连与重启恢复
- [ ] N03 Agent 迟到修改不覆盖人工
- [ ] N04 异常补丁/重试幂等
- [ ] N05 中文/emoji/组合字符
- [ ] N06 人工候选全操作
- [ ] N07 AI 候选真实链路
- [ ] N08 候选转报告并发幂等
- [ ] N09 模板多版本与隔离
- [ ] N10 审阅与恢复版本绑定
- [ ] N11 权限（无 bypass）
- [ ] N12 Word 浏览器下载
- [ ] N13 Word 格式与版式
- [ ] N14 导出并发与失败
- [ ] N15 导出与旧门禁隔离
- [ ] N16 迁移与恢复
- [ ] N17 预览与资源边界
- [ ] N18 主页轮询不破坏编辑器
- [ ] N19 完整业务流程
- [ ] N20 可重复部署

## 限制与已知环境事实

- 本机无 Word/WPS/LibreOffice → 渲染级验证受限，用结构级（OOXML 解析）+（若可装）LibreOffice 补充；如实标注。
- pandoc 为便携版放在仓库外 `D:/AI-work/tools/pandoc/pandoc-3.6.3/`，运行时配置 `word.pandoc_path` 指向；不入库。
- 公开仓库：不得包含任何内部规范名/案例名/密钥。
