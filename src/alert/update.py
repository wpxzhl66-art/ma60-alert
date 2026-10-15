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
            # 空返回 ≠ 没有新数据：baostock 限流/黑名单时就是静默返回空。
            # 落后 <30 天按原样记 no_new（节假日正常）；落后过久则显式标记，
            # 否则序列会像双汇发展那样断头 3.5 年而无人察觉（2026-10 实测）。
            gap = (pd.Timestamp(date.today()) - last).days
            return (code, "stale_no_data" if gap > 30 else "no_new", 0)
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
    res = pd.DataFrame(results, columns=["code", "status", "added"])
    stale = res[res["status"] == "stale_no_data"]
    if len(stale):
        print(f"[update][警告] {len(stale)} 只标的增量长期返回空（序列已断头，"
              f"baostock 限流/无数据），需人工核查或转腾讯源重建："
              f"{stale['code'].tolist()[:20]}" + (" ..." if len(stale) > 20 else ""))
    return res


def update_benchmark() -> bool:
    """沪深300 基准日线增量（BUG #10）。

    data/benchmark/hs300.csv.gz 原本只在一次性建库脚本里生成，日常更新
    链路不碰它 → 基准停在 2026-10-13 而日线已到 10-15，近一月库窗口
    被截短、研究管线的超额计算也用旧基准。本函数在每个更新轮追加增量。
    data/ 不入 git：云端没有这个文件（云端日历走 store 周线并集），
    只服务本机的研究管线与日历兜底。
    """
    p = ROOT / "data" / "benchmark" / "hs300.csv.gz"
    cols = ["date", "open", "high", "low", "close", "volume", "amount", "pctChg"]
    try:
        datasource.patch_baostock_io(45.0)
        import baostock as bs
        lg = bs.login()
        if lg.error_code != "0":
            print(f"[update] 基准更新失败：baostock 登录失败（{lg.error_msg}）")
            return False
        try:
            if p.exists():
                df = pd.read_csv(p, parse_dates=["date"])
                last = df["date"].max()
            else:
                df, last = None, pd.Timestamp("2005-01-04")
            if (pd.Timestamp(date.today()) - last).days <= 1:
                return True                       # 已新鲜
            start = (last - timedelta(days=10)).strftime("%Y-%m-%d")
            rs = bs.query_history_k_data_plus(
                "sh.000300", ",".join(cols), start_date=start, end_date="",
                frequency="d", adjustflag="3")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                print("[update] 基准更新：无新数据返回（可能限流），沿用旧基准")
                return False
            new = pd.DataFrame(rows, columns=cols)
            for c in cols[1:]:
                new[c] = pd.to_numeric(new[c], errors="coerce")
            new["date"] = pd.to_datetime(new["date"])
            merged = (new if df is None
                      else pd.concat([df, new]).drop_duplicates("date").sort_values("date"))
            p.parent.mkdir(parents=True, exist_ok=True)
            merged.to_csv(p, index=False, compression="gzip")
            print(f"[update] 沪深300基准已更新至 {merged['date'].max().date()}"
                  f"（新增 {len(new)} 行）")
            return True
        finally:
            bs.logout()
    except Exception as exc:  # noqa: BLE001
        print(f"[update] 基准更新失败（不影响日线主链路）：{type(exc).__name__} {exc}")
        return False
