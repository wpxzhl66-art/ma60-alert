"""看板数据构建：把预警/研究的产物整理成前端可直接消费的静态文件。

产物（均落在 <ROOT>/dashboard/）：
  data.json     每日动态：今日触发、运行流水、历史趋势、数据健康度（小、每次扫描都刷）
  history.json  近一月「上穿 60 周线」事件流水（明细见 dashboard/history.py）
  quotes.json   全市场最新价（不复权），账户估值的兜底价格源
  static.json   名称 + 申万行业（内容变了才提交，几乎不变）

注：weekly.json 自 v1.2.1 起停用——整包 4.5MB，国内访问 raw 拉不动，
    前端改为点开图表时按需向腾讯行情取单股周线（前复权）。

用法：
  python -m dashboard.build --all       # 全量构建
  python -m dashboard.build --names     # 仅刷新 store/stock_names.csv
  python -m dashboard.build --daily     # 日常：quotes + history + data
  python -m dashboard.build --weekly    # 周更：static + history + data
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "store"
STATE = STORE / "state"
OUT = ROOT / "dashboard"
CHARTS = ROOT / "charts"
SRC = ROOT / "src"

REPO = os.environ.get("ALERT_REPO", "wpxzhl66-art/ma60-alert")
CST = timezone(timedelta(hours=8))
WEEK_DECIMALS = 3
HISTORY_MAX = 400

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _now() -> datetime:
    return datetime.now(CST)


def _push_enabled() -> bool:
    """推送总开关（config/alert_config.yaml 的 ``push.enabled``）。

    只是给界面一个可见提示用的：关掉推送后首页显示「推送已停用」，
    避免把「没收到通知」误当成系统故障。读不到配置时按「启用」处理。
    """
    try:
        from alert import pusher                              # 延迟导入，离线时不炸
        return bool(pusher.push_enabled())
    except Exception:                                         # noqa: BLE001
        return True


def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _write_if_changed(p: Path, text: str, label: str) -> bool:
    """内容有变才落盘，避免无意义的仓库提交。"""
    old = p.read_text(encoding="utf-8") if p.exists() else None
    if old == text:
        print(f"[{label}] 内容未变，跳过 ({p.stat().st_size/1024:.0f} KB)")
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    print(f"[{label}] 已更新 -> {p.name} ({len(text.encode('utf-8'))/1024:.0f} KB)")
    return True


# --------------------------------------------------------------------------
# 1. 股票名称（腾讯行情批量拉取，落 store/stock_names.csv）
# --------------------------------------------------------------------------
def _store_codes() -> list[str]:
    p = STORE / "last_close.csv"
    out = []
    if p.exists():
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
            if i == 0:
                continue
            c = line.split(",", 1)[0].strip()
            if c:
                out.append(c)
    return out


def _tencent_code_count() -> int:
    """仓内以腾讯为数据源的标的数（baostock 缺口兜底部分）。"""
    d = _read_json(STATE / "code_sources.json", {}) or {}
    return sum(1 for v in d.values() if v == "tencent")


def refresh_names(codes: list[str] | None = None) -> dict:
    from alert import quoter  # 延迟导入，离线时不炸

    if codes is None:
        codes = _store_codes()
    df = quoter.fetch_quotes(codes, workers=8)
    names = {c: str(r["name"]).strip() for c, r in df.iterrows() if str(r.get("name", "")).strip()}
    lines = ["code,name"] + [f"{c},{n}" for c, n in sorted(names.items())]
    (STORE / "stock_names.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[names] {len(names)}/{len(codes)} 只名称已写入 store/stock_names.csv")
    return names


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
    import csv
    import gzip

    p = STORE / "industry_sw.csv.gz"
    if not p.exists():
        return {}
    op = gzip.open if p.suffix == ".gz" else open
    with op(p, "rt", encoding="utf-8") as f:
        out = {}
        for r in csv.DictReader(f):
            ind = (r.get("industry") or r.get("industry_sw") or "").strip()
            code = (r.get("code") or "").strip()
            if code and ind:
                out[code] = ind
    return out


# --------------------------------------------------------------------------
# 2. static.json —— 名称 + 行业（几乎不变）
# --------------------------------------------------------------------------
def build_static(data_date: str = "") -> dict:
    names = _load_names()
    industry = _load_industry()
    obj = {
        "count": len(names),
        "data_date": data_date,
        "names": names,
        "industry": industry,
    }
    _write_if_changed(OUT / "static.json", _dump(obj), "static")
    return obj


# --------------------------------------------------------------------------
# 3. quotes.json —— 全市场最新价（不复权）
# --------------------------------------------------------------------------
def build_quotes() -> dict:
    items: dict[str, list] = {}
    quote_date = ""
    p = STORE / "last_quotes.csv"
    if p.exists():
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
            if i == 0:
                continue
            parts = line.split(",")
            if len(parts) >= 4:
                try:
                    items[parts[0].strip()] = [round(float(parts[1]), 2),
                                               round(float(parts[2]), 2)]
                except ValueError:
                    pass
                ts = parts[3].strip()
                if len(ts) >= 8:
                    quote_date = max(quote_date, f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}")
    obj = {"date": quote_date, "items": items}
    _write_if_changed(OUT / "quotes.json", _dump(obj), "quotes")
    return obj


# --------------------------------------------------------------------------
# 4. weekly.json —— 已停用（v1.2.1）
# --------------------------------------------------------------------------
# 原实现把全市场 ~130 周收盘打成一个 4.5MB 的 weekly.json，由前端在打开周K时下载。
# 实测国内访问 raw.githubusercontent.com 拉该文件约 1.4 KB/s（30 秒仅 40KB），
# 导致「点开股票一直转圈」，且其数据源为后复权，与行情软件显示不一致。
# 现改为：前端点开图表时按需向腾讯行情取该股周线（前复权，几 KB，国内秒开），
# 持仓估值另行取不复权真实价。本文件不再生成，dashboard/weekly.json 已移除。
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 5. data.json —— 每日动态
# --------------------------------------------------------------------------
def _classify_run(created_utc: str, event: str) -> tuple[str, str]:
    try:
        dt = datetime.fromisoformat(created_utc.replace("Z", "+00:00")).astimezone(CST)
    except Exception:  # noqa: BLE001
        return ("手动触发", "manual")
    if event == "schedule":
        hm = dt.strftime("%H:%M")
        if hm in ("10:05", "11:05", "14:05", "14:55"):
            return (f"盘中扫描 {hm}", "intraday")
        if hm == "15:15":
            return ("收盘确认 15:15", "confirm")
        if hm == "16:07":
            return ("数据更新 16:07", "update")
        return (f"定时任务 {hm}", "schedule")
    return ("手动触发", "manual")


def fetch_runs(limit: int = 16) -> list[dict]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        return []
    import urllib.request

    url = f"https://api.github.com/repos/{REPO}/actions/runs?per_page={limit}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ma60-dashboard",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[runs] GitHub API 取数失败：{e}")
        return []

    out = []
    for it in payload.get("workflow_runs", []):
        label, kind = _classify_run(it.get("created_at", ""), it.get("event", ""))
        started = datetime.fromisoformat(it["created_at"].replace("Z", "+00:00"))
        ended = datetime.fromisoformat(it["updated_at"].replace("Z", "+00:00"))
        out.append({
            "id": it["id"], "label": label, "kind": kind,
            "event": it.get("event"), "status": it.get("status"),
            "conclusion": it.get("conclusion"),
            "at": started.astimezone(CST).strftime("%m-%d %H:%M"),
            "duration_s": max(0, int((ended - started).total_seconds())),
            "url": it.get("html_url"), "sha": (it.get("head_sha") or "")[:7],
        })
    print(f"[runs] 取到 {len(out)} 条运行记录")
    return out


def _health() -> dict:
    import gzip

    def size_mb(p: Path) -> float:
        return round(p.stat().st_size / 1048576, 2) if p.exists() else 0.0

    weekly_rows = 0
    p = STORE / "weekly_closes.csv.gz"
    if p.exists():
        with gzip.open(p, "rt", encoding="utf-8") as f:
            weekly_rows = sum(1 for _ in f) - 1

    days = sorted([d.name for d in CHARTS.iterdir() if d.is_dir()]) if CHARTS.exists() else []
    chart_count = sum(len(list((CHARTS / d).glob("*.png"))) for d in days[-7:])

    last_close = STORE / "last_close.csv"
    data_date = ""
    if last_close.exists():
        lines = last_close.read_text(encoding="utf-8").splitlines()
        if len(lines) > 1:
            cols = lines[1].split(",")
            if len(cols) >= 3:
                data_date = cols[2].strip()

    us = _read_json(STATE / "update_stats.json", {}) or {}

    return {
        "stocks": len(_store_codes()),
        "weekly_rows": weekly_rows,
        "data_date": data_date,
        "chart_days": days[-10:],
        "chart_count": chart_count,
        "names_covered": len(_load_names()),
        "industries": len(_load_industry()),
        "store_mb": round(sum(size_mb(STORE / f) for f in
                              ("weekly_closes.csv.gz", "last_close.csv", "avg_amt20.csv",
                               "last_quotes.csv", "industry_sw.csv.gz", "stock_names.csv")), 2),
        "source": "baostock（存储后复权·展示前复权）",
        "market_total": int(us.get("market_total") or 0),
        "update_stats": {
            "ts": us.get("ts", ""),
            "fetched": int(us.get("fetched") or 0),
            "todo": int(us.get("todo") or 0),
            "coverage": int(us.get("coverage") or 0),
            "failed": int(us.get("failed") or 0),
            "timed_out": bool(us.get("timed_out") or False),
            "fallback": int(us.get("fallback") or 0),
            "unresolved": int(us.get("unresolved") or 0),
            "source": us.get("source", ""),
            "gap_filled": int(us.get("gap_filled") or 0),
            "gap_short": int(us.get("gap_short") or 0),
            "gap_checked": int(us.get("gap_checked") or 0),
        },
        "coverage": us.get("refresh") or {},
        "tx_codes": _tencent_code_count(),
    }


def build_futures() -> dict:
    """生成 dashboard/fut.json：期货加权指数的触发清单与全品种概况。

    数据源为 store/fut/（云端每日增量更新）。触发结果优先取最近一次扫描写入的
    store/state/futures_triggers.json；缺失或过期时按纯日线口径现场重算。
    """
    import sys as _sys
    src_dir = str(ROOT / "src")
    if src_dir not in _sys.path:
        _sys.path.insert(0, src_dir)
    import pandas as pd
    from alert.config import load
    from fut import core as fcore
    from fut import scan as fscan
    from fut import varieties as fV

    weekly = fcore.load_weekly()
    daily = fcore.load_daily()
    today = date.today().isoformat()

    near_pct = 0.03
    try:
        near_pct = float(load()["futures"].get("near_pct", 0.03))
    except Exception:                               # noqa: BLE001
        pass

    hp = {
        "covered": int(daily["code"].nunique()) if not daily.empty else 0,
        "total": len(fV.CODES),
    }
    fus = _read_json(STORE / "fut" / "state" / "update_stats.json", {}) or {}
    hp.update({"ts": fus.get("ts", ""), "source": fus.get("source", ""),
               "failed": int(fus.get("failed") or 0)})

    # ---- 触发清单 ----
    rows_df = None
    st = _read_json(STATE / "futures_triggers.json", {}) or {}
    if st.get("day") == today and st.get("rows"):
        rows_df = pd.DataFrame(st["rows"])
        print(f"      期货触发取用扫描结果：{len(rows_df)} 个品种（{st.get('ts','')}）")
    if rows_df is None or rows_df.empty:
        try:
            rows_df = fscan.evaluate(weekly=weekly, daily=daily,
                                     use_realtime=True, near_pct=near_pct)
        except Exception as e:                      # noqa: BLE001
            print(f"      期货触发即时判定失败：{e}")
            rows_df = pd.DataFrame()

    # ---- 全品种概况（含实时价，与触发列表同口径）----
    all_rows: list[dict] = []
    wk_cache: dict[str, list] = {}
    snap = None
    try:
        snap = fscan.snapshot(near_pct=near_pct, use_realtime=True,
                             weekly=weekly, daily=daily)
    except Exception as e:                          # noqa: BLE001
        print(f"      期货全品种指标计算失败：{e}")

    if not weekly.empty:
        for sym, g in weekly.groupby("code"):
            g = g.sort_values("week_end")
            if len(g) <= 140:
                wk_cache[sym] = [[str(pd.Timestamp(w).date()), round(float(c), 3)]
                                 for w, c in zip(g["week_end"], g["close"])]
            else:
                wk_cache[sym] = [[str(pd.Timestamp(w).date()), round(float(c), 3)]
                                 for w, c in zip(g["week_end"].tail(140),
                                                 g["close"].to_numpy(float)[-140:])]

    if snap is not None and not snap.empty:
        for rec in snap.to_dict("records"):
            rec.pop("live", None)
            rec["near"] = rec.get("status") in ("突破", "临近")
            all_rows.append(rec)
    all_rows.sort(key=lambda r: abs(r.get("dev_pct", 0)))

    # ---- 触发品种附上周线序列（供图表按需展示）----
    rows_out: list[dict] = []
    if rows_df is not None and len(rows_df):
        for rec in rows_df.to_dict("records"):
            sym = rec.get("code")
            rec = {k: (None if isinstance(v, float) and v != v else v)
                   for k, v in rec.items()}
            rec["wk"] = wk_cache.get(sym, [])
            rows_out.append(rec)
        # 触发品种带上「可看图」标记（周线只存一份在 rows 里，避免体积翻倍）
        chartable = {r["code"] for r in rows_out}
        for r in all_rows:
            r["chart"] = r["code"] in chartable

    obj = {
        "generated_at": _now().isoformat(timespec="seconds"),
        "day": st.get("day") or today,
        "ts": st.get("ts") or "",
        "near_pct": near_pct,
        "rows": rows_out,
        "all": all_rows,
        "health": hp,
        "sectors": fV.SECTORS,
    }
    _write_if_changed(OUT / "fut.json", _dump(obj), "fut")
    nb = sum(1 for r in rows_out if r.get("status") == "突破")
    print(f"      期货 {len(all_rows)} 个品种可判定 / 触发 {len(rows_out)} 个"
          f"（突破 {nb}）")
    return obj


def build_pattern() -> dict:
    """生成 dashboard/pattern.json：右侧反转形态扫描结果 + 回测结论。

    数据源为扫描产物 output/pattern/latest.json —— 本机 pattern.scan 与云端
    pattern.store_scan 都写这个路径（判定共用 pattern.core，结果一致）。
    回测结论优先读**入库的** store/pattern_backtest.json（output/ 已 gitignore，
    云端读不到），使推送文案与看板引用的数字都有产物来源而非硬编码。

    两道防回退保护（2026-10-15 推送覆盖云端数据的教训）：
      1. 扫描产物缺失 → 原样保留既有 pattern.json，不写空文件；
      2. 扫描产物日期早于线上 → 不覆盖。
    """
    src = ROOT / "output" / "pattern" / "latest.json"
    obj = _read_json(src, {}) or {}
    old = _read_json(OUT / "pattern.json", {}) or {}
    # 筛选口径随数据一起落盘，前端不用硬编码数字。
    # 导入失败时退回字面值——看板构建不该因为一个常量读不到就整块失败。
    try:
        from pattern import core as pc
        from pattern import filters as pf
        fmeta = {"mv": [pc.MV_MIN_YI, pc.MV_MAX_YI],
                 "lift_days": pf.LIFT_WINDOW_DAYS,
                 "lift_ratio": pf.LIFT_RATIO_MAX,
                 "news_days": pf.NEWS_WINDOW_DAYS}
    except Exception:                                     # noqa: BLE001
        fmeta = {"mv": [30, 200], "lift_days": 60, "lift_ratio": 0.15, "news_days": 90}
    if not obj.get("day"):
        print("[pattern] 扫描产物缺失（首跑或离线），保留既有 pattern.json")
        return old
    if old.get("day") and str(obj["day"]) < str(old["day"]):
        print(f"[pattern] 本机产物 {obj['day']} 早于线上 {old['day']}，不覆盖")
        return old

    # 回测结论：output/ 已 gitignore → 云端读不到，因此以**入库的 store 副本**为准，
    # 本机 output/ 产物作回退（这样推送与看板引用的数字在云端也有来源，不靠硬编码）
    bt = (_read_json(ROOT / "store" / "pattern_backtest.json", {})
          or _read_json(ROOT / "output" / "_signal" / "pattern_backtest.json", {}) or {})
    summary: dict = {}
    for k in ("e4", "e3", "e2"):
        st = (bt.get("stages") or {}).get(k) or {}
        stat = st.get("stat") or {}
        d20, d60 = stat.get("20") or {}, stat.get("60") or {}
        if not d60:
            continue
        summary[k] = {
            "n": int(st.get("n_dedup") or 0),
            "ex20_med": round(float(d20.get("ex_med") or 0) * 100, 2),
            "ex60_med": round(float(d60.get("ex_med") or 0) * 100, 2),
            "ex60_win": round(float(d60.get("ex_win") or 0) * 100, 1),
            "ex60_mean": round(float(d60.get("ex_mean") or 0) * 100, 2),
            "t60": round(float(d60.get("clust_t") or 0), 2),
            "gap_zt": round(float(st.get("gap_zt") or 0) * 100, 1),
        }

    out = {
        "generated_at": _now().isoformat(timespec="seconds"),
        "day": obj.get("day", ""),
        "ts": obj.get("ts", ""),
        "scanned": int(obj.get("scanned") or 0),
        "lag": obj.get("lag", ""),
        "counts": obj.get("counts") or {"A": 0, "B": 0, "C": 0},
        "rows": obj.get("rows") or [],
        # 被筛掉的标的与口径一并落盘：名单变短时能看出「为什么少」，而不是黑箱
        "dropped": obj.get("dropped") or [],
        "rejected": obj.get("rejected") or [],
        "filter": fmeta,
        "backtest": summary,
        "span": (bt.get("meta") or {}).get("span", []),
    }
    _write_if_changed(OUT / "pattern.json", _dump(out), "pattern")
    c = out["counts"]
    print(f"      形态扫描 {out['day']} · 扫描 {out['scanned']} 只 · "
          f"A {c.get('A', 0)} / B {c.get('B', 0)} / C {c.get('C', 0)}")
    return out


def build_data(static_obj: dict | None = None) -> dict:
    latest = _read_json(STATE / "latest_triggers.json", {}) or {}
    push = _read_json(STATE / "push_state.json", {}) or {}
    hist = _read_json(STATE / "history.json", []) or []
    health = _health()
    if static_obj and static_obj.get("data_date"):
        health["data_date"] = static_obj["data_date"]
    health["last_update"] = health.get("data_date", "")

    by_day: dict[str, dict] = {}
    peak: dict[str, int] = {}
    for h in hist:
        d = h.get("day")
        if not d:
            continue
        if h.get("mode") == "demo":
            continue
        cur = by_day.get(d)
        if cur is None or h.get("ts", "") >= cur.get("ts", ""):
            by_day[d] = h
        peak[d] = max(peak.get(d, 0), h.get("n", 0))

    trend = [{
        "day": d, "n": rec.get("n", 0), "scanned": rec.get("scanned", 0),
        "mode": rec.get("mode", ""), "ts": rec.get("ts", ""), "peak": peak.get(d, rec.get("n", 0)),
    } for d, rec in sorted(by_day.items())[-30:]]

    # 历史触发摘要（明细在 history.json，这里只带几个数字供首页入口卡用）
    hj = _read_json(OUT / "history.json", {}) or {}
    hs = hj.get("summary") or {}

    obj = {
        "generated_at": _now().isoformat(timespec="seconds"),
        "today": date.today().isoformat(),
        "runs": fetch_runs(),
        "triggers": {
            "day": latest.get("day", ""),
            "ts": latest.get("ts", ""),
            "mode": latest.get("mode", ""),
            "mode_label": {"intraday": "盘中扫描", "confirm": "收盘确认",
                           "demo": "演示"}.get(latest.get("mode", ""), latest.get("mode", "")),
            "scanned": latest.get("scanned", 0),
            "count": len(latest.get("rows", [])),
            "rows": latest.get("rows", []),
        },
        "push": {"date": push.get("date", ""), "count": int(push.get("count", 0)), "limit": 5,
                 # 总开关：关掉后界面会显示「推送已停用」，避免被误当成系统故障
                 "enabled": _push_enabled()},
        "trend": trend,
        "health": health,
        "hist": {
            "start": hj.get("start", ""), "end": hj.get("end", ""),
            "events": int(hs.get("events", 0)), "stocks": int(hs.get("stocks", 0)),
            "above_now": int(hs.get("above_now", 0)),
        },
        "repo": REPO,
    }
    _write_if_changed(OUT / "data.json", _dump(obj), "data")
    print(f"      触发 {obj['triggers']['count']} 只 / 趋势 {len(trend)} 天 / 运行 {len(obj['runs'])} 条")
    return obj


def main() -> None:
    ap = argparse.ArgumentParser(description="看板数据构建")
    ap.add_argument("--all", action="store_true", help="全量（names+static+quotes+data）")
    ap.add_argument("--daily", action="store_true", help="日常：quotes + history + data")
    ap.add_argument("--weekly", action="store_true", help="周更：static + history + data")
    ap.add_argument("--names", action="store_true", help="刷新 stock_names.csv")
    ap.add_argument("--static", action="store_true")
    ap.add_argument("--quotes", action="store_true")
    ap.add_argument("--data", action="store_true")
    ap.add_argument("--futures", action="store_true", help="期货板块 fut.json")
    ap.add_argument("--pattern", action="store_true", help="右侧反转形态 pattern.json")
    a = ap.parse_args()
    if not any([a.all, a.daily, a.weekly, a.names, a.static, a.quotes, a.data,
                a.futures, a.pattern]):
        a.all = True

    static_obj = None
    if a.all or a.names:
        refresh_names()
    if a.all or a.weekly or a.static:
        static_obj = build_static()
    if a.all or a.daily or a.quotes:
        build_quotes()
    # 历史触发流水：data.json 的 hist 摘要依赖它，必须先刷新。
    # 云端没有 data/daily（不入库），build_history 自动降级为「只追加当日触发」。
    if a.all or a.daily or a.weekly or a.data:
        from dashboard.history import build as build_history
        build_history()
        build_data(static_obj)
    if a.all or a.daily or a.weekly or a.futures:
        build_futures()
    # 形态数据由形态扫描专属产出（本机 pattern.scan / 云端 pattern.store_scan），
    # 不与 --daily 联动：数据更新轮不跑形态扫描，latest.json 还停在上一次扫描，
    # 重建只会无谓地触碰 dashboard/pattern.json（两道防回退保护能兜住，但没必要）。
    if a.all or a.pattern:
        build_pattern()


if __name__ == "__main__":
    main()
