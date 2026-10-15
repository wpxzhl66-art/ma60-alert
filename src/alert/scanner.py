"""触发判定 + 去重状态。"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .config import load, state_dir

OUT = Path(__file__).resolve().parents[2] / "output" / "alert"


def _state_path() -> Path:
    return state_dir() / "alert_state.json"


def _load_state() -> dict:
    p = _state_path()
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _save_state(s: dict):
    _state_path().write_text(json.dumps(s, ensure_ascii=False, indent=1),
                             encoding="utf-8")


def _verify_exdiv(df: pd.DataFrame) -> pd.DataFrame:
    """除权除息日防护（v1.11.1）：对触发股逐只核对「昨收」的真实性。

    行情接口的「昨收」在除权除息日返回的是交易所**除权除息参考价**，
    不是真实昨收。本模块用 ``hfq_last_close / 昨收`` 推复权因子，除权日
    因子会整体失真——实测 2026-10-14 双汇发展除息，偏离值被抬约 7 个
    百分点，产生假触发并推送（真实偏离 -4.96%，被记录成 +2.0%）。

    触发股数量少（通常 < 100 只），逐只拉腾讯不复权日线核对昨收；
    不一致（>0.1%）则用真实昨收重算因子与偏离，并按严格口径重判触发。
    取不到行情的保持原值——网络故障时不能把全部触发丢掉，那会让
    预警整体瘫痪（除权日是少数，且断头序列另有 45 天防线兜底）。
    """
    if df.empty:
        return df
    try:
        from datasource import TencentSource
        src = TencentSource(fq="")
    except Exception:                              # noqa: BLE001
        print("[scanner] 除权复核数据源不可用，跳过（维持原判定）")
        return df

    kept = []
    for code, r in df.iterrows():
        ts = str(r.get("ts", "") or "")
        try:
            qday = pd.to_datetime(ts[:8]) if len(ts) >= 8 else pd.Timestamp(date.today())
        except Exception:                          # noqa: BLE001
            qday = pd.Timestamp(date.today())
        real = None
        try:
            dk = src.fetch_daily(code, fq="")
            dk = dk[dk["date"] < qday]
            if len(dk):
                c = float(dk["close"].iloc[-1])
                real = c if c > 0 else None
        except Exception:                          # noqa: BLE001
            real = None
        if real is None:
            kept.append(r)                         # 取不到 → 保持原值
            continue
        pre = float(r["preclose"])
        if abs(real - pre) / pre <= 0.001:
            kept.append(r)                         # 昨收一致，无需修正
            continue
        prev_hfq = float(r.get("hfq_prev_used", r["hfq_last_close"]))
        factor = prev_hfq / real
        price_hfq = float(r["price"]) * factor
        if not (prev_hfq < float(r["ma60_prev"]) <= price_hfq):
            print(f"[scanner] {code} 除权日复核：昨收 {pre} 与真实昨收 {real} 不符，"
                  f"重算后未触发 → 剔除（原偏离 {float(r['dev_pct'])*100:+.2f}%）")
            continue
        r = r.copy()
        r["factor"] = factor
        r["price_hfq"] = price_hfq
        r["dev_pct"] = price_hfq / float(r["ma60_prev"]) - 1.0
        print(f"[scanner] {code} 除权日修正：昨收 {pre} → 真实 {real}，"
              f"偏离修正为 {float(r['dev_pct'])*100:+.2f}%")
        kept.append(r)
    if not kept:
        return df.iloc[0:0]
    return pd.DataFrame(kept, columns=df.columns)


def evaluate(snapshot: pd.DataFrame, quotes: pd.DataFrame,
             mode: str = "intraday", dedup: bool = True) -> pd.DataFrame:
    """返回当日触发 A 类上穿的股票表。

    判定（严格口径）：
      昨收(hfq) < MA60_prev  且  现价(hfq) >= MA60_prev
    现价 hfq 换算：factor = 快照末收盘 / 行情昨收（两者为同一交易日的
    后复权价与不复权价之比，即当日复权因子）；P_hfq = 现价 × factor。

    输出列在快照列之外追加：chg_pct / amount_wan / turnover / mv_float_yi /
    dev_pct / amt_ratio / is_new / mode（看板排序与推送都依赖这些）。

    dedup=True 时只返回「本日/本周尚未推送过」的新触发（推送口径）；
    dedup=False 时返回全部触发，并在 is_new 列标注是否为新触发
    （看板展示当日全量触发用）。
    """
    df = snapshot.join(quotes, how="inner")
    df = df[(df["preclose"] > 0) & (df["price"] > 0) & (df["ma60_prev"] > 0)].copy()

    # ---- 停牌冻结行情过滤（BUG #6）----
    # 长期停牌股的行情接口仍返回价量 > 0 的冻结数据，且 **ts 也是当天时间戳**
    # （实测 sh.600825 停牌 3 天，ts 仍是 2026-10-15）——「ts 落后」判据对
    # A 股行情源无效（期货源才有真实 ts）。主判据用**当日成交额 = 0**：
    # 停牌股 price == preclose、amount_wan = 0、turnover = 0（实测确认）。
    # 价格不动而 MA60 每周下移时，停牌股可能「被动上穿」→ 无法交易的假触发。
    # 期货模块早有等价机制（RT_MAX_LAG_DAYS），A 股此前缺失。
    if "ts" in df.columns and len(df):
        qday = pd.to_datetime(df["ts"].astype(str).str.slice(0, 8),
                              format="%Y%m%d", errors="coerce")
        mday = qday.max()
        if pd.notna(mday):
            lag = np.array([np.busday_count(d.date(), mday.date())
                            if pd.notna(d) else 0 for d in qday], dtype=int)
        else:
            lag = np.zeros(len(df), dtype=int)
        # 成交额判停牌：行情源异常时（全场 amount=0）不启用，防止误杀全市场
        amt = pd.to_numeric(df.get("amount_wan"), errors="coerce")
        active = ((amt > 0) | amt.isna()).mean() if len(df) else 0.0
        frozen = amt.fillna(0) <= 0 if active >= 0.5 else pd.Series(False, index=df.index)
        drop = (lag >= 2) | frozen.to_numpy()
        n_frozen = int(drop.sum())
        if n_frozen:
            print(f"[scanner] 停牌过滤：{n_frozen} 只当日无成交或行情落后 ≥2 个交易日，"
                  "已剔除（冻结行情可能造成无法交易的假触发）")
            df = df[~drop]

    # ---- 复权因子的日期配对（BUG #4）----
    # 正常生产流程：日线仓停在昨日（D-1），行情「昨收」= D-1 收盘，
    # factor = hfq(D-1) / raw(D-1) 天然同日配对。
    # 但 16:07 更新后手动 force 补跑时，日线已含当日 D：hfq_last_close=D 收盘、
    # preclose=D-1 收盘 → factor 被抬高约当日涨幅（实测凌晨非常态 20 只触发
    # 中 3 只为错位假触发）。修正：错位时改用快照的前一日后复权收盘
    # （hfq_prev_close）配对，prev_below 判定同步用前一日的值。
    if "ts" in df.columns and len(df):
        qday = pd.to_datetime(df["ts"].astype(str).str.slice(0, 8),
                              format="%Y%m%d", errors="coerce")
    else:
        qday = pd.Series(pd.NaT, index=df.index)
    sday = pd.to_datetime(df["last_date"], errors="coerce")
    aligned = (sday < qday) | qday.isna() | sday.isna()
    misaligned = int((~aligned).sum())
    if misaligned and "hfq_prev_close" in df.columns:
        print(f"[scanner] {misaligned} 只快照与行情同日（16:07 更新后补跑场景），"
              "改用前一日后复权收盘配对因子")
        prev_hfq = np.where(aligned, df["hfq_last_close"],
                            df["hfq_prev_close"].fillna(df["hfq_last_close"]))
        df["hfq_prev_used"] = prev_hfq
    else:
        if misaligned:
            print(f"[scanner][警告] {misaligned} 只快照与行情同日但快照无前日收盘列"
                  "（旧快照），因子可能含当日涨跌幅偏差——建议重建快照")
        df["hfq_prev_used"] = df["hfq_last_close"]

    df["factor"] = df["hfq_prev_used"] / df["preclose"]
    # 因子异常（如盘中除权除息、数据错位）直接剔除，宁漏勿错
    df = df[(df["factor"] > 0.01) & (df["factor"] < 100)]
    df["price_hfq"] = df["price"] * df["factor"]
    df["dev_pct"] = df["price_hfq"] / df["ma60_prev"] - 1.0
    # 量比(额)：今日成交额(元) / 近20日均成交额(元)，统一不复权口径
    with np.errstate(all="ignore"):
        df["amt_ratio"] = df["amount_wan"] * 1e4 / df["avg_amt20"]

    prev_below = df["hfq_prev_used"] < df["ma60_prev"]
    now_above = df["price_hfq"] >= df["ma60_prev"]
    df = df[prev_below & now_above].copy()

    # 除权除息日复核（v1.11.1）：行情「昨收」在除权日是除权参考价，
    # 复权因子会失真（双汇发展 2026-10-14 实例）。演示模式跳过。
    if mode != "demo":
        df = _verify_exdiv(df)

    # 去重：同日 / 同周（dedup_weekly）
    st = _load_state()
    today = date.today().isoformat()
    cfg = load()["events"]
    this_week = f"{date.today().isocalendar().year}-W{date.today().isocalendar().week:02d}"
    is_new = []
    for code in df.index:
        rec = st.get(code, {})
        dup = rec.get("last_push_date") == today
        if cfg.get("dedup_weekly", True) and rec.get("last_push_week") == this_week:
            dup = True
        is_new.append(not dup)
    df["is_new"] = is_new
    df["mode"] = mode
    if dedup:
        df = df[df["is_new"]].copy()
    return df


def mark_pushed(codes, mode: str):
    st = _load_state()
    today = date.today().isoformat()
    iso = date.today().isocalendar()
    this_week = f"{iso.year}-W{iso.week:02d}"
    for c in codes:
        st[c] = {"last_push_date": today, "last_push_week": this_week,
                 "last_push_mode": mode, "last_push_ts": datetime.now().isoformat(timespec="seconds")}
    _save_state(st)
