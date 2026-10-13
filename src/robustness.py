"""稳健性分析：按股票聚类的 bootstrap + 去重口径敏感性对照。

解决的共同问题
==============
主口径「不去重」下，同一标的的相邻事件前瞻窗口大量重叠（见报告示意图），
样本并不独立。若直接按事件数计算标准误与置信区间，区间会被系统性低估，
本不显著的差异会显得显著。

一、按股票聚类的 bootstrap（cluster bootstrap）
------------------------------------------------
以**股票**为重抽样单位（而非事件）：每次有放回地抽 N 只股票（N = 样本内
股票数），把这些股票的**全部事件**汇总计算均值，重复 B 次得到经验分布，
取 2.5%/97.5% 分位为置信区间。
- 保留了原有的「按事件加权」估计量（不是按股票等权），只修正方差估计
- 同时给出**朴素区间**（把事件当独立样本）用于对照，
  `design_effect = 聚类区间宽度 / 朴素区间宽度`，显著 >1 说明重叠确实
  大幅抬高了名义显著性

二、去重口径敏感性对照
------------------------------------------------
按「同一标的 26 周冷却期只保留首次触线」的子样本再算一遍汇总，
与主口径并排比较。两口径结论一致 → 稳健。

工程实现要点
------------------------------------------------
重抽样用「各股事件和 / 各股事件数」的向量化写法，避免逐次 concat，
并按块推进以控制内存（全A股下 事件数 ~10 万、股票数 ~5000）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from engine import StatsConfig, summarize

# 计算置信区间所用的指标列
METRICS = ("ret", "ex_hs300", "ex_industry")


def cluster_bootstrap(events: pd.DataFrame, value_col: str, horizon: int,
                      event_type: str, n_iter: int = 2000,
                      seed: int = 20261013, min_stocks: int = 8,
                      chunk: int = 200) -> dict | None:
    """对单个 (事件类型, 窗口, 指标) 做按股票聚类的 bootstrap。"""
    col = f"{value_col}{horizon}"
    if col not in events.columns:
        return None
    sub = events.loc[events["event_type"] == event_type, ["code", col]].dropna()
    if sub.empty:
        return None
    grouped = sub.groupby("code")[col]
    sums = grouped.sum().to_numpy(dtype=float)
    counts = grouped.size().to_numpy(dtype=float)
    k = len(sums)
    if k < min_stocks:
        return None

    rng = np.random.default_rng(seed)
    draws = np.empty(n_iter, dtype=float)
    done = 0
    while done < n_iter:
        m = min(chunk, n_iter - done)
        idx = rng.integers(0, k, size=(m, k))
        tot_s = sums[idx].sum(axis=1)
        tot_n = counts[idx].sum(axis=1)
        draws[done:done + m] = tot_s / np.maximum(tot_n, 1)
        done += m

    vals = sub[col].to_numpy(dtype=float)
    pooled_mean = float(vals.mean())
    lo, hi = (float(x) for x in np.percentile(draws, [2.5, 97.5]))
    p = float(min((draws <= 0).mean(), (draws >= 0).mean()) * 2)
    p = min(p, 1.0)

    se_naive = float(vals.std(ddof=1) / np.sqrt(len(vals))) if len(vals) > 1 else np.nan
    naive_lo = pooled_mean - 1.96 * se_naive if se_naive == se_naive else np.nan
    naive_hi = pooled_mean + 1.96 * se_naive if se_naive == se_naive else np.nan
    de = ((hi - lo) / (naive_hi - naive_lo)) if (naive_hi - naive_lo) > 0 else np.nan

    return {
        "event_type": event_type,
        "horizon": horizon,
        "metric": value_col,
        "n_events": int(len(vals)),
        "n_stocks": int(k),
        "pooled_mean": pooled_mean,
        "ci_lo": lo,
        "ci_hi": hi,
        "p_cluster": p,
        "naive_ci_lo": naive_lo,
        "naive_ci_hi": naive_hi,
        "design_effect": float(de) if de == de else None,
    }


def cluster_bootstrap_table(events: pd.DataFrame, horizons, metrics=METRICS,
                            n_iter: int = 2000) -> pd.DataFrame:
    rows = []
    for etype in sorted(events["event_type"].dropna().unique()):
        for N in horizons:
            for m in metrics:
                r = cluster_bootstrap(events, m, N, etype, n_iter=n_iter)
                if r:
                    rows.append(r)
    return pd.DataFrame(rows)


def dedup_compare(events: pd.DataFrame, cfg: StatsConfig,
                  n_iter: int = 2000) -> pd.DataFrame:
    """去重口径 vs 主口径的并排汇总。

    列 event_type/horizon 等沿用 summarize 的输出，另加 caliber 列：
        all    = 主口径（不去重，全部计入）
        dedup  = 去重口径（26 周冷却期只保留首次）
    并附聚类 bootstrap 的 p 值，便于直接对比显著性是否随口径改变。
    """
    frames = []
    for caliber, sub in (("all", events),
                         ("dedup", events[events["dedup_kept"].astype(bool)])):
        if sub.empty:
            continue
        s = summarize(sub, cfg)
        if s.empty:
            continue
        s["caliber"] = caliber
        boot = cluster_bootstrap_table(sub, cfg.horizons, n_iter=n_iter)
        if not boot.empty:
            boot = boot[boot["metric"] == "ret"][
                ["event_type", "horizon", "p_cluster", "design_effect"]]
            s = s.merge(boot, on=["event_type", "horizon"], how="left")
        frames.append(s)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
