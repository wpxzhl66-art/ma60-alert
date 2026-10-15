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
CLOSE_MIN = 15 * 60   # 收盘线（15:00）：此后执行的扫描一律按「收盘确认」处理
KEEP_DAYS = 45        # 标记文件保留天数
MARKS = Path(__file__).resolve().parents[2] / "store" / "state" / "slot_marks.json"
UPDATE_MARKS = (Path(__file__).resolve().parents[2] / "store" / "state"
                / "update_marks.json")


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


def _decide_slot() -> tuple[str, int, bool]:
    """返回 ``(命中的槽位, 相对槽位的延迟分钟, 是否为迟补)``；未命中返回 ``("", 0, False)``。

    两级判定：
      1) 正常路径 —— 落在某槽位的 30 分钟窗口内；
      2) **兜底补扫** —— 当天一次都没跑成时，任何到得更晚的触发都补扫一次。
         这只是补漏：GitHub schedule 实测延迟 8~9 小时（10-15 全天 24 个网格点
         0 个准点到达），30 分钟窗口根本吸收不了；与其整天漏掉，不如晚扫。
         补扫同样会打标，一天最多补一次。
    """
    now = _now()
    today = now.strftime("%Y-%m-%d")
    now_m = now.hour * 60 + now.minute
    done = _load().get(today) or []
    for s in SLOTS:
        if s in done:
            continue
        delta = now_m - _minutes(s)
        if 0 <= delta <= WINDOW_MIN:
            return s, delta, False
    if not done and now_m >= _minutes(SLOTS[0]):
        late = max((s for s in SLOTS if _minutes(s) <= now_m),
                   key=_minutes, default=SLOTS[-1])
        return late, now_m - _minutes(late), True
    return "", 0, False


def _emit(out: dict) -> None:
    out_path = os.environ.get("GITHUB_OUTPUT")
    if out_path and out_path != os.devnull:
        with open(out_path, "a", encoding="utf-8") as fh:
            for k, v in out.items():
                fh.write(f"{k}={v}\n")


def decide() -> None:
    now = _now()
    now_m = now.hour * 60 + now.minute
    slot, delay, late = _decide_slot()
    # 模式按**实际执行时间**定，而不是按槽位名 —— 延迟到收盘后执行就该算收盘确认，
    # 否则晚上补扫出来的数据会被标成「盘中」，语义是错的。
    mode = "confirm" if now_m >= CLOSE_MIN else "intraday"
    _emit({"run": "true" if slot else "false", "slot": slot, "mode": mode})
    if slot:
        tail = (f"命中槽位 {slot}（延迟 {delay} 分钟"
                + ("，迟补" if late else "") + f"）→ 模式 {mode}")
    else:
        tail = "未命中，跳过本轮"
    print(f"[gate] 北京 {now:%H:%M} → {tail}", file=sys.stderr)


def decide_update() -> None:
    """16:07 数据更新轮的幂等闸门（BUG #2）。

    外接定时服务（cron-job.org）接管 16:07 后会与 GitHub cron 双触发；
    update 轮是全市场增量更新（约 1.5h 预算、5000+ 请求），重复跑有触发
    baostock 黑名单的风险。标记文件 store/state/update_marks.json 在
    更新**成功后**由 --mark-update 写入（随 store/ 一起提交），谁先跑完
    谁打标，后到者看到当日标记立即退出。
    """
    now = _now()
    today = now.strftime("%Y-%m-%d")
    try:
        marks = json.loads(UPDATE_MARKS.read_text(encoding="utf-8"))
        marks = marks if isinstance(marks, dict) else {}
    except (OSError, ValueError):
        marks = {}
    already = marks.get(today)
    _emit({"run": "false" if already else "true", "task": "update"})
    if already:
        print(f"[gate] update 轮：今日 {today} 已更新过（{already}），跳过",
              file=sys.stderr)
    else:
        print(f"[gate] update 轮：今日 {today} 尚未更新，放行", file=sys.stderr)


def mark_update() -> None:
    """标记今日数据更新已完成（成功后才调用）。"""
    now = _now()
    today = now.strftime("%Y-%m-%d")
    try:
        marks = json.loads(UPDATE_MARKS.read_text(encoding="utf-8"))
        marks = marks if isinstance(marks, dict) else {}
    except (OSError, ValueError):
        marks = {}
    cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    marks = {d: v for d, v in marks.items() if str(d) >= cutoff}
    marks[today] = now.strftime("%H:%M")
    UPDATE_MARKS.parent.mkdir(parents=True, exist_ok=True)
    UPDATE_MARKS.write_text(
        json.dumps(marks, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8")
    print(f"[gate] 已标记今日数据更新完成 @ {today} {marks[today]}", file=sys.stderr)


def main() -> None:
    if "--mark-update" in sys.argv:
        mark_update()
        return
    if "--mark" in sys.argv:
        idx = sys.argv.index("--mark")
        mark(sys.argv[idx + 1] if len(sys.argv) > idx + 1 else "")
        return
    task = "scan"
    if "--task" in sys.argv:
        idx = sys.argv.index("--task")
        task = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else "scan"
    if task == "update":
        decide_update()
    elif task == "demo":
        # 演示轮不设闸门，但**显式**输出 run=na：后续步骤不再依赖
        # 「闸门被跳过时输出恰好为空字符串」的脆弱语义（BUG #5）
        _emit({"run": "na", "slot": "", "mode": ""})
        print("[gate] demo 轮：不设闸门（run=na）", file=sys.stderr)
    else:
        decide()


if __name__ == "__main__":
    main()
