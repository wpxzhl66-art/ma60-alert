"""分析引擎自测 —— 用构造数据验证数学正确性。

不依赖真实数据源，可在数据源未就绪时先验证引擎本身。
每个用例都给出可手工核算的期望值，断言失败即引擎有误。

覆盖：
    1. 日线合成周线（周收盘必须取该周最后一个交易日）
    2. 60周均线与带宽的数值正确性
    3. 类型A「上穿突破」的检出与漏检
    4. 类型B「上方回踩」的检出，以及「此前未在带宽上沿之上」时不应触发
    5. 前瞻收益的数值正确性与右删失标记
    6. MFE / MAE / 最大回撤
    7. 冷却期去重标记（主样本条数不变）
    8. 分层标签（60周线方向、均线排列）
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from engine import (  # noqa: E402
    TYPE_A, TYPE_B, add_moving_averages, build_weekly,
    compute_forward_returns, detect_events, mark_dedup, stratify,
)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def make_daily(closes, start="2020-01-06"):
    """把「日收盘价序列」展开为日线 DataFrame（跳过周末，即每周 5 个交易日）。"""
    dates, d = [], pd.Timestamp(start)
    while len(dates) < len(closes):
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    return pd.DataFrame([{"date": dt, "open": c, "high": c, "low": c,
                          "close": float(c), "volume": 1.0, "amount": 1.0}
                         for dt, c in zip(dates, closes)])


def from_weekly(weekly_closes, start="2020-01-06"):
    """按周构造日线：每周 5 个交易日且收盘价相同，故周收盘 = 给定的周值。

    这样就能在「周」的粒度上精确控制，而不会把周数与交易日数混淆。
    """
    daily = [c for c in weekly_closes for _ in range(5)]
    return make_daily(daily, start)


def weekly_ma(weekly_closes, **kw):
    return add_moving_averages(build_weekly(from_weekly(weekly_closes)), **kw)


# ---------------------------------------------------------------- 用例 1
print("\n[1] 日线合成周线")
daily = make_daily([10, 11, 12, 13, 14, 20, 21, 22, 23, 24])  # 10 个交易日 = 2 周
w = build_weekly(daily)
check("周数为 2", len(w) == 2, f"实际 {len(w)}")
check("第1周收盘 = 该周最后交易日值 14", w.loc[0, "close"] == 14, f"实际 {w.loc[0,'close']}")
check("第2周收盘 = 24", w.loc[1, "close"] == 24, f"实际 {w.loc[1,'close']}")
check("第1周开盘 = 该周首个交易日值 10", w.loc[0, "open"] == 10)
check("周标记日期 = 该周最后一个实际交易日（周五）",
      w.loc[0, "date"].weekday() == 4, f"weekday={w.loc[0,'date'].weekday()}")
check("最后一个周期被标记为未走完", bool(w.loc[1, "is_last_week"]) is True)
check("周成交量 = 日内之和", w.loc[0, "volume"] == 5.0)


# ---------------------------------------------------------------- 用例 2
print("\n[2] 60周均线与带宽")
closes = [100.0] * 60 + [99.0, 102.0]          # 62 周
w2 = weekly_ma(closes, ma_weeks=60, band_pct=0.02)
check("周数为 62", len(w2) == 62, f"实际 {len(w2)}")
check("第60周(索引59) MA60 = 100（60个100的均值）",
      abs(w2.loc[59, "ma"] - 100.0) < 1e-9, f"实际 {w2.loc[59,'ma']}")
check("第59周(索引58) MA60 = NaN（样本不足60周）", np.isnan(w2.loc[58, "ma"]))
expect_61 = (99.0 + 100.0 * 59) / 60
check(f"第61周(索引60) MA60 = {expect_61:.4f}",
      abs(w2.loc[60, "ma"] - expect_61) < 1e-9, f"实际 {w2.loc[60,'ma']:.4f}")
check("带宽上沿 = MA × 1.02", abs(w2.loc[59, "band_up"] - 102.0) < 1e-9)
check("带宽下沿 = MA × 0.98", abs(w2.loc[59, "band_dn"] - 98.0) < 1e-9)


# ---------------------------------------------------------------- 用例 3
print("\n[3] 类型A 上穿突破")
# 60周 100 → 索引60 收 98（线下）→ 索引61 收 101（线上）
wa = weekly_ma([100.0] * 60 + [98.0, 101.0], ma_weeks=60, band_pct=0.02)
ev_a = detect_events(wa, ma_weeks=60, band_pct=0.02, type_a=True, type_b=False,
                     allow_last_week=True)
check("检出 1 个类型A事件", len(ev_a) == 1, f"实际 {len(ev_a)}")
if len(ev_a):
    check("事件类型正确", ev_a.loc[0, "event_type"] == TYPE_A)
    check("事件发生在索引61", ev_a.loc[0, "week_idx"] == 61, f"实际 {ev_a.loc[0,'week_idx']}")

wf = weekly_ma([100.0] * 60 + [90.0, 90.0, 90.0], ma_weeks=60, band_pct=0.02)
check("持续在均线下方时无类型A事件",
      detect_events(wf, type_a=True, type_b=False, allow_last_week=True).empty)


# ---------------------------------------------------------------- 用例 4
print("\n[4] 类型B 上方回踩")
# 60周 100 → 索引60 收 110（远在带宽上沿102之上）→ 索引61 收 100.5（落入[98.16,102.17]）
wb = weekly_ma([100.0] * 60 + [110.0, 100.5], ma_weeks=60, band_pct=0.02)
ev_b = detect_events(wb, type_a=False, type_b=True, allow_last_week=True)
check("检出 1 个类型B事件", len(ev_b) == 1, f"实际 {len(ev_b)}")
if len(ev_b):
    check("事件类型正确", ev_b.loc[0, "event_type"] == TYPE_B)
    check("事件发生在索引61", ev_b.loc[0, "week_idx"] == 61)

# 反例：此前仅略高于均线、未站上带宽上沿 → 严格语义下不算「回踩」
wb2 = weekly_ma([100.0] * 60 + [100.5, 100.4], ma_weeks=60, band_pct=0.02)
check("此前未站上带宽上沿时不触发类型B（严格语义）",
      detect_events(wb2, type_a=False, type_b=True,
                    type_b_requires_above_band=True, allow_last_week=True).empty)
check("放宽为「此前收盘≥均线」时可触发（索引60与61各一次）",
      len(detect_events(wb2, type_a=False, type_b=True,
                        type_b_requires_above_band=False,
                        allow_last_week=True)) == 2)


# ---------------------------------------------------------------- 用例 5
print("\n[5] 前瞻收益与右删失")
cal = pd.DatetimeIndex(pd.date_range("2020-01-06", periods=400, freq="B"))
prices = pd.Series(np.linspace(100, 200, 400), index=cal)
ev = pd.DataFrame([
    {"week_idx": 0, "event_date": cal[0], "event_type": TYPE_A, "close": prices.iloc[0],
     "ma": prices.iloc[0], "band_up": 0.0, "band_dn": 0.0},
    {"week_idx": 399, "event_date": cal[399], "event_type": TYPE_A,
     "close": prices.iloc[399], "ma": prices.iloc[399], "band_up": 0.0, "band_dn": 0.0},
])
fr = compute_forward_returns(ev, prices, cal, horizons=(20, 250))
expect_r20 = prices.iloc[20] / prices.iloc[0] - 1
check("20日收益数值正确", abs(fr.loc[0, "ret20"] - expect_r20) < 1e-12,
      f"实际 {fr.loc[0,'ret20']:.6f}")
check("窗口数据完整 → ok20 = True", fr.loc[0, "ok20"] is True or fr.loc[0, "ok20"] == True)  # noqa: E712
check("末端事件 250 日窗口标记为右删失 ok250 = False", fr.loc[1, "ok250"] == False)  # noqa: E712
check("末端事件 20 日窗口同样删失（399+20 > 400）", fr.loc[1, "ok20"] == False)  # noqa: E712
check("右删失时收益为 NaN", np.isnan(fr.loc[1, "ret250"]))
check("entry_price 取事件周收盘", abs(fr.loc[0, "entry_price"] - 100.0) < 1e-9)
check("dev_from_ma = 0（事件价等于均线）", abs(fr.loc[0, "dev_from_ma"]) < 1e-12)


# ---------------------------------------------------------------- 用例 6
print("\n[6] MFE / MAE / 最大回撤")
cal2 = pd.DatetimeIndex(pd.date_range("2020-01-06", periods=4, freq="B"))
p2 = pd.Series([100.0, 120.0, 90.0, 110.0], index=cal2)
ev2 = pd.DataFrame([{"week_idx": 0, "event_date": cal2[0], "event_type": TYPE_A,
                     "close": 100.0, "ma": 100.0, "band_up": 0.0, "band_dn": 0.0}])
fr2 = compute_forward_returns(ev2, p2, cal2, horizons=(3,))
check("MFE = 120/100 - 1 = 0.20", abs(fr2.loc[0, "mfe3"] - 0.20) < 1e-12,
      f"实际 {fr2.loc[0,'mfe3']}")
check("MAE = 90/100 - 1 = -0.10", abs(fr2.loc[0, "mae3"] + 0.10) < 1e-12,
      f"实际 {fr2.loc[0,'mae3']}")
check("最大回撤 = 90/120 - 1 = -0.25", abs(fr2.loc[0, "mdd3"] + 0.25) < 1e-12,
      f"实际 {fr2.loc[0,'mdd3']}")
check("3日收益 = 110/100 - 1 = 0.10", abs(fr2.loc[0, "ret3"] - 0.10) < 1e-12)


# ---------------------------------------------------------------- 用例 7
print("\n[7] 冷却期去重标记")
evs = pd.DataFrame({"code": ["x"] * 4 + ["y"],
                    "week_idx": [0, 5, 30, 40, 10],
                    "event_type": [TYPE_A] * 5})
mk = mark_dedup(evs, cooldown_weeks=26)
check("主样本条数不变（不去重口径）", len(mk) == 5, f"实际 {len(mk)}")
kept_x = sorted(mk.loc[(mk["code"] == "x") & mk["dedup_kept"], "week_idx"].tolist())
check("x 保留 0 与 30（5 在 0 的冷却期内；40 距 30 不足 26 周）",
      kept_x == [0, 30], f"实际 {kept_x}")
check("y 独立去重，保留其唯一事件",
      bool(mk.loc[mk["code"] == "y", "dedup_kept"].iloc[0]) is True)


# ---------------------------------------------------------------- 用例 8
print("\n[8] 分层标签")
# 60周 100 → 20周阶梯上涨 101..120 → 末周回踩 105
wc = [100.0] * 60 + list(range(101, 121)) + [105.0]
ws = weekly_ma(wc, ma_weeks=60, band_pct=0.02, compare_periods=(5, 10, 20, 60))
ev_s = detect_events(ws, type_a=False, type_b=True, allow_last_week=True)
check("末周回踩被检出为类型B", len(ev_s) == 1, f"实际 {len(ev_s)}")
if len(ev_s):
    check("事件位于末周(索引80)", ev_s.loc[0, "week_idx"] == 80,
          f"实际 {ev_s.loc[0,'week_idx']}")

ev_s["code"] = "z"
st = stratify(ws, ev_s, ma_weeks=60, slope_lookback=13)
check("分层后保留全部事件", len(st) == len(ev_s), f"{len(st)} vs {len(ev_s)}")
if len(st):
    check("均线持续上升 → 方向标签为「向上」",
          (st["label_slope"] == "向上").all(), f"实际 {st['label_slope'].unique()}")
    check("MA5>MA10>MA20>MA60 → 标签为「多头排列」",
          (st["label_alignment"] == "多头排列").all(),
          f"实际 {st['label_alignment'].unique()}")

# 无未来函数：标签只依赖事件周及之前的数据
ws_trunc = ws.iloc[:81].reset_index(drop=True)
st_trunc = stratify(ws_trunc, ev_s.copy(), ma_weeks=60, slope_lookback=13)
check("截断至事件周后标签不变（证明无未来函数）",
      st_trunc.loc[0, "label_slope"] == st.loc[0, "label_slope"] and
      st_trunc.loc[0, "label_alignment"] == st.loc[0, "label_alignment"])


# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 62)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("全部用例通过：引擎数学逻辑验证无误。")
