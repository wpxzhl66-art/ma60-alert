"""复权实现正确性的决定性判据。

原理：
    比例复权的复权因子 = 后复权价 / 不复权价。
    该因子**只在除权除息日跳变，其余交易日必须恒定**，因此是一个阶梯函数。
    若因子每天都变（连续漂移），说明该源使用的不是标准比例复权，
    其收益序列被系统性扭曲。

对每个数据源度量三个指标：
    - 唯一因子值个数：正确的源 ≈ 除权次数（茅台约每年1次）；错误的源 ≈ 交易日数
    - 因子保持不变的连续天数最大值：正确的源以「月」计；错误的源为 1
    - 因子单日最大跳变：正确的源等于该次分红率（约1~3%）；错误的源是每日微小漂移

同时检验：因子变化的日子是否与真实除权日吻合。
"""
from __future__ import annotations

import sys

import baostock as bs
import pandas as pd

CODE_BS = "sh.600519"
CODE_AK = "600519"
START, END = "2018-01-01", "2026-10-13"


def bs_fetch(adjustflag: str) -> pd.DataFrame:
    """baostock 单次查询约 4000 条上限，按年分段取数。"""
    frames = []
    for y in range(2018, 2027):
        s = f"{y}-01-01"
        e = min(pd.Timestamp(f"{y}-12-31"), pd.Timestamp(END)).date().isoformat()
        if pd.Timestamp(s) > pd.Timestamp(END):
            break
        rs = bs.query_history_k_data_plus(CODE_BS, "date,close", start_date=s,
                                         end_date=e, frequency="d",
                                         adjustflag=adjustflag)
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if rows:
            frames.append(pd.DataFrame(rows, columns=["date", "close"]))
    df = pd.concat(frames, ignore_index=True).drop_duplicates("date")
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.sort_values("date").reset_index(drop=True)


def ak_fetch(adjust: str) -> pd.DataFrame:
    import akshare as ak
    df = ak.stock_zh_a_hist(symbol=CODE_AK, period="daily",
                            start_date=pd.Timestamp(START).strftime("%Y%m%d"),
                            end_date=pd.Timestamp(END).strftime("%Y%m%d"),
                            adjust=adjust)
    df = df.rename(columns={"日期": "date", "收盘": "close"})
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df[["date", "close"]].sort_values("date").reset_index(drop=True)


def diagnose(name: str, hfq: pd.DataFrame, raw: pd.DataFrame) -> dict:
    m = hfq.merge(raw, on="date", suffixes=("_h", "_r")).dropna()
    m = m[m["close_r"] > 0].reset_index(drop=True)
    m["factor"] = m["close_h"] / m["close_r"]

    # 因子四舍五入到 1e-6 以消除浮点噪声
    f = m["factor"].round(6)
    uniq = f.nunique()
    changed = (f.diff().abs() > 1e-6).sum()
    jumps = f.diff().abs().dropna()

    # 最长恒定连续段
    streak, best = 1, 1
    vals = f.to_numpy()
    for i in range(1, len(vals)):
        streak = streak + 1 if abs(vals[i] - vals[i - 1]) <= 1e-6 else 1
        best = max(best, streak)

    print(f"\n【{name}】 交易日 {len(m)}  区间 {m['date'].iloc[0].date()} ~ {m['date'].iloc[-1].date()}")
    print(f"  唯一因子值个数        : {uniq}")
    print(f"  因子变化日数          : {changed}   （占 {changed / len(m):.1%}）")
    print(f"  因子最长恒定连续天数  : {best} 天")
    print(f"  因子单日最大跳变      : {jumps.max():.4%}")
    print(f"  因子整体区间          : {f.min():.4f} ~ {f.max():.4f}")

    # 判据只用两个稳健指标：
    #   唯一因子值个数 —— 正确实现 ≈ 除权次数（每年1~2次），错误实现 ≈ 交易日数
    #   最长恒定连续天数 —— 正确实现以「月」计，错误实现为 1~2 天
    # 「单日最大跳变」不作为否决项：它等于该次分红的除权比例，各年差异大
    # （如特别分红可达 17%），仅作展示。
    verdict = (uniq <= 60) and (best >= 20)
    print(f"  → 判定: {'阶梯函数（标准比例复权）' if verdict else '非阶梯（复权实现异常）'}")

    if changed:
        chg_days = m.loc[f.diff().abs() > 1e-6, "date"].dt.strftime("%Y-%m-%d").tolist()
        print(f"  因子发生跳变的日期（应为真实除权日）: {chg_days[:20]}")
    return {"name": name, "verdict": verdict, "unique": uniq, "best_streak": best,
            "max_jump": jumps.max(), "n": len(m)}


def main():
    lg = bs.login()
    if lg.error_code != "0":
        print("baostock 登录失败")
        return 1

    print("=" * 70)
    print("复权因子阶梯性判据测试（贵州茅台，2018-01-01 ~ 2026-10-13）")
    print("=" * 70)

    res = []
    res.append(diagnose("baostock", bs_fetch("1"), bs_fetch("3")))
    bs.logout()
    try:
        res.append(diagnose("akshare (东方财富)", ak_fetch("hfq"), ak_fetch("")))
    except Exception as exc:  # noqa: BLE001
        print(f"\nakshare 诊断失败: {exc}")

    print("\n" + "=" * 70)
    for r in res:
        print(f"  {r['name']:<22} 唯一因子 {r['unique']:>5}  "
              f"最长恒定 {r['best_streak']:>4}天  最大跳变 {r['max_jump']:.4%}  "
              f"→ {'正确' if r['verdict'] else '异常'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
