"""云端精简数据仓（GitHub Actions 部署用）。

全量日线（~440MB）不适合放仓库；云端只维护三件小事：
  store/weekly_closes.csv.gz  各股最近 ~130 个已完成周的周收盘（后复权）
  store/last_close.csv        各股最近交易日收盘（后复权）+ 日期（复权因子用）
  store/avg_amt20.csv         近 20 日平均成交额（元，不复权）
  store/universe_live.csv.gz  在市标的清单（云端无 data/ 也能知道全市场范围）

快照判定与本地完全共用 ma60_base.stock_from_weekly()。

全市场不遗漏（v1.3.1）：
  单一数据源不可靠 —— baostock 触发黑名单时**返回空值而非报错**，且它对
  部分老股本就无数据。为此引入腾讯行情作兜底源（见 datasource.TencentSource）：
    * 仓内已有标的抓取失败 → 本轮用腾讯补
    * 在市但仓内没有的标的 → 腾讯补齐（fill_universe_gaps）
    * 每只标的的数据来源记入 store/state/code_sources.json，避免口径来回切换
"""
from __future__ import annotations

import gzip
import json
import threading
import time
from datetime import date
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

import datasource  # noqa: F401  确保 patch_baostock_io 生效

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "store"
STATE = STORE / "state"
F_WEEKLY = STORE / "weekly_closes.csv.gz"
F_LAST = STORE / "last_close.csv"
F_AMT = STORE / "avg_amt20.csv"
F_UNIV = STORE / "universe_live.csv.gz"
# 日线滚动仓（v1.9.0）：形态板块（src/pattern）要在云端判定，而形态需要
# 310~355 个交易日的日线序列（250 日高点 + 60 日 + 最长 45 日回踩）。
# ⚠️ 为什么不放**单个**滚动文件：32MB 的单文件每天重写会让 gzip 字节全变，
#    git 无法 delta，仓库每天涨 32MB（一年 11GB）。改成**按交易日分片**后，
#    每天只新增 1 个小分片（约 50KB）、历史分片永不变更 → 逐日增量仅几十 KB。
F_DAILY_DIR = STORE / "daily"
KEEP_DAILY = 400          # 保留最近 N 个交易日分片（判定需 310~355，留余量）

KEEP_WEEKS = 130          # 仓内保留周数（判定需 61+13，留足余量）
FETCH_BUFFER_DAYS = 50    # 增量抓取回看天数（覆盖 20 日均额）
TX_WEEKS = 200            # 腾讯兜底抓取周数（足够覆盖 KEEP_WEEKS + 60 周均线）
TX_WORKERS = 6            # 腾讯兜底并发（实测 10 并发无压力，留余量）
OUTAGE_RATIO = 0.20       # baostock 失败率超过该阈值即判定为源故障，不做兜底翻转
OUTAGE_MIN = 20           # 且失败绝对数达到该值（小批量失败属个别缺口，不算故障）

_SRC = None
_TL = threading.local()


# ---------------------------------------------------------------- 读仓
def load_weekly() -> pd.DataFrame:
    if F_WEEKLY.exists():
        return pd.read_csv(F_WEEKLY, parse_dates=["week_end"])
    return pd.DataFrame(columns=["code", "week_end", "close"])


def save_weekly(df: pd.DataFrame):
    STORE.mkdir(parents=True, exist_ok=True)
    df.to_csv(F_WEEKLY, index=False, compression="gzip")


def load_last_close() -> pd.DataFrame:
    if F_LAST.exists():
        return pd.read_csv(F_LAST, parse_dates=["last_date"]).set_index("code")
    return pd.DataFrame(columns=["hfq_last_close", "last_date"])


def load_amt20() -> pd.Series:
    if F_AMT.exists():
        return pd.read_csv(F_AMT, index_col="code")["avg_amt20"]
    return pd.Series(dtype=float)


def export_universe() -> int:
    """把本地在市标的清单导出到 store —— 云端没有 data/（已 gitignore），
    靠这份快照才能知道"全市场到底有多少只、还差哪几只"。"""
    p = ROOT / "data" / "universe.csv.gz"
    if not p.exists():
        return 0
    u = pd.read_csv(p, dtype=str)
    live = u[u["status"].astype(str) == "1"][["code", "code_name", "ipoDate"]]
    STORE.mkdir(parents=True, exist_ok=True)
    live.to_csv(F_UNIV, index=False, compression="gzip")
    return len(live)


