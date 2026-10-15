"""推送文案模板（含同花顺跳转链接与回测参照）。

⚠️ 定位（2026-10-15 起）：本信号是**掉队风险提示**，不是买入信号。
依据：14 年全历史回测（5290 只 / 20.4 万原始事件 / 去重叠 5.3 万条），
触发后相对全市场等权超额中位 −2.1%（20 日）/ −4.2%（60 日）/ −6.8%（120 日），
16/16 个年份为负。改动本文件前请先读 output/_signal/回测报告.md。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

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


def risk_level(amt_ratio, turnover) -> tuple[str, str]:
    """按量能热度给触发分档，返回 (标签, 说明)。

    阈值取自全历史回测的分档中位超额（+60 日，相对全市场等权）：
      量比 ≥2.5 → −6.4%、换手 ≥5% → −7.6%（胜率 32–34%）
      量比 <1.0 且 换手 <2% → −2.8% / −3.0%（胜率 41–42%）
    即「放量上穿 = 追高」，后续掉队更明显。
    """
    a = float(amt_ratio) if np_finite(amt_ratio) else None
    t = float(turnover) if np_finite(turnover) else None
    hot = (a is not None and a >= 2.5) or (t is not None and t >= 5)
    calm = a is not None and a < 1.0 and t is not None and t < 2
    if hot:
        return ("放量追高", "历史上这一档掉队最明显")
    if calm:
        return ("缩量温和", "历史上这一档相对温和")
    return ("", "")


def _backtest_ref() -> str:
    """全历史回测结论（静态，结论稳定，故直接内嵌以免依赖产物文件）。"""
    if not load()["push"].get("include_backtest", True):
        return ""
    return ("\n\n---\n\n"
            "**⚠️ 信号性质（14 年全历史回测，5.3 万次触发）**\n"
            "触发后跑输全市场等权：20 日 −2.1%、60 日 −4.2%、120 日 −6.8%，"
            "16 个年份无一例外。\n"
            "**这是掉队风险提示，不是买入信号**——持仓股触发宜考虑减仓，"
            "空仓者不宜据此买入。")


def build_message(trig, mode: str, chart_urls: dict | None = None) -> tuple[str, str]:
    """trig: scanner.evaluate() 输出；chart_urls: code -> 周K图公网URL。

    云端在 A 股无新触发时传入 None，此处按空表处理。
    """
    if trig is None:
        trig = pd.DataFrame()
    tag = "盘中预警" if mode == "intraday" else "收盘确认"
    n = len(trig)
    lines = []
    n_list = min(n, load()["push"].get("max_stocks_per_msg", 30))
    title = (f"【60周线上穿·掉队风险·{tag}】{n} 只"
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
        lvl, _lvl_note = risk_level(vr, tn)
        lvl_txt = f" **〔{lvl}〕**" if lvl else ""
        cnt = int(r.get("a_events_1y", 0))
        first = "是" if cnt == 0 else f"否（近一年已{cnt}次）"
        lines.append(
            f"**{r['name']} {code}**{lvl_txt}  {_bucket(mv) if mv else '—'}｜{ind}\n"
            f"现价 {r['price']:.2f}（{r['chg_pct']:+.2f}%）"
            f"｜超越60周线 {r['dev_pct']*100:+.1f}%{vr_txt}\n"
            f"60周线 {r['slope']}{tn_txt}｜近一年首次上穿：{first}"
            + (f"\n\n![周K]({chart_urls[code]})" if chart_urls and code in chart_urls else "")
            + f"\n👉 {ths_links(code)}"
        )
    desp = "\n\n---\n\n".join(lines) if lines else \
        "交易时段内未发现新的上穿（无新增风险提示）。"
    desp += _backtest_ref()
    return title, desp


def np_finite(v) -> bool:
    try:
        return v == v and abs(float(v)) != float("inf")
    except Exception:  # noqa: BLE001
        return False
