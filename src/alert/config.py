"""配置加载。基础配置 config/alert_config.yaml 可被
config/alert_config.local.yaml（不入库，存放密钥等敏感项）覆盖。
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CFG_PATH = ROOT / "config" / "alert_config.yaml"
LOCAL_PATH = ROOT / "config" / "alert_config.local.yaml"


def is_cloud() -> bool:
    """云端运行（GitHub Actions）时置 ALERT_CLOUD=1。
    状态文件随之落到 store/ 以跨运行持久化。"""
    return os.environ.get("ALERT_CLOUD") == "1"


def state_dir() -> Path:
    """状态文件目录：云端在 store/state，本地在 output/alert。"""
    if is_cloud():
        d = ROOT / "store" / "state"
    else:
        d = ROOT / "output" / "alert"
    d.mkdir(parents=True, exist_ok=True)
    return d


_cache: dict | None = None


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    global _cache
    if _cache is None:
        with open(CFG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        if LOCAL_PATH.exists():
            with open(LOCAL_PATH, encoding="utf-8") as f:
                cfg = _deep_merge(cfg, yaml.safe_load(f) or {})
        _cache = cfg
    return _cache
