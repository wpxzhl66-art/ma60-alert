"""受控测速：字段数 / 并发数 对单只全历史取数耗时的影响。

注意两点（均为此前踩过的坑）：
  1. baostock 使用**单个全局 socket**，非线程安全 → 并发必须用**多进程**，
     用线程会出现请求交错、超时甚至数据错乱。
  2. 遍历结果集必须 `while rs.next(): rs.get_row_data()`；只调 next() 不取值
     会永久返回 True（无限空转）。
"""
from __future__ import annotations

import socket
import sys
import time
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

OUT = Path("output/bench.txt")
lines: list[str] = []

FIELDS_FULL = ("date,code,open,high,low,close,preclose,volume,amount,"
               "turn,tradestatus,pctChg,isST,peTTM,pbMRQ")
FIELDS_MIN = "date,close,turn,amount"

CODES = ["sh.600000", "sz.000001", "sh.601318", "sz.000651",
         "sh.600036", "sz.002415", "sh.601888", "sz.300750",
         "sh.600519", "sz.000858", "sh.601166", "sz.002594"]

_FIELDS = FIELDS_FULL


def _init(fields: str):
    global _FIELDS
    _FIELDS = fields
    import datasource
    datasource.patch_baostock_io(180.0)
    socket.setdefaulttimeout(180.0)
    import baostock as bs
    bs.login()


def _one(code: str):
    import baostock as bs
    t = time.time()
    rs = bs.query_history_k_data_plus(
        code, _FIELDS, start_date="2010-01-01", end_date="2026-10-13",
        frequency="d", adjustflag="1")
    n = 0
    while rs.next():
        rs.get_row_data()
        n += 1
    return (time.time() - t, n)


def log(s: str):
    print(s, flush=True)
    lines.append(s)


def bench(workers: int, fields: str, tag: str):
    t = time.time()
    with Pool(workers, initializer=_init, initargs=(fields,)) as pool:
        res = pool.map(_one, CODES)
    el = time.time() - t
    avg = sum(r[0] for r in res) / len(res)
    wall = el / len(CODES)
    log(f"  {tag}: 并发={workers}  总耗时 {el:.1f}s  "
        f"单只服务端均值 {avg:.1f}s  墙钟 {wall:.2f}s/只  "
        f"条数={sorted(r[1] for r in res)}")
    return wall


if __name__ == "__main__":
    log("=" * 74)
    log(f"受控测速（每档 {len(CODES)} 只，均为完整历史 2010-至今）")
    log("=" * 74)
    log("[A] 全字段 15 列")
    a1 = bench(1, FIELDS_FULL, "串行")
    a8 = bench(8, FIELDS_FULL, "8并发")
    log("[B] 精简 4 列 (date,close,turn,amount)")
    b1 = bench(1, FIELDS_MIN, "串行")
    b8 = bench(8, FIELDS_MIN, "8并发")
    b16 = bench(16, FIELDS_MIN, "16并发")
    b32 = bench(32, FIELDS_MIN, "32并发")
    log("-" * 74)
    log(f"串行单只：全字段 {a1*1:.2f}s vs 精简 {b1*1:.2f}s（字段数影响）")
    log(f"墙钟/只：全字段8并发 {a8:.2f}s | 精简 8:{b8:.2f}s  16:{b16:.2f}s  32:{b32:.2f}s")
    best = min(b8, b16, b32)
    log(f"全A股 5460 只 预估：{5460*best/3600:.1f} 小时（按最佳档 {best:.2f}s/只）")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
