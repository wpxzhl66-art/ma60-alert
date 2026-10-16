"""重要财经新闻采集（同花顺 / 金十数据 / 新浪财经）。

设计要点
--------
1. **三源独立、互不牵连**：任一源超时/解析失败只记录到 failures，其余照常输出。
2. **统一字段**：发布时间、来源、标题、摘要、原文链接（``News`` 数据类）。
3. **相似合并**：同一事件的多来源报道按标题相似度聚成一簇，
   保留** earliest **发布时间，并把所有来源都标注出来。
4. **排序**：先按「重要性」分值，分值相同按发布时间倒序。

这里的解析全部基于公开可读的新闻列表页/开放 feed 接口，只取列表可见的
标题与摘要，不做深层爬取，也不突破任何登录或付费限制。

用法：
    python -m alert.news --preview              # 打印要推送的新闻段落
    python -m alert.news --json                 # 输出结构化 JSON
"""
from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

import requests

from .config import load

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

#: 源标识 -> 展示名
SOURCE_NAMES = {
    "tonghuashun": "同花顺",
    "jin10": "金十数据",
    "sina": "新浪财经",
}

# ---------------------------------------------------------------- 重要性
#: 一级关键词：宏观与监管层面的硬事件
K1 = ("美联储", "降息", "加息", "降准", "LPR", "央行", "政治局", "国常会", "国务院",
      "证监会", "财政部", "发改委", "商务部", "统计局", "关税", "GDP", "CPI", "PPI",
      "PMI", "非农", "利率决议", "汇率", "人民币", "MLF", "社融", "信贷")
#: 二级关键词：市场与产业层面的事件
K2 = ("A股", "港股", "美股", "黄金", "原油", "油价", "IPO", "退市", "停牌", "立案",
      "财报", "业绩", "回购", "减持", "并购", "重组", "涨价", "订单", "政策",
      "监管", "芯片", "半导体", "新能源", "光伏", "房地产", "券商", "银行", "国债")
#: 突发/重要性标记词
K3 = ("突发", "紧急", "重磅", "重要", "辟谣", "澄清", "停牌核查", "风险提示")

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _clean(s: str) -> str:
    """去标签、还原 HTML 实体、压缩空白。

    同花顺正文里大量使用 &ensp;/&emsp; 做缩进，新浪摘要里偶有 &quot; 等，
    统一交给 html.unescape 处理，避免把实体原样推给用户。
    """
    if not s:
        return ""
    s = re.sub(r"(?is)<script[\s\S]*?</script>", " ", str(s))
    s = re.sub(r"(?is)<style[\s\S]*?</style>", " ", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)                       # &ensp; &amp; &quot; &#39; …
    # 不间断空格 / 全角空格 → 半角空格
    s = s.replace("\u00a0", " ").replace("\u3000", " ")
    s = _WS.sub(" ", s).strip()
    return s


def _http(url: str, *, timeout: float = 12.0, encoding: str | None = None,
          headers: dict | None = None) -> str:
    """GET 取文本；非 2xx 抛异常（由调用方降级）。"""
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        h.update(headers)
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    if encoding:
        r.encoding = encoding
    return r.text


