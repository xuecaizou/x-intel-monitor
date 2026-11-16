
## Word 渲染验收结论（2026-11-17）

- 真实 Word COM 自动化三次尝试（含 AutomationSecurity=3、Visible=false、DisplayAlerts=0、
  90s 守卫）：Documents.Open 阶段被不可见模态阻塞（本机 Word 授权/首启状态未知）→
  **渲染级验证未完成**，如实标注。
- 结构级证据已充分：真实 pandoc 3.6.3 产物 + ZIP/OOXML 部件校验 + OOXML 可见文本探针
  （R14 后含 XML 实体反转义）+ 浏览器真实下载事件 + 双样例
  （docs/samples/collaborative-writing/）。
- 剩余建议：在装有可用 Word/WPS/LibreOffice 且无首启模态的环境人工双击打开核验
  （样例已交付 docs/samples/collaborative-writing/）。
