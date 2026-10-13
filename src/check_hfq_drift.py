"""决定性测试：在长周期上比较不复权(bfq)与后复权(hfq)的累计收益差异。

核心逻辑：
  若 hfq 是 bfq 的乘性复权（正确实现），则两者累计收益之差 = 期间累计分红收益率（约每年1~3%，且必须为正）。
  若差异极大或随时间为负/无规律，说明 hfq 序列不可用。

分段取数（规避单次请求截断），每段校验条数并重试。
"""
import subprocess
import sys

WS = r"C:\Users\Administrator\.local\bin\westock.exe"
CODE = "sh600519"


def fetch_seg(code, start, end, fq, limit=800, retries=4):
    """取单段日线。fq=None 表示不复权。"""
    for _ in range(retries):
        cmd = [WS, "kline", code, "--period", "day", "--start", start,
               "--limit", str(limit)]
        if fq:
            cmd += ["--fq", fq]
        if end:
            cmd += ["--end", end]
        out = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        rows = {}
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue
            p = [x.strip() for x in line.strip("|").split("|")]
            if len(p) < 9 or p[0] == "date" or p[0].startswith("---"):
                continue
            try:
                rows[p[0]] = float(p[2])
            except ValueError:
                continue
        if rows:
            return rows
    return {}


def fetch_range(code, start_year, end_year, fq):
    """按半年分段取数并合并。"""
    merged = {}
    for y in range(start_year, end_year + 1):
        for a, b in ((1, 6), (7, 12)):
            s = f"{y}-{a:02d}-01"
            e = f"{y}-{b:02d}-{30 if b == 6 else 31}"
            if (y, b) >= (2026, 10):
                e = "2026-10-13"
            seg = fetch_seg(code, s, e, fq)
            merged.update(seg)
    return merged


def main():
    bfq = fetch_range(CODE, 2018, 2026, None)
    hfq = fetch_range(CODE, 2018, 2026, "hfq")
    print(f"不复权 取到 {len(bfq)} 个交易日")
    print(f"后复权 取到 {len(hfq)} 个交易日")

    common = sorted(set(bfq) & set(hfq))
    if len(common) < 100:
        print("可比对样本不足，无法判定")
        return 1

    print(f"可比对 {len(common)} 个交易日，区间 {common[0]} ~ {common[-1]}\n")

    cb0, cb1 = bfq[common[0]], bfq[common[-1]]
    ch0, ch1 = hfq[common[0]], hfq[common[-1]]
    rb = cb1 / cb0 - 1
    rh = ch1 / ch0 - 1

    print(f"不复权 累计收益: {cb0:.2f} → {cb1:.2f}  = {rb * 100:+.2f}%")
    print(f"后复权 累计收益: {ch0:.2f} → {ch1:.2f}  = {rh * 100:+.2f}%")
    print(f"累计收益之差   : {(rh - rb) * 100:+.2f} 个百分点")
    print("（正确复权下，此差应 ≈ 期间累计分红收益率，为正且约每年 1~3%）\n")

    ratios = [(d, hfq[d] / bfq[d]) for d in common]
    vals = [r for _, r in ratios]
    drift = (max(vals) - min(vals)) / min(vals) * 100
    print(f"hfq/bfq 比值: {min(vals):.4f} ~ {max(vals):.4f}  波动 {drift:.3f}%")

    # 比值随时间的净漂移：早期均值 vs 后期均值
    n = len(ratios) // 4
    early = sum(v for _, v in ratios[:n]) / n
    late = sum(v for _, v in ratios[-n:]) / n
    print(f"早期均值 {early:.4f} → 后期均值 {late:.4f}"
          f"  净漂移 {(late / early - 1) * 100:+.3f}%")
    print("\n判定标准：比值应为常数（波动 <0.05%）。")

    # 抽样展示
    print("\n抽样（每 300 个交易日取一个）:")
    print(f"{'date':<12}{'bfq':>10}{'hfq':>11}{'ratio':>10}")
    for d, r in ratios[::300]:
        print(f"{d:<12}{bfq[d]:>10.2f}{hfq[d]:>11.2f}{r:>10.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
