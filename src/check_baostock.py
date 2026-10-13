"""baostock 数据源能力验证。

目的：检验开源数据源能否解决 westock 的两个致命缺陷——
    ① 复权实现矛盾（前复权为加法复权、历史价为负）
    ② 退市股票不可得（生存者偏差不可消除）

同时验证长历史深度与关键字段可用性。
"""
from __future__ import annotations

import io
import sys

import baostock as bs
import pandas as pd


def quiet_login():
    """baostock 会向 stdout 打印登录信息，这里静默（Windows 下日志走 stderr）。"""
    return bs.login()


def fetch(code: str, start: str, end: str, adjustflag: str = "1") -> pd.DataFrame:
    rs = bs.query_history_k_data_plus(
        code,
        "date,code,open,high,low,close,volume,amount,turn,pctChg",
        start_date=start, end_date=end,
        frequency="d", adjustflag=adjustflag,
    )
    if rs.error_code != "0":
        raise RuntimeError(f"{code} 查询失败: {rs.error_code} {rs.error_msg}")
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    df = pd.DataFrame(rows, columns=rs.fields)
    for c in ("open", "high", "low", "close", "volume", "amount"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def main():
    lg = quiet_login()
    if lg.error_code != "0":
        print(f"登录失败: {lg.error_code} {lg.error_msg}")
        return 1
    print("登录成功\n")

    ok = []

    # ---------------- 1. 长历史深度 + 后复权 ----------------
    print("[1] 长历史与后复权（贵州茅台 sh.600519）")
    try:
        df = fetch("sh.600519", "2001-01-01", "2026-10-13", adjustflag="1")
        print(f"  条数 {len(df)}   区间 {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}")
        print(f"  首行: date={df['date'].iloc[0]} close={df['close'].iloc[0]}")
        print(f"  末行: date={df['date'].iloc[-1]} close={df['close'].iloc[-1]}")
        pos = (df["close"] > 0).all()
        print(f"  全部价格为正: {pos}")
        ok.append(("长历史可回溯至2001", df["date"].iloc[0].startswith("2001")))
        ok.append(("后复权价格恒为正", bool(pos)))
        ok.append(("条数覆盖完整（>4000）", len(df) > 4000))
    except Exception as exc:  # noqa: BLE001
        print(f"  失败: {exc}")

    # ---------------- 2. 三种复权口径的内在一致性 ----------------
    print("\n[2] 复权口径一致性（2018-01-02 ~ 2026-10-13）")
    try:
        hfq = fetch("sh.600519", "2018-01-01", "2026-10-13", "1")
        qfq = fetch("sh.600519", "2018-01-01", "2026-10-13", "2")
        raw = fetch("sh.600519", "2018-01-01", "2026-10-13", "3")
        m = hfq.merge(qfq, on="date", suffixes=("_h", "_q")).merge(
            raw[["date", "close"]].rename(columns={"close": "close_r"}), on="date")
        m["ratio_h"] = m["close_h"] / m["close_r"]
        m["ratio_q"] = m["close_q"] / m["close_r"]
        rh = (m["ratio_h"].max() - m["ratio_h"].min()) / m["ratio_h"].min() * 100
        rq = (m["ratio_q"].max() - m["ratio_q"].min()) / m["ratio_q"].min() * 100
        print(f"  后复权/不复权 比值波动: {rh:.4f}%   （区间 {m['ratio_h'].min():.4f}~{m['ratio_h'].max():.4f}）")
        print(f"  前复权/不复权 比值波动: {rq:.4f}%   （区间 {m['ratio_q'].min():.4f}~{m['ratio_q'].max():.4f}）")
        print(f"  后复权累计收益: {hfq['close'].iloc[-1] / hfq['close'].iloc[0] - 1:+.2%}")
        print(f"  不复权累计收益: {raw['close'].iloc[-1] / raw['close'].iloc[0] - 1:+.2%}")
        print(f"  全序列价格为正: 后复权={bool((hfq['close']>0).all())} 前复权={bool((qfq['close']>0).all())}")
        ok.append(("前复权无负价格", bool((qfq["close"] > 0).all())))
        ok.append(("后复权累计收益 ≥ 不复权（复权逻辑自洽）",
                   hfq["close"].iloc[-1] / hfq["close"].iloc[0] >=
                   raw["close"].iloc[-1] / raw["close"].iloc[0]))
    except Exception as exc:  # noqa: BLE001
        print(f"  失败: {exc}")

    # ---------------- 3. 退市股票覆盖 ----------------
    print("\n[3] 退市股票覆盖（决定生存者偏差能否消除）")
    for code, name in (("sh.600001", "邯郸钢铁(2009退市)"),
                       ("sz.000003", "PT金田A(2002退市)"),
                       ("sh.600002", "齐鲁石化(2006退市)")):
        try:
            d = fetch(code, "1990-01-01", "2026-10-13", "1")
            if len(d):
                print(f"  {code} {name}: {len(d)} 条  {d['date'].iloc[0]} ~ {d['date'].iloc[-1]}")
                ok.append((f"{name} 可取到历史数据", True))
            else:
                print(f"  {code} {name}: 空")
                ok.append((f"{name} 可取到历史数据", False))
        except Exception as exc:  # noqa: BLE001
            print(f"  {code} {name}: 失败 {exc}")
            ok.append((f"{name} 可取到历史数据", False))

    # ---------------- 4. 全市场股票清单 ----------------
    print("\n[4] 全市场股票清单")
    try:
        rs = bs.query_all_stock(day="2026-10-13")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        a = pd.DataFrame(rows, columns=rs.fields)
        print(f"  当日证券数 {len(a)}（含指数）")
        stocks = a[a["code"].str.match(r"^(sh\.6|sz\.0|sz\.3|sh\.68)")]
        print(f"  其中股票类约 {len(stocks)} 只")
        print(f"  字段: {list(a.columns)}")
        print(a.head(3).to_string(index=False))
        ok.append(("可取全市场清单", len(a) > 1000))
    except Exception as exc:  # noqa: BLE001
        print(f"  失败: {exc}")

    # ---------------- 5. 股本与行业（分层所需） ----------------
    print("\n[5] 市值与行业字段（分层所需）")
    try:
        rs = bs.query_stock_basic(code="sh.600519")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        print(f"  query_stock_basic 字段: {rs.fields}")
        if rows:
            print(f"  贵州茅台: {dict(zip(rs.fields, rows[0]))}")
    except Exception as exc:  # noqa: BLE001
        print(f"  失败: {exc}")

    bs.logout()

    print("\n" + "=" * 62)
    passed = sum(1 for _, v in ok if v)
    print(f"通过 {passed}/{len(ok)} 项")
    for k, v in ok:
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
