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
  async function loadJSON(name) {
    const urls = [REMOTE + '/' + name + '?_=' + Date.now(), './' + name];
    let err = null;
    for (const u of urls) {
      try {
        const r = await fetch(u, { cache: 'no-store' });
        if (!r.ok) { err = new Error(r.status); continue; }
        return await r.json();
      } catch (e) { err = e; }
    }
    throw err || new Error('加载失败：' + name);
  }

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

  /** 名称是否带风险标识：ST / *ST / 退市整理（「XX退」）。
   *  ⚠️ 口径与后端 src/pattern/core.py 的 RISK_NAME_PAT 保持一致（v1.19.0）；
   *  不能只判 "ST" —— 退市整理期的股票改名成「XX退」，名称里没有 ST。 */
  function isRiskName(name) {
    return /ST|退/i.test(String(name == null ? '' : name));
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
    isRiskName,
  };
})();
