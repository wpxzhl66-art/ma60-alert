"""BaostockSource 端到端验证。

验证内容：
    1. 后复权日线取数与字段完整性
    2. 事件时点流通市值的反推（解决市值分层的前视偏差）
    3. 停牌与 ST 标记
    4. 全市场清单含退市股
    5. 指数日线（市场基准）
    6. 与 westock 的原始价格交叉验证（不复权口径应一致）
    7. 取数耗时（决定全A股批量可行性）
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from datasource import BaostockSource  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def main():
    src = BaostockSource()
    with src:
        # ---------- 1. 后复权日线 ----------
        print("[1] 后复权日线与字段")
        t = time.time()
        df = src.fetch_daily("sh600519", "2010-01-01", "2026-10-13")
        dt = time.time() - t
        print(f"  {len(df)} 条  {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}  耗时 {dt:.1f}s")
        print(f"  字段: {list(df.columns)}")
        check("取到 2010 年以来完整数据", len(df) > 3900, f"{len(df)} 条")
        check("价格全部为正", bool((df["close"] > 0).all()))
        check("含换手率字段 turn", "turn" in df.columns)
        check("含 ST 标记 isST", "isST" in df.columns)
        check("含停牌标记 tradestatus", "tradestatus" in df.columns)
        check("含估值字段 peTTM", "peTTM" in df.columns)

        # ---------- 2. 流通市值反推 ----------
        print("\n[2] 事件时点流通市值反推（amount/turn）")
        mv = df["float_mv"].dropna()
        latest = df["float_mv"].iloc[-1]
        print(f"  最新流通市值 = {latest / 1e8:.0f} 亿元")
        check("流通市值字段已生成", "float_mv" in df.columns)
        check("市值数量级合理（100亿~10万亿）", 1e10 < latest < 1e13, f"{latest / 1e8:.0f}亿")
        check("市值序列覆盖率 > 80%", len(mv) / len(df) > 0.8, f"{len(mv) / len(df):.1%}")
        # 历史市值应随时间变化（而非静态快照，证明无前视偏差）
        early = df["float_mv"].iloc[100]
        check("2010年市值 ≠ 当前市值（证明为时点值）",
              abs(early / latest - 1) > 0.1,
              f"2010={early / 1e8:.0f}亿 vs 当前={latest / 1e8:.0f}亿")

        # ---------- 3. 指数基准 ----------
        print("\n[3] 指数日线（沪深300）")
        idx = src.index_daily("sh.000300", "2010-01-01", "2026-10-13")
        print(f"  {len(idx)} 条  {idx['date'].iloc[0].date()} ~ {idx['date'].iloc[-1].date()}")
        check("指数数据可用", len(idx) > 3800, f"{len(idx)} 条")
        check("指数价格为正", bool((idx["close"] > 0).all()))

        # ---------- 4. 全市场清单（含退市） ----------
        print("\n[4] 全市场清单")
        uni = src.list_universe(include_delisted=True)
        n_listed = int((uni["status"] == "1").sum()) if "status" in uni else -1
        n_delisted = int((uni["status"] == "0").sum()) if "status" in uni else -1
        print(f"  总计 {len(uni)} 只：上市 {n_listed}，退市 {n_delisted}")
        check("股票总数 > 5000", len(uni) > 5000, f"{len(uni)}")
        check("含退市股票（生存者偏差可消除）", n_delisted > 100, f"退市 {n_delisted} 只")

        # ---------- 5. 行业分类 ----------
        print("\n[5] 行业分类")
        try:
            ind = src.fetch_industry()
            print(f"  {len(ind)} 条，字段 {list(ind.columns)}")
            check("行业分类可用", len(ind) > 3000)
        except Exception as exc:  # noqa: BLE001
            print(f"  失败: {exc}")
            check("行业分类可用", False)

        # ---------- 6. 不复权交叉验证 ----------
        print("\n[6] 不复权口径与 westock 原始价交叉验证")
        raw = BaostockSource(adjustflag="3")
        with raw:
            rdf = raw.fetch_daily("sh600519", "2026-09-01", "2026-10-13")
        tail = rdf.tail(3)[["date", "close", "pctChg"]]
        print(tail.to_string(index=False))
        d1013 = rdf[rdf["date"] == pd.Timestamp("2026-10-13")]
        if len(d1013):
            pct = float(d1013["pctChg"].iloc[0])
            # westock 不复权当日报 -1.0178571%
            check("2026-10-13 涨跌幅与 westock 一致（-1.0179%）",
                  abs(pct - (-1.0179)) < 0.01, f"baostock={pct:.4f}%")

    print("\n" + "=" * 62)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