@dataclass
class News:
    title: str
    summary: str
    url: str
    source: str            # 来源标识
    ts: datetime           # 发布时间
    score: float = 0.0     # 重要性分值
    sources: list = field(default_factory=list)   # 合并后的全部来源
    merged: int = 1        # 合并了多少条

    @property
    def source_names(self) -> list:
        names = [SOURCE_NAMES.get(s, s) for s in (self.sources or [self.source])]
        # 去重并保持顺序
        out = []
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
def fetch_tonghuashun(cfg: dict) -> list[News]:
    """同花顺「今日热点」列表页（GBK 编码）。"""
    n_max = int(cfg.get("per_source", 25))
    html = _http("https://news.10jqka.com.cn/today_list/",
                 timeout=float(cfg.get("timeout", 12)), encoding="gbk")
    out: list[News] = []
    seen: set[str] = set()
    # ⚠️ 必须写成 <li[^>]*>：页面里既有裸 <li>（95 处）也有带属性的 <li class=…>（30 处），
    # 只匹配裸标签会让后者的块边界错位，摘要就跑到别的条目里、或直接丢失。
    for block in re.findall(r"<li[^>]*>(.*?)</li>", html, re.S):
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
        yyyy = ym.group(1)[:4]
        try:
            ts = datetime(int(yyyy), int(tm.group(1)), int(tm.group(2)),
                          int(tm.group(3)), int(tm.group(4)))
        except ValueError:
            continue
        # ⚠️ 属性实际写作 class="arc-cont news-link"（arc-cont 后不紧跟引号），
        # 写成 class="arc-cont" 会永远匹配不到 → 摘要全丢。这里不依赖 class 值边界。
        cont = re.search(r'arc-cont[^>]*>([\s\S]*?)</a>', block)
        summary = _clean(cont.group(1)) if cont else ""
        if url in seen:
            continue
        seen.add(url)
        out.append(News(title=title, summary=summary, url=url,
                        source="tonghuashun", ts=ts))
        if len(out) >= n_max:
            break
    if not out:
        raise ValueError("同花顺列表解析为空（页面结构可能已变更）")
    return out


