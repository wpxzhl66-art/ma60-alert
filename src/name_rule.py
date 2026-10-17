"""个股名称风险判定 —— **全项目唯一真源**（回测 / 实盘 / 推送 / 看板共用）。

为什么单独成文件
----------------
此前「ST 怎么判」散落在三处，各自演进就会漂移（本项目已经为此吃过亏）：

* ``src/pattern/core.py`` 的 ``RISK_NAME_PAT = re.compile(r"ST|退")``
  （形态回测与实盘扫描用）
* ``dashboard/core.js`` 的 ``isRiskName``（看板展示层用，手写正则）
* 推送链路此前**完全没有**风险股过滤

现在统一收敛到这里：

* Python 侧（``pattern`` / ``alert``）一律 ``from name_rule import ...``；
* ``pattern.core.is_risk_name`` 保留函数名，**改为转发**到这里（调用方不用改）；
* 前端 ``dashboard/core.js`` 的 ``isRiskName`` 是这里的手写镜像，
  改正则时必须同步（已加注释互指）。

判定规则（2026-10-17 定稿）
--------------------------
1. **ST / *ST —— 只看名称前缀**（用户明确要求「以 ST、*ST 开头」）。
   前缀里允许出现这些常见变体，一律先剥掉再判：

   * ``*``（半角）与 ``＊``（全角）—— ``*ST``、``＊ST``
   * 半角 / 全角空格（``\\u3000``）—— ``* ST``、``　ST``、`` *ST``
   * ``·`` ``・`` ``_`` ``-`` ``－`` —— 部分数据源的排版残留
   * 前置 ``S`` —— 老版「未股改 + 风险警示」写法 ``SST`` / ``S*ST``

   ⚠️ 只看前缀、不看任意位置：避免误伤名称中间恰好含「ST」的正常股票。
   判定后还要求 ``ST`` 后面不是英文字母，防止把英文简称误判为风险股
   （A 股暂无此类名称，属于防御性写法）。

2. **退市整理 —— 名称以「退」结尾，或含「退市」**。
   ⚠️ 不能只判 ST：进入退市整理期的股票会改名成「XX退」（如「元道退」），
   名称里根本没有 ST，只判 ST 会漏掉。

3. ``is_risk_name()`` = 上面两条取「或」，即**推送名单与形态扫描的剔除口径**。

用法：
    from name_rule import is_risk_name, drop_risk_rows
    trig, n = drop_risk_rows(trig)          # 返回 (过滤后的表, 剔除只数)
"""
from __future__ import annotations

import re

# 前缀噪声：* 号（半角/全角）、空格（半角/全角）、· ・ _ - －
_LEAD = r"[\s\u3000*＊·・_\-－]{0,4}"

# ST 前缀：允许前置噪声 + 可选的老「未股改」标记 S（SST / S*ST）
ST_PREFIX_PAT = re.compile(rf"^{_LEAD}s?{_LEAD}st(?![A-Za-z])", re.IGNORECASE)

# 退市整理：「XX退」结尾，或名称含「退市」
DELIST_PAT = re.compile(r"退\s*$|退市", re.IGNORECASE)


def is_st_name(name: str) -> bool:
    """名称是否以 ST / *ST（含空格、全角符号、S*ST 等前缀变体）开头。"""
    return bool(ST_PREFIX_PAT.match(str(name or "")))


def is_delisting_name(name: str) -> bool:
    """名称是否为退市整理期股票（「XX退」）或明确标注退市。"""
    return bool(DELIST_PAT.search(str(name or "")))


def is_risk_name(name: str) -> bool:
    """是否应剔除：ST 类风险警示股 或 退市整理股。"""
    s = str(name or "")
    return is_st_name(s) or is_delisting_name(s)


def risk_tag(name: str) -> str:
    """返回剔除原因（'' = 不剔除，'ST' / '退市'），便于日志与排障。"""
    if is_st_name(name):
        return "ST"
    if is_delisting_name(name):
        return "退市"
    return ""


def drop_risk_rows(df, name_col: str = "name"):
    """从名单里剔除风险股，返回 ``(过滤后的表, 剔除只数)``。

    ``df`` 为 pandas DataFrame（这里不 import pandas，保持本模块零依赖）。
    缺列 / 空表时原样返回，绝不抛异常——调用方是推送链路，不能被过滤逻辑打断。
    """
    if df is None or len(df) == 0 or name_col not in getattr(df, "columns", []):
        return df, 0
    mask = df[name_col].map(is_risk_name)
    n = int(mask.sum())
    return (df[~mask] if n else df), n


if __name__ == "__main__":
    cases = ["*ST景谷", "* ST海航", "　ST中天", "S*ST前锋", "SST华新", "ST泰格",
             "＊ST数源", "元道退", "长江退市", "紫竹高科", "贵州茅台", "三一重工",
             "国农科技", "ST", "", None]
    for c in cases:
        print(f"{str(c):12s} → {risk_tag(c) or '-':4s} "
              f"(st={int(is_st_name(c))}, delist={int(is_delisting_name(c))})")
