# X-Intel 运维手册（第四批）

## 1. 首次部署与登录

1. 安装依赖：`pip install -r requirements.txt`
2. 创建管理员（**仅本机**，禁止匿名远程初始化）：
   ```
   在项目 data/ 目录创建 admin_init.txt，内容一行：用户名:密码（密码≥8 位）
   ```
3. 启动：`python run.py` → 服务读取 admin_init.txt 创建管理员并删除该文件
4. 浏览器访问 → 自动跳转 `/login` → 登录
5. 自动化调用：登录后在「Token 管理」创建 Token（角色 read/write/admin），
   调用时带 `Authorization: Bearer xintel_…`（完整值仅显示一次）

> 兼容期：如需临时关闭认证，在 runtime.json 设 `auth.enabled=false`（**不推荐**，仅限完全隔离环境）。

## 2. Token 权限

| 角色 | 能力 |
|------|------|
| read | 查询推文/统计/日志/规则/任务/线索包 |
| write | read + 改配置/推送/删除/补发/任务提交 |
| admin | write + Token 管理/迁移预览/备份 |

吊销：`POST /api/auth/tokens/{id}/revoke`；轮换 = 新建 + 吊销旧。

## 3. 备份

```bash
python scripts/backup_restore.py backup --dest backups
python scripts/backup_restore.py verify backups/xintel-backup-YYYYmmdd_HHMMSS
```
- 使用 SQLite online backup API（**含 WAL 未检查点数据**）
- 备份含 runtime.json（webhook/API Key）→ 目录按敏感数据保护
- manifest 含代码 commit、schema 版本、文件 SHA-256

## 4. 恢复演练

```bash
# 恢复到空目录（不覆盖现有）
python scripts/backup_restore.py restore backups/<stamp> restore-test
# 校验：integrity + 关键表计数
```
生产恢复步骤：
1. **停止服务**
2. 备份当前 data/（即使要恢复也要先留现状）
3. 将备份目录内容复制到 data/
4. 启动后**先确认 auth.enabled**，再确认调度：恢复后默认不自动补发
5. 用 `export-pending` 导出未完成/不明任务，人工决定重试

## 5. 精确回退（第五批修订）

### 5.1 代码回退（含新增文件处理）
```bash
# 以"本批起点"为基准（不要用更早的基线，会回退掉中间批次的修复）
git checkout <本批起点commit> -- app/ api/ web/ tests/ scripts/ .github/     healthcheck.py smoke_test.py offline_test.py requirements.txt

# 本批/后续批次"新增但旧版本没有"的文件必须显式删除，否则会残留并可能被导入：
git rm -f app/auth.py app/backup.py app/safefetch.py app/integrity.py          web/login.html tests/testenv.py scripts/backup_restore.py          scripts/check_no_secrets.py .github/workflows/ci.yml 2>/dev/null

git status   # 复核差异：应只有预期文件
```

### 5.2 回退到无认证版本的强制要求
撤销认证会**重新暴露全部敏感接口**（配置、推送测试、补发、删除）。
执行前必须：
1. **暂停调度**：`scheduler.enabled=false`，重启后确认无抓取/推送
2. **关闭外发**：`outbound.enable=false`
3. **网络隔离**：仅监听 `127.0.0.1`（`server.host=127.0.0.1`），不要保持 `0.0.0.0`
4. 撤销后立即检查是否有多余的 API Token 文件/脚本仍在轮询

### 5.3 数据库配套
- 旧版本不认识新表（auth_*/delivery_*/ai_tasks/ai_result_conflicts）→ **保留不删是无害的**
- 但 `tweets` 新增列（analysis_version/ai_integrity/ai_review/source_id/text_hash）旧代码会忽略
- **回退数据库**：只能用备份恢复，会丢失备份之后的数据：
  ```
  # 先导出增量（重要）
  python scripts/backup_restore.py export-pending data/xintel.db > pending_before_rollback.json
  # 记录截止点
  sqlite3 data/xintel.db "SELECT MAX(id), MAX(created_at) FROM tweets;"
  # 停服务后恢复备份
  ```
- **不要**把"回退代码"与"恢复旧备份"混为一谈：前者保留数据，后者丢失数据

## 5.4 逐实例 → 旧版 pushed 语义降级（损失已知）

**先用矩阵测试理解损失（tests/test_t7_matrix.py）**：
- 任一终态 → pushed=1 → 旧代码不重发（**B 待重试会丢失**）
- 仅待发送 → pushed=0 → 旧代码接管

步骤：
1. 暂停调度（停服务）
2. `python scripts/backup_restore.py export-pending data/xintel.db > pending.json`
3. 记录数据截止点（当前时间 + 备份 manifest）
4. `git checkout 8a50473 -- app/ api/ web/ tests/`（或目标版本）
5. 启动旧版，复核 pending.json 中的条目按业务需要手工补发
6. **不要宣称旧版无损**——它没有逐实例语义

## 6. 出站 URL 安全

- RSS/媒体：仅 http/https；私网/回环/链路本地/保留地址拦截；重定向逐跳校验；响应 ≤8MB
- 管理员显式例外：`fetch.allow_private_urls=true`（仅限内网 AI 网关等明确用途）
- AI 网关地址由管理员在 AI 设置中配置，属于受信配置（不做私网拦截）

## 7. 健康检查

`GET /api/healthz` — 匿名可访问，仅返回 `{"ok":true}`，不含配置/路径/版本。

## 8. 测试

```bash
# 离线（推荐，CI 同款）：临时库 + 替身 + 禁用外联
python offline_test.py

# 集成（对运行实例；会真实调用 AI、写生产日志）
XINTEL_TOKEN=<admin token> python healthcheck.py
XINTEL_TOKEN=<admin token> python smoke_test.py --nolive
```
