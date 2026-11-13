# findings.md · 已确认事实与设计决定

## P0 基线（2026-11-13）

- 基线 HEAD = 034bf65a（main），工作区仅新增任务书/工作记录。
- 离线基线：22 套件全 PASS exit 0 → baseline-offline.log（22/22，无 skip）。
- 仓库无 AGENTS.md/CLAUDE.md；文档在 docs/（requirements R1-R78、architecture-notes、ops-runbook、bug-ledger）。
- 生产服务 8083 运行 main@034bf65（后台任务，集成阶段需重启换新代码）。

## P1 技术验证（全部通过）

1. **pycrdt 0.14.13 + pycrdt-websocket 0.16.9**（Python 3.14.6，用户 site）：
   - CRDT 双节点合并中文/emoji 收敛一致；`Doc.get(name, type=Text)` 关键字形式（0.14 API 变化）。
   - 包结构：**`pycrdt.websocket` 是 pycrdt 的子包**（0.16.9），导出 ASGIServer/WebsocketServer/YRoom；无独立顶层模块（任务书预警属实）。
   - `ASGIServer(on_connect=scope→bool)`：返回 True = 拒绝连接（鉴权钩子）；scope["path"] 即房间名。
   - `YRoom(ydoc=…, ystore=…)` 可预建；`WebsocketServer.rooms[name]` 预注册后 get_room 不再新建；`auto_clean_rooms` 空房自动删。
   - `YRoom.on_message(message)`：返回 True 跳过（但拿不到 channel 归属 → 只读过滤需按 channel 包装，见下）。
   - `serve()`：`async for message in channel`；channel 协议 = send/recv/path/__aiter__（ASGIWebsocket）。
2. **pycrdt Text 索引语义 = UTF-8 字节偏移**（实测：'a🌍b' len=6；中文三字节；跨字节边界索引会被钳到尾部）：
   - 服务端受控修改必须自算字节边界：`b_off = len(prefix.encode('utf-8'))`；边界精确处 del/insert 全部正确（含中文/emoji/café 组合字符混合串）。
3. **pandoc 3.6.3**（便携版，D:/AI-work/tools/pandoc/pandoc-3.6.3/，仓库外）：中文标题/加粗/表格/哈希/代码块全命中，OOXML 合法（16 parts），styles.xml 含 eastAsia 字体声明。
4. **前端构建链**：node v24.17.0 + npm 11.13.0 + esbuild 0.25.12 可用；锁定 yjs 13.6.33（稳定 13 线）+ y-websocket 2.1.0 + y-codemirror.next 0.3.6（peer 要求 yjs ^13.5.6 ✓）+ codemirror 6.0.2 + marked 12.0.2。
5. **pycrdt/store/base.py BaseYStore**：抽象接口 write(data)/read()/started/start_lock/encode_state_as_update/apply_updates；自研 SQLite YStore（写主库 doc_updates 表，便于统一备份）。

## 设计决定

- **协作存储**：doc_updates 逐更新落主库（单文件备份友好）；房间重建=按 seq 回放；初稿仅在 doc_updates 为空时从模板发布版/空正文初始化一次（UNIQUE(doc_uid,seq) 兜底）。
- **持久化确认**：自研 YStore.write 落库后推进房间 awareness 本地状态 saved_seq → 前端"已保存"以服务端落库为据。
- **业务修订**：周期(3s)对比 ydoc 文本哈希→doc_revisions(kind=save)；restore/agent_edit/suggest_apply/submit_review/export 均成不可变修订。
- **只读约束（服务端）**：包装 Channel，对只读连接丢弃 SYNC_STEP2/SYNC_UPDATE（服务端永不应用），放行 SYNC_STEP1/AWARENESS。
- **Agent 协作**：REST-only（不走 WS）；修改建议=锚点精确匹配（零/多匹配→明确失败）；直接修改=base_rev 乐观锁+幂等 client_id；身份=可吊销 agent token。
- **权限**：修正 auth.py 会话 role 硬编码 admin（auth_users.role 为准）；新增成员管理（admin）；文档 ACL=每用户 read/write + 默认权限列；审核=admin；模板维护=admin（预览=全员）。
- **WS 鉴权**：会话 Cookie 或一次性 ticket（60s，单次使用）；Origin 与 Host 校验。
- **Word 导出**：POST export → 主循环内原子捕获修订（冻结文本）→ 线程内 pandoc 子进程（list 传参、timeout、临时目录、--resource-path 限本目录）→ 校验 ZIP/OOXML → 下载鉴权；远程图片不抓取（明确缺图提示），受控本地图片（data/doc_assets/<doc_uid>/，≤8MB）进转换。
- **转换幂等/并发**：导出用捕获的修订版本，编辑不受阻塞；同 rev 同参数缓存命中（内容哈希比对防过期文件）。

