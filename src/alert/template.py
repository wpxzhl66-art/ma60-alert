"""推送文案模板（含同花顺跳转链接与回测参照）。"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .config import load

ROOT = Path(__file__).resolve().parents[2]
RESULT = ROOT / "output" / "study_result.json"


def ths_links(code: str) -> str:
    """同花顺网页行情链接（实测 scheme 无法唤起 App，仅保留网页）。"""
    code6 = code.split(".")[1]
    cfg = load()["jump"]
    return f"[同花顺行情页·{code6}]({cfg['web'].format(code6=code6)})"


def _bucket(mv_yi: float) -> str:
    cfg = load()["mktcap_buckets"]
    if mv_yi >= cfg["large_min"]:
        return "大盘"
    if mv_yi <= cfg["small_max"]:
        return "小盘"
    return "中盘"


def _backtest_ref() -> str:
    if not load()["push"].get("include_backtest", True) or not RESULT.exists():
        return ""
    try:
        res = json.loads(RESULT.read_text(encoding="utf-8"))
        rows = [r for r in res.get("overall", [])
                if r.get("event_type") == "A_上穿突破" and r.get("n_events", 0) > 0]
        if not rows:
            return ""
        parts = [f"{int(r['horizon'])}日 胜率{r.get('win_rate', 0)*100:.0f}%"
                 f" 均值{r.get('mean', 0)*100:+.1f}%" for r in rows]
        sample = res.get("meta", {}).get("scope", "样本")
        return f"\n\n> 回测参照（{sample}，A类上穿）：\n> " + " ｜ ".join(parts) + "\n"
    except Exception:  # noqa: BLE001
        return ""


def build_message(trig, mode: str, chart_urls: dict | None = None) -> tuple[str, str]:
    """trig: scanner.evaluate() 输出；chart_urls: code -> 周K图公网URL。"""
    tag = "盘中预警" if mode == "intraday" else "收盘确认"
    n = len(trig)
    lines = []
    n_list = min(n, load()["push"].get("max_stocks_per_msg", 30))
    title = (f"【60周线上穿·{tag}】{n} 只"
             + (f"（仅列前{n_list}）" if n > n_list else "")) if n \
        else f"【60周线上穿·{tag}】今日无触发"
    for code, r in trig.head(n_list).iterrows():
        mv = float(r.get("mv_float_yi", 0) or 0)
        ind = r.get("industry")
        ind = "未知" if ind is None or (isinstance(ind, float) and ind != ind) else str(ind)
        vr = r.get("amt_ratio")
        vr_txt = f"｜量比(额) {vr:.1f}x" if np_finite(vr) else ""
        tn = r.get("turnover")
        tn_txt = f"｜换手 {tn:.1f}%" if np_finite(tn) else ""
        cnt = int(r.get("a_events_1y", 0))
        first = "是" if cnt == 0 else f"否（近一年已{cnt}次）"
        lines.append(
            f"**{r['name']} {code}**  {_bucket(mv) if mv else '—'}｜{ind}\n"
            f"现价 {r['price']:.2f}（{r['chg_pct']:+.2f}%）"
            f"｜超越60周线 {r['dev_pct']*100:+.1f}%{vr_txt}\n"
            f"60周线 {r['slope']}{tn_txt}｜近一年首次上穿：{first}"
            + (f"\n\n![周K]({chart_urls[code]})" if chart_urls and code in chart_urls else "")
            + f"\n👉 {ths_links(code)}"
        )
    desp = "\n\n---\n\n".join(lines) if lines else "交易时段内未发现新的 A 类上穿。"
    desp += _backtest_ref()
    return title, desp


def np_finite(v) -> bool:
    try:
        return v == v and abs(float(v)) != float("inf")
    except Exception:  # noqa: BLE001
        return False
