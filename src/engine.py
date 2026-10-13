"""事件研究分析引擎。

职责链：
    日线 → 合成周线 → 60周均线与带宽 → 事件检测（类型A上穿 / 类型B回踩）
    → 去重（可选冷却期）→ 前瞻收益（3基准 × 5窗口）→ 分层打标 → 汇总

设计原则：
1. **只用收盘价**。核验报告发现该数据源的复权 OHLC 内部不一致（同一天
   高/低/收的缩放比值离散达 0.32%），故所有计算一律基于 close，规避污染。
2. **禁止未来函数**。所有分层标签只使用事件周及之前的信息；市值与行业
   标签必须使用事件时点数据，不得使用全期静态数据。
3. **样本重叠如实记录**。主口径不去重，但每笔事件附带 dedup 标记，
   便于输出集中度指标与去重对照。
4. **右删失显式标记**。前瞻窗口超出数据末端的，收益标记为 NaN 并给出标志位，
   不得静默丢弃。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# 事件类型常量
TYPE_A = "A_上穿突破"
TYPE_B = "B_上方回踩"


# ==========================================================================
# 一、周线合成
# ==========================================================================

def build_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    """日线合成周线。

    周收盘 = 该周最后一个交易日收盘（这是后复权口径下唯一正确的取法）。
    周标记日期 = 该周最后一个实际交易日（而非日历周五），以对齐真实成交。

    只对**存在**的 OHLCV 列做聚合；实际研究只用 close，故允许只传
    date/close 两列以节省全A股下的内存。
    """
    if daily.empty:
        return daily.copy()

    d = daily.copy().sort_values("date")
    d["period"] = d["date"].dt.to_period("W")

    agg = {"date": ("date", "last"), "close": ("close", "last"),
           "n_days": ("date", "count")}
    for c, how in (("open", "first"), ("high", "max"), ("low", "min"),
                   ("volume", "sum"), ("amount", "sum")):
        if c in d.columns:
            agg[c] = (c, how)

    w = d.groupby("period", sort=True).agg(**agg).reset_index(drop=True)

    # 最后一个周期可能未走完（本周尚未收盘），单独标记
    w["is_last_week"] = False
    if len(w):
        w.loc[w.index[-1], "is_last_week"] = True
    return w


# ==========================================================================
# 二、均线与事件检测
# ==========================================================================

def add_moving_averages(weekly: pd.DataFrame, ma_weeks: int = 60,
                        band_pct: float = 0.02,
                        compare_periods: tuple = (5, 10, 20, 60)) -> pd.DataFrame:
    """计算主均线、带宽，以及用于排列判定的多条均线。

    同时生成 week_idx —— 周序号是贯穿事件检测、去重、分层、关联的
    统一主键，必须在此层建立，避免下游各自重算导致错位。
    """
    w = weekly.copy().reset_index(drop=True)
    w["week_idx"] = np.arange(len(w))
    w["ma"] = w["close"].rolling(ma_weeks).mean()
    w["band_up"] = w["ma"] * (1 + band_pct)
    w["band_dn"] = w["ma"] * (1 - band_pct)
    for p in compare_periods:
        w[f"ma{p}"] = w["close"].rolling(p).mean()
    return w


def detect_events(weekly_ma: pd.DataFrame, ma_weeks: int = 60, band_pct: float = 0.02,
                  type_a: bool = True, type_b: bool = True,
                  type_b_requires_above_band: bool = True,
                  allow_last_week: bool = False) -> pd.DataFrame:
    """检测两类触及事件。

    类型A「上穿突破」：前一周收盘 < 前一周MA，本周收盘 >= 本周MA。
    类型B「上方回踩」：此前收盘在带宽上沿之上，本周收盘落入 [MA*(1-b), MA*(1+b)]。

    返回列：week_idx, event_date, event_type, close, ma, band_up, band_dn
    """
    w = weekly_ma.copy().reset_index(drop=True)
    w["week_idx"] = np.arange(len(w))
    w["prev_close"] = w["close"].shift(1)
    w["prev_ma"] = w["ma"].shift(1)
    w["prev_band_up"] = w["band_up"].shift(1)

    base = w["ma"].notna() & w["prev_ma"].notna()
    if not allow_last_week and len(w):
        base &= ~w["is_last_week"]

    masks = {}
    if type_a:
        masks[TYPE_A] = base & (w["prev_close"] < w["prev_ma"]) & (w["close"] >= w["ma"])
    if type_b:
        if type_b_requires_above_band:
            prior_above = w["prev_close"] > w["prev_band_up"]
        else:
            prior_above = w["prev_close"] >= w["prev_ma"]
        masks[TYPE_B] = base & prior_above & (w["close"] <= w["band_up"]) & \
            (w["close"] >= w["band_dn"])

    frames = []
    for etype, mask in masks.items():
        sub = w.loc[mask, ["week_idx", "date", "close", "ma", "band_up", "band_dn"]].copy()
        sub = sub.rename(columns={"date": "event_date"})
        sub.insert(1, "event_type", etype)
        frames.append(sub)

    if not frames:
        return pd.DataFrame(columns=["week_idx", "event_date", "event_type", "close",
                                     "ma", "band_up", "band_dn"])
    return pd.concat(frames, ignore_index=True).sort_values(
        "week_idx").reset_index(drop=True)


def mark_dedup(events: pd.DataFrame, cooldown_weeks: int = 26) -> pd.DataFrame:
    """标记事件是否通过冷却期去重。

    主口径为「不去重」（全部计入），本函数只产出 dedup 标记列，
    用于后续输出去重对照与集中度指标，不改变主样本。
    去重按股票分别进行，冷却期以事件所在周序号度量。
    """
    e = events.copy()
    e["dedup_kept"] = True
    if cooldown_weeks <= 0 or e.empty:
        return e
    last_kept: dict = {}
    for idx, row in e.iterrows():
        key = row.get("code", "__single__")
        prev = last_kept.get(key)
        if prev is not None and row["week_idx"] - prev < cooldown_weeks:
            e.at[idx, "dedup_kept"] = False
        else:
            last_kept[key] = row["week_idx"]
    return e


# ==========================================================================
# 三、前瞻收益
# ==========================================================================

def align_close(daily: pd.DataFrame, calendar: pd.DatetimeIndex) -> pd.Series:
    """把日收盘价对齐到统一交易日历。停牌日以前值填充（并在明细中可追溯）。"""
    s = daily.set_index("date")["close"]
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s.reindex(calendar).ffill()


def _position(calendar: pd.DatetimeIndex, ts) -> int | None:
    """定位时间戳在日历中的位置；若不在日历上则取其后第一个交易日。"""
    pos = calendar.searchsorted(ts, side="left")
    if pos >= len(calendar):
        return None
    return int(pos)


def compute_forward_returns(events: pd.DataFrame, close_aligned: pd.Series,
                            calendar: pd.DatetimeIndex,
                            horizons=(20, 40, 60, 120, 250),
                            bench: dict | None = None) -> pd.DataFrame:
    """计算每笔事件的前瞻收益与路径指标。

    bench: {基准名: 已对齐到 calendar 的收盘序列}，如 {"hs300": s, "sw_l1": s}

    输出列（每个窗口 N）：
        ret{N}      绝对收益
        ex_{name}{N}  相对该基准的超额收益
        mfe{N}      窗口内最大浮盈
        mae{N}      窗口内最大浮亏
        mdd{N}      窗口内最大回撤
        ok{N}       该窗口数据是否完整（False = 右删失）
    """
    bench = bench or {}
    calendar = pd.DatetimeIndex(calendar)
    records = []

    for _, ev in events.iterrows():
        t0 = _position(calendar, ev["event_date"])
        if t0 is None or t0 >= len(calendar):
            continue
        p0 = close_aligned.iloc[t0]
        if not np.isfinite(p0) or p0 <= 0:
            continue

        rec = {
            "week_idx": ev["week_idx"],
            "event_date": ev["event_date"],
            "event_type": ev["event_type"],
            "entry_price": p0,
            "entry_ma": ev["ma"],
            "dev_from_ma": p0 / ev["ma"] - 1,
            "t0_pos": t0,
        }

        for N in horizons:
            t1 = t0 + N
            if t1 >= len(calendar):
                rec[f"ok{N}"] = False
                for k in ("ret", "mfe", "mae", "mdd"):
                    rec[f"{k}{N}"] = np.nan
                for name in bench:
                    rec[f"ex_{name}{N}"] = np.nan
                continue

            p1 = close_aligned.iloc[t1]
            if not np.isfinite(p1):
                rec[f"ok{N}"] = False
                for k in ("ret", "mfe", "mae", "mdd"):
                    rec[f"{k}{N}"] = np.nan
                for name in bench:
                    rec[f"ex_{name}{N}"] = np.nan
                continue

            rec[f"ok{N}"] = True
            r = p1 / p0 - 1
            rec[f"ret{N}"] = r

            path = close_aligned.iloc[t0:t1 + 1].to_numpy(dtype=float)
            path = path[np.isfinite(path)]
            if path.size:
                rec[f"mfe{N}"] = path.max() / p0 - 1
                rec[f"mae{N}"] = path.min() / p0 - 1
                rec[f"mdd{N}"] = (path / np.maximum.accumulate(path) - 1).min()
            else:
                rec[f"mfe{N}"] = rec[f"mae{N}"] = rec[f"mdd{N}"] = np.nan

            for name, bs in bench.items():
                b0, b1 = bs.iloc[t0], bs.iloc[t1]
                rec[f"ex_{name}{N}"] = (r - (b1 / b0 - 1)) \
                    if (np.isfinite(b0) and np.isfinite(b1) and b0 > 0) else np.nan

        records.append(rec)

    return pd.DataFrame(records)


# ==========================================================================
# 四、分层打标
# ==========================================================================

def market_regime_series(index_weekly: pd.DataFrame, ma_weeks: int = 40,
                         slope_lookback: int = 4) -> pd.DataFrame:
    """由基准指数周线推导市场环境：牛市 / 熊市 / 震荡。

    牛市 = 指数在MA40上方 且 MA40向上；熊市 = 下方且向下；其余为震荡。
    """
    w = index_weekly.copy().reset_index(drop=True)
    w["idx_ma"] = w["close"].rolling(ma_weeks).mean()
    up = w["idx_ma"] > w["idx_ma"].shift(slope_lookback)
    above = w["close"] > w["idx_ma"]
    regime = np.where(above & up, "牛市",
                      np.where((~above) & (~up), "熊市", "震荡"))
    w["market_regime"] = np.where(w["idx_ma"].isna(), None, regime)
    return w[["date", "market_regime"]]


def stratify(weekly_ma: pd.DataFrame, events: pd.DataFrame,
             regime: pd.DataFrame | None = None,
             ma_weeks: int = 60, slope_lookback: int = 13,
             up_thr: float = 0.01, down_thr: float = -0.01,
             alignment_periods=(5, 10, 20, 60)) -> pd.DataFrame:
    """为每笔事件打上分层标签。

    所有标签只使用事件周及之前的数据，无未来函数。
    市值风格与板块标签依赖 iFinD 的历史股本与行业分类，在外部注入。
    """
    e = events.copy()
    w = weekly_ma.copy().reset_index(drop=True)
    if e.empty:
        return e

    w["ma_slope"] = w["ma"] / w["ma"].shift(slope_lookback) - 1

    def _slope_label(x):
        if not np.isfinite(x):
            return "未知"
        if x > up_thr:
            return "向上"
        if x < down_thr:
            return "向下"
        return "走平"

    w["label_slope"] = w["ma_slope"].map(_slope_label)

    cols = [f"ma{p}" for p in alignment_periods]
    have_align = all(c in w.columns for c in cols)
    if have_align:
        vals = w[cols].to_numpy(dtype=float)
        ordered = np.all(np.diff(vals, axis=1) < 0, axis=1)
        w["label_alignment"] = np.where(
            np.isfinite(vals).all(axis=1),
            np.where(ordered, "多头排列", "非多头排列"), "未知")

    keep = ["week_idx", "label_slope"]
    if have_align:
        keep.append("label_alignment")
    e = e.merge(w[keep], on="week_idx", how="left")

    if regime is not None and not regime.empty:
        e["event_date"] = pd.to_datetime(e["event_date"])
        e = e.merge(regime, left_on="event_date", right_on="date", how="left")
        e = e.drop(columns=["date"])
        e = e.rename(columns={"market_regime": "label_regime"})

    return e


# ==========================================================================
# 五、汇总统计
# ==========================================================================

@dataclass
class StatsConfig:
    horizons: tuple = (20, 40, 60, 120, 250)
    benchmarks: tuple = ("hs300",)


def summarize(events: pd.DataFrame, cfg: StatsConfig) -> pd.DataFrame:
    """按 事件类型 × 窗口 × 基准 汇总统计。

    同时输出均值与中位数（A股收益分布右偏，两者常背离，必须并列）。
    胜率按绝对收益与超额收益分别计算。
    """
    rows = []
    if events.empty:
        return pd.DataFrame()

    for etype, grp in events.groupby("event_type"):
        n_total = len(grp)
        n_stock = grp["code"].nunique() if "code" in grp else np.nan
        for N in cfg.horizons:
            ok = grp[f"ok{N}"] if f"ok{N}" in grp else pd.Series(False, index=grp.index)
            sub = grp.loc[ok.fillna(False)]
            ret = sub.get(f"ret{N}", pd.Series(dtype=float)).dropna()
            if ret.empty:
                continue

            stats = {
                "event_type": etype,
                "horizon": N,
                "n_events": int(len(ret)),
                "n_stocks": n_stock,
                "n_censored": int(n_total - len(ret)),
                "mean": ret.mean(),
                "median": ret.median(),
                "std": ret.std(),
                "win_rate": float((ret > 0).mean()),
                "pl_ratio": (ret[ret > 0].mean() / abs(ret[ret < 0].mean())
                             if (ret > 0).any() and (ret < 0).any() else np.nan),
                "p10": ret.quantile(0.10), "p25": ret.quantile(0.25),
                "p75": ret.quantile(0.75), "p90": ret.quantile(0.90),
                "mfe_mean": sub.get(f"mfe{N}", pd.Series(dtype=float)).mean(),
                "mae_mean": sub.get(f"mae{N}", pd.Series(dtype=float)).mean(),
                "mdd_mean": sub.get(f"mdd{N}", pd.Series(dtype=float)).mean(),
            }
            for name in cfg.benchmarks:
                col = f"ex_{name}{N}"
                if col in sub:
                    ex = sub[col].dropna()
                    stats[f"ex_{name}_mean"] = ex.mean() if len(ex) else np.nan
                    stats[f"ex_{name}_median"] = ex.median() if len(ex) else np.nan
                    stats[f"ex_{name}_win_rate"] = float((ex > 0).mean()) if len(ex) else np.nan
            rows.append(stats)

    return pd.DataFrame(rows)
