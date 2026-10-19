"""实时预警系统入口。

本地模式：
  python -m alert.run_alert --test              # 通道测试推送
  python -m alert.run_alert --scan              # 立即扫描一次（盘中预警）
  python -m alert.run_alert --confirm           # 收盘确认批
  python -m alert.run_alert --update            # 日线增量更新 + 快照重建
  python -m alert.run_alert --daemon            # 常驻：按配置时刻自动执行
  python -m alert.run_alert --demo              # 离线演示（模拟+2%，只打印）

云端模式（GitHub Actions 三段式：prepare → git commit → notify）：
  python -m alert.run_alert --is-trading-day    # 交易日校验（exit 0/42）
  python -m alert.run_alert --prepare --cloud [--demo]   # 判定+出图，不推送
  python -m alert.run_alert --notify --sha <commit_sha>  # 组装URL并推送
  python -m alert.run_alert --update-cloud      # 云端数据仓增量更新（store/）

人工触发：
  python -m alert.run_alert --push --title "标题" --message "正文"   # 手动推送一条到手机
  python -m alert.run_alert --push --title "标题" --dry-run         # 只验证链路不实际发送

"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import pusher, quoter, scanner, template
from .config import load, state_dir
from .ma60_base import build_snapshot, load_snapshot
# 名称风险判定（ST / *ST / 退市整理）全项目唯一真源：
# 推送名单（run_scan / run_prepare）与形态扫描（pattern.core）共用同一口径。
from name_rule import drop_risk_rows, risk_tag

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output" / "alert"
PENDING = OUT / "pending.json"
PENDING_CSV = OUT / "pending.csv"
# 进入推送文案与看板的字段（看板排序也依赖这里的成交额/换手率/市值）
MSG_FIELDS = ["name", "price", "chg_pct", "dev_pct", "slope", "a_events_1y",
              "industry", "mv_float_yi", "amount_wan", "turnover", "amt_ratio"]


# ---------------------------------------------------------------- 通用
def _chart_enabled() -> bool:
    base = load()["jump"].get("chart_base_url", "")
    return bool(base) and "USER/REPO" not in base


def _local_weekly(code: str):
    import pandas as pd
    f = ROOT / "data" / "daily" / f"{code.replace('.', '_')}.csv.gz"
    if not f.exists():
        return None
    df = pd.read_csv(f, parse_dates=["date"])
    s = df.set_index("date")["close"]
    return s.groupby(s.index.to_period("W")).last()


def _store_weekly_map():
    from .cloud_store import load_weekly
    wk = load_weekly()
    if not len(wk):
        return {}
    return {c: g.set_index("week_end")["close"] for c, g in wk.groupby("code")}


def _snapshot_behind(snap) -> bool:
    """快照是否落后于本地日线（BUG #7）。

    快照缓存 3 天：某只股票的数据被修复后（如双汇 10-15 补齐），若当日
    update 轮没跑，次日扫描仍用旧快照判定（坏 MA60 → 假触发）。用几只
    流动性主板股的日线最新日期做探针，快照比它们旧就强制重建（约 2~4 分钟）。
    """
    import pandas as pd
    try:
        if snap is None or "last_date" not in getattr(snap, "columns", []):
            return False
        snap_max = pd.to_datetime(snap["last_date"], errors="coerce").max()
        if pd.isna(snap_max):
            return False
        for name in ("sh_600000", "sz_000001", "sh_601318"):
            f = ROOT / "data" / "daily" / f"{name}.csv.gz"
            if not f.exists():
                continue
            d = pd.read_csv(f, usecols=["date"], parse_dates=["date"])
            if len(d) and d["date"].max() > snap_max:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _get_snapshot(force: bool = False, cloud: bool = False):
    if cloud:
        from .cloud_store import build_snapshot_from_store
        snap = build_snapshot_from_store()
        print(f"[snapshot] 云端仓快照：{len(snap)} 只")
        return snap
    snap = None if force else load_snapshot()
    if snap is not None and _snapshot_behind(snap):
        print("[snapshot] 本地日线比快照新（数据修复后未重建）→ 强制重建")
        snap = None
    if snap is None:
        print("[snapshot] 缓存缺失或过期，重建中（约 2~4 分钟）...")
        snap = build_snapshot()
    return snap


def _evaluate(cloud: bool, demo: bool, force_snapshot: bool,
              mode: str = "intraday", dedup: bool = True):
    snap = _get_snapshot(force_snapshot, cloud)
    quotes = quoter.fetch_quotes(snap.index.tolist())
    if demo:
        quotes["price"] = quotes["preclose"] * 1.02
        quotes["chg_pct"] = 2.0
    trig = scanner.evaluate(snap, quotes, mode="demo" if demo else mode, dedup=dedup)
    return snap, trig, quotes


# ---------------------------------------------------------------- 本地
def run_scan(mode: str = "intraday", push: bool = True, force_snapshot: bool = False):
    snap = _get_snapshot(force_snapshot)
    quotes = quoter.fetch_quotes(snap.index.tolist())
    trig = scanner.evaluate(snap, quotes, mode=mode)
    trig, n_risk = drop_risk_rows(trig)
    print(f"[{mode}] 触发 {len(trig)} 只"
          + (f"（已剔除 ST / 退市风险股 {n_risk} 只）" if n_risk else ""))
    if len(trig):
        print(trig[MSG_FIELDS].round(3).to_string())
    if push and len(trig):
        title, desp = template.build_message(trig, mode)
        if pusher.send(title, desp):
            scanner.mark_pushed(trig.index.tolist(), mode)
            print(f"[push] 已推送：{title}")
    elif push and mode == "confirm":
        title, desp = template.build_message(trig, mode)
        if pusher.send(title, desp):
            print(f"[push] 已推送：{title}")
    return trig


def run_update():
    from .update import update_benchmark, update_recent
    res = update_recent()
    print(res["status"].value_counts().to_dict())
    # 沪深300 基准日线增量（BUG #10）：研究管线的超额计算与近一月库日历
    # 兜底都读它。失败不影响日线主链路（基准滞后只是研究口径偏差）。
    update_benchmark()
    print("[snapshot] 重建快照...")
    build_snapshot()
    print("[update] 完成")


def run_daemon():
    cfg = load()["scan"]
    schedule = sorted(set(cfg["intraday_times"] + [cfg["close_confirm"], cfg["daily_update"]]))
    print(f"[daemon] 时刻表 {schedule}（Ctrl+C 退出）")
    done: set[str] = set()
    while True:
        now = datetime.now()
        hhmm = now.strftime("%H:%M")
        if hhmm in schedule and hhmm not in done:
            try:
                mdate = quoter.market_date()
                is_trading = mdate == now.date().isoformat()
                if hhmm == cfg["daily_update"]:
                    run_update()
                elif hhmm == cfg["close_confirm"]:
                    if is_trading:
                        run_scan("confirm")
                    else:
                        print("[daemon] 非交易日/无行情，跳过收盘确认")
                else:
                    if is_trading:
                        run_scan("intraday")
                    else:
                        print("[daemon] 非交易时段，跳过")
            except Exception as exc:  # noqa: BLE001
                print(f"[daemon] 任务异常: {type(exc).__name__} {exc}")
            done.add(hhmm)
        if hhmm == "00:00":
            done.clear()
        time.sleep(20)


def run_test():
    if not pusher.push_enabled():
        print("推送已停用（config push.enabled=false）→ 自检跳过发送")
        return
    key_ok = pusher.send(
        "预警系统自检（系统已就绪）",
        "实时预警系统部署自检通过。\n\n"
        "- Server酱通道：**正常**\n"
        "- 触发口径：昨收<60周线 且 现价≥60周线（严格）\n\n"
        f"网页行情示例（贵州茅台 600519）：\n\n{template.ths_links('sh.600519')}")
    print("测试推送", "成功" if key_ok else "失败")


def run_manual_push(title: str, desp: str, dry_run: bool | None = None) -> dict:
    """手动向手机发送一条推送通知，返回结构化结果状态。

    入口：``python -m alert.run_alert --push --title "标题" --message "正文" [--dry-run]``

    复用 ``pusher.send``（开关 / 每日额度 / 3 次重试 / dry-run 全部沿用），
    并在发送前做明确预检，返回可供人工与脚本消费的结果字典::

        {"ok": bool, "status": str, "detail": str, "remaining": int, "dry_run": bool}

    ``status`` 取值：``sent``（已发送）/ ``dry_run``（仅验证未实际发送）/
    ``disabled``（总开关关）/ ``no_quota``（额度用尽）/ ``no_key``（未配置密钥）/
    ``empty``（标题为空）/ ``failed``（网络错误或接口拒绝）。
    """
    import os
    title = (title or "").strip()
    desp = (desp or "").strip()
    is_dry = (os.environ.get("ALERT_DRYRUN") == "1") if dry_run is None else bool(dry_run)
    cfg = load()
    scfg = cfg.get("serverchan", {})

    if not title:
        print("[manual-push] ❌ 标题不能为空，未发送")
        return {"ok": False, "status": "empty", "detail": "标题不能为空",
                "remaining": pusher.remaining_quota(), "dry_run": is_dry}

    if is_dry:
        # dry-run：只验证链路；pusher.send 不会真正发送，也不受开关/额度/密钥约束。
        print("[manual-push] [dry-run] 跳过实际发送，验证链路中…")
        pusher.send(title, desp or "（无正文）", dry_run=True)
        print(f"[manual-push] ⚠️ dry-run：未实际发送（链路验证通过）：{title}")
        return {"ok": True, "status": "dry_run", "detail": "dry-run 模式，未实际发送",
                "remaining": pusher.remaining_quota(), "dry_run": True}

    # 以下为真实发送前的预检（与 pusher 内部一致，但先给出明确原因）
    key = os.environ.get("ALERT_SENDKEY") or scfg.get("sendkey", "")
    if not key:
        print("[manual-push] ❌ 未配置 Server酱 sendkey"
              "（alert_config.local.yaml 的 serverchan.sendkey 或环境变量 ALERT_SENDKEY）")
        return {"ok": False, "status": "no_key", "detail": "未配置 Server酱 sendkey",
                "remaining": pusher.remaining_quota(), "dry_run": is_dry}

    if not pusher.push_enabled():
        print(f"[manual-push] ❌ 推送已停用（push.enabled=false），未发送：{title}")
        return {"ok": False, "status": "disabled", "detail": "push.enabled=false",
                "remaining": pusher.remaining_quota(), "dry_run": is_dry}

    rem = pusher.remaining_quota()
    if rem <= 0:
        print(f"[manual-push] ❌ 今日额度已用尽"
              f"（{scfg.get('daily_limit', 5)} 条/天），未发送：{title}")
        return {"ok": False, "status": "no_quota", "detail": "今日推送额度已用尽",
                "remaining": rem, "dry_run": is_dry}

    if not desp:
        desp = "（无正文）"
    print(f"[manual-push] 预检通过，额度剩余 {rem} 条，发送中…")
    ok = pusher.send(title, desp)
    rem_after = pusher.remaining_quota()
    if ok:
        print(f"[manual-push] ✅ 已发送：{title}（额度剩余 {rem_after} 条）")
    else:
        print(f"[manual-push] ❌ 发送失败（网络错误或接口拒绝，已重试 3 次）：{title}")
    return {"ok": ok, "status": ("sent" if ok else "failed"),
            "detail": ("已发送" if ok else "网络错误或接口拒绝"),
            "remaining": rem_after, "dry_run": False}


def run_demo(cloud: bool = False):
    snap, trig, _ = _evaluate(cloud=cloud, demo=True, force_snapshot=False, dedup=False)
    trig, n_risk = drop_risk_rows(trig)
    print(f"[demo] 模拟全市场+2%：触发 {len(trig)} 只"
          + (f"（已剔除 ST / 退市 {n_risk} 只）" if n_risk else ""))
    if len(trig):
        print(trig[MSG_FIELDS].head(20).round(3).to_string())
        title, desp = template.build_message(trig.head(3), "demo")
        print("\n===== 推送文案预览 =====")
        print("标题:", title)
        print(desp[:1000])


# ---------------------------------------------------------------- 云端
def run_is_trading_day() -> int:
    mdate = quoter.market_date()
    today = date.today().isoformat()
    print(f"行情交易日={mdate}  今天={today}")
    return 0 if mdate == today else 42


def _record_dashboard_state(n_rows: int, trig, snap_size: int, mode: str,
                            quotes=None) -> None:
    """把本次扫描结果落到 store/state/，供看板消费。

    产物：
      latest_triggers.json  最近一次扫描的触发明细（含行业/斜率/量比等字段）
      history.json          按次的触发数流水（看板画趋势用，滚动保留 400 条）
      store/last_quotes.csv 全市场不复权最新价与昨收（账户估值用，随数据仓提交）
    """
    from .config import state_dir
    d = state_dir()
    day = date.today().isoformat()
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # 全市场最新价（不复权）→ store/last_quotes.csv
    if quotes is not None and len(quotes):
        try:
            q = quotes[["price", "preclose", "ts"]].copy()
            q.index.name = "code"
            q.to_csv(ROOT / "store" / "last_quotes.csv")
            print(f"[state] 最新价落盘 {len(q)} 只 → store/last_quotes.csv")
        except Exception as exc:  # noqa: BLE001
            print(f"[state] 最新价落盘失败: {exc}")

    rows = []
    if trig is not None and len(trig):
        for code, r in trig[MSG_FIELDS].iterrows():
            try:
                rows.append({
                    "code": code,
                    "name": str(r["name"]),
                    "price": round(float(r["price"]), 3),
                    "chg_pct": round(float(r["chg_pct"]), 2),
                    "dev_pct": round(float(r["dev_pct"]) * 100, 3),
                    "slope": str(r["slope"]),
                    "a_events_1y": int(r["a_events_1y"]),
                    "industry": str(r["industry"]),
                    "mv_float_yi": round(float(r["mv_float_yi"]), 1),
                    "amount_wan": round(float(r["amount_wan"]), 1),
                    "turnover": round(float(r["turnover"]), 2),
                    "amt_ratio": round(float(r["amt_ratio"]), 2),
                })
            except Exception as exc:  # noqa: BLE001
                print(f"[state] {code} 记录失败: {exc}")

    (d / "latest_triggers.json").write_text(
        json.dumps({"day": day, "ts": ts, "mode": mode,
                    "scanned": int(snap_size), "rows": rows},
                   ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    hp = d / "history.json"
    try:
        hist = json.loads(hp.read_text(encoding="utf-8"))
        if not isinstance(hist, list):
            hist = []
    except Exception:  # noqa: BLE001
        hist = []
    hist.append({"day": day, "ts": ts, "mode": mode,
                 "n": n_rows, "scanned": int(snap_size)})
    hp.write_text(json.dumps(hist[-400:], ensure_ascii=False, separators=(",", ":")),
                  encoding="utf-8")
    print(f"[state] 看板状态已记录：{n_rows} 只触发 / 快照 {snap_size} 只 → {d}")


def run_prepare(cloud: bool = True, demo: bool = False, mode: str = "intraday"):
    """云端第一段：判定 + 出图，结果落 pending，不推送。

    pending / 推送只含「新触发」（去重后）；看板状态记录当日全量触发。
    """
    snap, trig_all, quotes = _evaluate(cloud=cloud, demo=demo, force_snapshot=False,
                                       mode="demo" if demo else mode, dedup=False)
    trig_new = trig_all[trig_all["is_new"]] if len(trig_all) else trig_all
    # 剔除 ST / *ST / 退市整理股：只作用于推送名单（pending）。
    # trig_all 不动 —— 看板「当日触发」仍是全量，展示层自己再隐藏。
    trig, n_risk = drop_risk_rows(trig_new)
    print(f"[prepare] 当日触发 {len(trig_all)} 只，其中新触发 {len(trig_new)} 只"
          + (f"；剔除 ST / 退市 {n_risk} 只 → 待推 {len(trig)} 只" if n_risk
             else f"，待推 {len(trig)} 只"))
    if n_risk and "name" in getattr(trig_new, "columns", []):
        print("[prepare] 已剔除："
              + "、".join(f"{n}（{risk_tag(n)}）" for n in trig_new["name"] if risk_tag(n)))
    day = date.today().isoformat()
    result = {"mode": "demo" if demo else mode,
              "day": day, "n": int(len(trig)), "charts": {}}
    # v1.25.0 方案A「转发件」：当日汇总长图，供用户保存后转发微信群。
    # 与周K图同目录、同 commit，notify 用同一 sha 拼 jsDelivr URL。
    if len(trig) and load().get("share", {}).get("enabled", True):
        from .chart import render_daily_summary
        try:
            out = ROOT / "charts" / day / "daily.png"
            render_daily_summary(day, trig, out, mode=result["mode"])
            result["share_png"] = out.relative_to(ROOT).as_posix()
        except Exception as exc:                        # noqa: BLE001
            print(f"[share] 汇总长图渲染失败（不影响推送）: {exc}")
    if len(trig):
        OUT.mkdir(parents=True, exist_ok=True)
        # 云端快照在部分路径会丢失索引名称。固定写出 code 列，
        # 否则 notify 的 read_csv(index_col="code") 无法读取待推送名单。
        trig[MSG_FIELDS].copy().to_csv(PENDING_CSV, index_label="code")
    # 图表：为当日全部触发（含已推过的）出图，供看板点击查看
    if len(trig_all) and _chart_enabled():
        from .chart import render_weekly_chart
        wmap = _store_weekly_map() if cloud else None
        for code in trig_all.index:
            weekly = wmap.get(code) if wmap else None
            if weekly is None and not cloud:
                weekly = _local_weekly(code)
            if weekly is None:
                continue
            name = str(trig_all.at[code, "name"])
            out = ROOT / "charts" / day / f"{code.split('.')[1]}.png"
            # 内部存储为后复权，除以当日复权因子换算成前复权后再画，
            # 使坐标轴价格与行情软件一致（比值不变，信号不受影响）
            try:
                fac = float(trig_all.at[code, "factor"]) if "factor" in trig_all.columns else 0.0
            except (TypeError, ValueError):
                fac = 0.0
            scale = (1.0 / fac) if fac and fac == fac and fac > 0 else 1.0
            try:
                render_weekly_chart(code, name, weekly, out, scale=scale)
                result["charts"][code] = out.relative_to(ROOT).as_posix()
            except Exception as exc:  # noqa: BLE001
                print(f"[chart] {code} 渲染失败: {exc}")
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    PENDING.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                       encoding="utf-8")
    print(f"[prepare] {result['n']} 只待推，图表 {len(result['charts'])} 张 → {PENDING}")
    # 测试扫描隔离（BUG #13）：本机做开发验证时设 ALERT_TEST=1，
    # 不往看板状态/趋势流水里写「盘中扫描」记录（历史上混进过
    # 2026-10-14 收盘后的三轮测试扫描，趋势图失真）。
    if not demo and os.environ.get("ALERT_TEST") != "1":
        _record_dashboard_state(len(trig_all), trig_all if len(trig_all) else None,
                                len(snap), "demo" if demo else mode, quotes)
        # 期货加权指数的当日触发（独立板块，共用推送额度）
        try:
            from . import fut_alert
            fut_alert.record_state()
        except Exception as exc:  # noqa: BLE001
            print(f"[fut] 状态记录失败: {type(exc).__name__} {exc}")
    elif os.environ.get("ALERT_TEST") == "1":
        print("[prepare] ALERT_TEST=1 → 跳过看板状态记录（测试扫描不入流水）")


def run_notify(sha: str):
    """云端第三段：读 pending，组装带 commit-sha 的图表 URL 并推送。

    A 股、期货与右侧反转形态共用同一条消息与每日额度（Server酱 5 条/天）：
      * A 股有触发 → 期货、形态各自作为一节追加在末尾
      * A 股无触发而其余板块有 → 单独推一条，标题切到该板块主题
      * 全都无内容 → 不推送（省额度）

    形态段落只在**收盘确认轮**追加：形态是收盘后的判断，盘中追加没有意义。

    v1.9.0 起形态扫描云端也能跑（``pattern.store_scan`` 读 ``store/daily`` 分片仓），
    不再依赖本机的 ``data/daily``——本机不在线时由云端产出 dashboard/pattern.json。
    """
    from . import fut_alert, pattern_alert

    # 推送总开关：关闭时直接返回。**不要**在这里 mark_pushed——没发出去就不能记账，
    # 否则恢复推送后本周的信号会被当成「已推过」而永久漏掉。
    if not pusher.push_enabled():
        n = 0
        if PENDING.exists():
            try:
                n = int(json.loads(PENDING.read_text(encoding="utf-8")).get("n") or 0)
            except Exception:                              # noqa: BLE001
                n = 0
        print(f"[notify] 推送已停用（config push.enabled=false）：本轮 A 股新触发 {n} 只，"
              "只扫描记账、不发送")
        return

    if not PENDING.exists():
        print("[notify] 无 pending，跳过")
        return
    res = json.loads(PENDING.read_text(encoding="utf-8"))
    trig = None
    if res["n"] and PENDING_CSV.exists():
        import pandas as pd
        pending = pd.read_csv(PENDING_CSV)
        # 兼容此前已生成的 CSV：旧文件首列可能没有列名，但仍保存股票代码。
        code_col = "code" if "code" in pending.columns else pending.columns[0]
        trig = pending.set_index(code_col)
        trig.index.name = "code"
        base = load()["jump"].get("chart_base_url", "").rstrip("/")
        chart_urls = {code: f"{base}@{sha}/{rel}"
                      for code, rel in res["charts"].items()}
        title, desp = template.build_message(trig, res["mode"], chart_urls)
    else:
        title, desp = template.build_message(trig, res["mode"])

    fut_txt, fut_codes = fut_alert.build_section()
    pat_txt, pat_n = "", 0
    if res.get("mode") == "confirm":
        try:
            pat_txt, pat_n = pattern_alert.build_section()
        except Exception as exc:                       # noqa: BLE001
            print(f"[pattern] 段落组装失败：{type(exc).__name__} {exc}")

    # v1.21.0：重要财经新闻栏目（同花顺 / 金十数据 / 新浪财经三源）。
    # 任一源失败由 news 内部降级为栏目内提示，这里再兜一层，确保永不阻断主链路。
    news_txt, news_n = "", 0
    try:
        from . import news
        news_txt, news_n = news.build_section()
    except Exception as exc:                           # noqa: BLE001
        print(f"[news] 栏目生成失败：{type(exc).__name__} {exc}")

    # 纯新闻（A 股/期货/形态都空）只在**收盘确认轮**单独成条推送：
    # Server酱每日仅 5 条额度，盘中 4 个扫描槽若都能因新闻单独推送，
    # 当天额度会被新闻占满，真正的预警反而推不出去。
    alone_ok = (res.get("mode") == "confirm"
                and bool((load().get("news") or {}).get("push_alone", False)))
    if news_txt and not res["n"] and not fut_txt and not pat_txt and not alone_ok:
        print("[notify] 仅有财经新闻、且非收盘确认轮（或 news.push_alone=false），"
              "不单独推送（不占额度）")
        news_txt = ""

    extra = [t for t in (fut_txt, pat_txt, news_txt) if t]
    if extra:
        body = "\n\n---\n\n".join(extra)
        if res["n"]:
            desp = desp + "\n\n---\n\n" + body
        else:
            if fut_txt:
                title = f"【期货60周线】{len(fut_codes)} 个品种靠近加权均线"
            elif pat_txt:
                title = f"【右侧反转形态】{pat_n} 只候选（追涨风险提示）"
            elif news_txt:
                title = f"【重要财经新闻】{news_n} 条"
            desp = body
    elif not res["n"]:
        print("[notify] A 股、期货与形态均无内容，跳过推送（不占额度）")
        return

    # v1.25.0 方案A：A 股有触发时，末尾附「当日汇总长图 + 看板链接」，
    # 用户保存长图即可转发微信群（朋友无需装任何 App）。
    if res.get("share_png") and res["n"]:
        cfg = load().get("share", {})
        base = load()["jump"].get("chart_base_url", "").rstrip("/")
        if base:
            png_url = f"{base}@{sha}/{res['share_png']}"
            share_md = f"\n\n---\n\n**当日汇总图**（长按/保存后可转发微信群）：\n\n" \
                       f"![当日汇总]({png_url})"
            dash = cfg.get("dashboard_url", "")
            if dash:
                share_md += f"\n\n[在线看板（完整名单/周K/期货）]({dash})"
            desp += share_md

    if pusher.send(title, desp):
        if trig is not None:
            scanner.mark_pushed(trig.index.tolist(), res["mode"])
        if fut_codes:
            fut_alert.mark_pushed(fut_codes)
        print(f"[notify] 已推送：{title}")


NEWS_GRACE_MIN = 5   # 非交易日要闻轮次：允许提前 5 分钟进入下一轮（吸收时钟抖动）


def _hm_to_min(hm: str) -> int:
    h, m = str(hm).split(":")
    return int(h) * 60 + int(m)


def pick_news_round(rounds: list[str], now: datetime, done: set[str]) -> str:
    """选出当前可执行的要闻轮次：最晚一个「已到点且未执行」的轮次。

    规则：
      1) 只允许进入「已到点（含 5 分钟提前）」的轮次，不提前跑未来轮次；
      2) 已执行的轮次跳过；
      3) 日历日全部轮次执行完返回空串（当天不再推送）。
    """
    now_m = now.hour * 60 + now.minute
    cand = [r for r in rounds
            if _hm_to_min(r) - NEWS_GRACE_MIN <= now_m and r not in done]
    return cand[-1] if cand else ""


def run_news_summary(cloud: bool = False, push: bool = True) -> int:
    """非交易日财经要闻：多轮推送，每轮只推当日新增条目。

    轮次取 ``news.off_day_rounds``（默认与交易日同槽位
    09:35 / 10:15 / 13:30 / 14:20 / 15:10）。状态存
    ``store/state/news_marks.json``：
    ``{"2026-10-19": {"rounds": ["09:35"], "keys": ["<url|title>", ...]}}``
    旧格式（值为 "HH:MM" 字符串）视为「当日已汇总」，直接跳过。
    重复触发（GitHub cron 抖动、外接定时服务补触发）不会重复占用额度。
    """
    from . import news
    cfg = news._cfg()
    mark = state_dir() / "news_marks.json"
    today = date.today().isoformat()
    try:
        marks = json.loads(mark.read_text(encoding="utf-8")) if mark.exists() else {}
        marks = marks if isinstance(marks, dict) else {}
    except (OSError, ValueError):
        marks = {}
    state = marks.get(today)
    if isinstance(state, str):
        print(f"[news-summary] 今日（{today}）已汇总过（旧格式标记 {state}），跳过")
        return 0
    state = state if isinstance(state, dict) else {}
    done = [str(r) for r in (state.get("rounds") or [])]
    keys = [str(k) for k in (state.get("keys") or [])]
    rounds = [str(r) for r in (cfg.get("off_day_rounds") or ["09:35"])]
    rnd = pick_news_round(rounds, datetime.now(), set(done))
    if not rnd:
        print(f"[news-summary] 无待执行轮次（今日已完成 {len(done)}/{len(rounds)} 轮："
              f"{'、'.join(done) or '无'}），跳过")
        return 0
    # 1) 刷新看板 news.json（放宽到 48h，覆盖周末两天）
    eff = dict(cfg)
    eff["window_hours"] = float(cfg.get("off_day_window_hours", 48))
    news.build_dashboard(eff)
    # 2) 生成增量汇总栏目：只含当日未推送过的条目
    seen = set(keys) if bool(cfg.get("off_day_incremental", True)) else set()
    pushed: list[str] = []
    label = f"第 {len(done) + 1} 轮 {rnd}"
    text, n = news.build_summary(cfg, exclude=seen, label=label, out_keys=pushed)
    # 3) 推送（受 push.enabled 与每日额度约束；pusher 内部已处理 dry-run / 额度）
    sent = False
    if push and bool(cfg.get("off_day_push", True)) and n:
        title = f"【财经要闻】{rnd} · 新增 {n} 条"
        sent = pusher.send(title, text)
        print(f"[news-summary] 轮次 {rnd} 推送{'成功' if sent else '未发送（额度/开关）'}："
              f"新增 {n} 条")
    else:
        print(f"[news-summary] 轮次 {rnd} 生成 {n} 条（未推送：push={push} "
              f"off_day_push={cfg.get('off_day_push')} n={n}）")
    # 4) 打标：轮次必打（同一轮不重试）；条目仅在真正推送后计入已推
    done = sorted(set(done) | {rnd})
    if sent:
        keys = sorted(set(keys) | set(pushed))
    new_marks: dict = {today: {"rounds": done, "keys": keys}}
    cutoff = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    for d, v in marks.items():
        if d != today and str(d) >= cutoff:
            new_marks[d] = v
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(json.dumps(new_marks, ensure_ascii=False, indent=1, sort_keys=True),
                    encoding="utf-8")
    return n


def run_update_cloud():
    from .cloud_store import update_from_baostock
    update_from_baostock()
    # 期货加权指数（独立数据仓 store/fut/）：失败不影响 A 股数据仓
    try:
        from fut import core as fcore
        fcore.update()
    except Exception as exc:  # noqa: BLE001
        print(f"[fut] 期货增量更新失败（不影响 A 股）：{type(exc).__name__} {exc}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--demo", action="store_true")
    # 云端
    ap.add_argument("--is-trading-day", action="store_true")
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--update-cloud", action="store_true")
    ap.add_argument("--news", action="store_true",
                    help="非交易日财经要闻汇总（刷新看板 + 按配置单独推送）")
    ap.add_argument("--push", action="store_true",
                    help="手动向手机发送一条推送（需配合 --title / --message）")
    ap.add_argument("--title", default="", help="--push 的标题")
    ap.add_argument("--message", default="", help="--push 的正文（可省略，默认空）")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="只验证链路不实际发送（覆盖 ALERT_DRYRUN 环境变量）")
    ap.add_argument("--cloud", action="store_true", help="用 store 精简仓做快照")
    ap.add_argument("--mode", default="intraday", choices=["intraday", "confirm"])
    ap.add_argument("--sha", default="main")
    ap.add_argument("--force-snapshot", action="store_true")
    a = ap.parse_args()

    if a.test:
        run_test()
    elif a.scan:
        run_scan("intraday", force_snapshot=a.force_snapshot)
    elif a.confirm:
        run_scan("confirm", force_snapshot=a.force_snapshot)
    elif a.update:
        run_update()
    elif a.daemon:
        run_daemon()
    elif a.is_trading_day:
        raise SystemExit(run_is_trading_day())
    elif a.prepare:
        run_prepare(cloud=a.cloud, demo=a.demo, mode=a.mode)
    elif a.notify:
        run_notify(a.sha)
    elif a.update_cloud:
        run_update_cloud()
    elif a.news:
        run_news_summary(cloud=a.cloud)
    elif a.push:
        res = run_manual_push(a.title, a.message, dry_run=a.dry_run)
        print(f"[manual-push] 结果：{json.dumps(res, ensure_ascii=False)}")
        raise SystemExit(0 if res["ok"] else 1)
    elif a.demo:
        run_demo(cloud=a.cloud)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
