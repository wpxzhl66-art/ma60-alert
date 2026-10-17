"""重要财经新闻：统一采集（同花顺 / 金十数据 / 新浪财经）。

这是项目内**唯一**的新闻实现，同时服务于两个出口：

    build_section()   → 推送消息里的 Markdown 栏目（src/alert/run_alert.py）
    build_dashboard() → dashboard/news.json（src/dashboard/build.py → 看板「最新消息」卡）

历史沿革（为什么合并）
--------------------
此前存在两套并行实现，各有可取之处，也有不一致：

* ``src/alert/news.py``（推送侧，2026-10-17 建）：同花顺走 **HTML 列表页**（GBK，脆弱）；
  有重要性**打分排序**、「同一来源最多占 N 条」配额；合并后**保留最早发布时间**。
* ``src/dashboard/news.py``（看板侧，2026-10-17 建，同日删除）：同花顺走 **JSON 接口**（更稳）；
  有「只保留重要新闻」的**关键词过滤**、源侧 important 标记、2-gram 指纹去重；
  合并后取**最新**时间、内容取最全那条。

⚠️ 合并结论：**以本文件为唯一实现，``src/dashboard/news.py`` 已删除**。
看板侧唯一的调用点 ``src/dashboard/build.py`` 已改为 ``from alert.news import build_dashboard``；
对外 schema（``{generated_at, items[{title,summary,time,url,sources[],important}], sources{}}``）
保持不变，前端 ``dashboard/app.js`` 无需改动。

合并后保留双方有效能力：
  1. 同花顺 **JSON 接口为主、HTML 列表页为辅**（任一路可用即可）；
  2. 金十依次尝试两个 app-id（两套实现各带了一个）；
  3. **过滤**用看板侧的关键词表 + 源标记，**排序**用推送侧的分级权重与时效衰减；
  4. **合并**同时用 2-gram 指纹与字符序列比，并统一为**保留最早发布时间**（用户要求），
     内容取最全那条、来源全部标注；
  5. 源配额（避免单源刷屏）与「过滤后不足则按分值补齐」同时保留。

容错约定：**任一源失败只记录状态，不抛给调用方**；推送栏目在末尾注明失败源，
看板在 sources 里标记 ok=false。全部失败时栏目留空并说明。

用法：
    python -m alert.news --preview      # 打印推送栏目
    python -m alert.news --json         # 结构化输出（含各源状态）
    python -m alert.news --dashboard    # 只构建 dashboard/news.json
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

import requests

from .config import load

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

#: 源标识 -> 展示名（看板 JSON 与推送文案共用，改名只改这里）
SOURCE_NAMES = {
    "tonghuashun": "同花顺",
    "jin10": "金十数据",
    "sina": "新浪财经",
}
#: 金十接口的两个 app-id（两套实现各带一个，依次尝试，先通先用）
JIN10_APP_IDS = ("bVBF4FyRTn5NJF5n", "bJJcMqUEyIrRvBoT")

# ---------------------------------------------------------------- 重要性
#: 过滤用词表（来自看板侧实现）：命中其一才算「重要」
IMPORTANT_WORDS = (
    "央行|国务院|证监会|交易所|美联储|利率|降息|加息|通胀|cpi|pmi|gdp|非农|"
    "就业|原油|油价|黄金|关税|制裁|停复牌|并购|回购|减持|业绩预告|重大|突发|"
    "人民币|汇率|国债|房地产|财政|货币政策|地缘|战争|袭击"
)
IMPORTANT_RE = re.compile(IMPORTANT_WORDS, re.I)

#: 一级打分词（宏观与监管层面的硬事件）
K1 = ("美联储", "降息", "加息", "降准", "LPR", "央行", "政治局", "国常会", "国务院",
      "证监会", "财政部", "发改委", "商务部", "统计局", "关税", "GDP", "CPI", "PPI",
      "PMI", "非农", "利率决议", "汇率", "人民币", "MLF", "社融", "信贷")
#: 二级打分词（市场与产业层面）
K2 = ("A股", "港股", "美股", "黄金", "原油", "油价", "IPO", "退市", "停牌", "立案",
      "财报", "业绩", "回购", "减持", "并购", "重组", "涨价", "订单", "政策",
      "监管", "芯片", "半导体", "新能源", "光伏", "房地产", "券商", "银行", "国债")
#: 突发标记词
K3 = ("突发", "紧急", "重磅", "重要", "辟谣", "澄清", "停牌核查", "风险提示")

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_SCRIPT = re.compile(r"(?is)<script[\s\S]*?</script>|<style[\s\S]*?</style>")


def _clean(s: Any) -> str:
    """去标签、还原 HTML 实体、压缩空白。

    同花顺正文大量使用 &ensp;/&emsp; 做缩进，新浪摘要偶有 &quot; 等，
    统一交给 html.unescape，避免把实体原样推给用户。
    """
    if s is None:
        return ""
    t = _SCRIPT.sub(" ", str(s))
    t = _TAG.sub(" ", t)
    t = html.unescape(t)
    t = t.replace("\u00a0", " ").replace("\u3000", " ")
    return _WS.sub(" ", t).strip()


def _http(url: str, *, timeout: float = 12.0, encoding: str | None = None,
          headers: dict | None = None) -> str:
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        h.update(headers)
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    if encoding:
        r.encoding = encoding
    return r.text


def _ts(value: Any) -> datetime | None:
    """统一时间解析：Unix 秒 或 'YYYY-MM-DD HH:MM:SS'。"""
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(float(value)))
    except (TypeError, ValueError, OSError):
        pass
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


@dataclass
class News:
    """统一的新闻条目。两个出口（Markdown / JSON）都从这里渲染。"""
    title: str
    summary: str
    url: str
    source: str            # 源标识，见 SOURCE_NAMES
    ts: datetime           # 发布时间
    score: float = 0.0     # 重要性分值（越大越靠前）
    flagged: bool = False  # 源侧是否标记重要（同花顺 import=1 / 新浪 level / 金十 important）
    sources: list = field(default_factory=list)   # 合并后的全部来源标识
    merged: int = 1        # 合并了多少条

    @property
    def source_names(self) -> list:
        """去重后的展示名列表（保持顺序）。"""
        names = [SOURCE_NAMES.get(s, s) for s in (self.sources or [self.source])]
        out: list[str] = []
        for n in names:
            if n not in out:
                out.append(n)
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ts"] = self.ts.strftime("%Y-%m-%d %H:%M:%S")
        d["source_names"] = self.source_names
        return d


# ---------------------------------------------------------------- 抓取：同花顺
def _ths_from_json(cfg: dict) -> list[News]:
    """同花顺 JSON 接口（原看板侧实现的主路径，比 HTML 列表页稳）。"""
    limit = int(cfg.get("per_source", 25))
    url = f"https://news.10jqka.com.cn/tapp/news/push/stock/?page=1&limit={max(limit, 20)}"
    raw = _http(url, timeout=float(cfg.get("timeout", 12)),
                headers={"Referer": "https://news.10jqka.com.cn/"})
    rows = ((json.loads(raw).get("data") or {}).get("list")) or []
    out: list[News] = []
    for r in rows:
        ts = _ts(r.get("ctime"))
        title = _clean(r.get("title"))
        link = r.get("url") or r.get("appUrl") or ""
        if not title or ts is None or not link:
            continue
        out.append(News(
            title=title,
            summary=_clean(r.get("digest") or r.get("short") or ""),
            url=link, source="tonghuashun", ts=ts,
            flagged=str(r.get("import", "0")) == "1"))
        if len(out) >= limit:
            break
    return out


def _ths_from_html(cfg: dict) -> list[News]:
    """同花顺列表页（GBK）。JSON 接口不可用时兜底。"""
    limit = int(cfg.get("per_source", 25))
    page = _http("https://news.10jqka.com.cn/today_list/",
                 timeout=float(cfg.get("timeout", 12)), encoding="gbk")
    out: list[News] = []
    seen: set[str] = set()
    # ⚠️ 必须写成 <li[^>]*>：页面里既有裸 <li>（95 处）也有 <li class=…>（30 处），
    # 只匹配裸标签会让块边界错位，摘要就跑到别的条目里、或直接丢失。
    for block in re.findall(r"<li[^>]*>(.*?)</li>", page, re.S):
        m = re.search(r'class="arc-title"[\s\S]*?href="([^"]+)"[^>]*>([\s\S]*?)</a>', block)
        if not m:
            continue
        url, title = m.group(1), _clean(m.group(2))
        if "/c" not in url or len(title) < 6:
            continue
        ym = re.search(r"news\.10jqka\.com\.cn/(\d{8})/", url)
        tm = re.search(r"<span>(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2})</span>", block)
        if not (ym and tm):
            continue
        try:
            ts = datetime(int(ym.group(1)[:4]), int(tm.group(1)), int(tm.group(2)),
                          int(tm.group(3)), int(tm.group(4)))
        except ValueError:
            continue
        # ⚠️ 属性实际是 class="arc-cont news-link"（arc-cont 后不紧跟引号），
        # 写成 class="arc-cont" 会永远匹配不到 → 摘要全丢。
        cont = re.search(r"arc-cont[^>]*>([\s\S]*?)</a>", block)
        if url in seen:
            continue
        seen.add(url)
        out.append(News(title=title, summary=_clean(cont.group(1)) if cont else "",
                        url=url, source="tonghuashun", ts=ts))
        if len(out) >= limit:
            break
    return out


def fetch_tonghuashun(cfg: dict) -> list[News]:
    err: Exception | None = None
    for fn in (_ths_from_json, _ths_from_html):
        try:
            got = fn(cfg)
            if got:
                return got
            err = ValueError("返回空列表")
        except Exception as exc:                       # noqa: BLE001
            err = exc
    raise ValueError(f"同花顺两个入口都失败：{err}")


# ---------------------------------------------------------------- 抓取：金十数据
def fetch_jin10(cfg: dict) -> list[News]:
    """金十快讯。依次尝试两个 app-id；部分网络环境不可达 → 由调用方降级。"""
    limit = int(cfg.get("per_source", 25))
    last: Exception | None = None
    for app_id in JIN10_APP_IDS:
        try:
            raw = _http(
                "https://flash-api.jin10.com/get_flash_list?channel=-8200&vip=1",
                timeout=float(cfg.get("timeout", 12)),
                headers={"x-app-id": app_id, "x-version": "1.0.0",
                         "Referer": "https://www.jin10.com/"})
            payload = json.loads(raw)
        except Exception as exc:                       # noqa: BLE001
            last = exc
            continue
        rows = payload if isinstance(payload, list) else (payload.get("data") or [])
        out: list[News] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            ts = _ts(r.get("time") or r.get("create_time"))
            d = r.get("data") or {}
            body = _clean(d.get("content") or d.get("body") or r.get("content") or "")
            title = _clean(d.get("title") or r.get("title") or "")
            text = body or title
            if not text or ts is None:
                continue
            rid = str(r.get("id") or "")
            out.append(News(
                title=title or (text[:28] + ("…" if len(text) > 28 else "")),
                summary=text, source="jin10", ts=ts,
                url=(f"https://www.jin10.com/flash/{rid}" if rid
                     else "https://www.jin10.com/"),
                flagged=bool(r.get("important"))))
            if len(out) >= limit:
                break
        if out:
            return out
    raise ValueError(f"金十数据未取到快讯：{last}")


# ---------------------------------------------------------------- 抓取：新浪财经
def fetch_sina(cfg: dict) -> list[News]:
    limit = int(cfg.get("per_source", 25))
    lid = int(cfg.get("sina_lid", 2516))
    url = (f"https://feed.mix.sina.com.cn/api/roll/get"
           f"?pageid=153&lid={lid}&num={max(limit, 20)}&page=1")
    raw = _http(url, timeout=float(cfg.get("timeout", 12)),
                headers={"Referer": "https://finance.sina.com.cn/7x24/notification.shtml"})
    data = ((json.loads(raw).get("result") or {}).get("data")) or []
    out: list[News] = []
    for x in data:
        title = _clean(x.get("title"))
        link = x.get("url") or x.get("wapurl") or ""
        ts = _ts(x.get("ctime") or x.get("mtime") or x.get("intime"))
        if not title or not link or ts is None:
            continue
        out.append(News(
            title=title,
            summary=_clean(x.get("intro") or x.get("summary") or x.get("wapsummary") or ""),
            url=link, source="sina", ts=ts,
            flagged=str(x.get("level", "0")) in {"1", "2"}))   # level 1/2 = 编辑标记的重要稿
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 打分 / 相似度 / 合并
def score_one(n: News, now: datetime) -> float:
    """分级关键词权重 + 突发标记 + 时效衰减（来自推送侧的排序能力）。"""
    text = (n.title or "") + " " + (n.summary or "")
    s = 0.0
    for k in K1:
        if k in text:
            s += 3.0
    for k in K2:
        if k in text:
            s += 2.0
    for k in K3:
        if k in (n.title or ""):
            s += 2.0
    age_h = max(0.0, (now - n.ts).total_seconds() / 3600.0)
    s -= min(3.0, age_h / 8.0)          # 24h 内最多扣 3 分，越新越靠前
    if len(n.summary or "") >= 40:
        s += 0.5
    if n.flagged:
        s += 1.0
    return round(s, 2)


def is_important(n: News) -> bool:
    """是否算「重要」：源侧标记 或 命中关键词表（来自看板侧的过滤能力）。"""
    return n.flagged or bool(IMPORTANT_RE.search((n.title or "") + " " + (n.summary or "")))


def _fingerprint(text: str) -> set[str]:
    """中文按二元词切分，英文与数字保留为词（对不同媒体的措辞保持容错）。"""
    t = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text or "")
    grams = {t[i:i + 2] for i in range(max(0, len(t) - 1))}
    grams.update(re.findall(r"[A-Za-z]{2,}|\d+(?:\.\d+)?", t.lower()))
    return grams


def similar(a: News, b: News, ratio_thr: float, overlap_thr: float) -> bool:
    """是否判为同一事件：2-gram 重合度 或 标题字符序列比 或 标题互相包含。"""
    ta, tb = a.title or "", b.title or ""
    if ta and tb and (ta in tb or tb in ta):
        return True
    if SequenceMatcher(None, ta, tb).ratio() >= ratio_thr:
        return True
    ga = _fingerprint(ta + " " + (a.summary or ""))
    gb = _fingerprint(tb + " " + (b.summary or ""))
    if ga and gb and len(ga & gb) / max(1, len(ga | gb)) >= overlap_thr:
        return True
    return False


def merge_similar(items: list[News], ratio_thr: float, overlap_thr: float) -> list[News]:
    """同类聚簇：保留**最早**发布时间（用户要求），内容取最全，来源全部标注。"""
    clusters: list[list[News]] = []
    for n in sorted(items, key=lambda x: x.ts):        # 从早到晚
        hit = None
        for c in clusters:
            if any(similar(n, old, ratio_thr, overlap_thr) for old in c):
                hit = c
                break
        if hit is None:
            clusters.append([n])
        else:
            hit.append(n)
    out: list[News] = []
    for c in clusters:
        if len(c) == 1:
            out.append(c[0])
            continue
        rep = min(c, key=lambda x: x.ts)              # 最早发布
        rep.summary = max(c, key=lambda x: len(x.summary or "")).summary or rep.summary
        rep.score = max(x.score for x in c)
        rep.flagged = any(x.flagged for x in c)
        rep.sources = sorted({s for x in c for s in ([x.source] + (x.sources or []))})
        rep.merged = len(c)
        out.append(rep)
    return out


def _take(ranked: list[News], max_items: int, cap: int) -> list[News]:
    """取前 N 条，并限制同一来源最多占 cap 条（cap<=0 不限）。

    完全按分值排会让某个「评分体系更吃香」的源包揽版面（实测新浪海外宏观稿
    关键词命中率高，一度占满 8 条），那样三源采集就失去意义。
    """
    if max_items <= 0:
        return list(ranked)
    if cap <= 0:
        return ranked[:max_items]
    used: dict[str, int] = {}
    picked: list[News] = []
    rest: list[News] = []
    for n in ranked:
        if len(picked) < max_items and used.get(n.source, 0) < cap:
            picked.append(n)
            used[n.source] = used.get(n.source, 0) + 1
        else:
            rest.append(n)
    if len(picked) < max_items and rest:               # 某源不足时按分值补位
        picked.extend(rest[:max_items - len(picked)])
    return picked


# ---------------------------------------------------------------- 采集（唯一入口）
FETCHERS: dict[str, Callable[[dict], list[News]]] = {
    "tonghuashun": fetch_tonghuashun,
    "jin10": fetch_jin10,
    "sina": fetch_sina,
}


def _cfg() -> dict:
    return dict(load().get("news") or {})


def collect(cfg: dict | None = None) -> tuple[list[News], dict[str, dict]]:
    """采集三源并做筛选、合并、排序。

    返回 ``(items, status)``。``status`` 形如
    ``{"同花顺": {"ok": True, "count": 25}, "金十数据": {"ok": False, "error": "…"}}``，
    供推送栏目与看板卡片各自渲染失败提示。
    """
    cfg = cfg or _cfg()
    enabled = cfg.get("sources") or {}
    window_h = float(cfg.get("window_hours", 24))
    now = datetime.now()
    raw: list[News] = []
    status: dict[str, dict] = {}
    for key, fn in FETCHERS.items():
        name = SOURCE_NAMES[key]
        if not bool(enabled.get(key, True)):
            status[name] = {"ok": False, "count": 0, "error": "已在配置中停用"}
            continue
        try:
            got = fn(cfg)
        except Exception as exc:                       # noqa: BLE001
            print(f"[news] {name} 抓取失败：{type(exc).__name__} {exc}")
            status[name] = {"ok": False, "count": 0, "error": str(exc)[:120]}
            continue
        kept = [n for n in got if (now - n.ts) <= timedelta(hours=window_h)]
        raw.extend(kept)
        status[name] = {"ok": True, "count": len(kept)}
        print(f"[news] {name} 取到 {len(got)} 条，{window_h:.0f} 小时内 {len(kept)} 条")
    if not raw:
        return [], status

    for n in raw:
        n.score = score_one(n, now)
    merged = merge_similar(raw, float(cfg.get("sim_ratio", 0.62)),
                           float(cfg.get("sim_overlap", 0.38)))

    # 只看「重要」的（看板侧能力）；过滤后不足 min_items 时按分值补齐，
    # 避免关键词表偏窄时栏目空白。
    important = [n for n in merged if is_important(n)] \
        if bool(cfg.get("require_important", True)) else list(merged)
    min_items = int(cfg.get("min_items", 4))
    if len(important) < min_items:
        seen_ids = {id(x) for x in important}
        rest = [n for n in merged if id(n) not in seen_ids]
        rest.sort(key=lambda n: (-n.score, -n.ts.timestamp()))
        important = important + rest[:min_items - len(important)]

    important.sort(key=lambda n: (-int(n.flagged), -n.score, -n.ts.timestamp()))
    return important, status


def select(cfg: dict | None = None, max_items: int | None = None) -> tuple[list[News], dict]:
    """``collect()`` + 截断 + 源配额。两个出口共用。"""
    cfg = cfg or _cfg()
    items, status = collect(cfg)
    if not items:
        return [], status
    n = int(max_items if max_items is not None else cfg.get("max_items", 8))
    return _take(items, n, int(cfg.get("max_per_source", 5))), status


# ---------------------------------------------------------------- 出口一：推送栏目
def build_section(cfg: dict | None = None) -> tuple[str, int]:
    """生成推送用的 Markdown 段落，返回 ``(文本, 条数)``。

    任何失败都被吞掉并体现在文本里，绝不抛给调用方（推送不许被新闻打断）。
    """
    cfg = cfg or _cfg()
    if not bool(cfg.get("enabled", True)):
        return "", 0
    head = str(cfg.get("section_title") or "重要财经新闻").strip()
    items, status = select(cfg)
    failed = [k for k, v in status.items() if not v.get("ok")]
    lines = [f"## {head}（{datetime.now():%m-%d %H:%M}）", ""]
    if not items:
        lines.append("本期未抓到可推送的财经新闻"
                     + (f"（{'、'.join(failed)}暂不可用）" if failed else "") + "。")
        return "\n".join(lines), 0
    for i, n in enumerate(items, 1):
        title = f"**{i}. {n.title}**"
        if len(n.source_names) > 1:
            title += f"（{len(n.source_names)} 家报道）"
        lines.append(title)
        lines.append(f"`{' · '.join(n.source_names)}` · {n.ts:%m-%d %H:%M}")
        if n.summary:
            lines.append(n.summary[:160] + ("…" if len(n.summary) > 160 else ""))
        lines.append(f"[原文]({n.url})")
        lines.append("")
    if failed:
        lines.append("> ⚠️ " + "、".join(failed) +
                     "本次抓取失败，以上为其余来源；故障不影响后续推送。")
    return "\n".join(lines).strip(), len(items)


# ---------------------------------------------------------------- 出口二：看板 news.json
def build_dashboard(cfg: dict | None = None, out_dir: str | None = None) -> dict:
    """生成 ``dashboard/news.json``（schema 与原看板实现一致，前端无需改动）。

    ``{generated_at, items:[{title,summary,time,url,sources:[{name,url}],important}],
        sources:{名称:{ok,count,error}}}``
    """
    cfg = cfg or _cfg()
    target_dir = Path(out_dir) if out_dir else (Path(__file__).resolve().parents[2] / "dashboard")
    status: dict[str, dict] = {}
    obj: dict[str, Any] = {"generated_at": datetime.now().isoformat(timespec="seconds"),
                           "items": [], "sources": status}
    if bool(cfg.get("enabled", True)):
        items, status = select(cfg, max_items=int(cfg.get("dashboard_max_items", 15)))
        obj["sources"] = status
        obj["items"] = [{
            "title": n.title[:140],
            "summary": n.summary[:260],
            "time": n.ts.strftime("%Y-%m-%d %H:%M"),
            "url": n.url,
            "sources": [{"name": s, "url": n.url} for s in n.source_names],
            "important": bool(n.flagged),
        } for n in items]
    target = target_dir / "news.json"
    content = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
    # 内容没变就不重写，避免每次构建都产生一次无意义的仓库提交
    if not target.exists() or target.read_text(encoding="utf-8") != content:
        target.write_text(content, encoding="utf-8")
    print("      财经新闻 " + " / ".join(
        f"{k} {v.get('count', 0)}" for k, v in status.items()) +
        f" · 合并后 {len(obj['items'])} 条")
    return obj


def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="重要财经新闻（统一实现）")
    ap.add_argument("--preview", action="store_true", help="打印推送用 Markdown 栏目")
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON")
    ap.add_argument("--dashboard", action="store_true", help="只构建 dashboard/news.json")
    a = ap.parse_args()
    if a.dashboard:
        obj = build_dashboard()
        print(f"已写入 dashboard/news.json：{len(obj['items'])} 条")
        return 0
    items, status = select()
    if a.json or not a.preview:
        print(json.dumps({
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(items), "sources": status,
            "items": [n.to_dict() for n in items],
        }, ensure_ascii=False, indent=1))
    if a.preview:
        print(build_section()[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
