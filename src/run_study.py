"""研究主程序：从建库数据执行完整的 60 周线触及事件研究。

流程：
    读取日线库 → 统一交易日历 → 逐股合成周线并检测事件
    → 构造行业基准（自建等权组合，避免外部依赖）
    → 计算前瞻收益（绝对 / 沪深300 / 行业 三重基准）
    → 分层打标 → 汇总统计 → 落盘

输出：
    output/events_detail.csv.gz    每笔事件一行（Excel 明细的数据源）
    output/summary_overall.csv     总样本汇总
    output/summary_stratified.csv  分层汇总
    output/concentration.csv       样本集中度指标（不去重口径必看）
    output/study_result.json       供 HTML 报告消费的结果包
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from engine import (  # noqa: E402
    StatsConfig, add_moving_averages, align_close, build_weekly,
    compute_forward_returns, detect_events, mark_dedup, market_regime_series,
    stratify, summarize,
)
from robustness import cluster_bootstrap_table, dedup_compare  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "output"
START = "2010-01-01"
MA_WEEKS = 60
BAND_PCT = 0.02
HORIZONS = (20, 40, 60, 120, 250)
COOLDOWN_WEEKS = 26


def load_daily(limit: int | None = None):
    """载入日线库。

    只读研究必需的三列（date/close/float_mv），全A股下可把常驻内存压到
    约 1/4，避免 5000 只 × 4000 日 × 十几列的 DataFrame 撑爆内存。
    """
    files = sorted(glob.glob(str(DATA / "daily" / "*.csv.gz")))
    if limit:
        files = files[:limit]
    out = {}
    for f in files:
        code = Path(f).name.replace(".csv.gz", "").replace("_", ".", 1)
        try:
            head = pd.read_csv(f, nrows=0)
        except Exception:  # noqa: BLE001
            continue
        cols = [c for c in ("date", "close", "float_mv") if c in head.columns]
        df = pd.read_csv(f, usecols=cols, parse_dates=["date"])
        out[code] = df
    return out


def load_benchmark():
    p = DATA / "benchmark" / "hs300.csv.gz"
    if not p.exists():
        return None
    return pd.read_csv(p, parse_dates=["date"])


def load_industry():
    """载入行业映射。优先用申万一级（研究方案指定口径），回退到证监会分类。"""
    p = DATA / "industry_sw.csv.gz"
    if p.exists():
        df = pd.read_csv(p)
        if "code" in df.columns and "industry" in df.columns:
            m = df.dropna(subset=["industry"])
            return dict(zip(m["code"].astype(str), m["industry"].astype(str)))
    p2 = DATA / "industry.csv.gz"
    if p2.exists():
        df = pd.read_csv(p2)
        col = next((c for c in df.columns if c in ("industry", "行业")), None)
        if col and "code" in df.columns:
            m = df.dropna(subset=[col])
            return dict(zip(m["code"].astype(str), m[col].astype(str)))
    return {}


def build_industry_index(closes: dict, industry: dict, calendar: pd.DatetimeIndex):
    """自建行业等权指数。

    理由：baostock 提供的是证监会行业分类而非申万一级，外部指数源又难以保证
    复权一致性。用**样本内自建等权组合**作为行业基准，自洽、可复现、无外部依赖。
    """
    rets = {}
    for code, s in closes.items():
        ind = industry.get(code)
        if not ind or ind == "nan":
            continue
        r = s.pct_change()
        rets.setdefault(ind, []).append(r.rename(code))
    idx = {}
    for ind, lst in rets.items():
        m = pd.concat(lst, axis=1)
        # 显式对齐到统一日历，保证下游可用「日历位置」直接做位置索引
        series = (1 + m.mean(axis=1, skipna=True).fillna(0)).cumprod()
        idx[ind] = series.reindex(calendar).ffill().fillna(1.0)
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="限制股票数（调试）")
    ap.add_argument("--no-industry", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    t_start = time.time()

    print("=" * 62)
    print("60周线触及事件研究")
    print("=" * 62)

    # ---------- 1. 载入数据 ----------
    daily = load_daily(args.limit)
    print(f"载入 {len(daily)} 只标的的日线")
    bench = load_benchmark()
    if bench is None:
        print("缺少沪深300基准，先运行 build_dataset.py")
        return 1
    print(f"基准沪深300: {len(bench)} 条 {bench['date'].iloc[0].date()} ~ {bench['date'].iloc[-1].date()}")

    calendar = pd.DatetimeIndex(sorted(set(bench["date"])))
    hs300_close = align_close(bench, calendar)

    # ---------- 2. 逐股：合成周线 → 检测事件 ----------
    print("\n[1] 逐股合成周线并检测事件...")
    closes = {}
    all_events = []
    skipped = []
    for i, (code, df) in enumerate(daily.items(), 1):
        if len(df) < MA_WEEKS * 5:
            skipped.append((code, f"数据不足({len(df)}日)"))
            continue
        s = align_close(df, calendar)
        closes[code] = s
        w = build_weekly(df)
        w = add_moving_averages(w, ma_weeks=MA_WEEKS, band_pct=BAND_PCT,
                                compare_periods=(5, 10, 20, 60))
        ev = detect_events(w, ma_weeks=MA_WEEKS, band_pct=BAND_PCT,
                           type_a=True, type_b=True,
                           type_b_requires_above_band=True,
                           allow_last_week=False)
        if ev.empty:
            continue
        ev["code"] = code
        all_events.append((ev, w))
        if i % 50 == 0:
            print(f"  {i}/{len(daily)}")

    print(f"  有事件的标的 {len(all_events)} / {len(daily)}，跳过 {len(skipped)}")
    if not all_events:
        print("无事件，终止")
        return 1

    # 事件表 + 分层（分层需各自的周线，逐个处理）
    event_frames = []
    for ev, w in all_events:
        e = stratify(w, ev, ma_weeks=MA_WEEKS, slope_lookback=13)
        event_frames.append(e)
    events = pd.concat(event_frames, ignore_index=True)
    print(f"  原始事件总数 {len(events)}")

    # 冷却期去重标记（主口径不去重，仅标记以便对照）
    events = events.sort_values(["code", "week_idx"]).reset_index(drop=True)
    marked = []
    for code, grp in events.groupby("code", sort=False):
        marked.append(mark_dedup(grp, cooldown_weeks=COOLDOWN_WEEKS))
    events = pd.concat(marked, ignore_index=True)
    n_dedup = int(events["dedup_kept"].sum())
    print(f"  去重后（{COOLDOWN_WEEKS}周冷却）事件数 {n_dedup}")

    # 市场环境分层（由沪深300周线推导）
    bw = add_moving_averages(build_weekly(bench), ma_weeks=40, band_pct=0.02)
    regime = market_regime_series(bw, ma_weeks=40, slope_lookback=4)
    events = events.merge(regime, left_on="event_date", right_on="date",
                          how="left").drop(columns=["date"])
    events = events.rename(columns={"market_regime": "label_regime"})

    # ---------- 事件时点市值分层（横截面分位，无前视偏差） ----------
    # 口径说明：市值风格是**相对同期全市场**的概念，故用「事件当日横截面分位」，
    # 即该股当日流通市值在全市场当日的排名百分位，再按 1/3、2/3 分为小/中/大盘。
    # 旧口径用全样本事件的分位数作阈值，会让阈值含入事件之后的信息（前视偏差），
    # v1.1.1 起废弃。分位法还天然做到各档样本量均衡。
    mv_cols = {}
    for code, df in daily.items():
        if "float_mv" in df.columns:
            s = df.set_index("date")["float_mv"]
            mv_cols[code] = s[~s.index.duplicated(keep="last")].sort_index()
    if mv_cols:
        mv_panel = pd.DataFrame(mv_cols).reindex(calendar).ffill()
        mv_pct = mv_panel.rank(axis=1, pct=True)

        def _lookup(code, d, panel):
            try:
                return panel.at[pd.Timestamp(d), code]
            except (KeyError, TypeError):
                return np.nan

        events["mv_pct"] = [_lookup(r["code"], r["event_date"], mv_pct)
                            for _, r in events.iterrows()]
        events["float_mv"] = [_lookup(r["code"], r["event_date"], mv_panel)
                              for _, r in events.iterrows()]
        events["label_mktcap"] = np.where(
            events["mv_pct"].isna(), "未知",
            np.where(events["mv_pct"] <= 1 / 3, "小盘",
                     np.where(events["mv_pct"] >= 2 / 3, "大盘", "中盘")))
        cov = events["mv_pct"].notna().mean()
        print(f"  市值分位覆盖率 {cov:.1%}（横截面分位，阈值 1/3、2/3）")

    # ---------- 3. 行业基准 ----------
    bench_map = {"hs300": hs300_close}
    ind_idx = {}
    if not args.no_industry:
        industry = load_industry()
        if industry:
            print(f"\n[2] 自建行业等权指数（{len(set(industry.values()))} 个行业）")
            ind_idx = build_industry_index(closes, industry, calendar)
            events["industry"] = events["code"].map(industry)
            print(f"  可匹配行业的占比 {events['industry'].notna().mean():.1%}")
        else:
            print("\n[2] 无行业分类数据，跳过行业基准")

    # ---------- 4. 前瞻收益 ----------
    print("\n[3] 计算前瞻收益（三重基准 × 5窗口）...")
    frames = []
    for code, grp in events.groupby("code", sort=False):
        s = closes.get(code)
        if s is None:
            continue
        recs = compute_forward_returns(grp, s, calendar, horizons=HORIZONS,
                                       bench={"hs300": hs300_close})
        if recs.empty:
            continue
        recs["code"] = code
        frames.append(recs)
    detail = pd.concat(frames, ignore_index=True)
    detail = detail.merge(
        events[["code", "week_idx", "label_slope", "label_alignment",
                "label_regime", "label_mktcap", "float_mv",
                "dedup_kept"] + (["industry"] if "industry" in events else [])],
        on=["code", "week_idx"], how="left")

    # 行业超额
    # 用向量化替代逐行 .at 赋值：全A股下事件数可达十万级，逐行赋值会慢几十倍。
    if ind_idx:
        ind_arr = {k: v.to_numpy(dtype=float) for k, v in ind_idx.items()}
        t0arr = detail["t0_pos"].to_numpy(dtype=int)
        ind_col = detail["industry"].to_numpy() if "industry" in detail else None
        for N in HORIZONS:
            ex = np.full(len(detail), np.nan)
            ok = (detail[f"ok{N}"].to_numpy(dtype=bool)
                  if f"ok{N}" in detail else np.zeros(len(detail), dtype=bool))
            rets = detail[f"ret{N}"].to_numpy(dtype=float)
            if ind_col is not None:
                for k, arr in ind_arr.items():
                    mask = (ind_col == k) & ok
                    if not mask.any():
                        continue
                    idx = np.nonzero(mask)[0]
                    p0 = t0arr[idx]
                    p1 = p0 + N
                    keep = p1 < len(arr)
                    if not keep.any():
                        continue
                    ii = idx[keep]
                    b0 = arr[p0[keep]]
                    b1 = arr[p1[keep]]
                    good = (b0 > 0) & np.isfinite(b0) & np.isfinite(b1)
                    ex[ii[good]] = rets[ii[good]] - (b1[good] / b0[good] - 1)
            detail[f"ex_industry{N}"] = ex

    print(f"  明细 {len(detail)} 条")

    # ---------- 5. 汇总统计 ----------
    print("\n[4] 汇总统计...")
    cfg = StatsConfig(horizons=HORIZONS, benchmarks=("hs300", "industry"))
    overall = summarize(detail, cfg)
    overall.to_csv(OUT / "summary_overall.csv", index=False, encoding="utf-8-sig")

    strat_rows = []
    for label in ("label_slope", "label_alignment", "label_regime", "label_mktcap"):
        if label not in detail.columns:
            continue
        for key, grp in detail.groupby(label):
            if key in (None, "未知") or len(grp) < 30:
                continue
            sub = summarize(grp.assign(event_type=grp["event_type"]), cfg)
            if sub.empty:
                continue
            sub["stratum"] = label
            sub["stratum_value"] = key
            strat_rows.append(sub)
    stratified = pd.concat(strat_rows, ignore_index=True) if strat_rows else pd.DataFrame()
    if not stratified.empty:
        stratified.to_csv(OUT / "summary_stratified.csv", index=False, encoding="utf-8-sig")

    # ---------- 6. 稳健性分析（不去重口径必做） ----------
    print("\n[5] 稳健性分析（按股票聚类 bootstrap + 去重口径对照）...")
    boot = cluster_bootstrap_table(detail, HORIZONS, n_iter=2000)
    if not boot.empty:
        boot.to_csv(OUT / "robust_bootstrap.csv", index=False, encoding="utf-8-sig")
        show_b = boot[(boot["metric"] == "ret") & (boot["horizon"] == 250)]
        for _, r in show_b.iterrows():
            print(f"  {r['event_type']} 250日: 朴素区间 "
                  f"[{r['naive_ci_lo']:.3f}, {r['naive_ci_hi']:.3f}]  "
                  f"聚类区间 [{r['ci_lo']:.3f}, {r['ci_hi']:.3f}]  "
                  f"p={r['p_cluster']:.4f}  膨胀倍数={r['design_effect']:.2f}")
    dedup_cmp = dedup_compare(detail, cfg, n_iter=1000)
    if not dedup_cmp.empty:
        dedup_cmp.to_csv(OUT / "robust_dedup.csv", index=False, encoding="utf-8-sig")

    # ---------- 7. 集中度（不去重口径必看的诊断） ----------
    conc = (detail.groupby("code").size()
            .sort_values(ascending=False).rename("events").reset_index())
    conc["share"] = conc["events"] / len(detail)
    conc["cum_share"] = conc["share"].cumsum()
    conc.to_csv(OUT / "concentration.csv", index=False, encoding="utf-8-sig")
    top10 = conc["share"].head(10).sum()
    print(f"\n  事件总数 {len(detail)}  涉及标的 {detail['code'].nunique()}")
    print(f"  单只最多事件数 {conc['events'].iloc[0]}")
    print(f"  Top10 标的占样本 {top10:.1%}（>20% 说明结果受少数标的支配）")

    # ---------- 8. 落盘 ----------
    detail.to_csv(OUT / "events_detail.csv.gz", index=False, compression="gzip")

    def _f(x):
        return None if x is None or (isinstance(x, float) and not np.isfinite(x)) else float(x)

    result = {
        "meta": {
            "scope": f"{len(daily)} 只标的",
            "start": START,
            "ma_weeks": MA_WEEKS,
            "band_pct": BAND_PCT,
            "horizons": list(HORIZONS),
            "cooldown_weeks": COOLDOWN_WEEKS,
            "n_events": int(len(detail)),
            "n_events_dedup": n_dedup,
            "n_stocks": int(detail["code"].nunique()),
            "top10_share": _f(top10),
            "data_source": "baostock (adjustflag=1 后复权)",
            "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
            "runtime_sec": round(time.time() - t_start, 1),
        },
        "overall": overall.replace({np.nan: None}).to_dict("records"),
        "stratified": (stratified.replace({np.nan: None}).to_dict("records")
                       if not stratified.empty else []),
        "bootstrap": (boot.replace({np.nan: None}).to_dict("records")
                      if not boot.empty else []),
        "dedup_compare": (dedup_cmp.replace({np.nan: None}).to_dict("records")
                          if not dedup_cmp.empty else []),
        "concentration_top": conc.head(20).to_dict("records"),
        "event_type_counts": detail["event_type"].value_counts().to_dict(),
    }
    with open(OUT / "study_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 62)
    print("核心结果（总样本）")
    print("=" * 62)
    show = [c for c in ("event_type", "horizon", "n_events", "mean", "median",
                        "win_rate", "ex_hs300_mean", "ex_hs300_win_rate")
            if c in overall.columns]
    if not overall.empty:
        print(overall[show].to_string(index=False,
                                      float_format=lambda x: f"{x:.3f}"))
    print(f"\n输出目录: {OUT}  总耗时 {(time.time()-t_start)/60:.2f} 分钟")
    return 0


if __name__ == "__main__":
    sys.exit(main())