def live_universe() -> pd.DataFrame:
    """在市标的清单。本地优先用 data/universe，云端回退到仓内快照。"""
    p = ROOT / "data" / "universe.csv.gz"
    if p.exists():
        u = pd.read_csv(p, dtype=str)
        return u[u["status"].astype(str) == "1"][["code", "code_name", "ipoDate"]] \
                 .reset_index(drop=True)
    if F_UNIV.exists():
        return pd.read_csv(F_UNIV, dtype=str)
    return pd.DataFrame(columns=["code", "code_name", "ipoDate"])


# ---------------------------------------------------------------- 日线滚动仓（分片）
def daily_shards(keep: int = KEEP_DAILY) -> list[Path]:
    """按日期升序返回最近 keep 个交易日分片。"""
    if not F_DAILY_DIR.exists():
        return []
    return sorted(F_DAILY_DIR.glob("*.csv.gz"))[-keep:]


def load_daily_recent(keep: int = KEEP_DAILY) -> pd.DataFrame:
    """拼接最近 keep 个交易日的日线，列为 code/date/close/amount。

    缺失或损坏的分片跳过（不抛错）——宁可少一天，也不让整条链路挂掉。
    """
    empty = pd.DataFrame(columns=["code", "date", "close", "amount"])
    parts = []
    for f in daily_shards(keep):
        try:
            parts.append(pd.read_csv(f))
        except Exception:  # noqa: BLE001
            continue
    if not parts:
        return empty
    df = pd.concat(parts, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    return df


def _write_shard(d: pd.Timestamp, rows: pd.DataFrame) -> None:
    """写单个交易日分片。

    已存在则按 code 合并（新值覆盖旧值）——补跑时不会丢已抓到的标的；
    按 code 排序后再落盘，保证同一份数据产生**稳定的字节**（git diff 干净）。
    """
    F_DAILY_DIR.mkdir(parents=True, exist_ok=True)
    p = F_DAILY_DIR / f"{d:%Y-%m-%d}.csv.gz"
    if p.exists():
        try:
            rows = pd.concat([pd.read_csv(p), rows], ignore_index=True)
        except Exception:  # noqa: BLE001
            pass
    out = (rows.drop_duplicates("code", keep="last")
               .sort_values("code")[["code", "date", "close", "amount"]])
    out.to_csv(p, index=False, compression="gzip")


def append_daily_shards(fetched: dict, keep: int = KEEP_DAILY) -> int:
    """把本轮抓到的日线按交易日写入分片，并清理窗口外的旧分片。

    fetched = ``{code: DataFrame[date, close, amount]}`` —— 直接复用
    ``update_from_baostock`` 已经抓下来的数据，**零额外请求**。
    返回写入的分片数。
    """
    rows = []
    for c, df in (fetched or {}).items():
        if df is None or len(df) == 0:
            continue
        t = df[["date", "close", "amount"]].copy()
        t["code"] = c
        rows.append(t)
    if not rows:
        return 0
    allrows = pd.concat(rows, ignore_index=True)
    allrows["close"] = pd.to_numeric(allrows["close"], errors="coerce").round(4)
    allrows["amount"] = pd.to_numeric(allrows["amount"], errors="coerce").round(0)
    n = 0
    for d, g in allrows.groupby("date"):
        _write_shard(pd.Timestamp(d), g)
        n += 1
    # 滚动清理：只留最近 keep 个分片（git 对象仍在库内，但引用消失后
    # 后续 clone 不再拉取，仓库体积不会线性膨胀）
    stale = sorted(F_DAILY_DIR.glob("*.csv.gz"))[:-keep]
    for f in stale:
        try:
            f.unlink()
        except OSError:
            pass
    return n


def seed_daily_shards_from_local() -> int:
    """用本地 data/daily 播种云端日线分片仓（一次性操作）。

    只保留 date/close/amount 三列与最近 KEEP_DAILY 个交易日。
    部分精简源缺 amount → 自适应补 NaN（形态的量比会因此失效，仅影响该标的）。
    """
    import glob

    files = sorted(glob.glob(str(ROOT / "data" / "daily" / "*.csv.gz")))
    if not files:
        raise SystemExit("本地没有 data/daily，无法播种日线分片仓")

    uni = None
    up = ROOT / "data" / "universe.csv.gz"
    if up.exists():
        uni = pd.read_csv(up, dtype=str).set_index("code")

    parts, skipped = [], 0
    for i, f in enumerate(files, 1):
        code = Path(f).name.replace(".csv.gz", "").replace("_", ".", 1)
        if uni is not None and code in uni.index and str(uni.at[code, "status"]) != "1":
            continue
        try:
            df = pd.read_csv(f, parse_dates=["date"], usecols=["date", "close", "amount"])
        except ValueError:                     # 精简源没有 amount
            try:
                df = pd.read_csv(f, parse_dates=["date"], usecols=["date", "close"])
                df["amount"] = np.nan
            except Exception:                  # noqa: BLE001
                skipped += 1
                continue
        except Exception:                      # noqa: BLE001
            skipped += 1
            continue
        df["code"] = code
        parts.append(df[["code", "date", "close", "amount"]])
        if i % 1500 == 0:
            print(f"  读入 {i}/{len(files)}", flush=True)

    if not parts:
        raise SystemExit("本地日线全部读取失败")

    allrows = pd.concat(parts, ignore_index=True)
    days = np.sort(allrows["date"].unique())[-KEEP_DAILY:]
    allrows = allrows[allrows["date"] >= days[0]].copy()
    allrows["close"] = pd.to_numeric(allrows["close"], errors="coerce").round(4)
    allrows["amount"] = pd.to_numeric(allrows["amount"], errors="coerce").round(0)

    F_DAILY_DIR.mkdir(parents=True, exist_ok=True)
    for f in F_DAILY_DIR.glob("*.csv.gz"):      # 清掉旧分片，避免残留混入
        f.unlink()
    n = 0
    for d, g in allrows.groupby("date"):
        _write_shard(pd.Timestamp(d), g)
        n += 1
    size = sum(f.stat().st_size for f in F_DAILY_DIR.glob("*.csv.gz"))
    print(f"[store] 日线分片仓播种完成：{n} 个交易日 / {allrows['code'].nunique()} 只 / "
          f"{len(allrows):,} 行 / {size/1e6:.1f} MB"
          + (f"（跳过 {skipped} 只无数据）" if skipped else ""))
    return n


# ---------------------------------------------------------------- 播种（本地）
def seed_from_local(progress: bool = True):
    """用本地 data/daily 全量数据播种 store（一次性操作）。"""
    import glob

    from .ma60_base import cutoff_monday, stock_from_weekly

    uni = None
    if (ROOT / "data" / "universe.csv.gz").exists():
        uni = pd.read_csv(ROOT / "data" / "universe.csv.gz", dtype=str).set_index("code")

    weekly_rows, last_rows, amt_rows = [], [], []
    files = sorted(glob.glob(str(ROOT / "data" / "daily" / "*.csv.gz")))
    cutoff = cutoff_monday()
    for i, f in enumerate(files, 1):
        code = Path(f).name.replace(".csv.gz", "").replace("_", ".", 1)
        if uni is not None and code in uni.index and str(uni.at[code, "status"]) != "1":
            continue
        try:
            df = pd.read_csv(f, parse_dates=["date"])[["date", "close", "amount"]]
        except Exception:  # noqa: BLE001
            continue
        s = df.set_index("date")["close"]
        r = stock_from_weekly(code, s, cutoff)
        if r is None:
            continue
        w = r.pop("weekly_closes").tail(KEEP_WEEKS)
        weekly_rows.append(pd.DataFrame({
            "code": code, "week_end": w.index.to_timestamp(), "close": w.values}))
        last = df.sort_values("date").iloc[-1]
        last_rows.append({"code": code, "hfq_last_close": float(last["close"]),
                          "last_date": last["date"]})
        d20 = df.sort_values("date").tail(20)
        amt_rows.append({"code": code, "avg_amt20": float(d20["amount"].mean())})
        if progress and i % 1000 == 0:
            print(f"  播种 {i}/{len(files)}", flush=True)

    save_weekly(pd.concat(weekly_rows, ignore_index=True))
    pd.DataFrame(last_rows).to_csv(F_LAST, index=False)
    pd.DataFrame(amt_rows).to_csv(F_AMT, index=False)
    print(f"[store] 播种完成：周线 {sum(len(x) for x in weekly_rows):,} 行，"
          f"标的 {len(last_rows)} 只")
    n_uni = export_universe()
    print(f"[store] 在市标的清单已导出：{n_uni} 只")
    # 播种也算一次全量抓取（本地读文件，成功率应为 100%）
    write_update_stats(
        ts=pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        fetched=len(last_rows), todo=len(last_rows), total=len(last_rows),
        coverage=len(last_rows), failed=0, timed_out=False,
        in_store=len(last_rows), source="local-seed")


# ---------------------------------------------------------------- 云端增量
def _read_state(name: str) -> dict:
    p = STATE / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def market_total() -> int:
    """在市标的数。本地有 universe 时取真实值，云端回退到仓内快照，再不行沿用已登记值。"""
    n = len(live_universe())
    if n:
        return n
    return int(_read_state("update_stats.json").get("market_total") or 0)


def write_update_stats(**kw):
    """覆盖率留痕：看板据此显示抓取情况，让"漏抓"可见。"""
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        data = _read_state("update_stats.json")
        kw.setdefault("market_total", market_total())
        kw.setdefault("in_store", len(load_last_close()))
        kw.setdefault("refresh", _state_summary())
        data.update(kw)
        (STATE / "update_stats.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[store] 覆盖率留痕写入失败：{e}")


def _state_summary() -> dict:
    """仓内覆盖面明细：全市场 / 已纳入 / 数据不足（次新股）。"""
    have = set(load_last_close().index)
    uni = live_universe()
    live = set(uni["code"]) if len(uni) else set(have)
    short = sorted(live - have)
    return {
        "market_live": len(live),
        "in_store": len(have),
        "short_history": len(short),
        "short_list": short[:200],
        "full_coverage": len(live) == len(have) or len(short) == 0,
    }


def _init_worker():
    global _SRC
    datasource.patch_baostock_io(45.0)
    from datasource import BaostockSource
    _SRC = BaostockSource(segment="single")
    _SRC._ensure_login()


def _latest_expected_trading_day() -> pd.Timestamp:
    """预期的最新交易日（保守近似，忽略节假日）。

    仅用于"已最新则跳过"的判断，且必须落在"数据大概率已发布"的一侧：
    判断偏晚只会多抓一次，判断偏早则可能漏掉新数据。16:00 前（含盘中）
    回退到上一个工作日。
    """
    now = pd.Timestamp.now()
    t = now.normalize()
    if t.weekday() < 5 and now.hour >= 16:
        return t
    d = t - pd.Timedelta(days=1)
    while d.weekday() >= 5:
        d -= pd.Timedelta(days=1)
    return d


def _fetch_one(job):
    """抓取单只；空结果也重试（行情源限流/会话失效时常以空值返回而非抛错）。

    注意入参为单个元组 (code, start) —— `Pool.imap_unordered` **不会解包**
    参数元组，写成长签名会导致 `TypeError: missing 1 required positional argument`。
    """
    code, start = job
    global _SRC
    for attempt in range(3):
        try:
            df = _SRC.fetch_daily(code, start, None)
            if not df.empty:
                return code, df[["date", "close", "amount"]]
        except Exception:  # noqa: BLE001
            pass
        try:                       # 会话失效重建
            _SRC.close()
        except Exception:  # noqa: BLE001
            pass
        _init_worker()
        time.sleep(0.3 * (attempt + 1))
    return code, None


# ------------------------------------------------------------------ 腾讯兜底
def _tx_src():
    """每线程一个腾讯源实例（复用自己的 Session，避免连接风暴）。"""
    s = getattr(_TL, "src", None)
    if s is None:
        from datasource import TencentSource
        s = TencentSource(fq="hfq")
        _TL.src = s
    return s


def _weekly_index_to_monday(idx) -> pd.DatetimeIndex:
    """把任意标注日（周内任一交易日）归一到所在周的周一，与 baostock 分支口径一致。"""
    return pd.DatetimeIndex(idx).to_period("W").to_timestamp()


def _tencent_one(code: str, weeks: int = TX_WEEKS):
    """抓单只：周线 hfq（主用）+ 日线不复权（估 20 日均额）。

    返回 (code, dict|None)；dict = {weekly, last_close, last_date, avg_amt20}
    """
    src = _tx_src()
    try:
        w = src.fetch_weekly(code, weeks)
        if w.empty:
            return code, None
        amt = float("nan")
        # 成交额必须用**不复权**价估算（后复权价被拉高 N 倍，会严重虚增成交额）
        d = src.fetch_daily(code, None, None, "")
        if len(d):
            dd = d.tail(20)
            vwap = (dd["open"] + dd["close"] + dd["high"] + dd["low"]) / 4.0
            # 腾讯 K 线成交量单位为「手」；成交额按 (O+C+H+L)/4 近似 VWAP 估算
            amt = float((dd["volume"] * 100.0 * vwap).mean())
        last = w.iloc[-1]
        return code, {
            "weekly": w,
            "last_close": float(last["close"]),
            "last_date": pd.Timestamp(last["date"]).normalize(),
            "avg_amt20": amt,
        }
    except Exception:  # noqa: BLE001
        return code, None


def tencent_batch(codes, weeks: int = TX_WEEKS, max_workers: int = TX_WORKERS,
                  label: str = "") -> dict:
    """腾讯兜底批抓。返回 {code: {weekly, last_close, last_date, avg_amt20}}"""
    from concurrent.futures import ThreadPoolExecutor

    codes = list(codes)
    if not codes:
        return {}
    out: dict = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers) as ex:
        for i, (code, v) in enumerate(ex.map(lambda c: _tencent_one(c, weeks), codes), 1):
            if v is not None:
                out[code] = v
            if i % 500 == 0 or i == len(codes):
                print(f"  [腾讯兜底{label}] {i}/{len(codes)}  成功 {len(out)}  "
                      f"{time.time()-t0:.0f}s", flush=True)
    return out


