"""看板数据构建：把预警/研究的产物整理成前端可直接消费的静态文件。

产物（均落在 <ROOT>/dashboard/）：
  data.json     每日动态：今日触发、运行流水、历史趋势、数据健康度（小、每次扫描都刷）
  quotes.json   全市场最新价（不复权），账户估值的兜底价格源
  static.json   名称 + 申万行业（内容变了才提交，几乎不变）
  weekly.json   周收盘序列（纯文本，git 增量友好；内容变了才提交）

用法：
  python -m dashboard.build --all       # 全量构建
  python -m dashboard.build --names     # 仅刷新 store/stock_names.csv
  python -m dashboard.build --daily     # 日常：quotes + data
  python -m dashboard.build --weekly    # 周更：static + weekly + data
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
# 4. weekly.json —— 周收盘序列（全局周轴 + 每只起始下标）
# --------------------------------------------------------------------------
def _refresh_index_weekly(code: str = "sh.000300", n: int = 140) -> Path | None:
    """拉沪深300 周线做绩效基准，缓存到 store/index_weekly.csv。失败则沿用缓存。"""
    import urllib.request

    sec = code.replace(".", "")
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sec},week,,,{n},"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            payload = json.loads(r.read().decode("utf-8", "ignore"))
        blk = payload["data"][sec]
        bars = None
        for v in blk.values():
            if isinstance(v, list) and v and isinstance(v[0], list) and len(v[0]) >= 3:
                bars = v
                break
        if not bars:
            raise ValueError("返回结构不含周线数组")
        lines = ["code,week_end,close"]
        for b in bars:
            lines.append(f"{code},{b[0]},{round(float(b[2]), 4)}")
        (STORE / "index_weekly.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[index] {code} 基准周线 {len(bars)} 根已刷新")
        return STORE / "index_weekly.csv"
    except Exception as e:  # noqa: BLE001
        print(f"[index] 沪深300 周线拉取失败（沿用缓存）：{e}")
        p = STORE / "index_weekly.csv"
        return p if p.exists() else None


def build_weekly(refresh_index: bool = True) -> None:
    import csv
    import gzip

    src = STORE / "weekly_closes.csv.gz"
    if not src.exists():
        print("[weekly] 缺少 store/weekly_closes.csv.gz，跳过")
        return

    weeks: list[str] = []
    week_idx: dict[str, int] = {}
    rows: dict[str, list[tuple[int, float]]] = {}

    def put(code: str, wk: str, close: float) -> None:
        if wk not in week_idx:
            week_idx[wk] = len(weeks)
            weeks.append(wk)
        rows.setdefault(code, []).append((week_idx[wk], close))

    with gzip.open(src, "rt", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            put(r["code"], r["week_end"], float(r["close"]))

    idx = _refresh_index_weekly() if refresh_index else (STORE / "index_weekly.csv")
    if idx and idx.exists():
        with open(idx, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                put(r["code"], r["week_end"], float(r["close"]))

    stocks: dict[str, dict] = {}
    for code, seq in rows.items():
        seq.sort()
        idxs = [i for i, _ in seq]
        closes = [round(c, WEEK_DECIMALS) for _, c in seq]
        contiguous = idxs == list(range(idxs[0], idxs[0] + len(idxs)))
        entry: dict = {"c": closes}
        if contiguous:
            entry["b"] = idxs[0]
        else:
            entry["i"] = idxs
        stocks[code] = entry

    obj = {"weeks": weeks, "stocks": stocks}
    _write_if_changed(OUT / "weekly.json", _dump(obj), "weekly")


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
        "source": "baostock（后复权）",
    }


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
        "push": {"date": push.get("date", ""), "count": int(push.get("count", 0)), "limit": 5},
        "trend": trend,
        "health": health,
        "repo": REPO,
    }
    _write_if_changed(OUT / "data.json", _dump(obj), "data")
    print(f"      触发 {obj['triggers']['count']} 只 / 趋势 {len(trend)} 天 / 运行 {len(obj['runs'])} 条")
    return obj


def main() -> None:
    ap = argparse.ArgumentParser(description="看板数据构建")
    ap.add_argument("--all", action="store_true", help="全量（names+static+quotes+weekly+data）")
    ap.add_argument("--daily", action="store_true", help="日常：quotes + data")
    ap.add_argument("--weekly", action="store_true", help="周更：static + weekly + data")
    ap.add_argument("--names", action="store_true", help="刷新 stock_names.csv")
    ap.add_argument("--static", action="store_true")
    ap.add_argument("--quotes", action="store_true")
    ap.add_argument("--data", action="store_true")
    a = ap.parse_args()
    if not any([a.all, a.daily, a.weekly, a.names, a.static, a.quotes, a.data]):
        a.all = True

    static_obj = None
    if a.all or a.names:
        refresh_names()
    if a.all or a.weekly or a.static:
        static_obj = build_static()
    if a.all or a.daily or a.quotes:
        build_quotes()
    if a.all or a.weekly:
        build_weekly()
    if a.all or a.daily or a.weekly or a.data:
        build_data(static_obj)


if __name__ == "__main__":
    main()
