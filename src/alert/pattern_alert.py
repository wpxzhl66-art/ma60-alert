"""右侧反转形态：状态读取与推送段落组装。

独立板块，但推送**共用同一条消息与每日额度**——形态段落追加在 A 股收盘确认
消息末尾，不额外占额度（额度只有 5 条/天，盘中扫描最多占 4 条）。

数据链路（v1.9.0 起云端也能跑）：
    形态判定需要日线。本机用 data/daily（385MB，不入库）；云端改用
    store/daily 分片仓（最近 400 个交易日，25MB，入库）→ 电脑关机期间，
    云端收盘确认轮也能自己扫出结果并追加段落。
    本机与云端都写同一个 dashboard/pattern.json，云端读它组装推送；
    任一端口当天没跑 → 文件里的 day 不是今天 → 自动不追加（优雅降级）。

⚠️ 定位（2010-01-05 ~ 2026-10-15 全历史回测，产物见 output/_signal/pattern_backtest.json）：
   阶段④「启动日」介入后 60 日**跑输全市场等权 8.95%**、胜率 31.3%、聚类 t −7.64，
   15 个年份无一为正（阶段③ −5.33%、阶段② −6.92%，所有子集均为负）。
   ⚠️ **加上 30~200 亿流通市值筛选后结论没有改善**（旧口径 −8.41%/31.7%，样本 6556；
      新口径 −8.95%/31.3%，样本 4149）。这个筛除条件的作用是「排除有风险与不可操作的
      标的」，**不是**把信号变成正的——它依旧是风险提示，不能当买入依据。
   → 本板块是**追涨风险提示**，不是买入信号。改文案前请先读回测产物。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "dashboard" / "pattern.json"

STAGE_TITLE = {"A": "今日启动（阶段④）", "B": "洗盘观察（阶段③）",
               "C": "刚突破前高（阶段②）"}


def load_state() -> dict:
    """读取本机扫描产物 dashboard/pattern.json（缺失/损坏时返回空）。"""
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return {}


def _num(v, nd=2, suffix=""):
    if v is None:
        return "—"
    try:
        return f"{float(v):.{nd}f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def _line(r: dict) -> str:
    """单只标的的一行推送文案。"""
    tags = []
    if r.get("zt"):
        tags.append("涨停")
    if r.get("estage") == "e4":
        tags.append("放量启动")
    tag_txt = f" **〔{'·'.join(tags)}〕**" if tags else ""
    bits = [_num(r.get("price"), 2) + f"（{r.get('chg', 0):+.2f}%）",
            "量比 " + _num(r.get("vol"), 2),
            "距高点 " + _num(r.get("retrace"), 1, "%")]
    if r.get("span") is not None:
        bits.append(f"回踩 {int(r['span'])} 日")
    bits.append(str(r.get("industry") or "—"))
    return (f"**{r.get('name')} {r.get('code')}**{tag_txt}\n" + "｜".join(bits))


def build_section(max_c: int = 6) -> tuple[str, int]:
    """组装形态推送段落。

    返回 ``(markdown 文本, 名单总数)``。非当日扫描或无候选时返回 ``("", 0)``。
    正文里 A 类（今日启动）全列，B/C 类只列前 ``max_c`` 个，避免消息过长。
    """
    obj = load_state()
    if not obj or obj.get("day") != date.today().isoformat():
        return "", 0
    rows = obj.get("rows") or []
    counts = obj.get("counts") or {}
    total = len(rows)
    if not total:
        return "", 0

    bt = (obj.get("backtest") or {}).get("e4") or {}
    head = (f"## 右侧反转形态 · 追涨风险提示"
            f"（扫描 {obj.get('scanned', 0)} 只）")
    parts = [head]
    if counts.get("A"):
        sub = [r for r in rows if r.get("stage") == "A"][:max_c]
        parts.append(f"**{STAGE_TITLE['A']} {counts['A']} 只**\n" +
                     "\n".join("- " + _line(r) for r in sub))
    for k in ("B", "C"):
        n = counts.get(k, 0)
        if not n:
            continue
        sub = [r for r in rows if r.get("stage") == k][:max_c]
        names = "、".join(str(r.get("name")) for r in sub)
        # 明确写出「列前 N」，否则读起来像总共只有这几个
        head = (f"**{STAGE_TITLE[k]} {n} 只**（列前 {len(sub)}）："
                if n > len(sub) else f"**{STAGE_TITLE[k]} {n} 只**：")
        parts.append(head + names)
    if not counts.get("A") and not counts.get("B"):
        parts.append("今日无「启动 / 洗盘」候选（仅观察池有标的）。")

    tail = (f"\n\n_⚠️ 全历史回测（2010-01 起）：此形态「启动日」介入后 60 日跑输全市场等权 "
            f"{abs(bt.get('ex60_med', -8.95)):.1f}%（胜率 {bt.get('ex60_win', 31.3):.1f}%，"
            "全部 15 个年份无一为正），属追涨风险特征，不是买入信号；"
            "完整清单与回测明细见看板「形态」页。_")
    return "\n\n".join(parts) + tail, total


def mark_pushed(codes: list[str]) -> None:
    """预留：形态按日推送一次，当前无需跨轮去重（收盘确认每次必推）。"""
    return None