def _persist_fills(tx_got: dict, cutoff: pd.Timestamp, merge_weekly: bool = True):
    """把腾讯抓到的数据整理成三张仓表的待写行（与仓内旧数据合并，绝不覆盖）。

    返回 (周线行列表, last 行, amt 行, 处理的代码集合)
    """
    wk = load_weekly()
    wk_groups = {c: g for c, g in wk.groupby("code")} if len(wk) else {}
    new_weekly, last_rows, amt_rows, done = [], [], [], set()

    for code, v in tx_got.items():
        w = v["weekly"]
        wk_end = _weekly_index_to_monday(w["date"])
        ww = pd.DataFrame({"code": code, "week_end": wk_end,
                           "close": w["close"].values})
        ww = ww[ww["week_end"] < cutoff]                     # 只收已完成周
        if ww.empty:
            continue
        old = wk_groups.get(code) if merge_weekly else None
        merged = pd.concat([old, ww] if old is not None else [ww]) \
            .drop_duplicates("week_end", keep="last").sort_values("week_end")
        new_weekly.append(merged.tail(KEEP_WEEKS))
        done.add(code)
        last_rows.append({"code": code, "hfq_last_close": v["last_close"],
                          "last_date": v["last_date"]})
        amt_rows.append({"code": code, "avg_amt20": v["avg_amt20"]})
    return new_weekly, last_rows, amt_rows, done


