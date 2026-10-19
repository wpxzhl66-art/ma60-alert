"""全市场选股快照（v1.28.0 新增）：为看板「选股」条件面板提供数据底座。

思路：复权价的比例运算（偏离、上穿、多头排列、连涨、新高、量比）与复权因子无关，
可直接从 data/daily 的 hfq 日线库计算；只有展示用「现价」取 dashboard/quotes.json
（不复权）。因此无需拉取任何新数据，纯本地计算。

产物 dashboard/screener.json（schema screener.v1），字段（紧凑键，控制体积）：
  c  代码(sh.600000)   n  名称        i  行业
  p  现价(不复权)      pc 日涨跌幅%   t  换手率%      am 成交额(亿)
  vr 量比(当日成交额/前5日均额)
  d5/d10/d20/d60    收盘相对日MA5/10/20/60 的偏离 %
  x5/x10/x20/x60    今日是否上穿对应均线(0/1)
  mb 日线多头排列 ma5>ma10>ma20>ma60 (0/1)
  ud 连涨天数          h20 收创20日新高(0/1)
  w60 收盘相对60周线偏离 %   wx 今日上穿60周线(0/1，周线按日K聚合，当周含今日)

用法：
  python -m dashboard.screener          # 生成/更新 dashboard/screener.json
  python -m dashboard.build --daily     # 已并入日常链路
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "daily"
OUT = ROOT / "dashboard" / "screener.json"
CST = timezone(timedelta(hours=8))

_MIN_DAILY = 61          # 日线至少 61 根（够算 MA60 及其前值）
_MIN_WEEKLY = 61         # 周线至少 61 根（够算 60 周线及其前值）


def _r1(x) -> float:
    return round(float(x), 1) if x == x else 0.0


def _r2(x) -> float:
    return round(float(x), 2) if x == x else 0.0


def _calc_one(args):
    code, name, ind = args
    f = DATA / f"{code.replace('.', '_')}.csv.gz"
    if not f.exists():
        return None
    try:
        df = pd.read_csv(f)
    except Exception:
        return None
    if len(df) < _MIN_DAILY:
        return None
    c = df["close"].to_numpy()
    amt = df["amount"].to_numpy()
    turn = df["turn"].to_numpy()

    row = {"c": code, "n": name}
    if ind:
        row["i"] = ind

    # ---- 日线均线偏离与上穿（hfq 比例，复权无关）----
    mas = {}
    for n in (5, 10, 20, 60):
        ma_now = c[-n:].mean()
        ma_prev = c[-n - 1:-1].mean()
        mas[n] = ma_now
        row[f"d{n}"] = _r2((c[-1] / ma_now - 1) * 100)
        row[f"x{n}"] = 1 if (c[-2] <= ma_prev and c[-1] > ma_now) else 0
    row["mb"] = 1 if (mas[5] > mas[10] > mas[20] > mas[60]) else 0

    # ---- 连涨 / 20日新高 ----
    ud = 0
    for k in range(len(c) - 1, 0, -1):
        if c[k] > c[k - 1]:
            ud += 1
        else:
            break
    row["ud"] = ud
    row["h20"] = 1 if c[-1] >= c[-20:].max() else 0

    # ---- 流动性 ----
    prev5 = amt[-6:-1].mean()
    row["vr"] = _r2(amt[-1] / prev5) if prev5 > 0 else 0.0
    row["t"] = _r2(turn[-1])
    row["am"] = _r2(amt[-1] / 1e8)
    row["pc"] = _r2((c[-1] / c[-2] - 1) * 100)

    # ---- 周线（按日K聚合，当周含今日）----
    dates = pd.to_datetime(df["date"])
    wk = pd.Series(c, index=dates).resample("W-FRI").last().dropna()
    if len(wk) >= _MIN_WEEKLY:
        w = wk.to_numpy()
        ma_now = w[-60:].mean()
        ma_prev = w[-61:-1].mean()
        row["w60"] = _r2((w[-1] / ma_now - 1) * 100)
        row["wx"] = 1 if (w[-2] <= ma_prev and w[-1] > ma_now) else 0
    # 周线不足的股票：w60/wx 缺省，前端按「无周线数据」处理（不参与周线条件）
    return row


def _resolve_universe(base: Path | None = None) -> tuple[list[tuple[str, str, str]], str]:
    """返回 ([(code, name, industry)], 数据源说明)。

    ``data/`` 不入库（见 .gitignore），云端 runner 必然没有它。
    因此按「本地 data/ 优先，云端退到 store/ 活数据」解析：
      - 在市清单：data/universe.csv.gz（含 type/status）→ store/universe_live.csv.gz
      - 行业：data/industry.csv.gz → store/industry_sw.csv.gz
    """
    root = base or ROOT
    uni_path = root / "data" / "universe.csv.gz"
    if uni_path.exists():
        uni = pd.read_csv(uni_path, dtype=str)
        uni = uni[(uni["type"] == "1") & (uni["status"] == "1")]
        src = "data/universe.csv.gz"
    else:
        uni_path = root / "store" / "universe_live.csv.gz"
        uni = pd.read_csv(uni_path, dtype=str)
        src = "store/universe_live.csv.gz"
    ind_path = root / "data" / "industry.csv.gz"
    if not ind_path.exists():
        ind_path = root / "store" / "industry_sw.csv.gz"
    ind_map: dict[str, str] = {}
    try:
        ind_df = pd.read_csv(ind_path, dtype=str)
        col = "industry" if "industry" in ind_df.columns else ind_df.columns[1]
        ind_map = dict(zip(ind_df["code"], ind_df[col].fillna("")))
        src += f" + {ind_path.name}"
    except Exception as exc:  # noqa: BLE001
        print(f"      screener 行业文件不可用（{type(exc).__name__}），行业留空")
    jobs = [(r.code, getattr(r, "code_name", "") or "", ind_map.get(r.code, ""))
            for r in uni.itertuples()]
    return jobs, src


def build_screener() -> dict:
    t0 = time.time()
    # 云端没有 data/daily 日线库时**不能**写盘：否则会用空表覆盖已有的
    # 有效 screener.json（线上选股面板会被清空）。
    if not DATA.exists():
        keep = OUT.exists()
        print("      screener 跳过：无 data/daily 日线库（云端不入库）"
              + ("，保留现有 screener.json 不覆盖" if keep else ""))
        return {}
    jobs, uni_src = _resolve_universe()

    rows = []
    with Pool(16) as pool:
        for k, r in enumerate(pool.imap_unordered(_calc_one, jobs), 1):
            if r is not None:
                rows.append(r)
            if k % 1000 == 0:
                print(f"  screener {k}/{len(jobs)}  {time.time()-t0:.0f}s", flush=True)

    # ---- 现价（不复权）来自 quotes.json；缺失的剔除（停牌/无行情）----
    qj = json.loads((ROOT / "dashboard" / "quotes.json").read_text(encoding="utf-8"))
    items = qj.get("items") or {}
    priced = []
    for r in rows:
        q = items.get(r["c"])
        if not q or not q[0]:
            continue
        r["p"] = round(float(q[0]), 2)
        priced.append(r)

    priced.sort(key=lambda r: r["c"])
    if not priced:
        print("      screener 跳过：本次一只都没算出来，保留现有 screener.json 不覆盖")
        return {}
    out = {
        "schema": "screener.v1",
        "day": qj.get("date", ""),
        "updated": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
        "universe": len(jobs),
        "skipped": len(jobs) - len(priced),
        "counts": {"total": len(priced)},
        "rows": priced,
    }
    text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    OUT.write_text(text, encoding="utf-8")
    print(f"[screener] {len(priced)}/{len(jobs)} 只 -> screener.json "
          f"({len(text.encode())/1024:.0f} KB, {time.time()-t0:.0f}s, 源 {uni_src})")
    return out


if __name__ == "__main__":
    build_screener()
