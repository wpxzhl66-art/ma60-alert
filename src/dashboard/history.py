"""历史触发流水：把「上穿 60 周线」的标的按日期汇成一份可长期追踪的数据。

产物：dashboard/history.json（前端直接消费）

两条来源互补：
  · 重算 —— data/daily 全量日线（本地 385MB，不入库）→ 精确到日，
            判定口径与 scanner.evaluate / ma60_base 完全一致
  · 追加 —— store/state/latest_triggers.json（云端可用）→ 当日扫描结果

云端仓库没有 data/，所以云端只能追加；本地可全量重算。
重算保留「比本地日线更新」的既有事件 —— 那几天本地算不出来。
"""
from __future__ import annotations

import glob
import gzip
import json
from datetime import date, datetime, timedelta, timezone
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "daily"
BENCH = ROOT / "data" / "benchmark" / "hs300.csv.gz"
STORE = ROOT / "store"
STATE = STORE / "state"
OUT = ROOT / "dashboard"

MA_WEEKS = 60
WINDOW_DAYS = 30          # 看板默认回溯窗口（自然日）
CST = timezone(timedelta(hours=8))
_NEED = {"date", "close", "amount", "turn", "float_mv"}


def _now() -> datetime:
    return datetime.now(CST)


def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------
# 基础映射
# --------------------------------------------------------------------------
def _load_names() -> dict:
    p = STORE / "stock_names.csv"
    if not p.exists():
        return {}
    out = {}
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
        if i == 0 or "," not in line:
            continue
        c, n = line.split(",", 1)
        out[c.strip()] = n.strip()
    return out


def _load_industry() -> dict:
    p = STORE / "industry_sw.csv.gz"
    if not p.exists():
        return {}
    with gzip.open(p, "rt", encoding="utf-8") as f:
        out = {}
        for line in f:
            parts = line.rstrip("\n").split(",")
            if len(parts) >= 2 and parts[0] != "code":
                out[parts[0].strip()] = parts[1].strip()
    return out


def _live_codes() -> set:
    """在市标的（退市股不参与预警）。universe 缺失时不设限。"""
    p = ROOT / "data" / "universe.csv.gz"
    if not p.exists():
        return set()
    try:
        u = pd.read_csv(p, dtype=str)
        return set(u[u["status"] == "1"]["code"])
    except Exception:  # noqa: BLE001
        return set()


def _calendar() -> list:
    """交易日历：优先沪深300基准，回退用任一只主板股的日线日期。"""
    for p in (BENCH, DATA / "sh_600000.csv.gz"):
        if not p.exists():
            continue
        try:
            d = pd.read_csv(p, usecols=["date"], parse_dates=["date"])
            return sorted(pd.DatetimeIndex(d["date"].unique()))
        except Exception:  # noqa: BLE001
            continue
    return []


def qfq_factor() -> dict:
    """每只股票的后复权→前复权换算常数 k（前复权 = 后复权 × k）。

    k = 最新真实价 / 最新后复权价。两个量取自同一交易日的收盘：
      store/last_close.csv   的 hfq_last_close
      store/last_quotes.csv  的 preclose（行情快照当日的昨收 = 该日收盘）
    偏离度是无量纲的，不换算也准；但价格与行情软件差一个常数，用户会困惑。
    """
    lc = STORE / "last_close.csv"
    lq = STORE / "last_quotes.csv"
    if not lc.exists() or not lq.exists():
        return {}
    try:
        a = pd.read_csv(lc, dtype={"code": str})
        b = pd.read_csv(lq, dtype={"code": str})
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for r in a.merge(b, on="code", how="inner").itertuples():
        hfq, pre = float(r.hfq_last_close), float(r.preclose)
        if hfq > 0 and pre > 0 and 1e-4 < pre / hfq < 1e2:
            out[r.code] = pre / hfq
    return out


