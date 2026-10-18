"""周K图渲染：近两年周收盘 + 60周线 + 上穿点标注，输出 PNG。

Server酱 desp 支持 markdown 图片，配合 GitHub 仓库 +
jsDelivr CDN 即可在微信内直接展示周K走势。
"""
from __future__ import annotations

import os
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt   # noqa: E402
import pandas as pd               # noqa: E402

MA_WEEKS = 60
N_WEEKS = 110          # 图上显示约两年
UP = "#D6362B"         # 中文市场习惯：涨红
DOWN = "#1E8449"
LINE = "#2C3E50"
MA = "#E67E22"


def setup_font():
    candidates = ["Microsoft YaHei", "Noto Sans CJK SC", "WenQuanYi Zen Hei",
                  "SimHei", "PingFang SC"]
    avail = {f.name for f in
             __import__("matplotlib.font_manager", fromlist=["fontManager"]).fontManager.ttflist}
    for name in candidates:
        if name in avail:
            plt.rcParams["font.sans-serif"] = [name]
            break
    plt.rcParams["axes.unicode_minus"] = False


def render_weekly_chart(code: str, name: str, weekly: pd.Series,
                        out_path: Path, scale: float = 1.0) -> Path:
    """weekly: index=周期末Timestamp, values=周收盘（后复权）。

    scale: 显示换算系数。内部数据为后复权，乘上「当日不复权价 / 当日后复权价」
           即换算为前复权（两种复权仅差一个正常数，因此 收盘/MA60 的比值、
           上穿判定与信号完全不变，只是坐标轴数字变成行情软件里的真实价）。

    自动计算 MA60 并标注「最近一个已完成周是否处于上穿状态」。
    """
    setup_font()
    w = weekly.dropna().tail(N_WEEKS) * float(scale)
    if len(w) < MA_WEEKS + 1:
        raise ValueError(f"{code} 周样本不足({len(w)})")
    ma = w.rolling(MA_WEEKS).mean()

    fig, ax = plt.subplots(figsize=(8.0, 4.2), dpi=150)
    x = (w.index.to_timestamp() if isinstance(w.index, pd.PeriodIndex)
         else pd.DatetimeIndex(w.index))
    ax.plot(x, w.values, color=LINE, lw=1.4, label="周收盘", zorder=2)
    ax.plot(x, ma.values, color=MA, lw=1.6, label="60周线", zorder=3)

    # 上穿点标注：最后一个已完成周 收盘由下往上越过 MA60
    diffs = (w - ma).values
    for i in range(1, len(diffs)):
        if diffs[i - 1] < 0 <= diffs[i]:
            ax.scatter(x[i], w.values[i], s=42, color=UP, zorder=4)
    # 最新点强调
    last_up = diffs[-1] >= 0
    ax.scatter([x[-1]], [w.values[-1]], s=46,
               color=UP if w.values[-1] >= w.values[-2] else DOWN, zorder=5)

    ax.set_title(f"{name}（{code}）周K 与 60周线", fontsize=13, pad=8)
    note = "前复权价格"
    ax.text(0.995, 0.02, note, transform=ax.transAxes, ha="right",
            va="bottom", fontsize=8, color="#8E8E93")
    ax.grid(alpha=0.25, lw=0.6)
    ax.legend(loc="upper left", fontsize=9, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.autofmt_xdate(rotation=0, ha="center")
    fig.tight_layout()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def chart_url(code: str, day: str | None = None) -> str:
    """按配置生成图片的公网 URL（GitHub + jsDelivr）。"""
    from .config import load
    cfg = load()["jump"]
    base = cfg.get("chart_base_url", "").rstrip("/")
    if not base:
        return ""
    code6 = code.split(".")[1]
    day = day or pd.Timestamp.today().strftime("%Y-%m-%d")
    return f"{base}/{day}/{code6}.png"


# ---------------------------------------------------------------- 当日汇总长图
# 用途（v1.25.0 方案A「转发件」）：推送末尾附一张当日触发汇总竖图，
# 用户保存后可直接转发微信群，朋友无需点链接、无需装任何 App。
_MODE_LABEL = {"scan": "盘中扫描", "confirm": "收盘确认",
               "update": "数据更新", "demo": "演示"}


def render_daily_summary(day: str, trig: pd.DataFrame, out_path: Path,
                         mode: str = "", max_rows: int = 20) -> Path:
    """当日 A 股触发名单汇总竖图（微信友好 9:16 比例）。

    trig: prepare 阶段的待推名单（index=code，含 name/price/chg_pct/dev_pct 等）。
    只展示客观事实与既有回测口径，底部保留「非买入信号」声明（铁律 #3）。
    """
    setup_font()
    trig = trig.copy()
    trig["_abs"] = (trig["dev_pct"]).abs() if "dev_pct" in trig.columns else 0
    trig = trig.sort_values("_abs")

    def _f(val: object, d: float = 0.0) -> float:
        try:
            v = float(val)
            return d if v != v else v          # NaN -> 默认值
        except (TypeError, ValueError):
            return d

    total = len(trig)
    rows = trig.head(max_rows)
    shown = len(rows)

    head_h, row_h, foot_h = 1.55, 0.42, 1.05
    fig_h = head_h + row_h * max(shown, 1) + foot_h
    fig = plt.figure(figsize=(6.0, fig_h), dpi=150)
    fig.patch.set_facecolor("#F2F2F7")            # iOS 浅灰
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    # 头部
    ax.text(0.07, 0.965, "60周线预警 · 当日汇总", fontsize=17, weight="bold",
            color="#1C1C1E", va="top")
    ax.text(0.07, 0.905, f"{day} · {_MODE_LABEL.get(mode, mode)}",
            fontsize=10.5, color="#8E8E93", va="top")
    sub = f"触发 {total} 只（已剔除 ST / 退市整理股）"
    if total > shown:
        sub += f"，仅列前 {shown} 只（按贴近 60 周线程度排序）"
    ax.text(0.07, 0.852, sub, fontsize=11, color="#3A3A3C", va="top")
    ax.plot([0.07, 0.93], [0.815, 0.815], color="#D1D1D6", lw=0.8)

    # 表头
    y = 0.775
    cols = [("名称", 0.07, "left"), ("代码", 0.36, "left"),
            ("现价", 0.53, "right"), ("涨跌%", 0.68, "right"),
            ("偏离60周线", 0.93, "right")]
    for label, x0, ha in cols:
        ax.text(x0, y, label, fontsize=8.5, color="#8E8E93", ha=ha, va="top")

    # 数据行（涨红跌绿，奇偶行浅底）；行区在表头 0.775 与底部线 0.10 之间
    step = 0.675 / max(shown, 1)
    for i, (code, r) in enumerate(rows.iterrows()):
        y = 0.775 - (i + 1) * step
        if i % 2 == 1:
            ax.fill_between([0.05, 0.95], y - step * 0.42, y + step * 0.42,
                            color="#FFFFFF", zorder=0)
        name = str(r["name"])[:8]
        chg = _f(r.get("chg_pct"))
        dev = _f(r.get("dev_pct"))
        price = _f(r.get("price"))
        c = UP if chg >= 0 else DOWN
        ax.text(0.07, y, name, fontsize=11, color="#1C1C1E", va="center")
        ax.text(0.36, y, str(code).split(".")[-1], fontsize=9.5,
                color="#8E8E93", va="center")
        ax.text(0.53, y, f"{price:.2f}", fontsize=11, color="#1C1C1E",
                ha="right", va="center")
        ax.text(0.68, y, f"{chg:+.2f}", fontsize=11, color=c, ha="right",
                va="center")
        ax.text(0.93, y, f"{dev:+.2f}%", fontsize=11,
                color=UP if dev >= 0 else DOWN, ha="right", va="center")

    # 底部：声明 + 指引（铁律 #3：信号语义必须保留「非买入信号」）
    ax.plot([0.07, 0.93], [0.085, 0.085], color="#D1D1D6", lw=0.8)
    ax.text(0.07, 0.062, "本信号统计上是「掉队风险提示」而非买入机会。",
            fontsize=9.5, color="#8E8E93", va="top")
    ax.text(0.07, 0.028, "完整名单、周K图与期货/形态见在线看板",
            fontsize=9.5, color="#8E8E93", va="top")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path