def _append_back(new_weekly, last_rows, amt_rows):
    """缺口补齐专用写回：仓内本无这些标的，**追加**到现有表（不是替换）。"""
    if new_weekly:
        wk = load_weekly()
        combined = pd.concat([wk] + new_weekly, ignore_index=True)
        save_weekly(combined.drop_duplicates(["code", "week_end"], keep="last"))
    if last_rows:
        keep = load_last_close().reset_index()
        pd.concat([keep, pd.DataFrame(last_rows)], ignore_index=True) \
            .drop_duplicates("code", keep="last").to_csv(F_LAST, index=False)
    if amt_rows:
        keep = load_amt20().rename("avg_amt20").reset_index()
        pd.concat([keep, pd.DataFrame(amt_rows)], ignore_index=True) \
            .drop_duplicates("code", keep="last").to_csv(F_AMT, index=False)


def _mark_sources(codes, source: str):
    d = _read_state("code_sources.json")
    for c in codes:
        d[c] = source
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "code_sources.json").write_text(
        json.dumps(d, ensure_ascii=False), encoding="utf-8")


def fill_universe_gaps(max_workers: int = TX_WORKERS) -> dict:
    """补齐「在市但仓内没有」的标的（全市场不遗漏的兜底闸门）。

    - 覆盖两种情形：baostock 无该股数据（老股缺口）、新上市标的刚够 61 周。
    - 只入仓**足以计算 60 周线**的标的；上市不足 61 周的仍会每日重试，
      一旦满 61 周自动进仓，不需要人工干预。
    """
    uni = live_universe()
    if uni.empty:
        print("[store] 无在市清单，跳过缺口补齐")
        return {"checked": 0, "added": 0}
    have = set(load_last_close().index)
    miss = [c for c in uni["code"].tolist() if c not in have]
    if not miss:
        print("[store] 全市场已 100% 纳入仓内，无需补齐")
        return {"checked": 0, "added": 0}

    print(f"[store] 缺口补齐：在市 {len(uni)} 只，仓内 {len(have)} 只，"
          f"待验 {len(miss)} 只", flush=True)
    got = tencent_batch(miss, label="缺口", max_workers=max_workers)

    from .ma60_base import cutoff_monday, stock_from_weekly
    cutoff = cutoff_monday()
    ok, short = {}, []
    for code, v in got.items():
        s = v["weekly"].set_index("date")["close"]
        if stock_from_weekly(code, s, cutoff) is None:
            short.append(code)
        else:
            ok[code] = v
    failed = [c for c in miss if c not in got]

    if ok:
        new_weekly, last_rows, amt_rows, done = _persist_fills(ok, cutoff, merge_weekly=False)
        # 缺口标的仓内本来就没有，直接追加
        _append_back(new_weekly, last_rows, amt_rows)
        _mark_sources(done, "tencent")
    else:
        done = set()

    print(f"[store] 缺口补齐完成：进仓 {len(done)} 只；"
          f"上市不足 61 周暂无法判定 {len(short)} 只；取数失败 {len(failed)} 只")
    if failed:
        print(f"       失败清单（前 20）：{failed[:20]}")
    return {"checked": len(miss), "added": len(done),
            "short": len(short), "failed": len(failed)}