# --------------------------------------------------------------------------
# 一、重算：data/daily → 事件（日级精度）
# --------------------------------------------------------------------------
def _scan_file(args) -> tuple:
    """单只股票：算窗口内事件 + 当前状态。返回 (code, events, state)。"""
    f, start, end = args
    code = Path(f).name.replace(".csv.gz", "").replace("_", ".", 1)
    # 不同数据源的列不一致（腾讯兜底只存 date/close/turn/amount），按需取列
    try:
        df = pd.read_csv(f, usecols=lambda c: c in _NEED, parse_dates=["date"])
    except Exception:  # noqa: BLE001
        return (code, [], None)
    for c in ("amount", "turn", "float_mv"):
        if c not in df.columns:
            df[c] = np.nan
    df = (df.dropna(subset=["date", "close"]).sort_values("date")
            .drop_duplicates("date", keep="last"))
    if len(df) < 200:
        return (code, [], None)

    s = df.set_index("date")["close"].astype(float)
    # 周收盘 = 该周最后一个交易日收盘；周标签与 ma60_base 同构（W-SUN）
    wk = s.index.to_period("W")
    wclose = s.groupby(wk).last().sort_index()
    n = len(wclose)
    if n < MA_WEEKS + 1:
        return (code, [], None)

    # 与线上同算法（np.convolve），避免浮点累加顺序引入差异
    ma = np.convolve(wclose.values, np.ones(MA_WEEKS) / MA_WEEKS, mode="valid")
    mp = np.full(n, np.nan)
    mp[MA_WEEKS:] = ma[:n - MA_WEEKS]        # mp[i] = 截至第 i-1 周的 MA60
    ma_s = pd.Series(pd.Series(mp, index=wclose.index).reindex(wk).values,
                     index=s.index)

    prev = s.shift(1)
    hit = (prev < ma_s) & (s >= ma_s) & ma_s.notna() & (prev > 0)
    hit &= (s.index >= start) & (s.index <= end)

    idx = df.set_index("date")
    events = []
    for ts in s.index[hit]:
        c, m = float(s.loc[ts]), float(ma_s.loc[ts])
        pv = float(prev.loc[ts])
        r = idx.loc[ts]
        events.append({
            "d": ts.strftime("%Y-%m-%d"),
            "dev": round((c / m - 1) * 100, 3),
            "hx": c,                                          # 后复权收盘
            "mx": m,                                          # 后复权 60 周线
            # 后复权序列的日间比值就是真实涨跌幅（除权日也连续）
            "chg": round((c / pv - 1) * 100, 2) if pv > 0 else None,
            "amt": round(float(r["amount"]) / 1e4, 1) if pd.notna(r["amount"]) else None,
            "to": round(float(r["turn"]), 2) if pd.notna(r["turn"]) else None,
            "m": "close",
        })

    last = float(s.iloc[-1])
    mnow = float(ma_s.iloc[-1]) if pd.notna(ma_s.iloc[-1]) else 0.0
    mv = float(idx["float_mv"].iloc[-1]) if pd.notna(idx["float_mv"].iloc[-1]) else None
    state = {
        "hx_now": last,
        "dev_now": round((last / mnow - 1) * 100, 3) if mnow else None,
        "above": bool(mnow and last >= mnow),
        "mv": round(mv / 1e8, 1) if mv else None,
    }
    return (code, events, state)


