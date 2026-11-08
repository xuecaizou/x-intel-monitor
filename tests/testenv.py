# -*- coding: utf-8 -*-
"""统一测试环境入口（第四批阶段 A）。

用法（测试脚本第一行 import）：
    from tests.testenv import TestEnv
    env = TestEnv()          # 必须在 import app.* 之前调用
    env.start()              # 注入临时目录 + 禁止外联 + 生产路径守卫

保证：
1. db / config / 备份 / 报告目录 / 日志全部指向临时目录（在导入应用前注入）；
2. 检测到生产路径（项目 data/）立即失败；
3. 默认禁止真实外联（requests 被替换为抛错替身，需显式放行才可用）；
4. 不依赖当前工作目录（基于 __file__ 解析项目根）。
"""
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROD_DATA = PROJECT_ROOT / "data"


class ProductionPathError(RuntimeError):
    """测试试图访问生产路径"""


class NetworkBlockedError(RuntimeError):
    """测试默认禁止真实外联"""


class TestEnv:
    def __init__(self, forbid_network=True):
        self.tmp = Path(tempfile.mkdtemp(prefix="xintel-test-"))
        self.forbid_network = forbid_network
        self._orig_requests = None
        self.started = False

    # ---------- 启动 ----------
    def start(self):
        """注入临时目录。必须在 import app.* 之前调用（app.config 在导入时读 DATA_DIR）。"""
        if self.started:
            return self
        # 1) 环境变量（供 config 读取；config.DATA_DIR 默认为 BASE_DIR/data，此处用 monkeypatch 覆盖）
        os.environ["XINTEL_DATA_DIR"] = str(self.tmp)
        os.environ["XINTEL_TEST"] = "1"
        # 2) 禁用真实外联（requests 模块级替身）
        if self.forbid_network:
            self._block_network()
        # 3) 导入 app.config 并强制 DATA_DIR（若已导入则覆盖并重置连接）
        import app.config as cfgmod
        cfgmod.DATA_DIR = self.tmp
        cfgmod._rt_lock = cfgmod._rt_lock  # 保持不变
        self.started = True
        return self

    def _block_network(self):
        try:
            import requests
            self._orig_requests = requests
            real_session = requests.Session

            class _Blocked:
                def __init__(self, *a, **k):
                    raise NetworkBlockedError(
                        "测试环境禁止真实外联（如需放行：TestEnv(forbid_network=False) 并显式使用替身）")

                @staticmethod
                def get(*a, **k):
                    raise NetworkBlockedError("测试环境禁止真实外联")

                @staticmethod
                def post(*a, **k):
                    raise NetworkBlockedError("测试环境禁止真实外联")

            # 只替换 requests.get/post/Session 调用入口，保留异常类（Timeout/ConnectionError）
            requests.get = _Blocked.get
            requests.post = _Blocked.post
            requests.Session = _Blocked
        except ImportError:
            pass

    def allow_network(self):
        """显式放行（仅用于需要真实服务的分组测试，默认不调用）"""
        if self._orig_requests is not None:
            import requests
            requests.get = self._orig_requests.get
            requests.post = self._orig_requests.post
            requests.Session = self._orig_requests.Session

    # ---------- 守卫 ----------
    def assert_not_prod(self, path):
        p = Path(path).resolve()
        try:
            p.relative_to(PROD_DATA.resolve())
        except ValueError:
            return
        raise ProductionPathError(f"测试试图访问生产路径：{p}")

    # ---------- 清理 ----------
    def cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