def update_from_baostock(workers: int = 16, budget_sec: int = 6600,
                         tx_workers: int = TX_WORKERS) -> dict:
    """抓取最近交易日的 close/amount，增量更新三张仓表。

    安全约束（血泪教训，见 v1.3.1）：
      1) 与仓内旧数据**合并**，绝不整体覆盖——否则一次抓取不全就会把
         全市场覆盖面缩小成子集；
      2) 有时间预算，到点优雅停止并把已抓到的写回，避免被 job 超时强杀；
      3) 覆盖率写入 store/state/update_stats.json，让"漏抓"可见；
      4) **双源冗余**：baostock 抓不到（限流/黑名单/本身无数据）的标的
         自动转腾讯兜底，杜绝"静默空值"造成遗漏。
    """
    last = load_last_close()
    codes = last.index.tolist()
    if not codes:
        raise SystemExit("store 为空：请先在本地执行 seed_from_local() 并提交仓库")

    latest = last["last_date"].max().normalize()
    start = (latest - pd.Timedelta(days=FETCH_BUFFER_DAYS)).strftime("%Y-%m-%d")
    # 已有最新交易日的标的不必重复抓取（补跑/重跑时省下大量时间）
    todo = [c for c in codes
            if pd.Timestamp(last.at[c, "last_date"]).normalize() < latest]
    src_map = _read_state("code_sources.json")
    tx_native = [c for c in todo if src_map.get(c) == "tencent"]
    bs_todo = [c for c in todo if src_map.get(c) != "tencent"]
    print(f"[store] 待抓 {len(todo)}/{len(codes)} 只"
          f"（{len(codes)-len(todo)} 只已最新；其中腾讯源 {len(tx_native)} 只），"
          f"预算 {budget_sec}s", flush=True)

    fetched = {}
    failed = 0
    t0 = time.time()
    deadline = t0 + budget_sec
    timed_out = False
    if bs_todo:
        with Pool(workers, initializer=_init_worker) as pool:
            it = pool.imap_unordered(_fetch_one, [(c, start) for c in bs_todo], chunksize=20)
            for i, (code, df) in enumerate(it, 1):
                if df is not None and not df.empty:
                    fetched[code] = df
                else:
                    failed += 1
                if i % 500 == 0 or i == len(bs_todo):
                    print(f"  抓取 {i}/{len(bs_todo)}  成功 {len(fetched)} 失败 {failed}  "
                          f"{time.time()-t0:.0f}s", flush=True)
                if time.time() > deadline:
                    timed_out = True
                    print(f"[store][警告] 到达时间预算 {budget_sec}s，停止抓取；"
                          f"已成功 {len(fetched)} 只，其余沿用仓内旧数据", flush=True)
                    pool.terminate()
                    break
    print(f"[store] baostock 抓到 {len(fetched)}/{len(bs_todo)} 只  "
          f"用时 {time.time()-t0:.0f}s")

    # ---- 腾讯兜底：baostock 未抓到的 + 本源即为腾讯的 ----
    bs_failed = [c for c in bs_todo if c not in fetched]
    # 安全阀：baostock 大面积失败说明是**源故障**而非"个别标的缺数据"。
    # 此时若把整仓都翻转到腾讯，就会因复权口径不同源而产生接缝断崖，
    # 且恢复后还要再翻回来（二次断崖）。这种情况下宁可沿用旧数据（滞后一天无害）。
    ratio = len(bs_failed) / max(1, len(bs_todo))
    outage = len(bs_failed) >= OUTAGE_MIN and ratio > OUTAGE_RATIO
    if outage:
        print(f"[store][警告] baostock 失败 {len(bs_failed)}/{len(bs_todo)}"
              f"（{ratio:.0%}），判定为数据源故障 → 本轮不做兜底翻转，"
              f"这些标的沿用仓内旧数据", flush=True)
        tx_need = list(tx_native)
    else:
        tx_need = list(tx_native) + bs_failed

    tx_got = {}
    if tx_need:
        print(f"[store] 腾讯兜底 {len(tx_need)} 只"
              f"（含本源腾讯 {len(tx_native)} 只 / baostock 缺口 {len(tx_need)-len(tx_native)} 只）…",
              flush=True)
        tx_got = tencent_batch(tx_need, label="增量", max_workers=tx_workers)
        print(f"[store] 腾讯兜底成功 {len(tx_got)}/{len(tx_need)} 只  "
              f"用时 {time.time()-t0:.0f}s")

    cutoff = pd.Timestamp(date.today())
    cutoff = cutoff - pd.Timedelta(days=pd.Timestamp(date.today()).weekday())  # 本周一

    # --- 合并周线（预分组避免 O(n²) 过滤）---
    wk = load_weekly()
    wk_groups = {c: g for c, g in wk.groupby("code")} if len(wk) else {}
    amt_old = load_amt20()
    upd = set(fetched) | set(tx_got)
    new_weekly = []
    last_rows = []
    amt_rows = []
    for code, df in fetched.items():
        df = df.drop_duplicates("date").sort_values("date")
        s = df.set_index("date")["close"]
        wnew = s.groupby(s.index.to_period("W")).last()
        wnew.index = wnew.index.to_timestamp()
        wnew = wnew[wnew.index < cutoff]                     # 只收已完成周
        old = wk_groups.get(code)
        merged = pd.concat([old, pd.DataFrame(
            {"code": code, "week_end": wnew.index, "close": wnew.values})]
        ).drop_duplicates("week_end", keep="last").sort_values("week_end")
        merged = merged.tail(KEEP_WEEKS)
        new_weekly.append(merged)

        lc = df.iloc[-1]
        last_rows.append({"code": code, "hfq_last_close": float(lc["close"]),
                          "last_date": lc["date"]})
        amt_rows.append({"code": code, "avg_amt20": float(df["amount"].tail(20).mean())})

    # --- 腾讯兜底标的并入 ---
    # 关键：**整段替换**而非拼接。腾讯与 baostock 的复权口径不同源，
    # 若把腾讯的一段接在 baostock 的序列尾部，接缝处会出现价格断崖，
    # 会让未来 60 周的 MA60 一直是错的。所以兜底标的一律用腾讯重建整段。
    # （upd 已含 tx_got，旧行会从 keep_weekly 中排除，不会残留）
    if tx_got:
        tx_weekly, tx_last, tx_amt, tx_done = _persist_fills(tx_got, cutoff, merge_weekly=False)
        new_weekly += tx_weekly
        last_rows += tx_last
        amt_rows += tx_amt
        # 记录数据来源：原 baostock 标的若靠腾讯兜住，此后固定走腾讯，
        # 避免两源来回切换造成序列口径抖动
        _mark_sources(tx_got.keys(), "tencent")
        print(f"[store] 腾讯兜底写入 {len(tx_done)} 只（整段重建，不与 baostock 序列混接）")

    # 未抓到的标的沿用仓内旧行：绝不让一次抓取不全导致覆盖面缩水
    keep_weekly = [g for c, g in wk_groups.items() if c not in upd]
    if new_weekly or keep_weekly:
        save_weekly(pd.concat(new_weekly + keep_weekly, ignore_index=True))
    keep_last = last[~last.index.isin(upd)].reset_index()
    pd.concat([keep_last, pd.DataFrame(last_rows)], ignore_index=True).to_csv(F_LAST, index=False)
    keep_amt = amt_old[~amt_old.index.isin(upd)].rename("avg_amt20").reset_index()
    pd.concat([keep_amt, pd.DataFrame(amt_rows)], ignore_index=True).to_csv(F_AMT, index=False)

    # --- 日线分片仓：形态板块（src/pattern）云端判定所需 ---
    # 复用 fetched（本就是 date/close/amount 三列），不产生任何额外请求。
    # 腾讯兜底标的（tx_got）没有 hfq 日线 → 这些分片里缺它们，属已知降级。
    n_shard = append_daily_shards(fetched)
    if n_shard:
        print(f"[store] 日线分片仓写入 {n_shard} 个交易日（形态板块云端判定用）")

    coverage = len(keep_weekly) + len(new_weekly)
    unresolved = [c for c in codes if c not in upd]
    stats = {"fetched": len(fetched), "todo": len(todo), "total": len(codes),
             "coverage": coverage, "failed": failed, "timed_out": timed_out,
             "fallback": len(tx_got), "unresolved": len(unresolved)}
    if tx_got:
        print(f"[store] 其中 {len(tx_got)} 只由腾讯兜底补齐（baostock 未取到）")
    if unresolved:
        print(f"[store][警告] {len(unresolved)} 只本轮两源都没取到，"
              f"已沿用仓内旧数据（覆盖面未缩水）")
    # 缺口补齐：把「在市但仓内没有」的标的用腾讯补进来（保证全市场不遗漏）
    gap = fill_universe_gaps(max_workers=tx_workers)

    # 覆盖率留痕：看板"数据资产健康度"据此显示抓取情况，让漏抓可见
    write_update_stats(
        ts=pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        fetched=len(fetched), todo=len(todo), total=len(codes),
        coverage=coverage, failed=failed, timed_out=timed_out,
        fallback=len(tx_got), unresolved=len(unresolved),
        gap_filled=gap.get("added", 0), gap_short=gap.get("short", 0),
        gap_checked=gap.get("checked", 0))
    print(f"[store] 更新完成 {stats}  覆盖面 {coverage} 只")
    return stats


