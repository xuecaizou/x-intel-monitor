"""X-Intel 配置加载：默认值 <- config.yaml <- data/runtime.json 三层合并"""
import copy
import json
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

DEFAULTS = {
    "server": {"host": "0.0.0.0", "port": 8083},  # 0.0.0.0 = 局域网可访问
    "fetch": {"interval_minutes": 15, "timeout_seconds": 25, "retries": 1},
    "network": {"proxy": "socks5://127.0.0.1:10808"},  # X 域名被 DNS 污染，必须走代理
    "x": {
        "strategy": "auto",                 # auto: syndication → Nitter → Cookie 直连
        "proxy": "socks5://127.0.0.1:10808",  # X 域名被 DNS 污染，必须走代理；留空回退 network.proxy
        "show_replies": True,
        "max_items": 20,                    # 每账号每轮最多入库条数
        "first_fetch_limit": 10,            # 新账号首次抓取的历史回溯条数
        "nitter_instances": ["nitter.meowing.monster"],
        "auth_token": "",                   # 可选：X 登录 Cookie（第三级直连）
        "ct0": "",
    },
    "ai": {
        "enabled": False,                   # 配好 Key 后在页面打开
        "base_url": "https://api.deepseek.com/v1",  # OpenAI 兼容接口
        "api_key": "",
        "model": "deepseek-chat",
        "auto_analyze": True,               # 新推文自动研判+翻译
        "auto_limit": 10,                   # 每轮自动处理的最多条数（控成本）
        "max_tokens": 4096,  # 思维链模型思维链+正文共用额度，1200 会被截到空 content
        "proxy": "",                        # AI 接口可选代理（国内直连厂商留空）
    },
    "channels": {
        "wecom": {"enabled": True, "webhook": ""},
        "dingtalk": {"enabled": True, "webhook": "", "secret": ""},
        "pushplus": {"enabled": False, "token": ""},
        "wxpusher": {"enabled": False, "app_token": "", "uid": ""},
    },
    "push": {"digest_max_items": 8},
}


def _deep_merge(dst, src):
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


def load():
    cfg = copy.deepcopy(DEFAULTS)
    yml = BASE_DIR / "config.yaml"
    if not yml.exists():
        yml = BASE_DIR / "config.example.yaml"
    if yml.exists():
        try:
            _deep_merge(cfg, yaml.safe_load(yml.read_text(encoding="utf-8")))
        except Exception as e:
            print("[config] 配置解析失败，使用默认配置:", e)
    runtime = DATA_DIR / "runtime.json"
    if runtime.exists():
        try:
            _deep_merge(cfg, json.loads(runtime.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_runtime(overrides):
    """页面改动（通道凭据/AI Key/开关）持久化到 data/runtime.json"""
    DATA_DIR.mkdir(exist_ok=True)
    p = DATA_DIR / "runtime.json"
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    _deep_merge(data, overrides)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
