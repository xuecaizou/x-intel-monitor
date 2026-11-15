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
