"""生成自包含的 HTML 研究报告。

输入：output/study_result.json（由 src/run_study.py 产出）
输出：output/60周线触及事件研究_报告_<version>.html

设计：浅灰底 + 白卡片 + iOS 系统色的简洁风格（对齐用户指定审美）。
图表用 ECharts（CDN）；同时把所有关键数字以文字/表格冗余呈现，
即使图表脚本不可用，报告仍然可读。
涨跌配色遵循中国大陆习惯：涨=红、跌=绿。

用法：
    python src/make_report.py [--version v1.1.1]
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"

BENCH_CN = {"hs300": "沪深300", "industry": "申万一级行业"}
TYPE_CN = {"A_上穿突破": "类型A · 上穿突破", "B_上方回踩": "类型B · 上方回踩"}


def _fmt_pct(x, nd=2):
    if x is None:
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    if v != v:
        return "—"
    return f"{v*100:.{nd}f}%"


def _cls(x):
    """涨红跌绿。"""
    if x is None:
        return ""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    if v != v:
        return ""
    return "up" if v > 0 else ("down" if v < 0 else "")


def _esc(x):
    return html.escape(str(x))


def build_html(result: dict, version: str, stage: str = "final") -> str:
    meta = result.get("meta", {})
    overall = result.get("overall", []) or []
    stratified = result.get("stratified", []) or []
    conc = result.get("concentration_top", []) or []
    counts = result.get("event_type_counts", {}) or {}
    boot = result.get("bootstrap", []) or []
    dedup_cmp = result.get("dedup_compare", []) or []

    horizons = meta.get("horizons", [20, 40, 60, 120, 250])
    scope = meta.get("scope", "")

    # 阶段横幅：验证小样本与正式全样本必须显著区分，避免结果被误读
    if stage == "validation":
        banner = (
            '<div class="banner">'
            '<b>流程验证样本</b>'
            f'<span>本报告基于 <b>{_esc(scope)}</b> 运行，目的是验证「取数 → 事件检测 → '
            '收益计算 → 分层统计」全链路与各项口径是否无误，'
            '<b>尚不是全A股的最终研究结论</b>。样本量偏小且集中于沪深300成分股，'
            '结果受少数标的支配的可能性更高，请勿据此作出投资判断。</span>'
            '</div>')
        lede_scope = (f'当前为流程验证版，样本为 {_esc(scope)}；'
                      '最终版将覆盖全A股（含退市股，以消除生存者偏差）。')
    else:
        banner = ""
        lede_scope = '以全A股为样本（含退市股，消除生存者偏差）。'

    # ---------- KPI ----------
    n_events = meta.get("n_events", 0)
    n_stocks = meta.get("n_stocks", 0)
    n_dedup = meta.get("n_events_dedup", 0)
    top10 = meta.get("top10_share")
    kpi = [
        ("事件总数", f"{n_events:,}", f"涉及 {n_stocks:,} 只标的", ""),
        ("类型A / 类型B", f"{counts.get('A_上穿突破', 0):,} / {counts.get('B_上方回踩', 0):,}",
         "上穿突破 / 上方回踩", ""),
        ("去重后事件", f"{n_dedup:,}", f"26周冷却期；{n_events and n_dedup/n_events*100:.0f}% 保留", ""),
        ("Top10 标的占比", _fmt_pct(top10, 1), ">20% 说明结果受少数标的支配",
         "warn" if (top10 or 0) > 0.2 else ""),
    ]
    kpi_html = "".join(
        f'<div class="kpi {c}"><div class="kpi-label">{_esc(a)}</div>'
        f'<div class="kpi-value">{_esc(b)}</div>'
        f'<div class="kpi-sub">{_esc(d)}</div></div>'
        for a, b, d, c in kpi
    )

    # ---------- 总览表 ----------
    ths = ["事件类型", "窗口", "有效样本", "右删失"] + \
          ["均值", "中位数", "胜率", "盈亏比"] + \
          ["超额均值(沪深300)", "超额胜率(沪深300)", "超额均值(行业)", "超额胜率(行业)"] + \
          ["平均MFE", "平均MAE", "平均MDD"]
    thead = "".join(f"<th>{_esc(t)}</th>" for t in ths)
    tbody = []
    for r in overall:
        pl = r.get("pl_ratio")
        pl_s = "—" if pl is None else f"{float(pl):.2f}"
        tbody.append(
            "<tr>"
            f'<td class="t">{_esc(TYPE_CN.get(r.get("event_type"), r.get("event_type")))}</td>'
            f'<td class="num">{r.get("horizon")}日</td>'
            f'<td class="num">{r.get("n_events", 0):,}</td>'
            f'<td class="num muted">{r.get("n_censored", 0):,}</td>'
            f'<td class="num {_cls(r.get("mean"))}">{_fmt_pct(r.get("mean"))}</td>'
            f'<td class="num {_cls(r.get("median"))}">{_fmt_pct(r.get("median"))}</td>'
            f'<td class="num">{_fmt_pct(r.get("win_rate"), 1)}</td>'
            f'<td class="num">{pl_s}</td>'
            f'<td class="num {_cls(r.get("ex_hs300_mean"))}">{_fmt_pct(r.get("ex_hs300_mean"))}</td>'
            f'<td class="num">{_fmt_pct(r.get("ex_hs300_win_rate"), 1)}</td>'
            f'<td class="num {_cls(r.get("ex_industry_mean"))}">{_fmt_pct(r.get("ex_industry_mean"))}</td>'
            f'<td class="num">{_fmt_pct(r.get("ex_industry_win_rate"), 1)}</td>'
            f'<td class="num muted">{_fmt_pct(r.get("mfe_mean"))}</td>'
            f'<td class="num muted">{_fmt_pct(r.get("mae_mean"))}</td>'
            f'<td class="num muted">{_fmt_pct(r.get("mdd_mean"))}</td>'
            "</tr>"
        )

    # ---------- 分层表 ----------
    STRAT_CN = {"label_slope": "60周线方向", "label_alignment": "均线排列",
                "label_regime": "市场环境", "label_mktcap": "市值风格"}
    strat_rows = []
    for s in stratified:
        strat_rows.append(
            "<tr>"
            f'<td class="t">{_esc(STRAT_CN.get(s.get("stratum"), s.get("stratum")))}</td>'
            f'<td class="t">{_esc(s.get("stratum_value"))}</td>'
            f'<td class="t">{_esc(TYPE_CN.get(s.get("event_type"), s.get("event_type")))}</td>'
            f'<td class="num">{s.get("horizon")}日</td>'
            f'<td class="num">{s.get("n_events", 0):,}</td>'
            f'<td class="num {_cls(s.get("mean"))}">{_fmt_pct(s.get("mean"))}</td>'
            f'<td class="num {_cls(s.get("median"))}">{_fmt_pct(s.get("median"))}</td>'
            f'<td class="num">{_fmt_pct(s.get("win_rate"), 1)}</td>'
            f'<td class="num {_cls(s.get("ex_hs300_mean"))}">{_fmt_pct(s.get("ex_hs300_mean"))}</td>'
            f'<td class="num {_cls(s.get("ex_industry_mean"))}">{_fmt_pct(s.get("ex_industry_mean"))}</td>'
            "</tr>"
        )

    # ---------- 集中度 ----------
    conc_rows = []
    for c in conc:
        conc_rows.append(
            "<tr>"
            f'<td class="t">{_esc(c.get("code"))}</td>'
            f'<td class="num">{c.get("events")}</td>'
            f'<td class="num">{_fmt_pct(c.get("share"), 2)}</td>'
            f'<td class="num muted">{_fmt_pct(c.get("cum_share"), 2)}</td>'
            "</tr>"
        )

    # ---------- 稳健性：bootstrap 明细 ----------
    METRIC_CN = {"ret": "绝对收益", "ex_hs300": "超额(沪深300)",
                 "ex_industry": "超额(行业)"}

    def _pval(p):
        if p is None:
            return "—"
        try:
            v = float(p)
        except (TypeError, ValueError):
            return "—"
        if v != v:
            return "—"
        return "<0.0001" if v < 0.0001 else f"{v:.4f}"

    boot_ret = [b for b in boot if b.get("metric") == "ret"]
    boot_rows = []
    for b in sorted(boot_ret, key=lambda x: (x.get("event_type"), x.get("horizon"))):
        de = b.get("design_effect")
        de_s = "—" if de is None else f"{float(de):.2f}×"
        flag = ' class="num warn-cell"' if (de is not None and float(de) > 1.3) else ' class="num"'
        boot_rows.append(
            "<tr>"
            f'<td class="t">{_esc(TYPE_CN.get(b.get("event_type"), b.get("event_type")))}</td>'
            f'<td class="num">{b.get("horizon")}日</td>'
            f'<td class="num">{b.get("n_events", 0):,}</td>'
            f'<td class="num">{b.get("n_stocks", 0):,}</td>'
            f'<td class="num {_cls(b.get("pooled_mean"))}">{_fmt_pct(b.get("pooled_mean"))}</td>'
            f'<td class="num muted">[{_fmt_pct(b.get("naive_ci_lo"),1)}, {_fmt_pct(b.get("naive_ci_hi"),1)}]</td>'
            f'<td class="num">[{_fmt_pct(b.get("ci_lo"),1)}, {_fmt_pct(b.get("ci_hi"),1)}]</td>'
            f'<td class="num">{_pval(b.get("p_cluster"))}</td>'
            f'<td{flag}>{de_s}</td>'
            "</tr>"
        )

    # ---------- 稳健性：去重口径对照 ----------
    dedup_map = {}
    for d in dedup_cmp:
        key = (d.get("event_type"), d.get("horizon"), d.get("caliber"))
        dedup_map[key] = d
    dedup_rows = []
    for d in sorted([x for x in dedup_cmp if x.get("caliber") == "all"],
                    key=lambda x: (x.get("event_type"), x.get("horizon"))):
        et, hz = d.get("event_type"), d.get("horizon")
        dd = dedup_map.get((et, hz, "dedup"), {})
        diff = None
        if d.get("mean") is not None and dd.get("mean") is not None:
            diff = float(dd["mean"]) - float(d["mean"])
        dedup_rows.append(
            "<tr>"
            f'<td class="t">{_esc(TYPE_CN.get(et, et))}</td>'
            f'<td class="num">{hz}日</td>'
            f'<td class="num">{d.get("n_events", 0):,}</td>'
            f'<td class="num {_cls(d.get("mean"))}">{_fmt_pct(d.get("mean"))}</td>'
            f'<td class="num">{_fmt_pct(d.get("win_rate"), 1)}</td>'
            f'<td class="num">{dd.get("n_events", 0):,}</td>'
            f'<td class="num {_cls(dd.get("mean"))}">{_fmt_pct(dd.get("mean"))}</td>'
            f'<td class="num">{_fmt_pct(dd.get("win_rate"), 1)}</td>'
            f'<td class="num {_cls(diff)}">{"—" if diff is None else f"{diff*100:+.2f}pp"}</td>'
            "</tr>"
        )

    # ---------- 图表数据 ----------
    chart_payload = json.dumps({
        "horizons": horizons,
        "overall": overall,
        "stratified": stratified,
        "types": list(TYPE_CN.keys()),
    }, ensure_ascii=False)

    n_strat = len(stratified)
    generated = meta.get("generated_at", "")
    runtime = meta.get("runtime_sec", "")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股 60周线触及事件研究报告 {version}</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"></script>
<style>
  :root {{
    --bg: #F2F2F7; --card: #FFFFFF; --line: #E5E5EA;
    --label: #1C1C1E; --label2: rgba(60,60,67,.62); --label3: rgba(60,60,67,.36);
    --blue: #007AFF; --red: #FF3B30; --green: #34C759; --orange: #FF9500;
    --purple: #AF52DE; --teal: #5AC8FA; --gray: #8E8E93;
    --shadow: 0 1px 2px rgba(0,0,0,.04), 0 8px 24px rgba(0,0,0,.05);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--label);
    font: 15px/1.65 -apple-system, BlinkMacSystemFont, "SF Pro SC", "SF Pro Text",
          "PingFang SC", "Helvetica Neue", "Microsoft YaHei", sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 1120px; margin: 0 auto; padding: 40px 24px 80px; }}
  header.hero {{ padding: 8px 4px 28px; }}
  .eyebrow {{ font-size: 13px; letter-spacing: .06em; color: var(--blue);
              font-weight: 600; text-transform: uppercase; }}
  h1 {{ font-size: 34px; line-height: 1.25; margin: 10px 0 12px;
        letter-spacing: -0.02em; font-weight: 700; }}
  .lede {{ color: var(--label2); font-size: 16px; max-width: 760px; }}
  .badges {{ margin-top: 18px; display: flex; flex-wrap: wrap; gap: 8px; }}
  .badge {{ background: var(--card); border: 1px solid var(--line);
            border-radius: 999px; padding: 5px 13px; font-size: 12.5px;
            color: var(--label2); box-shadow: var(--shadow); }}
  .kpis {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin: 26px 0 8px; }}
  .kpi {{ background: var(--card); border-radius: 18px; padding: 18px 20px;
          box-shadow: var(--shadow); border: 1px solid rgba(0,0,0,.02); }}
  .kpi.warn {{ border-color: rgba(255,149,0,.5); background: #FFFBF2; }}
  .kpi-label {{ font-size: 12.5px; color: var(--label2); font-weight: 500; }}
  .kpi-value {{ font-size: 26px; font-weight: 700; letter-spacing: -0.02em; margin: 6px 0 2px; }}
  .kpi-sub {{ font-size: 12px; color: var(--label3); }}
  section {{ margin-top: 34px; }}
  h2 {{ font-size: 21px; letter-spacing: -0.01em; margin: 0 0 6px; font-weight: 650; }}
  .sec-sub {{ color: var(--label2); font-size: 14px; margin-bottom: 16px; }}
  .card {{ background: var(--card); border-radius: 18px; box-shadow: var(--shadow);
           padding: 20px 22px; border: 1px solid rgba(0,0,0,.02); }}
  .card + .card {{ margin-top: 14px; }}
  .chart {{ width: 100%; height: 380px; }}
  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
  .tablewrap {{ overflow-x: auto; border-radius: 14px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; min-width: 720px; }}
  th, td {{ padding: 9px 11px; border-bottom: 1px solid var(--line); white-space: nowrap; }}
  th {{ background: #FAFAFC; font-weight: 600; color: var(--label2);
        text-align: right; position: sticky; top: 0; font-size: 12px; }}
  th:first-child, td.t {{ text-align: left; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.muted {{ color: var(--label3); }}
  tbody tr:hover {{ background: #FAFAFC; }}
  .up {{ color: var(--red); font-weight: 600; }}
  .down {{ color: var(--green); font-weight: 600; }}
  .warn-cell {{ color: #B25E00; font-weight: 600; }}
  .note {{ background: #FFFBF2; border: 1px solid rgba(255,149,0,.35);
           border-radius: 14px; padding: 14px 18px; font-size: 13.5px;
           color: #7A4A00; }}
  .note b {{ color: #6B3F00; }}
  .banner {{ margin: 18px 0 0; background: #FFF4E5;
             border: 1px solid rgba(255,149,0,.55); border-radius: 16px;
             padding: 16px 20px; font-size: 14px; color: #7A4A00;
             display: flex; gap: 12px; align-items: flex-start;
             box-shadow: var(--shadow); }}
  .banner b:first-child {{ color: #B25E00; white-space: nowrap;
                           background: rgba(255,149,0,.16);
                           padding: 3px 10px; border-radius: 999px; font-size: 12.5px; }}
  .banner span {{ color: #7A4A00; }}
  ul.lim {{ margin: 8px 0 0; padding-left: 20px; color: var(--label2); font-size: 14px; }}
  ul.lim li {{ margin: 6px 0; }}
  footer {{ margin-top: 48px; padding-top: 20px; border-top: 1px solid var(--line);
            color: var(--label3); font-size: 12.5px; display: flex;
            justify-content: space-between; flex-wrap: wrap; gap: 10px; }}
  code {{ background: #F2F2F7; padding: 1px 6px; border-radius: 6px; font-size: 12.5px; }}
  @media (max-width: 860px) {{
    .kpis {{ grid-template-columns: repeat(2, 1fr); }}
    .grid2 {{ grid-template-columns: 1fr; }}
    h1 {{ font-size: 26px; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <div class="eyebrow">Event Study · 事件研究</div>
    <h1>A股股价触及 60 周线后的表现</h1>
    <p class="lede">统计股价在上穿 60 周均线（类型A）与自上方回踩触及
      60 周均线（类型B）之后，20 / 40 / 60 / 120 / 250 个交易日的收益分布，
      并以绝对收益、沪深300、申万一级行业三重基准衡量超额表现。
      {lede_scope}</p>
    <div class="badges">
      <span class="badge">样本期 {_esc(meta.get('start','2010-01-01'))} 起</span>
      <span class="badge">MA60 ±2% 带宽</span>
      <span class="badge">后复权 · 比例复权已验证</span>
      <span class="badge">全A含退市（消除生存者偏差）</span>
      <span class="badge">主口径不去重</span>
      <span class="badge">版本 {_esc(version)}</span>
    </div>
  </header>

  <div class="kpis">{kpi_html}</div>
{banner}

  <section>
    <h2>一、核心结论</h2>
    <div class="sec-sub">两类事件在 250 个交易日（约一年）维度上的表现差异最明显</div>
    <div class="card">
      <div id="chart-curve" class="chart"></div>
    </div>
    <div class="card">
      <div class="grid2">
        <div id="chart-excess" class="chart" style="height:340px"></div>
        <div id="chart-win" class="chart" style="height:340px"></div>
      </div>
    </div>
  </section>

  <section>
    <h2>二、总览统计</h2>
    <div class="sec-sub">按 事件类型 × 观察窗口 汇总；超额收益以沪深300与申万一级行业为基准</div>
    <div class="card">
      <div class="tablewrap">
        <table>
          <thead><tr>{thead}</tr></thead>
          <tbody>{''.join(tbody)}</tbody>
        </table>
      </div>
    </div>
  </section>

  <section>
    <h2>三、分层表现</h2>
    <div class="sec-sub">四个维度：60周线方向 · 均线排列 · 市场环境 · 市值风格（共 {n_strat:,} 组）</div>
    <div class="card"><div id="chart-strat" class="chart" style="height:420px"></div></div>
    <div class="card">
      <div class="tablewrap">
        <table>
          <thead><tr>
            <th>分层维度</th><th>取值</th><th>事件类型</th><th>窗口</th><th>样本</th>
            <th>均值</th><th>中位数</th><th>胜率</th>
            <th>超额均值(沪深300)</th><th>超额均值(行业)</th>
          </tr></thead>
          <tbody>{''.join(strat_rows) if strat_rows else '<tr><td colspan="10" class="muted">暂无分层结果</td></tr>'}</tbody>
        </table>
      </div>
    </div>
  </section>

  <section>
    <h2>四、样本集中度诊断</h2>
    <div class="sec-sub">主口径不去重，同一标的的相邻事件窗口高度重叠，须据此判断结论稳健性</div>
    <div class="card">
      <div class="tablewrap">
        <table>
          <thead><tr><th>标的</th><th>事件数</th><th>占样本</th><th>累计占比</th></tr></thead>
          <tbody>{''.join(conc_rows) if conc_rows else '<tr><td colspan="4" class="muted">暂无数据</td></tr>'}</tbody>
        </table>
      </div>
    </div>
    <div class="note" style="margin-top:14px">
      <b>如何解读：</b>若 Top10 标的合计占比显著超过 20%，说明统计量主要由少数标的的
      反复触线贡献，名义显著性被高估。此时应同时参照「去重后事件数」与
      <code>dedup_kept</code> 标记做敏感性分析。
    </div>
  </section>

  <section>
    <h2>五、稳健性检验</h2>
    <div class="sec-sub">主口径不去重会让同一标的的相邻事件窗口重叠，样本不独立。
      以下两项检验用于回答「结论是不是被重叠样本撑起来的」</div>

    <div class="note" style="margin-bottom:14px">
      <b>为什么要做：</b>同一只股票可在数月内反复触线，其前瞻窗口覆盖同一段行情，
      相当于一份证据被数了多次。若直接按事件数计算置信区间，区间会被系统性低估。
      <b>膨胀倍数</b> = 聚类区间宽度 ÷ 朴素区间宽度，显著大于 1 即说明重叠确实抬高了名义显著性。
    </div>

    <div class="card">
      <div id="chart-ci" class="chart" style="height:400px"></div>
    </div>

    <div class="card">
      <div class="sec-sub" style="margin:0 0 10px">
        检验一 · 按股票聚类的 bootstrap 置信区间（绝对收益；以股票为重抽样单位，2000 次）
      </div>
      <div class="tablewrap">
        <table>
          <thead><tr>
            <th>事件类型</th><th>窗口</th><th>事件数</th><th>股票数</th><th>点估计</th>
            <th>朴素区间（把事件当独立）</th><th>聚类区间（正确）</th><th>p 值</th><th>膨胀倍数</th>
          </tr></thead>
          <tbody>{''.join(boot_rows) if boot_rows else '<tr><td colspan="9" class="muted">暂无稳健性结果</td></tr>'}</tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <div class="sec-sub" style="margin:0 0 10px">
        检验二 · 去重口径敏感性对照（同一标的 26 周冷却期只保留首次触线）
      </div>
      <div class="tablewrap">
        <table>
          <thead><tr>
            <th>事件类型</th><th>窗口</th>
            <th>主口径样本</th><th>主口径均值</th><th>主口径胜率</th>
            <th>去重样本</th><th>去重均值</th><th>去重胜率</th><th>均值差</th>
          </tr></thead>
          <tbody>{''.join(dedup_rows) if dedup_rows else '<tr><td colspan="9" class="muted">暂无对照结果</td></tr>'}</tbody>
        </table>
      </div>
      <div class="note" style="margin-top:14px">
        <b>如何解读：</b>若两口径的均值与胜率接近（均值差在几个百分点内），
        说明结论不依赖去重方式，稳健；若去重后均值大幅回落，说明主口径的收益
        被重叠样本放大，须以去重口径为准。
      </div>
    </div>
  </section>

  <section>
    <h2>六、口径与已知局限</h2>
    <div class="card">
      <ul class="lim">
        <li><b>复权口径：</b>后复权（<code>adjustflag=1</code>）。已用「复权因子须为阶梯函数」
          的判据验证为比例复权，跳变日精确对应真实除权日。</li>
        <li><b>周线口径：</b>周收盘取该周最后一个交易日收盘（后复权下唯一正确取法）；
          周标记日为该周最后一个实际交易日。</li>
        <li><b>右删失：</b>前瞻窗口超出数据末端的样本收益记为空值并置 <code>ok{{N}}=False</code>，
          已单独计数，未静默丢弃。</li>
        <li><b>市值口径：</b>由 成交额 ÷ (换手率/100) 反推事件当日流通市值，
          并按当日全市场横截面分位分档，无前视偏差。</li>
        <li><b>样本重叠：</b>主口径不去重，<code>ret</code> 系列存在重叠窗口，
          常规 t 检验的名义显著性不成立，请以集中度诊断为准。</li>
        <li><b>行业基准：</b>为样本内自建等权组合；申万一级成分股为当前时点快照，
          非历史成分。退市股无申万归属者回退使用证监会分类（<code>industry_src</code> 标记）。</li>
        <li><b>数据结构：</b>本研究为历史统计描述，不构成任何投资建议。</li>
      </ul>
    </div>
  </section>

  <footer>
    <span>数据来源：{_esc(meta.get('data_source','baostock'))} · 样本 {_esc(meta.get('scope',''))}</span>
    <span>生成于 {_esc(generated)} · 计算耗时 {_esc(runtime)} 秒</span>
  </footer>
</div>

<script>
const D = {chart_payload};
const C = {{ blue:'#007AFF', red:'#FF3B30', green:'#34C759', orange:'#FF9500',
             purple:'#AF52DE', teal:'#5AC8FA', gray:'#8E8E93' }};
const HZ = D.horizons;
const TYPE_TAG = {{ 'A_上穿突破':'类型A 上穿突破', 'B_上方回踩':'类型B 上方回踩' }};
const TYPE_COLOR = {{ 'A_上穿突破': C.red, 'B_上方回踩': C.blue }};

function pick(type, field) {{
  return HZ.map(h => {{
    const r = D.overall.find(x => x.event_type === type && x.horizon === h);
    return r ? r[field] : null;
  }});
}}
const baseOpt = {{
  textStyle: {{ fontFamily:'-apple-system,"PingFang SC",sans-serif', color:'#3C3C43' }},
  grid: {{ left: 56, right: 24, top: 46, bottom: 40 }},
  tooltip: {{ trigger:'axis', valueFormatter: v => v==null?'—':(v*100).toFixed(2)+'%' }},
  legend: {{ top: 6, itemGap: 18, textStyle:{{ color:'#3C3C43', fontSize:12 }} }},
  xAxis: {{ type:'category', data: HZ.map(h=>h+'日'),
            axisLine:{{ lineStyle:{{ color:'#E5E5EA' }} }}, axisLabel:{{ color:'#8E8E93' }} }},
  yAxis: {{ type:'value', axisLabel: {{ formatter: v => (v*100).toFixed(0)+'%', color:'#8E8E93' }},
            splitLine: {{ lineStyle: {{ color:'#F2F2F7' }} }} }},
}};

const chartCurve = echarts.init(document.getElementById('chart-curve'));
chartCurve.setOption(Object.assign({{}}, baseOpt, {{
  title: {{ text:'平均收益随观察窗口的变化', left: 0, top: 0, textStyle:{{ fontSize:14, color:'#1C1C1E' }} }},
  legend: {{ top: 0, right: 0 }},
  grid: {{ left: 56, right: 24, top: 40, bottom: 40 }},
  series: [
    {{ name:'类型A 均值', type:'line', smooth:true, symbolSize:7, lineStyle:{{ width:3, color:C.red }}, itemStyle:{{ color:C.red }}, data: pick('A_上穿突破','mean') }},
    {{ name:'类型A 中位数', type:'line', smooth:true, symbolSize:6, lineStyle:{{ width:2, type:'dashed', color:C.red }}, itemStyle:{{ color:C.red }}, data: pick('A_上穿突破','median') }},
    {{ name:'类型B 均值', type:'line', smooth:true, symbolSize:7, lineStyle:{{ width:3, color:C.blue }}, itemStyle:{{ color:C.blue }}, data: pick('B_上方回踩','mean') }},
    {{ name:'类型B 中位数', type:'line', smooth:true, symbolSize:6, lineStyle:{{ width:2, type:'dashed', color:C.blue }}, itemStyle:{{ color:C.blue }}, data: pick('B_上方回踩','median') }},
  ]
}}));

const chartEx = echarts.init(document.getElementById('chart-excess'));
chartEx.setOption(Object.assign({{}}, baseOpt, {{
  title: {{ text:'超额收益（相对沪深300 / 行业）', left: 0, top: 0, textStyle:{{ fontSize:14, color:'#1C1C1E' }} }},
  legend: {{ top: 0, right: 0 }},
  grid: {{ left: 56, right: 16, top: 40, bottom: 40 }},
  series: [
    {{ name:'A 超额(沪深300)', type:'bar', itemStyle:{{ color:C.red, borderRadius:[4,4,0,0] }}, data: pick('A_上穿突破','ex_hs300_mean') }},
    {{ name:'A 超额(行业)', type:'bar', itemStyle:{{ color:C.orange, borderRadius:[4,4,0,0] }}, data: pick('A_上穿突破','ex_industry_mean') }},
    {{ name:'B 超额(沪深300)', type:'bar', itemStyle:{{ color:C.blue, borderRadius:[4,4,0,0] }}, data: pick('B_上方回踩','ex_hs300_mean') }},
    {{ name:'B 超额(行业)', type:'bar', itemStyle:{{ color:C.teal, borderRadius:[4,4,0,0] }}, data: pick('B_上方回踩','ex_industry_mean') }},
  ]
}}));

const chartWin = echarts.init(document.getElementById('chart-win'));
chartWin.setOption(Object.assign({{}}, baseOpt, {{
  title: {{ text:'胜率（绝对 / 超额）', left: 0, top: 0, textStyle:{{ fontSize:14, color:'#1C1C1E' }} }},
  legend: {{ top: 0, right: 0 }},
  grid: {{ left: 46, right: 16, top: 40, bottom: 40 }},
  series: [
    {{ name:'A 胜率', type:'line', smooth:true, symbolSize:6, lineStyle:{{ width:2, color:C.red }}, itemStyle:{{ color:C.red }}, data: pick('A_上穿突破','win_rate') }},
    {{ name:'A 超额胜率', type:'line', smooth:true, symbolSize:6, lineStyle:{{ width:2, type:'dashed', color:C.red }}, itemStyle:{{ color:C.red }}, data: pick('A_上穿突破','ex_hs300_win_rate') }},
    {{ name:'B 胜率', type:'line', smooth:true, symbolSize:6, lineStyle:{{ width:2, color:C.blue }}, itemStyle:{{ color:C.blue }}, data: pick('B_上方回踩','win_rate') }},
    {{ name:'B 超额胜率', type:'line', smooth:true, symbolSize:6, lineStyle:{{ width:2, type:'dashed', color:C.blue }}, itemStyle:{{ color:C.blue }}, data: pick('B_上方回踩','ex_hs300_win_rate') }},
  ]
}}));

// 分层：取 120 日窗口，按维度分组对比
const STRAT_CN = {{ label_slope:'60周线方向', label_alignment:'均线排列', label_regime:'市场环境', label_mktcap:'市值风格' }};
const chartStrat = echarts.init(document.getElementById('chart-strat'));
const dimsAvail = Object.keys(STRAT_CN).filter(k => D.stratified.some(s => s.stratum === k));
const selectDim = dimsAvail.includes('label_regime') ? 'label_regime' : (dimsAvail[0] || null);
function stratSeries(d) {{
  if (!d) return [];
  const vs = [...new Set(D.stratified.filter(s => s.stratum === d).map(s => s.stratum_value))];
  return D.types.map(t => ({{
    name: TYPE_TAG[t], type: 'bar', barMaxWidth: 34,
    itemStyle: {{ color: TYPE_COLOR[t], borderRadius: [4, 4, 0, 0] }},
    data: vs.map(v => {{
      const r = D.stratified.find(s => s.stratum === d && s.stratum_value === v
                                   && s.event_type === t && s.horizon === 120);
      return r ? r.ex_hs300_mean : null;
    }}),
  }}));
}}
const sv = selectDim
  ? [...new Set(D.stratified.filter(s => s.stratum === selectDim).map(s => s.stratum_value))]
  : [];
chartStrat.setOption(Object.assign({{}}, baseOpt, {{
  title: {{ text:'分层超额收益对比（120日 · 相对沪深300）', left: 0, top: 0, textStyle:{{ fontSize:14, color:'#1C1C1E' }} }},
  legend: {{ top: 0, right: 0 }},
  grid: {{ left: 56, right: 24, top: 40, bottom: 48 }},
  xAxis: {{ type:'category', data: sv, axisLabel:{{ color:'#8E8E93', interval:0, rotate: sv.length>6?20:0 }} }},
  tooltip: {{ trigger:'axis', valueFormatter: v => v==null?'—':(v*100).toFixed(2)+'%' }},
  series: stratSeries(selectDim),
}}));

// 稳健性：森林图（朴素区间 vs 聚类区间）
const BOOT = (D.bootstrap || []).filter(b => b.metric === 'ret');
const ciItems = [];
D.types.forEach(t => {{
  HZ.forEach(h => {{
    const b = BOOT.find(x => x.event_type === t && x.horizon === h);
    if (b) ciItems.push({{ label: TYPE_TAG[t] + ' · ' + h + '日', t: t, b: b }});
  }});
}});
const ciChart = echarts.init(document.getElementById('chart-ci'));
if (ciItems.length) {{
  const catNames = ciItems.map(x => x.label);
  const data = ciItems.map((x, i) => [i, x.b.naive_ci_lo, x.b.naive_ci_hi,
                                      x.b.ci_lo, x.b.ci_hi, x.b.pooled_mean]);
  ciChart.setOption({{
    textStyle: {{ fontFamily:'-apple-system,"PingFang SC",sans-serif' }},
    title: {{ text:'置信区间对比：聚类校正后区间明显变宽', left: 0, top: 0,
             textStyle: {{ fontSize:14, color:'#1C1C1E' }} }},
    legend: {{ top: 0, right: 0, data:['朴素区间','聚类区间','点估计'],
              textStyle: {{ fontSize:12, color:'#3C3C43' }} }},
    grid: {{ left: 150, right: 60, top: 46, bottom: 34 }},
    tooltip: {{ trigger:'item', formatter: p => {{
        const x = ciItems[p.dataIndex];
        return x.label + '<br/>点估计 ' + (x.b.pooled_mean*100).toFixed(2) + '%'
             + '<br/>朴素 [' + (x.b.naive_ci_lo*100).toFixed(1) + '%, ' + (x.b.naive_ci_hi*100).toFixed(1) + '%]'
             + '<br/>聚类 [' + (x.b.ci_lo*100).toFixed(1) + '%, ' + (x.b.ci_hi*100).toFixed(1) + '%]'
             + '<br/>膨胀 ' + (x.b.design_effect==null?'—':x.b.design_effect.toFixed(2)+'倍');
    }} }},
    xAxis: {{ type:'value', axisLabel: {{ formatter: v => (v*100).toFixed(0)+'%',
              color:'#8E8E93' }},
             splitLine: {{ lineStyle: {{ color:'#F2F2F7' }} }} }},
    yAxis: {{ type:'category', data: catNames, inverse: true,
             axisLine: {{ lineStyle: {{ color:'#E5E5EA' }} }},
             axisLabel: {{ color:'#3C3C43', fontSize: 11.5 }} }},
    series: [{{
      name:'区间', type:'custom',
      renderItem: function(params, api) {{
        const i = api.value(0);
        const item = ciItems[i];
        const col = TYPE_COLOR[item.t];
        const y  = api.coord([0, i])[1];
        const xn0 = api.coord([api.value(1), i])[0];
        const xn1 = api.coord([api.value(2), i])[0];
        const xc0 = api.coord([api.value(3), i])[0];
        const xc1 = api.coord([api.value(4), i])[0];
        const xm  = api.coord([api.value(5), i])[0];
        const cap = 5;
        return {{
          type:'group',
          children: [
            {{ type:'line', shape:{{ x1:xn0, y1:y, x2:xn1, y2:y }},
               style:{{ stroke:'#B4B2A9', lineWidth:4, opacity:.55, lineCap:'round' }} }},
            {{ type:'line', shape:{{ x1:xc0, y1:y, x2:xc1, y2:y }},
               style:{{ stroke:col, lineWidth:4, lineCap:'round' }} }},
            {{ type:'line', shape:{{ x1:xm, y1:y-cap, x2:xm, y2:y+cap }},
               style:{{ stroke:'#1C1C1E', lineWidth:2 }} }}
          ]
        }};
      }},
      encode: {{ x: [1,2,3,4,5], y: 0 }},
      data: data, z: 10
    }}],
    graphic: [{{
      type:'text', left: 150, bottom: 8,
      style: {{ text:'灰条=把事件当独立样本的朴素区间 ｜ 彩条=按股票聚类校正后的区间 ｜ 竖线=点估计',
               fontSize: 11, fill:'#8E8E93' }}
    }}]
  }});
}} else {{
  ciChart.setOption({{ title: {{ text:'暂无稳健性结果', left:'center', top:'middle',
    textStyle: {{ fontSize:14, color:'#8E8E93', fontWeight:'normal' }} }} }});
}}

window.addEventListener('resize', () => {{
  chartCurve.resize(); chartEx.resize(); chartWin.resize(); chartStrat.resize();
  ciChart.resize();
}});
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="v1.1.1")
    ap.add_argument("--stage", choices=["validation", "final"], default="final",
                    help="validation=流程验证样本（会显著标注），final=正式全样本")
    args = ap.parse_args()

    rp = OUT / "study_result.json"
    if not rp.exists():
        print("缺少 output/study_result.json，先运行 src/run_study.py")
        return 1
    with open(rp, encoding="utf-8") as f:
        result = json.load(f)

    html_text = build_html(result, args.version, stage=args.stage)
    tag = "_验证版" if args.stage == "validation" else ""
    out = OUT / f"60周线触及事件研究_报告{tag}_{args.version}.html"
    out.write_text(html_text, encoding="utf-8")
    print(f"已生成 {out}")
    print(f"  大小 {out.stat().st_size/1024:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
