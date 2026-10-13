"""备选数据源第二轮：腾讯接口复权判据 + baostock 限流恢复探测。"""
from __future__ import annotations

import threading
import time

import akshare as ak
import pandas as pd

OUT = "output/alt_check.txt"
lines: list[str] = []


def log(s: str):
    print(s, flush=True)
    lines.append(s)


def judge(name: str, raw: pd.DataFrame, hfq: pd.DataFrame, rc="close", hc="close"):
    try:
        a = raw[["date", rc]].rename(columns={rc: "raw"})
        b = hfq[["date", hc]].rename(columns={hc: "hfq"})
        a["date"] = pd.to_datetime(a["date"])
        b["date"] = pd.to_datetime(b["date"])
        m = a.merge(b, on="date").dropna()
        m = m[m["raw"] > 0]
        if len(m) < 50:
            log(f"  {name}: 重叠样本不足({len(m)})")
            return
        ratio = (m["hfq"] / m["raw"]).round(8)
        uniq = ratio.nunique()
        chg = ratio.diff().fillna(0) != 0
        runs = ratio.ne(ratio.shift()).cumsum()
        max_const = int(m.groupby(runs).size().max())
        verdict = "阶梯函数（正确）" if (uniq <= 60 and max_const >= 20) else "高频漂移（异常）"
        log(f"  {name}: 重叠 {len(m)}  取值 {uniq}  变化日 {int(chg.sum())} "
            f"（{chg.mean():.1%}）  最长恒定 {max_const} 天  → {verdict}")
    except Exception as exc:  # noqa: BLE001
        log(f"  {name} 判据失败: {type(exc).__name__}: {exc}")


def test_tx(symbol="sh600000"):
    log(f"[腾讯] {symbol}")
    try:
        t = time.time()
        raw = ak.stock_zh_a_hist_tx(symbol=symbol, start_date="20100101",
                                    end_date="20261013", adjust="")
        log(f"  不复权 {len(raw)} 条 {time.time()-t:.1f}s  列={list(raw.columns)}")
        t = time.time()
        hfq = ak.stock_zh_a_hist_tx(symbol=symbol, start_date="20100101",
                                    end_date="20261013", adjust="hfq")
        log(f"  后复权 {len(hfq)} 条 {time.time()-t:.1f}s  列={list(hfq.columns)}")
        judge("腾讯 hfq", raw, hfq)
    except Exception as exc:  # noqa: BLE001
        log(f"  失败: {type(exc).__name__}: {exc}")


def test_baostock(timeout=30):
    log(f"[baostock] 限流恢复探测（{timeout}s 超时）")
    box = {}

    def work():
        try:
            import baostock as bs
            bs.login()
            rs = bs.query_history_k_data_plus(
                "sh.600000", "date,close", start_date="2025-01-01",
                end_date="2026-10-13", frequency="d", adjustflag="1")
            n = 0
            while rs.next():
                rs.get_row_data()      # 必须取值推进游标，否则 next() 永久为 True
                n += 1
            box["n"] = n
            bs.logout()
        except Exception as exc:  # noqa: BLE001
            box["err"] = f"{type(exc).__name__}: {exc}"

    th = threading.Thread(target=work, daemon=True)
    t = time.time()
    th.start()
    th.join(timeout)
    if th.is_alive():
        log(f"  ❌ 仍被限流：查询 {timeout}s 未返回")
    else:
        log(f"  ✅ 已恢复：{box.get('n')} 条，用时 {time.time()-t:.1f}s  {box.get('err','')}")


if __name__ == "__main__":
    log("=" * 60)
    log("备选数据源核验（第二轮）")
    log("=" * 60)
    test_baostock(30)
    log("-" * 60)
    test_tx("sh600000")
    log("-" * 60)
    test_tx("sz000001")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
