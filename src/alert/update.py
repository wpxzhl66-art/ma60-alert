"""日线增量更新：为 data/daily 全部在市标的追加最近若干交易日（baostock，后复权）。

hfq 价为自上市起的累计复权序列，追加新段与既有数据天然衔接，无需全量重取。
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

import datasource  # noqa: F401  确保 patch_baostock_io 生效

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "daily"
_FIELDS = "date,close,turn,amount"

_SRC = None


def _init_worker():
    global _SRC
    datasource.patch_baostock_io(45.0)
    from datasource import BaostockSource
    _SRC = BaostockSource(segment="single")
    _SRC._ensure_login()


def _update_one(code: str) -> tuple[str, str, int]:
    f = DATA / f"{code.replace('.', '_')}.csv.gz"
    if not f.exists():
        return (code, "missing", 0)
    try:
        df = pd.read_csv(f, parse_dates=["date"])
        last = df["date"].max()
        if (pd.Timestamp(date.today()) - last).days <= 1:
            return (code, "fresh", 0)
        start = (last - timedelta(days=10)).strftime("%Y-%m-%d")
        rs = _SRC.fetch_daily(code, start, None)
        if rs.empty:
            return (code, "no_new", 0)
        before = len(df)
        df = (pd.concat([df, rs[["date", "close", "turn", "amount"]]])
                .drop_duplicates("date").sort_values("date"))
        df.to_csv(f, index=False, compression="gzip")
        return (code, "ok", len(df) - before)
    except Exception as exc:  # noqa: BLE001
        return (code, f"error:{type(exc).__name__}", 0)


def update_recent(codes: list[str] | None = None, workers: int = 16) -> pd.DataFrame:
    if codes is None:
        p = DATA.parent / "universe.csv.gz"
        codes = pd.read_csv(p, dtype=str)["code"].tolist()
    t0 = time.time()
    results = []
    with Pool(workers, initializer=_init_worker) as pool:
        for i, r in enumerate(pool.imap_unordered(_update_one, codes), 1):
            results.append(r)
            if i % 500 == 0 or i == len(codes):
                print(f"  更新 {i}/{len(codes)}  {time.time()-t0:.0f}s", flush=True)
    return pd.DataFrame(results, columns=["code", "status", "added"])
