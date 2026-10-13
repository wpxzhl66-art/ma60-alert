"""备选数据源测试：新浪（akshare stock_zh_a_daily）的后复权正确性与速度。

判据与 baostock 核验一致：比例复权的「后复权/不复权」比值必须是**阶梯函数**，
只在除权日跳变。若比值高频漂移，则该复权实现不可用于收益计算。
"""
from __future__ import annotations

import time

import akshare as ak
import numpy as np
import pandas as pd

OUT = "output/sina_check.txt"
lines: list[str] = []


def log(s: str):
    print(s, flush=True)
    lines.append(s)


def check(symbol: str = "sh600000"):
    t0 = time.time()
    try:
        raw = ak.stock_zh_a_daily(symbol=symbol, start_date="20100101",
                                  end_date="20261013", adjust="")
        t_raw = time.time() - t0
    except Exception as exc:  # noqa: BLE001
        log(f"{symbol} 不复权取数失败: {type(exc).__name__}: {exc}")
        return

    t1 = time.time()
    try:
        hfq = ak.stock_zh_a_daily(symbol=symbol, start_date="20100101",
                                  end_date="20261013", adjust="hfq")
        t_hfq = time.time() - t1
    except Exception as exc:  # noqa: BLE001
        log(f"{symbol} 后复权取数失败: {type(exc).__name__}: {exc}")
        return

    log(f"{symbol}  不复权 {len(raw)} 条 {t_raw:.1f}s / 后复权 {len(hfq)} 条 {t_hfq:.1f}s")
    log(f"  不复权列: {list(raw.columns)}")
    log(f"  后复权列: {list(hfq.columns)}")

    a = raw[["date", "close"]].copy()
    b = hfq[["date", "close"]].copy()
    a["date"] = pd.to_datetime(a["date"])
    b["date"] = pd.to_datetime(b["date"])
    m = a.merge(b, on="date", suffixes=("_raw", "_hfq")).dropna()
    m = m[m["close_raw"] > 0]
    if m.empty:
        log("  无重叠数据，无法比对")
        return

    m["ratio"] = m["close_hfq"] / m["close_raw"]
    uniq = m["ratio"].round(8).nunique()
    chg = m["ratio"].round(8).diff().fillna(0) != 0
    n_chg = int(chg.sum())
    runs = m["ratio"].round(8).ne(m["ratio"].round(8).shift()).cumsum()
    max_const = int(m.groupby(runs).size().max())
    jumps = m.loc[chg, "date"].dt.strftime("%Y-%m-%d").tolist()

    log(f"  比值取值数 {uniq}   变化日 {n_chg} 天（{n_chg/len(m):.1%}）  最长恒定 {max_const} 天")
    log(f"  变化日: {jumps[:30]}{' ...' if len(jumps) > 30 else ''}")
    log(f"  比值首/中/末: {m['ratio'].iloc[0]:.6f} / {m['ratio'].iloc[len(m)//2]:.6f} / {m['ratio'].iloc[-1]:.6f}")
    log(f"  首日收盘: 不复权 {m['close_raw'].iloc[0]}  后复权 {m['close_hfq'].iloc[0]}")

    verdict = "阶梯函数（正确 ✅）" if (uniq <= 60 and max_const >= 20) else "高频漂移（异常 ❌）"
    log(f"  判定: {verdict}")


if __name__ == "__main__":
    log("=" * 60)
    log("新浪接口 后复权正确性核验")
    log("=" * 60)
    for sym in ["sh600000", "sz000001"]:
        check(sym)
        log("-" * 60)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
