"""腾讯批量实时行情。

接口：http://qt.gtimg.cn/q=sh600519,sz000001,...（GBK 编码，~ 分隔）
关键字段（实测 2026-10-14）：
  [1]名称 [2]代码 [3]现价 [4]昨收 [5]今开 [6]成交量(手)
  [30]时间戳 YYYYMMDDHHMMSS [31]涨跌 [32]涨跌% [37]成交额(万)
  [38]换手率 [44]流通市值(亿) [45]总市值(亿)
注意：退市股返回冻结数据（时间戳陈旧、成交量 0），由调用方用 universe 状态过滤。
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

URL = "http://qt.gtimg.cn/q="
_CHUNK = 80
_TIMEOUT = 10

_COLUMNS = ["code", "name", "price", "preclose", "open", "vol_hand",
            "ts", "chg_pct", "amount_wan", "turnover", "mv_float_yi"]


def _to_tencent(code: str) -> str:
    return code.replace(".", "", 1)          # sh.600519 -> sh600519


def _parse(raw_text: str) -> list[dict]:
    out = []
    for line in raw_text.split(";"):
        line = line.strip()
        if '="' not in line:
            continue
        var = line.split('="', 1)[0]              # 如 v_sh600000
        payload = line.split('="', 1)[1].rstrip('"')
        p = payload.split("~")
        if len(p) < 45:
            continue
        m = re.match(r"v_([a-z]{2})(\d{6})$", var)
        if not m:
            continue
        out.append({
            "code": f"{m.group(1)}.{m.group(2)}",
            "name": p[1],
            "price": float(p[3] or 0),
            "preclose": float(p[4] or 0),
            "open": float(p[5] or 0),
            "vol_hand": float(p[6] or 0),
            "ts": p[30],
            "chg_pct": float(p[32] or 0),
            "amount_wan": float(p[37] or 0),
            "turnover": float(p[38] or 0),
            "mv_float_yi": float(p[44] or 0),
        })
    return out


def fetch_quotes(codes: list[str], workers: int = 8) -> pd.DataFrame:
    """批量拉实时行情。返回 DataFrame（列见 _COLUMNS），失败的代码缺失。"""
    chunks = [codes[i:i + _CHUNK] for i in range(0, len(codes), _CHUNK)]

    def one(chunk: list[str]):
        q = ",".join(_to_tencent(c) for c in chunk)
        for attempt in range(3):
            try:
                r = requests.get(URL + q, timeout=_TIMEOUT)
                r.encoding = "gbk"
                return _parse(r.text)
            except Exception:  # noqa: BLE001
                if attempt == 2:
                    return []
    rows = []
    with ThreadPoolExecutor(workers) as ex:
        for part in ex.map(one, chunks):
            rows.extend(part)
    df = pd.DataFrame(rows, columns=_COLUMNS)
    return df.drop_duplicates("code").set_index("code")


def market_date() -> str | None:
    """返回当前行情所属交易日（YYYY-MM-DD），用于判断今天是否已有行情。"""
    df = fetch_quotes(["sh.000001"])
    if df.empty:
        return None
    ts = df.iloc[0]["ts"]
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else None