# ---------------------------------------------------------------- 抓取：金十数据
def fetch_jin10(cfg: dict) -> list[News]:
    """金十数据快讯（flash 接口，需 x-app-id 头）。

    ⚠️ 该域名在部分网络环境下不可达（本机实测连接被重置）。
    失败时会抛异常，由 ``collect()`` 降级为「栏目提示」，不影响其余源。
    """
    n_max = int(cfg.get("per_source", 25))
    headers = {"x-app-id": "bJJcMqUEyIrRvBoT", "x-version": "1.0.0",
               "Referer": "https://www.jin10.com/"}
    url = ("https://flash-api.jin10.com/get_flash_list"
           "?channel=-8200&vip=1")
    raw = _http(url + ("&max_time=" + datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                       if cfg.get("jin10_max_time") else ""),
                timeout=float(cfg.get("timeout", 12)), headers=headers)
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"金十返回非 JSON：{exc}") from exc

    rows = payload if isinstance(payload, list) else (payload.get("data") or [])
    out: list[News] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        tstr = r.get("time") or r.get("create_time") or ""
        try:
            ts = datetime.strptime(str(tstr)[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        d = r.get("data") or {}
        # 快讯正文优先；无正文则用 title
        body = d.get("content") or d.get("body") or r.get("content") or ""
        title = d.get("title") or r.get("title") or ""
        text = _clean(body) or _clean(title)
        if not text:
            continue
        rid = str(r.get("id") or "")
        out.append(News(
            title=(_clean(title) or (text[:28] + ("…" if len(text) > 28 else ""))),
            summary=text,
            url=(f"https://www.jin10.com/flash/{rid}" if rid else "https://www.jin10.com/"),
            source="jin10", ts=ts))
        if len(out) >= n_max:
            break
    if not out:
        raise ValueError("金十数据未返回可解析的快讯")
    return out


# ---------------------------------------------------------------- 抓取：新浪财经
def fetch_sina(cfg: dict) -> list[News]:
    """新浪财经滚动新闻（roll 接口，返回 JSON 及其在这次 Dropdown 上的 meta。

    lid=2516 为「全球财经」滚动，可按配置换成其它 lid。
    """
    lid = int(cfg.get("sina_lid", 2516))
    n_max = int(cfg.get("per_source", 25))
    url = (f"https://feed.mix.sina.com.cn/api/roll/get"
           f"?pageid=153&lid={lid}&num={max(n_max, 10)}&page=1")
    raw = _http(url, timeout=float(cfg.get("timeout", 12)))
    payload = json.loads(raw)
    data = ((payload.get("result") or {}).get("data")) or []
    out: list[News] = []
    for x in data:
        title = _clean(x.get("title"))
        summary = _clean(x.get("intro") or x.get("summary") or x.get("wapsummary") or "")
        link = x.get("url") or x.get("wapurl") or ""
        if not title or not link:
            continue
        ts = None
        for k in ("ctime", "mtime", "intime"):
            v = x.get(k)
            if v:
                try:
                    ts = datetime.fromtimestamp(int(v))
                    break
                except (TypeError, ValueError, OSError):
                    continue
        if ts is None:
            continue
        out.append(News(title=title, summary=summary, url=link,
                        source="sina", ts=ts))
        if len(out) >= n_max:
            break
    if not out:
        raise ValueError("新浪财经返回为空")
    return out


# ---------------------------------------------------------------- 重要性 & 相似合并
def score_one(n: News, now: datetime) -> float:
    """重要性分值：关键词命中 + 突发标记 + 时效衰减。"""
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
    # 24 小时内线性衰减最多扣 3 分（越新越靠前）
    s -= min(3.0, age_h / 8.0)
    # 有摘要的更可读，微弱加权
    if len(n.summary or "") >= 40:
        s += 0.5
    return round(s, 2)


_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+")


def _norm_title(t: str) -> str:
    return _PUNCT.sub("", (t or "").lower())


def _similarity(a: str, b: str) -> float:
    """标题相似度：字符序列比 + 词集合 Jaccard，取较大者。"""
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    seq = SequenceMatcher(None, na, nb).ratio()
    sa, sb = set(na), set(nb)
    # 2-gram 集合更稳（避免单字噪声）
    ga = {na[i:i + 2] for i in range(len(na) - 1)}
    gb = {nb[i:i + 2] for i in range(len(nb) - 1)}
    jac = len(ga & gb) / max(1, len(ga | gb))
    jac_chr = len(sa & sb) / max(1, len(sa | sb))
    return max(seq, jac, jac_chr)


def merge_similar(items: list[News], threshold: float) -> list[News]:
    """把高度相似的报道聚成一簇：保留最早发布时间，汇总所有来源。"""
    items = sorted(items, key=lambda n: n.ts)   # 从早到晚
    clusters: list[list[News]] = []
    for n in items:
        hit = None
        for c in clusters:
            if _similarity(n.title, c[0].title) >= threshold:
                hit = c
                break
        if hit is None:
            clusters.append([n])
        else:
            hit.append(n)
    merged: list[News] = []
    for c in clusters:
        if len(c) == 1:
            merged.append(c[0])
            continue
        # 保留最早的作为代表条，来源汇总、摘要取最全的一条
        c_sorted = sorted(c, key=lambda x: x.ts)
        rep = c_sorted[0]
        longest = max(c, key=lambda x: len(x.summary or ""))
        best = max(c, key=lambda x: x.score)
        rep.summary = longest.summary or rep.summary
        rep.score = best.score
        rep.sources = sorted({s for x in c for s in ([x.source] + (x.sources or []))})
        rep.merged = len(c)
        merged.append(rep)
    return merged


# ---------------------------------------------------------------- 汇总入口
FETCHERS = {
    "tonghuashun": fetch_tonghuashun,
    "jin10": fetch_jin10,
    "sina": fetch_sina,
}


def _cfg() -> dict:
    return dict(load().get("news") or {})


def collect(cfg: dict | None = None) -> tuple[list[News], list[str], list[str]]:
    """采集三源。

    返回 ``(items, failures, disabled)``：
      * ``items``  去重合并后排好序的新闻（已按 ``max_items`` 截断）
      * ``failures`` 失败源的展示名列表（用于栏目内提示）
      * ``disabled`` 配置里被关闭的源
    """
    cfg = cfg or _cfg()
    enabled = cfg.get("sources") or {}
    window_h = float(cfg.get("window_hours", 24))
    threshold = float(cfg.get("sim_threshold", 0.62))
    max_items = int(cfg.get("max_items", 8))
    cap = int(cfg.get("max_per_source", 5))
    now = datetime.now()
    items: list[News] = []
    failures: list[str] = []
    disabled: list[str] = []
    for key, fn in FETCHERS.items():
        name = SOURCE_NAMES[key]
        if not bool(enabled.get(key, True)):
            disabled.append(name)
            continue
        try:
            got = fn(cfg)
        except Exception as exc:                     # noqa: BLE001
            print(f"[news] {name} 抓取失败：{type(exc).__name__} {exc}")
            failures.append(name)
            continue
        kept = [n for n in got if (now - n.ts) <= timedelta(hours=window_h)]
        items.extend(kept)
        print(f"[news] {name} 取到 {len(got)} 条，{window_h:.0f} 小时内 {len(kept)} 条")
    if not items:
        return [], failures, disabled
    for n in items:
        n.score = score_one(n, now)
    merged = merge_similar(items, threshold)
    merged.sort(key=lambda n: (-n.score, -n.ts.timestamp()))
    return _take(merged, max_items, cap), failures, disabled


def _take(ranked: list[News], max_items: int, cap: int) -> list[News]:
    """取前 N 条，并限制**同一来源**最多占 cap 条（cap<=0 表示不限）。

    为什么需要配额：完全按分值排会让某个「评分体系更吃香」的源包揽版面
    （实测新浪的海外宏观稿keywords 命中率高，一度占满 8 条），那样三源采集
    就失去了意义。配额用完后，剩余名额由其它源按分值补位；
    若某源失败或内容不足，名额自动让给其余源，不会浪费。
    """
    if max_items <= 0:
        return list(ranked)
    if cap <= 0:
        return ranked[:max_items]
    used: dict[str, int] = {}
    picked: list[News] = []
    rest: list[News] = []
    for n in ranked:
        key = n.source
        if len(picked) < max_items and used.get(key, 0) < cap:
            picked.append(n)
            used[key] = used.get(key, 0) + 1
        else:
            rest.append(n)
    # 名额没占满（某源内容不足）时，放宽配额按分值补位
    if len(picked) < max_items and rest:
        picked.extend(rest[:max_items - len(picked)])
    return picked


def build_section(cfg: dict | None = None) -> tuple[str, int]:
    """生成推送用的 Markdown 段落。返回 ``(文本, 条数)``。

    无论成功失败都返回字符串：全部源失败时给出提示而不是抛异常。
    """
    cfg = cfg or _cfg()
    title_head = str(cfg.get("section_title") or "重要财经新闻").strip()
    if not bool(cfg.get("enabled", True)):
        return "", 0
    items, failures, _disabled = collect(cfg)
    now_txt = datetime.now().strftime("%m-%d %H:%M")
    lines = [f"## {title_head}（{now_txt}）", ""]
    if not items:
        lines.append("本期未抓到可推送的财经新闻" +
                     ("（" + "、".join(failures) + "暂不可用）" if failures else "") + "。")
        return "\n".join(lines), 0
    for i, n in enumerate(items, 1):
        srcs = " · ".join(n.source_names)
        urls = n.url
        head = f"**{i}. {n.title}**"
        if n.merged > 1 and len(n.source_names) > 1:
            head += f"（{len(n.source_names)} 家报道）"
        lines.append(head)
        lines.append(f"`{srcs}` · {n.ts.strftime('%m-%d %H:%M')}")
        if n.summary:
            s = n.summary
            lines.append(s[:160] + ("…" if len(s) > 160 else ""))
        lines.append(f"[原文]({urls})")
        lines.append("")
    if failures:
        lines.append("> ⚠️ " + "、".join(failures) +
                     "本次抓取失败，以上为其余来源；故障不影响后续推送。")
    return "\n".join(lines).strip(), len(items)


def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="重要财经新闻采集")
    ap.add_argument("--preview", action="store_true", help="打印推送用 Markdown 段落")
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON")
    args = ap.parse_args()
    items, failures, disabled = collect()
    if args.json:
        print(json.dumps({
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(items), "failures": failures, "disabled": disabled,
            "items": [n.to_dict() for n in items],
        }, ensure_ascii=False, indent=1))
    else:
        if args.preview:
            print(build_section()[0])
        else:
            for n in items:
                print(f"[{n.score:>5}] {n.ts:%m-%d %H:%M} {','.join(n.source_names)} "
                      f"×{n.merged} | {n.title[:40]}")
            print(f"共 {len(items)} 条｜失败源：{failures or '无'}｜已关闭源：{disabled or '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
