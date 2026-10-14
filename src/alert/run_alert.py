"""实时预警系统入口。

本地模式：
  python -m alert.run_alert --test              # 通道测试推送
  python -m alert.run_alert --scan              # 立即扫描一次（盘中预警）
  python -m alert.run_alert --confirm           # 收盘确认批
  python -m alert.run_alert --update            # 日线增量更新 + 快照重建
  python -m alert.run_alert --daemon            # 常驻：按配置时刻自动执行
  python -m alert.run_alert --demo              # 离线演示（模拟+2%，只打印）

云端模式（GitHub Actions 三段式：prepare → git commit → notify）：
  python -m alert.run_alert --is-trading-day    # 交易日校验（exit 0/42）
  python -m alert.run_alert --prepare --cloud [--demo]   # 判定+出图，不推送
  python -m alert.run_alert --notify --sha <commit_sha>  # 组装URL并推送
  python -m alert.run_alert --update-cloud      # 云端数据仓增量更新（store/）
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np

from . import pusher, quoter, scanner, template
from .config import load
from .ma60_base import build_snapshot, load_snapshot

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output" / "alert"
PENDING = OUT / "pending.json"
PENDING_CSV = OUT / "pending.csv"
MSG_FIELDS = ["name", "price", "preclose", "chg_pct", "dev_pct", "slope",
              "alignment", "a_events_1y", "industry", "mv_float_yi", "amt_ratio"]


# ---------------------------------------------------------------- 通用
def _chart_enabled() -> bool:
    base = load()["jump"].get("chart_base_url", "")
    return bool(base) and "USER/REPO" not in base


def _local_weekly(code: str):
    import pandas as pd
    f = ROOT / "data" / "daily" / f"{code.replace('.', '_')}.csv.gz"
    if not f.exists():
        return None
    df = pd.read_csv(f, parse_dates=["date"])
    s = df.set_index("date")["close"]
    return s.groupby(s.index.to_period("W")).last()


def _store_weekly_map():
    from .cloud_store import load_weekly
    wk = load_weekly()
    if not len(wk):
        return {}
    return {c: g.set_index("week_end")["close"] for c, g in wk.groupby("code")}


def _get_snapshot(force: bool = False, cloud: bool = False):
    if cloud:
        from .cloud_store import build_snapshot_from_store
        snap = build_snapshot_from_store()
        print(f"[snapshot] 云端仓快照：{len(snap)} 只")
        return snap
    snap = None if force else load_snapshot()
    if snap is None:
        print("[snapshot] 缓存缺失或过期，重建中（约 2~4 分钟）...")
        snap = build_snapshot()
    return snap


def _evaluate(cloud: bool, demo: bool, force_snapshot: bool):
    snap = _get_snapshot(force_snapshot, cloud)
    quotes = quoter.fetch_quotes(snap.index.tolist())
    if demo:
        quotes["price"] = quotes["preclose"] * 1.02
        quotes["chg_pct"] = 2.0
    trig = scanner.evaluate(snap, quotes, mode="demo" if demo else "scan")
    return snap, trig


# ---------------------------------------------------------------- 本地
def run_scan(mode: str = "intraday", push: bool = True, force_snapshot: bool = False):
    snap = _get_snapshot(force_snapshot)
    quotes = quoter.fetch_quotes(snap.index.tolist())
    trig = scanner.evaluate(snap, quotes, mode=mode)
    print(f"[{mode}] 触发 {len(trig)} 只")
    if len(trig):
        print(trig[MSG_FIELDS].round(3).to_string())
    if push and len(trig):
        title, desp = template.build_message(trig, mode)
        if pusher.send(title, desp):
            scanner.mark_pushed(trig.index.tolist(), mode)
            print(f"[push] 已推送：{title}")
    elif push and mode == "confirm":
        title, desp = template.build_message(trig, mode)
        if pusher.send(title, desp):
            print(f"[push] 已推送：{title}")
    return trig


def run_update():
    from .update import update_recent
    res = update_recent()
    print(res["status"].value_counts().to_dict())
    print("[snapshot] 重建快照...")
    build_snapshot()
    print("[update] 完成")


def run_daemon():
    cfg = load()["scan"]
    schedule = sorted(set(cfg["intraday_times"] + [cfg["close_confirm"], cfg["daily_update"]]))
    print(f"[daemon] 时刻表 {schedule}（Ctrl+C 退出）")
    done: set[str] = set()
    while True:
        now = datetime.now()
        hhmm = now.strftime("%H:%M")
        if hhmm in schedule and hhmm not in done:
            try:
                mdate = quoter.market_date()
                is_trading = mdate == now.date().isoformat()
                if hhmm == cfg["daily_update"]:
                    run_update()
                elif hhmm == cfg["close_confirm"]:
                    if is_trading:
                        run_scan("confirm")
                    else:
                        print("[daemon] 非交易日/无行情，跳过收盘确认")
                else:
                    if is_trading:
                        run_scan("intraday")
                    else:
                        print("[daemon] 非交易时段，跳过")
            except Exception as exc:  # noqa: BLE001
                print(f"[daemon] 任务异常: {type(exc).__name__} {exc}")
            done.add(hhmm)
        if hhmm == "00:00":
            done.clear()
        time.sleep(20)


def run_test():
    key_ok = pusher.send(
        "预警系统自检（系统已就绪）",
        "实时预警系统部署自检通过。\n\n"
        "- Server酱通道：**正常**\n"
        "- 触发口径：昨收<60周线 且 现价≥60周线（严格）\n\n"
        f"网页行情示例（贵州茅台 600519）：\n\n{template.ths_links('sh.600519')}")
    print("测试推送", "成功" if key_ok else "失败")


def run_demo(cloud: bool = False):
    snap, trig = _evaluate(cloud=cloud, demo=True, force_snapshot=False)
    print(f"[demo] 模拟全市场+2%：触发 {len(trig)} 只")
    if len(trig):
        print(trig[MSG_FIELDS].head(20).round(3).to_string())
        title, desp = template.build_message(trig.head(3), "demo")
        print("\n===== 推送文案预览 =====")
        print("标题:", title)
        print(desp[:1000])


# ---------------------------------------------------------------- 云端
def run_is_trading_day() -> int:
    mdate = quoter.market_date()
    today = date.today().isoformat()
    print(f"行情交易日={mdate}  今天={today}")
    return 0 if mdate == today else 42


def run_prepare(cloud: bool = True, demo: bool = False, mode: str = "scan"):
    """云端第一段：判定 + 出图，结果落 pending，不推送。"""
    snap, trig = _evaluate(cloud=cloud, demo=demo, force_snapshot=False)
    day = date.today().isoformat()
    result = {"mode": "demo" if demo else mode,
              "day": day, "n": int(len(trig)), "charts": {}}
    if len(trig):
        trig_out = trig[MSG_FIELDS].copy()
        trig_out.to_csv(PENDING_CSV)
        if _chart_enabled():
            from .chart import render_weekly_chart
            wmap = _store_weekly_map() if cloud else None
            for code in trig_out.index:
                weekly = wmap.get(code) if wmap else None
                if weekly is None and not cloud:
                    weekly = _local_weekly(code)
                if weekly is None:
                    continue
                name = str(trig_out.at[code, "name"])
                out = ROOT / "charts" / day / f"{code.split('.')[1]}.png"
                try:
                    render_weekly_chart(code, name, weekly, out)
                    result["charts"][code] = out.relative_to(ROOT).as_posix()
                except Exception as exc:  # noqa: BLE001
                    print(f"[chart] {code} 渲染失败: {exc}")
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    PENDING.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                       encoding="utf-8")
    print(f"[prepare] {result['n']} 只待推，图表 {len(result['charts'])} 张 → {PENDING}")


def run_notify(sha: str):
    """云端第三段：读 pending，组装带 commit-sha 的图表 URL 并推送。"""
    if not PENDING.exists():
        print("[notify] 无 pending，跳过")
        return
    res = json.loads(PENDING.read_text(encoding="utf-8"))
    trig = None
    if res["n"] and PENDING_CSV.exists():
        import pandas as pd
        trig = pd.read_csv(PENDING_CSV, index_col="code")
        base = load()["jump"].get("chart_base_url", "").rstrip("/")
        chart_urls = {code: f"{base}@{sha}/{rel}"
                      for code, rel in res["charts"].items()}
        title, desp = template.build_message(trig, res["mode"], chart_urls)
    else:
        title, desp = template.build_message(trig, res["mode"])
    if pusher.send(title, desp):
        if trig is not None:
            scanner.mark_pushed(trig.index.tolist(), res["mode"])
        print(f"[notify] 已推送：{title}")


def run_update_cloud():
    from .cloud_store import update_from_baostock
    update_from_baostock()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--demo", action="store_true")
    # 云端
    ap.add_argument("--is-trading-day", action="store_true")
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--update-cloud", action="store_true")
    ap.add_argument("--cloud", action="store_true", help="用 store 精简仓做快照")
    ap.add_argument("--mode", default="scan", choices=["scan", "confirm"])
    ap.add_argument("--sha", default="main")
    ap.add_argument("--force-snapshot", action="store_true")
    a = ap.parse_args()

    if a.test:
        run_test()
    elif a.scan:
        run_scan("intraday", force_snapshot=a.force_snapshot)
    elif a.confirm:
        run_scan("confirm", force_snapshot=a.force_snapshot)
    elif a.update:
        run_update()
    elif a.daemon:
        run_daemon()
    elif a.is_trading_day:
        raise SystemExit(run_is_trading_day())
    elif a.prepare:
        run_prepare(cloud=a.cloud, demo=a.demo, mode=a.mode)
    elif a.notify:
        run_notify(a.sha)
    elif a.update_cloud:
        run_update_cloud()
    elif a.demo:
        run_demo(cloud=a.cloud)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
