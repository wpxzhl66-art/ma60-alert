"""复权一致性核验：对比同一区间内 bfq / hfq / qfq 的比值是否稳定。
用途：确认数据源复权实现是否可靠，决定事件研究采用哪种复权口径。
"""
import subprocess
import sys

WS = r"C:\Users\Administrator\.local\bin\westock.exe"
CODE = "sh600519"
START = "2026-09-01"
END = "2026-10-13"


def fetch(code, period, start, end, fq, limit=5000, retries=3):
    """取 K 线，解析 markdown 表格。带重试（接口存在瞬时空返回）。

    fq=None 表示不复权（CLI 中省略 --fq 参数，传 --fq bfq 会返回空）。
    """
    for attempt in range(retries):
        cmd = [WS, "kline", code, "--period", period, "--start", start,
               "--limit", str(limit)]
        if fq:
            cmd += ["--fq", fq]
        if end:
            cmd += ["--end", end]
        out = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        rows = []
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue
            parts = [p.strip() for p in line.strip("|").split("|")]
            if len(parts) < 9 or parts[0] == "date" or parts[0].startswith("---"):
                continue
            rows.append(parts)
        if rows:
            return rows
    return []


def main():
    data = {}
    for label, fq in (("bfq", None), ("hfq", "hfq"), ("qfq", "qfq")):
        data[label] = {r[0]: r for r in fetch(CODE, "day", START, END, fq)}
        print(f"{label}: {len(data[label])} 条")

    dates = sorted(set(data["bfq"]) & set(data["hfq"]) & set(data["qfq"]), reverse=True)
    print(f"\n可比对日期数: {len(dates)}\n")

    hdr = f"{'date':<12}{'close_bfq':>11}{'close_qfq':>11}{'close_hfq':>11}" \
          f"{'hfq/bfq':>10}{'qfq-bfq':>10}{'high_rat':>10}{'low_rat':>10}"
    print(hdr)
    print("-" * len(hdr))

    ratios = []
    for d in dates:
        b, h, q = data["bfq"][d], data["hfq"][d], data["qfq"][d]
        try:
            cb, ch, cq = float(b[2]), float(h[2]), float(q[2])
            ob, oh = float(b[1]), float(h[1])
            hb, hh = float(b[3]), float(h[3])
            lb, lh = float(b[4]), float(h[4])
            cr = ch / cb
            ratios.append(cr)
            print(f"{d:<12}{cb:>11.2f}{cq:>11.2f}{ch:>11.2f}"
                  f"{cr:>10.4f}{cq - cb:>10.2f}{hh / hb:>10.4f}{lh / lb:>10.4f}")
        except (ValueError, ZeroDivisionError) as e:
            print(f"{d:<12} 解析异常: {e}")

    if ratios:
        spread = (max(ratios) - min(ratios)) / min(ratios) * 100
        print(f"\n【hfq/bfq 比值区间】{min(ratios):.4f} ~ {max(ratios):.4f}"
              f"  波动幅度 {spread:.3f}%")
        print("判定：波动 <0.05% 视为复权因子稳定；>0.05% 说明复权序列内部不一致。")


if __name__ == "__main__":
    sys.exit(main())
