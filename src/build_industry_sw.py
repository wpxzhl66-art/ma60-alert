"""构建「股票 → 申万一级行业」映射表。

背景：baostock 只提供**证监会**行业分类，而研究方案要求的行业基准是
**申万一级**。本脚本通过 akshare 的申万指数成分接口补齐该口径。

做法：
    1. 取申万一级行业清单（31 个，`sw_index_first_info`）
    2. 逐行业取成分股（`index_component_sw`，31 次请求）
    3. 输出 data/industry_sw.csv.gz，列 = [code, industry_sw, industry_src]

口径说明与局限（须在报告中披露）：
    - 申万成分股是**当前时点**快照，非历史成分。行业归属的时序变动极少，
      但不为零；严格研究应使用历史行业归属。
    - 退市股不在当前申万成分名单中（覆盖率为 0）。为此保留 baostock 的
      证监会分类作为**回退**，并写入 industry_src 列以便追溯，
      回退标签统一加前缀「证监会:」避免与申万标签混淆。

用法：
    python src/build_industry_sw.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "industry_sw.csv.gz"


def _to_code6_prefix(code6: str) -> str:
    """6 位代码 → baostock 风格 sh./sz./bj. 前缀。"""
    c = str(code6).strip().zfill(6)
    if c.startswith(("60", "68", "9")):
        return f"sh.{c}"
    if c.startswith(("00", "30", "20")):
        return f"sz.{c}"
    if c.startswith(("43", "83", "87", "88")):
        return f"bj.{c}"
    return c


def build_sw_map() -> pd.DataFrame:
    import akshare as ak

    info = ak.sw_index_first_info()
    print(f"申万一级行业 {len(info)} 个")

    rows = []
    for i, r in info.iterrows():
        code = str(r["行业代码"]).replace(".SI", "")
        name = str(r["行业名称"])
        expect = r.get("成份个数")
        try:
            cons = ak.index_component_sw(symbol=code)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i+1}/{len(info)}] {name} 取成分失败: {type(exc).__name__}")
            continue
        n = len(cons)
        flag = "" if (pd.isna(expect) or n > 0) else "  ⚠空"
        print(f"  [{i+1}/{len(info)}] {name}({code}) 成分 {n} 只{flag}")
        for c in cons["证券代码"].astype(str):
            rows.append({"code": _to_code6_prefix(c), "industry_sw": name})
        time.sleep(0.3)

    sw = pd.DataFrame(rows).drop_duplicates("code")
    print(f"申万映射合计 {len(sw)} 只（去重后）")
    return sw


def attach_fallback(sw: pd.DataFrame) -> pd.DataFrame:
    """用 baostock 证监会分类补齐申万未覆盖的标的（主要为退市股）。"""
    p = DATA / "industry.csv.gz"
    if not p.exists():
        print("无 industry.csv.gz，跳过回退合并")
        sw["industry_src"] = "申万一级"
        return sw

    cs = pd.read_csv(p)
    col = next((c for c in cs.columns if c in ("industry", "行业")), None)
    if col is None:
        sw["industry_src"] = "申万一级"
        return sw

    cs = cs[["code", col]].rename(columns={col: "csrc"})
    cs["csrc"] = cs["csrc"].map(
        lambda x: None if pd.isna(x) else f"证监会:{x}")
    merged = sw.merge(cs, on="code", how="outer")
    merged["industry"] = merged["industry_sw"].fillna(merged["csrc"])
    merged["industry_src"] = [
        "申万一级" if isinstance(a, str) and a else
        ("证监会(回退)" if isinstance(b, str) and b else "未知")
        for a, b in zip(merged["industry_sw"], merged["csrc"])
    ]
    n_sw = int((merged["industry_src"] == "申万一级").sum())
    n_fb = int((merged["industry_src"] == "证监会(回退)").sum())
    n_un = int((merged["industry_src"] == "未知").sum())
    print(f"行业标签来源：申万一级 {n_sw} / 证监会回退 {n_fb} / 未知 {n_un}")
    return merged[["code", "industry", "industry_sw", "industry_src"]]


def main():
    sw = build_sw_map()
    out = attach_fallback(sw)
    out.to_csv(OUT, index=False, compression="gzip")
    print(f"\n已写入 {OUT}  共 {len(out)} 条")
    print("行业分布（前10）：")
    print(out["industry"].value_counts().head(10).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
