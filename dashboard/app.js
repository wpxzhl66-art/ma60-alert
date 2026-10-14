/* ============================================================
   60周线预警台 · 核心前端
   数据来源：GitHub 仓库 dashboard/ 目录（每日由 Actions 更新），
             远端不可达时回退到本地同目录文件。
   ============================================================ */
(function () {
  'use strict';

  const REPO = 'wpxzhl66-art/ma60-alert';
  const REMOTE = 'https://raw.githubusercontent.com/' + REPO + '/main/dashboard';

  // 交付物版本号：每次改动递增，界面右上角可见（见 CHANGELOG.md）
  const APP_VERSION = 'v1.5.1';

  const S = {
    data: null,        // data.json
    static: null,      // static.json
    quotes: {},        // quotes.json：全市场最新价（账户估值兜底）
    quotesDate: '',
    one: {},           // 单股/单品种周线缓存（按需拉取，key = code|fq）
    codeIndex: null,   // 搜索用
    fut: null,         // fut.json：期货加权指数板块（切到该页才拉）
    futLoading: null,
    sort: {},          // 各列表的当前排序 {storeKey: {id, dir}}
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
  const yi = (v) => (v == null || isNaN(v) || !v) ? '—' : Number(v).toFixed(1) + ' 亿';

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

  /* ------------------------------------------------ 排序
     三个列表共用一套「排序栏」：
       · 点未选中的字段 → 切到该字段，用它的默认方向
       · 再点已选中的字段 → 翻转方向
       · 选择写入 localStorage，下次打开保持

     字段描述 { id, label, key(item), dir 默认方向, abs 按绝对值 }
     注意 abs（只给「偏离」用）：偏离是有正负的，但用户关心的是「离 60 周线多远」，
     所以排序键取绝对值 —— 越接近 0 越靠前。 */
  const SORT_LS = 'wb.sort.';

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

  /** 挂载排序栏到 S.sort[storeKey]，状态变化时回调 onChange */
  function mountSort(storeKey, nodeId, spec, onChange) {
    S.sort[storeKey] = readSort(storeKey, spec);
    const node = $(nodeId);
    const repaint = () => drawSortBar(node, spec, S.sort[storeKey], (id) => {
      const cur = S.sort[storeKey];
      if (id === cur.id) cur.dir = cur.dir === 'asc' ? 'desc' : 'asc';
      else {
        const f = spec.filter((s) => s.id === id)[0];
        S.sort[storeKey] = { id: id, dir: f.dir || 'desc' };
      }
      writeSort(storeKey, S.sort[storeKey]);
      repaint();
      onChange();
    });
    repaint();
  }

  /* A 股触发明细的排序字段（前 4 项为用户明确要做排序的口径） */
  const TRIG_SORTS = [
    { id: 'dev', label: '偏离', key: (r) => r.dev_pct, abs: true, dir: 'asc' },
    { id: 'chg', label: '涨跌幅', key: (r) => r.chg_pct, dir: 'desc' },
    { id: 'price', label: '股价', key: (r) => r.price, dir: 'desc' },
    { id: 'mv', label: '市值', key: (r) => r.mv_float_yi, dir: 'desc' },
    { id: 'amt', label: '成交额', key: (r) => r.amount_wan, dir: 'desc' },
    { id: 'vr', label: '量比', key: (r) => r.amt_ratio, dir: 'desc' },
    { id: 'to', label: '换手率', key: (r) => r.turnover, dir: 'desc' },
    { id: 'ev', label: '年内上穿', key: (r) => r.a_events_1y, dir: 'desc' },
    { id: 'ind', label: '行业', key: (r) => r.industry, dir: 'asc' },
  ];

  /* 期货品种共用一套排序（触发列表与全品种列表各记一份选择） */
  const FUT_SORTS = [
    { id: 'dev', label: '偏离', key: (r) => r.dev_pct, abs: true, dir: 'asc' },
    { id: 'price', label: '加权价', key: (r) => r.price, dir: 'desc' },
    { id: 'oi', label: '持仓量', key: (r) => r.oi_sum, dir: 'desc' },
    { id: 'ev', label: '年内上穿', key: (r) => r.up_cnt_1y, dir: 'desc' },
    { id: 'sec', label: '板块', key: (r) => r.sector, dir: 'asc' },
  ];

  /* 排序字段若不在行内固定展示，就在副标题里补一项，
     否则用户看不出「凭什么这样排」 */
  const extraTxt = (id, r) => {
    if (id === 'mv') return '市值 ' + yi(r.mv_float_yi);
    if (id === 'amt') return '成交额 ' + amtWan(r.amount_wan);
    if (id === 'to') return '换手 ' + num(r.turnover, 2) + '%';
    if (id === 'oi') return '持仓 ' + oiTxt(r.oi_sum);
    return null;
  };

  function extraSpan(id, r) {
    const t = extraTxt(id, r);
    return t ? '<span>' + esc(t) + '</span>' : '';
  }

  /** 元 → 万元/亿元（腾讯成交额原始单位为万元） */
  function amtWan(w) {
    if (w == null || isNaN(w) || !w) return '—';
    return w >= 10000 ? (w / 10000).toFixed(2) + ' 亿' : Math.round(w) + ' 万';
  }

  /** 持仓量（手）→ 手/万手 */
  function oiTxt(v) {
    if (v == null || isNaN(v) || !v) return '—';
    return v >= 10000 ? (v / 10000).toFixed(1) + ' 万手' : Math.round(v) + ' 手';
  }

  /* ------------------------------------------------ 图表守卫
     ECharts 来自本地 vendor/（随站点发布），万一加载失败也不能拖垮整页：
     列表、指标照常渲染，只把图表区域换成提示。 */
  function initChart(node) {
    if (typeof window.echarts === 'undefined') {
      node.innerHTML = '<div class="empty">图表库未加载<br>请检查网络后刷新</div>';
      return null;
    }
    return window.echarts.init(node);
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

  /* ------------------------------------------------ 单股/单品种周线（按需、前复权）
     整包 weekly.json（4MB+）已于 v1.2.1 停用——国内访问 raw.githubusercontent
     基本拉不完，点开股票会一直转圈。现在只取这一只的周线（几 KB，前复权口径，
     与行情软件显示一致），失败就直接提示，不再做整包回退。 */
  const WK_URL = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=';

  async function fetchWeeklyOne(code, fq) {
    fq = (fq === undefined || fq === null) ? 'qfq' : fq;   // 默认前复权（看盘口径）
    const key = code + '|' + fq;
    if (S.one[key]) return S.one[key];
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
      S.one[key] = out;
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

    mountSort('trig', 'trigSort', TRIG_SORTS, renderTrigs);
    renderTrigs();
  }

  /** 今日触发明细列表（排序变化时单独重画，不动 Hero） */
  function renderTrigs() {
    const rows = (S.data && S.data.triggers && S.data.triggers.rows) || [];
    const list = $('trigList');
    list.innerHTML = '';
    if (!rows.length) {
      $('trigHint').textContent = '';
      list.appendChild(el('div', 'empty',
        '<div class="big">◌</div>当日暂无标的触及 60 周线。<br>盘中扫描时刻：10:05 / 11:05 / 14:05 / 14:55'));
      $('trigNote').textContent = '';
      return;
    }
    const st = S.sort.trig;
    const sorted = sortRows(rows, TRIG_SORTS, st);
    $('trigHint').textContent =
      '共 ' + rows.length + ' 只 · ' + sortTip(curField(TRIG_SORTS, st), st.dir);
    sorted.forEach((r, i) => list.appendChild(trigRow(r, i)));
    $('trigNote').innerHTML =
      '口径：严格型 A 类上穿（上周收盘 &lt; 60 周线 且 现价 ≥ 60 周线）。' +
      '「偏离」＝ 现价高于或低于 60 周线的百分比；「量比」＝ 今日成交额 / 近 20 日均成交额。' +
      '切换排序后，若该字段平时不显示，会临时补在编号行里。';
  }

  function trigRow(r, i) {
    const row = el('div', 'row tap');
    const devCls = r.dev_pct > 0 ? 'up' : 'down';
    row.innerHTML =
      '<div class="idx">' + (i + 1) + '</div>' +
      '<div class="grow">' +
        '<div class="t1">' + esc(r.name) + ' <span class="tag">' + esc(r.industry) + '</span></div>' +
        '<div class="t2"><span class="code">' + esc(r.code.replace('.', '')) + '</span>' +
          '<span>量比 ' + num(r.amt_ratio, 2) + '</span>' +
          '<span>年内上穿 ' + num(r.a_events_1y, 0) + ' 次</span>' +
          '<span class="' + devCls + '">偏离 ' + pct(r.dev_pct, 2) + '</span>' +
          extraSpan(S.sort.trig.id, r) +
        '</div>' +
      '</div>' +
      '<div class="r">' +
        '<div class="p">' + num(r.price, 2) + '</div>' +
        '<div class="s ' + cls(r.chg_pct) + '">' + pct(r.chg_pct, 2) + '</div>' +
      '</div>';
    row.addEventListener('click', () => openKline(r.code, r.name));
    return row;
  }

  /* ------------------------------------------------ 视图：期货
     期货品种的「持仓量加权指数」，与 A 股分开成独立板块。
     加权口径：加权价 = Σ(合约收盘 × 合约持仓量) / Σ(持仓量)。
     数据由云端每日合成后写入 dashboard/fut.json，切到本页才按需加载。 */
  function loadFutures() {
    if (S.fut) return Promise.resolve(S.fut);
    if (S.futLoading) return S.futLoading;
    S.futLoading = loadJSON('fut.json').then((d) => { S.fut = d; return d; })
      .catch((e) => { S.futLoading = null; throw e; });
    return S.futLoading;
  }

  function futPrice(v) {
    if (v == null || isNaN(v)) return '—';
    const a = Math.abs(v);
    return Number(v).toFixed(a >= 1000 ? 1 : a >= 100 ? 2 : 3);
  }

  function futTag(st) {
    return st === '突破' ? '<span class="tag bo">突破</span>'
                         : '<span class="tag near">临近</span>';
  }

  function futRow(r, i) {
    const row = el('div', 'row tap');
    row.innerHTML =
      '<div class="idx">' + (i + 1) + '</div>' +
      '<div class="grow">' +
        '<div class="t1">' + esc(r.name) + '加权 ' + futTag(r.status) +
          ' <span class="tag">' + esc(r.sector) + '</span></div>' +
        '<div class="t2"><span class="code">' + esc(r.code) + '</span>' +
          '<span>60 周线 ' + futPrice(r.ma60) + '</span>' +
          '<span class="' + (r.dev_pct > 0 ? 'up' : 'down') + '">偏离 ' +
            pct(r.dev_pct * 100, 2) + '</span>' +
          '<span>年内上穿 ' + num(r.up_cnt_1y, 0) + ' 次</span>' +
          extraSpan(S.sort.fut.id, r) +
        '</div>' +
      '</div>' +
      '<div class="r">' +
        '<div class="p">' + futPrice(r.price) + '</div>' +
        '<div class="s muted">' + esc(r.slope) + '</div>' +
      '</div>';
    row.addEventListener('click', () => openFutKline(r.code, r.name));
    return row;
  }

  function futAllRow(r, i) {
    const row = el('div', 'row tap');
    const st = r.near ? futTag(r.status) : '<span class="tag">—</span>';
    row.innerHTML =
      '<div class="idx">' + (i + 1) + '</div>' +
      '<div class="grow">' +
        '<div class="t1">' + esc(r.name) + '加权 ' + st +
          ' <span class="tag">' + esc(r.sector) + '</span></div>' +
        '<div class="t2"><span class="code">' + esc(r.code) + '</span>' +
          '<span>' + esc(r.market) + '</span>' +
          '<span>60 周线 ' + futPrice(r.ma60) + '</span>' +
          '<span>年内上穿 ' + num(r.up_cnt_1y, 0) + ' 次</span>' +
          extraSpan(S.sort.futall.id, r) +
        '</div>' +
      '</div>' +
      '<div class="r">' +
        '<div class="p">' + futPrice(r.price) + '</div>' +
        '<div class="s ' + (r.dev_pct > 0 ? 'up' : 'down') + '">' +
          pct(r.dev_pct * 100, 2) + '</div>' +
      '</div>';
    if (r.chart) row.addEventListener('click', () => openFutKline(r.code, r.name));
    else row.classList.remove('tap');
    return row;
  }

  /** 期货触发列表（排序变化时单独重画） */
  function renderFutList() {
    const d = S.fut || {};
    const rows = d.rows || [];
    const list = $('futList');
    list.innerHTML = '';
    if (!rows.length) {
      $('futHint').textContent = '';
      list.appendChild(el('div', 'empty',
        '<div class="big">◌</div>今日暂无期货品种靠近 60 周线。'));
      return;
    }
    const st = S.sort.fut;
    $('futHint').textContent =
      '共 ' + rows.length + ' 个 · ' + sortTip(curField(FUT_SORTS, st), st.dir);
    sortRows(rows, FUT_SORTS, st).forEach((r, i) => list.appendChild(futRow(r, i)));
  }

  /** 期货全品种列表 */
  function renderFutAll() {
    const d = S.fut || {};
    const all = d.all || [];
    const hp = d.health || {};
    const list = $('futAllList');
    list.innerHTML = '';
    const st = S.sort.futall;
    $('futAllHint').textContent =
      all.length + ' 个品种 · ' + sortTip(curField(FUT_SORTS, st), st.dir);
    sortRows(all, FUT_SORTS, st).forEach((r, i) => list.appendChild(futAllRow(r, i)));
    const covered = hp.covered || all.length;
    const total = hp.total || all.length;
    $('futAllNote').innerHTML = covered < total
      ? '已纳入 <b>' + covered + '</b> / ' + total + ' 个品种；' +
        '其余品种历史合约不足 61 周（新上市品种），每周自动重试。'
      : '全部 <b>' + total + '</b> 个品种均在扫描范围内（上期所 20 · 大商所 23 · ' +
        '郑商所 26 · 上能源 5 · 中金所 8）。';
  }

  async function renderFutures() {
    const hero = $('futHero');
    hero.innerHTML = '<div class="loading"><span class="spinner"></span> 正在加载期货加权数据…</div>';
    let d;
    try {
      d = await loadFutures();
    } catch (e) {
      hero.innerHTML = '<div class="banner err">期货数据加载失败：' + esc(e.message) +
        '<br><br>请确认仓库已推送 dashboard/fut.json。</div>';
      $('futList').innerHTML = '';
      $('futAllList').innerHTML = '';
      return;
    }
    const rows = d.rows || [];
    const all = d.all || [];
    const hp = d.health || {};
    const breakouts = rows.filter((r) => r.status === '突破');
    const nears = rows.filter((r) => r.status !== '突破');

    hero.innerHTML =
      '<div class="metrics c2">' +
        '<div class="metric hero"><div class="k">今日触发（加权价格 vs 60 周线）</div>' +
          '<div class="v up">' + rows.length +
            ' <span style="font-size:15px;font-weight:600">个品种</span></div>' +
          '<div class="x">' + esc(d.day || '—') +
            ' · 覆盖 ' + (hp.covered || all.length) + ' / ' + (hp.total || all.length) +
            ' 个品种' + (d.ts ? ' · ' + esc(d.ts.slice(5, 16).replace('T', ' ')) : '') + '</div>' +
        '</div>' +
        '<div class="metric"><div class="k">上穿突破</div>' +
          '<div class="v sm up">' + breakouts.length + ' 个</div>' +
          '<div class="x">上一交易日在此线下方、当前已上穿</div></div>' +
        '<div class="metric"><div class="k">临近（±' +
          ((d.near_pct || 0.03) * 100).toFixed(0) + '%）</div>' +
          '<div class="v sm">' + nears.length + ' 个</div>' +
          '<div class="x">尚未上穿，但已在 60 周线附近</div></div>' +
        '<div class="metric"><div class="k">加权口径</div>' +
          '<div class="v sm">持仓量加权</div>' +
          '<div class="x">Σ(收盘×持仓) / Σ(持仓)，无换月跳空</div></div>' +
      '</div>';

    mountSort('fut', 'futSort', FUT_SORTS, renderFutList);
    mountSort('futall', 'futAllSort', FUT_SORTS, renderFutAll);
    renderFutList();
    renderFutAll();

    $('futNote').innerHTML =
      '口径：<b>突破</b>＝上一交易日加权收盘 &lt; 60 周线 且 最新加权价 ≥ 60 周线；' +
      '<b>临近</b>＝未上穿但与 60 周线的偏离在 ±' +
      ((d.near_pct || 0.03) * 100).toFixed(0) + '% 之内。' +
      '「加权」＝该品种全部在市合约按<b>持仓量</b>加权（' +
      'Σ(收盘×持仓量) / Σ(持仓量)），与文华/东财的「XX加权」同口径，' +
      '不会像主力连续那样在换月时跳空。';
  }

  async function openFutKline(code, name) {
    kContext = { code: code, name: name, kind: 'fut' };
    $('chartTitle').textContent = name + '加权 · 周 K';
    $('chartMask').classList.add('show');
    $('chartBuy').style.display = 'none';
    $('chartSell').style.display = 'none';
    $('kChart').innerHTML = '<div class="loading"><span class="spinner"></span> 正在加载周线…</div>';
    let d = S.fut;
    if (!d) { try { d = await loadFutures(); } catch (e) { d = null; } }
    const r = d && (d.rows || []).concat(d.all || []).find((x) => x.code === code);
    if (!r || !r.wk || !r.wk.length) {
      $('kChart').innerHTML =
        '<div class="empty">该品种周线数据缺失<br>（历史合约不足 61 周）</div>';
      return;
    }
    renderFutKline(r);
    renderFutStats(r);
  }

  function renderFutKline(r) {
    const node = $('kChart');
    if (kChart && !kChart.isDisposed()) kChart.dispose();
    node.innerHTML = '';
    kChart = initChart(node);
    if (!kChart) return;
    const weeks = r.wk.map((x) => x[0]);
    const closes = r.wk.map((x) => x[1]);
    const m60 = ma(closes, 60);
    kChart.setOption(weeklyChartOption(weeks, closes, m60, {
      closeLabel: '加权周收盘', color: '#5856d6', gridLeft: 52,
      areaTop: 'rgba(88,86,214,.16)', areaBottom: 'rgba(88,86,214,0)',
      fmt: futPrice,
    }));
    kChart.resize();
  }

  function renderFutStats(r) {
    $('chartStats').innerHTML =
      metric('加权价', futPrice(r.price), r.name + '加权') +
      metric('60 周线', futPrice(r.ma60), '偏离 ' + pct(r.dev_pct * 100, 2)) +
      metric('状态', r.status, r.sector + ' · 均线' + r.slope);
    $('chartNote').innerHTML =
      '曲线为该品种的<b>持仓量加权价</b>与 <b>60 周线</b>：' +
      'Σ(合约收盘 × 合约持仓量) / Σ(持仓量)，覆盖全部在市合约。' +
      '加权口径不产生换月跳空，因此 60 周线不会被主力换月的价格断崖污染。';
  }

  /* ------------------------------------------------ 视图：运行 */
  function renderSystem() {
    const d = S.data;
    const h = d.health || {};
    const us = h.update_stats || {};
    const cov = h.coverage || {};

    // 全市场覆盖面：把「扫描范围」讲清楚 —— 不是只扫一部分，而是全市场逐一判定
    const live = cov.market_live || h.market_total || 0;
    const have = h.stocks || 0;
    const shortN = cov.short_history != null ? cov.short_history
                                             : Math.max(0, live - have);
    const pct = live ? (have / live * 100) : 0;

    let coverCell = '';
    if (live) {
      coverCell = '<div class="metric hero"><div class="k">全市场覆盖</div>' +
        '<div class="v sm">' + have + ' / ' + live + ' 只 · ' + pct.toFixed(1) + '%</div>' +
        '<div class="x">上市不足 61 周的次新股 ' + shortN +
        ' 只（无法计算 60 周线，满 61 周自动纳入）' +
        (h.tx_codes ? ' · 其中 ' + h.tx_codes + ' 只由腾讯源兜底' : '') +
        '</div></div>';
    }

    // 抓取覆盖率：如实反映上次增量更新抓到多少，漏抓可见
    let fetchCell = '';
    if (us.todo || us.fetched) {
      const rate = us.todo ? Math.round(us.fetched / us.todo * 100) : 100;
      const tx = us.fallback || 0;
      fetchCell = '<div class="metric"><div class="k">数据仓抓取（上次增量更新）</div>' +
        '<div class="v sm">' + us.fetched + ' / ' + us.todo + ' 只 · ' + rate + '%</div>' +
        '<div class="x">' + esc(us.ts || '') +
        (tx ? ' · 其中 ' + tx + ' 只由腾讯兜底补齐' : '') +
        (us.unresolved ? ' · ' + us.unresolved + ' 只两源均未取到（已沿用旧值，覆盖面未缩水）'
                       : '') +
        (us.timed_out ? ' · 达到时间预算提前停止' : '') + '</div></div>';
    }

    $('healthGrid').innerHTML =
      metric('仓内标的', h.stocks || 0, '只（可计算 60 周线）') +
      metric('周线记录', ((h.weekly_rows || 0) / 10000).toFixed(1), '万行') +
      metric('最新数据日', h.last_update || '—', 'baostock 后复权') +
      metric('图表累计', h.chart_count || 0, '张（近 7 个交易日）') +
      metric('数据仓体积', h.store_mb || 0, 'MB') +
      metric('名称覆盖', h.names_covered || 0, '只') +
      coverCell + fetchCell;

    const gapTxt = us.gap_filled
      ? '本轮新纳入 <b>' + us.gap_filled + '</b> 只（腾讯源补齐）。' : '';
    $('healthNote').innerHTML =
      '<b>每次扫描都是全市场逐只判定，不做抽样、不留遗漏。</b>' +
      '「仓内标的」＝已纳入扫描、且历史足够计算 60 周线的标的数（' + have + ' 只）；' +
      '与全市场在市 ' + live + ' 只的差额是 <b>' + shortN +
      ' 只上市不足 61 周的次新股</b>——物理上还画不出 60 周线，' +
      '它们每周自动重试，满 61 周即刻进仓。<br>' +
      gapTxt +
      '数据源以 baostock 为主，腾讯行情为兜底（baostock 触发限流或个别标的缺数据时自动切换，' +
      '当前 ' + (h.tx_codes || 0) + ' 只走腾讯源）。每个交易日 16:07 自动增量更新，' +
      '合并写入、失败只沿用旧值、绝不缩小覆盖面；10:05 / 11:05 / 14:05 / 14:55 盘中扫描，15:15 收盘确认。';

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
    trendChart = trendChart || initChart(node);
    if (!trendChart) return;
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

  /** 周线折线图的公共配置。股票图与期货图共用，保证交互一致。 */
  function weeklyChartOption(weeks, closes, m60, o) {
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
          if (m60[i] != null) s += '<br/>60 周线 ' + o.fmt(m60[i]);
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
          name: '60 周线', type: 'line', data: m60, symbol: 'none',
          lineStyle: { width: 1.8, color: '#ff9500' },
        },
      ],
    };
  }

  async function openKline(code, name) {
    kContext = { code: code, name: name, kind: 'stock' };
    $('chartTitle').textContent = name + ' · 周 K';
    $('chartMask').classList.add('show');
    $('chartBuy').style.display = '';
    $('chartSell').style.display = '';
    $('kChart').innerHTML = '<div class="loading"><span class="spinner"></span> 正在加载周线…</div>';
    const series = await fetchWeeklyOne(code).catch(() => null);
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
    kChart = initChart(node);
    if (!kChart) return;
    const weeks = series.map((d) => d.week);
    const closes = series.map((d) => d.close);
    const m60 = ma(closes, 60);

    const marks = (window.WB_ACCOUNT && window.WB_ACCOUNT.marksFor(code, weeks)) ||
                  { buys: [], sells: [] };
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

    kChart.setOption(weeklyChartOption(weeks, closes, m60, {
      closeLabel: '周收盘', color: '#007aff',
      areaTop: 'rgba(0,122,255,.16)', areaBottom: 'rgba(0,122,255,0)',
      fmt: (v) => v.toFixed(2),
      markPoint: buyPts.concat(sellPts),
    }));
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
      metric('申万行业', ind, q ? '现价 ' + num(q[0], 2) : '');
    $('chartNote').innerHTML =
      '曲线为<b>前复权周收盘价</b>与 <b>60 周线</b>（换前复权只改变价格刻度，' +
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
        if (b.dataset.tab === 'futures') renderFutures();   // 懒加载：切到本页才拉 fut.json
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
    const vb = $('verBadge');
    if (vb) vb.textContent = APP_VERSION;
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

  /* 供 account.js 与测试脚本使用的最小导出集。别加没人用的东西。 */
  window.WB = {
    S: S, $: $, el: el, esc: esc, num: num, pct: pct, cls: cls, metric: metric,
    fetchWeeklyOne: fetchWeeklyOne, fetchQuotes: fetchQuotes,
    openKline: openKline, closeTrade: closeTrade,
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
