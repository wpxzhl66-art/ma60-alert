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
                        out_path: Path, hfq_note: bool = True) -> Path:
    """weekly: index=周期末Timestamp, values=周收盘（后复权）。

    自动计算 MA60 并标注「最近一个已完成周是否处于上穿状态」。
    """
    setup_font()
    w = weekly.dropna().tail(N_WEEKS)
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
    note = "后复权价格" if hfq_note else ""
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
