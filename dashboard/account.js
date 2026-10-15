/* ============================================================
   60周线预警台 · 模拟资金账户
   - 登录：手机号验证码（WorkBuddy 云服务；WorkBuddy/微信登录已移除）
   - 账本：云端数据库，手机与电脑共用，行级权限按用户隔离
   - 费用：佣金万 2.5（最低 5 元）+ 卖出印花税千 1 + 过户费万 0.1
   - 规则：A 股 100 股整数倍、T+1（当日买入次日才可卖）
   ============================================================ */
(function () {
  'use strict';

  const WB = window.WB;
  const $ = WB.$, el = WB.el, esc = WB.esc, num = WB.num, pct = WB.pct, cls = WB.cls, metric = WB.metric;

  /* activate 返回的 publicConfig —— 前端可安全持有，服务端按域名校验来源 */
  const PUBLIC_CONFIG = {
    endpoint: 'https://ma60-alert-dashboard.app.workbuddy.host',
    oauthRelayBaseUrl: 'https://www.workbuddy.cn/v2/as/genie-baas/oauth',
    publishableKey: 'wbpk_te5IMgYD76lWGIPXEJGUR3_SxZb4bb2kkipE11JDG41Va6yEUbh2b7Z',
  };

  const FEE = {
    commission_rate: 0.00025,   // 佣金 万 2.5
    commission_min: 5,          // 单笔最低 5 元
    stamp_rate: 0.001,          // 印花税 千 1（仅卖出）
    transfer_rate: 0.00001,     // 过户费 万 0.1（买卖双边）
  };
  const DEFAULT_CAPITAL = 1000000;

  const A = {
    cloud: null, session: null, account: null, trades: [],
    live: {}, pos: {}, cash: 0, ready: false, booted: false,
    side: 'buy', code: null, name: null, equity: null,
  };

  /* ------------------------------------------------ 费用与持仓 */
  function calcFees(side, price, shares) {
    const amount = price * shares;
    const commission = Math.max(amount * FEE.commission_rate, FEE.commission_min);
    const stamp = side === 'sell' ? amount * FEE.stamp_rate : 0;
    const transfer = amount * FEE.transfer_rate;
    const total = commission + stamp + transfer;
    return {
      amount: +amount.toFixed(2),
      commission: +commission.toFixed(2),
      stamp: +stamp.toFixed(2),
      transfer: +transfer.toFixed(2),
      total: +total.toFixed(2),
    };
  }

  /** 移动加权平均成本法推导持仓 */
  function computePositions() {
    const map = {};
    const sorted = A.trades.slice().sort((a, b) =>
      (a.trade_date < b.trade_date ? -1 : a.trade_date > b.trade_date ? 1 : a.id - b.id));
    for (const t of sorted) {
      const code = t.code;
      const p = map[code] || (map[code] = {
        code: code, name: t.stock_name || code, shares: 0, cost: 0,
        realized: 0, fees: 0, buys: 0, sells: 0, wins: 0, lastTag: '',
        todayBuy: 0, lots: [],
      });
      const price = +t.price, sh = +t.shares, fee = +t.fee_total;
      p.fees += fee;
      if (t.side === 'buy') {
        const ns = p.shares + sh;
        p.cost = (p.cost * p.shares + price * sh + fee) / ns;
        p.shares = ns;
        p.buys += 1;
        p.lots.push({ date: t.trade_date, shares: sh });
        if (t.tag) p.lastTag = t.tag;
        if (t.trade_date === todayStr()) p.todayBuy += sh;
      } else {
        const sellSh = Math.min(sh, p.shares);
        const realized = (price - p.cost) * sellSh - fee;
        p.realized += realized;
        p.sells += 1;
        if (realized > 0) p.wins += 1;
        p.shares = Math.max(0, p.shares - sellSh);
        // 消耗最早的可卖份额
        let left = sellSh;
        while (left > 0 && p.lots.length) {
          const lot = p.lots[0];
          const take = Math.min(lot.shares, left);
          lot.shares -= take; left -= take;
          if (lot.shares <= 0) p.lots.shift();
        }
        const tagKey = t.tag || p.lastTag || '未标记';
        p.tagStat = p.tagStat || {};
        const ts = p.tagStat[tagKey] || (p.tagStat[tagKey] = { n: 0, wins: 0, realized: 0 });
        ts.n += 1; ts.realized += realized; if (realized > 0) ts.wins += 1;
        if (t.trade_date === todayStr()) p.todayBuy = Math.max(0, p.todayBuy - sellSh);
      }
      if (p.shares <= 1e-6) p.shares = 0;
    }
    return map;
  }

  function todayStr() {
    const d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }

  function computeCash() {
    let cash = +(A.account ? A.account.initial_capital : DEFAULT_CAPITAL);
    for (const t of A.trades) {
      const amount = +t.amount, fee = +t.fee_total;
      cash += (t.side === 'buy' ? -(amount + fee) : (amount - fee));
    }
    return cash;
  }

  /* ------------------------------------------------ 价格 */
  /** 优先实时行情，回退到数据仓里的最新收盘价 */
  function priceOf(code) {
    const q = A.live[code];
    if (q && q.price > 0) return q.price;
    const s = (WB.S.quotes || {})[code];
    if (s) return +s[0];
    return null;
  }
  function priceSource(code) {
    const q = A.live[code];
    if (q && q.price > 0) return '实时';
    const s = (WB.S.quotes || {})[code];
    if (s) {
      const d = (WB.S.quotesDate || '').slice(5);
      return '数据仓 ' + (d ? d.slice(3) + '/' + d.slice(0, 2) : '最新');
    }
    return '无行情';
  }
  function stockName(code) {
    const n = (WB.S.static && WB.S.static.names) || {};
    if (n[code]) return n[code];
    const p = A.pos[code];
    return (p && p.name) || code;
  }

  async function refreshLive() {
    const codes = Object.keys(A.pos).filter((c) => A.pos[c].shares > 0);
    if (!codes.length) return;
    const chunks = [];
    for (let i = 0; i < codes.length; i += 60) chunks.push(codes.slice(i, i + 60));
    for (const c of chunks) {
      try {
        const q = await WB.fetchQuotes(c);
        Object.assign(A.live, q);
      } catch (e) { /* 忽略，回退收盘价 */ }
    }
  }

  /* ------------------------------------------------ 登录界面 */
  function authBox() { return $('authBox'); }

  function showLoading(msg) {
    authBox().style.display = '';
    $('acctBox').style.display = 'none';
    authBox().innerHTML = '<div class="loading"><span class="spinner"></span> ' + esc(msg || '加载中…') + '</div>';
  }

  function showLoginError(msg) {
    const box = $('loginMsg');
    if (box) box.innerHTML = '<div class="banner err">' + esc(msg) + '</div>';
    else authBox().insertAdjacentHTML('afterbegin', '<div class="banner err">' + esc(msg) + '</div>');
  }

  function loginView() {
    authBox().style.display = '';
    $('acctBox').style.display = 'none';
    authBox().innerHTML =
      '<div class="section-label">模拟资金账户</div>' +
      '<div class="card"><div class="card-head"><h2>登录后开始记账</h2>' +
      '<div class="hint">手机 / 电脑同一本账</div></div>' +
      '<div class="card-body">' +
        '<div class="note" style="padding:0 0 14px">' +
          '用来自动记录你在 60 周线信号上的每一笔买卖，实时算出持仓成本、浮动盈亏、' +
          '收益率与最大回撤，并把买卖点标在周 K 图上。' +
        '</div>' +
        '<div id="phoneForm">' +
          '<div class="field"><label>手机号</label>' +
            '<input id="fPhone" type="tel" inputmode="numeric" maxlength="11" placeholder="11 位手机号"></div>' +
          '<div class="field"><label>验证码</label>' +
            '<div style="display:flex;gap:8px">' +
              '<input id="fCode" type="text" inputmode="numeric" maxlength="6" placeholder="6 位验证码" style="flex:1">' +
              '<button class="btn sec" id="btnSendCode" style="white-space:nowrap">获取验证码</button>' +
            '</div>' +
            '<div id="phoneErr" style="font-size:12.5px;color:var(--up);margin-top:5px"></div>' +
          '</div>' +
          '<button class="btn block" id="btnDoPhone">登录 / 注册</button>' +
        '</div>' +
        '<div id="loginMsg"></div>' +
      '</div></div>';

    $('btnSendCode').addEventListener('click', sendPhoneCode);
    $('btnDoPhone').addEventListener('click', submitPhoneLogin);
    $('fPhone').addEventListener('input', () => { $('phoneErr').textContent = ''; });
  }

  /* ------------------------------------------------ 登录动作 */
  // 仅保留手机号验证码登录（WorkBuddy / 微信登录已按需求移除，v1.10.2）。
  // restoreAuth 里的 OAuth 回调处理保留：万一还有旧的回调链接进来，能优雅提示。

  let pendingOtp = null;

  async function sendPhoneCode() {
    const phone = $('fPhone').value.trim();
    if (!/^1\d{10}$/.test(phone)) { $('phoneErr').textContent = '请输入正确的 11 位手机号'; return; }
    $('phoneErr').textContent = '';
    const btn = $('btnSendCode');
    btn.disabled = true;
    const sent = await A.cloud.auth.sendOtp({ phone: phone });
    btn.disabled = false;
    if (sent.error) return showLoginError(sent.error.message);
    pendingOtp = { phone: phone, verificationId: sent.data.verificationId, isExistingUser: sent.data.isExistingUser };
    let n = 60;
    btn.textContent = n + 's';
    const timer = setInterval(() => {
      n -= 1; btn.textContent = n + 's';
      if (n <= 0) { clearInterval(timer); btn.textContent = '获取验证码'; btn.disabled = false; }
    }, 1000);
    btn.disabled = true;
  }

  async function submitPhoneLogin() {
    const phone = $('fPhone').value.trim();
    const code = $('fCode').value.trim();
    const p = pendingOtp;
    if (!p || p.phone !== phone) return showLoginError('请先获取该手机号的验证码');
    if (!code) return showLoginError('请输入验证码');
    const btn = $('btnDoPhone');
    btn.disabled = true;
    const done = await A.cloud.auth.verifyOtp({
      phone: p.phone, verificationId: p.verificationId,
      isExistingUser: p.isExistingUser, token: code,
    });
    btn.disabled = false;
    if (done.error) return showLoginError(done.error.message);
    pendingOtp = null;
    await onSignedIn(done.data.user);
  }

  /* ------------------------------------------------ 会话与数据 */
  async function restoreAuth() {
    const url = new URL(location.href);
    if (!url.searchParams.has('code') && !url.searchParams.has('error')) {
      return continueNormalStartup();
    }
    const provider = url.searchParams.get('provider');
    if (provider !== 'workbuddy' && provider !== 'wechat') {
      return showLoginError('授权回调缺少有效的登录方式，请重新登录。');
    }
    const handle = provider === 'workbuddy'
      ? A.cloud.auth.handleWorkBuddyCallback : A.cloud.auth.handleWechatWebCallback;
    if (typeof handle !== 'function') {
      return showLoginError('暂时无法使用此方式登录，请改用手机号验证码。');
    }
    const { data: sess, error } = await handle.call(A.cloud.auth);
    if (error) return showLoginError(error.message);
    if (sess) {
      for (const k of ['code', 'state', 'provider']) url.searchParams.delete(k);
      history.replaceState(null, '', url.pathname + url.search + url.hash);
      return onSignedIn(sess.user);
    }
    return continueNormalStartup();
  }

  async function continueNormalStartup() {
    showLoading('正在检查登录状态…');
    const { data: sess } = await A.cloud.auth.getSession();
    if (sess) return onSignedIn(sess.user);
    loginView();
  }

  async function onSignedIn(user) {
    A.session = { user: user || {} };
    showLoading('正在读取账户…');
    try {
      await ensureAccount();
      await loadTrades();
    } catch (e) {
      authBox().innerHTML = '<div class="banner err">账户读取失败：' + esc(e.message || e) + '</div>';
      return;
    }
    A.ready = true;
    await refreshLive();
    renderAll();
  }

  async function ensureAccount() {
    const { data, error } = await A.cloud.database.from('ma60_accounts')
      .select('*').order('id', { ascending: true }).limit(1);
    if (error) throw error;
    if (data && data.length) { A.account = data[0]; return; }
    const nick = (A.session.user && A.session.user.name) ? A.session.user.name : null;
    const { data: created, error: e2 } = await A.cloud.database.from('ma60_accounts')
      .insert({ name: '默认账户', initial_capital: DEFAULT_CAPITAL, owner_name: nick })
      .select();
    if (e2) throw e2;
    if (!created || !created.length) throw new Error('账户创建失败（权限被拒绝）');
    A.account = created[0];
  }

  async function loadTrades() {
    const { data, error } = await A.cloud.database.from('ma60_trades')
      .select('*').eq('account_id', A.account.id)
      .order('trade_date', { ascending: true }).order('id', { ascending: true }).limit(3000);
    if (error) throw error;
    A.trades = data || [];
    A.pos = computePositions();
    A.cash = computeCash();
  }

  /* ------------------------------------------------ 渲染 */
  function renderAll() {
    authBox().style.display = 'none';
    $('acctBox').style.display = '';
    renderAssets();
    renderPositions();
    renderPerf();
    renderTags();
    renderTrades();
    renderEquity();
  }

  function totalMarketValue() {
    let mv = 0;
    for (const c in A.pos) {
      const p = A.pos[c];
      if (p.shares <= 0) continue;
      const px = priceOf(c);
      if (px != null) mv += px * p.shares;
    }
    return mv;
  }
  function totalCost() {
    let c0 = 0;
    for (const c in A.pos) {
      const p = A.pos[c];
      if (p.shares > 0) c0 += p.cost * p.shares;
    }
    return c0;
  }
  function realizedTotal() {
    let r = 0;
    for (const c in A.pos) r += A.pos[c].realized;
    return r;
  }

  function renderAssets() {
    const mv = totalMarketValue();
    const cost = totalCost();
    const init = +(A.account.initial_capital || DEFAULT_CAPITAL);
    const total = A.cash + mv;
    const profit = total - init;
    const ret = init ? profit / init * 100 : 0;
    const floating = mv - cost;

    $('assetGrid').innerHTML =
      '<div class="metric hero"><div class="k">总资产</div>' +
        '<div class="v">' + num(total, 2) + '</div>' +
        '<div class="x">初始资金 ' + num(init, 0) + ' 元</div></div>' +
      '<div class="metric"><div class="k">累计收益</div>' +
        '<div class="v sm ' + cls(profit) + '">' + (profit >= 0 ? '+' : '') + num(profit, 2) + '</div>' +
        '<div class="x ' + cls(ret) + '">' + pct(ret, 2) + '</div></div>' +
      '<div class="metric"><div class="k">可用现金</div><div class="v sm">' + num(A.cash, 2) + '</div>' +
        '<div class="x">仓位 ' + (total > 0 ? (mv / total * 100).toFixed(1) : '0.0') + '%</div></div>' +
      '<div class="metric"><div class="k">持仓市值</div><div class="v sm">' + num(mv, 2) + '</div>' +
        '<div class="x ' + cls(floating) + '">浮动 ' + (floating >= 0 ? '+' : '') + num(floating, 2) + '</div></div>' +
      '<div class="metric"><div class="k">已实现盈亏</div>' +
        '<div class="v sm ' + cls(realizedTotal()) + '">' + (realizedTotal() >= 0 ? '+' : '') + num(realizedTotal(), 2) + '</div>' +
        '<div class="x">' + A.trades.length + ' 笔成交</div></div>';

    const user = A.session.user || {};
    $('acctMeta').innerHTML =
      '<div class="row" style="padding:0 0 6px"><div class="grow"><div class="t1">' +
        esc(user.name || '已登录用户') + '</div>' +
        '<div class="t2"><span>' + esc(A.account.name || '默认账户') + '</span>' +
        '<span>行情来源：' + (Object.keys(A.live).length ? '实时（腾讯）' : '数据仓最新收盘') + '</span></div></div></div>';
  }

  function renderPositions() {
    const list = $('posList');
    list.innerHTML = '';
    const rows = Object.values(A.pos).filter((p) => p.shares > 0)
      .sort((a, b) => (priceOf(b.code) || 0) * b.shares - (priceOf(a.code) || 0) * a.shares);
    $('posHint').textContent = rows.length ? rows.length + ' 只' : '';
    if (!rows.length) {
      list.appendChild(el('div', 'empty', '<div class="big">◌</div>还没有持仓<br>点「记一笔交易」或从「今日」里直接买入'));
      return;
    }
    rows.forEach((p) => {
      const px = priceOf(p.code);
      const mv = px == null ? null : px * p.shares;
      const fl = px == null ? null : (px - p.cost) * p.shares;
      const flp = px == null ? null : (px / p.cost - 1) * 100;
      const avail = Math.max(0, p.shares - p.todayBuy);
      const row = el('div', 'row tap');
      row.innerHTML =
        '<div class="grow"><div class="t1">' + esc(stockName(p.code)) +
          '</div><div class="t2"><span class="code">' + esc(p.code.replace('.', '')) + '</span>' +
          '<span>' + num(p.shares, 0) + ' 股</span>' +
          '<span>成本 ' + num(p.cost, 3) + '</span>' +
          (p.todayBuy > 0 ? '<span class="tag orange">T+1 冻结 ' + num(p.todayBuy, 0) + '</span>' : '') +
        '</div></div>' +
        '<div class="r"><div class="p">' + (mv == null ? '—' : num(mv, 0)) + '</div>' +
          '<div class="s ' + cls(fl) + '">' + (fl == null ? '无行情' :
            (fl >= 0 ? '+' : '') + num(fl, 0) + '（' + pct(flp, 2) + '）') + '</div></div>';
      row.addEventListener('click', () => openKlineFor(p.code));
      list.appendChild(row);
    });
  }

  function openKlineFor(code) {
    WB.openKline(code, stockName(code));
  }

  function renderTrades() {
    const list = $('tradeList');
    list.innerHTML = '';
    $('tradeHint').textContent = A.trades.length ? A.trades.length + ' 笔' : '';
    if (!A.trades.length) {
      list.appendChild(el('div', 'empty', '暂无成交记录'));
      return;
    }
    A.trades.slice().reverse().forEach((t) => {
      const isBuy = t.side === 'buy';
      const row = el('div', 'row');
      row.innerHTML =
        '<span class="tag ' + (isBuy ? 'up' : 'down') + '">' + (isBuy ? '买入' : '卖出') + '</span>' +
        '<div class="grow"><div class="t1">' + esc(t.stock_name || stockName(t.code)) +
          (t.tag ? ' <span class="tag blue">' + esc(t.tag) + '</span>' : '') + '</div>' +
        '<div class="t2"><span>' + esc(t.trade_date) + '</span>' +
          '<span>' + num(t.price, 3) + ' × ' + num(t.shares, 0) + '</span>' +
          '<span>费用 ' + num(t.fee_total, 2) + '</span>' +
          (t.note ? '<span>' + esc(t.note) + '</span>' : '') + '</div></div>' +
        '<div class="r"><div class="p">' + num(t.amount, 0) + '</div>' +
          '<div class="s muted">成交额</div></div>' +
        '<button class="btn ghost sm" data-del="' + t.id + '">撤</button>';
      row.querySelector('[data-del]').addEventListener('click', (e) => {
        e.stopPropagation(); deleteTrade(t);
      });
      list.appendChild(row);
    });
  }

  function renderPerf() {
    const init = +(A.account.initial_capital || DEFAULT_CAPITAL);
    const total = A.cash + totalMarketValue();
    let sells = 0, wins = 0;
    for (const c in A.pos) { sells += A.pos[c].sells; wins += A.pos[c].wins; }
    const winRate = sells ? wins / sells * 100 : null;
    const eq = A.equity && A.equity.series;
    const mdd = eq && eq.length ? maxDrawdown(eq.map((d) => d.v)) : null;
    const benchRet = A.equity && A.equity.benchRet != null ? A.equity.benchRet : null;
    const myRet = init ? (total - init) / init * 100 : 0;

    $('perfGrid').innerHTML =
      metric('胜率', winRate == null ? '—' : winRate.toFixed(1) + '%',
        sells ? wins + ' 胜 / ' + sells + ' 次平仓' : '还没有平仓记录', '') +
      metric('最大回撤', mdd == null ? '—' : mdd.toFixed(2) + '%', '按周结算') +
      metric('账户收益', pct(myRet, 2), '相对初始资金', cls(myRet)) +
      metric('沪深300', benchRet == null ? '—' : pct(benchRet, 2), '同起点对比', cls(benchRet || 0)) +
      metric('超额收益', benchRet == null ? '—' : pct(myRet - benchRet, 2), '账户 − 基准', cls(myRet - (benchRet || 0))) +
      metric('交易笔数', A.trades.length, '买入 ' + A.trades.filter((t) => t.side === 'buy').length +
        ' / 卖出 ' + A.trades.filter((t) => t.side === 'sell').length);
  }

  function maxDrawdown(arr) {
    let peak = -Infinity, mdd = 0;
    for (const v of arr) {
      if (v > peak) peak = v;
      if (peak > 0) mdd = Math.min(mdd, v / peak - 1);
    }
    return mdd * 100;
  }

  function renderTags() {
    const list = $('tagList');
    list.innerHTML = '';
    const agg = {};
    for (const c in A.pos) {
      const ts = A.pos[c].tagStat;
      if (!ts) continue;
      for (const k in ts) {
        const a = agg[k] || (agg[k] = { n: 0, wins: 0, realized: 0, codes: new Set() });
        a.n += ts[k].n; a.wins += ts[k].wins; a.realized += ts[k].realized; a.codes.add(c);
      }
    }
    const keys = Object.keys(agg);
    if (!keys.length) {
      list.appendChild(el('div', 'empty', '还没有带标签的平仓记录<br>买入时打个标签，就能看出哪种打法真正有效'));
      return;
    }
    keys.sort((a, b) => agg[b].realized - agg[a].realized).forEach((k) => {
      const a = agg[k];
      const wr = a.n ? a.wins / a.n * 100 : 0;
      const row = el('div', 'row');
      row.innerHTML =
        '<div class="grow"><div class="t1">' + esc(k) + '</div>' +
        '<div class="t2"><span>' + a.codes.size + ' 只标的</span><span>' + a.n + ' 次平仓</span>' +
        '<span>胜率 ' + wr.toFixed(0) + '%</span></div></div>' +
        '<div class="r"><div class="p ' + cls(a.realized) + '">' +
          (a.realized >= 0 ? '+' : '') + num(a.realized, 0) + '</div>' +
          '<div class="s muted">已实现</div></div>';
      list.appendChild(row);
    });
  }

  /* ------------------------------------------------ 资产曲线 */
  async function renderEquity() {
    const node = $('equityChart');
    if (!A.trades.length) {
      node.innerHTML = '<div class="empty">有成交后这里会画出资产曲线</div>';
      $('equityHint').textContent = '';
      A.equity = null;
      return;
    }
    node.innerHTML = '<div class="loading"><span class="spinner"></span></div>';
    const init = +(A.account.initial_capital || DEFAULT_CAPITAL);
    const startDate = A.trades.map((t) => t.trade_date).sort()[0];
    const codes = Object.keys(A.pos);

    // 按需拉周线（逐只，几 KB）：基准 + 持仓个股。
    // 持仓估值必须用「不复权」真实价（前复权会压低历史价格 → 市值算错），
    // 因此这里传 fq=''；基准归一化后与复权口径无关。
    const got = {};
    await Promise.all(['sh.000300'].concat(codes).map(async (c) => {
      try { got[c] = await WB.fetchWeeklyOne(c, ''); } catch (e) { got[c] = []; }
    }));

    const axis = (got['sh.000300'] || []).filter((d) => d.week >= startDate);
    if (!axis.length) { node.innerHTML = '<div class="empty">区间内无基准数据</div>'; return; }

    const cache = {};
    codes.forEach((c) => { cache[c] = got[c] || []; });
    const closeAt = (c, w) => {
      const arr = cache[c];
      if (!arr || !arr.length) return null;
      let lo = 0, hi = arr.length - 1, ans = null;
      while (lo <= hi) {
        const m = (lo + hi) >> 1;
        if (arr[m].week <= w) { ans = arr[m].close; lo = m + 1; } else hi = m - 1;
      }
      return ans;
    };
    const sharesAt = (c, w) => {
      let sh = 0;
      for (const t of A.trades) {
        if (t.code !== c || t.trade_date > w) continue;
        sh += (t.side === 'buy' ? +t.shares : -+t.shares);
      }
      return Math.max(0, sh);
    };
    const cashAt = (w) => {
      let cash = init;
      for (const t of A.trades) {
        if (t.trade_date > w) continue;
        cash += (t.side === 'buy' ? -(+t.amount + +t.fee_total) : (+t.amount - +t.fee_total));
      }
      return cash;
    };

    const series = axis.map((d) => ({ week: d.week, v: cashAt(d.week) + codes.reduce((s, c) => {
      const sh = sharesAt(c, d.week); const px = closeAt(c, d.week);
      return s + (px == null ? 0 : px * sh);
    }, 0) }));
    if (series.length) series[series.length - 1].v = A.cash + totalMarketValue();

    const b0 = axis[0].close;
    const benchSeries = axis.map((d) => ({ week: d.week, v: init * d.close / b0 }));
    const myRet = (series[series.length - 1].v / init - 1) * 100;
    const bRet = (axis[axis.length - 1].close / b0 - 1) * 100;
    A.equity = { series: series, benchRet: bRet };

    node.innerHTML = '';
    if (A.equityChart) { A.equityChart.dispose(); }
    A.equityChart = (typeof window.echarts === 'undefined') ? null
      : window.echarts.init(node);
    if (!A.equityChart) {
      node.innerHTML = '<div class="empty">图表库未加载<br>请检查网络后刷新</div>';
      return;
    }
    A.equityChart.setOption({
      animation: false,
      grid: { left: 52, right: 14, top: 18, bottom: 28 },
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(28,28,30,.92)', borderWidth: 0,
        textStyle: { color: '#fff', fontSize: 12 },
        valueFormatter: (v) => num(v, 0),
      },
      xAxis: {
        type: 'category', data: series.map((d) => d.week.slice(2, 7)),
        axisLine: { lineStyle: { color: '#8e8e93', width: .5 } }, axisTick: { show: false },
        axisLabel: { color: '#8e8e93', fontSize: 10 },
      },
      yAxis: {
        type: 'value', scale: true,
        splitLine: { lineStyle: { color: 'rgba(142,142,147,.18)' } },
        axisLabel: { color: '#8e8e93', fontSize: 10, formatter: (v) => (v / 10000).toFixed(0) + '万' },
      },
      series: [
        { name: '账户总资产', type: 'line', data: series.map((d) => d.v), symbol: 'none',
          lineStyle: { width: 2, color: '#007aff' },
          areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [{ offset: 0, color: 'rgba(0,122,255,.18)' }, { offset: 1, color: 'rgba(0,122,255,0)' }] } } },
        { name: '沪深300', type: 'line', data: benchSeries.map((d) => d.v), symbol: 'none',
          lineStyle: { width: 1.4, color: '#8e8e93', type: 'dashed' } },
      ],
    });
    A.equityChart.resize();
    $('equityHint').textContent = '账户 ' + pct(myRet, 2) + ' / 基准 ' + pct(bRet, 2);
    renderPerf();
  }

  /* ------------------------------------------------ 交易弹层 */
  function buildStockIndex() {
    if (WB.S.codeIndex) return WB.S.codeIndex;
    const st = WB.S.static || {};
    const names = st.names || {};
    const arr = Object.keys(names).map((c) => ({ code: c, name: names[c], py: '' }));
    WB.S.codeIndex = arr;
    return arr;
  }

  function searchStocks(q) {
    q = (q || '').trim();
    const idx = buildStockIndex();
    if (!q) return idx.slice(0, 8);
    const up = q.toUpperCase();
    const out = [];
    for (const it of idx) {
      const bare = it.code.replace('.', '');
      if (bare.startsWith(up) || it.name.indexOf(q) >= 0 || it.code.replace('.', '').indexOf(q) >= 0) {
        out.push(it);
        if (out.length >= 8) break;
      }
    }
    return out;
  }

  function openTrade(side, code, name) {
    if (!A.ready) { switchToAccount(); return; }
    A.side = side || 'buy';
    A.code = code || null;
    A.name = name || (code ? stockName(code) : '');
    $('tradeMask').classList.add('show');
    setSide(A.side);
    $('fStock').value = A.code ? (A.code.replace('.', '') + ' ' + A.name) : '';
    $('fNote').value = '';
    $('fTag').value = '';
    $('fShares').value = '';
    $('tradeMsg').innerHTML = '';
    refreshTradePrice();
    updateFeePreview();
  }

  function switchToAccount() {
    const b = document.querySelector('.tabbar button[data-tab="account"]');
    if (b) b.click();
  }

  function setSide(s) {
    A.side = s;
    $('sideBuy').className = s === 'buy' ? 'on buy' : '';
    $('sideSell').className = s === 'sell' ? 'on sell' : '';
    $('tradeTitle').textContent = s === 'buy' ? '买入建仓' : '卖出平仓';
    $('btnSubmit').className = 'btn block ' + (s === 'buy' ? 'buy' : 'sell');
    updateFeePreview();
  }

  function refreshTradePrice() {
    if (!A.code) { $('priceHint').textContent = ''; return; }
    const px = priceOf(A.code);
    $('priceHint').textContent = '（' + priceSource(A.code) + '）';
    if (px != null && !$('fPrice').value) $('fPrice').value = px.toFixed(3);
  }

  function updateFeePreview() {
    const price = +$('fPrice').value || 0;
    const shares = +$('fShares').value || 0;
    if (!price || !shares) {
      $('feePreview').innerHTML = '<span class="muted" style="font-size:13px">填好价格与数量后，这里会显示预估费用</span>';
      return;
    }
    const f = calcFees(A.side, price, shares);
    const pos = A.pos[A.code];
    const avail = pos ? Math.max(0, pos.shares - pos.todayBuy) : 0;
    const net = A.side === 'buy' ? f.amount + f.total : f.amount - f.total;
    $('feePreview').innerHTML =
      '<div style="display:flex;justify-content:space-between;font-size:13.5px;padding:2px 0">' +
        '<span class="muted">成交金额</span><b>' + num(f.amount, 2) + '</b></div>' +
      '<div style="display:flex;justify-content:space-between;font-size:13.5px;padding:2px 0">' +
        '<span class="muted">佣金（万 2.5，最低 5 元）</span><span>' + num(f.commission, 2) + '</span></div>' +
      (A.side === 'sell' ? '<div style="display:flex;justify-content:space-between;font-size:13.5px;padding:2px 0">' +
        '<span class="muted">印花税（千 1）</span><span>' + num(f.stamp, 2) + '</span></div>' : '') +
      '<div style="display:flex;justify-content:space-between;font-size:13.5px;padding:2px 0">' +
        '<span class="muted">过户费（万 0.1）</span><span>' + num(f.transfer, 2) + '</span></div>' +
      '<div class="hr" style="margin:8px 0"></div>' +
      '<div style="display:flex;justify-content:space-between;font-size:14px">' +
        '<b>' + (A.side === 'buy' ? '需付' : '可得') + '</b>' +
        '<b class="' + (A.side === 'buy' ? 'up' : 'down') + '">' + num(net, 2) + '</b></div>' +
      '<div class="t2" style="margin-top:6px"><span>可用现金 ' + num(A.cash, 0) + '</span>' +
        (A.side === 'sell' ? '<span>可卖 ' + num(avail, 0) + ' 股（T+1）</span>' : '') + '</div>';
  }

  async function submitTrade() {
    const msg = $('tradeMsg');
    msg.innerHTML = '';
    if (!A.code) return showMsg('请先选择股票');
    const price = +$('fPrice').value;
    const shares = Math.round(+$('fShares').value);
    if (!(price > 0)) return showMsg('成交价必须大于 0');
    if (!(shares > 0)) return showMsg('数量必须大于 0');
    if (shares % 100 !== 0) return showMsg('A 股买入/卖出数量须为 100 股的整数倍');

    const pos = A.pos[A.code];
    const hold = pos ? pos.shares : 0;
    const avail = pos ? Math.max(0, pos.shares - pos.todayBuy) : 0;
    const f = calcFees(A.side, price, shares);

    if (A.side === 'sell') {
      if (shares > avail) {
        return showMsg(hold > avail
          ? '可卖不足：持仓 ' + num(hold, 0) + ' 股，其中 ' + num(pos.todayBuy, 0) + ' 股为今日买入（T+1 需次日才可卖）'
          : '没有可卖持仓（当前持有 ' + num(hold, 0) + ' 股）');
      }
    } else if (f.amount + f.total > A.cash + 0.01) {
      return showMsg('可用现金不足：需 ' + num(f.amount + f.total, 2) + '，可用 ' + num(A.cash, 2));
    }

    const btn = $('btnSubmit');
    btn.disabled = true; btn.textContent = '提交中…';
    const nick = (A.session.user && A.session.user.name) ? A.session.user.name : null;
    const { data, error } = await A.cloud.database.from('ma60_trades').insert({
      account_id: A.account.id,
      code: A.code,
      stock_name: stockName(A.code),
      side: A.side,
      price: price,
      shares: shares,
      amount: f.amount,
      commission: f.commission,
      stamp_tax: f.stamp,
      transfer_fee: f.transfer,
      fee_total: f.total,
      trade_date: todayStr(),
      tag: $('fTag').value || null,
      note: $('fNote').value.trim() || null,
      owner_name: nick,
    }).select();
    btn.disabled = false; btn.textContent = '确认';

    if (error) return showMsg('保存失败：' + (error.message || error.code));
    if (!data || !data.length) return showMsg('保存被拒绝（可能登录已过期，请重新登录）');

    A.trades.push(data[0]);
    A.pos = computePositions();
    A.cash = computeCash();
    WB.closeTrade();
    renderAll();
  }

  function showMsg(t) {
    $('tradeMsg').innerHTML = '<div class="banner err">' + esc(t) + '</div>';
  }

  async function deleteTrade(t) {
    if (!confirm('撤销这笔成交？\n' + t.trade_date + ' ' + (t.side === 'buy' ? '买入' : '卖出') + ' ' +
      stockName(t.code) + ' ' + t.shares + ' 股 @' + t.price)) return;
    const { data, error } = await A.cloud.database.from('ma60_trades')
      .delete().eq('id', t.id).select();
    if (error) { alert('删除失败：' + (error.message || error.code)); return; }
    if (!data || !data.length) { alert('这笔记录不属于当前账号，未删除。'); return; }
    A.trades = A.trades.filter((x) => x.id !== t.id);
    A.pos = computePositions();
    A.cash = computeCash();
    renderAll();
  }

  function exportCsv() {
    const head = ['日期', '方向', '代码', '名称', '价格', '数量', '成交额', '佣金', '印花税', '过户费', '费用合计', '标签', '备注'];
    const rows = A.trades.map((t) => [t.trade_date, t.side === 'buy' ? '买入' : '卖出',
      t.code.replace('.', ''), t.stock_name || '', t.price, t.shares, t.amount,
      t.commission, t.stamp_tax, t.transfer_fee, t.fee_total, t.tag || '', t.note || '']);
    const csv = '\ufeff' + [head].concat(rows).map((r) =>
      r.map((v) => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"').join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = '模拟账户成交流水_' + todayStr() + '.csv';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 3000);
  }

  async function logout() {
    if (!confirm('退出登录？')) return;
    await A.cloud.auth.signOut();
    A.session = null; A.account = null; A.trades = []; A.pos = {}; A.ready = false;
    loginView();
  }

  /* ------------------------------------------------ 图表买卖点
     把成交日对齐到周 K 的横轴刻度上。腾讯周线的刻度是「该周最后交易日」，
     所以成交日（周二/周三…）要往后吸附到本周那根 K 线 —— 否则坐标落不到
     任何一个类目上，标记会静默消失。
     早于序列首日（或序列为空）时原样返回，交给图表自行忽略。 */
  function weekOf(dateStr, weeks) {
    if (!weeks || !weeks.length) return dateStr;
    let lo = 0, hi = weeks.length - 1, ans = null;
    while (lo <= hi) {
      const m = (lo + hi) >> 1;
      if (weeks[m] >= dateStr) { ans = weeks[m]; hi = m - 1; } else lo = m + 1;
    }
    return ans || weeks[weeks.length - 1];
  }

  function marksFor(code, weeks) {
    const buys = [], sells = [];
    for (const t of A.trades) {
      if (t.code !== code) continue;
      const item = { week: weekOf(t.trade_date, weeks), price: +t.price,
                     date: t.trade_date, shares: +t.shares };
      (t.side === 'buy' ? buys : sells).push(item);
    }
    return { buys: buys, sells: sells };
  }

  /* ------------------------------------------------ 生命周期 */
  function bind() {
    $('sideBuy').addEventListener('click', () => setSide('buy'));
    $('sideSell').addEventListener('click', () => setSide('sell'));
    $('btnSubmit').addEventListener('click', submitTrade);
    $('btnExport').addEventListener('click', exportCsv);
    $('btnLogout').addEventListener('click', logout);
    $('btnTrade').addEventListener('click', () => {
      if (!A.ready) { switchToAccount(); return; }
      openTrade('buy', null, null);
    });
    ['fPrice', 'fShares'].forEach((id) => $(id).addEventListener('input', updateFeePreview));

    const inp = $('fStock');
    let sug = null;
    const closeSug = () => { if (sug) { sug.remove(); sug = null; } };
    inp.addEventListener('input', () => {
      closeSug();
      const q = inp.value.trim();
      A.code = null;
      const list = searchStocks(q);
      if (!list.length) return;
      sug = el('div', 'card');
      sug.style.cssText = 'position:absolute;z-index:20;left:18px;right:18px;margin-top:-8px;max-height:260px;overflow:auto';
      list.forEach((it) => {
        const r = el('div', 'row tap');
        r.innerHTML = '<div class="grow"><div class="t1">' + esc(it.name) + '</div>' +
          '<div class="t2"><span class="code">' + esc(it.code.replace('.', '')) + '</span></div></div>';
        r.addEventListener('click', () => {
          A.code = it.code; A.name = it.name;
          inp.value = it.code.replace('.', '') + ' ' + it.name;
          closeSug();
          $('fPrice').value = '';
          refreshTradePrice();
          updateFeePreview();
        });
        sug.appendChild(r);
      });
      inp.parentNode.style.position = 'relative';
      inp.parentNode.appendChild(sug);
    });
    document.addEventListener('click', (e) => { if (e.target !== inp) closeSug(); });
  }

  window.WB_ACCOUNT = {
    boot: function () {
      if (A.booted) return;
      A.booted = true;
      bind();
      if (!window.WorkBuddyCloud) {
        authBox().innerHTML = '<div class="banner err">云端 SDK 未能加载，请检查网络后刷新。</div>';
        return;
      }
      A.cloud = window.WorkBuddyCloud.createWorkBuddyCloud({
        endpoint: PUBLIC_CONFIG.endpoint,
        oauthRelayBaseUrl: PUBLIC_CONFIG.oauthRelayBaseUrl,
        publishableKey: PUBLIC_CONFIG.publishableKey,
      });
      restoreAuth().catch((e) => showLoginError(String(e && e.message || e)));
    },
    onShow: function () {
      if (!A.ready) return;
      refreshLive().then(() => { renderAll(); }).catch(() => {});
    },
    onQuotes: function () {
      if (A.ready) renderAll();
    },
    openTrade: openTrade,
    marksFor: marksFor,
    resize: function () { if (A.equityChart) A.equityChart.resize(); },
  };
})();
