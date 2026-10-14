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
