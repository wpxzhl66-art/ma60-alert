#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通达信选股结果 → 看板数据（dashboard/tdx_pick.json）

用途
    把通达信（MCP tdx_screener）的条件选股结果，转成看板「通达信选股」板块
    直接消费的 JSON。原始导出不改动，转换结果可重复生成。

输入（二选一）
    1) --raw  通达信选股器的原始 JSON（推荐，字段最全）
              结构见 data/tdx/raw/2026-10-16_screener.json
              data[] 每项至少含：sec_code / sec_name / now_price / chg
              60 周均值字段名兼容 ma60w / ma60 / 「周线现价60日均值…」
    2) --csv  手工整理的 CSV，表头固定 5 列：
              code,name,price,chg,ma60w

输出
    dashboard/tdx_pick.json（schema = tdx_pick.v1）

校验（不通过的行不进名单，写进 rejected）
    - 代码必须是 6 位数字
    - 名称必须是 ST/*ST/退市 之外的正常名称（复用 src/name_rule.py）
    - 现价必须高于 60 周均线（选股器口径复核，防止价格与均线错位）
    - 同一代码只保留一条

用法
    PYTHONPATH=src <venv-python> -m tdx.import_pick \
        --raw data/tdx/raw/2026-10-16_screener.json \
        --out dashboard/tdx_pick.json \
        --query "当天股价上穿60周均线，排除ST股"
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from name_rule import is_risk_name  # noqa: E402

SCHEMA = 'tdx_pick.v1'
MA_PERIOD = 60
# 偏离分档（单位：%）。口径与前端 app.js 的 TDX_ZONES 必须一致。
FRESH_MAX = 2.0     # ≤2%：刚上穿，形态最新鲜
HOLD_MAX = 10.0     # 2%~10%：已站上，仍需回踩确认
ZONE_TEXT = {
    'fresh': '刚上穿',
    'hold': '已站上',
    'far': '远离',
}
MA_KEYS = ('ma60w', 'ma60', 'ma60_week', 'w_ma60')


# ----------------------------------------------------------------- 基础工具
def clean_name(name: str) -> str:
    """去掉通达信为凑三家宽而在两字股名里插入的空格（英 力 特 → 英力特）。"""
    return re.sub(r'\s+', '', str(name or '')).strip()


def market_prefix(code: str, market=None) -> str:
    """6 位代码 → 腾讯/项目通用的 sh./sz./bj. 前缀。"""
    if market in (1, '1'):
        return 'sh'
    if market in (2, '2'):
        return 'bj'
    if market in (0, '0'):
        return 'sz'
    if code.startswith(('600', '601', '603', '605', '688', '689')):
        return 'sh'
    if code.startswith(('430', '830', '831', '832', '833', '834', '835',
                        '836', '837', '838', '839', '870', '871', '872',
                        '873', '874', '875', '876', '877', '878', '920')):
        return 'bj'
    return 'sz'


def zone_of(dev: float) -> str:
    if dev <= FRESH_MAX:
        return 'fresh'
    if dev <= HOLD_MAX:
        return 'hold'
    return 'far'


def pick_ma(row: dict):
    for k in MA_KEYS:
        if k in row and row[k] not in (None, ''):
            return float(row[k])
    for k, v in row.items():
        if '60日均值' in str(k) or '60周' in str(k):
            if v not in (None, ''):
                return float(v)
    return None


# ----------------------------------------------------------------- 读取输入
def load_raw(path: Path) -> dict:
    j = json.loads(path.read_text(encoding='utf-8'))
    rows = []
    for r in j.get('data') or j.get('rows') or []:
        code = str(r.get('sec_code') or r.get('code') or '').strip()
        rows.append({
            'code': code,
            'name': r.get('sec_name') or r.get('name') or '',
            'market': r.get('market'),
            'price': r.get('now_price', r.get('price')),
            'chg': r.get('chg', r.get('change_pct')),
            'ma60w': pick_ma(r),
        })
    return {
        'meta': j,
        'rows': rows,
        'day': j.get('day') or (j.get('response_meta') or {}).get('data_date') or '',
    }


def load_csv(path: Path) -> dict:
    rows = []
    with path.open(encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            rows.append({
                'code': str(r.get('code') or r.get('代码') or '').strip(),
                'name': r.get('name') or r.get('名称') or '',
                'market': r.get('market'),
                'price': r.get('price') or r.get('最新价'),
                'chg': r.get('chg') or r.get('涨跌幅'),
                'ma60w': r.get('ma60w') or r.get('60周线'),
            })
    return {'meta': {}, 'rows': rows, 'day': ''}


# ----------------------------------------------------------------- 行业补充
def load_industry(static_path: Path) -> dict:
    """dashboard/static.json 自带全市场行业映射（key = sh.600000）。"""
    try:
        j = json.loads(static_path.read_text(encoding='utf-8'))
        return j.get('industry') or {}
    except Exception:
        return {}


def load_names(static_path: Path) -> dict:
    try:
        j = json.loads(static_path.read_text(encoding='utf-8'))
        return j.get('names') or {}
    except Exception:
        return {}


# ----------------------------------------------------------------- 转换主体
def build(raw: dict, static_path: Path, query: str, day: str) -> dict:
    industry = load_industry(static_path)
    std_names = load_names(static_path)
    out, rejected, seen = [], [], set()

    for r in raw['rows']:
        code = str(r['code']).strip()
        name = clean_name(r['name'])
        if not re.fullmatch(r'\d{6}', code):
            rejected.append({'code': code, 'name': name, 'why': '代码不是 6 位数字'})
            continue
        if code in seen:
            rejected.append({'code': code, 'name': name, 'why': '代码重复'})
            continue
        try:
            price = float(r['price'])
            ma60w = float(r['ma60w'])
        except (TypeError, ValueError):
            rejected.append({'code': code, 'name': name, 'why': '价格或 60 周线缺失'})
            continue
        if is_risk_name(name):
            rejected.append({'code': code, 'name': name, 'why': 'ST/退市名称'})
            continue
        if price <= ma60w:
            rejected.append({'code': code, 'name': name,
                             'why': '现价未高于 60 周线（%.2f ≤ %.2f）' % (price, ma60w)})
            continue

        pre = market_prefix(code, r.get('market'))
        full = pre + '.' + code
        dev = (price / ma60w - 1.0) * 100.0
        seen.add(code)
        out.append({
            'code': full,
            'tdx_code': code,
            'name': std_names.get(full) or name,
            'industry': industry.get(full) or '',
            'price': round(price, 3),
            'chg': round(float(r['chg'] or 0), 2),
            'ma60w': round(ma60w, 3),
            'dev_pct': round(dev, 2),
            'zone': zone_of(dev),
            'condition': '上穿60周线',
            'day': day,
        })

    out.sort(key=lambda x: x['dev_pct'])
    counts = {'total': len(out), 'fresh': 0, 'hold': 0, 'far': 0}
    for x in out:
        counts[x['zone']] += 1
    industries = {}
    for x in out:
        if x['industry']:
            industries[x['industry']] = industries.get(x['industry'], 0) + 1
    top_ind = sorted(industries.items(), key=lambda kv: -kv[1])[:5]

    meta = raw['meta'] or {}
    return {
        'schema': SCHEMA,
        'generated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'day': day,
        'source': {
            'provider': '通达信',
            'channel': 'MCP tdx-connector / tdx_screener',
            'query': query,
            'rang': 'AG（A 股）',
            'raw_total': (meta.get('response_meta') or {}).get('total') or len(raw['rows']),
        },
        'condition': {
            'ma_period': MA_PERIOD,
            'rules': [
                '本周收盘价 > 60 周均线',
                '上周收盘价 ≤ 60 周均线',
                '名称不含 ST / *ST / 退市标识',
            ],
            'exclude_st': True,
            'zones': {
                'fresh': {'label': ZONE_TEXT['fresh'], 'max_dev_pct': FRESH_MAX},
                'hold': {'label': ZONE_TEXT['hold'], 'max_dev_pct': HOLD_MAX},
                'far': {'label': ZONE_TEXT['far'], 'max_dev_pct': None},
            },
        },
        'fields': [
            {'key': 'code', 'label': '代码', 'note': '项目通用格式 sh./sz./bj. + 6 位'},
            {'key': 'name', 'label': '名称', 'note': '以项目 static.json 名称为准'},
            {'key': 'industry', 'label': '行业', 'note': '来自 static.json，可能为空'},
            {'key': 'price', 'label': '最新价', 'note': '元，收盘价'},
            {'key': 'chg', 'label': '涨跌幅', 'note': '%，当日'},
            {'key': 'ma60w', 'label': '60周线', 'note': '周线 60 周收盘均价'},
            {'key': 'dev_pct', 'label': '偏离', 'note': '现价相对 60 周线的百分比'},
            {'key': 'zone', 'label': '分档', 'note': 'fresh ≤2% / hold ≤10% / far >10%'},
            {'key': 'condition', 'label': '入选条件', 'note': '固定文案，供前端标注'},
            {'key': 'day', 'label': '数据日', 'note': '该名单对应的交易日'},
        ],
        'counts': counts,
        'top_industries': [{'name': k, 'n': v} for k, v in top_ind],
        'rejected': rejected,
        'rows': out,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description='通达信选股结果转看板 JSON')
    ap.add_argument('--raw', help='通达信选股器原始 JSON')
    ap.add_argument('--csv', help='手工 CSV（code,name,price,chg,ma60w）')
    ap.add_argument('--out', default=str(ROOT / 'dashboard' / 'tdx_pick.json'))
    ap.add_argument('--static', default=str(ROOT / 'dashboard' / 'static.json'))
    ap.add_argument('--query', default='当天股价上穿60周均线，排除ST股')
    ap.add_argument('--day', default='', help='数据日 YYYY-MM-DD；默认取文件名前 10 位')
    args = ap.parse_args()

    if not args.raw and not args.csv:
        ap.error('必须给 --raw 或 --csv')

    src = Path(args.raw or args.csv)
    raw = load_raw(src) if args.raw else load_csv(src)
    day = args.day or (re.match(r'(\d{4}-\d{2}-\d{2})', src.name).group(1)
                       if re.match(r'(\d{4}-\d{2}-\d{2})', src.name) else '')
    data = build(raw, Path(args.static), args.query, day)
    if not day:
        data['day'] = ''

    out = Path(args.out)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')

    c = data['counts']
    print('[tdx] 输入 %d 行 → 入库 %d 只（刚上穿 %d / 已站上 %d / 远离 %d），剔除 %d 行' % (
        len(raw['rows']), c['total'], c['fresh'], c['hold'], c['far'], len(data['rejected'])))
    for r in data['rejected']:
        print('  [剔除] %s %s — %s' % (r['code'], r['name'], r['why']))
    if data['top_industries']:
        print('[tdx] 行业前五：' + '、'.join('%s %d' % (i['name'], i['n'])
                                        for i in data['top_industries']))
    print('[tdx] 已写 %s' % out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
