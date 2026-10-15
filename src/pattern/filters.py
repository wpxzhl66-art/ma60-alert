"""右侧反转形态 · 实盘后置过滤（解禁 · 重大利空）。

**为什么不在 core.py 里**（层次划分）：
  * 引擎级硬筛（流通市值区间、ST/退市名称）→ ``core.py``，回测与实盘**通用**；
  * 本模块的解禁与利空 → **仅实盘**。理由：解禁接口只能按「未来日期」查，
    14 年回测无法还原「当时尚未发生的解禁」；公告接口也不提供足够长的历史。
    写进 core 会让回测用上未来信息，得出虚假结论。

数据源：东财数据中心（2026-10-16 实测可用；唯 ``push2.*`` 被本机代理拦，本模块不用它）
  * 解禁  ``datacenter-web.eastmoney.com`` reportName=RPT_LIFT_STAGE
  * 公告  ``np-anotice-stock.eastmoney.com`` /api/security/ann

只对**形态候选股**（通常 30~60 只）逐只查，不做全市场扫描。

⚠️ 两条硬约定：
  1. **判标题、不判公告类型**。类型名会骗人：
     「股票交易异常波动」「风险提示公告」是形态候选股的**常态**（形态本身就要求
     近期大阳线），把它们当利空会把整张名单清空；「资金占用」多为半年报标准
     披露文件（几乎每家公司都有）。所以只用标题关键词。
  2. **查不到就放行**。任何一项数据取不到，都不因「数据缺失」误杀标的，
     而是在返回里记 ``unverified``，由调用方在 lag 文案里注明。
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta

import requests

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
DATA_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
ANN_API = "https://np-anotice-stock.eastmoney.com/api/security/ann"

# ---------------- 解禁 ----------------
LIFT_WINDOW_DAYS = 60     # 未来 N 天内的解禁纳入考量（解禁前 1~2 个月市场就会提前反应）
LIFT_RATIO_MAX = 0.15     # 解禁市值 / 当前流通市值 >= 该比例 → 剔除
LIFT_BATCH = 30           # 一次请求查多少只（URL 长度考虑）

# ---------------- 重大利空 ----------------
# 每条规则带**各自的回溯窗口**：定性重罚（退市、立案、处罚、非标）是一年以上的
# 长期阴影；业绩类信息随下一个报告期失效，看 120 天（覆盖一个季报周期）；
# 债务/冻结/停产介于两者之间。
# ⚠️ 统一用一个窗口会两头出错：窗口短了会把三个月前的业绩预亏放过（实测 90 天内
#    这批候选股一只都没命中，等于没筛）；窗口长了会把去年已整改的处罚一直挂着。
NEGATIVE_RULES: tuple[tuple[str, str, int], ...] = (
    ("退市风险", r"退市风险警示|终止上市|暂停上市|强制退市|进入退市整理", 365),
    ("立案调查", r"立案调查|立案告知|被立案|立案侦查", 365),
    ("监管处罚", r"行政处罚|市场禁入|公开谴责|通报批评|监管函|警示函|纪律处分", 365),
    ("财务非标", r"非标准?审计|无法表示意见|保留意见|否定意见|会计差错|追溯重述", 365),
    ("业绩恶化", r"业绩预亏|预计亏损|首亏|续亏|由盈转亏|业绩大幅下滑", 120),
    ("债务风险", r"债务违约|未能清偿|无法偿还|资金链断裂", 180),
    ("股权冻结", r"司法冻结|轮候冻结|股份被冻结|股份冻结|强制平仓|被动减持", 180),
    ("经营中断", r"停产|重大安全事故|被查封|吊销许可|银行账户被冻结", 180),
)

# 抓取窗口 = 最长的规则窗口（短窗口规则在判定时各自再收窄）
NEWS_WINDOW_DAYS = max(d for _, _, d in NEGATIVE_RULES)
NEWS_PAGE = 200           # 单只股票取多少条公告（倒序，足够覆盖一年）

# 负向词：标题含这些说明是**利好或中性**，必须排除，否则规则会反向误杀。
# 「无保留意见」含「保留意见」、「解除质押/冻结」含「冻结」——不排除就全错。
NEG_EXCLUDE = re.compile(r"无保留意见|解除|已撤销|撤销|不存在|未发生|终止减持")

# 「可能被终止上市 / 可能被实施退市风险警示」这类是**风险提示**（年报前的常规提示），
# 只在个股市**当前**名称仍带风险标识时才当真。理由：实测 002058 的历史公告抬头是
# 「*ST威尔」，而它现已重组改名「紫竹高科」——退市风险已解除，若照剔会把刚摘帽的
# 好公司误杀。已实施/已决定终止上市则属既成事实，不受此限制。
HINT_ONLY = re.compile(r"可能被终止上市|可能被实施退市风险警示|可能被暂停上市")

# 软信号：不剔除，只记录在候选股上供人工参考
# （用户 2026-10-16 明确：大股东/实控人减持**只标记不剔除**——减持公告里
#   「高管小额减持」「已完成减持」占比不小，一概剔除会误杀）
SOFT_RULES: tuple[tuple[str, str], ...] = (
    ("减持", r"减持"),
    ("问询/关注函", r"问询函|关注函"),
    ("新增质押", r"质押"),
)

_NEG_RE = tuple((tag, re.compile(pat), days) for tag, pat, days in NEGATIVE_RULES)
_SOFT_RE = tuple((tag, re.compile(pat)) for tag, pat in SOFT_RULES)


def _get(url: str, params: dict, tries: int = 2, check_msg: bool = False) -> dict:
    """带重试的 GET（东财偶发 5xx / 连接重置）。

    ``check_msg=True``：东财数据中心在**参数语法错**时 HTTP 仍是 200，只在
    ``message`` 里报错、``result`` 为 null —— 不校验就会把「查询失败」静默当成
    「查无结果」。解禁/利空过滤一旦静默失效，等于筛除条件形同虚设，
    所以这类接口必须显式校验。
    """
    last: Exception | None = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=20)
            r.raise_for_status()
            js = r.json()
            if check_msg:
                # "返回数据为空"是**正常空结果**（该股确实没有解禁），不是错误；
                # 只有语法类错误（如 InputMismatchException）才必须抛出来。
                msg = str(js.get("message") or "ok")
                if msg not in ("ok", "", "返回数据为空"):
                    raise RuntimeError(f"接口报错：{msg}")
            return js
        except Exception as exc:                          # noqa: BLE001
            last = exc
            if i + 1 < tries:
                time.sleep(0.6 * (i + 1))
    raise last if last else RuntimeError("request failed")


def _code6(code: str) -> str:
    """``sh.600000`` → ``600000``（东财接口用 6 位码）。"""
    return code.split(".")[-1]


def fetch_lift(codes6: list[str], today: str) -> dict[str, list[dict]]:
    """未来 ``LIFT_WINDOW_DAYS`` 天的解禁记录，按 6 位代码分组。

    ⚠️ filter 语法：东财的表达式解析器**不认 ``and``**（会抛
    ``InputMismatchException`` 且 HTTP 仍 200），多条件必须写成**连续括号分组**：
    ``((A)(B)(C))``。这种写法错了不会报错、只会返回空 —— 用 ``and`` 拼过一版，
    结果「解禁命中 0 只」是假阴性，等于这条筛除条件完全没生效。
    """
    end = (date.fromisoformat(today) + timedelta(days=LIFT_WINDOW_DAYS)).isoformat()
    out: dict[str, list[dict]] = {}
    for i in range(0, len(codes6), LIFT_BATCH):
        batch = codes6[i:i + LIFT_BATCH]
        codes_in = ",".join(f'"{c}"' for c in batch)
        flt = (f"((FREE_DATE>='{today}')(FREE_DATE<='{end}')"
               f"(SECURITY_CODE in ({codes_in})))")
        js = _get(DATA_API, {
            "reportName": "RPT_LIFT_STAGE",
            "columns": ("SECURITY_CODE,SECURITY_NAME_ABBR,FREE_DATE,"
                        "LIFT_MARKET_CAP,FREE_RATIO,FREE_SHARES_TYPE"),
            "pageSize": 500, "sortColumns": "FREE_DATE", "sortTypes": 1,
            "filter": flt,
        }, check_msg=True)
        for d in ((js.get("result") or {}).get("data") or []):
            out.setdefault(str(d.get("SECURITY_CODE")), []).append(d)
    return out


def fetch_news(codes6: list[str], today: str) -> dict[str, list[dict]]:
    """近 ``NEWS_WINDOW_DAYS`` 天的公告（标题 + 日期），按 6 位代码分组。

    逐只查而不是批量：批量接口按时间倒序返回**混合**流的列表，
    单只股票的公告可能被挤出页外，漏掉关键利空。
    """
    since = (date.fromisoformat(today) - timedelta(days=NEWS_WINDOW_DAYS)).isoformat()
    out: dict[str, list[dict]] = {}
    for c in codes6:
        js = _get(ANN_API, {
            "sr": -1, "page_size": NEWS_PAGE, "page_index": 1,
            "ann_type": "A", "client_source": "web", "stock_list": c,
        })
        for it in ((js.get("data") or {}).get("list") or []):
            day = str(it.get("notice_date") or "")[:10]
            if day and day >= since:
                out.setdefault(c, []).append({"date": day, "title": str(it.get("title") or "")})
        time.sleep(0.05)          # 轻微限速，避免被限流
    return out


def _hard_hits(news: list[dict], today: str,
               risky_name: bool = False) -> tuple[list[dict], list[dict]]:
    """标题命中强负面规则的公告。

    返回 ``(剔除命中, 降级提示)``，每条为 ``{tag, date, title}``。
    两档判定的细节见 ``HINT_ONLY``：已摘帽公司历史上的「可能被终止上市」
    只降级为提示，不当剔除理由。

    ``risky_name`` = 该股**当前**名称是否带 ST/退市标识。
    """
    t0 = date.fromisoformat(today)
    hard: list[dict] = []
    hint: list[dict] = []
    for n in news:
        t = n["title"]
        if NEG_EXCLUDE.search(t):
            continue
        try:
            age = (t0 - date.fromisoformat(n["date"])).days
        except ValueError:
            age = 0
        for tag, rx, days in _NEG_RE:
            if age > days or not rx.search(t):
                continue
            if tag == "退市风险" and HINT_ONLY.search(t) and not risky_name:
                hint.append({"tag": tag, "date": n["date"], "title": t})
            else:
                hard.append({"tag": tag, "date": n["date"], "title": t})
            break

    def _dedup(items: list[dict]) -> list[dict]:
        """同一标签只留一条（同一事项的系列公告会连发好几份）。"""
        seen, out = set(), []
        for x in items:
            if x["tag"] in seen:
                continue
            seen.add(x["tag"])
            out.append(x)
        return out

    return _dedup(hard), _dedup(hint)


def _soft_reasons(news: list[dict]) -> list[str]:
    tags = []
    for n in news:
        for tag, rx in _SOFT_RE:
            if rx.search(n["title"]) and tag not in tags:
                tags.append(tag)
    return tags


def check(rows: list[dict], today: str | None = None,
          quiet: bool = False) -> tuple[list[dict], list[dict], str]:
    """对候选股做解禁 / 利空过滤。

    返回 ``(保留, 剔除, 说明)``：剔除项带 ``drop_reason``；
    说明为空串表示两项都校验成功，非空则列出未校验项（给 lag 文案用）。
    """
    if not rows:
        return [], [], ""
    today = today or date.today().isoformat()
    codes6 = [_code6(r["code"]) for r in rows]

    lift: dict[str, list[dict]] = {}
    news: dict[str, list[dict]] = {}
    notes: list[str] = []
    try:
        lift = fetch_lift(codes6, today)
    except Exception as exc:                              # noqa: BLE001
        notes.append("解禁未校验")
        if not quiet:
            print(f"[filter] 解禁数据获取失败（已放行）：{type(exc).__name__} {exc}")
    try:
        news = fetch_news(codes6, today)
    except Exception as exc:                              # noqa: BLE001
        notes.append("公告未校验")
        if not quiet:
            print(f"[filter] 公告数据获取失败（已放行）：{type(exc).__name__} {exc}")

    kept: list[dict] = []
    dropped: list[dict] = []
    for r in rows:
        c6 = _code6(r["code"])
        name = str(r.get("name") or "")
        reasons: list[str] = []

        # ---- 解禁：解禁市值 / 当前流通市值 ----
        mv = float(r.get("mv") or 0)
        for d in lift.get(c6) or []:
            cap_yi = float(d.get("LIFT_MARKET_CAP") or 0) / 1e4      # 万元 → 亿元
            if mv <= 0:
                continue
            ratio = cap_yi / mv
            if ratio >= LIFT_RATIO_MAX:
                reasons.append(
                    f"解禁 {str(d.get('FREE_DATE'))[:10]} "
                    f"{cap_yi:.0f}亿（占流通 {ratio * 100:.0f}%）")

        # ---- 重大利空：公告标题（各规则按自己的窗口回溯） ----
        nl = news.get(c6) or []
        risky = bool(re.search(r"ST|退", name, re.IGNORECASE))
        hits, hints = _hard_hits(nl, today, risky_name=risky)
        reasons.extend(f"{h['tag']}（{h['date']}）" for h in hits)

        if reasons:
            # 命中标题原文一并落盘：看板上能点开看依据，不用再去翻公告
            dropped.append({**r, "drop_reason": reasons,
                            "drop_news": [f"{h['date']} {h['title']}" for h in hits]})
        else:
            soft = _soft_reasons(nl) if nl else []
            if hints:
                soft = soft + ["历史退市提示（已摘帽，风险已解除）"]
            kept.append({**r, "soft_flags": soft} if soft else r)
    return kept, dropped, "、".join(notes)


def apply_and_report(rows: list[dict], today: str | None = None,
                     quiet: bool = False) -> tuple[list[dict], list[dict], str]:
    """``check`` 的带打印版本（扫描入口用）。"""
    kept, dropped, note = check(rows, today=today, quiet=quiet)
    if not quiet and dropped:
        print(f"[filter] 解禁/利空剔除 {len(dropped)} 只：")
        for r in dropped:
            print(f"     {r['code']} {r.get('name', '')[:6]:<7} {'; '.join(r['drop_reason'])}")
    elif not quiet:
        print(f"[filter] 解禁/利空过滤：全部 {len(rows)} 只通过"
              + (f"（{note}）" if note else ""))
    return kept, dropped, note
