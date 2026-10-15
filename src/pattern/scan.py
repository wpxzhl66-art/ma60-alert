"""右侧反转五阶段形态 · 全市场实盘扫描。

数据链路（决定了本模块只能在本机跑）：
    data/daily/*.csv.gz   后复权日线（5290 只，385MB，**不入库** → 云端没有）
  + 腾讯实时行情            当日收盘/成交额，用于合成最后一根 K 线

云端（GitHub Actions）拿不到日线仓，因此形态扫描由**本机定时任务**执行，
结果写 store/state/pattern_triggers.json（推送）与 dashboard/pattern.json（看板），
随代码提交推上仓库，云端收盘确认时把形态段落追加到同一条推送里。

用法（在 src 目录下）：
    python -m pattern.scan              # 扫描 + 写 output/pattern/scan.json
    python -m pattern.scan --quiet      # 只写文件，不打印明细
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pattern import core as pc                       # noqa: E402

DAILY = ROOT / "data" / "daily"
OUT_DIR = ROOT / "output" / "pattern"
STAGE_CODE = {"e4": "A", "e3": "B", "e2": "C"}
STAGE_NAME = {"A": "今日启动", "B": "洗盘观察", "C": "刚突破前高"}


def load_universe() -> tuple[set, dict]:
    """可交易范围（沪深主板 + 创业板 + 科创板，剔除退市/指数）与行业映射。"""
    u = pd.read_csv(ROOT / "data/universe.csv.gz")
    u = u[(u.status == 1) & (u.type == 1)]
    u = u[u.code.str.match(r"(sh\.6|sz\.0|sz\.3)")]
    ind = pd.read_csv(ROOT / "data/industry.csv.gz").dropna(subset=["industry"])
    ind = ind[["code", "industry"]].drop_duplicates("code")
    return set(u.code), dict(zip(ind.code, ind.industry))


def fetch_snapshot(codes: list[str]) -> pd.DataFrame:
    """当日快照：优先用刚抓的实时行情（含成交额/换手/市值，形态判定需要）。

    非交易时段腾讯返回最近一个交易日的收盘数据，同样可用。
    """
    from alert.quoter import fetch_quotes
    q = fetch_quotes(codes)
    if q.empty:
        return q
    q = q[(q.price > 0) & (q.amount_wan > 0)]          # 剔停牌（成交量 0）
    return q


def load_prev_snapshot() -> tuple[str, dict]:
    """读 store/last_quotes.csv（上一交易日收盘快照），用于补日线仓与今日之间的缺口。

    返回 ``(日期, {code: (price, preclose)})``。文件里若混有多个日期则视为不可靠、
    整体丢弃——缺口宁可不补，也不能拿错日期的价格拼进序列。
    """
    p = ROOT / "store" / "last_quotes.csv"
    if not p.exists():
        return "", {}
    out: dict = {}
    day = ""
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:                                    # noqa: BLE001
        return "", {}
    for i, line in enumerate(lines):
        if i == 0 or "," not in line:
            continue
        parts = line.split(",")
        if len(parts) < 4:
            continue
        ts = parts[3].strip()
        if len(ts) < 8:
            continue
        d = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
        if day and d != day:
            return "", {}                                # 混合日期 → 整份丢弃
        day = d
        try:
            out[parts[0].strip()] = (float(parts[1]), float(parts[2]))
        except ValueError:
            continue
    return day, out


def _one(job: tuple) -> dict | None:
    """单只标的：日线 + 缺口补齐 + 当日合成 K 线 → 判定最后一日所处阶段。"""
    code, snap_date, snap, pmin, mvmin, prev = job
    f = DAILY / f"{code.replace('.', '_')}.csv.gz"
    if not f.exists():
        return None
    try:
        df = pd.read_csv(f, parse_dates=["date"], usecols=["date", "close", "amount"])
    except ValueError:      # 精简源可能没有 amount → 量比失效，阶段④只剩涨停一条路径
        try:
            df = pd.read_csv(f, parse_dates=["date"], usecols=["date", "close"])
            df["amount"] = np.nan
        except Exception:                                   # noqa: BLE001
            return None
    except Exception:                                       # noqa: BLE001
        return None
    df = df[df["date"] < pd.Timestamp(snap_date)]
    if len(df) < pc.DD_PRIOR + pc.DD_RECENT:
        return None
    c_last = float(df["close"].iloc[-1])
    rows = []
    # 缺口 1：日线仓末日与上一交易日快照之间的空档（价格可补，成交额缺 → 置 NaN）
    if prev is not None:
        prev_day, pprice, ppre = prev
        if pd.Timestamp(prev_day) > df["date"].iloc[-1] and ppre > 0:
            rows.append({"date": pd.Timestamp(prev_day),
                         "close": c_last * (pprice / ppre), "amount": np.nan})
    # 缺口 2：当日（实时快照）——后复权序列按涨跌幅外推，成交额用实时值（元）
    chg = float(snap["chg_pct"])
    c_syn = (rows[-1]["close"] if rows else c_last) * (1 + chg / 100.0)
    if c_syn < pmin:
        return None
    rows.append({"date": pd.Timestamp(snap_date), "close": c_syn,
                 "amount": float(snap["amount_wan"]) * 1e4})
    df = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)

    res = pc.analyze(df, code=code)
    last = res["n"] - 1
    if last < 0:
        return None
    d = res["detail"].get(last)
    if d is None:
        return None
    # 量比/回撤等指标一律取判定引擎的输出（口径唯一），此处只补行情元信息
    return {**d, "code": code, "stage": STAGE_CODE[d["stage"]], "estage": d["stage"],
            "price": round(float(snap["price"]), 2),
            "turnover": round(float(snap["turnover"]), 2),
            "amount_wan": round(float(snap["amount_wan"]), 1),
            "mv": round(float(snap["mv_float_yi"]), 1),
            "name": str(snap["name"])}


def scan(quiet: bool = False, workers: int = 8) -> dict:
    codes_all, ind_map = load_universe()
    q = fetch_snapshot(sorted(codes_all))
    if q.empty:
        raise SystemExit("[pattern] 实时行情为空，无法扫描")
    snap_date = str(q.iloc[0]["ts"])[:8]
    snap_date = f"{snap_date[:4]}-{snap_date[4:6]}-{snap_date[6:8]}"
    codes = [c for c in q.index if c in codes_all]

    # 数据新鲜度体检：日线仓必须跟得上，否则中间缺 K 线、形态判定失真
    # （曾因丢掉 10-14 一天，把「万和电气」从 A 类错落到 C 类）
    prev_day, prev_map = load_prev_snapshot()
    sample = DAILY / f"{codes[0].replace('.', '_')}.csv.gz"
    lag = ""
    if sample.exists():
        d0 = pd.read_csv(sample, parse_dates=["date"], usecols=["date"])["date"].max()
        gap = int(np.busday_count(d0.date(), pd.Timestamp(snap_date).date()))
        fillable = bool(prev_day) and pd.Timestamp(prev_day) > d0 and pd.Timestamp(prev_day) < pd.Timestamp(snap_date)
        if fillable:
            gap -= 1
        if gap > 0:
            lag = (f"（日线仓止于 {d0.date()}，仍落后 {gap} 个交易日"
                   + ("；已用上一交易日快照补位" if fillable else "")
                   + "，建议先跑日线更新）")
        elif fillable:
            lag = f"（日线仓止于 {d0.date()}，已用 {prev_day} 快照补位）"

    jobs = []
    for c in codes:
        r = q.loc[c]
        pv = prev_map.get(c)
        prev = (prev_day, pv[0], pv[1]) if (pv and prev_day) else None
        jobs.append((c, snap_date, {"chg_pct": r["chg_pct"], "amount_wan": r["amount_wan"],
                                    "price": r["price"], "turnover": r["turnover"],
                                    "mv_float_yi": r["mv_float_yi"], "name": r["name"]},
                     pc.PRICE_MIN, pc.MV_MIN_YI, prev))
    rows: list[dict] = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for rec in ex.map(_one, jobs, chunksize=40):
            if rec is None:
                continue
            if rec["mv"] < pc.MV_MIN_YI or "ST" in rec["name"].upper():
                continue
            rec["industry"] = ind_map.get(rec["code"], "—")
            rows.append(rec)

    for r in rows:
        r["zt"] = bool(r.get("zt"))
    rows.sort(key=lambda r: ({"A": 0, "B": 1, "C": 2}[r["stage"]], -r["chg"]))
    counts = {k: sum(1 for r in rows if r["stage"] == k) for k in ("A", "B", "C")}
    obj = {"day": snap_date, "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
           "snapshot_date": snap_date, "prev_snapshot": prev_day,
           "scanned": len(jobs), "lag": lag,
           "counts": counts, "rows": rows}
    if not quiet:
        print(f"[pattern] 快照 {snap_date} · 扫描 {len(jobs)} 只 · "
              f"A 启动 {counts['A']} / B 洗盘 {counts['B']} / C 新突破 {counts['C']} {lag}")
        for s in ("A", "B", "C"):
            sub = [r for r in rows if r["stage"] == s][:8]
            if not sub:
                continue
            print(f"  [{s}] {STAGE_NAME[s]}")
            for r in sub:
                print(f"     {r['code']} {r['name'][:6]:<7} {r['price']:>8.2f}"
                      f" {r['chg']:+6.2f}%  量比 {r['vol']}  回撤 {r['retrace']}%  {r['industry']}")
    return obj


def main() -> None:
    ap = argparse.ArgumentParser(description="右侧反转五阶段形态扫描")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--print-json", action="store_true")
    a = ap.parse_args()
    obj = scan(quiet=a.quiet)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / f"scan_{obj['day']}.json"
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (OUT_DIR / "latest.json").write_text(
        json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"[pattern] 已写 {p}")
    if a.print_json:
        print(json.dumps(obj["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
