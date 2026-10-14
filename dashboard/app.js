/* ============================================================
   60周线预警台 · 核心前端
   数据来源：GitHub 仓库 dashboard/ 目录（每日由 Actions 更新），
             远端不可达时回退到本地同目录文件。
   ============================================================ */
(function () {
  'use strict';

  const REPO = 'wpxzhl66-art/ma60-alert';
  const REMOTE = 'https://raw.githubusercontent.com/' + REPO + '/main/dashboard';
  const QIMG = 'https://raw.githubusercontent.com/' + REPO + '/main/charts';

  const S = {
    data: null,        // data.json
    static: null,      // static.json
    quotes: {},        // quotes.json：全市场最新价（账户估值兜底）
    weekly: null,      // 整包周线（v1.2.1 起已停用，仅作兜底）
    weeklyLoading: null,
    weeklyOne: {},     // 单股周线缓存（按需拉取）
    codeIndex: null,   // 搜索用
  };

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
  const pct = (v, d) => (v == null || isNaN(v)) ? '—' : (v > 0 ? '+' : '') + Number(v).toFixed(d == null ? 2 : d) + '%';
  const cls = (v) => (v > 0 ? 'up' : v < 0 ? 'down' : 'muted');
  const yi = (v) => (v == null || isNaN(v)) ? '—' : Number(v).toFixed(1) + ' 亿';
  const wan = (v) => (v == null || isNaN(v)) ? '—' : (v / 10000).toFixed(2) + ' 万';

  function timeAgo(iso) {
    if (!iso) return '—';
    const t = new Date(iso.replace(' ', 'T'));
    if (isNaN(t)) return iso;
    const s = (Date.now() - t.getTime()) / 1000;
    if (s < 60) return '刚刚';
    if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
    if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
    return Math.floor(s / 86400) + ' 天前';
  }

  /* ------------------------------------------------ 数据加载 */
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

  async function loadWeekly() {
    if (S.weekly) return S.weekly;
    if (S.weeklyLoading) return S.weeklyLoading;
    S.weeklyLoading = (async () => {
      const urls = [REMOTE + '/weekly.json?_=' + Date.now(), './weekly.json'];
      for (const u of urls) {
        try {
          const r = await fetch(u, { cache: 'no-store' });
          if (!r.ok) continue;
          S.weekly = await r.json();
          return S.weekly;
        } catch (e) { /* 试下一个 */ }
      }
      throw new Error('周线数据加载失败');
    })();
    return S.weeklyLoading;
  }

  /** 取某只股票的周线序列 [{week, close}, ...] */
  function weeklySeries(code) {
    if (!S.weekly) return null;
    const st = S.weekly.stocks && S.weekly.stocks[code];
    if (!st) return null;
    const weeks = S.weekly.weeks;
    const out = [];
    if (st.b !== undefined) {
      for (let k = 0; k < st.c.length; k++) out.push({ week: weeks[st.b + k], close: st.c[k] });
    } else {
      for (let k = 0; k < st.c.length; k++) out.push({ week: weeks[st.i[k]], close: st.c[k] });
    }
    return out;
  }

  /* ------------------------------------------------ 单股周线（按需、前复权）
     整包 weekly.json 有 4MB+，国内访问 raw.githubusercontent 基本拉不完，
     所以点开图表时只取这一只股票的周线（几 KB，国内 CDN 秒开，前复权口径），
     失败才退回整合包。 */
  const WK_URL = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=';

  async function fetchWeeklyOne(code, fq) {
    fq = (fq === undefined || fq === null) ? 'qfq' : fq;   // 默认前复权（看盘口径）
    if (!S.weeklyOne) S.weeklyOne = {};
    const key = code + '|' + fq;
    if (S.weeklyOne[key]) return S.weeklyOne[key];
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
      S.weeklyOne[key] = out;
      return out;
    } finally { clearTimeout(timer); }
  }

  /* ------------------------------------------------ 实时行情（JSONP 式 script 注入） */
  function readQuoteGlobals(codes, out) {
    codes.forEach((c) => {
      const k = 'v_' + c.replace('.', '');
      const raw = window[k];
      if (typeof raw === 'string' && !out[c]) {
        const p = raw.split('~');
        if (p.length > 40) {
          out[c] = { name: p[1], price: +p[3] || 0, preclose: +p[4] || 0,
                     chg: +p[32] || 0, ts: p[30] || '', mv: +p[44] || 0 };
        }
      }
    });
  }

  function fetchQuotes(codes) {
    codes = (codes || []).filter(Boolean);
    if (!codes.length) return Promise.resolve({});
    return new Promise((resolve) => {
      const out = {};
      let settled = false;
      const finish = () => {
        if (settled) return; settled = true;
        readQuoteGlobals(codes, out);
        codes.forEach((c) => { try { delete window['v_' + c.replace('.', '')]; } catch (e) {} });
        resolve(out);
      };
      const s = document.createElement('script');
      s.charset = 'gbk';
      s.src = 'https://qt.gtimg.cn/q=' + codes.map((c) => c.replace('.', '')).join(',') + '&_=' + Date.now();
      s.onload = finish;
      s.onerror = finish;
      setTimeout(finish, 9000);
      document.head.appendChild(s);
    });
  }

  async function refreshQuotes(codes) {
    const q = await fetchQuotes(codes);
    Object.assign(S.quotes, q);
    return q;
  }

  /* ------------------------------------------------ 视图：今日 */
  /* -------------------------------------------- 下一次扫描（GitHub 定时推算） */
  const SCAN_SCHEDULE = [   // 与 .github/workflows/alert.yml 的北京时间 cron 保持一致
    { hm: '10:05', label: '盘中扫描' },
    { hm: '11:05', label: '盘中扫描' },
    { hm: '14:05', label: '盘中扫描' },
    { hm: '14:55', label: '尾盘扫描' },
    { hm: '15:15', label: '收盘确认' },
    { hm: '16:07', label: '数据更新' },
  ];

  function nextScan(now) {
    now = now || new Date();
    for (let add = 0; add < 8; add++) {
      const base = new Date(now.getFullYear(), now.getMonth(), now.getDate() + add);
      const wd = base.getDay();
      if (wd === 0 || wd === 6) continue;          // cron 只排周一至周五
      for (const s of SCAN_SCHEDULE) {
        const t = new Date(base.getFullYear(), base.getMonth(), base.getDate(),
                           +s.hm.slice(0, 2), +s.hm.slice(3, 5));
        if (t > now) return { time: t, hm: s.hm, label: s.label, dayOffset: add };
      }
    }
    return null;
  }

  function nextScanMetric() {
    const ns = nextScan();
    if (!ns) return '';
    const dayWord = ns.dayOffset === 0 ? '今天' : ns.dayOffset === 1 ? '明天'
      : '周' + '日一二三四五六'[ns.time.getDay()];
    const mins = Math.max(0, Math.round((ns.time.getTime() - Date.now()) / 60000));
    const left = mins >= 60
      ? Math.floor(mins / 60) + ' 小时 ' + (mins % 60) + ' 分钟后'
      : mins + ' 分钟后';
    return '<div class="metric hero"><div class="k">下一次扫描（GitHub 定时 · 交易日）</div>' +
      '<div class="v">' + esc(dayWord + ' ' + ns.hm) + '</div>' +
      '<div class="x">' + esc(ns.label) + ' · ' + esc(left) +
      ' · 定时可能有数分钟延迟，节假日自动跳过</div></div>';
  }

  function renderToday() {
    const d = S.data;
    const t = d.triggers || {};
    const rows = t.rows || [];
    const today = d.today;

    $('subTitle').textContent = '数据 ' + (d.health && d.health.last_update ? d.health.last_update : '—') +
      ' · ' + (t.day || '—');

    const pill = $('dayPill');
    const isToday = t.day === today;
    pill.className = 'pill ' + (isToday ? 'on' : 'off');
    pill.innerHTML = '<i class="dot"></i><span>' +
      (isToday ? '今日已扫描' + (t.ts ? ' · ' + esc(t.ts.slice(11, 16)) : '')
               : '非交易日 / 未扫描') + '</span>';

    // Hero
    const newOnes = rows.filter((r) => r.is_new).length;
    const modeLabel = t.mode_label || '扫描';
    const ts = t.ts || '';
    $('todayHero').innerHTML =
      '<div class="metrics c2">' +
        '<div class="metric hero"><div class="k">当日触及 60 周线（A 类上穿）</div>' +
          '<div class="v up">' + rows.length + ' <span style="font-size:15px;font-weight:600">只</span></div>' +
          '<div class="x">' + esc(t.day || '—') + ' · ' + esc(modeLabel) + ' · 快照 ' + (t.scanned || 0) + ' 只</div>' +
        '</div>' +
        '<div class="metric"><div class="k">本次新触发</div><div class="v sm">' + newOnes + ' 只</div>' +
          '<div class="x">其余今日已推送过</div></div>' +
        '<div class="metric"><div class="k">扫描时间</div><div class="v sm">' +
          esc(ts ? ts.slice(5, 16).replace('T', ' ') : '—') + '</div><div class="x">' + timeAgo(t.ts) + '</div></div>' +
        nextScanMetric() +
      '</div>';

    // 列表
    const list = $('trigList');
    list.innerHTML = '';
    $('trigHint').textContent = rows.length ? '点击查看周 K 与买卖点' : '';
    if (!rows.length) {
      list.appendChild(el('div', 'empty',
        '<div class="big">◌</div>当日暂无标的触及 60 周线。<br>盘中扫描时刻：10:05 / 11:05 / 14:05 / 14:55'));
      $('trigNote').textContent = '';
      return;
    }
    rows.forEach((r) => list.appendChild(trigRow(r)));
    $('trigNote').innerHTML =
      '口径：严格型 A 类上穿（上周收盘 &lt; 60 周线 且 现价 ≥ 60 周线）。' +
      '「偏离」＝ 现价相对 60 周线的乖离；「量比」＝ 今日成交额 / 近 20 日均成交额。';
  }

  function trigRow(r) {
    const row = el('div', 'row tap');
    const devCls = r.dev_pct > 0 ? 'up' : 'down';
    row.innerHTML =
      '<div class="grow">' +
        '<div class="t1">' + esc(r.name) + ' <span class="tag">' + esc(r.industry) + '</span></div>' +
        '<div class="t2"><span class="code">' + esc(r.code.replace('.', '')) + '</span>' +
          '<span>量比 ' + num(r.amt_ratio, 2) + '</span>' +
          '<span>年内上穿 ' + num(r.a_events_1y, 0) + ' 次</span>' +
          '<span class="' + devCls + '">偏离 ' + pct(r.dev_pct, 2) + '</span>' +
        '</div>' +
      '</div>' +
      '<div class="r">' +
        '<div class="p">' + num(r.price, 2) + '</div>' +
        '<div class="s ' + cls(r.chg_pct) + '">' + pct(r.chg_pct, 2) + '</div>' +
      '</div>';
    row.addEventListener('click', () => openKline(r.code, r.name));
    return row;
  }

  /* ------------------------------------------------ 视图：运行 */
  function renderSystem() {
    const d = S.data;
    const h = d.health || {};

    $('healthGrid').innerHTML =
      metric('覆盖标的', h.stocks || 0, '只（全A在市）') +
      metric('周线记录', ((h.weekly_rows || 0) / 10000).toFixed(1), '万行') +
      metric('最新数据日', h.last_update || '—', 'baostock 后复权') +
      metric('图表累计', h.chart_count || 0, '张（近 7 个交易日）') +
      metric('数据仓体积', h.store_mb || 0, 'MB') +
      metric('名称覆盖', h.names_covered || 0, '只');
    $('healthNote').innerHTML =
      '数据源 ' + esc(h.source || '—') + '；最近出图日：' +
      ((h.chart_days || []).slice(-4).join('、') || '—') +
      '。<br>每个交易日的 16:07 会自动增量更新数据仓，10:05 / 11:05 / 14:05 / 14:55 盘中扫描，15:15 收盘确认。';

    // 额度
    const p = d.push || {};
    const used = p.count || 0, lim = p.limit || 5;
    const rate = Math.min(100, Math.round(used / lim * 100));
    $('quotaBox').innerHTML =
      '<div style="display:flex;align-items:baseline;gap:8px">' +
        '<span style="font-size:26px;font-weight:680;letter-spacing:-.6px">' + used + '</span>' +
        '<span class="muted" style="font-size:13px">/ ' + lim + ' 条</span>' +
        '<span class="muted" style="margin-left:auto;font-size:12px">' + esc(p.date || '—') + '</span>' +
      '</div>' +
      '<div class="bar-track"><div class="bar-fill" style="width:' + rate +
        '%;background:' + (rate >= 100 ? 'var(--up)' : 'var(--blue)') + '"></div></div>' +
      '<div class="note" style="padding:10px 0 0">Server酱 免费额度每日 5 条。' +
      '盘中扫描仅在「有新触发」时推送，收盘确认批每次必推，护栏确保不超限。</div>';

    // 趋势
    const tr = d.trend || [];
    $('trendHint').textContent = tr.length ? (tr[0].day.slice(5) + ' ~ ' + tr[tr.length - 1].day.slice(5)) : '';
    drawTrend(tr);

    // 运行流水
    const list = $('runList');
    list.innerHTML = '';
    const runs = (d.runs || []).slice(0, 12);
    if (!runs.length) {
      list.appendChild(el('div', 'empty', '<div class="big">◌</div>暂无运行记录'));
      return;
    }
    runs.forEach((r) => {
      const st = r.status === 'completed' ? (r.conclusion === 'success' ? 'ok' : 'fail') : 'run';
      const badge = st === 'ok' ? '<span class="tag down">成功</span>'
        : st === 'fail' ? '<span class="tag up">' + esc(r.conclusion || '失败') + '</span>'
        : '<span class="tag orange">进行中</span>';
      const row = el('div', 'row tap');
      row.innerHTML =
        '<div class="grow"><div class="t1">' + esc(r.label) + ' ' + badge + '</div>' +
        '<div class="t2"><span>' + esc(r.at) + '</span><span>' + esc(r.event || '') + '</span>' +
        '<span class="code">' + esc(r.sha || '') + '</span></div></div>' +
        '<div class="r"><div class="p">' + (r.duration_s >= 60 ?
          (r.duration_s / 60).toFixed(1) + ' 分' : r.duration_s + ' 秒') + '</div>' +
        '<div class="s muted">' + (r.status === 'completed' ? '已完成' : '运行中') + '</div></div>';
      if (r.url) row.addEventListener('click', () => window.open(r.url, '_blank'));
      list.appendChild(row);
    });
  }

  function metric(k, v, x, cl) {
    return '<div class="metric"><div class="k">' + esc(k) + '</div>' +
      '<div class="v sm ' + (cl || '') + '">' + esc(v) + '</div>' +
      (x ? '<div class="x">' + esc(x) + '</div>' : '') + '</div>';
  }

  let trendChart = null;
  function drawTrend(tr) {
    const node = $('trendChart');
    if (!tr.length) { node.innerHTML = '<div class="empty">还没有积累足够的扫描记录</div>'; return; }
    trendChart = trendChart || echarts.init(node);
    trendChart.setOption({
      grid: { left: 34, right: 12, top: 16, bottom: 26 },
      tooltip: {
        trigger: 'axis',
        formatter: (ps) => {
          const i = ps[0].dataIndex, t = tr[i];
          return t.day + '<br/>触发 <b>' + t.peak + '</b> 只<br/>最近批次（' +
            (t.mode === 'intraday' ? '盘中' : t.mode === 'confirm' ? '收盘' : t.mode) + '）' + t.n + ' 只';
        },
        backgroundColor: 'rgba(28,28,30,.92)', borderWidth: 0,
        textStyle: { color: '#fff', fontSize: 12 },
      },
      xAxis: {
        type: 'category',
        data: tr.map((t) => t.day.slice(5)),
        axisLine: { lineStyle: { color: '#8e8e93', width: .5 } },
        axisTick: { show: false },
        axisLabel: { color: '#8e8e93', fontSize: 10, interval: 'auto' },
      },
      yAxis: {
        type: 'value',
        splitLine: { lineStyle: { color: 'rgba(142,142,147,.18)' } },
        axisLabel: { color: '#8e8e93', fontSize: 10 },
      },
      series: [{
        type: 'bar',
        data: tr.map((t) => t.peak),
        itemStyle: { color: '#007aff', borderRadius: [4, 4, 0, 0] },
        barMaxWidth: 20,
        markLine: {
          silent: true, symbol: 'none',
          lineStyle: { color: '#ff9500', type: 'dashed', width: 1 },
          label: { formatter: '额度 5', color: '#ff9500', fontSize: 10, position: 'insideEndTop' },
          data: [{ yAxis: 5 }],
        },
      }],
    });
    trendChart.resize();
  }

  /* ------------------------------------------------ 周K 图 */
  let kChart = null;
  let kContext = null;

  async function openKline(code, name) {
    kContext = { code: code, name: name };
    $('chartTitle').textContent = name + ' · 周 K';
    $('chartMask').classList.add('show');
    $('kChart').innerHTML = '<div class="loading"><span class="spinner"></span> 正在加载周线…</div>';
    // 快速路径：单股前复权周线；样本不足 61 根（新股）或失败时退回整合包
    let series = await fetchWeeklyOne(code).catch(() => null);
    if (!series || series.length < 61) {
      try {
        await loadWeekly();
        const s2 = weeklySeries(code);
        if (s2 && s2.length) series = s2;
      } catch (e) { /* 保留快速路径结果 */ }
    }
    if (!series || !series.length) {
      $('kChart').innerHTML =
        '<div class="empty">周线数据获取失败<br>请检查网络后重试</div>';
      return;
    }
    renderKline(code, series);
    renderChartStats(code, series);
  }

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

  function renderKline(code, series) {
    const node = $('kChart');
    // 必须先销毁旧实例再重建：上一张图的画布已被 innerHTML 清空，
    // 复用旧实例会画到已脱离 DOM 的画布上 → 第二次打开看不到图
    if (kChart && !kChart.isDisposed()) kChart.dispose();
    node.innerHTML = '';
    kChart = echarts.init(node);
    const weeks = series.map((d) => d.week);
    const closes = series.map((d) => d.close);
    const m60 = ma(closes, 60);

    const marks = (window.WB_ACCOUNT && window.WB_ACCOUNT.marksFor(code)) || { buys: [], sells: [], holds: [] };
    const buyPts = marks.buys.map((m) => ({
      name: 'B', coord: [m.week, m.price],
      value: 'B', symbol: 'pin', symbolSize: 34,
      itemStyle: { color: '#ff3b30' }, label: { color: '#fff', fontSize: 11, fontWeight: 700 },
    }));
    const sellPts = marks.sells.map((m) => ({
      name: 'S', coord: [m.week, m.price],
      value: 'S', symbol: 'pin', symbolSize: 34,
      itemStyle: { color: '#34c759' }, label: { color: '#fff', fontSize: 11, fontWeight: 700 },
    }));

    kChart.setOption({
      animation: false,
      grid: { left: 46, right: 14, top: 22, bottom: 30 },
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(28,28,30,.92)', borderWidth: 0,
        textStyle: { color: '#fff', fontSize: 12 },
        formatter: (ps) => {
          if (!ps || !ps.length) return '';
          const i = ps[0].dataIndex;
          let s = weeks[i] + '<br/>周收盘 <b>' + closes[i].toFixed(2) + '</b>';
          if (m60[i] != null) s += '<br/>60 周线 ' + m60[i].toFixed(2);
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
          name: '周收盘', type: 'line', data: closes, smooth: false,
          symbol: 'none', lineStyle: { width: 1.8, color: '#007aff' },
          areaStyle: {
            color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [{ offset: 0, color: 'rgba(0,122,255,.16)' },
                           { offset: 1, color: 'rgba(0,122,255,0)' }] },
          },
          markPoint: { data: buyPts.concat(sellPts), silent: true },
        },
        {
          name: '60 周线', type: 'line', data: m60, symbol: 'none',
          lineStyle: { width: 1.8, color: '#ff9500' },
        },
      ],
    });
    kChart.resize();
  }

  function renderChartStats(code, series) {
    const last = series[series.length - 1];
    const closes = series.map((d) => d.close);
    const m60 = ma(closes, 60);
    const maNow = m60[m60.length - 1];
    const dev = maNow ? (last.close / maNow - 1) * 100 : null;
    const st = (S.static || {});
    const ind = (st.industry || {})[code] || '—';
    const q = S.quotes[code];
    $('chartStats').innerHTML =
      metric('最新周收盘', num(last.close, 2), last.week) +
      metric('60 周线', num(maNow, 2), dev == null ? '' : '偏离 ' + pct(dev, 2)) +
      metric('申万行业', ind, q ? '最新价 ' + num(q[0], 2) : '');
    $('chartNote').innerHTML =
      '曲线为<b>前复权周收盘价</b>与 <b>60 周均线</b>（换前复权只改变价格刻度，' +
      '收盘/均线比值与预警信号完全一致）。' +
      'B / S 标记来自你的模拟账户在该标的上的买卖点。';
  }

  /* ------------------------------------------------ 弹层与 Tab */
  function bindSheets() {
    $('chartClose').addEventListener('click', () => $('chartMask').classList.remove('show'));
    $('chartMask').addEventListener('click', (e) => { if (e.target.id === 'chartMask') $('chartMask').classList.remove('show'); });
    $('tradeClose').addEventListener('click', closeTrade);
    $('btnCancel').addEventListener('click', closeTrade);
    $('tradeMask').addEventListener('click', (e) => { if (e.target.id === 'tradeMask') closeTrade(); });
    $('chartBuy').addEventListener('click', () => { if (kContext) { $('chartMask').classList.remove('show'); window.WB_ACCOUNT.openTrade('buy', kContext.code, kContext.name); } });
    $('chartSell').addEventListener('click', () => { if (kContext) { $('chartMask').classList.remove('show'); window.WB_ACCOUNT.openTrade('sell', kContext.code, kContext.name); } });
  }

  function closeTrade() { $('tradeMask').classList.remove('show'); }

  function bindTabs() {
    document.querySelectorAll('.tabbar button').forEach((b) => {
      b.addEventListener('click', () => {
        document.querySelectorAll('.tabbar button').forEach((x) => x.classList.remove('on'));
        document.querySelectorAll('.tabpane').forEach((x) => x.classList.remove('on'));
        b.classList.add('on');
        $('pane-' + b.dataset.tab).classList.add('on');
        window.scrollTo({ top: 0 });
        if (b.dataset.tab === 'system' && trendChart) setTimeout(() => trendChart.resize(), 50);
        if (b.dataset.tab === 'account' && window.WB_ACCOUNT) window.WB_ACCOUNT.onShow();
      });
    });
  }

  /** 支持 ?tab=today|system|account 直达（也用于分享与自动截图） */
  function applyTabFromUrl() {
    const t = new URL(location.href).searchParams.get('tab');
    if (!t) return;
    const b = document.querySelector('.tabbar button[data-tab="' + t + '"]');
    if (b) b.click();
  }

  window.addEventListener('resize', () => {
    if (trendChart) trendChart.resize();
    if (kChart) kChart.resize();
    if (window.WB_ACCOUNT) window.WB_ACCOUNT.resize();
  });

  /* ------------------------------------------------ 启动 */
  async function boot() {
    bindTabs();
    bindSheets();
    try {
      const [d, st] = await Promise.all([loadJSON('data.json'), loadJSON('static.json')]);
      S.data = d;
      S.static = st;
      renderToday();
      renderSystem();
      window.WB_ACCOUNT.boot();
      applyTabFromUrl();
      // 兜底价格源（可选，失败不影响主流程）
      loadJSON('quotes.json').then((q) => {
        S.quotes = (q && q.items) || {};
        S.quotesDate = (q && q.date) || '';
        if (window.WB_ACCOUNT) window.WB_ACCOUNT.onQuotes();
      }).catch(() => {});
    } catch (e) {
      $('todayHero').innerHTML = '<div class="banner err">数据加载失败：' + esc(e.message) +
        '<br><br>请确认仓库已推送 dashboard/ 数据文件。</div>';
      $('trigList').innerHTML = '';
    }
  }

  window.WB = {
    S: S, $: $, el: el, esc: esc, num: num, pct: pct, cls: cls, metric: metric,
    loadJSON: loadJSON, loadWeekly: loadWeekly, weeklySeries: weeklySeries,
    fetchWeeklyOne: fetchWeeklyOne,
    fetchQuotes: fetchQuotes, refreshQuotes: refreshQuotes,
    openKline: openKline, ma: ma, timeAgo: timeAgo, yi: yi, wan: wan,
    REPO: REPO, REMOTE: REMOTE, QIMG: QIMG,
    closeTrade: closeTrade,
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
