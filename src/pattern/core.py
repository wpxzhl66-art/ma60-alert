"""右侧反转五阶段形态 · 判定引擎（口径唯一真源）。

思路来源：用户《股票选股思路及讲解.docx》+ 福成股份示意 K 线（2026-10-15）。

  阶段①  前期沿下行趋势走低（只观察，不介入）
         量化：更早 250 日的高点 → 其后 60 日低点，回撤 >= 25%
  阶段②  不再创新低、向上突破前高 = 反转二次确认
         量化：创 60 日新高（突破本轮反弹前高）；首个突破日须在近 45 日内
         （不是「创 250 日新高」——原文档示意图例股福成股份突破的是反弹前高，
           用年线高点判定会把它漏掉）
  阶段③  突破后回踩支撑、缩量止跌企稳 = 洗盘三次确认
         量化：距突破高点回撤 4%~20%、回踩不少于 3 日、近 3 日不再破前一日 0.5%
  阶段④  涨停或放量大阳线启动 = 介入点
         量化：涨停，或（涨幅 >= 5% 且量比 >= 1.8），且收盘创回踩段新高
  阶段⑤  持有至 20%~50% 收益分批止盈（持有管理，不参与扫描）

三类输出（与看板、推送的 A/B/C 一一对应）：
  e4 = 阶段④启动日  → 实盘 A 类「今日启动」
  e3 = 阶段③企稳中  → 实盘 B 类「洗盘观察」
  e2 = 阶段②刚突破  → 实盘 C 类「观察反转是否成立」

⚠️ 本模块是**判定口径的唯一真源**：实盘扫描（pattern/scan.py）与全历史回测
   （output/_signal/pattern_backtest.py）都导入这里。改阈值即同时改变两处，
   不要在别处复制判定逻辑——上一次 MA60 的教训就是两份口径并存后悄悄漂移。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- 阈值（唯一真源）
DD_MIN = -0.25            # 阶段①：前期回撤下限（更早 250 日高点 → 其后 60 日低点）
DD_PRIOR = 250            # 阶段①：更早区间的长度（交易日）
DD_RECENT = 60            # 阶段①：近期区间长度（含当日）

NH_WINDOW = 60            # 阶段②：突破窗口（创 60 日新高 = 突破本轮反弹前高）
NH_MAX_AGE = 45           # 阶段②：首个突破日距当日不得超过 N 交易日（再老就不算「新反转」）
NH_FRESH = 5              # 阶段②：突破后 N 日内算「刚突破」（e2 观察点）
NH_BRK_MIN_PCT = 3.0      # 阶段②：突破日须为有效阳线（涨幅下限 %）

RETRACE_LO = -0.20        # 阶段③：回踩深度下限
RETRACE_HI = -0.04        # 阶段③：回踩深度上限
PB_MIN_BARS = 3           # 阶段③：回踩至少 N 个交易日
STABLE_TOL = 0.995        # 阶段③：企稳判定——后一日不低于前一日 × 该系数

YANG_PCT = 5.0            # 阶段④：大阳线涨幅下限（%）
VOLRATIO_MIN = 1.8        # 阶段④：大阳线量比下限（成交额 / 前 20 日均额）
ZT_MAIN = 9.8             # 主板涨停判定线（%）
ZT_GEM = 19.6             # 创业板 / 科创板涨停判定线（%）

PRICE_MIN = 2.0           # 实盘剔除仙股（仅实盘用：仓库日线为后复权，价格不可比）
MV_MIN_YI = 10.0          # 流通市值下限（亿元），回测与实盘通用


def limit_pct(code: str) -> float:
    """涨停幅度判定线（%）：创业板 / 科创板 20%，其余 10%。"""
    seg = code.split(".")[-1][:2] if "." in code else code[:2]
    return ZT_GEM if seg in ("30", "68") else ZT_MAIN


def _rolling_max_shift(s: pd.Series, w: int, shift: int) -> np.ndarray:
    """索引 i 处 = 区间 [i-shift-w+1, i-shift] 的最大值（NaN 表示区间不完整）。"""
    return s.rolling(w).max().shift(shift).to_numpy()


def analyze(df: pd.DataFrame, code: str = "") -> dict:
    """对单只标的的日线序列逐日判定，返回三类事件索引与明细。

    ``df`` 需按日期升序，含 ``close``；``amount`` 缺失时量比相关判定自动失效
    （阶段④只剩涨停一条路径）。全程只用 i 及之前的数据，无前视偏差。

    返回::

        {"n": 序列长度, "e2": [...], "e3": [...], "e4": [...], "detail": {i: {...}}}

    detail 仅含被判为某阶段的日子，字段见 _rec()。
    """
    out: dict = {"n": 0, "e2": [], "e3": [], "e4": [], "detail": {}}
    if df is None or len(df) == 0:
        return out
    c = df["close"].to_numpy(dtype=float)
    n = c.size
    if n < DD_PRIOR + DD_RECENT:
        return out
    out["n"] = n

    has_amt = "amount" in df.columns
    amt = df["amount"].to_numpy(dtype=float) if has_amt else np.full(n, np.nan)

    s = pd.Series(c)
    # ---- 阶段①：前期深跌 ----
    trough = s.rolling(DD_RECENT).min().to_numpy()               # [i-59, i]
    peak = _rolling_max_shift(s, DD_PRIOR, DD_RECENT)            # [i-309, i-60]
    with np.errstate(invalid="ignore", divide="ignore"):
        dd = trough / peak - 1.0
    ok_dd = np.isfinite(dd) & (dd <= DD_MIN)

    # ---- 阶段②：创 NH_WINDOW 日新高 ----
    prevmax = _rolling_max_shift(s, NH_WINDOW, 1)                # [i-60, i-1]
    ishigh = np.zeros(n, dtype=bool)
    fin = np.isfinite(prevmax)
    ishigh[fin] = c[fin] > prevmax[fin]

    # 新高簇起点（brk）与「i 之前最后一个新高日」（hi）——均向量化
    idx = np.arange(n)
    seg_start = ishigh & ~np.concatenate([[False], ishigh[:-1]])
    brk_of = np.maximum.accumulate(np.where(seg_start, idx, -1))  # i 所属簇的起点
    hi_run = np.maximum.accumulate(np.where(ishigh, idx, -1))     # 截至 i 的最后新高日
    last_hi = np.concatenate([[-1], hi_run[:-1]])                 # 严格早于 i 的最后新高日
    has_hi = last_hi >= 0
    hi_pos = np.where(has_hi, last_hi, 0)
    brk_age = np.where(has_hi, idx - brk_of[hi_pos], 10 ** 9)     # 首个突破日距今日数
    c_hi = np.where(has_hi, c[hi_pos], np.nan)

    # ---- 阶段④：量比 / 涨停 / 大阳线 ----
    if has_amt:
        a20 = pd.Series(amt).rolling(20).mean().shift(1).to_numpy()
        with np.errstate(invalid="ignore", divide="ignore"):
            vr = amt / a20
    else:
        vr = np.full(n, np.nan)
    chg = np.zeros(n)
    chg[1:] = c[1:] / c[:-1] - 1.0
    chg_pct = chg * 100.0
    zt = chg_pct >= limit_pct(code) - 0.05
    strong = zt | ((chg_pct >= YANG_PCT) & np.isfinite(vr) & (vr >= VOLRATIO_MIN))

    # ---------------- e2：刚突破新高（有效阳线 + 近距离）----------------
    for i in np.flatnonzero(ishigh):
        if not ok_dd[i]:
            continue
        b = brk_of[i]
        if b < 0 or i - b > NH_FRESH:
            continue
        if chg_pct[i] < NH_BRK_MIN_PCT:
            continue
        out["e2"].append(int(i))
        out["detail"][int(i)] = _rec("e2", i, chg_pct, vr, dd, retrace=None,
                                     span=None, brk_age=int(i - b))

    # ---------------- e3 / e4：回踩段内的企稳与启动 ----------------
    # 向量化粗筛：结构新鲜 + 今日非新高 + 已自高点回撤（e3 另需收在高点下方）
    fresh = has_hi & (brk_age <= NH_MAX_AGE)
    cand = np.flatnonzero(ok_dd & fresh & ~ishigh & (c < c_hi))
    for i in cand:
        hi = int(last_hi[i])
        after = c[hi:i + 1]
        m = after.size
        if m < 2 or m - 1 < PB_MIN_BARS:
            continue
        lo = int(np.argmin(after))
        retrace = after[lo] / c[hi] - 1.0
        if not (RETRACE_LO <= retrace <= RETRACE_HI):
            continue
        span = m - 1 - lo                       # 低点之后走过的交易日数
        zone = after[1:-1].max() if m > 2 else after[lo]   # 回踩段内最高收盘（不含高点与当日）
        stable = span >= 3 and bool(np.all(after[lo + 1:] >= after[lo:-1] * STABLE_TOL))
        b_age = int(brk_age[i])
        if strong[i] and c[i] > zone:
            out["e4"].append(int(i))
            out["detail"][int(i)] = _rec("e4", i, chg_pct, vr, dd, retrace, span, b_age,
                                         zt=bool(zt[i]))
        elif (not strong[i]) and stable and c[i] < c[hi] * 0.99:
            out["e3"].append(int(i))
            out["detail"][int(i)] = _rec("e3", i, chg_pct, vr, dd, retrace, span, b_age)
    return out


def _rec(stage: str, i: int, chg_pct, vr, dd, retrace, span, brk_age, zt=False) -> dict:
    def f(v, nd=2):
        return None if v is None or not np.isfinite(v) else round(float(v), nd)
    return {
        "stage": stage,
        "chg": f(chg_pct[i]),
        "vol": f(vr[i]),
        "dd": f(dd[i] * 100, 1) if np.isfinite(dd[i]) else None,
        "retrace": f(retrace * 100, 1) if retrace is not None else None,
        "span": int(span) if span is not None else None,
        "brk_age": brk_age,
        "zt": bool(zt),
    }


def stage_label(stage: str) -> str:
    return {"e4": "阶段④ 今日启动", "e3": "阶段③ 洗盘企稳",
            "e2": "阶段② 刚突破前高"}.get(stage, stage)
