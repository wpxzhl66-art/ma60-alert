"""Server酱推送（含每日额度护栏与重试）。"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import requests

from .config import load

OUT = Path(__file__).resolve().parents[2] / "output" / "alert"
_STATE = OUT / "push_state.json"
API = "https://sctapi.ftqq.com/{key}.send"


def _today_count() -> int:
    if _STATE.exists():
        s = json.loads(_STATE.read_text(encoding="utf-8"))
        if s.get("date") == date.today().isoformat():
            return int(s.get("count", 0))
    return 0


def _bump(n: int = 1):
    OUT.mkdir(parents=True, exist_ok=True)
    s = {"date": date.today().isoformat(), "count": _today_count() + n}
    _STATE.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")


def remaining_quota() -> int:
    cfg = load()["serverchan"]
    return max(0, int(cfg.get("daily_limit", 5)) - _today_count())


def send(title: str, desp: str) -> bool:
    """推送一条消息。额度不足返回 False；网络失败重试 3 次。"""
    import os
    cfg = load()["serverchan"]
    key = os.environ.get("ALERT_SENDKEY") or cfg["sendkey"]   # 云端用 Secrets
    if remaining_quota() <= 0:
        print(f"[pusher] 今日额度已用尽（{cfg.get('daily_limit')}条），跳过：{title}")
        return False
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
