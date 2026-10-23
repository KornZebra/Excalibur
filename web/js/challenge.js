/**
 * V2-M4 挑战控制台（blackboard 模式状态板）。
 *
 * 三栏（对应升级提示词 §6.4）：
 *   左 = Ideas 列表（状态徽标，点击看 transition 历史）
 *   中 = Solver 时间线（按 SolverRun 分组）
 *   右 = Memory 流（按 kind 过滤，hint/failure 置顶）
 * 顶部 = 倒计时 / 并发数 / 终止状态 + hint 输入框 + flag 提交框
 *
 * 数据来源全部是 v2 API（GET /api/challenges/{id}/state 为唯一真相）：
 *   - 打开时先 GET 一次快照 → 三栏渲染
 *   - 点"启动"用 SSE 订阅 POST /api/challenges/{id}/run 的全部事件
 *     （dispatcher_decision / solver_spawned / solver_concluded / idea_transition /
 *      efficiency_reminder / hint_added / challenge_terminated / done）
 *   - 事件只做"局部追加 + 节流刷新"，每次 terminated 后重新 GET 一次保证与后端一致
 *
 * 本文件不改动既有对话视图逻辑，只在 CTFA.challenge 命名空间下工作。
 */
(function (global) {
  'use strict';

  var CTFA = global.CTFA = global.CTFA || {};
  var doc = global.document;

  /** 控制台快照自动刷新间隔（ms）：题目状态无需手动刷新 */
  var AUTO_REFRESH_MS = 8000;

  var refs = {};
  var state = null;

  var board = {
    challengeId: '',
    snapshot: null,
    ideas: [],
    memories: [],
    solverRuns: [],
    solvers: {},          // solverRunId -> {taskType, status, events:[]}
    transitions: {},      // ideaId -> [{from,to,by,ts}]
    decisions: [],
    reminders: [],
    streaming: false,
    stopping: false,      // 已请求停止、等待归档（期间禁用启动/停止）
    terminated: false,    // 会话已归档（自动刷新跳过）
    refreshing: false,
    controller: null,
    timer: null
  };

  /* ------------------------------ 小工具 ------------------------------ */
  function el(tag, cls, text) {
    var node = doc.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

  function fmtTime(ts) {
    if (!ts) return '';
    var d = new Date(ts);
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  function fmtDuration(seconds) {
    var total = Math.max(0, Math.round(Number(seconds) || 0));
    var m = Math.floor(total / 60);
    var s = total % 60;
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  var STATUS_TONE = {
    pending: 'warn',
    testing: 'info',
    verified: 'ok',
    failed: 'danger',
    skipped: 'muted'
  };
  var STATUS_TEXT = {
    pending: '待验证',
    testing: '验证中',
    verified: '已验证',
    failed: '已排除',
    skipped: '已跳过'
  };
  var KIND_TONE = { hint: 'ok', failure: 'danger', fact: 'info', evidence: 'info', note: 'muted' };
  var KIND_TEXT = { hint: '人类提示', failure: '失败', fact: '事实', evidence: '证据', note: '备注' };

  /* ------------------------------ 渲染 ------------------------------ */
  function renderTop() {
    var top = refs.top;
    clear(top);
    var snap = board.snapshot || {};
    var live = (snap.live) || {};
    var session = snap.session || {};
    var verdict = live.verdict || session.verdict || '';
    var stateText = live.state || session.status || 'idle';

    top.appendChild(chip('题目 ' + (board.challengeId || '-'), board.challengeId ? 'chip' : 'warn'));
    if (!board.challengeId) {
      top.appendChild(chip('未选择题目：请在「题目」页或聊天区顶部下拉中选择', 'warn'));
      return;
    }
    top.appendChild(chip('状态 ' + stateText, stateText === 'running' ? 'info' : 'muted'));
    if (verdict) top.appendChild(chip('结论 ' + verdict, verdict === 'TERMINATE_SUCCESS' ? 'ok' : 'warn'));
    var budget = Number(snap.timeBudgetSeconds || 0);
    var used = Number(snap.elapsedSeconds || 0);
    top.appendChild(chip('用时 ' + fmtDuration(used) + (budget ? ' / ' + fmtDuration(budget) : ''), 'chip'));
    top.appendChild(chip('并发 ' + (snap.runningCount || 0) + '/' + ((live.solvers && live.solvers.maxConcurrent) || '-'), 'chip'));
    top.appendChild(chip('Idea ' + (snap.ideas ? snap.ideas.length : 0), 'chip'));
    top.appendChild(chip('Memory ' + (snap.memories ? snap.memories.length : 0), 'chip'));
    top.appendChild(chip('提醒 ' + board.reminders.length, board.reminders.length ? 'warn' : 'muted'));
    if (snap.flagVerified) top.appendChild(chip('FLAG 已提交', 'ok'));
    if (snap.scopeOk === false) {
      top.appendChild(chip('目标越界：已拒绝派发' + (snap.scopeReason ? '（' + snap.scopeReason + '）' : ''), 'danger'));
    } else if (snap.scopeReason) {
      top.appendChild(chip('范围 ' + String(snap.scopeReason).slice(0, 40), 'muted'));
    }
  }

  function chip(text, tone) {
    var node = el('span', 'chip' + (tone ? ' chip-' + tone : ''), text);
    return node;
  }

  function renderIdeas() {
    var box = refs.ideas;
    clear(box);
    if (!board.ideas.length) {
      box.appendChild(el('div', 'board-empty', '暂无 Idea。Dispatcher 会先派发 bootstrap 产出初始方向。'));
      return;
    }
    board.ideas.forEach(function (idea) {
      var card = el('article', 'idea-card idea-' + (STATUS_TONE[idea.status] || 'muted'));
      var head = el('div', 'idea-head');
      head.appendChild(chip(STATUS_TEXT[idea.status] || idea.status, STATUS_TONE[idea.status] || 'muted'));
      if (idea.claimedBy) head.appendChild(chip('认领 ' + String(idea.claimedBy).slice(-8), 'muted'));
      card.appendChild(head);
      card.appendChild(el('div', 'idea-text', idea.text || '(空)'));
      var history = board.transitions[idea.id] || [];
      if (history.length) {
        var ul = el('ul', 'idea-history');
        history.forEach(function (item) {
          ul.appendChild(el('li', null, fmtTime(item.ts) + ' ' + item.from + ' → ' + item.to + ' · ' + (item.by || 'agent')));
        });
        card.appendChild(ul);
      }
      card.addEventListener('click', function () {
        card.classList.toggle('open');
      });
      box.appendChild(card);
    });
  }

  function renderSolvers() {
    var box = refs.solvers;
    clear(box);
    var ids = Object.keys(board.solvers);
    if (!ids.length) {
      box.appendChild(el('div', 'board-empty', '暂无 Solver。启动后会看到 bootstrap → explore → reason 的派发与收尾。'));
      return;
    }
    ids.forEach(function (id) {
      var entry = board.solvers[id];
      var card = el('article', 'solver-card');
      var head = el('div', 'solver-head');
      head.appendChild(chip(entry.taskType || 'solver', 'info'));
      head.appendChild(chip(entry.status || 'running', entry.status === 'running' ? 'info' : 'muted'));
      if (entry.ideaId) head.appendChild(chip('idea ' + String(entry.ideaId).slice(-8), 'muted'));
      card.appendChild(head);
      card.appendChild(el('div', 'mono solver-id', id));
      var ul = el('ul', 'solver-events');
      (entry.events || []).slice(-30).forEach(function (event) {
        ul.appendChild(el('li', null, fmtTime(event.ts) + ' · ' + event.text));
      });
      card.appendChild(ul);
      box.appendChild(card);
    });
  }

  function renderMemories() {
    var box = refs.memories;
    clear(box);
    var filter = refs.memoryFilter && refs.memoryFilter.value;
    var list = board.memories.slice();
    // hint / failure 置顶（与后端 MEMORY_PRIORITY 语义一致）
    var weight = { hint: 0, failure: 1, fact: 2, evidence: 3, note: 4 };
    list.sort(function (a, b) {
      var wa = weight[a.kind] === undefined ? 9 : weight[a.kind];
      var wb = weight[b.kind] === undefined ? 9 : weight[b.kind];
      if (wa !== wb) return wa - wb;
      return String(b.createdAt || '').localeCompare(String(a.createdAt || ''));
    });
    if (filter) list = list.filter(function (m) { return m.kind === filter; });
    if (!list.length) {
      box.appendChild(el('div', 'board-empty', '暂无 Memory。失败也会留痕（失败即产出）。'));
      return;
    }
    list.forEach(function (memory) {
      var card = el('article', 'memory-card memory-' + (KIND_TONE[memory.kind] || 'muted'));
      var head = el('div', 'memory-head');
      head.appendChild(chip(KIND_TEXT[memory.kind] || memory.kind, KIND_TONE[memory.kind] || 'muted'));
      head.appendChild(chip(memory.source || 'agent', 'muted'));
      card.appendChild(head);
      card.appendChild(el('div', 'memory-summary', memory.summary || ''));
      if (memory.body) {
        var pre = el('pre', 'memory-body', String(memory.body).slice(0, 1200));
        card.appendChild(pre);
      }
      card.addEventListener('click', function () { card.classList.toggle('open'); });
      box.appendChild(card);
    });
  }

  function renderDecisions() {
    var box = refs.decisions;
    if (!box) return;
    clear(box);
    if (!board.decisions.length) {
      box.appendChild(el('div', 'board-empty', '尚未启动。Dispatcher 的每次决策都会出现在这里（控制面可审计）。'));
      return;
    }
    board.decisions.slice(-40).forEach(function (item) {
      var row = el('div', 'decision-row');
      row.appendChild(el('span', 'mono', fmtTime(item.ts)));
      row.appendChild(el('span', 'decision-action', item.action || ''));
      row.appendChild(el('span', 'decision-reason', item.reason || ''));
      box.appendChild(row);
    });
  }

  function renderAll() {
    renderTop();
    renderIdeas();
    renderSolvers();
    renderMemories();
    renderDecisions();
  }

  /* ------------------------------ 数据 ------------------------------ */
  async function refresh() {
    if (!board.challengeId || board.refreshing) return;
    board.refreshing = true;
    try {
      var snapshot = await CTFA.api.request('/api/challenges/' + encodeURIComponent(board.challengeId) + '/state');
      applySnapshot(snapshot);
    } catch (err) {
      notify('加载挑战快照失败：' + (err && err.message ? err.message : err), 'danger');
    } finally {
      board.refreshing = false;
    }
  }

  /** 定时自动刷新：控制台可见且有选中题目时，每 AUTO_REFRESH_MS 拉一次快照。 */
  function startAutoRefresh() {
    if (board.timer) return;
    board.timer = setInterval(function () {
      if (document.hidden || !board.challengeId || board.refreshing) return;
      if (refs.view && refs.view.hidden) return; // 控制台不在前台时不做无谓请求
      if (board.terminated) return;              // 已归档的题目状态不会再变化
      refresh();
    }, AUTO_REFRESH_MS);
  }

  function applySnapshot(snapshot) {
    board.snapshot = snapshot || {};
    board.ideas = (snapshot && snapshot.ideas) || [];
    board.memories = (snapshot && snapshot.memories) || [];
    board.solverRuns = (snapshot && snapshot.solverRuns) || [];
    // 会话已归档 → 停止自动刷新（状态不会再变化）；再次启动时由 start() 复位
    var liveState = ((snapshot && snapshot.live) || {}).state || ((snapshot && snapshot.session) || {}).status || '';
    if (liveState === 'terminated' || liveState === 'aborted') {
      board.terminated = true;
      board.stopping = false;
      board.streaming = false;
    }
    (board.solverRuns || []).forEach(function (run) {
      var id = run.id;
      // 终态优先展示结束原因（outcome）——例如会话因 TERMINATE_SUCCESS 归档时
      // 在跑的 Solver 会归一为 cancelled，但 outcome 说明了真实原因
      var label = (run.concluded ? run.outcome || run.status : run.status) || 'running';
      if (!board.solvers[id]) {
        board.solvers[id] = { taskType: run.taskType, status: label, ideaId: run.ideaId, events: [] };
      } else {
        board.solvers[id].status = label;
      }
    });
    renderAll();
  }

  /* ------------------------------ SSE ------------------------------ */
  function onEvent(type, data) {
    var payload = data || {};
    var ts = payload.ts || Date.now();
    switch (type) {
      case 'dispatcher_decision':
        board.decisions.push({
          action: payload.action,
          reason: payload.reason || payload.waitReason || '',
          tick: payload.tick,
          ts: ts
        });
        renderDecisions();
        renderTop();
        break;
      case 'solver_spawned':
        board.solvers[payload.solverRunId] = board.solvers[payload.solverRunId] || { events: [] };
        board.solvers[payload.solverRunId].taskType = payload.taskType;
        board.solvers[payload.solverRunId].ideaId = payload.ideaId;
        board.solvers[payload.solverRunId].status = 'running';
        pushSolverEvent(payload.solverRunId, '派发 ' + (payload.taskType || ''));
        break;
      case 'solver_concluded':
        board.solvers[payload.solverRunId] = board.solvers[payload.solverRunId] || { events: [] };
        board.solvers[payload.solverRunId].status = payload.outcome || 'concluded';
        pushSolverEvent(payload.solverRunId, '收尾：' + (payload.outcome || '') + (payload.concluded ? '（已产出）' : ''));
        break;
      case 'progress':
        if (payload.solverRunId && payload.stage) pushSolverEvent(payload.solverRunId, payload.message || payload.stage);
        break;
      case 'idea_transition':
        (board.transitions[payload.ideaId] = board.transitions[payload.ideaId] || []).push({
          from: payload.from, to: payload.to, by: payload.by, ts: ts
        });
        refresh();
        break;
      case 'efficiency_reminder':
        board.reminders.push({ rule: payload.rule, reason: payload.reason, ts: ts });
        pushSolverEvent(payload.solverRunId, '⚑ Observer/' + (payload.rule || '') + '：' + (payload.reason || ''));
        renderTop();
        break;
      case 'hint_added':
        notify('已注入提示：' + (payload.summary || ''), 'ok');
        refresh();
        break;
      case 'challenge_terminated':
        board.terminated = true;
        board.stopping = false;
        notify('挑战结束：' + (payload.verdict || '') + '（' + (payload.reason || '') + '）',
          payload.verdict === 'TERMINATE_SUCCESS' ? 'ok' : 'warn');
        refresh();
        break;
      case 'error':
        notify('运行错误：' + (payload.message || ''), 'danger');
        break;
      case 'done':
        board.streaming = false;
        board.stopping = false;
        board.terminated = true;
        updateButtons();
        refresh();
        break;
      default:
        break;
    }
  }

  function pushSolverEvent(solverRunId, text) {
    if (!solverRunId) return;
    var entry = board.solvers[solverRunId] = board.solvers[solverRunId] || { events: [] };
    entry.events = entry.events || [];
    entry.events.push({ text: text, ts: Date.now() });
    renderSolvers();
  }

  async function start() {
    if (board.streaming) return;
    if (!board.challengeId) {
      notify('尚未选择题目：请在「题目」页选择，或使用聊天区顶部的题目下拉框。', 'warn');
      return;
    }
    board.terminated = false;
    board.stopping = false;
    var body = {};
    if (refs.interval && Number(refs.interval.value) > 0) body.interval_seconds = Number(refs.interval.value);
    if (refs.budget && Number(refs.budget.value) > 0) body.time_budget_minutes = Number(refs.budget.value);
    board.streaming = true;
    updateButtons();
    var handle = CTFA.api.createStream(
      '/api/challenges/' + encodeURIComponent(board.challengeId) + '/run',
      body,
      {
        onEvent: onEvent,
        onError: function (err) { notify('SSE 错误：' + (err && err.message ? err.message : err), 'danger'); },
        onClose: function () { board.streaming = false; board.stopping = false; updateButtons(); refresh(); }
      }
    );
    board.controller = handle.controller;
    try {
      await handle.promise;
    } catch (err) {
      notify('启动失败：' + (err && err.message ? err.message : err), 'danger');
    }
  }

  async function stop() {
    if (!board.challengeId || board.stopping) return;
    board.stopping = true;
    updateButtons();
    try {
      await CTFA.api.request('/api/challenges/' + encodeURIComponent(board.challengeId) + '/stop', {
        method: 'POST'
      });
      notify('已请求停止：Dispatcher 会立即终止各 Solver 并归档（数秒内完成，状态自动刷新）', 'warn');
      refresh();
    } catch (err) {
      board.stopping = false;   // 停止请求失败（如会话已归档）→ 恢复按钮并刷新真实状态
      notify('停止失败：' + ((err && err.message) || err) + '，已刷新当前状态', 'danger');
      refresh();
      updateButtons();
    }
  }

  async function sendHint() {
    var text = refs.hintInput ? refs.hintInput.value.trim() : '';
    if (!board.challengeId || !text) return;
    try {
      await CTFA.api.request('/api/challenges/' + encodeURIComponent(board.challengeId) + '/hint', {
        method: 'POST',
        body: JSON.stringify({ text: text, author: 'human' })
      });
      refs.hintInput.value = '';
      notify('提示已注入（hint Memory，优先级最高）', 'ok');
      refresh();
    } catch (err) {
      notify('提示注入失败：' + (err && err.message ? err.message : err), 'danger');
    }
  }

  async function submitFlag() {
    var value = refs.flagInput ? refs.flagInput.value.trim() : '';
    if (!board.challengeId || !value) return;
    try {
      await CTFA.api.request('/api/challenges/' + encodeURIComponent(board.challengeId) + '/flag', {
        method: 'POST',
        body: JSON.stringify({ flag: value })
      });
      refs.flagInput.value = '';
      notify('flag 已提交', 'ok');
      refresh();
    } catch (err) {
      notify('flag 提交失败：' + (err && err.message ? err.message : err), 'danger');
    }
  }

  function updateButtons() {
    if (refs.btnStart) refs.btnStart.disabled = board.streaming || board.stopping || !board.challengeId;
    if (refs.btnStop) refs.btnStop.disabled = !board.streaming || board.stopping;
    if (refs.btnHint) refs.btnHint.disabled = !board.challengeId;
    if (refs.btnFlag) refs.btnFlag.disabled = !board.challengeId;
    if (refs.status) {
      refs.status.textContent = board.stopping
        ? '正在停止…（等待各 Solver 收尾归档）'
        : board.streaming
          ? '运行中（SSE 订阅中）'
          : (board.challengeId ? '待启动 / 已结束' : '未选择题目');
    }
  }

  function notify(message, tone) {
    if (CTFA.timeline && typeof CTFA.timeline.notice === 'function') {
      CTFA.timeline.notice(message, tone);
      return;
    }
    if (global.console) global.console.log('[challenge]', message);
  }

  /* ------------------------------ 生命周期 ------------------------------ */
  function open(challengeId) {
    board.challengeId = challengeId || '';
    board.solvers = {};
    board.transitions = {};
    board.decisions = [];
    board.reminders = [];
    board.stopping = false;
    board.terminated = false;
    updateButtons();
    refresh();
  }

  function init(options) {
    options = options || {};
    refs = {
      view: options.view,
      top: null,
      ideas: null,
      solvers: null,
      memories: null,
      decisions: null,
      status: null
    };
    if (!refs.view) return;
    build();
    startAutoRefresh();
    if (options.challengeId) open(options.challengeId);
  }

  function build() {
    var view = refs.view;
    clear(view);

    var head = el('div', 'board-head');
    head.appendChild(el('h2', 'view-title', '挑战控制台'));
    head.appendChild(el('span', 'view-hint', 'POST /api/challenges/{id}/run · 调度完全由 Dispatcher 负责'));
    var top = el('div', 'board-top');
    head.appendChild(top);
    refs.top = top;

    var controls = el('div', 'board-controls');
    var startBtn = el('button', 'btn primary', '▶ 启动自主解题');
    startBtn.id = 'challenge-start';
    // 注意：不能用 `var start` 命名——它会遮蔽外层的 start() 函数，
    // `addEventListener('click', start)` 会把按钮元素自己当监听器注册而抛 TypeError，
    // 导致整个控制行（启动/停止/刷新/hint/flag）渲染中断、启动永远无响应。
    startBtn.addEventListener('click', start);
    var stopBtn = el('button', 'btn danger', '■ 停止');
    stopBtn.id = 'challenge-stop';
    stopBtn.addEventListener('click', stop);
    var refreshBtn = el('button', 'btn', '⟳ 刷新');
    refreshBtn.addEventListener('click', refresh);
    controls.appendChild(startBtn);
    controls.appendChild(stopBtn);
    controls.appendChild(refreshBtn);

    var interval = el('input', 'search board-num');
    interval.type = 'number';
    interval.min = '0';
    interval.step = '0.5';
    interval.placeholder = '轮询间隔(s)';
    interval.value = '1';
    var budget = el('input', 'search board-num');
    budget.type = 'number';
    budget.min = '0';
    budget.placeholder = '时间预算(min)';
    controls.appendChild(interval);
    controls.appendChild(budget);
    refs.interval = interval;
    refs.budget = budget;

    var hintInput = el('input', 'search board-input');
    hintInput.type = 'text';
    hintInput.placeholder = '人工提示（Hint 原语：直接注入所有后续 Solver）';
    var hintBtn = el('button', 'btn', '注入提示');
    hintBtn.addEventListener('click', sendHint);
    var flagInput = el('input', 'search board-input');
    flagInput.type = 'text';
    flagInput.placeholder = '提交 flag（如 flag{...}）';
    var flagBtn = el('button', 'btn', '提交 flag');
    flagBtn.addEventListener('click', submitFlag);
    controls.appendChild(hintInput);
    controls.appendChild(hintBtn);
    controls.appendChild(flagInput);
    controls.appendChild(flagBtn);
    refs.hintInput = hintInput;
    refs.flagInput = flagInput;
    refs.btnStart = startBtn;
    refs.btnStop = stopBtn;
    refs.btnHint = hintBtn;
    refs.btnFlag = flagBtn;

    var filter = el('select', 'board-filter');
    [['', '全部 Memory'], ['hint', '人类提示'], ['failure', '失败'], ['fact', '事实'], ['evidence', '证据'], ['note', '备注']].forEach(function (pair) {
      var opt = el('option', null, pair[1]);
      opt.value = pair[0];
      filter.appendChild(opt);
    });
    filter.addEventListener('change', renderMemories);
    refs.memoryFilter = filter;
    controls.appendChild(filter);

    var status = el('span', 'board-status', '待启动 / 已结束');
    controls.appendChild(status);
    refs.status = status;
    head.appendChild(controls);
    view.appendChild(head);

    var grid = el('div', 'board-grid');
    var colIdeas = el('section', 'board-col');
    colIdeas.appendChild(el('h3', 'board-col-title', 'Ideas（方向假设）'));
    var ideas = el('div', 'board-scroll');
    colIdeas.appendChild(ideas);
    var colSolvers = el('section', 'board-col');
    colSolvers.appendChild(el('h3', 'board-col-title', 'Solver 时间线'));
    var solvers = el('div', 'board-scroll');
    colSolvers.appendChild(solvers);
    var colMemory = el('section', 'board-col');
    colMemory.appendChild(el('h3', 'board-col-title', 'Memory 流'));
    var memories = el('div', 'board-scroll');
    colMemory.appendChild(memories);
    grid.appendChild(colIdeas);
    grid.appendChild(colSolvers);
    grid.appendChild(colMemory);
    view.appendChild(grid);

    var decisionsBox = el('section', 'board-decisions');
    decisionsBox.appendChild(el('h3', 'board-col-title', 'Dispatcher 决策审计'));
    var decisions = el('div', 'decision-list');
    decisionsBox.appendChild(decisions);
    view.appendChild(decisionsBox);

    refs.ideas = ideas;
    refs.solvers = solvers;
    refs.memories = memories;
    refs.decisions = decisions;

    renderAll();
    updateButtons();
  }

  CTFA.challenge = {
    init: init,
    open: open,
    refresh: refresh,
    start: start,
    stop: stop,
    board: board
  };
})(typeof window !== 'undefined' ? window : this);