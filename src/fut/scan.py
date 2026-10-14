"""期货加权指数 → 60 周线分层预警判定。

判定口径（与 A 股同源，均为「价格 vs 60 周均线」）：

* ``60 周均线`` = 已完成周的加权收盘的 60 周简单移动平均（剔除本周未完成周）
* ``突破``     = 上一交易日加权收盘 < MA60  且  当前加权价 >= MA60
* ``临近``     = 未突破，但当前加权价与 MA60 的偏离在 ±near_pct 之内

其中「当前加权价」的取法：
  * 盘中（日线还停在昨天）：``now`` = 实时加权价，``prev`` = 昨日加权收盘
  * 收盘后（日线已含今天）：``now`` = 今日加权收盘，``prev`` = 前一交易日收盘
  * 实时不可用：退化为纯日线口径
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from . import core
from . import varieties as V

MA_WEEKS = 60
SLOPE_WEEKS = 13
DEFAULT_NEAR_PCT = 0.03


def snapshot(near_pct: float = DEFAULT_NEAR_PCT,
             use_realtime: bool = True,
             weekly: pd.DataFrame | None = None,
             daily: pd.DataFrame | None = None,
             last: pd.DataFrame | None = None,
             symbols: list[str] | None = None) -> pd.DataFrame:
    """全部品种的 60 周线指标（不筛选）。status ∈ {突破, 临近, —}。"""
    weekly = core.load_weekly() if weekly is None else weekly
    daily = core.load_daily() if daily is None else daily
    last = core.load_last() if last is None else last
    if weekly.empty:
        return pd.DataFrame()

    # 实时加权价（盘中用）；失败则退化为日线口径
    now_map: dict[str, float] = {}
    if use_realtime:
        try:
            rt = core.live_weighted(symbols)
            if not rt.empty:
                now_map = dict(zip(rt["code"], rt["price"]))
        except Exception as e:                     # noqa: BLE001
            print(f"[fut] 实时加权价获取失败，退化为日线口径：{e}")

    # 日线口径下的 prev / now
    today = pd.Timestamp(date.today()).normalize()
    prev_map: dict[str, float] = {}
    dnow_map: dict[str, float] = {}
    dlast_map: dict[str, float] = {}          # 各品种最后一根日线收盘（无论是否今天）
    if not daily.empty:
        for sym, g in daily.groupby("code"):
            g = g.sort_values("date")
            last_row = g.iloc[-1]
            dlast_map[sym] = float(last_row["close"])
            has_today = pd.Timestamp(last_row["date"]).normalize() == today
            if has_today and len(g) >= 2:
                prev_map[sym] = float(g["close"].iloc[-2])
                dnow_map[sym] = float(last_row["close"])
            else:
                prev_map[sym] = float(last_row["close"])
    elif not last.empty:
        prev_map = dict(zip(last["code"], last["last_close"]))
        dlast_map = dict(prev_map)

    rows = []
    for sym, g in weekly.groupby("code"):
        g = g.sort_values("week_end")
        closes = g["close"].to_numpy(float)
        if len(closes) < MA_WEEKS + 1:
            continue
        ma = pd.Series(closes).rolling(MA_WEEKS).mean().to_numpy()
        ma_now = float(ma[-1])
        if not np.isfinite(ma_now) or ma_now <= 0:
            continue

        prev = float(prev_map.get(sym, closes[-1]))
        # 当前价优先级：实时加权 → 今日日线收盘 → 最近一根日线收盘 → 周线收盘。
        # 第三档很关键：实时行情整体不可用时若直接退到周线收盘，会出现
        # 「now 取上周五、prev 取昨日」的时序颠倒，判定失真。
        now = float(now_map.get(sym) or dnow_map.get(sym)
                    or dlast_map.get(sym) or closes[-1])
        dev = now / ma_now - 1.0
        breakout = (prev < ma_now) and (now >= ma_now)
        near = abs(dev) <= near_pct
        status = "突破" if breakout else ("临近" if near else "—")

        ma_13w = float(ma[-1 - SLOPE_WEEKS]) if len(ma) > SLOPE_WEEKS else np.nan
        slope = ("向上" if ma_now > ma_13w * 1.01
                 else "向下" if ma_now < ma_13w * 0.99 else "走平")

        start = max(MA_WEEKS + 1, len(closes) - 52)
        cnt = 0
        for i in range(start, len(closes)):
            if closes[i - 1] < ma[i - 1 - MA_WEEKS] and closes[i] >= ma[i - MA_WEEKS]:
                cnt += 1

        rows.append({
            "code": sym,
            "name": V.NAME.get(sym, sym),
            "sector": V.SECTOR.get(sym, ""),
            "market": V.MARKET.get(sym, ""),
            "price": round(now, 3),
            "prev": round(prev, 3),
            "ma60": round(ma_now, 3),
            "dev_pct": round(dev, 5),
            "status": status,
            "slope": slope,
            "up_cnt_1y": cnt,
            "weeks": int(len(closes)),
            "n_con": int(g["n_con"].iloc[-1]) if "n_con" in g.columns else 0,
            # 全品种总持仓量（手）：看板可按资金关注度排序
            "oi_sum": int(g["oi_sum"].iloc[-1]) if "oi_sum" in g.columns
                      and pd.notna(g["oi_sum"].iloc[-1]) else 0,
            "live": sym in now_map,
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return df.sort_values("dev_pct", key=lambda s: s.abs()).reset_index(drop=True)


def evaluate(near_pct: float = DEFAULT_NEAR_PCT,
             use_realtime: bool = True,
             **kw) -> pd.DataFrame:
    """今日触发清单（含「突破」与「临近」两类），突破在前。"""
    snap = snapshot(near_pct=near_pct, use_realtime=use_realtime, **kw)
    if snap.empty:
        return snap
    df = snap[snap["status"].isin(["突破", "临近"])].copy()
    if df.empty:
        return df
    df["_o"] = (df["status"] != "突破").astype(int)     # 突破排在临近之前
    return (df.sort_values(["_o", "dev_pct"])
              .drop(columns=["_o"]).reset_index(drop=True))


def summarize(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {"total": 0, "breakout": 0, "near": 0, "sectors": {}}
    b = int((df["status"] == "突破").sum())
    return {
        "total": int(len(df)),
        "breakout": b,
        "near": int(len(df) - b),
        "sectors": df.groupby("sector")["code"].count().to_dict(),
    }
