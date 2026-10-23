/* ============================================================================
 * dashboard.js — 数据看板（U14）
 * ----------------------------------------------------------------------------
 * 参考 ARTEX 数据看板的信息层级（KPI 指标卡行 → 趋势图 → 分布/榜单 → 最近动态），
 * 但**不引入任何图表库**（本项目无构建链、无外部依赖）：趋势与分布全部用
 * 轻量 CSS 柱条绘制，颜色一律走 css/style.css 的设计变量。
 *
 * 数据来源：GET /api/stats/dashboard（一次性聚合，避免逐卡片请求）。
 *
 * 指标口径（按产品要求）：
 *   **已捕获 flag 即视为已解答** —— 「解题数 = 已捕获 FLAG 数」，
 *   「解题成功率 = 解题数 / 题目总数」；
 *   题目维度的归属状态（控制台 verdict / 绑定会话 flag）另由后端 status 字段提供备查。
 * ========================================================================== */
(function (global) {
  'use strict';

  var CTFA = (global.CTFA = global.CTFA || {});
  var doc = global.document;

  // 趋势固定拉取 MAX_DAYS 天，缩放只改可视区间（不重复请求后端）
  var MAX_DAYS = 90;
  var DEFAULT_SPAN = 14;
  var ZOOM_MIN = 3;
  var LS_PROJECT = 'ctfa.dashProject';

  var state = {
    box: null, data: null, loading: false, error: '', fetchedAt: 0, chartObserver: null,
    projectId: '',                                  // '' = 全局总览
    zoom: { span: DEFAULT_SPAN, end: null }         // end=null 表示贴住最新一天
  };

  function loadProject() {
    try { state.projectId = global.localStorage.getItem(LS_PROJECT) || ''; } catch (e) { state.projectId = ''; }
  }

  function saveProject(id) {
    state.projectId = id || '';
    try {
      if (state.projectId) global.localStorage.setItem(LS_PROJECT, state.projectId);
      else global.localStorage.removeItem(LS_PROJECT);
    } catch (e) { /* 隐私模式忽略 */ }
  }

  /* ------------------------------ 小工具 ------------------------------ */

  function el(tag, cls, text) {
    var node = doc.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

  function badge(text, tone) { return el('span', 'badge badge-' + (tone || 'chip'), text); }

  function fmtNum(n) {
    var v = Number(n) || 0;
    if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
    if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
    if (v >= 1e4) return (v / 1e3).toFixed(1) + 'k';
    return String(v);
  }

  function fmtPct(ratio) {
    var v = Number(ratio) || 0;
    return Math.round(v * 1000) / 10 + '%';
  }

  function fmtRel(ms) {
    if (!ms) return '—';
    var diff = Date.now() - Number(ms);
    if (diff < 0) diff = 0;
    var s = Math.floor(diff / 1000);
    if (s < 60) return s + ' 秒前';
    var m = Math.floor(s / 60);
    if (m < 60) return m + ' 分钟前';
    var h = Math.floor(m / 60);
    if (h < 24) return h + ' 小时前';
    return Math.floor(h / 24) + ' 天前';
  }

  function fmtTime(ms) {
    if (!ms) return '';
    var d = new Date(Number(ms));
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
      + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  /* ------------------------------ 数据加载 ------------------------------ */

  function load() {
    if (state.loading) return Promise.resolve();
    state.loading = true;
    if (!state.data) paintLoading();
    var query = '/api/stats/dashboard?days=' + MAX_DAYS
      + (state.projectId ? '&project_id=' + encodeURIComponent(state.projectId) : '');
    return CTFA.api.request(query)
      .then(function (data) {
        state.data = data || null;
        state.error = '';
        state.fetchedAt = Date.now();
      })
      .catch(function (err) {
        state.error = (err && err.message) || String(err);
      })
      .then(function () {
        state.loading = false;
        paint();
      });
  }

  function paintLoading() {
    if (!state.box) return;
    clear(state.box);
    state.box.appendChild(el('div', 'dash-empty', '正在加载统计数据…'));
  }

  /* ------------------------------- 渲染 ------------------------------- */

  function paint() {
    var box = state.box;
    if (!box) return;
    // 重建 DOM 前断开上一轮折线图的尺寸观察器，避免泄漏
    if (state.chartObserver) { state.chartObserver.disconnect(); state.chartObserver = null; }
    clear(box);

    if (state.error) {
      var errBox = el('div', 'dash-empty');
      errBox.appendChild(el('div', null, '统计数据加载失败：' + state.error));
      var retry = el('button', 'btn', '重试');
      retry.addEventListener('click', function () { load(); });
      errBox.appendChild(retry);
      box.appendChild(errBox);
      return;
    }
    if (!state.data) { paintLoading(); return; }

    box.appendChild(headerEl(state.data));
    box.appendChild(kpiRow(state.data));
    box.appendChild(trendCard(state.data));
    var row2 = el('div', 'dash-grid dash-grid-3');
    row2.appendChild(categoryCard(state.data));
    row2.appendChild(toolsCard(state.data));
    row2.appendChild(projectCard(state.data));
    box.appendChild(row2);
    var row3 = el('div', 'dash-grid dash-grid-2');
    row3.appendChild(recentFlagsCard(state.data));
    row3.appendChild(recentChallengesCard(state.data));
    box.appendChild(row3);
  }

  function optionEl(value, label) {
    var o = doc.createElement('option');
    o.value = value;
    o.textContent = label;
    return o;
  }

  function headerEl(data) {
    var scope = data.scope || {};
    var inProject = scope.kind === 'project';
    var head = el('div', 'dash-head');
    var left = el('div', 'dash-head-main');
    left.appendChild(el('span', 'view-hint', inProject
      ? '项目范围：' + (scope.projectName || scope.projectId) + '（仅统计本项目的会话 / FLAG / 题目）'
      : '全局总览 · 汇总所有项目与会话'));
    head.appendChild(left);

    var right = el('div', 'chips');
    var select = el('select', 'dash-scope-select');
    select.title = '切换统计范围：全局总览 / 指定项目';
    select.setAttribute('aria-label', '统计范围');
    select.appendChild(optionEl('', '全局总览'));
    (data.projects || []).forEach(function (p) {
      select.appendChild(optionEl(p.id, p.name));
    });
    select.value = scope.projectId || '';
    select.addEventListener('change', function () {
      saveProject(select.value);
      state.zoom = { span: DEFAULT_SPAN, end: null };   // 切换范围时重置时间轴缩放
      load();
    });
    right.appendChild(select);

    right.appendChild(el('span', 'dash-stamp', '统计于 ' + fmtTime(state.fetchedAt || data.generatedAt)));
    var btn = el('button', 'btn', '⟳ 刷新');
    btn.addEventListener('click', function () { load(); });
    right.appendChild(btn);
    head.appendChild(right);
    return head;
  }

  /* ------------------------------- KPI 行 ------------------------------- */

  function tile(label, value, sub, tone) {
    var box = el('div', 'dash-tile' + (tone ? ' dash-tile-' + tone : ''));
    box.appendChild(el('div', 'dash-tile-label', label));
    box.appendChild(el('div', 'dash-tile-value', value));
    if (sub) box.appendChild(sub);
    return box;
  }

  function miniStats(pairs) {
    var row = el('div', 'dash-tile-sub');
    pairs.forEach(function (pair) {
      if (pair[1] === null || pair[1] === undefined) return;
      row.appendChild(el('span', 'dash-mini dash-mini-' + (pair[2] || 'muted'), pair[0] + ' ' + pair[1]));
    });
    return row;
  }

  function kpiRow(data) {
    var t = data.totals || {};
    var live = data.live || {};
    var st = data.status || {};
    var running = (live.runningConversations || 0) + (live.runningChallenges || 0);
    var grid = el('div', 'dash-kpis');
    var lastFlag = (data.recentFlags || [])[0];

    grid.appendChild(tile('题目数', fmtNum(t.challenges),
      miniStats([['尝试', t.attempted, 'muted'], ['解题中', st.running, 'info']])));

    // 解题数 = 已捕获 FLAG 数（「拿到 flag 即视为已解答」）
    grid.appendChild(tile('解题数', fmtNum(t.solved),
      miniStats([['近 7 天', t.flagsRecent, 'ok'], ['成功率', fmtPct(t.successRate), 'muted']]), 'accent'));

    // 成功率 = 有 flag 的题目数 / 题目总数（题目维度：分子分母同量纲，
    // 不会被「一题多 flag」顶到 100% 以上）；题目为 0 时后端返回 null → 显示「—」
    var rate = (t.successRate === null || t.successRate === undefined) ? null : t.successRate;
    grid.appendChild(tile('解题成功率', rate === null ? '—' : fmtPct(rate),
      miniStats([['已解答题', t.solvedByChallenge, 'ok'], ['题目', t.challenges, 'muted']])));

    grid.appendChild(tile('FLAG 数', fmtNum(t.flags),
      miniStats([['覆盖会话', t.flagsConversations, 'muted'],
        ['最近', lastFlag ? fmtRel(lastFlag.at) : '—', 'muted']])));

    grid.appendChild(tile('进行中任务', fmtNum(running),
      miniStats([['对话', live.runningConversations, 'info'], ['题目', live.runningChallenges, 'info'],
        ['Solver', live.activeSolvers, 'muted']]), running ? 'running' : ''));

    grid.appendChild(tile('会话数', fmtNum(t.conversations),
      miniStats([['消息', fmtNum(t.messages), 'muted'], ['项目', t.projects, 'muted']])));
    return grid;
  }

  /* ------------------------------ 趋势折线图 ------------------------------ */
  // 纯 SVG 自绘（无图表库）：FLAG 捕获（左轴）与工具调用（右轴）两条折线同图对比。
  // 两条序列量纲差异大（0-3 vs 0-128），故采用**双 Y 轴**，左右轴各自标注刻度。
  // 交互：鼠标停留在某条折线上 → 该线加粗发光、另一条淡出、显示该点标记 + 数据浮层。
  //
  // 注：SVG 以「容器实测宽度」为 viewBox 宽度渲染（1:1 缩放），避免 preserveAspectRatio
  // 拉伸导致圆点变椭圆、文字变形；容器尺寸变化由 ResizeObserver 触发重建。

  var SVG_NS = 'http://www.w3.org/2000/svg';
  var CHART_H = 220;
  var CHART_PAD_L = 40;
  var CHART_PAD_R = 54;

  function svgNode(tag, attrs) {
    var node = doc.createElementNS(SVG_NS, tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) { node.setAttribute(key, attrs[key]); });
    }
    return node;
  }

  function lineChart(trend) {
    var wrap = el('div', 'dash-linewrap');
    var tip = el('div', 'dash-tip');
    var active = { series: '', idx: -1 };
    var current = null;   // 本次构建的节点索引

    function clearHover() {
      active.series = '';
      active.idx = -1;
      if (!current) return;
      current.root.classList.remove('is-hover');
      current.lineF.classList.remove('on');
      current.lineT.classList.remove('on');
      current.dotsF.forEach(function (c) { c.classList.remove('on'); });
      current.dotsT.forEach(function (c) { c.classList.remove('on'); });
      current.halo.classList.remove('on');
      tip.classList.remove('on');
    }

    function paintHover() {
      if (!current) return;
      var i = active.idx;
      var n = trend.length;
      if (!active.series || i < 0 || i >= n) { clearHover(); return; }
      var d = trend[i];
      var isFlags = active.series === 'flags';
      current.root.classList.add('is-hover');
      current.lineF.classList.toggle('on', isFlags);
      current.lineT.classList.toggle('on', !isFlags);
      current.dotsF.forEach(function (c, k) { c.classList.toggle('on', isFlags && k === i); });
      current.dotsT.forEach(function (c, k) { c.classList.toggle('on', !isFlags && k === i); });

      var cx = current.px(i);
      var cy = isFlags ? current.pyF(d.flags) : current.pyT(d.toolCalls);
      current.halo.setAttribute('cx', cx);
      current.halo.setAttribute('cy', cy);
      current.halo.setAttribute('class', 'dash-halo on ' + (isFlags ? 'dash-halo-flags' : 'dash-halo-tools'));
      current.guide.setAttribute('x1', cx);
      current.guide.setAttribute('x2', cx);

      clear(tip);
      tip.appendChild(el('div', 'dash-tip-date', d.date));
      [
        ['FLAG 捕获', d.flags, isFlags],
        ['工具调用', d.toolCalls, !isFlags]
      ].forEach(function (row) {
        var line = el('div', 'dash-tip-row' + (row[2] ? ' on' : '')
          + (row[0].indexOf('FLAG') === 0 ? ' dash-tip-flags' : ' dash-tip-tools'));
        line.appendChild(el('span', 'dash-tip-key', row[0]));
        line.appendChild(el('span', 'dash-tip-val mono', fmtNum(row[1])));
        tip.appendChild(line);
      });

      var leftPct = cx / current.W * 100;
      var topPct = cy / CHART_H * 100;
      tip.style.left = leftPct + '%';
      tip.style.top = topPct + '%';
      tip.classList.toggle('flip-x', leftPct > 66);
      tip.classList.toggle('flip-y', topPct < 36);
      tip.classList.add('on');
    }

    function build() {
      var W = Math.round(wrap.clientWidth || 0);
      if (W < 220) return;                       // 容器不可见/过窄：等下一次尺寸变化
      var padL = CHART_PAD_L, padR = CHART_PAD_R, padT = 16, padB = 26;
      var plotW = W - padL - padR;
      var plotH = CHART_H - padT - padB;
      var n = trend.length;
      var maxF = trend.reduce(function (a, d) { return Math.max(a, d.flags || 0); }, 0);
      var maxT = trend.reduce(function (a, d) { return Math.max(a, d.toolCalls || 0); }, 0);
      var scaleF = maxF || 1;
      var scaleT = maxT || 1;

      function px(i) { return n > 1 ? padL + plotW * i / (n - 1) : padL + plotW / 2; }
      function pyF(v) { return padT + plotH * (1 - (v || 0) / scaleF); }
      function pyT(v) { return padT + plotH * (1 - (v || 0) / scaleT); }

      var old = wrap.querySelector('svg');
      if (old) old.remove();

      var root = svgNode('svg', {
        viewBox: '0 0 ' + W + ' ' + CHART_H,
        width: '100%',
        height: String(CHART_H),
        class: 'dash-line-chart',
        role: 'img'
      });

      // 横向网格 + 左右轴刻度
      [0, 0.5, 1].forEach(function (frac) {
        var y = padT + plotH * frac;
        root.appendChild(svgNode('line', {
          x1: padL, y1: y, x2: padL + plotW, y2: y, class: 'dash-grid-line'
        }));
        var lf = svgNode('text', {
          x: padL - 8, y: y + 3.5, 'text-anchor': 'end', class: 'dash-axis-tick dash-tick-flags'
        });
        lf.textContent = fmtNum(Math.round(scaleF * (1 - frac)));
        root.appendChild(lf);
        var rt = svgNode('text', {
          x: padL + plotW + 8, y: y + 3.5, 'text-anchor': 'start', class: 'dash-axis-tick dash-tick-tools'
        });
        rt.textContent = fmtNum(Math.round(scaleT * (1 - frac)));
        root.appendChild(rt);
      });

      // x 轴日期（首 / 中 / 尾）
      trend.forEach(function (d, i) {
        var show = i === 0 || i === n - 1 || i === Math.floor((n - 1) / 2);
        if (!show) return;
        var t = svgNode('text', {
          x: px(i), y: CHART_H - 8,
          'text-anchor': i === 0 ? 'start' : (i === n - 1 ? 'end' : 'middle'),
          class: 'dash-axis-tick'
        });
        t.textContent = d.date;
        root.appendChild(t);
      });

      // 悬停垂直指示线
      var guide = svgNode('line', {
        x1: -10, y1: padT, x2: -10, y2: padT + plotH, class: 'dash-guide'
      });
      root.appendChild(guide);

      var ptsF = trend.map(function (d, i) { return px(i) + ',' + pyF(d.flags); }).join(' ');
      var ptsT = trend.map(function (d, i) { return px(i) + ',' + pyT(d.toolCalls); }).join(' ');

      var lineF = svgNode('polyline', { points: ptsF, class: 'dash-line dash-line-flags' });
      var lineT = svgNode('polyline', { points: ptsT, class: 'dash-line dash-line-tools' });
      root.appendChild(lineF);
      root.appendChild(lineT);

      var dotsF = [], dotsT = [];
      trend.forEach(function (d, i) {
        var cf = svgNode('circle', { cx: px(i), cy: pyF(d.flags), r: 3, class: 'dash-dot dash-dot-flags' });
        var ct = svgNode('circle', { cx: px(i), cy: pyT(d.toolCalls), r: 3, class: 'dash-dot dash-dot-tools' });
        root.appendChild(cf); root.appendChild(ct);
        dotsF.push(cf); dotsT.push(ct);
      });

      var halo = svgNode('circle', { cx: -20, cy: -20, r: 6, class: 'dash-halo' });
      root.appendChild(halo);

      // 命中区：宽描边透明折线，让「停在折线上」容易命中
      var hitF = svgNode('polyline', { points: ptsF, class: 'dash-line-hit' });
      var hitT = svgNode('polyline', { points: ptsT, class: 'dash-line-hit' });
      root.appendChild(hitF);
      root.appendChild(hitT);

      current = { W: W, root: root, lineF: lineF, lineT: lineT, dotsF: dotsF, dotsT: dotsT,
                  halo: halo, guide: guide, px: px, pyF: pyF, pyT: pyT };

      function indexFromClientX(clientX) {
        var rect = root.getBoundingClientRect();
        if (!rect.width) return -1;
        var vbX = (clientX - rect.left) * (W / rect.width);
        var step = n > 1 ? plotW / (n - 1) : plotW;
        var i = Math.round((vbX - padL) / step);
        return Math.max(0, Math.min(n - 1, i));
      }

      [[ 'flags', hitF ], [ 'tools', hitT ]].forEach(function (pair) {
        pair[1].addEventListener('mouseenter', function (e) {
          active.series = pair[0];
          // 进入折线时先按指针位置定位到最近的数据点，避免「未移动鼠标就看不到浮层」
          active.idx = indexFromClientX(e.clientX);
          paintHover();
        });
        pair[1].addEventListener('mouseleave', function () {
          if (active.series === pair[0]) clearHover();
        });
      });
      root.addEventListener('mousemove', function (e) {
        if (!active.series) return;
        var i = indexFromClientX(e.clientX);
        if (i !== active.idx) { active.idx = i; paintHover(); }
      });
      root.addEventListener('mouseleave', clearHover);

      wrap.insertBefore(root, tip);
      clearHover();
    }

    wrap.appendChild(tip);
    global.setTimeout(build, 0);
    if (global.ResizeObserver) {
      state.chartObserver = new global.ResizeObserver(function () { build(); });
      state.chartObserver.observe(wrap);
    }
    return { el: wrap, build: build, maxFlags: 0, maxTools: 0 };
  }

  function trendCard(data) {
    var card = el('section', 'dash-card');
    var all = data.trend || [];

    var head = el('div', 'dash-card-head');
    var title = el('h3', 'dash-card-title', '趋势');
    head.appendChild(title);
    head.appendChild(el('span', 'dash-card-sub', '左轴 FLAG 捕获 · 右轴 工具调用（各自缩放）'));

    var ops = el('div', 'chips dash-trend-ops');
    var rangeTag = el('span', 'dash-range-tag', '');
    var resetBtn = el('button', 'btn btn-ghost dash-zoom-reset', '重置缩放');
    resetBtn.title = '恢复最近 ' + DEFAULT_SPAN + ' 天（也可双击图表）';
    resetBtn.addEventListener('click', function () {
      state.zoom = { span: DEFAULT_SPAN, end: null };
      render();
    });
    ops.appendChild(rangeTag);
    ops.appendChild(resetBtn);
    head.appendChild(ops);
    card.appendChild(head);

    var body = el('div', 'dash-trend-body');
    card.appendChild(body);
    if (!all.length) { body.appendChild(emptyRow('暂无趋势数据')); return card; }

    var chartWrap = null;

    /** 当前可视区间（end 为开区间下标）。 */
    function visible() {
      var n = all.length;
      var span = Math.max(ZOOM_MIN, Math.min(state.zoom.span || DEFAULT_SPAN, n));
      var end = state.zoom.end == null ? n : Math.max(span, Math.min(state.zoom.end, n));
      var start = Math.max(0, end - span);
      return { slice: all.slice(start, end), start: start, end: start + (end - start) };
    }

    function resetZoom() {
      state.zoom = { span: DEFAULT_SPAN, end: null };
      render();
    }

    function onWheel(evt) {
      var n = all.length;
      if (n <= ZOOM_MIN) return;
      evt.preventDefault();
      var view = visible();
      var curSpan = view.slice.length;
      var rect = chartWrap ? chartWrap.getBoundingClientRect() : null;
      if (!rect || !rect.width) return;
      var plotW = Math.max(1, rect.width - CHART_PAD_L - CHART_PAD_R);
      var ratio = (evt.clientX - rect.left - CHART_PAD_L) / plotW;
      ratio = Math.max(0, Math.min(1, ratio));
      var anchor = view.start + ratio * (curSpan - 1);
      var factor = evt.deltaY < 0 ? 0.8 : 1.25;        // 上滚放大、下滚缩小
      var next = Math.round(curSpan * factor);
      next = Math.max(ZOOM_MIN, Math.min(n, next));
      if (next === curSpan) return;
      var start = Math.round(anchor - ratio * (next - 1));
      start = Math.max(0, Math.min(n - next, start));
      state.zoom = { span: next, end: start + next };
      render();
    }

    function render() {
      var view = visible();
      var slice = view.slice;
      var spanDays = slice.length;
      var n = all.length;

      if (state.chartObserver) { state.chartObserver.disconnect(); state.chartObserver = null; }
      clear(body);

      title.textContent = '趋势（' + slice[0].date + ' ~ ' + slice[spanDays - 1].date + ' · ' + spanDays + ' 天）';
      rangeTag.textContent = '可视 ' + spanDays + ' / ' + n + ' 天';
      resetBtn.hidden = spanDays === DEFAULT_SPAN && state.zoom.end == null;

      var legend = el('div', 'dash-legend');
      legend.appendChild(el('span', 'dash-legend-item dash-swatch-accent', 'FLAG 捕获（左轴）'));
      legend.appendChild(el('span', 'dash-legend-item dash-swatch-alt', '工具调用（右轴）'));
      legend.appendChild(el('span', 'dash-legend-hint', '滚轮缩放时间轴 · 双击重置'));
      body.appendChild(legend);

      var chart = lineChart(slice);
      chartWrap = chart.el;
      chartWrap.addEventListener('wheel', onWheel, { passive: false });
      chartWrap.addEventListener('dblclick', resetZoom);
      body.appendChild(chartWrap);

      var sumFlags = slice.reduce(function (a, d) { return a + d.flags; }, 0);
      var sumTools = slice.reduce(function (a, d) { return a + d.toolCalls; }, 0);
      var sumConvs = slice.reduce(function (a, d) { return a + d.conversations; }, 0);
      var maxF = slice.reduce(function (a, d) { return Math.max(a, d.flags || 0); }, 0);
      var maxT = slice.reduce(function (a, d) { return Math.max(a, d.toolCalls || 0); }, 0);
      var foot = el('div', 'dash-card-foot');
      foot.appendChild(el('span', 'dash-foot-item', '区间内 FLAG ' + sumFlags));
      foot.appendChild(el('span', 'dash-foot-item', '新建会话 ' + sumConvs));
      foot.appendChild(el('span', 'dash-foot-item', '工具调用 ' + fmtNum(sumTools)));
      foot.appendChild(el('span', 'dash-foot-item', '单日峰值 FLAG ' + maxF + ' / 调用 ' + fmtNum(maxT)));
      body.appendChild(foot);
    }

    render();
    return card;
  }

  /* ---------------------------- 分布 / 榜单 ---------------------------- */

  function distRow(label, value, max, right, tone) {
    var row = el('div', 'dash-dist-row');
    row.appendChild(el('span', 'dash-dist-label', label));
    var track = el('div', 'dash-dist-track');
    var fill = el('div', 'dash-dist-fill' + (tone ? ' dash-fill-' + tone : ''));
    fill.style.width = (max ? Math.max(value / max * 100, value > 0 ? 3 : 0) : 0) + '%';
    track.appendChild(fill);
    row.appendChild(track);
    row.appendChild(el('span', 'dash-dist-value mono', String(value)));
    if (right) row.appendChild(el('span', 'dash-dist-right', right));
    return row;
  }

  function card(title, sub) {
    var c = el('section', 'dash-card');
    var head = el('div', 'dash-card-head');
    head.appendChild(el('h3', 'dash-card-title', title));
    if (sub) head.appendChild(el('span', 'dash-card-sub', sub));
    c.appendChild(head);
    return c;
  }

  function emptyRow(text) { return el('div', 'dash-empty-sm', text); }

  function categoryCard(data) {
    var c = card('题目类型分布', '已解答 / 总数');
    var list = data.categories || [];
    if (!list.length) { c.appendChild(emptyRow('暂无题目')); return c; }
    var max = list.reduce(function (a, x) { return Math.max(a, x.total); }, 0);
    list.forEach(function (item) {
      c.appendChild(distRow(item.key, item.total, max, item.solved ? (item.solved + ' 已解答') : '', 'accent'));
    });
    return c;
  }

  function toolsCard(data) {
    var t = data.totals || {};
    var c = card('工具调用 Top', '失败 ' + t.toolFailures + ' / 共 ' + fmtNum(t.toolCalls));
    var list = data.tools || [];
    if (!list.length) { c.appendChild(emptyRow('暂无工具调用记录')); return c; }
    var max = list.reduce(function (a, x) { return Math.max(a, x.calls); }, 0);
    list.forEach(function (item) {
      var right = item.failures ? ('失败 ' + item.failures) : '';
      c.appendChild(distRow(item.name, item.calls, max, right, 'accent'));
    });
    return c;
  }

  function projectCard(data) {
    var list = data.projects || [];
    // 卡片始终展示全量项目（便于对照），标题按列表长度而不是 totals.projects
    var c = card('项目概览', list.length + ' 个项目');
    if (!list.length) { c.appendChild(emptyRow('暂无项目')); return c; }
    list.forEach(function (p) {
      var row = el('div', 'dash-list-row');
      row.appendChild(el('span', 'dash-list-main', p.name));
      row.appendChild(el('span', 'dash-list-meta mono', p.conversations + ' 会话 · ' + p.flags + ' flag'));
      c.appendChild(row);
    });
    return c;
  }

  function recentFlagsCard(data) {
    var c = card('最近捕获 FLAG', (data.totals || {}).flags + ' 条记录');
    var list = data.recentFlags || [];
    if (!list.length) { c.appendChild(emptyRow('暂无 flag')); return c; }
    list.forEach(function (f) {
      var row = el('div', 'dash-list-row');
      row.appendChild(el('span', 'dash-flag mono', f.value));
      row.appendChild(el('span', 'dash-list-meta', fmtRel(f.at)));
      c.appendChild(row);
    });
    return c;
  }

  function recentChallengesCard(data) {
    var c = card('最近题目', '按更新时间');
    var list = data.recentChallenges || [];
    if (!list.length) { c.appendChild(emptyRow('暂无题目')); return c; }
    list.forEach(function (ch) {
      var row = el('div', 'dash-list-row');
      row.appendChild(el('span', 'dash-list-main', ch.name));
      var tone = ch.solved ? 'on' : (ch.status === 'running' ? 'info' : 'chip');
      var text = ch.solved ? '已解答' : (ch.status === 'running' ? '解题中' : '未解答');
      row.appendChild(badge(text, tone));
      if (ch.category) row.appendChild(badge(ch.category, 'cat'));
      c.appendChild(row);
    });
    return c;
  }

  /* ------------------------------- 对外 ------------------------------- */

  CTFA.dashboardView = {
    init: function (options) {
      state.box = (options && options.box) || null;
      loadProject();
    },
    load: load,
    render: function (box) {
      if (box) state.box = box;
      paint();
    },
    _state: state
  };
})(typeof window !== 'undefined' ? window : this);
