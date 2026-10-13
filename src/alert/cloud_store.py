"""云端精简数据仓（GitHub Actions 部署用）。

全量日线（~440MB）不适合放仓库；云端只维护三件小事：
  store/weekly_closes.csv.gz  各股最近 ~130 个已完成周的周收盘（后复权）
  store/last_close.csv        各股最近交易日收盘（后复权）+ 日期（复权因子用）
  store/avg_amt20.csv         近 20 日平均成交额（元，不复权）

快照判定与本地完全共用 ma60_base.stock_from_weekly()。
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

import datasource  # noqa: F401  确保 patch_baostock_io 生效

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "store"
F_WEEKLY = STORE / "weekly_closes.csv.gz"
F_LAST = STORE / "last_close.csv"
F_AMT = STORE / "avg_amt20.csv"

KEEP_WEEKS = 130          # 仓内保留周数（判定需 61+13，留足余量）
FETCH_BUFFER_DAYS = 50    # 增量抓取回看天数（覆盖 20 日均额）

_SRC = None


# ---------------------------------------------------------------- 读仓
def load_weekly() -> pd.DataFrame:
    if F_WEEKLY.exists():
        return pd.read_csv(F_WEEKLY, parse_dates=["week_end"])
    return pd.DataFrame(columns=["code", "week_end", "close"])


def save_weekly(df: pd.DataFrame):
    STORE.mkdir(parents=True, exist_ok=True)
    df.to_csv(F_WEEKLY, index=False, compression="gzip")


def load_last_close() -> pd.DataFrame:
    if F_LAST.exists():
        return pd.read_csv(F_LAST, parse_dates=["last_date"]).set_index("code")
    return pd.DataFrame(columns=["hfq_last_close", "last_date"])


def load_amt20() -> pd.Series:
    if F_AMT.exists():
        return pd.read_csv(F_AMT, index_col="code")["avg_amt20"]
    return pd.Series(dtype=float)


# ---------------------------------------------------------------- 播种（本地）
def seed_from_local(progress: bool = True):
    """用本地 data/daily 全量数据播种 store（一次性操作）。"""
    import glob

    from .ma60_base import cutoff_monday, stock_from_weekly

    uni = None
    if (ROOT / "data" / "universe.csv.gz").exists():
        uni = pd.read_csv(ROOT / "data" / "universe.csv.gz", dtype=str).set_index("code")

    weekly_rows, last_rows, amt_rows = [], [], []
    files = sorted(glob.glob(str(ROOT / "data" / "daily" / "*.csv.gz")))
    cutoff = cutoff_monday()
    for i, f in enumerate(files, 1):
        code = Path(f).name.replace(".csv.gz", "").replace("_", ".", 1)
        if uni is not None and code in uni.index and str(uni.at[code, "status"]) != "1":
            continue
        try:
            df = pd.read_csv(f, parse_dates=["date"])[["date", "close", "amount"]]
        except Exception:  # noqa: BLE001
            continue
        s = df.set_index("date")["close"]
        r = stock_from_weekly(code, s, cutoff)
        if r is None:
            continue
        w = r.pop("weekly_closes").tail(KEEP_WEEKS)
        weekly_rows.append(pd.DataFrame({
            "code": code, "week_end": w.index.to_timestamp(), "close": w.values}))
        last = df.sort_values("date").iloc[-1]
        last_rows.append({"code": code, "hfq_last_close": float(last["close"]),
                          "last_date": last["date"]})
        d20 = df.sort_values("date").tail(20)
        amt_rows.append({"code": code, "avg_amt20": float(d20["amount"].mean())})
        if progress and i % 1000 == 0:
            print(f"  播种 {i}/{len(files)}", flush=True)

    save_weekly(pd.concat(weekly_rows, ignore_index=True))
    pd.DataFrame(last_rows).to_csv(F_LAST, index=False)
    pd.DataFrame(amt_rows).to_csv(F_AMT, index=False)
    print(f"[store] 播种完成：周线 {sum(len(x) for x in weekly_rows):,} 行，"
          f"标的 {len(last_rows)} 只")


# ---------------------------------------------------------------- 云端增量
def _init_worker():
    global _SRC
    datasource.patch_baostock_io(45.0)
    from datasource import BaostockSource
    _SRC = BaostockSource(segment="single")
    _SRC._ensure_login()


def _fetch_one(code: str, start: str):
    global _SRC
    for attempt in range(3):
        try:
            df = _SRC.fetch_daily(code, start, None)
            return code, df[["date", "close", "amount"]] if not df.empty else None
        except Exception:  # noqa: BLE001
            try:                       # 会话失效重建
                _SRC.close()
            except Exception:  # noqa: BLE001
                pass
            _init_worker()
    return code, None


def update_from_baostock(workers: int = 16) -> dict:
    """抓取最近交易日的 close/amount，增量更新三张仓表。"""
    last = load_last_close()
    codes = last.index.tolist()
    if not codes:
        raise SystemExit("store 为空：请先在本地执行 seed_from_local() 并提交仓库")

    start = (last["last_date"].max() - pd.Timedelta(days=FETCH_BUFFER_DAYS)).strftime("%Y-%m-%d")
    fetched = {}
    t0 = time.time()
    with Pool(workers, initializer=_init_worker) as pool:
        for i, (code, df) in enumerate(
                pool.starmap(_fetch_one, [(c, start) for c in codes]), 1):
            if df is not None and not df.empty:
                fetched[code] = df
            if i % 1000 == 0:
                print(f"  抓取 {i}/{len(codes)}  {time.time()-t0:.0f}s", flush=True)
    print(f"[store] 抓到 {len(fetched)}/{len(codes)} 只  用时 {time.time()-t0:.0f}s")

    cutoff = pd.Timestamp(date.today())
    cutoff = cutoff - pd.Timedelta(days=pd.Timestamp(date.today()).weekday())  # 本周一

    # --- 合并周线（预分组避免 O(n²) 过滤）---
    wk = load_weekly()
    wk_groups = {c: g for c, g in wk.groupby("code")} if len(wk) else {}
    new_weekly = []
    last_rows = []
    amt_rows = []
    for code, df in fetched.items():
        df = df.drop_duplicates("date").sort_values("date")
        s = df.set_index("date")["close"]
        wnew = s.groupby(s.index.to_period("W")).last()
        wnew.index = wnew.index.to_timestamp()
        wnew = wnew[wnew.index < cutoff]                     # 只收已完成周
        old = wk_groups.get(code)
        merged = pd.concat([old, pd.DataFrame(
            {"code": code, "week_end": wnew.index, "close": wnew.values})]
        ).drop_duplicates("week_end", keep="last").sort_values("week_end")
        merged = merged.tail(KEEP_WEEKS)
        new_weekly.append(merged)

        lc = df.iloc[-1]
        last_rows.append({"code": code, "hfq_last_close": float(lc["close"]),
                          "last_date": lc["date"]})
        amt_rows.append({"code": code, "avg_amt20": float(df["amount"].tail(20).mean())})

    save_weekly(pd.concat(new_weekly, ignore_index=True))
    pd.DataFrame(last_rows).to_csv(F_LAST, index=False)
    pd.DataFrame(amt_rows).to_csv(F_AMT, index=False)
    stats = {"fetched": len(fetched), "total": len(codes)}
    print(f"[store] 更新完成 {stats}")
    return stats


# ---------------------------------------------------------------- 快照（云端）
def build_snapshot_from_store() -> pd.DataFrame:
    from .ma60_base import _industry_map, cutoff_monday, stock_from_weekly

    wk = load_weekly()
    last = load_last_close()
    amt = load_amt20()
    ind_map = _industry_map()
    cutoff = cutoff_monday()

    rows = []
    for code, g in wk.groupby("code"):
        s = g.set_index("week_end")["close"]
        r = stock_from_weekly(code, s, cutoff)
        if r is None:
            continue
        r.pop("weekly_closes")
        if code in last.index:
            r["hfq_last_close"] = float(last.at[code, "hfq_last_close"])
            r["last_date"] = pd.Timestamp(last.at[code, "last_date"]).strftime("%Y-%m-%d")
        else:
            continue
        r["avg_amt20"] = float(amt.get(code, np.nan))
        r["industry"] = ind_map.get(code, "未知")
        rows.append(r)
    return pd.DataFrame(rows).set_index("code")
