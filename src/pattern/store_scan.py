"""右侧反转形态 · 云端扫描（GitHub Actions 用）。

与 ``pattern.scan`` 的唯一差别是**数据源**，判定完全共用 ``pattern.core.analyze()``：

    本机   data/daily/*.csv.gz      全历史（385MB，不入库）
    云端   store/daily/*.csv.gz     最近 400 个交易日分片（25MB，随代码入库）

为什么需要它：形态原本只能在本机跑（云端没有日线仓），**电脑一关机就断**。
``store/daily`` 提供 400 个交易日的滚动日线，覆盖形态判定所需的 310~355 个
交易日，因此形态扫描可以完全脱离本机运行。

当日 K 线：
  * 分片里已有当日（update 轮已跑）→ 直接用分片真实值；
  * 分片止于前一交易日（收盘确认轮先跑的情形）→ 用腾讯实时行情合成一根。
    收盘后行情返回的即当日收盘价；后复权序列按不复权涨跌幅外推与真实后复权价
    等价——复权只是对整条序列乘同一个常数。

用法（在 src 目录下）：
    python -m pattern.store_scan              # 扫描 + 写 output/pattern/latest.json + 构建看板数据
    python -m pattern.store_scan --quiet --no-build
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pattern import core as pc                                    # noqa: E402
from pattern import filters as pf                                 # noqa: E402
from pattern.scan import OUT_DIR, STAGE_CODE, STAGE_NAME          # noqa: E402


def _universe() -> set[str]:
    """可交易范围（沪主板/深主板/创业板/科创板）。云端无 data/，走仓内快照。"""
    from alert.cloud_store import live_universe

    u = live_universe()
    if u.empty:
        return set()
    code = u["code"].astype(str)
    return set(code[code.str.match(r"(sh\.6|sz\.0|sz\.3)")])


def _industry() -> dict:
    from alert.ma60_base import _industry_map
    return _industry_map()


def _judge(code: str, df: pd.DataFrame, snap: dict, snap_date: str) -> dict | None:
    """单只标的：分片日线（必要时补当日）→ 判定最后一日所处阶段。

    ⚠️ 必须按**该股自己**的末日决定是否合成当日，不能用全市场末日：
    日线更新偶有漏抓（实测数码视讯/长亮科技/领湃科技缺 10-15），
    用全市场末日会让这些个股被拿**昨天的形态当成今天的**报出来。
    """
    d0 = pd.Timestamp(snap_date)
    if bool((df["date"] == d0).any()):
        sub = df[df["date"] <= d0]                    # 已有当日真实值，直接用
    else:                                             # 缺当日 → 按涨跌幅合成一根
        sub = df[df["date"] < d0]
        if len(sub) < pc.DD_PRIOR + pc.DD_RECENT:
            return None
        c_syn = float(sub["close"].iloc[-1]) * (1 + float(snap["chg_pct"]) / 100.0)
        if c_syn < pc.PRICE_MIN:
            return None
        sub = pd.concat([sub, pd.DataFrame([{
            "date": d0, "close": c_syn,
            "amount": float(snap["amount_wan"]) * 1e4}])], ignore_index=True)
    if len(sub) < pc.DD_PRIOR + pc.DD_RECENT:
        return None
    res = pc.analyze(sub, code=code)
    i = res["n"] - 1
    if i < 0:
        return None
    d = res["detail"].get(i)
    if d is None:
        return None
    # 指标一律取判定引擎输出（口径唯一），此处只补行情元信息
    return {**d, "code": code, "stage": STAGE_CODE[d["stage"]], "estage": d["stage"]}


# 明细字段裁剪统一用 pattern.core.brief（v1.22.1 去重：原与本模块逐字相同的那份已删）
_brief = pc.brief


def scan(quiet: bool = False) -> dict:
    from alert.cloud_store import load_daily_recent
    from pattern.scan import fetch_snapshot, market_snapshot_date

    daily = load_daily_recent()
    if daily.empty:
        raise SystemExit("[pattern] store/daily 分片为空，无法扫描（请先播种并提交仓库）")
    daily = daily.sort_values(["code", "date"])
    last_day = pd.Timestamp(daily["date"].max())

    codes_all = _universe()
    if not codes_all:
        raise SystemExit("[pattern] 在市标的清单为空（store/universe_live.csv.gz）")
    # 直接复用本机快照口径：剔停牌（含用每只自己的行情时间戳滤掉冻结数据）
    q = fetch_snapshot(sorted(codes_all))
    if q.empty:
        raise SystemExit("[pattern] 实时行情为空，无法扫描")

    q_day = pd.Timestamp(market_snapshot_date(q))
    # 快照日取行情日与分片末日的较晚者；**是否合成当日由每只股票自己决定**
    # （见 _judge：个股日线漏抓时仍能正确补当日，而不是把昨天的形态报成今天）
    snap_date = max(q_day, last_day).strftime("%Y-%m-%d")
    synth_global = q_day > last_day

    ind_map = _industry()
    groups = {c: g for c, g in daily.groupby("code", sort=False)}
    codes = [c for c in q.index if c in codes_all and c in groups]

    rows: list[dict] = []
    rejected: list[dict] = []          # 硬筛剔除明细（透明化：看板可展示「另有 N 只因…被剔除」）
    for c in codes:
        r = q.loc[c]
        rec = _judge(c, groups[c], r, snap_date)
        if rec is None:
            continue
        rec.update({
            "price": round(float(r["price"]), 2),
            "turnover": round(float(r["turnover"]), 2),
            "amount_wan": round(float(r["amount_wan"]), 1),
            "mv": round(float(r["mv_float_yi"]), 1),
            "name": str(r["name"]),
        })
        # 引擎级硬筛（回测与实盘同口径）：流通市值区间 + 风险名称（ST/*ST/退市）
        whys = []
        if not pc.in_mv_range(rec["mv"]):
            side = "低于下限" if rec["mv"] < pc.MV_MIN_YI else "高于上限"
            whys.append(f"流通市值 {rec['mv']:.0f}亿（{side} {pc.MV_MIN_YI:.0f}~{pc.MV_MAX_YI:.0f}亿）")
        if pc.is_risk_name(rec["name"]):
            whys.append("风险名称（ST/退市）")
        if whys:
            rejected.append({**_brief(rec), "why": "；".join(whys)})
            continue
        rec["industry"] = ind_map.get(c, "—")
        rec["zt"] = bool(rec.get("zt"))
        rows.append(rec)

    rows.sort(key=lambda r: ({"A": 0, "B": 1, "C": 2}[r["stage"]], -r["chg"]))

    # 新鲜度体检：合成只能补一根，落后更多就会失真（本机踩过：丢一天致 A→C 错判）
    gap = int(np.busday_count(last_day.date(), pd.Timestamp(snap_date).date()))
    if gap > 1:
        lag = (f"（云端日线仓止于 {last_day.date()}，落后 {gap} 个交易日，"
               f"形态判定可能失真）")
    elif synth_global:
        lag = f"（日线仓止于 {last_day.date()}，当日已用实时行情合成）"
    else:
        lag = ""

    # 实盘后置过滤：大额解禁 / 近期重大利空（仅实盘，回测用不了——见 filters 模块说明）
    kept, dropped, fnote = pf.apply_and_report(rows, today=snap_date, quiet=quiet)
    if fnote:
        lag = f"{lag}（{fnote}）" if lag else f"（{fnote}）"
    rows = kept
    counts = {k: sum(1 for r in rows if r["stage"] == k) for k in ("A", "B", "C")}

    obj = {"day": snap_date, "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
           "snapshot_date": snap_date, "source": "cloud", "snap_synth": bool(synth_global),
           "scanned": len(codes), "lag": lag, "counts": counts, "rows": rows,
           "dropped": dropped, "rejected": rejected}
    if not quiet:
        print(f"[pattern] 云端快照 {snap_date} · 扫描 {len(codes)} 只 · "
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
    ap = argparse.ArgumentParser(description="右侧反转形态 · 云端扫描")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-build", action="store_true", help="不构建 dashboard/pattern.json")
    a = ap.parse_args()

    obj = scan(quiet=a.quiet)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    (OUT_DIR / f"scan_{obj['day']}.json").write_text(txt, encoding="utf-8")
    (OUT_DIR / "latest.json").write_text(txt, encoding="utf-8")
    print(f"[pattern] 已写 {OUT_DIR / 'latest.json'}")

    if not a.no_build:
        from dashboard.build import build_pattern
        build_pattern()


if __name__ == "__main__":
    main()
