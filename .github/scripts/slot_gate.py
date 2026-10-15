#!/usr/bin/env python3
"""交易槽位闸门。

GitHub Actions 的 schedule 事件会延迟数小时，也可能整条丢弃（2026-10-14 实测：
实际运行时间与 cron 定义完全不符；2026-10-15 上午两轮直接零 run）。因此
alert.yml 在交易时段内每 15 分钟放一个触发点，再把冗余触发点收敛回原定节奏：
只有当前北京时间落进某个目标槽位的窗口内、且该槽位今日未执行过，才放行扫描。
其余触发点立即退出，不安装依赖，单次开销约 10 秒。

用法：
    python3 slot_gate.py                 # 判定，结果写入 $GITHUB_OUTPUT
    python3 slot_gate.py --mark HH:MM    # 标记某个槽位今日已执行
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CN = timezone(timedelta(hours=8))
SLOTS = ("10:05", "11:05", "14:05", "14:55", "15:15")   # 北京时间
WINDOW_MIN = 30       # 槽位到点后 30 分钟内到达都算命中，用于吸收 schedule 延迟
KEEP_DAYS = 45        # 标记文件保留天数
MARKS = Path(__file__).resolve().parents[2] / "store" / "state" / "slot_marks.json"


def _now() -> datetime:
    return datetime.now(CN)


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _load() -> dict:
    try:
        data = json.loads(MARKS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    MARKS.parent.mkdir(parents=True, exist_ok=True)
    MARKS.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
                     encoding="utf-8")


def _prune(data: dict, today: str) -> dict:
    """只保留最近 KEEP_DAYS 天，避免文件无限增长。"""
    cutoff = datetime.strptime(today, "%Y-%m-%d") - timedelta(days=KEEP_DAYS)
    kept: dict[str, list[str]] = {}
    for day, slots in data.items():
        if not isinstance(slots, list):
            continue
        try:
            keep = datetime.strptime(day, "%Y-%m-%d") >= cutoff
        except ValueError:
            continue
        if keep:
            kept[day] = sorted({str(s) for s in slots})
    return kept


def mark(slot: str) -> None:
    if not slot:
        print("[gate] 槽位为空，不标记", file=sys.stderr)
        return
    now = _now()
    today = now.strftime("%Y-%m-%d")
    data = _prune(_load(), today)
    slots = set(data.get(today) or [])
    slots.add(slot)
    data[today] = sorted(slots)
    _save(data)
    print(f"[gate] 已标记槽位 {slot} @ {now:%Y-%m-%d %H:%M}", file=sys.stderr)


def _decide_slot() -> tuple[str, int]:
    """返回 (命中的槽位, 相对槽位的延迟分钟)；未命中返回 ("", 0)。"""
    now = _now()
    today = now.strftime("%Y-%m-%d")
    now_m = now.hour * 60 + now.minute
    done = _load().get(today) or []
    for s in SLOTS:
        if s in done:
            continue
        delta = now_m - _minutes(s)
        if 0 <= delta <= WINDOW_MIN:
            return s, delta
    return "", 0


def decide() -> None:
    now = _now()
    slot, delay = _decide_slot()
    out_path = os.environ.get("GITHUB_OUTPUT")
    if out_path and out_path != os.devnull:
        with open(out_path, "a", encoding="utf-8") as fh:
            fh.write(f"run={'true' if slot else 'false'}\n")
            fh.write(f"slot={slot}\n")
    tail = f"命中槽位 {slot}（延迟 {delay} 分钟）" if slot else "未命中，跳过本轮"
    print(f"[gate] 北京 {now:%H:%M} → {tail}", file=sys.stderr)


def main() -> None:
    if "--mark" in sys.argv:
        idx = sys.argv.index("--mark")
        mark(sys.argv[idx + 1] if len(sys.argv) > idx + 1 else "")
        return
    decide()


if __name__ == "__main__":
    main()
