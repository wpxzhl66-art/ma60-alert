"""导出 Excel 明细工作簿。

读取 output/ 下的研究结果，生成多工作表 xlsx：
    1. 说明        口径、字段字典、已知局限
    2. 总览        事件类型 × 窗口 的汇总统计
    3. 分层统计    按各分层维度的汇总
    4. 事件明细    每笔事件一行（研究主数据）
    5. 集中度      事件在各标的上的分布（不去重口径必看）
    6. 标的清单    参与研究的标的与数据条数

设计要点：
    - xlsx 单表上限 1,048,576 行；明细超限时自动按行数切片为多表。
    - 中文列名，附单位与说明，便于直接阅读。
    - 不依赖 xlsxwriter，仅用 openpyxl。

用法：
    python src/export_excel.py [--version v1.1.1]
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"
DATA = ROOT / "data"

XLSX_MAX_ROWS = 1_048_576
BENCH_LABEL = {"hs300": "沪深300", "industry": "申万一级行业"}

FIELD_DOC = [
    ("week_idx", "事件所在周序号", "自样本起点(2010-01-04所在周)起计，0 起"),
    ("event_date", "事件周标记日", "该周最后一个实际交易日"),
    ("event_type", "事件类型", "A_上穿突破 = 收盘由下向上穿越60周线；B_上方回踩 = 此前在带宽上沿之上、本周收盘落入 60周线±2% 带内"),
    ("code", "标的代码", "baostock 格式，如 sh.600519"),
    ("entry_price", "事件周收盘价", "后复权口径，作为收益计算基准价"),
    ("entry_ma", "60周均线值", "事件周当周的 MA60（后复权）"),
    ("dev_from_ma", "收盘相对均线偏离", "entry_price / entry_ma - 1"),
    ("industry", "所属行业", "申万一级；退市股回退为证监会分类（见 industry_src）"),
    ("industry_src", "行业标签来源", "申万一级 / 证监会(回退) / 未知"),
    ("label_slope", "60周线方向", "事件周 MA60 相对 13 周前的涨跌幅分级：向上>1%、向下<-1%、其余走平"),
    ("label_alignment", "均线排列", "MA5>MA10>MA20>MA60 为「多头排列」，否则「非多头排列」"),
    ("label_regime", "市场环境", "由沪深300周线与MA40推导：牛市/熊市/震荡"),
    ("label_mktcap", "市值风格", "事件当日流通市值在全市场的横截面分位：<=1/3 小盘、>=2/3 大盘、其余中盘"),
    ("mv_pct", "市值横截面分位", "事件当日该股流通市值在全市场的排名百分位"),
    ("float_mv", "事件时点流通市值(元)", "由 成交额/(换手率/100) 反推，无前视偏差"),
    ("dedup_kept", "去重后是否保留", "主口径不去重（全部计入）；本列标记 26 周冷却期去重下是否保留，供敏感性分析"),
    ("t0_pos", "事件日在交易日历中的位置", "0 起，用于追溯后续 N 日的对齐"),
    ("ret{N}", f"N 交易日绝对收益", "close(t0+N)/close(t0)-1，后复权"),
    ("ex_hs300{N}", f"N 交易日相对沪深300超额", "ret{N} - 沪深300同期收益"),
    ("ex_industry{N}", f"N 交易日相对行业超额", "ret{N} - 申万一级等权行业指数同期收益"),
    ("mfe{N}", f"N 日内最大浮盈", "窗口内最高收盘价/入场价-1"),
    ("mae{N}", f"N 日内最大浮亏", "窗口内最低收盘价/入场价-1"),
    ("mdd{N}", f"N 日内最大回撤", "窗口内收盘价相对历史峰值的最大回撤"),
    ("ok{N}", f"N 日窗口数据是否完整", "False = 右删失（窗口超出数据末端），收益记为空值"),
]


def _read_csv(name: str) -> pd.DataFrame | None:
    p = OUT / name
    if not p.exists():
        return None
    return pd.read_csv(p)


def _autosize(ws, df: pd.DataFrame, max_w: int = 44):
    for i, col in enumerate(df.columns, 1):
        width = max(len(str(col)) * 2, 10)
        sample = df[col].head(200)
        for v in sample:
            width = max(width, len(str(v)) + 2)
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(width, max_w)


def _write(ws, df: pd.DataFrame, freeze: str = "A2"):
    from openpyxl.styles import Alignment, Font, PatternFill

    ws.append(list(df.columns))
    head_fill = PatternFill("solid", fgColor="F2F2F7")
    head_font = Font(bold=True, size=10)
    for c in ws[1]:
        c.fill = head_fill
        c.font = head_font
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for row in df.itertuples(index=False):
        ws.append([None if (isinstance(v, float) and not np.isfinite(v)) else v
                   for v in row])
    ws.freeze_panes = freeze
    _autosize(ws, df)


def build_notes(meta: dict) -> pd.DataFrame:
    rows = [
        ("研究报告", "A股股价触及60周线后表现的事件研究"),
        ("生成时间", str(meta.get("generated_at", ""))),
        ("数据来源", str(meta.get("data_source", "baostock 后复权"))),
        ("样本范围", str(meta.get("scope", ""))),
        ("样本期", f"{meta.get('start','2010-01-01')} ~ 数据末端"),
        ("事件总数", f"{meta.get('n_events','')} 笔（涉及 {meta.get('n_stocks','')} 只标的）"),
        ("去重后事件数", f"{meta.get('n_events_dedup','')} 笔（26 周冷却期，仅作敏感性对照）"),
        ("主口径去重策略", "不去重，全部计入（附 dedup_kept 标记）"),
        ("", ""),
        ("【事件定义】", ""),
        ("类型A 上穿突破", "前一周收盘 < 前一周MA60 且 本周收盘 >= 本周MA60"),
        ("类型B 上方回踩", "前一周收盘 > 前一周带宽上沿(MA60×1.02)，且本周收盘落入 [MA60×0.98, MA60×1.02]"),
        ("", ""),
        ("【计算口径】", ""),
        ("复权方式", "后复权(adjustflag=1)。经复权因子阶梯函数判据验证为比例复权，可信"),
        ("周线合成", "周收盘 = 该周最后一个交易日收盘（后复权下的正确取法）"),
        ("观察窗口", "事件周收盘后第 20 / 40 / 60 / 120 / 250 个交易日"),
        ("右删失处理", "窗口超出数据末端的，收益记为空值并置 ok{N}=False，不静默丢弃"),
        ("市值口径", "事件当日流通市值 = 成交额/(换手率/100)，横截面分位分档，无前视偏差"),
        ("", ""),
        ("【已知局限】", ""),
        ("样本重叠", "主口径不去重，同一标的相邻事件的前瞻窗口高度重叠，名义显著性被夸大；请以「集中度」表判断结果是否受少数标的支配"),
        ("行业基准", "为样本内自建等权组合；申万一级成分股为当前时点快照，非历史成分"),
        ("退市股行业", "退市股不在当前申万成分名单中，回退使用证监会分类（industry_src 标记）"),
        ("", ""),
        ("【字段字典】", ""),
    ]
    for item in FIELD_DOC:
        rows.append((item[0], " ".join(str(x) for x in item[1:])))
    return pd.DataFrame(rows, columns=["项目 / 字段", "说明"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="v1.1.1")
    args = ap.parse_args()

    rp = OUT / "study_result.json"
    if not rp.exists():
        print("缺少 output/study_result.json，先运行 src/run_study.py")
        return 1
    with open(rp, encoding="utf-8") as f:
        result = json.load(f)
    meta = result.get("meta", {})

    detail = _read_csv("events_detail.csv.gz")
    overall = _read_csv("summary_overall.csv")
    strat = _read_csv("summary_stratified.csv")
    conc = _read_csv("concentration.csv")

    if detail is None:
        print("缺少 events_detail.csv.gz")
        return 1

    # 明细中文列名
    ren = {
        "week_idx": "周序号", "event_date": "事件周标记日", "event_type": "事件类型",
        "code": "标的代码", "entry_price": "入场价(后复权)", "entry_ma": "60周均线值",
        "dev_from_ma": "相对均线偏离", "industry": "所属行业",
        "industry_src": "行业标签来源", "label_slope": "60周线方向",
        "label_alignment": "均线排列", "label_regime": "市场环境",
        "label_mktcap": "市值风格", "mv_pct": "市值横截面分位",
        "float_mv": "事件时点流通市值(元)", "dedup_kept": "去重后保留",
        "t0_pos": "日历位置",
    }
    for N in (20, 40, 60, 120, 250):
        ren[f"ret{N}"] = f"{N}日收益"
        ren[f"ex_hs300{N}"] = f"{N}日超额(沪深300)"
        ren[f"ex_industry{N}"] = f"{N}日超额(行业)"
        ren[f"mfe{N}"] = f"{N}日最大浮盈"
        ren[f"mae{N}"] = f"{N}日最大浮亏"
        ren[f"mdd{N}"] = f"{N}日最大回撤"
        ren[f"ok{N}"] = f"{N}日窗口完整"
    order = [c for c in ren if c in detail.columns]
    detail_cn = detail[order].rename(columns=ren)

    xlsx = OUT / f"60周线触及事件研究_明细_{args.version}.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as writer:
        if "Sheet" in writer.book.sheetnames:      # 去掉 openpyxl 默认空表
            del writer.book["Sheet"]
        notes = build_notes(meta)
        _write(writer.book.create_sheet("说明"), notes, freeze="A2")

        if overall is not None and len(overall):
            _write(writer.book.create_sheet("总览"), overall)
        if strat is not None and len(strat):
            _write(writer.book.create_sheet("分层统计"), strat)
        if conc is not None and len(conc):
            _write(writer.book.create_sheet("集中度"), conc)

        # 标的清单
        files = sorted(glob.glob(str(DATA / "daily" / "*.csv.gz")))
        lst = pd.DataFrame({
            "标的代码": [Path(f).name.replace(".csv.gz", "").replace("_", ".", 1)
                         for f in files]
        })
        if len(lst):
            _write(writer.book.create_sheet("标的清单"), lst)

        # 明细（超限自动切片）
        n = len(detail_cn)
        if n <= XLSX_MAX_ROWS - 1:
            _write(writer.book.create_sheet("事件明细"), detail_cn)
        else:
            per = XLSX_MAX_ROWS - 1
            for k in range(0, n, per):
                _write(writer.book.create_sheet(f"事件明细{k//per+1}"),
                       detail_cn.iloc[k:k + per])

    size_mb = xlsx.stat().st_size / 1e6
    print(f"已生成 {xlsx}")
    print(f"  明细 {len(detail_cn)} 行  文件 {size_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
