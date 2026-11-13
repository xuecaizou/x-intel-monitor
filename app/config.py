"""X-Intel 配置加载：默认值 <- config.yaml <- data/runtime.json 三层合并"""
import copy
import datetime
import threading
import time
import json
import os
import uuid
from pathlib import Path

import yaml

_rt_lock = threading.Lock()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

DEFAULTS = {
    "server": {"host": "0.0.0.0", "port": 8083},  # 0.0.0.0 = 局域网可访问
    "fetch": {"interval_minutes": 15, "timeout_seconds": 25, "retries": 1},
    # 第五批：恢复/演练场景默认暂停调度与外发（显式开启才运行）
    "scheduler": {"enabled": True},
    "outbound": {"enable": True, "trusted_proxy": False, "allow_private": {}},
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
    # 报告编写工作台（协作写作）
    "word": {
        "pandoc_path": "",            # pandoc 可执行文件绝对路径（空=禁用 Word 导出并提示）
        "timeout_seconds": 120,       # 单次转换超时
        "max_md_bytes": 2000000,      # 正文上限
    },
    "docs": {
        "max_asset_bytes": 8000000,   # 受控本地图片上限
    },
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
    if ensure_instance_ids(cfg):
        save_runtime({"channels": cfg.get("channels") or {}})
    return cfg


def ensure_instance_ids(cfg):
    """为每个通道实例补稳定身份 iid（ci_+12hex，不随改名/排序变化；删除重建生成新 iid）
    与 enabled_since 投递边界（迁移时=当前时间，即默认不补发历史；新实例=创建时刻）。
    返回是否有变更（有则由 load() 持久化）。"""
    changed = False
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for cc in (cfg.get("channels") or {}).values():
        insts = cc.get("instances") if isinstance(cc, dict) else None
        if not isinstance(insts, list):
            continue
        for it in insts:
            if not isinstance(it, dict):
                continue
            if not it.get("iid"):
                it["iid"] = "ci_" + uuid.uuid4().hex[:12]
                changed = True
            if not it.get("enabled_since"):
                it["enabled_since"] = now
                changed = True
    return changed


def _replace_with_retry(src, dst, attempts=6, delay=0.02):
    """os.replace 带 Windows 瞬时占用重试。

    Windows 上 Defender/索引服务可能短暂打开目标文件，导致 os.replace 抛
    PermissionError(WinError 5)——这在并发保存时已被实测复现（T8-1）。
    有界重试只吸收瞬时占用，目标被长期锁定时最终仍然抛出。"""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))


def save_runtime(overrides):
    """页面改动持久化到 data/runtime.json。
    并发保护：模块锁（单进程）+ 原子替换（唯一 tmp→os.replace 带瞬时占用重试）；
    写后校验；上一版本备份 .bak。"""
    DATA_DIR.mkdir(exist_ok=True)
    p = DATA_DIR / "runtime.json"
    with _rt_lock:
        data = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        _deep_merge(data, overrides)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        # 唯一临时名：同进程由模块锁串行；唯一名避免多进程/残留 .tmp 互踩
        tmp = p.with_suffix(f".json.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        if json.loads(tmp.read_text(encoding="utf-8")) != data:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("runtime.json 写后校验失败")
        try:
            if p.exists():
                bak = p.with_suffix(".json.bak")
                for i in range(3):
                    try:
                        bak.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
                        break
                    except PermissionError:
                        if i == 2:
                            raise
                        time.sleep(0.02 * (i + 1))
            _replace_with_retry(tmp, p)
        finally:
            tmp.unlink(missing_ok=True)
