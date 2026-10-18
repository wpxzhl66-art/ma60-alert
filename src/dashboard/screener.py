"""全市场选股快照（v1.28.0 新增）：为看板「选股」条件面板提供数据底座。

思路：复权价的比例运算（偏离、上穿、多头排列、连涨、新高、量比）与复权因子无关，
可直接从 data/daily 的 hfq 日线库计算；只有展示用「现价」取 dashboard/quotes.json
（不复权）。因此无需拉取任何新数据，纯本地计算。

产物 dashboard/screener.json（schema screener.v1），字段（紧凑键，控制体积）：
  c  代码(sh.600000)   n  名称        i  行业
  p  现价(不复权)      pc 日涨跌幅%   t  换手率%      am 成交额(亿)
  vr 量比(当日成交额/前5日均额)
  d5/d10/d20/d60    收盘相对日MA5/10/20/60 的偏离 %
  x5/x10/x20/x60    今日是否上穿对应均线(0/1)
  mb 日线多头排列 ma5>ma10>ma20>ma60 (0/1)
  ud 连涨天数          h20 收创20日新高(0/1)
  w60 收盘相对60周线偏离 %   wx 今日上穿60周线(0/1，周线按日K聚合，当周含今日)

用法：
  python -m dashboard.screener          # 生成/更新 dashboard/screener.json
  python -m dashboard.build --daily     # 已并入日常链路
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "daily"
OUT = ROOT / "dashboard" / "screener.json"
CST = timezone(timedelta(hours=8))

_MIN_DAILY = 61          # 日线至少 61 根（够算 MA60 及其前值）
_MIN_WEEKLY = 61         # 周线至少 61 根（够算 60 周线及其前值）


def _r1(x) -> float:
    return round(float(x), 1) if x == x else 0.0


def _r2(x) -> float:
    return round(float(x), 2) if x == x else 0.0


def _calc_one(args):
    code, name, ind = args
    f = DATA / f"{code.replace('.', '_')}.csv.gz"
    if not f.exists():
        return None
    try:
        df = pd.read_csv(f)
    except Exception:
        return None
    if len(df) < _MIN_DAILY:
        return None
    c = df["close"].to_numpy()
    amt = df["amount"].to_numpy()
    turn = df["turn"].to_numpy()

    row = {"c": code, "n": name}
    if ind:
        row["i"] = ind

    # ---- 日线均线偏离与上穿（hfq 比例，复权无关）----
    mas = {}
    for n in (5, 10, 20, 60):
        ma_now = c[-n:].mean()
        ma_prev = c[-n - 1:-1].mean()
        mas[n] = ma_now
        row[f"d{n}"] = _r2((c[-1] / ma_now - 1) * 100)
        row[f"x{n}"] = 1 if (c[-2] <= ma_prev and c[-1] > ma_now) else 0
    row["mb"] = 1 if (mas[5] > mas[10] > mas[20] > mas[60]) else 0

    # ---- 连涨 / 20日新高 ----
    ud = 0
    for k in range(len(c) - 1, 0, -1):
        if c[k] > c[k - 1]:
            ud += 1
        else:
            break
    row["ud"] = ud
    row["h20"] = 1 if c[-1] >= c[-20:].max() else 0

    # ---- 流动性 ----
    prev5 = amt[-6:-1].mean()
    row["vr"] = _r2(amt[-1] / prev5) if prev5 > 0 else 0.0
    row["t"] = _r2(turn[-1])
    row["am"] = _r2(amt[-1] / 1e8)
    row["pc"] = _r2((c[-1] / c[-2] - 1) * 100)

    # ---- 周线（按日K聚合，当周含今日）----
    dates = pd.to_datetime(df["date"])
    wk = pd.Series(c, index=dates).resample("W-FRI").last().dropna()
    if len(wk) >= _MIN_WEEKLY:
        w = wk.to_numpy()
        ma_now = w[-60:].mean()
        ma_prev = w[-61:-1].mean()
        row["w60"] = _r2((w[-1] / ma_now - 1) * 100)
        row["wx"] = 1 if (w[-2] <= ma_prev and w[-1] > ma_now) else 0
    # 周线不足的股票：w60/wx 缺省，前端按「无周线数据」处理（不参与周线条件）
    return row


def build_screener() -> dict:
    t0 = time.time()
    uni = pd.read_csv(ROOT / "data" / "universe.csv.gz", dtype=str)
    uni = uni[(uni["type"] == "1") & (uni["status"] == "1")]
    ind_df = pd.read_csv(ROOT / "data" / "industry.csv.gz", dtype=str)
    ind_map = dict(zip(ind_df["code"], ind_df["industry"].fillna("")))
    jobs = [(r.code, r.code_name, ind_map.get(r.code, ""))
            for r in uni.itertuples()]

    rows = []
    with Pool(16) as pool:
        for k, r in enumerate(pool.imap_unordered(_calc_one, jobs), 1):
            if r is not None:
                rows.append(r)
            if k % 1000 == 0:
                print(f"  screener {k}/{len(jobs)}  {time.time()-t0:.0f}s", flush=True)

    # ---- 现价（不复权）来自 quotes.json；缺失的剔除（停牌/无行情）----
    qj = json.loads((ROOT / "dashboard" / "quotes.json").read_text(encoding="utf-8"))
    items = qj.get("items") or {}
    priced = []
    for r in rows:
        q = items.get(r["c"])
        if not q or not q[0]:
            continue
        r["p"] = round(float(q[0]), 2)
        priced.append(r)

    priced.sort(key=lambda r: r["c"])
    out = {
        "schema": "screener.v1",
        "day": qj.get("date", ""),
        "updated": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
        "universe": len(jobs),
        "skipped": len(jobs) - len(priced),
        "counts": {"total": len(priced)},
        "rows": priced,
    }
    text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    OUT.write_text(text, encoding="utf-8")
    print(f"[screener] {len(priced)}/{len(jobs)} 只 -> screener.json "
          f"({len(text.encode())/1024:.0f} KB, {time.time()-t0:.0f}s)")
    return out


if __name__ == "__main__":
    build_screener()