## 缺陷与环境限制记录

- 【已确认待修】auth.py:308 会话 principal role 硬编码 "admin"（auth_users.role 被忽略）。
- 环境无 Word/WPS/LibreOffice → 渲染级验证受限：结构级验证（OOXML 解析）为主，渲染如实标注"未验证"。
- pandoc 不入库（公开仓库不放大二进制）；部署文档写明下载+版本锁定+word.pandoc_path 配置。
- CI（GitHub）无 pandoc：导出测试在无 pandoc 环境显式 SKIP 并记录原因（不算隐藏失败）。

## 实现期新发现（2026-11-14 凌晨）

1. **SQLite 保留字**：doc_updates 列名 `update` 触发语法错误 → 改 `update_blob`（三处 SQL 同步）。
2. **Starlette Mount 传完整路径**：ASGI 子应用的 scope["path"] = `/ws/docs/<uid>`（非剩余路径）→ 房间注册/查找键必须用 scope path；`_room_by_uid` 用 doc_uid。已用真实 uvicorn + websockets 客户端验证。
3. **房间生命周期**：`auto_clean_rooms` 删除房间后必须同步清理 `_room_by_uid/_rev_tasks/_conn_registry`（陈旧引用让重连拿到已停止的房间 → 客户端收不到任何同步）。override `delete_room` 解决。
4. **pycrdt Text 索引 = UTF-8 字节偏移**（跨字节索引被钳到尾部）→ 服务端受控修改全部自算字节边界（`len(s[:i].encode('utf-8'))`），中文/emoji/café 混合串实测精确。
5. **websockets 16 为纯 asyncio API**（无同步 __enter__）→ 测试客户端用"内部事件循环线程 + run_coroutine_threadsafe"桥接。
6. **TestClient WS 桥接在此环境不可用**（accept 前阻塞）→ 协作测试改用真实 uvicorn + 真实 WS 客户端（更符合 N01）。
7. **smoke_test 直读同进程 db**：XINTEL_BASE 指向隔离实例时必须同时 `XINTEL_DATA_DIR` 对齐，否则 HTTP 与 db 数据面错位（G7/G8 假失败）。
8. **会话角色历史缺陷已修**：auth.py 会话 principal 曾硬编码 role=admin；现在以 auth_users.role 为准（W10-4 断言）。
9. **AI 推荐 worker 退出后 retry 不拉起**（真实缺陷）→ retry_recommend_task 重新 `_ensure_recommend_thread`（M3-5）。
10. **导出校验探针**：不能拿文档标题硬套模板骨架文档 → 改为正文首个非空行探针。
11. **IAB/嵌入式浏览器禁用 prompt()/confirm()** → 面板与编辑器全部交互改内联表单模态（对真实浏览器也是更好 UX）；旧功能里的 confirm 保留（真实浏览器可用）。
12. **Word COM 自动化在本机被不可见模态阻塞**（90s 守卫超时）→ 渲染级验证如实标注"未完成"；结构级 OOXML 解析 + 真实浏览器下载事件 + 真实 pandoc 产物已完成。

## 测试证据汇总

- 离线全套件（最终）：25 套件全 PASS exit 0 → full-offline-final.log（含工作台 W×80 + 协作 C×21 + 迁移 M×14 断言）
- healthcheck（隔离播种实例）：37/38，唯一 FAIL=GET /api/ai/models（需真实 AI 网关，环境限制）→ healthcheck-final.log
- smoke（隔离播种实例）：48/56，8 项 FAIL 全为外部环境项（真实 AI 网关×3 / 真实企微 webhook×1 / healthcheck 复合×1 / 种子库无 RSS 历史数据×3）→ smoke-final.log；生产实例运行需凭据（本会话无）如实标注
- 浏览器 E2E（隔离实例 18083→真实浏览器）：登录→主页新 KPI→模板创建(表单模态)→发布 v1→协作编辑（中文+emoji）→双标签实时收敛（"已同步"持久化确认）→转报告（模板版本固定+变量实例化）→编辑→提交审阅→批准→下载 Word（真实浏览器下载事件 + 服务端产物结构校验 13/13）→缺依赖明确报错→重连恢复
- Word 样例：docs/samples/collaborative-writing/{word-sample-short,word-sample-comprehensive}.docx（pandoc 3.6.3，综合样例结构校验 13/13：多级标题/表格/嵌套列表/长IOC/哈希/代码/受控图片/来源）
- Agent 冒烟：scripts/agent_demo.py 对真实实例跑通（读取→建议 200→受控修改 rev+1→client_id 幂等）
