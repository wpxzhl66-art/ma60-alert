"""Server酱推送（含每日额度护栏与重试）。"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import requests

from .config import load, state_dir

OUT = Path(__file__).resolve().parents[2] / "output" / "alert"
API = "https://sctapi.ftqq.com/{key}.send"


def _state_path() -> Path:
    return state_dir() / "push_state.json"


def _today_count() -> int:
    p = _state_path()
    if p.exists():
        s = json.loads(p.read_text(encoding="utf-8"))
        if s.get("date") == date.today().isoformat():
            return int(s.get("count", 0))
    return 0


def _bump(n: int = 1):
    s = {"date": date.today().isoformat(), "count": _today_count() + n}
    _state_path().write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")


def push_enabled() -> bool:
    """推送总开关（config ``push.enabled``）。

    关掉它只停「发送」，扫描 / 出图 / 看板 / 历史台账都不受影响——
    它们是 prepare 阶段的事，与推送无关。
    """
    cfg = load().get("push") or {}
    return bool(cfg.get("enabled", True))


def remaining_quota() -> int:
    cfg = load()["serverchan"]
    return max(0, int(cfg.get("daily_limit", 5)) - _today_count())


def send(title: str, desp: str, dry_run: bool | None = None) -> bool:
    """推送一条消息。额度不足返回 False；网络失败重试 3 次。

    dry_run=True（或环境变量 ALERT_DRYRUN=1）时只打印不发送，用于链路验证。
    总开关关闭（push.enabled=false）时直接返回 False，不消耗额度。
    """
    import os
    if not push_enabled():
        print(f"[pusher] 推送已停用（push.enabled=false），跳过发送：{title}")
        return False
    cfg = load()["serverchan"]
    if dry_run is None:
        dry_run = os.environ.get("ALERT_DRYRUN") == "1"
    key = os.environ.get("ALERT_SENDKEY") or cfg["sendkey"]   # 云端用 Secrets
    if remaining_quota() <= 0:
        print(f"[pusher] 今日额度已用尽（{cfg.get('daily_limit')}条），跳过：{title}")
        return False
    if dry_run:
        print(f"[pusher][dry-run] 跳过实际发送：{title}")
        print(desp[:500])
        _bump()
        return True
    for attempt in range(3):
        try:
            r = requests.post(API.format(key=key),
                              data={"title": title, "desp": desp}, timeout=30)
            ok = (r.status_code == 200
                  and r.json().get("code") == 0)
            if ok:
                _bump()
                return True
            print(f"[pusher] 第{attempt+1}次失败: {r.status_code} {r.text[:120]}")
        except Exception as exc:  # noqa: BLE001
            print(f"[pusher] 第{attempt+1}次异常: {type(exc).__name__} {exc}")
    return False
