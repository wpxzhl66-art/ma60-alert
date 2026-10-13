"""口径核对：独立重算若干事件，与主流程输出交叉验证。

目的：主流程（run_study.py）用 engine.py 的函数链计算，本脚本用**另一条
独立代码路径**（直接读原始日线、手工滚动均线、手工定位收益）重算，
两者一致才说明口径无误。

核对项：
    1. 周线合成：事件周收盘 == 该周最后一个交易日收盘
    2. MA60：事件周及前一周的 60 周均线值
    3. 事件判定：是否同时满足类型 A / 类型 B 的定义
    4. 前瞻收益：entry→t0+N 的收益与明细表 ret{N} 一致
    5. 无未来函数：仅用事件周及之前的数据算标签

用法：
    python src/verify_events.py [--n 3]
"""
from __future__ import annotations

import argparse
import glob
import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "output"
BAND = 0.02
MA_WEEKS = 60
HORIZONS = (20, 40, 60, 120, 250)


def load_detail():
    p = OUT / "events_detail.csv.gz"
    if not p.exists():
        print("缺少 output/events_detail.csv.gz")
        return None
    return pd.read_csv(p, parse_dates=["event_date"])


def load_stock(code: str) -> pd.DataFrame:
    f = DATA / "daily" / f"{code.replace('.', '_')}.csv.gz"
    df = pd.read_csv(f, parse_dates=["date"]).sort_values("date").reset_index(drop=True)
    return df


def weekly_close(daily: pd.DataFrame) -> pd.DataFrame:
    """独立实现：按 ISO 周分组取最后交易日收盘。"""
    d = daily.copy()
    iso = d["date"].dt.isocalendar()
    d["y"] = iso["year"].to_numpy()
    d["w"] = iso["week"].to_numpy()
    g = d.groupby(["y", "w"], sort=True).agg(
        last_date=("date", "last"), close=("close", "last")).reset_index()
    g["ma60"] = g["close"].rolling(MA_WEEKS).mean()
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3, help="每种类型抽检的事件数")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    detail = load_detail()
    if detail is None:
        return 1

    rng = random.Random(args.seed)
    print("=" * 78)
    print("口径核对：独立重算 vs 主流程输出")
    print("=" * 78)

    n_pass = n_fail = 0

    def rep(ok: bool, msg: str):
        nonlocal n_pass, n_fail
        tag = "PASS" if ok else "FAIL"
        if ok:
            n_pass += 1
        else:
            n_fail += 1
        print(f"  [{tag}] {msg}")

    for etype in ("A_上穿突破", "B_上方回踩"):
        pool = detail[detail["event_type"] == etype]
        if pool.empty:
            continue
        picks = rng.sample(range(len(pool)), min(args.n, len(pool)))
        for k in picks:
            r = pool.iloc[k]
            code = r["code"]
            ed = pd.Timestamp(r["event_date"])
            print(f"\n--- {etype} | {code} | 事件周 {ed.date()} ---")
            daily = load_stock(code)
            wk = weekly_close(daily)

            row = wk[wk["last_date"] == ed]
            if row.empty:
                # 事件周标记日若不在本脚本的 ISO 周聚合里，用最近一周
                row = wk[wk["last_date"] <= ed].tail(1)
                rep(False, f"事件周标记日 {ed.date()} 未在周线中找到（取最近一周替代）")
            i = int(row.index[0])

            # 1. 周收盘 == 该周最后一个交易日收盘
            wk_days = daily[(daily["date"] <= ed)]
            iso = ed.isocalendar()
            same_week = wk_days[
                (wk_days["date"].dt.isocalendar().year == iso.year)
                & (wk_days["date"].dt.isocalendar().week == iso.week)]
            ok_close = bool(
                abs(float(row["close"].iloc[0]) - float(same_week["close"].iloc[-1])) < 1e-9)
            rep(ok_close, f"周收盘 {row['close'].iloc[0]:.4f} == "
                          f"该周最后交易日({same_week['date'].iloc[-1].date()})收盘 "
                          f"{same_week['close'].iloc[-1]:.4f}")

            # 2. MA60 一致性
            my_ma = float(row["ma60"].iloc[0])
            ok_ma = abs(my_ma - float(r["entry_ma"])) < max(1e-6, abs(my_ma) * 1e-6)
            rep(ok_ma, f"MA60 独立重算 {my_ma:.4f} vs 明细 entry_ma {float(r['entry_ma']):.4f}")

            # 3. 事件判定
            if i >= 1:
                pc = float(wk["close"].iloc[i - 1])
                pm = float(wk["ma60"].iloc[i - 1])
                c = float(wk["close"].iloc[i])
                m = my_ma
                if etype == "A_上穿突破":
                    ok_ev = (pc < pm) and (c >= m)
                else:
                    ok_ev = (pc > pm * (1 + BAND)) and (m * (1 - BAND) <= c <= m * (1 + BAND))
                rep(ok_ev, f"事件条件：前周收盘 {pc:.4f} 前周MA {pm:.4f} → "
                           f"本周收盘 {c:.4f} 本周MA {m:.4f}")

            # 4. 前瞻收益
            cal = daily["date"].to_numpy()
            pos = int(np.searchsorted(cal, np.datetime64(ed)))
            if pos < len(cal) and cal[pos] != np.datetime64(ed):
                pos = pos  # 事件日不在交易日历上则取其后第一个交易日
            p0 = float(daily["close"].iloc[pos])
            for N in HORIZONS:
                col = f"ret{N}"
                if col not in detail.columns:
                    continue
                t1 = pos + N
                got = r[col]
                if t1 >= len(daily):
                    ok = pd.isna(got)
                    rep(ok, f"ret{N} 右删失（明细为空值）")
                    continue
                p1 = float(daily["close"].iloc[t1])
                exp = p1 / p0 - 1
                if pd.isna(got):
                    rep(False, f"ret{N} 明细为空但本地可算（期望 {exp:.4f}）")
                else:
                    ok = abs(exp - float(got)) < 1e-6
                    rep(ok, f"ret{N} 独立重算 {exp:.6f} vs 明细 {float(got):.6f}")

    print("\n" + "=" * 78)
    print(f"核对结果：通过 {n_pass} 项，失败 {n_fail} 项")
    print("=" * 78)
    return 0 if n_fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
