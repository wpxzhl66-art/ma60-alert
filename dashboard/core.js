/* ============================================================
   60周线预警台 · 共享核心
   常量、DOM 工具、数字格式、排序组件、数据加载。

   主看板（app.js）与历史页（history.js）都从这里取用，不各写一份。
   本文件不碰任何具体 DOM —— 两个页面都能安全引入。
   ============================================================ */
window.WBcore = (function () {
  'use strict';

  const REPO = 'wpxzhl66-art/ma60-alert';
  const REMOTE = 'https://raw.githubusercontent.com/' + REPO + '/main/dashboard';

  /* ------------------------------------------------ 工具 */
  const $ = (id) => document.getElementById(id);

  const el = (tag, cls, html) => {
    const d = document.createElement(tag);
    if (cls) d.className = cls;
    if (html != null) d.innerHTML = html;
    return d;
  };

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  const num = (v, d) => (v == null || isNaN(v)) ? '—' : Number(v).toFixed(d == null ? 2 : d);

  const pct = (v, d) => (v == null || isNaN(v))
    ? '—' : (v > 0 ? '+' : '') + Number(v).toFixed(d == null ? 2 : d) + '%';

  const cls = (v) => (v > 0 ? 'up' : v < 0 ? 'down' : 'muted');

  const yi = (v) => (v == null || isNaN(v) || !v) ? '—' : Number(v).toFixed(1) + ' 亿';

  /** 元 → 万元/亿元（腾讯成交额原始单位为万元） */
  const amtWan = (w) => {
    if (w == null || isNaN(w) || !w) return '—';
    return w >= 10000 ? (w / 10000).toFixed(2) + ' 亿' : Math.round(w) + ' 万';
  };

  function timeAgo(iso) {
    if (!iso) return '—';
    const t = new Date(String(iso).replace(' ', 'T'));
    if (isNaN(t)) return iso;
    const s = (Date.now() - t.getTime()) / 1000;
    if (s < 60) return '刚刚';
    if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
    if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
    return Math.floor(s / 86400) + ' 天前';
  }

  /* ------------------------------------------------ 排序
     多个列表共用一套「排序栏」：
       · 点未选中的字段 → 切到该字段，用它的默认方向
       · 再点已选中的字段 → 翻转方向
       · 选择写入 localStorage，下次打开保持

     字段描述 { id, label, key(item), dir 默认方向, abs 按绝对值, tip 自定义提示 }
     注意 abs（给「偏离」用）：偏离有正负，但用户关心「离 60 周线多远」，
     所以排序键取绝对值 —— 越接近 0 越靠前。 */
  const SORT_LS = 'wb.sort.';
  const sort = {};                 // storeKey -> { id, dir }

  function readSort(storeKey, spec) {
    let raw = null;
    try { raw = JSON.parse(localStorage.getItem(SORT_LS + storeKey) || 'null'); }
    catch (e) { raw = null; }
    const hit = raw && spec.filter((s) => s.id === raw.id)[0];
    if (hit) return { id: hit.id, dir: raw.dir === 'asc' ? 'asc' : 'desc' };
    return { id: spec[0].id, dir: spec[0].dir || 'desc' };
  }

  function writeSort(storeKey, st) {
    try { localStorage.setItem(SORT_LS + storeKey, JSON.stringify(st)); }
    catch (e) { /* 隐私模式下忽略 */ }
  }

  function curField(spec, st) {
    return spec.filter((s) => s.id === st.id)[0] || spec[0];
  }

  /** 排序：缺值一律沉底（不随方向翻转）；字符串按中文（拼音）比较 */
  function sortRows(rows, spec, st) {
    const f = curField(spec, st);
    const sign = st.dir === 'asc' ? 1 : -1;
    const val = (r) => {
      let v = f.key(r);
      if (f.abs) v = Math.abs(v == null || isNaN(v) ? NaN : v);
      return v;
    };
    return rows.slice().sort((a, b) => {
      const x = val(a), y = val(b);
      if (typeof x === 'string' || typeof y === 'string') {
        const ex = (x == null || x === ''), ey = (y == null || y === '');
        if (ex && ey) return 0;
        if (ex) return 1;
        if (ey) return -1;
        return sign * String(x).localeCompare(String(y), 'zh-Hans-CN');
      }
      const nx = (x == null || isNaN(x)), ny = (y == null || isNaN(y));
      if (nx && ny) return 0;
      if (nx) return 1;
      if (ny) return -1;
      return sign * (x - y);
    });
  }

  /** 排序说明，显示在列表标题右侧 */
  function sortTip(s, dir) {
    if (s.tip) return typeof s.tip === 'function' ? s.tip(dir) : s.tip;
    if (s.id === 'dev') {
      return dir === 'asc' ? '按偏离绝对值升序 · 最贴近 60 周线的排最前'
                           : '按偏离绝对值降序 · 偏离最远的排最前';
    }
    if (s.id === 'ind' || s.id === 'sec') return '按名称聚拢同行业/同板块';
    return s.label + (dir === 'asc' ? ' 从小到大' : ' 从大到小');
  }

  function drawSortBar(node, spec, st, onPick) {
    node.innerHTML = '';
    spec.forEach((s) => {
      const b = el('button', 'chip' + (s.id === st.id ? ' on' : ''));
      b.type = 'button';
      b.innerHTML = esc(s.label) +
        (s.id === st.id ? '<span class="ar">' + (st.dir === 'asc' ? '▲' : '▼') + '</span>' : '');
      b.addEventListener('click', () => onPick(s.id));
      node.appendChild(b);
    });
  }

  /** 挂载排序栏到 sort[storeKey]，状态变化时回调 onChange */
  function mountSort(storeKey, nodeId, spec, onChange) {
    sort[storeKey] = readSort(storeKey, spec);
    const node = $(nodeId);
    const repaint = () => drawSortBar(node, spec, sort[storeKey], (id) => {
      const cur = sort[storeKey];
      if (id === cur.id) cur.dir = cur.dir === 'asc' ? 'desc' : 'asc';
      else {
        const f = spec.filter((s) => s.id === id)[0];
        sort[storeKey] = { id: id, dir: f.dir || 'desc' };
      }
      writeSort(storeKey, sort[storeKey]);
      repaint();
      onChange();
    });
    repaint();
  }

  /* ------------------------------------------------ 数据加载
     远端仓库优先，失败回退同目录本地文件（本地开发/离线可用）。 */
  /* ------------------------------------------------ 数据加载（v1.19.1 加固）
     站点自带 json 只是兜底，远端 raw（云端每天更新）优先。
     ⚠️ v1.19.1 修的 bug：raw.githubusercontent.com 在国内会「沉默挂起」
     （TCP 连不上但 fetch 既不报错也不返回）——旧实现没有超时，
     结果整个看板永远停在「正在加载…」。
     现在：远端 3.5 秒拿不到 → 立刻回退站点自带快照，并记住「远端不可用」，
     同一页面后续文件直接走本地（不再每个文件都干等一次）；
     远端成功后标记复位，下次打开仍优先取最新数据。
     页面可用 WBcore.dataSource() 判断这批数据是远端最新还是站点快照。 */
  const REMOTE_TIMEOUT_MS = 3500;
  const LOCAL_TIMEOUT_MS = 15000;
  let remoteDown = false;      // 本次页面会话内远端是否已判定不可用
  let lastSource = '';         // 'remote'（最新）| 'local'（站点自带快照）

  /** 带超时的 JSON 拉取：超时/非 2xx/解析失败都算失败 */
  async function fetchJSON(url, timeoutMs) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const r = await fetch(url, { cache: 'no-store', signal: ctl.signal });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return await r.json();
    } finally {
      clearTimeout(timer);
    }
  }

  async function loadJSON(name) {
    if (!remoteDown) {
      try {
        const j = await fetchJSON(REMOTE + '/' + name + '?_=' + Date.now(), REMOTE_TIMEOUT_MS);
        lastSource = 'remote';
        return j;
      } catch (e) {
        remoteDown = true;     // 后续文件不再尝试远端，直接走本地
      }
    }
    const j = await fetchJSON('./' + name, LOCAL_TIMEOUT_MS);
    lastSource = 'local';
    return j;
  }

  /** 最近一批数据来自哪里：'remote'（最新）| 'local'（站点自带快照） */
  function dataSource() { return lastSource; }
  /** 远端是否已被判定连不上（用于界面提示，不给用户看技术细节） */
  function remoteUnavailable() { return remoteDown; }

  /* ------------------------------------------------ 单股周线（前复权，按需）
     整包 weekly.json（4MB+）自 v1.2.1 停用 —— 国内访问 raw 基本拉不完。
     改为点开某只时才取它的周线（几 KB），失败就直接提示。 */
  const WK_URL = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=';
  const _one = {};

  async function fetchWeeklyOne(code, fq) {
    fq = (fq === undefined || fq === null) ? 'qfq' : fq;   // 默认前复权（看盘口径）
    const key = code + '|' + fq;
    if (_one[key]) return _one[key];
    const sec = code.replace('.', '');
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 9000);
    try {
      const r = await fetch(WK_URL + sec + ',week,,,320,' + fq, { signal: ctl.signal });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      const blk = j && j.data && j.data[sec];
      const want = fq === 'qfq' ? 'qfqweek' : (fq === 'hfq' ? 'hfqweek' : 'week');
      let bars = (blk && Array.isArray(blk[want]) && blk[want].length) ? blk[want] : null;
      if (!bars && blk) {
        for (const k of ['qfqweek', 'week', 'hfqweek']) {
          if (Array.isArray(blk[k]) && blk[k].length) { bars = blk[k]; break; }
        }
      }
      if (!bars) throw new Error('无周线数据');
      const out = bars.filter((b) => b && b.length >= 3)
        .map((b) => ({ week: b[0], close: +b[2] }));
      if (!out.length) throw new Error('无周线数据');
      _one[key] = out;
      return out;
    } finally { clearTimeout(timer); }
  }

  /** 单股日线（前复权，按需）——v1.17.0
      同一个腾讯接口换成 day；一次取 400 根（接口上限 640），
      够「最近 200 日 + 前置 60 日算均线」用。缓存同周线，当天反复切换不重复请求。 */
  const _d = {};
  async function fetchDailyOne(code, fq, cnt) {
    fq = (fq === undefined || fq === null) ? 'qfq' : fq;
    const n = cnt || 400;
    const key = code + '|' + fq + '|' + n;
    if (_d[key]) return _d[key];
    const sec = code.replace('.', '');
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 9000);
    try {
      const r = await fetch(WK_URL + sec + ',day,,,' + n + ',' + fq, { signal: ctl.signal });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      const blk = j && j.data && j.data[sec];
      const want = fq === 'qfq' ? 'qfqday' : (fq === 'hfq' ? 'hfqday' : 'day');
      let bars = (blk && Array.isArray(blk[want]) && blk[want].length) ? blk[want] : null;
      if (!bars && blk) {
        for (const k of ['qfqday', 'day', 'hfqday']) {
          if (Array.isArray(blk[k]) && blk[k].length) { bars = blk[k]; break; }
        }
      }
      if (!bars) throw new Error('无日线数据');
      const out = bars.filter((b) => b && b.length >= 3)
        .map((b) => ({ day: b[0], close: +b[2] }));
      if (!out.length) throw new Error('无日线数据');
      _d[key] = out;
      return out;
    } finally { clearTimeout(timer); }
  }

  /** K 线（蜡烛图）数据（前复权，按需）——v1.20.0
      同一个腾讯接口，period='day'|'week'|'month'；返回 OHLCV（腾讯顺序：
      [日期, 开, 收, 高, 低, 量(手)]）。day 取 400 根（接口上限 640），
      够「最近 120 根显示 + 前置 60 根算 MA60」；week/month 取 320 根。 */
  const _bars = {};
  async function fetchBarsOne(code, period) {
    const cnt = period === 'day' ? 400 : 320;
    const key = code + '|' + period + '|' + cnt;
    if (_bars[key]) return _bars[key];
    const sec = code.replace('.', '');
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 9000);
    try {
      const r = await fetch(WK_URL + sec + ',' + period + ',,,' + cnt + ',qfq',
        { signal: ctl.signal });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      const blk = j && j.data && j.data[sec];
      const want = 'qfq' + period;
      let bars = (blk && Array.isArray(blk[want]) && blk[want].length) ? blk[want] : null;
      if (!bars && blk) {
        for (const k of [want, period, 'qfq' + period]) {
          if (Array.isArray(blk[k]) && blk[k].length) { bars = blk[k]; break; }
        }
      }
      if (!bars) throw new Error('无K线数据');
      const out = bars.filter((b) => b && b.length >= 6).map((b) => ({
        d: b[0], open: +b[1], close: +b[2], high: +b[3], low: +b[4], vol: +b[5],
      })).filter((b) => b.close > 0);
      if (!out.length) throw new Error('无K线数据');
      _bars[key] = out;
      return out;
    } finally { clearTimeout(timer); }
  }

  /** 当日分时（前复权口径即原始价）——v1.20.0
      返回 { pre: 昨收, pts: [{t, price, avg, vol}] }；avg=均价线=累计额/(累计量×100)。
      分钟成交量 = 相邻累计量之差（手）。 */
  const MIN_URL = 'https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=';
  const _min = {};
  async function fetchMinuteOne(code) {
    if (_min[code]) return _min[code];
    const sec = code.replace('.', '');
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 9000);
    try {
      const r = await fetch(MIN_URL + sec, { signal: ctl.signal });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      const blk = j && j.data && j.data[sec] && j.data[sec].data;
      const rows = blk && Array.isArray(blk.data) ? blk.data : null;
      const qt = (j && j.data && j.data[sec] && j.data[sec].qt) || [];
      const pre = +(blk && blk.prec != null ? blk.prec : qt[4]) || 0;
      if (!rows || !rows.length) throw new Error('无分时数据');
      let cumV = 0, cumA = 0;
      const pts = rows.map((s) => {
        const p = String(s).split(' ');
        if (p.length < 3) return null;
        const price = +p[1];
        const v = +p[2] || 0, a = +p[3] || 0;
        const dv = Math.max(0, v - cumV);
        cumV = v; cumA = a;
        return { t: p[0].slice(0, 2) + ':' + p[0].slice(2, 4),
          price: price, avg: v > 0 ? a / (v * 100) : price, vol: dv };
      }).filter((x) => x && x.price > 0);
      if (!pts.length) throw new Error('无分时数据');
      const out = { pre: pre || pts[0].price, pts: pts };
      _min[code] = out;
      return out;
    } finally { clearTimeout(timer); }
  }

  /** 月线按年聚合成年K（零额外请求）：开=年首月开、收=年末日收、
      高=各月最高、低=各月最低、量=求和。 */
  function barsYearly(barsMonth) {
    const by = {};
    barsMonth.forEach((b) => {
      const y = String(b.d).slice(0, 4);
      const o = by[y] || (by[y] = { d: y, open: b.open, close: b.close,
        high: b.high, low: b.low, vol: 0 });
      o.close = b.close;
      o.high = Math.max(o.high, b.high);
      o.low = Math.min(o.low, b.low);
      o.vol += b.vol;
    });
    return Object.keys(by).sort().map((y) => by[y]);
  }

  /** 指标卡（.metrics 网格的单元）：k 标题 / v 值 / x 副注 / cl 颜色类 */
  function metric(k, v, x, cl) {
    return '<div class="metric"><div class="k">' + esc(k) + '</div>' +
      '<div class="v sm ' + (cl || '') + '">' + esc(v) + '</div>' +
      (x ? '<div class="x">' + esc(x) + '</div>' : '') + '</div>';
  }

  /* ------------------------------------------------ K 线图公共配置
     股票周/日 K 与期货加权周 K 共用，也供历史页复用 —— 两处图表交互保持一致。
     o: { color, areaTop, areaBottom, closeLabel, maLabel, fmt, gridLeft, markPoint } */
  function weeklyChartOption(weeks, closes, m60, o) {
    const maLabel = o.maLabel || '60 周线';
    return {
      animation: false,
      grid: { left: o.gridLeft || 46, right: 14, top: 22, bottom: 30 },
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(28,28,30,.92)', borderWidth: 0,
        textStyle: { color: '#fff', fontSize: 12 },
        formatter: (ps) => {
          if (!ps || !ps.length) return '';
          const i = ps[0].dataIndex;
          let s = weeks[i] + '<br/>' + o.closeLabel + ' <b>' + o.fmt(closes[i]) + '</b>';
          if (m60[i] != null) s += '<br/>' + maLabel + ' ' + o.fmt(m60[i]);
          return s;
        },
      },
      xAxis: {
        type: 'category', data: weeks, boundaryGap: false,
        axisLine: { lineStyle: { color: '#8e8e93', width: .5 } },
        axisTick: { show: false },
        axisLabel: { color: '#8e8e93', fontSize: 10, formatter: (v) => v.slice(2, 7) },
      },
      yAxis: {
        type: 'value', scale: true,
        splitLine: { lineStyle: { color: 'rgba(142,142,147,.18)' } },
        axisLabel: { color: '#8e8e93', fontSize: 10 },
      },
      series: [
        {
          name: o.closeLabel, type: 'line', data: closes, smooth: false,
          symbol: 'none', lineStyle: { width: 1.8, color: o.color },
          areaStyle: {
            color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [{ offset: 0, color: o.areaTop },
                           { offset: 1, color: o.areaBottom }] },
          },
          markPoint: o.markPoint ? { data: o.markPoint, silent: true } : undefined,
        },
        {
          name: maLabel, type: 'line', data: m60, symbol: 'none',
          lineStyle: { width: 1.8, color: '#ff9500' },
        },
      ],
    };
  }

  /* ------------------------------------------------ 蜡烛图 / 分时图公共配置（v1.20.0）
     A 股惯例：涨红 #ff3b30、跌绿 #34c759（与全站口径一致）。
     MA 配色：MA5 黄 / MA10 紫 / MA20 蓝 / MA60 橙 —— 橙色延续旧「60 周期线」心智。 */
  const K_UP = '#ff3b30';
  const K_DN = '#34c759';
  const K_MAS = [
    ['ma5', 'MA5', '#ffcc00'], ['ma10', 'MA10', '#af52de'],
    ['ma20', 'MA20', '#007aff'], ['ma60', 'MA60', '#ff9500'],
  ];

  /** 蜡烛图：bars=[{d,open,close,high,low,vol}]，o:{zoom 默认显示根数, markPoint, volUnit} */
  function klineChartOption(bars, o) {
    const xs = bars.map((b) => b.d);
    const candles = bars.map((b) => [b.open, b.close, b.low, b.high]);
    const mas = o.mas || {};
    const legend = K_MAS.filter((m) => mas[m[0]]).map((m) => m[1]);
    const series = [{
      name: 'K线', type: 'candlestick', data: candles, barMaxWidth: 14,
      itemStyle: { color: K_UP, color0: K_DN, borderColor: K_UP, borderColor0: K_DN },
      markPoint: o.markPoint ? { data: o.markPoint, silent: true } : undefined,
    }];
    K_MAS.forEach((m) => {
      if (!mas[m[0]]) return;
      series.push({ name: m[1], type: 'line', data: mas[m[0]], symbol: 'none',
        lineStyle: { width: 1, color: m[2] }, emphasis: { disabled: true } });
    });
    series.push({
      name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
      data: bars.map((b) => b.vol), barMaxWidth: 14,
      itemStyle: { color: (p) => (bars[p.dataIndex].close >= bars[p.dataIndex].open ? K_UP : K_DN) },
    });
    let zoom;
    if (o.zoom && bars.length > o.zoom) {
      const s = Math.round((1 - o.zoom / bars.length) * 100);
      zoom = [{ type: 'inside', xAxisIndex: [0, 1], start: s, end: 100 }];
    } else {
      zoom = [{ type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 }];
    }
    return {
      animation: false,
      legend: { show: legend.length > 0, data: legend, top: 0, right: 4,
        itemWidth: 14, itemHeight: 8, textStyle: { color: '#8e8e93', fontSize: 10 } },
      grid: [
        { left: 46, right: 14, top: 22, height: '60%' },
        { left: 46, right: 14, top: '74%', height: '16%' },
      ],
      tooltip: {
        trigger: 'axis', backgroundColor: 'rgba(28,28,30,.92)', borderWidth: 0,
        textStyle: { color: '#fff', fontSize: 12 },
        formatter: (ps) => {
          if (!ps || !ps.length) return '';
          const i = ps[0].dataIndex;
          const b = bars[i];
          let s = xs[i] + '<br/>开 <b>' + b.open.toFixed(2) + '</b>　收 <b>' +
            b.close.toFixed(2) + '</b><br/>高 ' + b.high.toFixed(2) + '　低 ' +
            b.low.toFixed(2) + '<br/>量 ' + Math.round(b.vol).toLocaleString() + (o.volUnit || ' 手');
          K_MAS.forEach((m) => {
            if (mas[m[0]] && mas[m[0]][i] != null) s += '<br/><span style="color:' + m[2] + '">' +
              m[1] + ' ' + mas[m[0]][i].toFixed(2) + '</span>';
          });
          return s;
        },
      },
      xAxis: [
        { type: 'category', data: xs, gridIndex: 0, boundaryGap: true,
          axisLine: { lineStyle: { color: '#8e8e93', width: .5 } },
          axisTick: { show: false }, axisLabel: { show: false },
          splitLine: { show: false } },
        { type: 'category', data: xs, gridIndex: 1, boundaryGap: true,
          axisLine: { lineStyle: { color: '#8e8e93', width: .5 } },
          axisTick: { show: false },
          axisLabel: { color: '#8e8e93', fontSize: 10, formatter: (v) => v.slice(2, 7) },
          splitLine: { show: false } },
      ],
      yAxis: [
        { type: 'value', scale: true, gridIndex: 0,
          splitLine: { lineStyle: { color: 'rgba(142,142,147,.18)' } },
          axisLabel: { color: '#8e8e93', fontSize: 10 } },
        { type: 'value', gridIndex: 1,
          splitLine: { show: false }, axisLabel: { show: false },
          axisLine: { show: false }, axisTick: { show: false } },
      ],
      dataZoom: zoom,
      series: series,
    };
  }

  /** 分时图：m={pre 昨收, pts:[{t,price,avg,vol}]}；蓝线现价 + 橙线均价 + 昨收虚线 + 量副图 */
  function minuteChartOption(m, o) {
    const pts = m.pts;
    const xs = pts.map((p) => p.t);
    const prices = pts.map((p) => p.price);
    const avgs = pts.map((p) => p.avg);
    return {
      animation: false,
      grid: [
        { left: 46, right: 14, top: 14, height: '58%' },
        { left: 46, right: 14, top: '74%', height: '16%' },
      ],
      tooltip: {
        trigger: 'axis', backgroundColor: 'rgba(28,28,30,.92)', borderWidth: 0,
        textStyle: { color: '#fff', fontSize: 12 },
        formatter: (ps) => {
          if (!ps || !ps.length) return '';
          const i = ps[0].dataIndex;
          return xs[i] + '<br/>现价 <b>' + prices[i].toFixed(2) + '</b><br/>均价 ' +
            avgs[i].toFixed(2) + '<br/>量 ' + Math.round(pts[i].vol).toLocaleString() + ' 手';
        },
      },
      xAxis: [
        { type: 'category', data: xs, gridIndex: 0, boundaryGap: false,
          axisLine: { lineStyle: { color: '#8e8e93', width: .5 } },
          axisTick: { show: false }, axisLabel: { show: false }, splitLine: { show: false } },
        { type: 'category', data: xs, gridIndex: 1, boundaryGap: false,
          axisLine: { lineStyle: { color: '#8e8e93', width: .5 } },
          axisTick: { show: false },
          axisLabel: { color: '#8e8e93', fontSize: 10,
            interval: (i, v) => ['09:30', '10:00', '10:30', '11:00', '13:00', '14:00', '14:30'].indexOf(v) >= 0 },
          splitLine: { show: false } },
      ],
      yAxis: [
        { type: 'value', scale: true, gridIndex: 0,
          splitLine: { lineStyle: { color: 'rgba(142,142,147,.18)' } },
          axisLabel: { color: '#8e8e93', fontSize: 10 } },
        { type: 'value', gridIndex: 1, splitLine: { show: false },
          axisLabel: { show: false }, axisLine: { show: false }, axisTick: { show: false } },
      ],
      series: [
        { name: '现价', type: 'line', data: prices, symbol: 'none',
          lineStyle: { width: 1.4, color: '#007aff' },
          areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [{ offset: 0, color: 'rgba(0,122,255,.10)' },
                         { offset: 1, color: 'rgba(0,122,255,0)' }] } },
          markLine: m.pre > 0 ? { silent: true, symbol: 'none',
            lineStyle: { color: '#8e8e93', width: 1, type: 'dashed' },
            label: { show: true, position: 'end', color: '#8e8e93', fontSize: 9,
              formatter: () => m.pre.toFixed(2) },
            data: [{ yAxis: m.pre }] } : undefined },
        { name: '均价', type: 'line', data: avgs, symbol: 'none',
          lineStyle: { width: 1.2, color: '#ff9500' }, emphasis: { disabled: true } },
        { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
          data: pts.map((p) => p.vol), barMaxWidth: 4,
          itemStyle: { color: (p) =>
            (p.dataIndex === 0 ? '#8e8e93' :
              (pts[p.dataIndex].price >= pts[p.dataIndex - 1].price ? K_UP : K_DN)) } },
      ],
    };
  }

  /** 名称是否带风险标识：ST / *ST（前缀）或退市整理（「XX退」）。
   *  ⚠️ 口径与后端 src/name_rule.py 保持一致 —— 那是全项目唯一真源
   *  （pattern.core 与 alert.run_alert 都转发到它）。改正则必须两处同步。
   *  ST 只看**名称前缀**，前缀里允许 * 号、半角/全角空格、·・_- 与老「SST / S*ST」标记；
   *  不能只判 "ST" —— 退市整理期的股票改名成「XX退」，名称里没有 ST。 */
  function isRiskName(name) {
    var s = String(name == null ? '' : name);
    return /^[\s\u3000*＊·・_\-－]{0,4}s?[\s\u3000*＊·・_\-－]{0,4}st(?![A-Za-z])/i.test(s)
      || /退\s*$|退市/i.test(s);
  }

  /** 简单移动平均；窗口未满的位置为 null */
  function ma(arr, n) {
    const out = [];
    let sum = 0;
    for (let i = 0; i < arr.length; i++) {
      sum += arr[i];
      if (i >= n) sum -= arr[i - n];
      out.push(i >= n - 1 ? +(sum / n).toFixed(4) : null);
    }
    return out;
  }

  return {
    REPO, REMOTE, $, el, esc, num, pct, cls, yi, amtWan, timeAgo,
    sort, readSort, writeSort, curField, sortRows, sortTip, drawSortBar,
    mountSort, loadJSON, fetchWeeklyOne, fetchDailyOne, weeklyChartOption, ma, metric,
    fetchBarsOne, fetchMinuteOne, barsYearly, klineChartOption, minuteChartOption,
    isRiskName, dataSource, remoteUnavailable,
  };
})();
