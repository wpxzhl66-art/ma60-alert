"""60周线快照：MA60_prev、均线方向、行业、年内上穿计数、20日均额。

两条数据路径共用同一判定核心 `stock_from_weekly()`：
  - 本地：data/daily 日线 → 周收盘（build_snapshot）
  - 云端：store/ 精简仓（cloud_store.build_snapshot_from_store）

口径（与研究管线一致）：
  - 周收盘 = 该周最后一个交易日收盘（后复权）
  - MA60_prev = 截至「上一已完成交易周」的 60 周收盘均值
    （今天若是周六/周日，则刚结束的周也算已完成）
  - 年内上穿 = 过去 52 个已完成周内，周线级别 A 类事件次数
"""
from __future__ import annotations

import glob
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
OUT = ROOT / "output" / "alert"
MA_WEEKS = 60
SLOPE_WEEKS = 13
# 断头序列防护（v1.11.1）：在市标的的日线若落后超过该天数，说明其序列
# 已不可信（增量长期失败被静默跳过，或长期停牌）。用断头序列算出的
# MA60 是几年前的旧值，dev 会完全失真——实测 2026-10-14 双汇发展
# 因序列断头 3.5 年 + 除息日昨收失真，被算出 +2% 的假偏离并推送
# （真实偏离 -4.96%）。宁漏勿错：这类标的直接排除出扫描并打印告警。
STALE_MAX_DAYS = 45


def cutoff_monday(today: date | None = None) -> pd.Timestamp:
    """「第一个未完成周」的周一。周末运行时，刚结束的周视为已完成。"""
    today = today or date.today()
    shift = {5: 3, 6: 2}.get(today.weekday(), 0)   # 周六+3 -> 下周一
    d = pd.Timestamp(today + timedelta(days=shift))
    return d - pd.Timedelta(days=d.weekday())


def stock_from_weekly(code: str, weekly: pd.Series,
                      cutoff: pd.Timestamp | None = None) -> dict | None:
    """由「日线或周线」收盘序列计算快照行。

    输入可以是日线收盘（DatetimeIndex，函数内部聚合为周线）或已聚合的
    周线收盘；统一取每周最后一个交易日的收盘，只保留已完成周。
    信息不足（< 61 周）返回 None。
    """
    if not isinstance(weekly.index, pd.PeriodIndex):
        weekly = weekly.copy()
        weekly.index = pd.DatetimeIndex(weekly.index).to_period("W")
    w = weekly.groupby(level=0).last().sort_index()      # 周内取最后交易日
    cutoff = cutoff or cutoff_monday()
    w = w[w.index.to_timestamp(how="end").normalize() < cutoff]
    if len(w) < MA_WEEKS + 1:
        return None

    closes = w.values.astype(float)
    ma = np.convolve(closes, np.ones(MA_WEEKS) / MA_WEEKS, mode="valid")
    ma_now = float(ma[-1])
    ma_13w = float(ma[-1 - SLOPE_WEEKS]) if len(ma) > SLOPE_WEEKS else np.nan

    # 年内 A 类上穿计数：ma[j] 对应 closes[j + MA_WEEKS - 1]
    start = max(MA_WEEKS + 1, len(closes) - 52)
    cnt = 0
    for i in range(start, len(closes)):
        if closes[i - 1] < ma[i - 1 - MA_WEEKS] and closes[i] >= ma[i - MA_WEEKS]:
            cnt += 1

    return {
        "code": code,
        "ma60_prev": round(ma_now, 4),
        "weekly_closes": w,                      # 供图表使用（不落盘）
        "a_events_1y": cnt,
        "slope": ("向上" if ma_now > ma_13w * 1.01
                  else "向下" if ma_now < ma_13w * 0.99 else "走平"),
    }


def _industry_map() -> dict:
    # 云端优先读 store/（已随代码入库）；本地回退 data/
    for cand in ("industry_sw.csv.gz", "industry.csv.gz"):
        for base in (ROOT / "store", DATA):
            p = base / cand
            if p.exists():
                d = pd.read_csv(p, dtype=str)
                c_code = "code" if "code" in d.columns else d.columns[1]
                c_ind = "industry_sw" if "industry_sw" in d.columns else "industry"
                return dict(zip(d[c_code].astype(str), d[c_ind].astype(str)))
    return {}


def build_snapshot(progress: bool = True) -> pd.DataFrame:
    """本地路径：遍历 data/daily 构建快照（约 2~4 分钟，落盘缓存）。"""
    uni = None
    if (DATA / "universe.csv.gz").exists():
        uni = pd.read_csv(DATA / "universe.csv.gz", dtype=str).set_index("code")
    cutoff = cutoff_monday()
    ind_map = _industry_map()

    rows = []
    stale_codes = []
    files = sorted(glob.glob(str(DATA / "daily" / "*.csv.gz")))
    for i, f in enumerate(files, 1):
        code = Path(f).name.replace(".csv.gz", "").replace("_", ".", 1)
        if uni is not None and code in uni.index and str(uni.at[code, "status"]) != "1":
            continue                                    # 退市股不参与预警
        try:
            df = pd.read_csv(f, parse_dates=["date"])
        except Exception:  # noqa: BLE001
            continue
        r = stock_from_weekly(code, df.set_index("date")["close"], cutoff)
        if r is None:
            continue
        d20 = df.sort_values("date").tail(20)
        r["avg_amt20"] = (float(d20["amount"].mean())
                          if "amount" in d20.columns else np.nan)
        r["hfq_last_close"] = float(df.sort_values("date").iloc[-1]["close"])
        r["last_date"] = df["date"].max().strftime("%Y-%m-%d")
        r["industry"] = ind_map.get(code, "未知")
        r.pop("weekly_closes")
        # 断头序列防护：日线落后过久的在市标的不参与判定（宁漏勿错，
        # 见 STALE_MAX_DAYS 注释）。停牌股同样被排除——反正无法交易，
        # 预警对它没有意义；复牌后数据更新会自动重新纳入。
        if (pd.Timestamp(date.today()) - pd.Timestamp(r["last_date"])).days > STALE_MAX_DAYS:
            stale_codes.append(code)
            continue
        rows.append(r)
        if progress and i % 500 == 0:
            print(f"  快照 {i}/{len(files)}", flush=True)

    if stale_codes:
        print(f"[snapshot][警告] {len(stale_codes)} 只标的日线落后超过 "
              f"{STALE_MAX_DAYS} 天，已排除出扫描（序列断头/长期停牌，"
              f"MA60 不可信）：{stale_codes[:20]}"
              + (" ..." if len(stale_codes) > 20 else ""))

    snap = pd.DataFrame(rows).set_index("code")
    OUT.mkdir(parents=True, exist_ok=True)
    snap.to_csv(OUT / "ma60_base.csv")
    snap.to_csv(OUT / f"ma60_base_{date.today():%Y%m%d}.csv")
    return snap


def load_snapshot(max_age_days: int = 3) -> pd.DataFrame | None:
    p = OUT / "ma60_base.csv"
    if not p.exists():
        return None
    if date.today() - date.fromtimestamp(p.stat().st_mtime) > timedelta(days=max_age_days):
        return None
    return pd.read_csv(OUT / "ma60_base.csv", index_col="code")
