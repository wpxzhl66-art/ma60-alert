"""双源交叉校验：baostock × akshare。

两个源相互独立（baostock 自有数据中心；akshare 抓取东方财富接口），
因此二者一致才能证明数据可信。校验维度：
    1. 同一日收盘价一致性（后复权）
    2. 区间累计收益一致性
    3. 日收益一致性
    4. 覆盖深度对比
    5. 单次取数耗时（决定全A股批量可行性）
"""
from __future__ import annotations

import sys
import time

import baostock as bs
import pandas as pd

CODE_BS = "sh.600519"
CODE_AK = "600519"


def baostock_daily(start: str, end: str, adjustflag: str = "1") -> pd.DataFrame:
    rs = bs.query_history_k_data_plus(
        CODE_BS, "date,close", start_date=start, end_date=end,
        frequency="d", adjustflag=adjustflag)
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    df = pd.DataFrame(rows, columns=rs.fields)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    return df


def akshare_daily(start: str, end: str, adjust: str = "hfq") -> pd.DataFrame:
    """akshare 的日期参数要求 YYYYMMDD（带连字符会静默返回异常结果）。"""
    import akshare as ak
    s = pd.Timestamp(start).strftime("%Y%m%d")
    e = pd.Timestamp(end).strftime("%Y%m%d")
    df = ak.stock_zh_a_hist(symbol=CODE_AK, period="daily",
                            start_date=s, end_date=e, adjust=adjust)
    df = df.rename(columns={"日期": "date", "收盘": "close"})
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df[["date", "close"]]


def main():
    start, end = "2010-01-01", "2026-10-13"
    result = []

    # ---- baostock ----
    lg = bs.login()
    if lg.error_code != "0":
        print("baostock 登录失败")
        return 1
    t = time.time()
    b = baostock_daily(start, end, "1")
    t_bs = time.time() - t
    print(f"[baostock] 后复权 {len(b)} 条  {b['date'].iloc[0].date()} ~ {b['date'].iloc[-1].date()}  耗时 {t_bs:.1f}s")

    # ---- akshare ----
    t = time.time()
    a = akshare_daily(start, end, "hfq")
    t_ak = time.time() - t
    print(f"[akshare]  后复权 {len(a)} 条  {a['date'].iloc[0].date()} ~ {a['date'].iloc[-1].date()}  耗时 {t_ak:.1f}s")
    bs.logout()

    # ---- 对比 ----
    m = b.merge(a, on="date", suffixes=("_bs", "_ak"))
    print(f"\n可比对交易日 {len(m)} 天")
    if len(m) < 100:
        print("可比对样本不足")
        return 1

    m["diff_ratio"] = m["close_ak"] / m["close_bs"]
    dev = (m["diff_ratio"].max() - m["diff_ratio"].min()) / m["diff_ratio"].min() * 100

    rb = b["close"].iloc[-1] / b["close"].iloc[0] - 1
    ra = a["close"].iloc[-1] / a["close"].iloc[0] - 1

    m["r_bs"] = m["close_bs"].pct_change()
    m["r_ak"] = m["close_ak"].pct_change()
    d = (m["r_bs"] - m["r_ak"]).dropna()

    print(f"\n{'校验项':<28}{'结果':>18}{'判定'}")
    print("-" * 62)
    print(f"{'可比对交易日数':<28}{len(m):>18}")
    print(f"{'收盘价比值波动':<28}{dev:>17.4f}%{'  PASS' if dev < 0.05 else '  FAIL'}")
    result.append(("收盘价比值恒定（<0.05%）", dev < 0.05))
    print(f"{'baostock 累计收益':<28}{rb:>17.2%}")
    print(f"{'akshare  累计收益':<28}{ra:>17.2%}")
    gap = abs(rb - ra) * 100
    print(f"{'两源累计收益差':<28}{gap:>17.3f}pp{'  PASS' if gap < 0.5 else '  FAIL'}")
    result.append(("两源累计收益一致（<0.5pp）", gap < 0.5))
    print(f"{'日收益最大绝对偏差':<28}{d.abs().max():>17.6f}{'  PASS' if d.abs().max() < 0.002 else '  FAIL'}")
    result.append(("日收益一致（偏差<0.2%）", d.abs().max() < 0.002))
    print(f"\n单只股票单次取数耗时: baostock {t_bs:.1f}s | akshare {t_ak:.1f}s")
    print(f"全A股约5400只串行预估: baostock {5400 * t_bs / 3600:.1f}h | akshare {5400 * t_ak / 3600:.1f}h")
    print(f"8进程并行预估:        baostock {5400 * t_bs / 3600 / 8:.1f}h | akshare {5400 * t_ak / 3600 / 8:.1f}h")

    print("\n抽样（每 300 天）:")
    print(f"{'date':<12}{'baostock':>12}{'akshare':>12}{'比值':>10}")
    for _, r in m.iloc[::300].iterrows():
        print(f"{r['date'].date()!s:<12}{r['close_bs']:>12.2f}{r['close_ak']:>12.2f}{r['diff_ratio']:>10.4f}")

    print("\n" + "=" * 62)
    print(f"通过 {sum(1 for _, v in result if v)}/{len(result)} 项")
    for k, v in result:
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    return 0 if all(v for _, v in result) else 1


if __name__ == "__main__":
    sys.exit(main())