# ---------------------------------------------------------------- 快照（云端）
def build_snapshot_from_store() -> pd.DataFrame:
    from .ma60_base import _industry_map, cutoff_monday, stock_from_weekly, STALE_MAX_DAYS

    wk = load_weekly()
    last = load_last_close()
    amt = load_amt20()
    ind_map = _industry_map()
    cutoff = cutoff_monday()

    rows = []
    stale_codes = []
    for code, g in wk.groupby("code"):
        s = g.set_index("week_end")["close"]
        r = stock_from_weekly(code, s, cutoff)
        if r is None:
            continue
        r.pop("weekly_closes")
        if code in last.index:
            r["hfq_last_close"] = float(last.at[code, "hfq_last_close"])
            r["last_date"] = pd.Timestamp(last.at[code, "last_date"]).strftime("%Y-%m-%d")
        else:
            continue
        r["avg_amt20"] = float(amt.get(code, np.nan))
        r["industry"] = ind_map.get(code, "未知")
        # 断头序列防护（与本地 build_snapshot 同口径）：last_date 落后过久的
        # 在市标的，其 MA60 是几年前的旧值，dev 完全不可信——宁漏勿错。
        # 实测 2026-10-14 双汇发展（store 序列断头 3.5 年）被算出 +2% 假偏离。
        if (pd.Timestamp(date.today()) - pd.Timestamp(r["last_date"])).days > STALE_MAX_DAYS:
            stale_codes.append(code)
            continue
        rows.append(r)
    if stale_codes:
        print(f"[snapshot][警告] {len(stale_codes)} 只标的 last_date 落后超过 "
              f"{STALE_MAX_DAYS} 天，已排除出扫描：{stale_codes[:20]}"
              + (" ..." if len(stale_codes) > 20 else ""))
    return pd.DataFrame(rows).set_index("code")