def rebuild(window_days: int = WINDOW_DAYS) -> tuple:
    """全量重算。返回 (events, states, meta)。data/daily 不存在时返回空。"""
    files = sorted(glob.glob(str(DATA / "*.csv.gz")))
    if not files:
        return [], {}, {}

    live = _live_codes()
    if live:
        files = [f for f in files
                 if Path(f).name.replace(".csv.gz", "").replace("_", ".", 1) in live]
    cal = _calendar()
    end = cal[-1] if cal else pd.Timestamp(date.today())
    start = end - pd.Timedelta(days=window_days)

    events, states = [], {}
    jobs = [(f, start, end) for f in files]
    with Pool(min(12, max(1, len(files) // 400))) as pool:
        for code, evs, st in pool.imap_unordered(_scan_file, jobs, chunksize=40):
            if evs:
                for e in evs:
                    e["c"] = code
                events.extend(evs)
            if st:
                states[code] = st
    meta = {
        "data_date": end.strftime("%Y-%m-%d"),
        "cal_days": [d.strftime("%Y-%m-%d") for d in cal if d >= start],
    }
    print(f"[history] 重算 {len(events)} 事件 / {len(states)} 只状态，"
          f"{start.date()} ~ {end.date()}")
    return events, states, meta


# --------------------------------------------------------------------------
# 二、追加：store/state/latest_triggers.json → 当日事件
# --------------------------------------------------------------------------
def from_latest() -> tuple:
    """取最近一次扫描的触发清单（云端唯一可用的来源）。"""
    latest = _read_json(STATE / "latest_triggers.json", {}) or {}
    day, rows = latest.get("day"), (latest.get("rows") or [])
    if not day or not rows:
        return [], {}
    events, states = [], {}
    for r in rows:
        c = r.get("code")
        if not c:
            continue
        dev = r.get("dev_pct")
        events.append({
            "c": c, "d": day,
            "dev": round(float(dev), 3) if dev is not None else None,
            "hx": None, "mx": None,
            "chg": r.get("chg_pct"), "amt": r.get("amount_wan"),
            "to": r.get("turnover"),
            "m": "intraday" if latest.get("mode") == "intraday" else "close",
        })
        states[c] = {
            "dev_now": round(float(dev), 3) if dev is not None else None,
            "above": True,                       # 触发即站上 60 周线
            "mv": r.get("mv_float_yi"),
        }
    return events, states


# --------------------------------------------------------------------------
# 三、合并 → history.json
# --------------------------------------------------------------------------
def build(window_days: int = WINDOW_DAYS, out: Path | None = None) -> dict:
    out = out or (OUT / "history.json")
    names, inds, kfac = _load_names(), _load_industry(), qfq_factor()

    events, states, meta = rebuild(window_days)
    source = "rebuild" if events else "append"

    # 本地日线还没更新到的那几天：保留既有事件，不丢数据
    old = _read_json(out, {}) or {}
    since = meta.get("data_date") or ""
    kept = []
    if since:
        for e in (old.get("events") or []):
            if e.get("d", "") > since:
                e.pop("px", None)
                e.pop("ma", None)
                kept.append(e)

    app_ev, app_st = from_latest()
    app_ev = [e for e in app_ev if not since or e["d"] > since]

    # 去重（同一标的同一天只留一条）；按优先级 app < kept < 重算
    base = {}
    for e in app_ev + kept + events:
        base[e["c"] + "|" + e["d"]] = e
    evs = sorted(base.values(), key=lambda e: (e["d"], e["c"]))

    # 窗口裁剪：以最新事件日为基准
    if evs:
        end = max(e["d"] for e in evs)
        cut = (datetime.fromisoformat(end) - timedelta(days=window_days)).date()
        evs = [e for e in evs if e["d"] >= cut.isoformat()]
        start = evs[0]["d"]
    else:
        end, start = "", ""

    all_st = dict(states)
    all_st.update(app_st)

    # 价格换算为前复权（看盘口径）；同时留下后复权值供收益计算
    for e in evs:
        k = kfac.get(e["c"]) or 0
        e["px"] = round(e["hx"] * k, 3) if (k and e.get("hx")) else None
        e["ma"] = round(e["mx"] * k, 3) if (k and e.get("mx")) else None
        e.pop("hx", None)
        e.pop("mx", None)

    # 股票聚合
    stocks: dict[str, dict] = {}
    for e in evs:
        c = e["c"]
        s = stocks.get(c)
        if s is None:
            stocks[c] = s = {"c": c, "n": names.get(c, c), "i": inds.get(c, "未知"),
                             "cnt": 1, "first": e["d"], "last": e["d"],
                             "_px0": e["px"]}
        else:
            s["cnt"] += 1
            if e["d"] < s["first"]:
                s["first"] = e["d"]
            if e["d"] > s["last"]:
                s["last"] = e["d"]
    for c, s in stocks.items():
        st = all_st.get(c) or {}
        k = kfac.get(c) or 0
        px0, hx_now = s.pop("_px0", None), st.get("hx_now")
        s["dev_now"] = st.get("dev_now")
        s["above"] = st.get("above")
        s["mv"] = st.get("mv")
        s["ret"] = (round((hx_now * k / px0 - 1) * 100, 2)
                    if (px0 and hx_now and k) else None)

    # 摘要
    by_day: dict[str, int] = {}
    by_ind: dict[str, int] = {}
    for e in evs:
        by_day[e["d"]] = by_day.get(e["d"], 0) + 1
        ind = (stocks.get(e["c"]) or {}).get("i", "未知")
        by_ind[ind] = by_ind.get(ind, 0) + 1
    peak = max(by_day.items(), key=lambda x: x[1]) if by_day else ("", 0)
    last = max(by_day.items()) if by_day else ("", 0)
    above = sum(1 for s in stocks.values() if s.get("above") is True)

    obj = {
        "generated_at": _now().isoformat(timespec="seconds"),
        "window_days": window_days,
        "start": start,
        "end": end,
        "data_date": meta.get("data_date") or end,
        "source": source,
        "summary": {
            "events": len(evs),
            "stocks": len(stocks),
            "trade_days": len(set(meta.get("cal_days") or []) | set(by_day)) or len(by_day),
            "active_days": len(by_day),
            "peak": {"d": peak[0], "n": peak[1]},
            "last": {"d": last[0], "n": last[1]},
            "above_now": above,
            "below_now": len(stocks) - above,
            "industries": sorted(by_ind.items(), key=lambda x: -x[1])[:12],
            "days": sorted(by_day.items()),
        },
        "events": evs,
        "stocks": stocks,
    }
    text = _dump(obj)
    if out.exists():
        try:
            if out.read_text(encoding="utf-8") == text:
                print(f"[history] 内容未变，跳过（{len(evs)} 事件）")
                return obj
        except Exception:  # noqa: BLE001
            pass
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"[history] 已写入 {out.name}：{len(evs)} 事件 / {len(stocks)} 只"
          f" / {len(text.encode('utf-8'))/1024:.0f} KB（{source}）")
    return obj


if __name__ == "__main__":
    build()
