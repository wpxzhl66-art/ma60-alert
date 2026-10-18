/* ============================================================
   60周线预警台 · 核心前端
   数据来源：GitHub 仓库 dashboard/ 目录（每日由 Actions 更新），
             远端不可达时回退到本地同目录文件。
   ============================================================ */
(function () {
  'use strict';

  /* 共享核心：常量、DOM 工具、排序组件、数据加载（见 core.js） */
  const C = window.WBcore;
  const { REPO, REMOTE, $, el, esc, num, pct, cls, yi, amtWan, timeAgo,
          readSort, curField, sortRows, sortTip, drawSortBar,
          mountSort, loadJSON, fetchWeeklyOne, fetchDailyOne, weeklyChartOption, ma, metric,
          fetchBarsOne, fetchMinuteOne, barsYearly, klineChartOption, minuteChartOption,
          isRiskName } = C;

  // 交付物版本号：每次改动递增，界面右上角可见（见 CHANGELOG.md）
  const APP_VERSION = 'v1.26.0';

  const S = {
    data: null,        // data.json
    static: null,      // static.json
    quotes: {},        // quotes.json：全市场最新价（账户估值兜底）
    quotesDate: '',
    codeIndex: null,   // 搜索用
    fut: null,         // fut.json：期货加权指数板块（切到该页才拉）
    futLoading: null,
    pat: null,         // pattern.json：右侧反转形态板块（切到该页才拉）
    patLoading: null,
    news: null,       // news.json：三家财经源的重要新闻（相似项已合并）
    sort: C.sort,      // 各列表的当前排序（由 core.js 维护）
  };

  /* 排序组件（readSort / sortRows / mountSort 等）已移入 core.js */

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

  /* 数据加载（loadJSON）与单股周线（fetchWeeklyOne）已移入 core.js */

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
    return '<div class="metric hero"><div class="k">下一次自动检查（交易日）</div>' +
      '<div class="v">' + esc(dayWord + ' ' + ns.hm) + '</div>' +
      '<div class="x">' + esc(ns.label) + ' · ' + esc(left) +
      ' · 到点自动跑，周末和节假日跳过</div></div>';
  }

  /** 首页「形态」入口卡：本机或云端扫过才有数据，缺失时整卡隐藏 */
  function fillPatCard() {
    const p = S.pat, box = $('patBox'), card = $('patCard');
    if (!box || !card) return;
    if (!p || !p.day) { card.style.display = 'none'; return; }
    const c = p.counts || {};
    const stale = S.data && S.data.today && p.day !== S.data.today;
    card.style.display = '';
    box.innerHTML =
      '<div class="t"><div>今天突然大涨的股票 · 今日启动 ' + (c.A || 0) + ' 只</div>' +
      '<div class="s">' + esc(p.day) + ' · 涨完跌下来 ' + (c.B || 0) + ' 只 · 刚创新高 ' +
        (c.C || 0) + ' 只' + (stale ? ' · 这不是今天扫的' : '') +
        ' · 追涨风险提示（不是买入推荐）</div></div>' +
      '<button class="btn sec sm" id="patGo">去看看</button>';
    const go = $('patGo');
    if (go) {
      go.addEventListener('click', () => {
        const b = document.querySelector('.tabbar button[data-tab="pattern"]');
        if (b) b.click();
      });
    }
  }

  /* ---------------- 扫描结果展示层筛除 ST / 退市股（v1.19.0） ----------------
     按用户要求：ST、*ST、退市整理期（名称含「退」）的股票不出现在扫描结果里。
     ⚠️ 只在看板这一层筛除，后端 store/state/latest_triggers.json 的原始数据不动，
     所以随时可以回到完整名单（把这里的过滤去掉即可）。 */
  function trigVisibleRows() {
    const all = (S.data && S.data.triggers && S.data.triggers.rows) || [];
    const rows = all.filter((r) => !isRiskName(r.name));
    return { rows: rows, hidden: all.length - rows.length };
  }

  function renderToday() {
    const d = S.data;
    const t = d.triggers || {};
    const tv = trigVisibleRows();
    const rows = tv.rows;
    const today = d.today;

    // v1.19.1：远端数据源连不上、用的是站点自带快照 → 明确告诉用户（数据可能不是最新）
    const srcWarn = $('srcWarn');
    if (srcWarn) {
      srcWarn.innerHTML = C.remoteUnavailable()
        ? '<div class="banner" style="margin-bottom:12px">当前显示的是<b>发布时自带的快照数据</b>（截至 ' +
          esc((d.health && d.health.last_update) || d.today || '—') +
          '）——实时数据源（GitHub）这次没连上，<b>刷新页面会再试一次</b>。' +
          '看名单、历史、形态分析都不受影响，只是可能少最近一两天的新增。</div>'
        : '';
    }

    // 近一月历史触发入口卡（数据来自 data.json 的 hist 摘要字段）
    const h = d.hist || {};
    const hc = $('histCard');
    if (hc) {
      if (h.events) {
        hc.style.display = '';
        const span = String(h.start || '').slice(5) + ' ~ ' + String(h.end || '').slice(5);
        const pctAbove = h.stocks ? Math.round(h.above_now / h.stocks * 100) : 0;
        $('histBox').innerHTML =
          '<div class="t"><div>最近一个月被扫出 ' + h.events + ' 次 · 涉及 ' + h.stocks + ' 只</div>' +
          '<div class="s">' + span + ' · 现在还有 ' + pctAbove + '% 站在长期平均价上方</div></div>' +
          '<a class="btn sec sm" href="./history.html">看明细</a>';
      } else hc.style.display = 'none';
    }

    $('subTitle').textContent = '帮你看住股票 · 数据 ' +
      (d.health && d.health.last_update ? d.health.last_update : '—');

    const pill = $('dayPill');
    const isToday = t.day === today;
    pill.className = 'pill ' + (isToday ? 'on' : 'off');
    pill.innerHTML = '<i class="dot"></i><span>' +
      (isToday ? '今天已查过' + (t.ts ? ' · ' + esc(t.ts.slice(11, 16)) : '')
               : '今天还没查') + '</span>';

    // 推送总开关关闭时给出可见提示，避免把「没收到通知」误当成系统故障
    const pp = $('pushPill');
    if (pp) pp.style.display = (d.push && d.push.enabled === false) ? '' : 'none';

    // v1.25.0 分享参数：?date=YYYY-MM-DD 打开的是某日分享链接时，
    // 若与看板当前数据日期不一致则提示（data.json 每日覆盖，不留历史）。
    // 注意：必须在 hero innerHTML 赋值之后插入，否则会被覆盖。

    // Hero —— 第一眼就回答「今天要不要紧张」，术语收进可折叠说明
    const modeLabel = t.mode_label || '扫描';
    const ts = t.ts || '';
    const hotN = rows.filter((r) => riskLevel(r)).length;
    const n = rows.length;
    $('todayHero').innerHTML =
      '<div class="status ' + (n ? 'warn' : 'ok') + '">' +
        '<div class="emoji">' + (n ? '⚠️' : '✅') + '</div>' +
        '<div class="body">' +
          '<div class="title">' + (n ? '今天有 ' + n + ' 只股票要留意' : '今天没有要提醒的股票') + '</div>' +
          '<div class="desc">' + (n
            ? '它们之前跌了很久，最近刚爬回到<b>长期平均价</b>附近。历史上这种股票，' +
              '接下来半年平均比大盘差 <b>4.2%</b>。'
            : '全部 ' + (t.scanned || 0) + ' 只股票都逐个查过了，没有一只出现这种情况，' +
              '不用做任何操作。') + '</div>' +
        '</div>' +
      '</div>' +
      '<details class="help"><summary>这是什么意思？我该做什么？</summary>' +
        '<div class="help-body">' +
          '<span class="q"><b>「长期平均价」是什么？</b>' +
            '把这只股票过去 60 周每周的收盘价加起来除以 60，就是它的长期平均价（也叫 60 周均线）。' +
            '价格在这条线上方，说明近期还算强；跌到下方，就说明走弱了。</span>' +
          '<span class="q"><b>为什么这些股票要留意？</b>' +
            '它们跌了很久，现在只是「爬回来喘口气」，并不代表已经走强。' +
            '过去 14 年里有 5.3 万次同样的情况，之后半年平均比大盘差 4.2%，16 个年份没有一个例外。</span>' +
          '<span class="q"><b>我该做什么？</b>' +
            '手里有这个股票 → 可以考虑减一点；手里没有 → <b>不要因为这个就买入</b>。' +
            '这是「风险提醒」，不是「买入推荐」。</span>' +
          '<span class="q"><b>下面那些数字怎么看？</b>' +
            '「偏离 1.2%」＝现在价格比长期平均价高 1.2%；' +
            '「量比 2.0」＝今天的成交额是最近 20 天平均的 2 倍；' +
            '「涨跌幅」＝今天比昨天涨跌了多少。</span>' +
        '</div>' +
      '</details>' +
      '<div class="metrics c2">' +
        nextScanMetric() +
        (n ? '<div class="metric"><div class="k">里面最需要小心的</div>' +
          '<div class="v sm">' + hotN + ' 只</div>' +
          '<div class="x">涨得猛、成交还大，历史上表现最差</div></div>' : '') +
        '<div class="metric"><div class="k">今天一共查了</div><div class="v sm">' +
          (t.scanned || 0) + ' 只</div><div class="x">全部 A 股，一只不落</div></div>' +
        '<div class="metric"><div class="k">最近一次结果</div><div class="v sm">' +
          esc(t.day || '—') + '</div><div class="x">' + esc(modeLabel) + ' · ' +
          esc(ts ? ts.slice(11, 16) : '') + ' · ' + timeAgo(t.ts) + '</div></div>' +
      '</div>' +
      '<div class="howto">' +
        '<div class="s"><i>1</i><span>先看<b>上面那句总结</b>——今天要不要紧张，一句话说完。</span></div>' +
        '<div class="s"><i>2</i><span>要紧张就往下翻，<b>点一下股票名字</b>就能看它的周线图。</span></div>' +
        '<div class="s"><i>3</i><span>手里有名单里的股票，去「我的」里<b>记一笔</b>，以后能复盘。</span></div>' +
      '</div>';

    // v1.25.0 分享参数：?date=YYYY-MM-DD 打开的是某日分享链接时，
    // 若与看板当前数据日期不一致则提示（data.json 每日覆盖，不留历史）。
    try {
      const qd = new URLSearchParams(location.search).get('date');
      if (qd && S.data && S.data.today && qd !== S.data.today) {
        $('todayHero').insertAdjacentHTML('afterbegin',
          '<div class="banner" style="background:var(--orange-bg,#FFF3E0);color:var(--orange);margin-bottom:10px">' +
          '这是一条 ' + esc(qd) + ' 的分享；看板当前展示最新数据（' + esc(S.data.today) + '）。</div>');
      }
    } catch (e) { /* 分享参数解析失败不影响主流程 */ }

    mountSort('trig', 'trigSort', TRIG_SORTS, renderTrigs);
    renderTrigs();
  }

  function renderNews() {
    const box = $('newsList'), hint = $('newsHint'), note = $('newsNote');
    if (!box || !hint || !note) return;
    const n = S.news;
    const rows = (n && n.items) || [];
    box.innerHTML = '';
    if (!rows.length) {
      hint.textContent = '暂无重要消息';
      box.appendChild(el('div', 'empty', '暂时没有采集到符合重要性条件的财经新闻。'));
    } else {
      hint.textContent = rows.length + ' 条 · ' + timeAgo(n.generated_at);
      rows.forEach((r) => {
        const sources = (r.sources || []).map((s) => s.name).join(' · ');
        const row = el('div', 'row clickable',
          '<div class="t"><div>' + esc(r.title) + '</div><div class="s">' +
          esc(r.time || '—') + ' · ' + esc(sources) + (r.summary ? ' · ' + esc(r.summary) : '') +
          '</div></div><div class="r">›</div>');
        row.addEventListener('click', () => { if (r.url) window.open(r.url, '_blank', 'noopener'); });
        box.appendChild(row);
      });
    }
    const failed = Object.entries((n && n.sources) || {}).filter(([, s]) => !s.ok).map(([name]) => name);
    note.textContent = failed.length ? failed.join('、') + ' 暂时不可用，下次自动更新会重试。' :
      '每次日常数据更新都会重新采集；点击新闻可查看原始来源。';
  }

  /** 今日触发明细列表（排序变化时单独重画，不动 Hero） */
  function renderTrigs() {
    const tv = trigVisibleRows();
    const rows = tv.rows;
    const hidden = tv.hidden;
    const list = $('trigList');
    list.innerHTML = '';
    if (!rows.length) {
      $('trigHint').textContent = '';
      list.appendChild(el('div', 'empty',
        '<div class="big">✅</div>今天没有股票出现这种情况。<br>' +
        '每天会自动检查 4 次：10:05 / 11:05 / 14:05 / 14:55，收盘后再复核一次。'));
      $('trigNote').textContent = '';
      return;
    }
    const st = S.sort.trig;
    const sorted = sortRows(rows, TRIG_SORTS, st);
    $('trigHint').textContent =
      '共 ' + rows.length + ' 只 · ' + sortTip(curField(TRIG_SORTS, st), st.dir) +
      (hidden ? ' · 已隐藏 ' + hidden + ' 只 ST' : '');
    sorted.forEach((r, i) => list.appendChild(trigRow(r, i)));
    $('trigNote').innerHTML =
      (hidden
        ? '<b>已按你的设置隐藏 ' + hidden + ' 只 ST / 退市风险股</b>' +
          '（名称里带 ST 或「退」的，不在这份名单里显示）。<br/>'
        : '') +
      '<b>这些数字怎么看</b><br/>' +
      '「偏离 1.2%」＝现价比 60 周平均线高 1.2%；' +
      '「量比 2.0」＝今天的成交额是最近 20 天平均的 2 倍；' +
      '「年内上穿 3 次」＝今年已经有 3 次出现过今天这种情况。<br/>' +
      '<b>为什么这是风险提示</b>：14 年全历史回测（5.3 万次触发）显示，触发后跑输全市场等权 ' +
      '20 日 −2.1% / 60 日 −4.2% / 120 日 −6.8%，16 个年份无一例外。' +
      '<b>这是掉队风险提示，不是买入信号</b> —— 持仓股触发宜考虑减仓，空仓者不宜据此买入。<br/>' +
      '按量能分档：<span class="lv hot">涨得猛</span> 之后掉队最明显（+60 日 −6.4%，胜率 34%）；' +
      '<span class="lv calm">跌得慢</span> 相对轻（+60 日 −2.8%，胜率 42%）。<br/>' +
      '<b>入选口径</b>：严格型 A 类上穿（上周收盘 &lt; 60 周线 且 现价 ≥ 60 周线）。' +
      '切换排序后，若该字段平时不显示，会临时补在编号行里。';
  }

  /** 按量能热度给触发分档。阈值取自 14 年全历史回测的分档超额中位（+60 日）：
   *  量比≥2.5 或 换手≥5% → −6.4%/−7.6%（掉队最明显）；量比<1.0 且 换手<2% → −2.8%/−3.0%。
   *  v1.11.0：标签文案改成白话（「涨得猛 / 跌得慢」），并配一句 why 解释；
   *  ⚠️ 类名 .lv.hot / .lv.calm 与 t2 里的字段名不动 —— 探针按它们定位。 */
  function riskLevel(r) {
    const ok = (v) => typeof v === 'number' && isFinite(v);
    if ((ok(r.amt_ratio) && r.amt_ratio >= 2.5) || (ok(r.turnover) && r.turnover >= 5)) {
      return { text: '涨得猛', cls: 'hot', color: 'var(--orange)',
               why: '涨得急、买的人还多 —— 历史上这种之后掉队最明显（半年跑输 6.4%）' };
    }
    if (ok(r.amt_ratio) && r.amt_ratio < 1.0 && ok(r.turnover) && r.turnover < 2) {
      return { text: '跌得慢', cls: 'calm', color: 'var(--blue)',
               why: '价格跌得温和、成交也小 —— 历史上下跌相对轻（半年跑输 2.8%）' };
    }
    return null;
  }

  function trigRow(r, i) {
    const row = el('div', 'row tap');
    const devCls = r.dev_pct > 0 ? 'up' : 'down';
    const lv = riskLevel(r);
    const lvTag = lv ? ' <span class="lv ' + lv.cls + '">' + lv.text + '</span>' : '';
    row.innerHTML =
      '<div class="idx">' + (i + 1) + '</div>' +
      '<div class="grow">' +
        '<div class="t1">' + esc(r.name) + lvTag + ' <span class="tag">' + esc(r.industry) + '</span></div>' +
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
    // 把整行数据带给弹层（详情区展示与复核用）
    row.addEventListener('click', () => openKline(r.code, r.name, r));
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
    return st === '突破' ? '<span class="tag bo">刚涨上去</span>'
                         : '<span class="tag near">快到了</span>';
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
        '<div class="big">✅</div>今天没有期货品种靠近长期平均价。'));
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
        '剩下的上市时间太短、还算不出 60 周平均价，每周都会自动重试，够了就自动加进来。'
      : '全部 <b>' + total + '</b> 个品种都在检查范围内（上期所 20 · 大商所 23 · ' +
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

    const nearPct = ((d.near_pct || 0.03) * 100).toFixed(0);
    hero.innerHTML =
      '<div class="status ' + (rows.length ? 'warn' : 'ok') + '">' +
        '<div class="emoji">' + (rows.length ? '⚠️' : '✅') + '</div>' +
        '<div class="body">' +
          '<div class="title">' + (rows.length
            ? '今天有 ' + rows.length + ' 个期货品种要留意'
            : '今天没有期货品种要提醒') + '</div>' +
          '<div class="desc">' + (rows.length
            ? '和股票一样，它们也是价格回到了<b>长期平均价</b>附近。其中 <b>' +
              breakouts.length + ' 个刚刚涨上去</b>，另外 ' + nears.length +
              ' 个还在附近晃（还没涨上去）。'
            : '全部 ' + (hp.covered || all.length) + ' 个品种都逐个查过了，没有一个靠近长期平均价。') +
          '</div>' +
        '</div>' +
      '</div>' +
      '<details class="help"><summary>「靠近均线」为什么值得看？</summary>' +
        '<div class="help-body">' +
          '<span class="q"><b>长期平均价</b>＝把过去 60 周每周的价格加起来除以 60。' +
            '价格在这条线附近时，往往是行情要选方向的时候。</span>' +
          '<span class="q"><b>「刚涨上去」和「还在附近」的区别：</b>' +
            '刚涨上去＝上周还在这条线下面，现在已经站上去了；' +
            '还在附近＝没站上去，但离得很近（差不到 ±' + nearPct + '%）。</span>' +
          '<span class="q"><b>为什么用「加权价」而不是主连合约？</b>' +
            '加权价＝该品种所有在交易的合约，按持仓量大小加权平均。' +
            '换月的时候不会像主连合约那样突然跳一下，算出来的均线更干净。</span>' +
        '</div>' +
      '</details>' +
      '<div class="metrics c2">' +
        '<div class="metric hero"><div class="k">今天要留意的品种</div>' +
          '<div class="v">' + rows.length +
            ' <span style="font-size:15px;font-weight:600">个</span></div>' +
          '<div class="x">' + esc(d.day || '—') +
            ' · 已覆盖 ' + (hp.covered || all.length) + ' / ' + (hp.total || all.length) +
            ' 个品种' + (d.ts ? ' · ' + esc(d.ts.slice(5, 16).replace('T', ' ')) : '') + '</div>' +
        '</div>' +
        '<div class="metric"><div class="k">刚刚涨上去的</div>' +
          '<div class="v sm">' + breakouts.length + ' 个</div>' +
          '<div class="x">昨天还在线下，今天站上去了</div></div>' +
        '<div class="metric"><div class="k">离得很近的（±' + nearPct + '% 以内）</div>' +
          '<div class="v sm">' + nears.length + ' 个</div>' +
          '<div class="x">还没站上去，但已经在旁边了</div></div>' +
        '<div class="metric"><div class="k">价格怎么算的</div>' +
          '<div class="v sm">持仓量加权</div>' +
          '<div class="x">所有合约按持仓量加权平均，换月不跳空</div></div>' +
      '</div>';

    mountSort('fut', 'futSort', FUT_SORTS, renderFutList);
    mountSort('futall', 'futAllSort', FUT_SORTS, renderFutAll);
    renderFutList();
    renderFutAll();

    $('futNote').innerHTML =
      '<b>「刚涨上去」和「快到了」的区别</b>：' +
      '刚涨上去＝上一交易日还在均线下面、最新价格已经站上去了（可能真突破）；' +
      '快到了＝还没站上去，但离均线不到 ±' + nearPct + '%。<br/>' +
      '<b>每个数字怎么看</b>：「加权价」＝该品种所有在交易合约按持仓量加权平均出来的价格；' +
      '「60 周线」＝它过去 60 周的长期平均价；' +
      '「偏离 1.2%」＝加权价比长期平均价高 1.2%；' +
      '「年内上穿 N 次」＝今年已经有 N 次出现过这种情况。<br/>' +
      '<b>为什么用加权价</b>：加权＝按<b>持仓量</b>把全部在市合约平均起来' +
      '（Σ(收盘×持仓量) / Σ(持仓量)），与文华/东财的「XX加权」同口径，' +
      '不会像主力连续合约那样在换月时价格跳一下、把均线带歪。';
  }

  async function openFutKline(code, name) {
    kContext = { code: code, name: name, kind: 'fut' };
    $('chartTitle').textContent = name + '加权 · 周线图';
    $('chartMask').classList.add('show');
    $('chartBuy').style.display = 'none';
    $('chartSell').style.display = 'none';
    $('chartRange').style.display = 'none';   // 期货只有加权周线，不提供日线范围切换（v1.17.0）
    kRange = 'week'; kBars = {}; kMinute = null;
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
      metric('60 周平均价', futPrice(r.ma60), '偏离 ' + pct(r.dev_pct * 100, 2)) +
      metric('当前状态', r.status, r.sector + ' · 均线' + r.slope);
    $('chartNote').innerHTML =
      '<b>怎么看这张图</b>：<b style="color:#5856d6">紫线</b>是该品种的加权价，' +
      '<b style="color:#ff9500">橙线</b>是「60 周平均价」。' +
      '加权价＝把该品种全部在交易的合约，按持仓量大小加权平均' +
      '（Σ(合约收盘 × 持仓量) / Σ(持仓量)）。<br/>' +
      '这样做的好处：换月的时候价格不会突然跳一下，长期平均价不会被带歪。';
  }

  /* 右侧反转形态：C 类（刚突破前高）数量最多，单独给一套排序 */
  const PAT_SORTS = [
    { id: 'chg', label: '涨跌幅', key: (r) => r.chg, dir: 'desc' },
    { id: 'vol', label: '量比', key: (r) => r.vol, dir: 'desc' },
    { id: 'brk', label: '突破距今日', key: (r) => r.brk_age, dir: 'asc' },
    { id: 'dd', label: '前期跌幅', key: (r) => r.dd, dir: 'asc' },
    { id: 'mv', label: '市值', key: (r) => r.mv, dir: 'desc' },
    { id: 'ind', label: '行业', key: (r) => r.industry, dir: 'asc' },
  ];

  /* 排序字段若不在行内固定展示，就在副标题里补一项 */
  const PAT_EXTRA = {
    dd: (r) => '前期跌幅 ' + num(r.dd, 1) + '%',
    mv: (r) => '市值 ' + yi(r.mv),
  };

  /* ------------------------------------------------ 视图：形态
     右侧反转五阶段（用户 2026-10-15 提供的选股思路）。
     数据由扫描写入 dashboard/pattern.json：本机（data/daily 全历史）与
     云端（store/daily 400 日分片）都能产出，判定共用 src/pattern/core.py，
     结果逐只一致。两者都离线时文件日期会变旧，页面据此提示「非当日」。
     ⚠️ 定位（2010-01 起全历史回测）：阶段④「启动日」介入后 60 日跑输全市场等权
        8.95%（胜率 31.3%，全部 15 个年份无一为正）；阶段③ −5.33%、阶段② −6.92%。
        加入 30~200 亿市值筛选后结论**没有改善**（旧口径 −8.41%），这个筛除条件
        只负责排除有风险与不可操作的标的，不会把信号变成正的。
        → 这是**追涨风险提示**，不是买入信号。改文案前先读
        store/pattern_backtest.json（入库版，云端也读得到）。 */
  function loadPattern() {
    if (S.pat) return Promise.resolve(S.pat);
    if (S.patLoading) return S.patLoading;
    S.patLoading = loadJSON('pattern.json').then((d) => { S.pat = d; return d; })
      .catch((e) => { S.patLoading = null; throw e; });
    return S.patLoading;
  }

  function patRow(r, i) {
    const row = el('div', 'row tap');
    const zt = r.zt ? ' <span class="tag bo">涨停</span>' : '';
    const t2 = ['<span class="code">' + esc(String(r.code).replace('.', '')) + '</span>',
                '<span>量比 ' + num(r.vol, 2) + '</span>'];
    if (r.retrace != null) t2.push('<span>距高点 ' + num(r.retrace, 1) + '%</span>');
    if (r.span != null) t2.push('<span>回踩 ' + num(r.span, 0) + ' 日</span>');
    if (r.stage === 'C' && r.brk_age != null) {
      t2.push('<span>突破 ' + num(r.brk_age, 0) + ' 日前</span>');
    }
    const ex = PAT_EXTRA[S.sort.pat.id];
    if (ex) t2.push('<span>' + esc(ex(r)) + '</span>');
    row.innerHTML =
      '<div class="idx">' + (i + 1) + '</div>' +
      '<div class="grow">' +
        '<div class="t1">' + esc(r.name) + zt +
          ' <span class="tag">' + esc(r.industry) + '</span></div>' +
        '<div class="t2">' + t2.join('') + '</div>' +
      '</div>' +
      '<div class="r">' +
        '<div class="p">' + num(r.price, 2) + '</div>' +
        '<div class="s ' + cls(r.chg) + '">' + pct(r.chg, 2) + '</div>' +
      '</div>';
    row.addEventListener('click', () => openKline(r.code, r.name, r));
    return row;
  }

  function renderPatList(key) {
    const d = S.pat || {};
    const rows = (d.rows || []).filter((r) => r.stage === key);
    const list = $('pat' + key + 'List');
    const hint = $('pat' + key + 'Hint');
    list.innerHTML = '';
    if (!rows.length) {
      hint.textContent = '';
      list.appendChild(el('div', 'empty', '<div class="big">✅</div>今天这类没有股票入选。'));
      return;
    }
    const useSort = key === 'C';
    hint.textContent = '共 ' + rows.length + ' 只' +
      (useSort ? ' · ' + sortTip(curField(PAT_SORTS, S.sort.pat), S.sort.pat.dir) : '');
    const sorted = useSort ? sortRows(rows, PAT_SORTS, S.sort.pat) : rows;
    sorted.forEach((r, i) => list.appendChild(patRow(r, i)));
  }

  async function renderPattern() {
    const hero = $('patHero');
    hero.innerHTML = '<div class="loading"><span class="spinner"></span> 正在加载形态扫描结果…</div>';
    let d;
    try {
      d = await loadPattern();
    } catch (e) {
      hero.innerHTML = '<div class="banner err">形态数据加载失败：' + esc(e.message) +
        '<br><br>该文件由扫描生成（本机或云端均可产出），请检查是否已生成并提交。</div>';
      ['A', 'B', 'C'].forEach((k) => { $('pat' + k + 'List').innerHTML = ''; });
      return;
    }
    const c = d.counts || {};
    const bt = d.backtest || {};
    const e4 = bt.e4 || {}, e3 = bt.e3 || {}, e2 = bt.e2 || {};
    const today = S.data && S.data.today;
    const stale = d.day && today && d.day !== today;

    const lossTxt = Math.abs(e4.ex60_med || -8.95).toFixed(1);
    const winTxt = (e4.ex60_win || 31.3).toFixed(1);
    hero.innerHTML =
      '<div class="status warn">' +
        '<div class="emoji">⚠️</div>' +
        '<div class="body">' +
          '<div class="title">这些不是「买入推荐」</div>' +
          '<div class="desc">下面这些股票<b>今天涨得很猛</b>。但按这套形态在老远就下手，' +
            '历史上启动后 60 天平均<b>跑输 ' + lossTxt + '%</b>，胜率只有 ' + winTxt + '%。' +
            '这里是<b>追涨风险提示</b>。</div>' +
        '</div>' +
      '</div>' +
      '<details class="help"><summary>为什么会跑输？这一页该怎么用？</summary>' +
        '<div class="help-body">' +
          '<span class="q"><b>「跑输大盘」是什么意思？</b>' +
            '同样的钱，买了这只股票，60 天后比「随便买一篮子股票」少赚（或多亏）这么多个百分点。</span>' +
          '<span class="q"><b>为什么会这样？</b>' +
            '一只股票突然大涨，多半是短线资金推动的。追进去的人多、后面没人接力，就容易回落。' +
            '这不是猜的，是把过去 16 年、4,000 多次同样情况全算了一遍的结果。</span>' +
          '<span class="q"><b>那这一页有什么用？</b>' +
            '两大用处：① <b>提醒自己别追高</b>；② 手里刚大涨的股票，看看要不要落袋。' +
            '它<b>不是用来挑股票的</b>。</span>' +
        '</div>' +
      '</details>' +
      '<div class="metrics c2">' +
        '<div class="metric hero"><div class="k">今天突然大涨的（最要小心）</div>' +
          '<div class="v sm">' + (c.A || 0) +
            ' <span style="font-size:15px;font-weight:600">只</span></div>' +
          '<div class="x">' + esc(d.day || '—') + ' · 扫描 ' + (d.scanned || 0) + ' 只' +
            (d.ts ? ' · ' + esc(d.ts.slice(11, 16)) : '') + '</div></div>' +
        '<div class="metric"><div class="k">涨完跌下来、跌不动的</div>' +
          '<div class="v sm">' + (c.B || 0) + ' 只</div>' +
          '<div class="x">比上面那类好一些，但仍然是负超额</div></div>' +
        '<div class="metric"><div class="k">刚冲过前一次高点的</div>' +
          '<div class="v sm">' + (c.C || 0) + ' 只</div>' +
          '<div class="x">数量最多，同样不建议追</div></div>' +
        '<div class="metric"><div class="k">这套做法的历史成绩（' +
          (e4.n || 0).toLocaleString() + ' 次同样情况）</div>' +
          '<div class="v sm">追涨风险</div>' +
          '<div class="x">启动后 60 日跑输 ' + lossTxt + '% · 胜率 ' + winTxt + '%</div></div>' +
      '</div>' +
      (stale ? '<div class="banner">这一页的数据停在 ' + esc(d.day) +
        '（今天还没扫到），下面的名单不是今天的结果。</div>' : '');

    mountSort('pat', 'patCSort', PAT_SORTS, () => renderPatList('C'));
    ['A', 'B', 'C'].forEach(renderPatList);

    const pct1 = (v) => (v == null ? '—' : (Math.abs(v)).toFixed(1) + '%');
    const f = d.filter || {};
    const rej = d.rejected || [], dro = d.dropped || [];
    const rejLine = (r) => '<li>' + esc(r.name) + ' <span class="code">' + esc(r.code) +
      '</span> <span class="muted">' + esc(r.why || (r.drop_reason || []).join('；')) + '</span></li>';
    $('patNote').innerHTML =
      '<b>简单说，这套形态是四步</b>：' +
      '① 先跌掉一大半 → ② 涨回来、创了近期新高 → ③ 往回踩一脚但没破位 → ④ 突然放量大涨。' +
      '这一页抓的就是第 ④ 步 —— 也就是「看起来最像买点」的那一刻。<br/>' +
      '<b>形态口径</b>（阈值集中在 <span class="code">src/pattern/core.py</span>，' +
      '回测与实盘共用同一份判定逻辑）：' +
      '① 前期深跌＝更早 250 日高点 → 近 60 日低点回撤 ≥25%；' +
      '② 突破＝创 60 日新高（突破本轮反弹前高，而非年线高点）；' +
      '③ 洗盘＝回踩 4%~20%、不少于 3 日、近 3 日不再破前一日 0.5%；' +
      '④ 启动＝涨停，或涨幅 ≥5% 且量比 ≥1.8，且收盘创回踩段新高。' +
      '<br><b>筛除条件</b>（按此过滤，被排除的标的见下方明细）：' +
      '① 排除 ST/*ST 与退市整理股（名称以 ST 开头，或含「退」）；' +
      '② 流通市值 ' + (f.mv ? f.mv[0] + '~' + f.mv[1] : '30~200') + ' 亿；' +
      '③ 未来 ' + (f.lift_days || 60) + ' 天内解禁市值 ≥ 当前流通市值 ' +
        Math.round((f.lift_ratio || 0.15) * 100) + '% 的剔除；' +
      '④ 近 ' + (f.news_days || 90) + ' 天公告命中重大利空' +
        '（退市/立案/处罚/财务非标/债务违约/股权冻结/停产/业绩恶化）的剔除。' +
      (rej.length + dro.length
        ? '<details class="rej"><summary>本次已排除 ' + (rej.length + dro.length) +
          ' 只（市值·名称 ' + rej.length + '，解禁·利空 ' + dro.length + '）</summary>' +
          '<ul>' + [...dro, ...rej].map(rejLine).join('') + '</ul></details>'
        : '') +
      '<br><b>数据链路</b>：本机（全历史日线）与云端（最近 400 交易日分片仓）' +
      '都会在收盘后扫描并提交仓库，判定逻辑同一份、结果一致；' +
      '本机离线时由云端兜底。' +
      (d.lag ? ' <b>数据提示</b>：' + esc(d.lag) : '') +
      '<br><b>⚠️ 信号性质（全历史回测 2010-01 起）</b>：' +
      '阶段④启动 ' + (e4.n || 0).toLocaleString() + ' 次 → 60 日超额中位 ' +
        pct1(e4.ex60_med) + '、胜率 ' + (e4.ex60_win || 0).toFixed(1) + '%；' +
      '阶段③ ' + (e3.n || 0).toLocaleString() + ' 次 → ' + pct1(e3.ex60_med) + '；' +
      '阶段② ' + (e2.n || 0).toLocaleString() + ' 次 → ' + pct1(e2.ex60_med) + '。' +
      '三个阶段<b>全部为负超额，15 个年份无一为正</b>；' +
      '加上市值与风险筛除后结论<b>没有改善</b>——筛除只负责排除有风险、' +
      '不可操作的标的，不会把信号变成正的。' +
      '<b>本板块是追涨风险提示，不是买入信号</b>，按此形态在启动日追进历史上平均跑输市场。';

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
      coverCell = '<div class="metric hero"><div class="k">覆盖了多少股票</div>' +
        '<div class="v sm">' + have + ' / ' + live + ' 只 · ' + pct.toFixed(1) + '%</div>' +
        '<div class="x">' + shortN + ' 只上市还不到 61 周，暂时画不出长期平均价，' +
        '满 61 周会自动加进来' +
        (h.tx_codes ? ' · 其中 ' + h.tx_codes + ' 只用备用源补齐' : '') +
        '</div></div>';
    }

    // 抓取覆盖率：如实反映上次增量更新抓到多少，漏抓可见
    let fetchCell = '';
    if (us.todo || us.fetched) {
      const rate = us.todo ? Math.round(us.fetched / us.todo * 100) : 100;
      const tx = us.fallback || 0;
      fetchCell = '<div class="metric"><div class="k">上次更新抓到了多少</div>' +
        '<div class="v sm">' + us.fetched + ' / ' + us.todo + ' 只 · ' + rate + '%</div>' +
        '<div class="x">' + esc(us.ts || '') +
        (tx ? ' · 其中 ' + tx + ' 只用备用源补齐' : '') +
        (us.unresolved ? ' · ' + us.unresolved + ' 只两个源都没取到（沿用上次的数据，覆盖面未缩水）'
                       : '') +
        (us.timed_out ? ' · 到时间上限提前收工' : '') + '</div></div>';
    }

    $('healthGrid').innerHTML =
      metric('正在盯着的股票', h.stocks || 0, '只（能算出 60 周平均价的）') +
      metric('攒了多少周的价格', ((h.weekly_rows || 0) / 10000).toFixed(1), '万条记录') +
      metric('数据最新到哪天', h.last_update || '—', '已做除权复权处理') +
      metric('画过的图', h.chart_count || 0, '张（最近 7 个交易日）') +
      metric('数据占用空间', h.store_mb || 0, 'MB') +
      metric('认识多少只股票', h.names_covered || 0, '只（有名称）') +
      coverCell + fetchCell;

    const gapTxt = us.gap_filled
      ? '本轮新纳入 <b>' + us.gap_filled + '</b> 只（由备用源补齐）。' : '';
    $('healthNote').innerHTML =
      '<b>每次检查都是把全部股票挨个算一遍</b>，不抽样、不跳着看。<br/>' +
      '「正在盯着的股票」' + have + ' 只＝历史数据够长、算得出 60 周平均价的股票。' +
      '和全市场 ' + live + ' 只相差的 <b>' + shortN + ' 只</b>，是上市还不到 61 周的新股——' +
      '物理上还画不出 60 周平均价。系统每周自动重试，满 61 周即刻纳入。<br/>' +
      gapTxt +
      '数据主要以 baostock 为主源，个别股票取不到时自动换腾讯行情兜底（当前 ' +
      (h.tx_codes || 0) + ' 只用备用源）。每个交易日 16:07 自动更新数据；' +
      '10:05 / 11:05 / 14:05 / 14:55 盘中检查，15:15 收盘后复核。<br/>' +
      '更新原则：<b>只补齐、不覆盖</b>。取不到的股票沿用上次数据，<b>绝不会让覆盖面缩水</b>。';

    // 额度
    const p = d.push || {};
    const used = p.count || 0, lim = p.limit || 5;
    const rate = Math.min(100, Math.round(used / lim * 100));
    const paused = p.enabled === false;
    $('quotaBox').innerHTML =
      (paused ? '<div class="banner" style="margin:0 0 12px">' +
        '<b>通知目前是关着的</b>（按你的要求关的）。看板、历史记录、数据更新都照常，' +
        '只是不再发手机通知。</div>' : '') +
      '<div style="display:flex;align-items:baseline;gap:8px">' +
        '<span style="font-size:26px;font-weight:680;letter-spacing:-.6px">' + used + '</span>' +
        '<span class="muted" style="font-size:13px">/ ' + lim + ' 条</span>' +
        '<span class="muted" style="margin-left:auto;font-size:12px">' + esc(p.date || '—') + '</span>' +
      '</div>' +
      '<div class="bar-track"><div class="bar-fill" style="width:' + rate +
        '%;background:' + (rate >= 100 ? 'var(--up)' : 'var(--blue)') + '"></div></div>' +
      '<div class="note" style="padding:10px 0 0">' +
        '通知走 Server酱 的免费通道，每天最多 ' + lim + ' 条，超过就发不出去了。<br/>' +
        '盘中只有「出现新情况」时才发，收盘后那一次必发；有护栏，不会超量。</div>';

    // 趋势
    const tr = d.trend || [];
    $('trendHint').textContent = tr.length ? (tr[0].day.slice(5) + ' ~ ' + tr[tr.length - 1].day.slice(5)) : '';
    drawTrend(tr);

    // 运行流水（v1.18.0：默认只亮最近 3 条，其余折叠，可展开/收起）
    const list = $('runList');
    list.innerHTML = '';
    const runs = (d.runs || []).slice(0, 12);
    if (!runs.length) {
      list.appendChild(el('div', 'empty', '<div class="big">◌</div>暂无运行记录'));
      return;
    }
    const rows = runs.map((r) => {
      const st = r.status === 'completed' ? (r.conclusion === 'success' ? 'ok' : 'fail') : 'run';
      const badge = st === 'ok' ? '<span class="tag down">跑成功了</span>'
        : st === 'fail' ? '<span class="tag up">' + esc(r.conclusion || '没跑成') + '</span>'
        : '<span class="tag orange">正在跑</span>';
      const row = el('div', 'row tap');
      row.innerHTML =
        '<div class="grow"><div class="t1">' + esc(r.label) + ' ' + badge + '</div>' +
        '<div class="t2"><span>' + esc(r.at) + '</span><span>' + esc(r.event || '') + '</span>' +
        '<span class="code">' + esc(r.sha || '') + '</span></div></div>' +
        '<div class="r"><div class="p">' + (r.duration_s >= 60 ?
          (r.duration_s / 60).toFixed(1) + ' 分' : r.duration_s + ' 秒') + '</div>' +
        '<div class="s muted">' + (r.status === 'completed' ? '已结束' : '还在跑') + '</div></div>';
      if (r.url) row.addEventListener('click', () => window.open(r.url, '_blank'));
      list.appendChild(row);
      return row;
    });

    if (rows.length > 4) {                     // 4 条以内没必要折叠
      const VIS = 3;
      const togg = el('div', 'row more-row tap');
      const apply = (open) => {
        rows.forEach((row, i) => { row.style.display = (open || i < VIS) ? '' : 'none'; });
        togg.innerHTML = '<span class="muted" style="margin:0 auto">' +
          (open ? '▲ 收起，只看最近 ' + VIS + ' 条'
                : '▼ 还有更早的 ' + (rows.length - VIS) + ' 条任务') + '</span>';
      };
      let open = localStorage.getItem('wb.runsOpen') === '1';
      togg.addEventListener('click', () => {
        open = !open;
        localStorage.setItem('wb.runsOpen', open ? '1' : '0');
        apply(open);
      });
      list.appendChild(togg);
      apply(open);
    }
  }

  let trendChart = null;
  function drawTrend(tr) {
    const node = $('trendChart');
    if (!tr.length) { node.innerHTML = '<div class="empty">还没有攒够记录</div>'; return; }
    trendChart = trendChart || initChart(node);
    if (!trendChart) return;
    trendChart.setOption({
      grid: { left: 34, right: 12, top: 16, bottom: 26 },
      tooltip: {
        trigger: 'axis',
        formatter: (ps) => {
          const i = ps[0].dataIndex, t = tr[i];
          return t.day + '<br/>当天要留意 <b>' + t.peak + '</b> 只<br/>最近一次（' +
            (t.mode === 'intraday' ? '盘中' : t.mode === 'confirm' ? '收盘后' : t.mode) + '）有 ' +
            t.n + ' 只';
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
          label: { formatter: '每天最多 5 条', color: '#ff9500', fontSize: 10, position: 'insideEndTop' },
          data: [{ yAxis: 5 }],
        },
      }],
    });
    trendChart.resize();
  }

  /* ------------------------------------------------ 周K 图 */
  let kChart = null;
  let kContext = null;
  let kRange = 'week';        // 图表周期：minute/day/week/month/year（v1.20.0 同花顺式五周期）
  let kBars = {};             // 周期→OHLCV 缓存（v1.20.0：替代 kWeekly/kDaily，当天复用）
  let kMinute = null;         // 当日分时缓存 {pre, pts}

  /* 周线图公共配置（weeklyChartOption）与 ma() 已移入 core.js */

  async function openKline(code, name, ctx) {
    kContext = { code: code, name: name, kind: 'stock', ctx: ctx || null };
    kRange = 'week';                        // 每次打开默认「周K」（系统 60 周线口径）
    kBars = {}; kMinute = null;
    $('chartTitle').textContent = name + ' · 周K';
    $('chartMask').classList.add('show');
    $('chartBuy').style.display = '';
    $('chartSell').style.display = '';
    $('chartRange').style.display = '';
    renderRangeChips();                     // v1.20.0：分时/日K/周K/月K/年K
    $('kChart').innerHTML = '<div class="loading"><span class="spinner"></span> 正在加载周K…</div>';
    const bars = await fetchBarsOne(code, 'week').catch(() => null);
    if (!bars || !bars.length) {
      $('kChart').innerHTML =
        '<div class="empty">周K数据获取失败<br>请检查网络后重试</div>';
      return;
    }
    kBars.week = bars;
    renderKline(code, bars, 'week');
    renderChartStats(code, bars);
  }

  /* ---------------- 周期切换（v1.20.0 同花顺式） ----------------
     蜡烛图 + MA5/10/20/60 + 成交量副图，可缩放；分时用腾讯分时接口；
     年K 由月K 按年聚合（零额外请求）。同周期当天缓存、切回不再请求。 */
  function renderRangeChips() {
    const box = $('chartRange');
    if (!box) return;
    box.innerHTML = '';
    [['minute', '分时'], ['day', '日K'], ['week', '周K'], ['month', '月K'], ['year', '年K']]
      .forEach((kv) => {
        const b = el('button', 'chip' + (kRange === kv[0] ? ' on' : ''), kv[1]);
        b.type = 'button';
        b.dataset.range = kv[0];
        b.addEventListener('click', () => switchRange(kv[0]));
        box.appendChild(b);
      });
  }

  const RANGE_TITLE = { minute: ' · 分时', day: ' · 日K', week: ' · 周K', month: ' · 月K', year: ' · 年K' };

  async function switchRange(id) {
    if (!kContext || id === kRange) return;
    kRange = id;
    renderRangeChips();
    $('chartTitle').textContent = kContext.name + RANGE_TITLE[id];
    if (id === 'minute') {
      $('kChart').innerHTML = '<div class="loading"><span class="spinner"></span> 正在加载分时…</div>';
      if (!kMinute) kMinute = await fetchMinuteOne(kContext.code).catch(() => null);
      if (!kMinute || !kMinute.pts.length) {
        $('kChart').innerHTML = '<div class="empty">分时数据获取失败<br>（收盘日都有，请检查网络后重试）</div>';
        return;
      }
      renderMinute(kMinute);
      writeChartNote('minute');
      return;
    }
    const src = (id === 'year') ? 'month' : id;
    $('kChart').innerHTML = '<div class="loading"><span class="spinner"></span> 正在加载' +
      RANGE_TITLE[id].slice(2) + '…</div>';
    if (!kBars[src]) kBars[src] = await fetchBarsOne(kContext.code, src).catch(() => null);
    const srcBars = kBars[src];
    if (!srcBars || !srcBars.length) {
      $('kChart').innerHTML = '<div class="empty">' + RANGE_TITLE[id].slice(2) +
        '数据获取失败<br>请检查网络后重试</div>';
      return;
    }
    const bars = (id === 'year') ? barsYearly(srcBars) : srcBars;
    renderKline(kContext.code, bars, id);
    writeChartNote(id);
  }

  function renderKline(code, bars, mode) {
    const node = $('kChart');
    // 必须先销毁旧实例再重建：上一张图的画布已被 innerHTML 清空，
    // 复用旧实例会画到已脱离 DOM 的画布上 → 第二次打开看不到图
    if (kChart && !kChart.isDisposed()) kChart.dispose();
    node.innerHTML = '';
    kChart = initChart(node);
    if (!kChart) return;
    // 均线先在完整收盘序列上算，再截取窗口 —— 否则窗口开头会没有均线
    const closes = bars.map((b) => b.close);
    const mas = {};
    [[5, 'ma5'], [10, 'ma10'], [20, 'ma20'], [60, 'ma60']].forEach((kv) => {
      mas[kv[1]] = ma(closes, kv[0]);
    });
    // B/S 买卖点按周记录，只在周K 视图标注（其他周期不硬凑）
    const marks = mode === 'week'
      ? ((window.WB_ACCOUNT && window.WB_ACCOUNT.marksFor(code, bars.map((b) => b.d)))
         || { buys: [], sells: [] })
      : { buys: [], sells: [] };
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
    kChart.setOption(klineChartOption(bars, {
      zoom: mode === 'day' ? 120 : 0,       // 日K 默认显示最近 120 根，可缩放
      mas: mas,
      volUnit: ' 手',
      markPoint: buyPts.concat(sellPts),
    }));
    node.dataset.range = mode;              // 供测试判断当前图属于哪个周期
    kChart.resize();
  }

  function renderMinute(m) {
    const node = $('kChart');
    if (kChart && !kChart.isDisposed()) kChart.dispose();
    node.innerHTML = '';
    kChart = initChart(node);
    if (!kChart) return;
    kChart.setOption(minuteChartOption(m, {}));
    node.dataset.range = 'minute';
    kChart.resize();
  }

  function renderChartStats(code, bars) {
    const last = bars[bars.length - 1];
    const m60 = ma(bars.map((b) => b.close), 60);
    const maNow = m60[m60.length - 1];
    const dev = maNow ? (last.close / maNow - 1) * 100 : null;
    const st = (S.static || {});
    const ind = (st.industry || {})[code] || '—';
    const q = S.quotes[code];
    $('chartStats').innerHTML =
      metric('这周收盘价', num(last.close, 2), last.d) +
      metric('60 周平均价', num(maNow, 2), dev == null ? '' : '偏离 ' + pct(dev, 2)) +
      metric('所属行业', ind, q ? '现在的价格 ' + num(q[0], 2) : '');
    writeChartNote(kRange);     // v1.20.0：按当前周期给说明
  }

  /* 图下说明：各周期一套文案（v1.20.0） */
  function writeChartNote(period) {
    const common = '图上价格用的是<b>前复权</b>口径（把历史分红送股折算掉），只改变价格刻度，' +
      '和系统判断用的口径算出来结论完全一致。<br/>' +
      '图上的 <b>B / S</b> 是你自己在模拟账户里记的买入 / 卖出点（按周记录，在周K 显示）。';
    if (period === 'minute') {
      $('chartNote').innerHTML =
        '<b>怎么看这张图</b>：<b style="color:#007aff">蓝线</b>是<b>今天每分钟</b>的价格，' +
        '<b style="color:#ff9500">黄线</b>是当日均价（至今的平均成交价），灰色虚线是昨收。<br/>' +
        '下面的柱子是每分钟成交量，红涨绿跌。<br/>' +
        '判断系统信号请看<b>周K</b>——那里有「60 周平均价」长期口径。';
      return;
    }
    if (period === 'week') {
      $('chartNote').innerHTML =
        '<b>怎么看这张图</b>：红柱涨、绿柱跌（每根 = 一周）。' +
        '<b style="color:#ff9500">橙线</b>是 <b>MA60＝「60 周平均价」</b>（长期平均价，系统判定口径）——' +
        '周K 从下面爬回来上穿橙线，就是「今天」页里提醒你的那种情况；' +
        '跌破橙线说明还在走弱。黄/紫/蓝细线是 MA5/10/20 短中期平均价。<br/>' +
        '下面的柱子是每周成交量（手）。可以<b>在图上滚轮/双指缩放</b>，拖动看更早的历史。' + common;
      return;
    }
    const n = { day: '天', month: '月', year: '年' }[period] || '天';
    $('chartNote').innerHTML =
      '<b>怎么看这张图</b>：红柱涨、绿柱跌（每根 = 一' + n + '）。' +
      '黄/紫/蓝/橙细线是 MA5 / MA10 / MA20 / MA60——' +
      (period === 'month' || period === 'year'
        ? '这里的均线口径跟着周期走（月K 的 MA60 是 60 个月平均价）。'
        : '日线图的 MA60 是「60 日平均价」，<b>不是</b>系统判定用的 60 周口径。') + '<br/>' +
      '下面的柱子是每' + n + '成交量（手）。可以<b>在图上滚轮/双指缩放</b>，拖动看更早的历史。' +
      (period === 'day'
        ? '日K 默认显示最近 120 个交易日。<br/>'
        : '<br/>') + common;
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
        // v1.14.0：期货并入策略一（today）页，随本页显示/切换时渲染
        if (b.dataset.tab === 'today') renderFutures();
        if (b.dataset.tab === 'pattern') renderPattern();   // 懒加载：pattern.json 由本机/云端扫描生成
        // v1.26.0：新闻独立成板块，切到该页时重绘（数据在 boot 时已加载，无需再请求）
        if (b.dataset.tab === 'news') renderNews();
        if (b.dataset.tab === 'account' && window.WB_ACCOUNT) window.WB_ACCOUNT.onShow();
      });
    });
  }

  /** 支持 ?tab=today|pattern|news|system|account 直达（也用于分享与自动截图） */
  function applyTabFromUrl() {
    let t = new URL(location.href).searchParams.get('tab');
    if (!t) return;
    if (t === 'futures') t = 'today';   // v1.14.0：期货并入策略一，旧链接兼容
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
      renderFutures();   // v1.14.0：期货并入策略一页（默认页），启动即拉 fut.json
      window.WB_ACCOUNT.boot();
      applyTabFromUrl();
      // 兜底价格源（可选，失败不影响主流程）
      loadJSON('quotes.json').then((q) => {
        S.quotes = (q && q.items) || {};
        S.quotesDate = (q && q.date) || '';
        if (window.WB_ACCOUNT) window.WB_ACCOUNT.onQuotes();
      }).catch(() => {});
      // 形态入口卡（数据由本机生成，属可选板块，失败只是不显示该卡）
      loadJSON('pattern.json').then((p) => { S.pat = p; fillPatCard(); }).catch(() => {});
      loadJSON('news.json').then((n) => { S.news = n; renderNews(); }).catch(() => {
        S.news = { items: [], sources: {} }; renderNews();
      });
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
