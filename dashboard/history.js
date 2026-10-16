/* ============================================================
   近一月历史触发 · 前端
   数据：history.json（由 src/dashboard/history.py 生成）
   判定口径与实时扫描一致：收盘由下向上越过 60 周线。
   ============================================================ */
(function () {
  'use strict';

  const C = window.WBcore;
  const { $, el, esc, num, pct, cls, metric, amtWan,
          curField, sortRows, sortTip, mountSort,
          loadJSON, fetchWeeklyOne, weeklyChartOption, ma, isRiskName } = C;

  // 交付物版本号：每次改动递增（见 CHANGELOG.md）
  const APP_VERSION = 'v1.19.1';

  const S = {
    d: null,          // history.json
    view: 'day',      // day 按日期 | stock 按股票
    scope: 'all',     // all | above | below
    q: '',            // 搜索词
  };

  /* ------------------------------------------------ 排序字段 */
  const DEV_TIP = (d) => '按偏离绝对值 ' + (d === 'asc' ? '升序 · 最贴近 60 周线' : '降序 · 偏离最远');

  const DAY_SORTS = [
    { id: 'dev', label: '偏离', key: (r) => r.dev, abs: true, dir: 'asc', tip: DEV_TIP },
    { id: 'chg', label: '涨跌幅', key: (r) => r.chg, dir: 'desc' },
    { id: 'px', label: '股价', key: (r) => r.px, dir: 'desc' },
    { id: 'amt', label: '成交额', key: (r) => r.amt, dir: 'desc' },
    { id: 'to', label: '换手率', key: (r) => r.to, dir: 'desc' },
    { id: 'ind', label: '行业', key: (r) => indOf(r.c), dir: 'asc' },
  ];

  const STOCK_SORTS = [
    { id: 'cnt', label: '触发次数', key: (r) => r.cnt, dir: 'desc' },
    { id: 'last', label: '最近触发', key: (r) => r.last, dir: 'desc' },
    { id: 'dev', label: '当前偏离', key: (r) => r.dev_now, abs: true, dir: 'asc', tip: DEV_TIP },
    { id: 'ret', label: '触发后收益', key: (r) => r.ret, dir: 'desc' },
    { id: 'mv', label: '市值', key: (r) => r.mv, dir: 'desc' },
    { id: 'ind', label: '行业', key: (r) => r.i, dir: 'asc' },
  ];

  const stkOf = (c) => (S.d.stocks || {})[c] || {};
  const indOf = (c) => stkOf(c).i || '未知';

  /* ------------------------------------------------ 工具 */
  const WD = '日一二三四五六';

  function fmtDay(d) {
    const t = new Date(d + 'T00:00:00');
    return d.slice(5) + ' 周' + WD[t.getDay()];
  }

  const c6 = (code) => String(code).replace('.', '');

  /* ------------------------------------------------ 概览 */
  function renderHero() {
    const s = S.d.summary || {};
    const d0 = S.d;
    const span = (d0.start || '').slice(5) + ' ~ ' + (d0.end || '').slice(5);
    const pctAbove = s.stocks ? Math.round(s.above_now / s.stocks * 100) : 0;
    $('heroBox').innerHTML =
      '<div class="metrics c2">' +
        metric('触发生效', s.events + ' 次', span) +
        metric('涉及标的', s.stocks + ' 只', s.trade_days + ' 个交易日') +
        metric('当前仍站上', s.above_now + ' 只', '占 ' + pctAbove + '%') +
        metric('当前已跌破', s.below_now + ' 只', '触发后回落') +
      '</div>' +
      '<div class="note">口径：收盘价由下向上越过 60 周线（后复权），与实时扫描一致。' +
        '「当前」指数据日 ' + esc(d0.data_date || '—') + '。<br/>' +
        '<b>信号性质</b>：14 年全历史回测（5.3 万次触发）显示，触发后跑输全市场等权 ' +
        '20 日 −2.1% / 60 日 −4.2% / 120 日 −6.8%，16 个年份无一例外 —— ' +
        '<b>这是掉队风险提示，不是买入信号</b>。下方「首次触发至今」是绝对涨跌，' +
        '要对照同期大盘看才有意义。</div>';
    $('subTitle').textContent =
      '最近 ' + d0.window_days + ' 天 · 数据截至 ' + (d0.data_date || '—') +
      ' · 生成于 ' + String(d0.generated_at || '').slice(5, 16).replace('T', ' ');
  }

  /* ------------------------------------------------ 每日触发条 */
  function renderCal() {
    const days = S.d.summary.days || [];
    const node = $('cal');
    node.innerHTML = '';
    if (!days.length) return;
    const max = Math.max(...days.map((x) => x[1]));
    days.forEach(([d, n]) => {
      const i = el('i');
      i.style.height = Math.max(3, Math.round(n / max * 100)) + '%';
      i.dataset.hot = n >= max * 0.6 ? '2' : (n >= max * 0.3 ? '1' : '0');
      i.title = d + '：' + n + ' 只';
      node.appendChild(i);
    });
    $('calX').innerHTML =
      '<span>' + days[0][0].slice(5) + '</span>' +
      '<span>峰值 ' + S.d.summary.peak.n + ' 只（' + S.d.summary.peak.d.slice(5) + '）</span>' +
      '<span>' + days[days.length - 1][0].slice(5) + '</span>';
    const avg = Math.round(days.reduce((a, x) => a + x[1], 0) / days.length);
    $('calHint').textContent = '日均 ' + avg + ' 只';
  }

  /* ------------------------------------------------ 行业分布 */
  function renderInds() {
    const inds = S.d.summary.industries || [];
    const node = $('indList');
    node.innerHTML = '';
    if (!inds.length) {
      node.innerHTML = '<div class="empty">暂无数据</div>';
      return;
    }
    const max = inds[0][1];
    inds.forEach(([name, n]) => {
      const row = el('div', 'indrow');
      row.innerHTML =
        '<div class="h"><b>' + esc(name) + '</b><span>' + n + ' 次</span></div>' +
        '<div class="bar-track"><div class="bar-fill" style="width:' +
          (n / max * 100).toFixed(1) + '%"></div></div>';
      node.appendChild(row);
    });
    $('indHint').textContent = '共 ' + inds.length + ' 个行业';
  }

  /* ------------------------------------------------ 列表 */
  /* 排序字段若不在行内固定展示，就补一项说明「凭什么这样排」 */
  const DAY_EXTRA = {
    amt: (r) => '成交额 ' + amtWan(r.amt),
    to: (r) => '换手 ' + num(r.to, 2) + '%',
  };

  function dayExtraSpan(r) {
    const st = C.sort.histDay;
    const t = st && DAY_EXTRA[st.id] ? DAY_EXTRA[st.id](r) : null;
    return t ? '<span>' + esc(t) + '</span>' : '';
  }

  function visibleEvents() {
    const q = S.q.trim().toLowerCase();
    return (S.d.events || []).filter((e) => {
      const s = stkOf(e.c);
      if (isRiskName(s.n)) return false;        // v1.19.0：ST / 退市股不显示（与「策略一」口径一致）
      if (S.scope === 'above' && s.above !== true) return false;
      if (S.scope === 'below' && s.above !== false) return false;
      if (!q) return true;
      return c6(e.c).indexOf(q) >= 0 ||
        String(s.n || '').toLowerCase().indexOf(q) >= 0 ||
        String(s.i || '').toLowerCase().indexOf(q) >= 0;
    });
  }

  function evRow(r, i) {
    const s = stkOf(r.c);
    const row = el('div', 'row tap');
    row.innerHTML =
      '<div class="idx">' + (i + 1) + '</div>' +
      '<div class="grow">' +
        '<div class="t1">' + esc(s.n || r.c) +
          ' <span class="tag">' + esc(s.i || '未知') + '</span></div>' +
        '<div class="t2">' +
          '<span class="code">' + esc(c6(r.c)) + '</span>' +
          '<span class="' + (r.dev > 0 ? 'up' : 'down') + '">偏离 ' + pct(r.dev, 2) + '</span>' +
          '<span>60 周线 ' + num(r.ma, 2) + '</span>' +
          dayExtraSpan(r) +
          (r.m === 'intraday' ? '<span>盘中触发</span>' : '') +
        '</div>' +
      '</div>' +
      '<div class="r">' +
        '<div class="p">' + num(r.px, 2) + '</div>' +
        '<div class="s ' + cls(r.chg) + '">' + pct(r.chg, 2) + '</div>' +
      '</div>';
    row.addEventListener('click', () => openChart(r.c, s.n || r.c));
    return row;
  }

  function stockRow(s, i) {
    const row = el('div', 'row tap');
    const ab = s.above === true ? '<span class="tag bo">仍站上</span>'
             : s.above === false ? '<span class="tag">已跌破</span>' : '';
    const dots = (s._evs || []).map((e) => '<b>' + e.d.slice(5) + '</b>').join('');
    row.innerHTML =
      '<div class="idx">' + (i + 1) + '</div>' +
      '<div class="grow">' +
        '<div class="t1">' + esc(s.n || s.c) +
          ' <span class="tag">' + esc(s.i || '未知') + '</span></div>' +
        '<div class="t2">' +
          '<span class="code">' + esc(c6(s.c)) + '</span>' +
          '<span>' + s.cnt + ' 次触发</span>' +
          '<span>最近 ' + s.last.slice(5) + '</span>' + ab +
        '</div>' +
        (dots ? '<div class="dots">' + dots + '</div>' : '') +
      '</div>' +
      '<div class="r">' +
        '<div class="p ' + (s.dev_now == null ? 'muted' : cls(s.dev_now)) + '">' +
          pct(s.dev_now, 2) + '</div>' +
        '<div class="s ' + (s.ret == null ? 'muted' : cls(s.ret)) + '">' +
          (s.ret == null ? '—' : pct(s.ret, 1)) + '</div>' +
      '</div>';
    row.addEventListener('click', () => openChart(s.c, s.n || s.c));
    return row;
  }

  function emptyBox(txt) {
    return '<div class="empty"><div class="big">◎</div>' + txt + '</div>';
  }

  function renderBody() {
    const rows = visibleEvents();
    const node = $('evList');
    node.innerHTML = '';

    if (!rows.length) {
      node.innerHTML = emptyBox('没有匹配的触发记录');
      $('listHint').textContent = '0 条';
      $('listNote').textContent = '';
      return;
    }

    if (S.view === 'day') {
      const st = C.sort.histDay;
      const groups = {};
      rows.forEach((r) => { (groups[r.d] = groups[r.d] || []).push(r); });
      Object.keys(groups).sort().reverse().forEach((d) => {
        const list = sortRows(groups[d], DAY_SORTS, st);
        const sep = el('div', 'daysep');
        sep.innerHTML = '<span>' + fmtDay(d) + '</span>' +
          '<span class="n">' + list.length + ' 只</span>';
        node.appendChild(sep);
        list.forEach((r, i) => node.appendChild(evRow(r, i)));
      });
      $('listTitle').textContent = '按日期';
      $('listHint').textContent = rows.length + ' 次 · ' +
        sortTip(curField(DAY_SORTS, st), st.dir);
    } else {
      const st = C.sort.histStock;
      const map = {};
      rows.forEach((r) => { (map[r.c] = map[r.c] || []).push(r); });
      const list = Object.keys(map).map((c) => {
        const s = Object.assign({}, stkOf(c));
        s.c = c;
        s._evs = map[c].slice().sort((a, b) => (a.d < b.d ? 1 : -1));
        return s;
      });
      sortRows(list, STOCK_SORTS, st).forEach((s, i) => node.appendChild(stockRow(s, i)));
      $('listTitle').textContent = '按股票';
      $('listHint').textContent = list.length + ' 只 · ' +
        sortTip(curField(STOCK_SORTS, st), st.dir);
    }

    /* v1.19.0：ST / 退市股不显示，如实告知隐藏了多少条（与「策略一」同一口径） */
    const stHidden = (S.d.events || [])
      .filter((e) => isRiskName(stkOf(e.c).n)).length;
    const stTip = stHidden
      ? '<br/><b>已隐藏 ' + stHidden + ' 条 ST / 退市风险股记录</b>' +
        '（名称里带 ST 或「退」的，不在这份名单里显示）。'
      : '';
    $('listNote').innerHTML = (S.view === 'day'
      ? '点任意一行，就能看这只股票的周线图、以及它是在哪一周被扫出来的。'
      : '右边两个数字：现在比「长期平均价」高或低多少 / 从第一次被扫出来到现在涨跌了多少。点一下看周线图。')
      + stTip;
  }

  function mountSortForView() {
    if (S.view === 'day') mountSort('histDay', 'sortBar', DAY_SORTS, renderBody);
    else mountSort('histStock', 'sortBar', STOCK_SORTS, renderBody);
  }

  /* ------------------------------------------------ 周K 弹层 */
  let kChart = null;

  async function openChart(code, name) {
    $('chartTitle').textContent = name + ' · 周 K';
    $('chartMask').classList.add('show');
    $('kChart').innerHTML = '<div class="loading"><span class="spinner"></span> 正在加载周线…</div>';
    const series = await fetchWeeklyOne(code).catch(() => null);
    if (!series || !series.length) {
      $('kChart').innerHTML = '<div class="empty">周线数据获取失败<br>请检查网络后重试</div>';
      $('chartStats').innerHTML = '';
      return;
    }
    drawChart(code, series);
    drawStats(code, series);
  }

  function drawChart(code, series) {
    const node = $('kChart');
    // 必须先销毁旧实例：画布已被 innerHTML 清空，复用旧实例会画到脱离 DOM 的画布上
    if (kChart && !kChart.isDisposed()) kChart.dispose();
    node.innerHTML = '';
    if (typeof window.echarts === 'undefined') {
      node.innerHTML = '<div class="empty">图表库未加载<br>请检查网络后刷新</div>';
      return;
    }
    kChart = window.echarts.init(node);

    const weeks = series.map((b) => b.week);
    const closes = series.map((b) => b.close);
    const m60 = ma(closes, 60);

    // 触发点标注：腾讯周刻度 = 该周最后交易日，事件日往后吸附到本周那根 K 线
    const seen = {};
    const pts = [];
    (S.d.events || []).filter((e) => e.c === code).forEach((e) => {
      let idx = -1;
      for (let i = 0; i < weeks.length; i++) {
        if (weeks[i] >= e.d) { idx = i; break; }
      }
      if (idx < 0 || seen[idx]) return;
      seen[idx] = 1;
      pts.push({
        name: '触', coord: [weeks[idx], closes[idx]],
        value: '触', symbol: 'pin', symbolSize: 30,
        itemStyle: { color: '#ff9500' },
        label: { color: '#fff', fontSize: 10, fontWeight: 700 },
      });
    });

    kChart.setOption(weeklyChartOption(weeks, closes, m60, {
      closeLabel: '周收盘', color: '#007aff',
      areaTop: 'rgba(0,122,255,.16)', areaBottom: 'rgba(0,122,255,0)',
      fmt: (v) => v.toFixed(2),
      markPoint: pts,
    }));
    kChart.resize();
  }

  function drawStats(code, series) {
    const last = series[series.length - 1];
    const closes = series.map((b) => b.close);
    const m60 = ma(closes, 60);
    const maNow = m60[m60.length - 1];
    const dev = maNow ? (last.close / maNow - 1) * 100 : null;
    const evs = (S.d.events || []).filter((e) => e.c === code);
    const lastEv = evs.slice().sort((a, b) => (a.d < b.d ? 1 : -1))[0];
    const st = stkOf(code);

    $('chartStats').innerHTML =
      metric('这周收盘价', num(last.close, 2), last.week) +
      metric('60 周平均价', num(maNow, 2), '偏离 ' + pct(dev, 2)) +
      metric('这一个月被扫出', evs.length + ' 次',
             lastEv ? '最近 ' + lastEv.d.slice(5) : '—');

    const parts = [];
    if (st.ret != null) parts.push('从第一次被扫出到现在 ' + pct(st.ret, 1));
    if (st.above === true) parts.push('现在仍在长期平均价上方');
    else if (st.above === false) parts.push('现在已经跌到长期平均价下方');
    parts.push('橙色标记为触发当周');
    $('chartNote').textContent = parts.join(' · ');
  }

  /* ------------------------------------------------ 交互绑定 */
  function bind() {
    document.querySelectorAll('#viewSeg button').forEach((b) => {
      b.addEventListener('click', () => {
        document.querySelectorAll('#viewSeg button').forEach((x) => x.classList.remove('on'));
        b.classList.add('on');
        S.view = b.dataset.view;
        mountSortForView();
        renderBody();      // 切视图要换一套排序字段，列表需随之重渲染
      });
    });

    document.querySelectorAll('[data-scope]').forEach((b) => {
      b.addEventListener('click', () => {
        document.querySelectorAll('[data-scope]').forEach((x) => x.classList.remove('on'));
        b.classList.add('on');
        S.scope = b.dataset.scope;
        renderBody();
      });
    });

    let timer = null;
    $('q').addEventListener('input', (e) => {
      clearTimeout(timer);
      const v = e.target.value;
      timer = setTimeout(() => { S.q = v; renderBody(); }, 180);
    });

    $('chartClose').addEventListener('click', () => $('chartMask').classList.remove('show'));
    $('chartMask').addEventListener('click', (e) => {
      if (e.target.id === 'chartMask') $('chartMask').classList.remove('show');
    });
  }

  window.addEventListener('resize', () => { if (kChart) kChart.resize(); });

  /* ------------------------------------------------ 启动 */
  async function boot() {
    const vb = $('verBadge');
    if (vb) vb.textContent = APP_VERSION;
    try {
      S.d = await loadJSON('history.json');
    } catch (e) {
      $('heroBox').innerHTML = '<div class="banner err">数据加载失败：' + esc(e.message) +
        '<br><br>请确认 dashboard/history.json 已生成并推送。</div>';
      $('evList').innerHTML = '';
      return;
    }
    // v1.19.1：远端数据源连不上、用的是站点自带快照 → 明确告示（数据可能不是最新）
    const sw = $('srcWarn');
    if (sw && C.remoteUnavailable()) {
      sw.innerHTML = '<div class="banner" style="margin-bottom:12px">当前显示的是' +
        '<b>发布时自带的快照数据</b>（截至 ' + esc(S.d.data_date || '—') +
        '）——实时数据源（GitHub）这次没连上，<b>刷新页面会再试一次</b>。</div>';
    }
    renderHero();
    renderCal();
    renderInds();
    mountSortForView();
    renderBody();      // mountSort 只画排序栏，首次列表要显式渲染
    bind();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
