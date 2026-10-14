"""国内期货「持仓量加权指数」的构建与增量更新。

为什么要自建
------------
文华／东财的「XX加权」指数没有可用的免费接口（东财 K 线在本机被代理层
稳定拦截，重试 6 次全失败），而新浪只提供 **主力连续**（``RB0``）——
换月当天价格会跳空，这个断崖会让 60 周均线产生虚假的「触及」。
因此按行业标准口径自建：

    加权价 = Σ(合约收盘 × 合约持仓量) / Σ(持仓量)

数据源（新浪财经，实测稳定）
---------------------------
1) 具体月份合约日线
   ``stock2.finance.sina.com.cn/futures/api/jsonp.php/.../InnerFuturesNewService.getDailyKLine?symbol=RB2610``
   返回 ``[{d,o,h,l,c,v,p,s}]``，其中 ``v`` = 成交量(手)、``p`` = 持仓量(手)。
2) 实时行情（批量，一次可查数十个代码）
   ``hq.sinajs.cn/list=nf_RB2610,nf_RB2611,...``
   - 商品期货（44 项）：``[8]`` = 最新价、``[13]`` = 持仓量
   - 中金所（50 项，首字段即数字）：``[3]`` = 最新价、``[6]`` = 持仓量

存储：``store/fut/``
--------------------
``varieties.csv``            品种清单（覆盖率口径用）
``daily_oi.csv.gz``          加权日线 OHLC（原始数据，增量与判定都基于它）
``weekly_oi.csv.gz``         加权周线 OHLC（由日线聚合，供判定/图表）
``last_close.csv``           各品种最新加权价与日期
``state/update_stats.json``  覆盖率留痕
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from . import varieties as V

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "store" / "fut"
STATE = STORE / "state"
F_DAILY = STORE / "daily_oi.csv.gz"
F_WEEKLY = STORE / "weekly_oi.csv.gz"
F_LAST = STORE / "last_close.csv"
F_VAR = STORE / "varieties.csv"

_SINA_DAILY = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
               "var%20t=/InnerFuturesNewService.getDailyKLine?symbol={}")
_SINA_RT = "https://hq.sinajs.cn/list={}"
_HEAD = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
         "Referer": "https://finance.sina.com.cn"}

HIST_MONTHS = 14          # 全量建库：向前回溯的合约月份数（重建历史加权用）
LIVE_MONTHS = 30          # 当前在市合约的月份跨度（覆盖全部挂牌合约，不遗漏）
REBUILD_WORKERS = 8
MIN_INTERVAL = 0.05       # 全局最小请求间隔（秒），新浪对突发请求不友好


# --------------------------------------------------------------------------
# 限速：全局令牌桶（多线程共享）
# --------------------------------------------------------------------------
_rate_lock = threading.Lock()
_next_slot = 0.0


def _throttle():
    global _next_slot
    with _rate_lock:
        now = time.time()
        if now < _next_slot:
            time.sleep(_next_slot - now)
        _next_slot = time.time() + MIN_INTERVAL


def contract_codes(sym: str, past: int = 0, future: int = 0,
                   today: date | None = None) -> list[str]:
    """生成 ``[今天-past月, 今天+future月]`` 区间内全部月份合约代码。

    ``past=0, future=12`` 即「当前月与未来 12 个月」——这正是某交易日
    **全部在市合约**的集合（各交易所挂牌不超过一年）。方向务必别搞反：
    写成过去的月份会取到已到期合约的历史价，把加权值算飞。
    """
    today = today or date.today()
    y, m = today.year, today.month
    out = []
    for k in range(-past, future + 1):
        mm = m + k
        yy = y + (mm - 1) // 12
        mm = (mm - 1) % 12 + 1
        if yy < 2000:
            continue
        out.append(f"{sym}{yy % 100:02d}{mm:02d}")
    return out


# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------
def fetch_contract(code: str, retries: int = 3) -> pd.DataFrame | None:
    """抓单个合约的日线。合约不存在返回 None（区别于抓取失败）。"""
    last_exc = None
    for a in range(retries):
        _throttle()
        try:
            r = requests.get(_SINA_DAILY.format(code), timeout=15, headers=_HEAD)
            b = r.text
            if '"d"' not in b:
                return None                      # 该合约不存在
            arr = json.loads(b[b.index("([") + 1: b.rindex("]") + 1])
            if not arr:
                return None
            df = pd.DataFrame(arr)
            df["date"] = pd.to_datetime(df["d"])
            for c in ("o", "h", "l", "c", "v", "p"):
                if c not in df.columns:
                    df[c] = np.nan
                df[c] = pd.to_numeric(df[c], errors="coerce")
            return (df.rename(columns={"o": "open", "h": "high", "l": "low",
                                       "c": "close", "v": "vol", "p": "oi"})
                      [["date", "open", "high", "low", "close", "vol", "oi"]]
                      .dropna(subset=["close"]))
        except Exception as e:                    # noqa: BLE001
            last_exc = e
            time.sleep(0.4 * (a + 1))
    raise RuntimeError(f"抓取 {code} 失败: {type(last_exc).__name__}: {last_exc}")


def fetch_symbol(sym: str, past: int, future: int,
                 log=None) -> dict[str, pd.DataFrame]:
    """抓一个品种在区间内的全部合约日线。返回 {合约代码: DataFrame}。"""
    out: dict[str, pd.DataFrame] = {}
    for code in contract_codes(sym, past, future):
        try:
            df = fetch_contract(code)
        except Exception:                         # noqa: BLE001
            continue                              # 单点失败不拖累整品种
        if df is not None and not df.empty:
            out[code] = df
    if log:
        log(f"    {sym:4} {len(out):>2} 个合约")
    return out


# --------------------------------------------------------------------------
# 加权合成
# --------------------------------------------------------------------------
def build_weighted(frames: dict[str, pd.DataFrame], wcol: str = "oi") -> pd.DataFrame:
    """多合约 → 加权日线 OHLC（按持仓量加权；wcol='vol' 则按成交量加权）。

    仅保留权重 > 0 的合约（无持仓/无成交的月份不参与）。
    """
    parts = []
    for code, df in frames.items():
        d = df[["date", "open", "high", "low", "close", wcol]].copy()
        d["con"] = code
        parts.append(d)
    if not parts:
        return pd.DataFrame()
    allrows = pd.concat(parts, ignore_index=True)
    allrows[wcol] = pd.to_numeric(allrows[wcol], errors="coerce").fillna(0.0)
    allrows = allrows[allrows[wcol] > 0]
    if allrows.empty:
        return pd.DataFrame()

    w = allrows[wcol].to_numpy(float)
    g = allrows.groupby("date", sort=True)
    out = pd.DataFrame(index=g.size().index)
    for col in ("open", "high", "low", "close"):
        v = pd.to_numeric(allrows[col], errors="coerce").to_numpy(float)
        num = pd.Series(v * w, index=allrows.index).groupby(allrows["date"]).sum()
        out[col] = num / g[wcol].sum()
    out["n_con"] = g["con"].nunique()
    out["oi_sum"] = g[wcol].sum()
    out = out.dropna(subset=["close"]).reset_index().rename(columns={"index": "date"})
    out["date"] = pd.to_datetime(out["date"])
    return out.sort_values("date").reset_index(drop=True)


def to_weekly(daily: pd.DataFrame, cutoff: pd.Timestamp | None = None) -> pd.DataFrame:
    """加权日线 → 加权周线（week_end 为周一，只保留已完成周）。"""
    if daily is None or daily.empty:
        return pd.DataFrame()
    d = daily.set_index("date").sort_index()
    wk = d.resample("W-SUN").agg({"open": "first", "high": "max", "low": "min",
                                  "close": "last", "n_con": "mean", "oi_sum": "last"})
    wk = wk.dropna(subset=["close"])
    wk.index = wk.index - pd.Timedelta(days=6)          # 周日 → 周一
    cutoff = cutoff if cutoff is not None else cutoff_monday()
    wk = wk[wk.index < cutoff]
    wk = wk.tail(KEEP_WEEKS)
    out = wk.reset_index()
    out = out.rename(columns={out.columns[0]: "week_end"})   # 索引名可能为 date
    out["week_end"] = pd.to_datetime(out["week_end"])
    return out


def cutoff_monday(today: date | None = None) -> pd.Timestamp:
    """「第一个未完成周」的周一（与 A 股模块同口径）。"""
    today = today or date.today()
    shift = {5: 3, 6: 2}.get(today.weekday(), 0)
    d = pd.Timestamp(today + timedelta(days=shift))
    return d - pd.Timedelta(days=d.weekday())


# --------------------------------------------------------------------------
# 数据仓读写
# --------------------------------------------------------------------------
def load_daily() -> pd.DataFrame:
    if not F_DAILY.exists():
        return pd.DataFrame(columns=["code", "date", "open", "high", "low",
                                     "close", "n_con", "oi_sum"])
    return pd.read_csv(F_DAILY, parse_dates=["date"])


def load_weekly() -> pd.DataFrame:
    if not F_WEEKLY.exists():
        return pd.DataFrame(columns=["code", "week_end", "open", "high", "low",
                                     "close", "n_con", "oi_sum"])
    return pd.read_csv(F_WEEKLY, parse_dates=["week_end"])


def load_last() -> pd.DataFrame:
    if not F_LAST.exists():
        return pd.DataFrame(columns=["code", "last_close", "last_date"])
    d = pd.read_csv(F_LAST)
    if not d.empty:
        d["last_date"] = pd.to_datetime(d["last_date"])
    return d


def _save_daily(df: pd.DataFrame):
    STORE.mkdir(parents=True, exist_ok=True)
    df.sort_values(["code", "date"]).to_csv(F_DAILY, index=False, compression="gzip")


def _save_weekly(df: pd.DataFrame):
    STORE.mkdir(parents=True, exist_ok=True)
    df.sort_values(["code", "week_end"]).to_csv(F_WEEKLY, index=False,
                                                compression="gzip")


def _save_last(df: pd.DataFrame):
    STORE.mkdir(parents=True, exist_ok=True)
    df.sort_values("code").to_csv(F_LAST, index=False)


def save_varieties():
    STORE.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(V.VARIETIES, columns=["code", "name", "market", "sector"]) \
        .to_csv(F_VAR, index=False, encoding="utf-8-sig")


def _read_state(name: str) -> dict:
    p = STATE / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:                          # noqa: BLE001
            return {}
    return {}


def write_update_stats(**kw):
    """覆盖率留痕：看板据此显示抓取情况，让「漏抓」可见。"""
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        data = _read_state("update_stats.json")
        data.update(kw)
        data.setdefault("total", len(V.CODES))
        (STATE / "update_stats.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:                         # noqa: BLE001
        print(f"[fut] 覆盖率留痕写入失败：{e}")


# --------------------------------------------------------------------------
# 全量建库
# --------------------------------------------------------------------------
def rebuild(symbols: list[str] | None = None, workers: int = REBUILD_WORKERS,
            past: int = HIST_MONTHS, future: int = LIVE_MONTHS,
            progress: bool = True) -> dict:
    """全量重建加权指数数据仓。

    一次性把每个品种的合约日线抓下来（含历史月份，故能重建历史加权序列），
    合成日线 → 聚合周线 → 落盘。默认覆盖全部 82 个品种。
    """
    symbols = symbols or V.CODES
    t0 = time.time()
    daily_rows, weekly_rows, last_rows = [], [], []
    failed: list[str] = []

    def work(sym):
        frames = fetch_symbol(sym, past, future)
        if not frames:
            return sym, None, None
        w = build_weighted(frames, "oi")
        if w.empty:
            return sym, None, None
        return sym, w, frames

    done = 0
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(work, s): s for s in symbols}
        for fu in as_completed(futs):
            sym = futs[fu]
            done += 1
            try:
                sym, w, frames = fu.result()
            except Exception as e:                 # noqa: BLE001
                failed.append(sym)
                if progress:
                    print(f"  [{done:>2}/{len(symbols)}] {sym:4} 失败 {type(e).__name__}")
                continue
            if w is None:
                failed.append(sym)
                if progress:
                    print(f"  [{done:>2}/{len(symbols)}] {sym:4} 无可用合约")
                continue
            w = w.copy()
            w.insert(0, "code", sym)
            daily_rows.append(w)
            wk = to_weekly(w)
            if not wk.empty:
                wk.insert(0, "code", sym)
                weekly_rows.append(wk)
            last_rows.append({"code": sym, "last_close": float(w["close"].iloc[-1]),
                              "last_date": w["date"].iloc[-1]})
            if progress:
                print(f"  [{done:>2}/{len(symbols)}] {sym:4} {len(frames):>2}合约 "
                      f"{len(w):>4}日 {len(wk):>3}周  末价={w['close'].iloc[-1]:,.1f}")

    if daily_rows:
        _save_daily(pd.concat(daily_rows, ignore_index=True))
    if weekly_rows:
        _save_weekly(pd.concat(weekly_rows, ignore_index=True))
    if last_rows:
        _save_last(pd.DataFrame(last_rows))
    save_varieties()

    stats = {"fetched": len(daily_rows), "total": len(symbols),
             "failed": len(failed), "elapsed": round(time.time() - t0, 1),
             "source": "sina-rebuild"}
    write_update_stats(ts=pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
                       coverage=len(daily_rows), **stats)
    print(f"[fut] 建库完成 {len(daily_rows)}/{len(symbols)} 品种  "
          f"用时 {time.time()-t0:.0f}s" + (f"  失败 {failed}" if failed else ""))
    return stats


# --------------------------------------------------------------------------
# 增量更新
# --------------------------------------------------------------------------
def update(workers: int = REBUILD_WORKERS, future: int = LIVE_MONTHS,
           progress: bool = True) -> dict:
    """增量：只抓「当前在市合约」（当前月 ~ 未来 future 个月），重算最近一年并合并。

    与仓内数据**按日期合并**（新值覆盖同日旧值，其余保持），绝不整体覆盖，
    避免一次抓取不全导致序列缩水。
    """
    old_daily = load_daily()
    if old_daily.empty:
        print("[fut] 仓内为空，回退到全量建库")
        return rebuild(workers=workers, progress=progress)

    t0 = time.time()
    codes = sorted(old_daily["code"].unique())
    new_rows, last_rows, failed = [], [], []

    def work(sym):
        frames = fetch_symbol(sym, 0, future)
        if not frames:
            return sym, None
        return sym, build_weighted(frames, "oi")

    done = 0
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(work, s): s for s in codes}
        for fu in as_completed(futs):
            sym = futs[fu]
            done += 1
            try:
                sym, w = fu.result()
            except Exception:                      # noqa: BLE001
                failed.append(sym)
                continue
            if w is None or w.empty:
                failed.append(sym)
                continue
            # 只取仓内已有日期之后（含重叠区，用新值修正）
            old = old_daily[old_daily["code"] == sym]
            if not old.empty:
                first = old["date"].min()
                w = w[w["date"] >= first]
            w = w.copy()
            w.insert(0, "code", sym)
            new_rows.append(w)
            last_rows.append({"code": sym, "last_close": float(w["close"].iloc[-1]),
                              "last_date": w["date"].iloc[-1]})

    if not new_rows:
        print("[fut][警告] 增量未取到任何数据，仓内保持不变")
        write_update_stats(ts=pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
                           fetched=0, failed=len(codes), coverage=len(codes),
                           note="增量全部失败，已沿用旧数据")
        return {"fetched": 0, "failed": len(codes), "coverage": len(codes)}

    new_daily = pd.concat(new_rows, ignore_index=True)
    upd_codes = set(new_daily["code"])
    keep = old_daily[~old_daily["code"].isin(upd_codes)]
    merged = pd.concat([keep, new_daily], ignore_index=True)
    merged = merged.drop_duplicates(["code", "date"], keep="last") \
                   .sort_values(["code", "date"])
    _save_daily(merged)

    # 周线整体由日线重算（品种数不多，成本可控且口径统一）
    wk_all = []
    for sym, g in merged.groupby("code"):
        wk = to_weekly(g.drop(columns=["code"]))
        if not wk.empty:
            wk.insert(0, "code", sym)
            wk_all.append(wk)
    _save_weekly(pd.concat(wk_all, ignore_index=True))

    old_last = load_last()
    keep_last = old_last[~old_last["code"].isin(upd_codes)]
    _save_last(pd.concat([keep_last, pd.DataFrame(last_rows)], ignore_index=True))

    coverage = int(merged["code"].nunique())
    stats = {"fetched": len(new_rows), "total": len(codes), "coverage": coverage,
             "failed": len(failed), "elapsed": round(time.time() - t0, 1),
             "source": "sina-increment"}
    write_update_stats(ts=pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"), **stats)
    print(f"[fut] 增量完成 {len(new_rows)}/{len(codes)} 品种  覆盖面 {coverage}  "
          f"用时 {time.time()-t0:.0f}s" + (f"  失败 {len(failed)}" if failed else ""))
    return stats


# --------------------------------------------------------------------------
# 实时行情 → 实时加权价（盘中扫描用）
# --------------------------------------------------------------------------
def _parse_rt(code6: str, body: str, today: str | None = None) -> dict | None:
    f = body.split(",")
    if len(f) < 20:
        return None
    try:
        float(f[0])
        cffex = True                    # 首字段是数字 → 中金所布局
    except ValueError:
        cffex = False
    try:
        if cffex:
            return {"contract": code6, "price": float(f[3]),
                    "oi": float(f[6]) if f[6] else 0.0}
        # 商品期货：[17] 是行情日期。已到期/停牌的合约会一直停在历史日期上，
        # 若不剔除，这些死合约的历史价会混进加权平均（实测把沪金算高 4%）。
        if today and len(f) > 17 and f[17] and f[17] != today:
            return None
        return {"contract": code6, "price": float(f[8]),
                "oi": float(f[13]) if f[13] else 0.0}
    except (ValueError, IndexError):
        return None


def fetch_realtime(contracts: list[str], batch: int = 60) -> pd.DataFrame:
    """批量拉合约实时行情。返回 contract/price/oi（已剔除已到期合约）。"""
    rows = []
    today = date.today().isoformat()
    sess = requests.Session()
    for i in range(0, len(contracts), batch):
        chunk = contracts[i:i + batch]
        _throttle()
        try:
            r = sess.get(_SINA_RT.format(",".join("nf_" + c for c in chunk)),
                         timeout=12, headers=_HEAD)
            r.encoding = "gbk"
        except Exception:                          # noqa: BLE001
            continue
        for line in r.text.strip().split("\n"):
            m = re.match(r'var hq_str_nf_(\w+)="(.*)";', line.strip())
            if not m:
                continue
            rec = _parse_rt(m.group(1), m.group(2), today)
            if rec and rec["price"] > 0:
                rows.append(rec)
    return pd.DataFrame(rows)


def live_weighted(symbols: list[str] | None = None,
                  future: int = LIVE_MONTHS) -> pd.DataFrame:
    """当前实时加权价（按持仓量加权）。返回 code/price/oi_sum/n_con。"""
    symbols = symbols or V.CODES
    want: dict[str, list[str]] = {}
    for s in symbols:
        cs = contract_codes(s, 0, future)
        if cs:
            want[s] = cs
    all_c = [c for cs in want.values() for c in cs]
    rt = fetch_realtime(all_c)
    if rt.empty:
        return pd.DataFrame(columns=["code", "price", "oi_sum", "n_con"])
    rt["code"] = rt["contract"].str.replace(r"\d+$", "", regex=True).str.upper()
    rt = rt[rt["oi"] > 0]
    g = rt.groupby("code")
    out = pd.DataFrame({
        "price": g.apply(lambda d: np.average(d["price"], weights=d["oi"]),
                         include_groups=False),
        "oi_sum": g["oi"].sum(),
        "n_con": g["contract"].nunique(),
    })
    return out.reset_index()
