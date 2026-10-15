"""期货加权指数预警：状态落盘、去重、推送文案。

独立板块，但在推送通道上与 A 股**共用同一条消息与每日额度**：
A 股自有触发时，期货作为一节追加在消息末尾；A 股无触发而期货有时，
也会单独推一条（主题切换为期货）。
"""
from __future__ import annotations

import json
from datetime import date, datetime

from .config import load, state_dir


def _state_path():
    return state_dir() / "futures_triggers.json"


def _push_path():
    return state_dir() / "fut_push_state.json"


def _week_tag() -> str:
    iso = date.today().isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _load_push_state() -> dict:
    try:
        return json.loads(_push_path().read_text(encoding="utf-8"))
    except Exception:                                # noqa: BLE001
        return {}


def _save_push_state(st: dict) -> None:
    try:
        _push_path().write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception as e:                           # noqa: BLE001
        print(f"[fut] 推送状态写入失败：{e}")


def record_state() -> dict:
    """判定当日期货触发并落盘（供看板与推送消费）。返回落盘对象。"""
    cfg = load().get("futures") or {}
    if not cfg.get("enabled", True):
        return {}
    try:
        from fut import core as fcore
        from fut import scan as fscan
    except Exception as e:                           # noqa: BLE001
        print(f"[fut] 模块不可用，跳过：{e}")
        return {}

    weekly = fcore.load_weekly()
    if weekly.empty:
        print("[fut] 期货数据仓为空，跳过判定（先执行 --update-cloud 建仓）")
        return {}

    near_pct = float(cfg.get("near_pct", 0.03))
    daily = fcore.load_daily()
    df = fscan.evaluate(weekly=weekly, daily=daily, use_realtime=True,
                        near_pct=near_pct)

    st = _load_push_state()
    wk_tag, today = _week_tag(), date.today().isoformat()
    dedup_w = bool(cfg.get("dedup_weekly", True))
    rows = []
    if len(df):
        for rec in df.to_dict("records"):
            code = rec.get("code")
            prev = st.get(code, {})
            # 去重键是「品种 + 状态 + 周」：本周内同一品种同一状态只提醒一次；
            # 若从「临近」升级为「突破」则算新事件，仍会提醒。
            if dedup_w:
                dup = (prev.get("last_week") == wk_tag
                       and prev.get("status") == rec.get("status"))
            else:
                dup = prev.get("last_date") == today
            rec["is_new"] = not dup
            rows.append(rec)

    day = date.today().isoformat()
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    covered = int(daily["code"].nunique()) if not daily.empty else 0
    obj = {"day": day, "ts": ts, "near_pct": near_pct,
           "covered": covered, "rows": rows}
    try:
        _state_path().parent.mkdir(parents=True, exist_ok=True)
        _state_path().write_text(json.dumps(obj, ensure_ascii=False,
                                            separators=(",", ":")), encoding="utf-8")
    except Exception as e:                           # noqa: BLE001
        print(f"[fut] 触发状态写入失败：{e}")

    nb = sum(1 for r in rows if r["status"] == "突破")
    nn = len(rows) - nb
    nnew = sum(1 for r in rows if r["is_new"])
    print(f"[fut] 期货触发 {len(rows)} 个（突破 {nb} / 临近 {nn}），"
          f"其中新触发 {nnew} 个 → {_state_path()}")
    return obj


def _fmt(v: float) -> str:
    """期货价格式化：≥1000 取整、<100 保留 3 位、其余 2 位（原三段式
    条件表达式可读性差，拆成普通分支，行为不变）。"""
    a = abs(v)
    if a >= 1000:
        return f"{v:,.0f}"
    if a < 100:
        return f"{v:,.3f}"
    return f"{v:,.2f}"


def _line(r: dict) -> str:
    dev = r.get("dev_pct", 0) * 100
    return (f"**{r.get('name')}加权 {r.get('code')}**  {_fmt(r.get('price'))}"
            f"｜60周线 {_fmt(r.get('ma60'))}（{dev:+.1f}%）"
            f"｜均线{r.get('slope')}｜年内上穿 {r.get('up_cnt_1y', 0)} 次")


def build_section(only_new: bool = True) -> tuple[str, list[list[str]]]:
    """组装期货推送段落。

    返回 ``(markdown 文本, 本次应标记为「已推送」的 [[code, status], ...])``。

    注意：第二个返回值是**全部新触发品种**，而不仅是正文里列出的那些。
    正文为避免消息过长只列前 ``push_near_max`` 个临近品种，但去重必须按
    「全量」记账——否则每次盘中扫描都会把上次没列出的品种补位顶上，
    同一个品种一天被滚动推送四轮，白耗额度。
    """
    cfg = load().get("futures") or {}
    if not cfg.get("enabled", True) or not cfg.get("push", True):
        return "", []
    try:
        obj = json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:                                # noqa: BLE001
        return "", []
    if obj.get("day") != date.today().isoformat():
        return "", []

    rows = obj.get("rows") or []
    if only_new:
        rows = [r for r in rows if r.get("is_new")]
    if not rows:
        return "", []
    max_near = int(cfg.get("push_near_max", 5))
    bks = [r for r in rows if r["status"] == "突破"]
    nrs = [r for r in rows if r["status"] != "突破"]
    shown_near = nrs[:max_near]

    parts = [f"## 期货加权 · 60 周线（覆盖 {obj.get('covered', 0)} 个品种）"]
    if bks:
        parts.append(f"**上穿突破 {len(bks)} 个**\n" +
                     "\n".join("- " + _line(r) for r in bks))
    if shown_near:
        more = len(nrs) - len(shown_near)
        head = f"**临近 60 周线 {len(nrs)} 个**（±{obj.get('near_pct', 0.03) * 100:.0f}%）"
        if more > 0:
            head += f"，仅列前 {len(shown_near)}"
        parts.append(head + "\n" +
                     "\n".join("- " + _line(r) for r in shown_near))
    txt = "\n\n".join(parts)
    if cfg.get("dedup_weekly", True):
        txt += "\n\n_同一品种同一周只推首次；完整清单见看板「期货」页。_"
    marked = [[r["code"], r["status"]] for r in bks + nrs]
    return txt, marked


def mark_pushed(pairs: list) -> None:
    """标记已推送。``pairs`` 可传 ``[[code, status], ...]`` 或 ``[code, ...]``。"""
    st = _load_push_state()
    today, wk = date.today().isoformat(), _week_tag()
    for it in pairs:
        if isinstance(it, (list, tuple)):
            code, status = (list(it) + ["", ""])[:2]
        else:
            code, status = it, ""
        st[code] = {"last_date": today, "last_week": wk, "status": status}
    _save_push_state(st)
