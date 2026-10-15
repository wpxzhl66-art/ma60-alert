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
        factor = float(r["hfq_last_close"]) / real
        price_hfq = float(r["price"]) * factor
        if not (float(r["hfq_last_close"]) < float(r["ma60_prev"]) <= price_hfq):
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
    df["factor"] = df["hfq_last_close"] / df["preclose"]
    # 因子异常（如盘中除权除息、数据错位）直接剔除，宁漏勿错
    df = df[(df["factor"] > 0.01) & (df["factor"] < 100)]
    df["price_hfq"] = df["price"] * df["factor"]
    df["dev_pct"] = df["price_hfq"] / df["ma60_prev"] - 1.0
    # 量比(额)：今日成交额(元) / 近20日均成交额(元)，统一不复权口径
    with np.errstate(all="ignore"):
        df["amt_ratio"] = df["amount_wan"] * 1e4 / df["avg_amt20"]

    prev_below = df["hfq_last_close"] < df["ma60_prev"]
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
