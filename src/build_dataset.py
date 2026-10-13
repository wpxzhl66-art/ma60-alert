"""批量建库：全A股后复权日线 + 基准指数 + 成分股清单。

设计要点：
1. 并行取数用**多进程**（baostock session 非线程安全，每进程各建 login）。
2. 每只股票落一个 CSV（gzip），已存在则跳过 → 支持断点续跑。
3. 每只股票落库前做**数据质量校验**（条数、正价格、日期连续性），
   不合格的记入失败清单，不静默丢弃。
4. 输出建库进度与总结报告，供后续分析直接消费。

用法：
    python src/build_dataset.py --scope hs300          # 沪深300（小样本验证）
    python src/build_dataset.py --scope all            # 全A股（含退市）
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "daily"
BENCH = DATA / "benchmark"

START = "2010-01-01"
ADJUSTFLAG = "1"

# 数据质量校验口径（v1.1.1 修订）
# ---------------------------------------------------------------
# 旧口径用固定 EXPECTED_MIN_ROWS=3500 判"条数不足"，这是**错误**的：
# 全市场 5562 只标的中有 2885 只（51.9%）是 2015 年后上市的，其可得
# 历史天然不足 3500 条，会被整批误判为取数失败而丢弃。
#
# 正确做法：按标的**上市日期**判断应有区间，并区分两类问题——
#   硬错误（丢弃并记录）：空数据、条数不足以形成 MA60、非正价格、重复日期
#   软告警（保留并记录）：首条数据晚于理论首日、仍在市标的末条数据偏旧
# 软告警只写入 warn 列，绝不丢弃数据，避免误杀。
MIN_ROWS = 300            # 60 周 ≈ 300 交易日，低于此无法算出 MA60
START_COVER_TOL_DAYS = 60 # 首条数据晚于理论首日超过此天数 → 软告警
END_COVER_TOL_DAYS = 30   # 在市标的末条数据早于今日超过此天数 → 软告警

#: 取数分段方式。'single' 一次请求取全区间（最快）；
#: 'auto' 按估算交易日分 2 段（更保守）。
SEGMENT = "single"

#: socket 超时（秒）。用于把"被限流导致的永久挂起"转化为可重试的异常。
TIMEOUT = 90.0

# ---------------------------------------------------------------------------
# worker 级会话复用
# ---------------------------------------------------------------------------
# baostock 的 session **非线程安全**，但同一进程内可安全复用。早期实现为
# 「每只股票新建一个 session 并 login/logout」，每只股票白白多付一次 TCP
# 握手（约占单只耗时的 10%~20%）。改为每个 worker 进程登录一次、全生命周期
# 复用，是纯收益的优化。
_WORKER_SRC = None


def _init_worker(adjustflag: str, segment: str, timeout: float = 90.0):
    global _WORKER_SRC
    from datasource import BaostockSource
    _WORKER_SRC = BaostockSource(adjustflag=adjustflag, segment=segment,
                                 timeout=timeout)
    _WORKER_SRC._ensure_login()


def _worker_source():
    global _WORKER_SRC
    if _WORKER_SRC is None:
        _init_worker(ADJUSTFLAG, SEGMENT, TIMEOUT)
    return _WORKER_SRC


def _reset_worker_source():
    global _WORKER_SRC
    if _WORKER_SRC is not None:
        try:
            _WORKER_SRC.close()
        except Exception:  # noqa: BLE001
            pass
    _WORKER_SRC = None


def probe_ready(timeout: float = 60.0) -> tuple[bool, str]:
    """探测 baostock 是否可用：发一次极小查询。

    被限流时的表现是**查询挂起而非报错**，因此必须依赖 socket 超时来判定；
    本函数返回 (是否可用, 说明)。绝不在探测失败时抛异常。
    """
    from datasource import BaostockSource
    src = BaostockSource(segment="single", timeout=timeout)
    try:
        src._ensure_login()
        import baostock as bs
        rs = bs.query_history_k_data_plus(
            "sh.600000", "date,close", start_date="2026-09-01",
            end_date="2026-10-13", frequency="d", adjustflag="1")
        n = 0
        # 必须调用 get_row_data()：baostock 的 next() 只判断"当前页是否还有
        # 数据"，游标 cur_row_num 仅在 get_row_data() 中推进。只调 next() 不取值
        # 会永久返回 True（无限空转）——早期误判为"服务端限流"的根因即在于此。
        while rs.next():
            rs.get_row_data()
            n += 1
        return (n > 0), f"{n} 条"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        try:
            src.close()
        except Exception:  # noqa: BLE001
            pass


def wait_until_ready(max_wait_min: float = 240.0, interval_sec: float = 180.0,
                     probe_timeout: float = 60.0) -> bool:
    """轮询等待 baostock 解除限流。"""
    t0 = time.time()
    attempt = 0
    while True:
        attempt += 1
        ok, info = probe_ready(probe_timeout)
        el = (time.time() - t0) / 60
        print(f"  [探测 {attempt}] {'可用 ✅' if ok else '不可用 ❌'}  {info}"
              f"  （已等待 {el:.0f} 分钟）", flush=True)
        if ok:
            return True
        if el >= max_wait_min:
            print(f"  等待已达 {max_wait_min:.0f} 分钟仍不可用，放弃本次自动启动。",
                  flush=True)
            return False
        time.sleep(interval_sec)


def _fetch_one(task):
    """子进程任务：取单只股票日线并落盘。

    task = (code, force, ipo_date, status)
      ipo_date: 上市日期（'YYYY-MM-DD' 或 None）
      status:   '1'=在市 / '0'=已退市
    """
    code, force, ipo_date, status = task
    out = RAW / f"{code.replace('.', '_')}.csv.gz"
    if out.exists() and not force:
        return (code, "skip", 0, "")
    RAW.mkdir(parents=True, exist_ok=True)

    src = _worker_source()
    last = ""
    for attempt in range(3):
        try:
            df = src.fetch_daily(code, START, None)
            last = ""
            break
        except Exception as exc:  # noqa: BLE001
            # 会话可能已失效（服务端断开）：重建会话与重试
            last = f"{type(exc).__name__}"
            time.sleep(1.5 * (attempt + 1))
            try:
                _reset_worker_source()
                src = _worker_source()
            except Exception as exc2:  # noqa: BLE001
                last = f"{type(exc2).__name__}"
    else:
        return (code, f"error:{last}", 0, "")

    # ---- 硬错误：丢弃 ----
    if df.empty:
        return (code, "empty", 0, "")
    if len(df) < MIN_ROWS:
        return (code, f"short:{len(df)}", len(df), "")
    if (df["close"] <= 0).any():
        return (code, "nonpositive", len(df), "")
    if df["date"].duplicated().any():
        return (code, "dup_date", len(df), "")

    # ---- 软告警：保留 ----
    warns = []
    exp_start = pd.Timestamp(START)
    if ipo_date and str(ipo_date) not in ("nan", "None", ""):
        exp_start = max(exp_start, pd.Timestamp(str(ipo_date)))
    first = df["date"].iloc[0]
    if first > exp_start + pd.Timedelta(days=START_COVER_TOL_DAYS):
        warns.append(f"start_late:{first.date()}>{(exp_start.date())}")
    if str(status) == "1":
        today = pd.Timestamp(date.today().isoformat())
        last = df["date"].iloc[-1]
        if last < today - pd.Timedelta(days=END_COVER_TOL_DAYS):
            warns.append(f"end_stale:{last.date()}")

    df.to_csv(out, index=False, compression="gzip")
    return (code, "ok", len(df), ";".join(warns))


def build_stock_daily(codes, meta: dict | None = None, workers: int = 8,
                      force: bool = False):
    from multiprocessing import Pool

    meta = meta or {}
    RAW.mkdir(parents=True, exist_ok=True)
    tasks = [(c, force, meta.get(c, (None, "1"))[0], meta.get(c, (None, "1"))[1])
             for c in codes]
    t0 = time.time()
    results = []
    with Pool(workers, initializer=_init_worker,
              initargs=(ADJUSTFLAG, SEGMENT, TIMEOUT)) as pool:
        for i, r in enumerate(pool.imap_unordered(_fetch_one, tasks, chunksize=4), 1):
            results.append(r)
            if i % 25 == 0 or i == len(tasks):
                el = time.time() - t0
                eta = el / i * (len(tasks) - i)
                print(f"  {i}/{len(tasks)}  用时 {el/60:.1f}min  "
                      f"平均 {el/i:.1f}s/只  预计剩余 {eta/60:.1f}min", flush=True)

    dfr = pd.DataFrame(results, columns=["code", "status", "rows", "warn"])
    ok = dfr[dfr["status"].isin(["ok", "skip"])]
    bad = dfr[~dfr["status"].isin(["ok", "skip"])]
    n_warn = int((dfr["warn"].fillna("") != "").sum())
    print(f"\n成功 {len(ok)} / 失败 {len(bad)}  软告警 {n_warn}  总耗时 {(time.time()-t0)/60:.1f}min")
    if len(bad):
        print("失败明细（前20）：")
        print(bad.head(20).to_string(index=False))
    bad.to_csv(DATA / "build_failures.csv", index=False, encoding="utf-8-sig")
    if n_warn:
        dfr[dfr["warn"].fillna("") != ""].to_csv(
            DATA / "build_warnings.csv", index=False, encoding="utf-8-sig")
    return dfr


def build_benchmarks():
    BENCH.mkdir(parents=True, exist_ok=True)
    from datasource import BaostockSource
    src = BaostockSource(segment="auto")
    with src:
        print("取沪深300指数日线...")
        idx = src.index_daily("sh.000300", START, None)
        idx.to_csv(BENCH / "hs300.csv.gz", index=False, compression="gzip")
        print(f"  沪深300 {len(idx)} 条  {idx['date'].iloc[0].date()} ~ {idx['date'].iloc[-1].date()}")

        print("取全市场标的清单（含退市）...")
        uni = src.list_universe(include_delisted=True)
        uni.to_csv(DATA / "universe.csv.gz", index=False, compression="gzip")
        n_del = int((uni["status"] == "0").sum()) if "status" in uni else 0
        print(f"  标的 {len(uni)} 只（其中退市 {n_del} 只）")

        print("取行业分类...")
        try:
            ind = src.fetch_industry()
            ind.to_csv(DATA / "industry.csv.gz", index=False, compression="gzip")
            print(f"  行业分类 {len(ind)} 条")
        except Exception as exc:  # noqa: BLE001
            print(f"  行业分类失败: {exc}")
    return idx, uni


def get_scope(scope: str, uni: pd.DataFrame | None):
    if scope == "hs300":
        from datasource import BaostockSource
        src = BaostockSource()
        with src:
            cons = src.list_constituents("000300")
        codes = cons["code"].tolist()
        print(f"沪深300 成分股 {len(codes)} 只")
        return codes
    if scope == "all" and uni is not None:
        codes = uni["code"].tolist()
        print(f"全市场（含退市）{len(codes)} 只")
        return codes
    raise ValueError(scope)


def build_meta(uni: pd.DataFrame | None, codes) -> dict:
    """构造 code → (ipo_date, status) 映射，供质量校验判断应有历史区间。

    取不到上市日期的标的回退为 (None, '1')，即按全区间校验。
    """
    if uni is None or "code" not in uni.columns:
        return {}
    sub = uni.set_index("code")
    meta = {}
    for c in codes:
        if c in sub.index:
            row = sub.loc[c]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            ipo = row.get("ipoDate")
            st = row.get("status", "1")
            meta[c] = (None if pd.isna(ipo) else str(ipo), str(st))
        else:
            meta[c] = (None, "1")
    return meta


def main():
    global SEGMENT
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["hs300", "all"], default="hs300")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--segment", choices=["single", "auto"], default=SEGMENT)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--skip-bench", action="store_true")
    ap.add_argument("--wait-ready", action="store_true",
                    help="开跑前先轮询等待数据源解除限流")
    ap.add_argument("--wait-minutes", type=float, default=240.0,
                    help="--wait-ready 的最长等待分钟数")
    args = ap.parse_args()
    SEGMENT = args.segment

    print("=" * 62)
    print(f"批量建库  范围={args.scope}  并发={args.workers}  起始={START}  分段={SEGMENT}")
    print("=" * 62)

    if args.wait_ready:
        print("等待数据源就绪（被限流时 baostock 会挂起查询而非报错）...")
        if not wait_until_ready(max_wait_min=args.wait_minutes):
            return 2

    uni = None
    if not args.skip_bench:
        _, uni = build_benchmarks()
    else:
        p = DATA / "universe.csv.gz"
        if p.exists():
            uni = pd.read_csv(p, dtype=str)

    codes = get_scope(args.scope, uni)
    meta = build_meta(uni, codes)
    build_stock_daily(codes, meta=meta, workers=args.workers, force=args.force)
    print("\n建库完成。数据目录:", RAW)


if __name__ == "__main__":
    main()
