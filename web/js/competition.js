/* ============================================================================
 * competition.js — 竞赛自动化 Agent 指挥台（N3 完整版）
 * ----------------------------------------------------------------------------
 * 四栏布局：
 *   左 220px  竞赛项目管理（新建/选择/删除）
 *   中 flex   Tab 三页：文档解析 / 题目池 / 策略配置
 *   右 340px  竞赛全局黑板（可折叠）
 *   右 360px  实时日志 + 提交记录（可折叠）
 *
 * 严格 UI 校验：
 *   1) 存在未启用能力时禁用「启动自动化」并给出明确提示；
 *   2) 凭证只显示引用名，任何明文 key 一律打码；
 *   3) view_hint 开关默认关闭，开启需确认扣分风险；
 *   4) 竞赛状态 idle/syncing/running/paused/error 五色标签；
 *   5) 日志来自 SSE，逐条带阶段与决策理由。
 * ========================================================================== */
(function (global) {
  'use strict';

  var CTFA = (global.CTFA = global.CTFA || {});
  var doc = global.document;

  /** 必需能力：缺任一则不允许启动自动化 */
  var REQUIRED_CAPS = ['list_exercises', 'build_env', 'submit_flag'];
  var CAP_LABEL = {
    get_rules: '拉取规则', get_scoreboard: '得分排名', submit_flag: '提交 flag',
    list_exercises: '题目列表', get_exercise: '题目详情', build_env: '启动环境',
    recover_env: '回收环境', get_notices: '公告', view_hint: '查看提示'
  };
  var KIND_LABEL = {
    rule: '规则', protocol: '协议', constraint: '约束', credential: '凭证',
    feedback: '反馈', notice: '公告', strategy: '策略'
  };
  var BOARD_KINDS = ['rule', 'protocol', 'constraint', 'feedback'];
  var STATUS_LABEL = {
    idle: '空闲', syncing: '同步中', running: '运行中', paused: '已暂停', error: '异常'
  };
  var PHASE_TONE = {
    SYNC: 'sync', SCHEDULE: 'schedule', PREPARE_ENV: 'env', DISPATCH: 'dispatch',
    SUBMIT: 'submit', FEEDBACK: 'feedback', ERROR: 'error', SYSTEM: 'system'
  };

  var state = {
    competitions: [], currentId: null,
    detail: null, status: null, exercises: [], board: null, caps: [], subs: null,
    tab: 'ingest', docText: '', docUrl: '', preview: null,
    logs: [], boardCollapsed: false, logsCollapsed: false,
    strategyDraft: null, editingCap: null, submitTarget: null,
    busy: false, hint: null, parsing: false,
    // P2：可拖拽列宽（localStorage 持久化）
    widths: { projects: 220, board: 340, logs: 360 },
    // P2：凭证输入框展开状态（凭证只存内存，不落库）
    credOpen: false
  };
  var WIDTH_KEY = 'ctfa.compWidths';
  var WIDTH_LIMITS = { projects: [160, 380], board: [240, 620], logs: [260, 680] };
  var es = null;
  var logger = global.console;

  // ---------------------------------------------------------------- 基础工具
  function el(tag, cls, text) {
    var node = doc.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

  /** 凭证脱敏：只保留引用形式；出现疑似密钥立即打码 */
  function maskSecret(value) {
    var text = String(value || '');
    if (!text) return '（未配置）';
    if (/^(config|env):/.test(text)) return text;
    if (/ak_|sk-|token|key/i.test(text) || text.length > 24) {
      return text.slice(0, 4) + '****' + text.slice(-2) + '（已脱敏）';
    }
    return text;
  }

  function toast(message, tone, sticky) {
    state.hint = { message: String(message), tone: tone || 'info' };
    paintHint();
    if (sticky) return;  // 常驻提示（如"解析中"），由后续 toast 覆盖
    global.setTimeout(function () {
      if (state.hint && state.hint.message === String(message)) {
        state.hint = null;
        paintHint();
      }
    }, 5200);
  }

  function clamp(v, min, max) { return Math.max(min, Math.min(max, v)); }

  function loadWidths() {
    try {
      var raw = global.localStorage.getItem(WIDTH_KEY);
      if (!raw) return;
      var parsed = JSON.parse(raw);
      ['projects', 'board', 'logs'].forEach(function (k) {
        var lim = WIDTH_LIMITS[k];
        if (typeof parsed[k] === 'number') state.widths[k] = clamp(parsed[k], lim[0], lim[1]);
      });
    } catch (e) { /* ignore */ }
  }

  function saveWidths() {
    try { global.localStorage.setItem(WIDTH_KEY, JSON.stringify(state.widths)); } catch (e) { /* ignore */ }
  }

  function applyWidths() {
    var shell = doc.querySelector('.comp-shell');
    if (!shell) return;
    shell.style.setProperty('--w-projects', state.widths.projects + 'px');
    shell.style.setProperty('--w-board', state.widths.board + 'px');
    shell.style.setProperty('--w-logs', state.widths.logs + 'px');
  }

  /** 相邻栏之间的拖拽分隔条：拖动改宽度，双击恢复默认。 */
  function splitter(which) {
    var bar = el('div', 'comp-splitter comp-splitter--' + which);
    bar.title = '拖动调整宽度，双击恢复默认';
    bar.addEventListener('mousedown', function (evt) {
      evt.preventDefault();
      var startX = evt.clientX;
      var startW = state.widths[which];
      doc.body.classList.add('comp-resizing');
      function onMove(e) {
        var dx = e.clientX - startX;
        var lim = WIDTH_LIMITS[which];
        // 左侧栏向右拖变宽；右侧栏向左拖变宽（dx 取反）
        var next = which === 'projects' ? startW + dx : startW - dx;
        // 容器感知钳制：始终给中栏留 240px，否则固定列会把中栏压没（布局错乱）
        var shell = doc.querySelector('.comp-shell');
        var avail = shell ? shell.clientWidth : 1440;
        var others = 0;
        ['projects', 'board', 'logs'].forEach(function (k) {
          if (k !== which) others += state.widths[k];
        });
        var ceiling = Math.max(lim[0], Math.min(lim[1], avail - others - 18 - 240));
        state.widths[which] = clamp(next, lim[0], ceiling);
        applyWidths();
      }
      function onUp() {
        doc.removeEventListener('mousemove', onMove);
        doc.removeEventListener('mouseup', onUp);
        doc.body.classList.remove('comp-resizing');
        saveWidths();
      }
      doc.addEventListener('mousemove', onMove);
      doc.addEventListener('mouseup', onUp);
    });
    bar.addEventListener('dblclick', function () {
      state.widths[which] = which === 'projects' ? 220 : (which === 'board' ? 340 : 360);
      applyWidths();
      saveWidths();
      toast('已恢复默认宽度', 'info');
    });
    return bar;
  }

  function fmtTime(iso) {
    if (!iso) return '-';
    try {
      var d = new Date(iso);
      return d.toLocaleTimeString('zh-CN', { hour12: false });
    } catch (e) { return String(iso); }
  }

  async function req(path, options, timeoutMs) {
    var opts = Object.assign({ headers: {} }, options || {});
    var controller = null;
    if (timeoutMs) {
      controller = new global.AbortController();
      opts.signal = controller.signal;
    }
    opts.headers = Object.assign({ Accept: 'application/json' }, opts.headers);
    if (opts.body && typeof opts.body !== 'string') {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.body);
    }
    var timer = null;
    if (controller) {
      timer = global.setTimeout(function () { controller.abort(); }, timeoutMs);
    }
    var res;
    try {
      res = await fetch(path, opts);
    } catch (err) {
      if (timer) global.clearTimeout(timer);
      if (err && err.name === 'AbortError') {
        throw new Error('请求超时（' + Math.round(timeoutMs / 1000) + 's）：AI 抽取较慢，请重试或补充文档结构');
      }
      throw err;
    }
    if (timer) global.clearTimeout(timer);
    var text = await res.text();
    var data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = { detail: text }; }
    if (!res.ok) {
      var msg = (data && (data.detail || data.message)) || ('HTTP ' + res.status);
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    return data;
  }

  // ---------------------------------------------------------------- 数据
  async function loadList() {
    var data = await req('/api/competitions');
    state.competitions = (data && data.competitions) || [];
    if (!state.currentId && state.competitions.length) {
      var saved = null;
      try { saved = global.localStorage.getItem('ctfa.compCurrent'); } catch (e) { /* ignore */ }
      var hit = state.competitions.filter(function (c) { return c.id === saved; })[0];
      state.currentId = hit ? hit.id : state.competitions[0].id;
    }
    if (state.currentId && !state.competitions.some(function (c) { return c.id === state.currentId; })) {
      state.currentId = state.competitions.length ? state.competitions[0].id : null;
    }
  }

  async function loadAll() {
    try {
      await loadList();
      if (!state.currentId) { render(); return; }
      var base = '/api/competitions/' + encodeURIComponent(state.currentId);
      var r = await Promise.all([
        req(base).catch(function () { return null; }),
        req(base + '/status').catch(function () { return null; }),
        req(base + '/exercises').catch(function () { return { exercises: [] }; }),
        req(base + '/board').catch(function () { return null; }),
        req(base + '/capabilities').catch(function () { return { capabilities: [] }; }),
        req(base + '/submissions').catch(function () { return null; })
      ]);
      state.detail = r[0];
      state.status = r[1];
      state.exercises = (r[2] && r[2].exercises) || [];
      state.board = r[3];
      state.caps = (r[4] && r[4].capabilities) || [];
      state.subs = r[5];
      state.strategyDraft = Object.assign({}, strategyDefaults(),
        (state.detail && state.detail.competition && state.detail.competition.strategy) || {});
      try { global.localStorage.setItem('ctfa.compCurrent', state.currentId); } catch (e) { /* ignore */ }
      subscribeLogs();
      render();
    } catch (err) {
      toast('加载失败：' + (err && err.message ? err.message : err), 'error');
      render();
    }
  }

  function strategyDefaults() {
    return {
      w_score: 0.35, w_difficulty: 0.25, w_fresh: 0.20, w_level: 0.15,
      max_attempts: 5, wrong_before_cooldown: 3, cooldown_minutes: 10,
      rateLimitPerSec: 1, maxInstances: 3, hintEnabled: false, autoSolve: false, flagRegex: ''
    };
  }

  // ---------------------------------------------------------------- SSE 日志
  function subscribeLogs() {
    if (es) { es.close(); es = null; }
    if (!state.currentId) return;
    try {
      es = new global.EventSource('/api/competitions/' + encodeURIComponent(state.currentId) + '/logs');
      es.onmessage = function (evt) {
        try {
          var entry = JSON.parse(evt.data);
          state.logs.push(entry);
          if (state.logs.length > 300) state.logs.shift();
          paintLogs();
        } catch (e) { /* 忽略非 JSON 帧 */ }
      };
      es.onerror = function () { /* EventSource 自动重连 */ };
    } catch (e) {
      toast('日志流连接失败：' + e.message, 'error');
    }
  }

  // ---------------------------------------------------------------- 校验
  function missingCaps() {
    return REQUIRED_CAPS.filter(function (name) {
      var cap = state.caps.filter(function (c) { return c.name === name; })[0];
      return !cap || !cap.enabled;
    });
  }
  function compStatus() {
    return (state.detail && state.detail.competition && state.detail.competition.status) || 'idle';
  }

  // ---------------------------------------------------------------- 渲染：外壳
  function render() {
    var root = doc.getElementById('competition-view');
    if (!root) return;
    try {
      renderInner(root);
    } catch (err) {
      logger.error("指挥台渲染失败", err); global.console.error("RENDER_FAIL:", err && err.stack || err);
      clear(root);
      var box = el('div', 'comp-shell');
      var col = el('div', 'comp-col');
      col.appendChild(el('h3', 'comp-col-title', '页面渲染出错'));
      col.appendChild(el('div', 'comp-empty', String((err && err.message) || err)));
      col.appendChild(el('div', 'comp-empty', '请刷新页面重试；若持续出现请反馈该错误信息。'));
      box.appendChild(col);
      root.appendChild(box);
    }
  }

  function renderInner(root) {
    clear(root);
    var shell = el('div', 'comp-shell' +
      (state.boardCollapsed ? ' board-folded' : '') +
      (state.logsCollapsed ? ' logs-folded' : ''));

    shell.appendChild(projectSidebar());
    shell.appendChild(splitter('projects'));
    if (!state.currentId) {
      var empty = el('div', 'comp-main comp-main--empty');
      empty.appendChild(el('div', 'comp-empty-big', '尚未选择竞赛项目'));
      empty.appendChild(el('div', 'comp-empty-sub',
        '左侧新建一个竞赛（只需名称）→ 上传官方文档 → 解析后自动生成接入参数与能力清单'));
      shell.appendChild(empty);
    } else {
      shell.appendChild(mainArea());
      shell.appendChild(splitter('board'));
      shell.appendChild(boardPanel());
      shell.appendChild(splitter('logs'));
      shell.appendChild(logPanel());
    }
    root.appendChild(shell);
    applyWidths();
    paintHint();
    paintLogs();
  }

  function paintHint() {
    var host = doc.getElementById('comp-hint-host');
    if (!host) return;
    clear(host);
    if (!state.hint) return;
    var bar = el('div', 'comp-hint comp-hint--' + (state.hint.tone || 'info'));
    bar.appendChild(el('span', '', state.hint.message));
    var close = el('button', 'comp-hint-close', '×');
    close.type = 'button';
    close.addEventListener('click', function () { state.hint = null; paintHint(); });
    bar.appendChild(close);
    host.appendChild(bar);
  }

  // ---------------------------------------------------------------- 左：项目侧栏
  function projectSidebar() {
    var aside = el('aside', 'comp-projects');
    var head = el('div', 'comp-projects-head');
    head.appendChild(el('span', 'comp-projects-title', '竞赛项目'));
    var add = el('button', 'comp-icon-btn', '＋');
    add.type = 'button';
    add.title = '新建竞赛';
    add.addEventListener('click', function () { toggleNewForm(); });
    head.appendChild(add);
    aside.appendChild(head);

    var list = el('div', 'comp-project-list');
    if (!state.competitions.length) {
      list.appendChild(el('div', 'comp-empty', '暂无竞赛，点击 ＋ 新建'));
    }
    state.competitions.forEach(function (c) {
      var item = el('div', 'comp-project' + (c.id === state.currentId ? ' comp-project--on' : ''));
      var dot = el('span', 'comp-status-dot comp-status-dot--' + c.status);
      item.appendChild(dot);
      var body = el('div', 'comp-project-body');
      body.appendChild(el('div', 'comp-project-name', c.name));
      body.appendChild(el('div', 'comp-project-meta', c.platformKind));
      item.appendChild(body);
      var del = el('button', 'comp-icon-btn comp-project-del', '×');
      del.type = 'button';
      del.title = '删除竞赛';
      del.addEventListener('click', async function (evt) {
        evt.stopPropagation();
        if (!global.confirm('删除竞赛「' + c.name + '」？黑板、题目池与提交记录都会一并删除。')) return;
        try {
          await req('/api/competitions/' + encodeURIComponent(c.id), { method: 'DELETE' });
          if (state.currentId === c.id) state.currentId = null;
          toast('已删除竞赛：' + c.name, 'success');
          await loadAll();
        } catch (err) { toast('删除失败：' + err.message, 'error'); }
      });
      item.appendChild(del);
      item.addEventListener('click', function () {
        if (state.currentId === c.id) return;
        state.currentId = c.id;
        state.logs = [];
        state.preview = null;
        state.tab = 'ingest';
        loadAll();
      });
      list.appendChild(item);
    });
    aside.appendChild(list);
    aside.appendChild(newProjectForm());
    return aside;
  }

  function toggleNewForm() {
    var form = doc.getElementById('comp-new-form');
    if (form) form.hidden = !form.hidden;
  }

  function newProjectForm() {
    var box = el('div', 'comp-new-form');
    box.id = 'comp-new-form';
    box.hidden = true;
    box.appendChild(el('div', 'comp-card-sub',
      '只需填名称即可创建；Base URL / 平台类型 / 鉴权 Header 会在你上传官方文档、AI 解析后自动生成。'));

    var nameField = el('label', 'comp-field');
    nameField.appendChild(el('span', '', '竞赛名称'));
    var nameInput = el('input', 'comp-input');
    nameInput.placeholder = '例如：西湖论剑 2026';
    nameField.appendChild(nameInput);
    box.appendChild(nameField);

    var refField = el('label', 'comp-field');
    refField.appendChild(el('span', '', '凭证引用（可选，稍后也可提供）'));
    var refInput = el('input', 'comp-input');
    refInput.placeholder = 'config:competition.access_key / env:CTF_ACCESS_KEY';
    refField.appendChild(refInput);
    box.appendChild(refField);

    var create = el('button', 'comp-btn comp-btn--primary', '创建');
    create.type = 'button';
    create.addEventListener('click', async function () {
      if (!nameInput.value.trim()) { toast('请填写竞赛名称', 'warn'); return; }
      try {
        var created = await req('/api/competitions', {
          method: 'POST',
          body: {
            name: nameInput.value.trim(),
            auth_ref: refInput.value.trim()
          }
        });
        state.currentId = created.id;
        state.tab = 'ingest';
        toast('已创建竞赛「' + created.name + '」：下一步请上传官方文档', 'success');
        await loadAll();
      } catch (err) { toast('创建失败：' + err.message, 'error'); }
    });
    box.appendChild(create);
    return box;
  }

  // ---------------------------------------------------------------- 中：主区
  function mainArea() {
    var main = el('main', 'comp-main');
    main.appendChild(el('div', 'comp-hint-host')).id = 'comp-hint-host';
    main.appendChild(mainHead());

    var tabs = el('div', 'comp-tabs');
    [['ingest', '文档解析'], ['pool', '题目池'], ['strategy', '策略配置']].forEach(function (pair) {
      var btn = el('button', 'comp-tab' + (state.tab === pair[0] ? ' on' : ''), pair[1]);
      btn.type = 'button';
      btn.addEventListener('click', function () {
        state.tab = pair[0];
        state.editingCap = null;
        render();
      });
      tabs.appendChild(btn);
    });
    main.appendChild(tabs);

    var pane = el('div', 'comp-pane');
    if (state.tab === 'ingest') pane.appendChild(ingestPane());
    else if (state.tab === 'pool') pane.appendChild(poolPane());
    else pane.appendChild(strategyPane());
    main.appendChild(pane);
    return main;
  }

  function mainHead() {
    var comp = state.detail && state.detail.competition;
    var head = el('div', 'comp-main-head');
    var title = el('div', 'comp-main-title');
    title.appendChild(el('span', 'comp-name', comp ? comp.name : ''));
    var st = compStatus();
    title.appendChild(el('span', 'comp-status comp-status--' + st, STATUS_LABEL[st] || st));
    if (comp) {
      title.appendChild(el('span', 'comp-meta', comp.platformKind + ' · ' + comp.baseUrl));
      title.appendChild(el('span', 'comp-meta comp-meta--masked',
        '凭证 ' + maskSecret(comp.authRef) + '（明文不落库、不上屏）'));
    }
    head.appendChild(title);

    var actions = el('div', 'comp-main-actions');
    var protocolCount = (state.board && state.board.counts && state.board.counts.protocol) || 0;
    var running = st === 'running';
    [['test', '连接测试'], ['sync', '同步题目'], ['export', '导出复盘']].forEach(function (pair) {
      var b = el('button', 'comp-btn', pair[1]);
      b.type = 'button';
      b.disabled = state.busy;
      b.addEventListener('click', function () { runAction(pair[0]); });
      actions.appendChild(b);
    });
    var toggle = el('button', 'comp-btn ' + (running ? '' : 'comp-btn--primary'),
      running ? '暂停自动化' : '启动自动化');
    toggle.type = 'button';
    toggle.disabled = state.busy || (!running && protocolCount === 0);
    toggle.title = (!running && protocolCount === 0)
      ? '黑板中还没有接口事实：请先在「文档解析」页把官方文档存入黑板'
      : '';
    toggle.addEventListener('click', function () { runAction(running ? 'pause' : 'start'); });
    actions.appendChild(toggle);
    head.appendChild(actions);

    // P2：解析出 auth_header 但还没有凭证 → 提示提供（只存内存）
    var comp0 = state.detail && state.detail.competition;
    var hasCred = ((state.board && state.board.facts) || []).some(function (f) {
      return f.kind === 'credential' && String(f.summary).indexOf('已提供') >= 0;
    });
    if (comp0 && comp0.authHeader && !hasCred) {
      var credBox = el('div', 'comp-cred-hint');
      credBox.appendChild(el('span', '',
        '🔑 文档已解析出鉴权 Header「' + comp0.authHeader + '」，请提供凭证（仅存进程内存，不落库、不上屏）：'));
      var credInput = el('input', 'comp-input comp-input--grow');
      credInput.type = 'password';
      credInput.placeholder = '粘贴 AccessKey / Token';
      credBox.appendChild(credInput);
      var credBtn = el('button', 'comp-btn comp-btn--primary', '保存凭证');
      credBtn.type = 'button';
      credBtn.addEventListener('click', async function () {
        var value = credInput.value.trim();
        if (!value) { toast('凭证不能为空', 'warn'); return; }
        try {
          var base0 = '/api/competitions/' + encodeURIComponent(state.currentId);
          var res = await req(base0 + '/credential', { method: 'POST', body: { value: value } });
          toast('凭证已保存（内存态，脱敏显示 ' + res.masked + '）', 'success');
          credInput.value = '';
          await loadAll();
        } catch (err) { toast('保存凭证失败：' + err.message, 'error'); }
      });
      credBox.appendChild(credBtn);
      head.appendChild(credBox);
    }

    if (!running && protocolCount === 0) {
      head.appendChild(el('div', 'comp-block-hint',
        '⛔ 启动已禁用：黑板中还没有接口/规则事实。请先在「文档解析」页上传官方文档并落库，' +
        '解析出的接口会自动进入黑板。'));
    }
    return head;
  }

  async function runAction(name) {
    if (!state.currentId) return;
    var base = '/api/competitions/' + encodeURIComponent(state.currentId);
    if (name === 'export') {
      global.open(base + '/export?format=timeline', '_blank');
      return;
    }
    state.busy = true;
    try {
      if (name === 'test') {
        toast('连接测试中（只读能力 dry-run）…', 'info');
        var t = await req(base + '/test', { method: 'POST' });
        var tks = Object.keys(t.results || {});
        var okCount = tks.filter(function (k) { return t.results[k].ok; }).length;
        var skipNote = (t.skipped && t.skipped.length)
          ? '（跳过 ' + t.skipped.length + ' 个写接口/带参接口：连接测试不触发领题与提交）' : '';
        toast('连接测试完成：' + okCount + '/' + tks.length + ' 项通过' + skipNote,
              okCount ? 'success' : 'error');
      } else if (name === 'sync') {
        var s = await req(base + '/sync', { method: 'POST' });
        var errNote = (s.errors && s.errors.length)
          ? '；' + s.errors.length + ' 项失败（详见引擎日志）' : '';
        toast('同步完成：题目 ' + s.exercises + ' 道，公告 ' + s.notices + ' 条' + errNote,
              s.exercises ? 'success' : 'warn');
      } else if (name === 'start') {
        await req(base + '/start', { method: 'POST' });
        toast('自动化已启动：策略引擎开始调度', 'success');
      } else if (name === 'pause') {
        await req(base + '/pause', { method: 'POST' });
        toast('自动化已暂停', 'warn');
      }
    } catch (err) {
      toast(name + ' 失败：' + err.message, 'error');
    } finally {
      state.busy = false;
      await loadAll();
    }
  }

  // ---------------------------------------------------------------- Tab1 文档解析
  function ingestPane() {
    var pane = el('div', 'comp-pane-inner');
    var card = el('section', 'comp-card');
    card.appendChild(el('h3', 'comp-card-title', '官方文档输入'));
    card.appendChild(el('div', 'comp-card-sub',
      '支持粘贴 Markdown / 上传 .md 文件 / 从 URL 抓取（跨域受限时请粘贴）。解析产物为能力清单与平台约束，默认不启用。'));

    var area = el('textarea', 'comp-doc');
    area.rows = 10;
    area.placeholder = '粘贴官方 API 文档（api_doc.md / MCP 接入文档）…';
    area.value = state.docText;
    area.addEventListener('input', function () { state.docText = area.value; });
    card.appendChild(area);
    if (!state.docText.trim()) {
      card.appendChild(el('div', 'comp-card-sub',
        '⚠ 当前文档为空：请粘贴 Markdown 或点「上传 .md」；解析按钮在内容为空时不会发起请求。'));
    }

    var row = el('div', 'comp-row');
    var file = el('input', '');
    file.type = 'file';
    file.accept = '.md,.txt';
    file.hidden = true;
    var fileBtn = el('button', 'comp-btn', '上传 .md');
    fileBtn.type = 'button';
    fileBtn.addEventListener('click', function () { file.click(); });
    file.addEventListener('change', function () {
      var f = file.files && file.files[0];
      if (!f) return;
      var reader = new global.FileReader();
      reader.onload = function () {
        state.docText = String(reader.result || '');
        render();
      };
      reader.readAsText(f);
    });
    row.appendChild(fileBtn);
    row.appendChild(file);

    var urlInput = el('input', 'comp-input comp-input--grow');
    urlInput.placeholder = '从 URL 抓取（可选）';
    urlInput.value = state.docUrl;
    urlInput.addEventListener('input', function () { state.docUrl = urlInput.value; });
    row.appendChild(urlInput);
    var urlBtn = el('button', 'comp-btn', '抓取');
    urlBtn.type = 'button';
    urlBtn.addEventListener('click', async function () {
      if (!state.docUrl.trim()) return;
      try {
        var res = await fetch(state.docUrl.trim());
        if (!res.ok) throw new Error('HTTP ' + res.status);
        state.docText = await res.text();
        toast('已抓取文档（' + state.docText.length + ' 字符）', 'success');
        render();
      } catch (err) {
        toast('抓取失败（可能跨域）：' + err.message + '；请改为粘贴文本', 'warn');
      }
    });
    row.appendChild(urlBtn);

    var parseBtn = el('button', 'comp-btn comp-btn--primary',
      state.parsing ? '解析中…' : '解析（预览）');
    parseBtn.type = 'button';
    parseBtn.disabled = state.parsing;
    parseBtn.addEventListener('click', async function () {
      if (state.parsing) return;
      if (!state.docText.trim()) { toast('文档为空：请粘贴内容或上传 .md 文件', 'warn'); return; }
      state.parsing = true;
      render();
      toast('AI 解析中（规则优先，必要时调用模型，最长约 45 秒）…', 'info', true);
      try {
        var base = '/api/competitions/' + encodeURIComponent(state.currentId);
        var res = await req(
          base + '/ingest-doc',
          { method: 'POST', body: { text: state.docText, apply: false } },
          120000
        );
        state.preview = res;
        var count = (res.facts || []).length;
        toast('解析完成：' + count + ' 条事实（协议 ' + (res.protocolCount || 0) + ' 条）' +
          (count < 2 ? '；文档结构偏少，可在黑板人工追加' : ''),
          res.valid ? 'success' : 'warn');
      } catch (err) {
        toast('解析失败：' + (err && err.message ? err.message : err), 'error');
      } finally {
        state.parsing = false;
        render();
      }
    });
    row.appendChild(parseBtn);

    var applyBtn = el('button', 'comp-btn comp-btn--primary',
      state.parsing ? '落库中…' : '落库到黑板');
    applyBtn.type = 'button';
    applyBtn.disabled = state.parsing || !state.preview || !state.preview.valid;
    applyBtn.addEventListener('click', async function () {
      if (state.parsing) return;
      state.parsing = true;
      render();
      try {
        var base = '/api/competitions/' + encodeURIComponent(state.currentId);
        var res = await req(base + '/ingest-doc', { method: 'POST', body: { text: state.docText, apply: true } });
        state.preview = null;
        toast('已落库：' + ((res.persisted && res.persisted.capabilities) || []).length +
          ' 项能力（默认关闭，需连通测试后启用）', 'success');
        await loadAll();
      } catch (err) {
        toast('落库失败：' + err.message, 'error');
      } finally {
        state.parsing = false;
        render();
      }
    });
    row.appendChild(applyBtn);
    card.appendChild(row);

    if (state.preview) card.appendChild(previewBlock(state.preview));
    pane.appendChild(card);
    pane.appendChild(capabilityTable());
    return pane;
  }

  function previewBlock(res) {
    var box = el('div', 'comp-preview');
    var facts = res.facts || [];
    var counts = {};
    facts.forEach(function (f) { counts[f.kind] = (counts[f.kind] || 0) + 1; });
    box.appendChild(el('div', 'comp-preview-head',
      '解析结果：' + facts.length + ' 条事实（协议 ' + (counts.protocol || 0) +
      ' · 规则 ' + (counts.rule || 0) + ' · 约束 ' + (counts.constraint || 0) + '）'));
    var chips = el('div', 'comp-chips');
    facts.slice(0, 14).forEach(function (f) {
      chips.appendChild(el('span', 'comp-chip',
        (KIND_LABEL[f.kind] || f.kind) + ' · ' + (f.summary || '').slice(0, 40)));
    });
    if (facts.length > 14) {
      chips.appendChild(el('span', 'comp-chip', '… 其余 ' + (facts.length - 14) + ' 条落库后见黑板'));
    }
    box.appendChild(chips);
    (res.notes || []).forEach(function (n) {
      box.appendChild(el('div', 'comp-preview-note', 'ℹ ' + n));
    });
    (res.errors || []).forEach(function (e) {
      box.appendChild(el('div', 'comp-preview-err', '⚠ ' + e));
    });
    return box;
  }

  function capabilityTable() {
    var card = el('section', 'comp-card');
    var head = el('div', 'comp-card-head');
    var protocolCount = (state.board && state.board.counts && state.board.counts.protocol) || 0;
    head.appendChild(el('h3', 'comp-card-title', '黑板接口条目（' + protocolCount + '）'));
    head.appendChild(el('span', 'comp-card-sub',
      '来自文档解析的事实（append-only）。条目 body 保存「方法/路径/说明」，引擎按此调用平台；'
      + '解析不准时可在黑板人工追加修正条目。'));
    card.appendChild(head);

    var facts = ((state.board && state.board.facts) || [])
      .filter(function (f) { return f.kind === 'protocol' || f.kind === 'rule' || f.kind === 'constraint'; });
    if (!facts.length) {
      card.appendChild(el('div', 'comp-empty',
        '黑板中还没有接口/规则事实：请粘贴官方文档并点「落库到黑板」'));
      return card;
    }

    var table = el('table', 'comp-table');
    var thead = el('thead');
    var hr = el('tr');
    ['类型', '条目', '来源', '时间'].forEach(function (t) { hr.appendChild(el('th', '', t)); });
    thead.appendChild(hr);
    table.appendChild(thead);
    var tbody = el('tbody');
    facts.forEach(function (f) {
      var tr = el('tr');
      tr.appendChild(el('td', 'comp-td-name', (KIND_LABEL[f.kind] || f.kind)));
      tr.appendChild(el('td', 'comp-td-detail', f.summary));
      tr.appendChild(el('td', 'comp-td-time', f.source));
      tr.appendChild(el('td', 'comp-td-time', fmtTime(f.createdAt)));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    card.appendChild(table);
    return card;
  }

  // ---------------------------------------------------------------- Tab2 题目池
  function poolPane() {
    var pane = el('div', 'comp-pane-inner');
    var card = el('section', 'comp-card');
    var head = el('div', 'comp-card-head');
    head.appendChild(el('h3', 'comp-card-title', '题目池（' + state.exercises.length + '）'));
    var actions = el('div', 'comp-row');
    var syncBtn = el('button', 'comp-btn', '同步题目');
    syncBtn.type = 'button';
    syncBtn.addEventListener('click', function () { runAction('sync'); });
    actions.appendChild(syncBtn);
    head.appendChild(actions);
    card.appendChild(head);

    if (!state.exercises.length) {
      card.appendChild(el('div', 'comp-empty',
        '题目池为空：请先在「文档解析」页启用 list_exercises，再点「同步题目」'));
      pane.appendChild(card);
      return pane;
    }

    var table = el('table', 'comp-table');
    var thead = el('thead');
    var hr = el('tr');
    ['题目', '分类', '分值', '难度', '状态', 'Flag', '尝试', '操作'].forEach(function (t) {
      hr.appendChild(el('th', '', t));
    });
    thead.appendChild(hr);
    table.appendChild(thead);

    var tbody = el('tbody');
    state.exercises.forEach(function (ex) {
      var tr = el('tr', ex.pinned ? 'comp-tr--pinned' : '');
      tr.appendChild(el('td', 'comp-td-name', ex.name || ex.key));
      tr.appendChild(el('td', '', ex.category || '-'));
      tr.appendChild(el('td', 'comp-td-score', String(ex.score)));
      tr.appendChild(el('td', '', ex.difficulty || '-'));
      tr.appendChild(el('td', '', ex.status));
      tr.appendChild(el('td', 'comp-td-flags', ex.flagsGot + '/' + ex.flagsTotal));
      tr.appendChild(el('td', '', String(ex.attempts)));
      var ops = el('td', 'comp-td-ops');
      var pin = el('button', 'comp-mini-btn', ex.pinned ? '取消置顶' : '📌 置顶');
      pin.type = 'button';
      pin.addEventListener('click', async function () {
        try {
          var base = '/api/competitions/' + encodeURIComponent(state.currentId);
          await req(base + '/pin/' + encodeURIComponent(ex.key) +
            '?pinned=' + (ex.pinned ? 'false' : 'true'), { method: 'POST' });
          toast((ex.pinned ? '已取消置顶 ' : '已置顶 ') + (ex.name || ex.key), 'success');
          await loadAll();
        } catch (err) { toast('操作失败：' + err.message, 'error'); }
      });
      ops.appendChild(pin);
      var submit = el('button', 'comp-mini-btn', '提交 flag');
      submit.type = 'button';
      submit.addEventListener('click', function () {
        state.submitTarget = ex.key;
        render();
      });
      ops.appendChild(submit);
      tr.appendChild(ops);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    // 表格外包横向滚动容器：8 列 nowrap 表格最小宽 ~480px，中栏窗格窄时
    // 不包这层会溢出并被右侧黑板面板盖住（操作列不可见不可点）
    var wrap = el('div', 'comp-table-wrap');
    wrap.appendChild(table);
    card.appendChild(wrap);

    if (state.submitTarget) card.appendChild(submitModal(state.submitTarget));
    pane.appendChild(card);
    return pane;
  }

  function submitModal(key) {
    var ex = state.exercises.filter(function (e) { return e.key === key; })[0];
    var box = el('div', 'comp-modal');
    var body = el('div', 'comp-modal-body');
    body.appendChild(el('h3', 'comp-card-title', '手动兜底提交 · ' + (ex ? (ex.name || ex.key) : key)));
    body.appendChild(el('div', 'comp-card-sub',
      '人工提交仍会走去重与平台判定；提交记录会写入黑板反馈。flag 上屏时自动脱敏。'));
    var input = el('input', 'comp-input');
    input.placeholder = 'flag{...}';
    body.appendChild(input);
    var row = el('div', 'comp-row');
    var ok = el('button', 'comp-btn comp-btn--primary', '提交');
    ok.type = 'button';
    ok.addEventListener('click', async function () {
      var flag = input.value.trim();
      if (!flag) { toast('flag 不能为空', 'warn'); return; }
      try {
        var base = '/api/competitions/' + encodeURIComponent(state.currentId);
        var res = await req(base + '/submit', {
          method: 'POST', body: { exercise_key: key, flag: flag }
        });
        toast(res.ok ? '提交成功：' + res.message : '提交未通过：' + res.message,
          res.ok ? 'success' : 'warn');
        state.submitTarget = null;
        await loadAll();
      } catch (err) { toast('提交失败：' + err.message, 'error'); }
    });
    row.appendChild(ok);
    var cancel = el('button', 'comp-btn', '取消');
    cancel.type = 'button';
    cancel.addEventListener('click', function () { state.submitTarget = null; render(); });
    row.appendChild(cancel);
    body.appendChild(row);
    box.appendChild(body);
    return box;
  }

  // ---------------------------------------------------------------- Tab3 策略配置
  function strategyPane() {
    var pane = el('div', 'comp-pane-inner');
    var cfg = state.strategyDraft || strategyDefaults();
    var runtime = (state.status && state.status.runtime) || {};

    var card = el('section', 'comp-card');
    card.appendChild(el('h3', 'comp-card-title', '运行状态'));
    var metrics = el('div', 'comp-metrics');
    metrics.appendChild(metric('运行实例', (runtime.runningInstances || 0) + ' / ' + (runtime.maxInstances || cfg.maxInstances)));
    metrics.appendChild(metric('限流', (runtime.rateLimitPerSec || cfg.rateLimitPerSec) + ' 次/秒'));
    metrics.appendChild(metric('已启用能力',
      ((state.status && state.status.capabilities && state.status.capabilities.enabled) || 0) +
      ' / ' + ((state.status && state.status.capabilities && state.status.capabilities.total) || 0)));
    metrics.appendChild(metric('提交统计', Object.keys((state.subs && state.subs.counts) || {})
      .map(function (k) { return k + ' ' + state.subs.counts[k]; }).join(' · ') || '暂无'));
    card.appendChild(metrics);

    var bucket = el('div', 'comp-bucket');
    var used = Math.min(1, (runtime.runningInstances || 0) / Math.max(1, runtime.maxInstances || cfg.maxInstances));
    var fill = el('div', 'comp-bucket-fill');
    fill.style.width = Math.round(used * 100) + '%';
    bucket.appendChild(fill);
    card.appendChild(el('div', 'comp-card-sub', '实例配额水位（令牌桶按限流配置执行）'));
    card.appendChild(bucket);
    pane.appendChild(card);

    var form = el('section', 'comp-card');
    form.appendChild(el('h3', 'comp-card-title', '选题权重'));
    form.appendChild(el('div', 'comp-card-sub',
      'priority = w分值×分值 + w难度×(1-难度) + w新鲜度 + w关卡；置顶题目跳过评分'));
    [['w_score', '分值权重'], ['w_difficulty', '难度权重'], ['w_fresh', '新鲜度权重'],
     ['w_level', '关卡亲和权重']].forEach(function (pair) {
      form.appendChild(slider(pair[0], pair[1], cfg[pair[0]], 0, 1, 0.05));
    });
    pane.appendChild(form);

    var cons = el('section', 'comp-card');
    cons.appendChild(el('h3', 'comp-card-title', '约束与开关'));
    cons.appendChild(numberField('max_attempts', '单题最大尝试', cfg.max_attempts));
    cons.appendChild(numberField('wrong_before_cooldown', '错误几次进冷却', cfg.wrong_before_cooldown));
    cons.appendChild(numberField('cooldown_minutes', '冷却分钟', cfg.cooldown_minutes));
    cons.appendChild(numberField('rateLimitPerSec', '限流（次/秒）', cfg.rateLimitPerSec));
    cons.appendChild(numberField('maxInstances', '最大实例数', cfg.maxInstances));
    cons.appendChild(textField('flagRegex', 'flag 格式正则（可选）', cfg.flagRegex));

    var hintRow = el('label', 'comp-switch');
    var hintBox = el('input', '');
    hintBox.type = 'checkbox';
    hintBox.checked = !!cfg.hintEnabled;
    hintBox.addEventListener('change', function () {
      if (hintBox.checked && !global.confirm(
        '⚠ view_hint 有扣分风险：部分平台首次查看提示会扣除该题总分的 10%。\n确定开启？')) {
        hintBox.checked = false;
        return;
      }
      cfg.hintEnabled = hintBox.checked;
      state.strategyDraft = cfg;
    });
    hintRow.appendChild(hintBox);
    hintRow.appendChild(el('span', '', '允许调用 view_hint（默认关闭；开启会扣分）'));
    cons.appendChild(hintRow);

    var autoRow = el('label', 'comp-switch');
    var autoBox = el('input', '');
    autoBox.type = 'checkbox';
    autoBox.checked = !!cfg.autoSolve;
    autoBox.addEventListener('change', function () {
      cfg.autoSolve = autoBox.checked;
      state.strategyDraft = cfg;
    });
    autoRow.appendChild(autoBox);
    autoRow.appendChild(el('span', '', 'auto_solve：自动派发 Solver 解题（关闭时只做同步/提交）'));
    cons.appendChild(autoRow);

    var saveRow = el('div', 'comp-row');
    var save = el('button', 'comp-btn comp-btn--primary', '保存策略');
    save.type = 'button';
    save.addEventListener('click', async function () {
      try {
        var base = '/api/competitions/' + encodeURIComponent(state.currentId);
        await req(base, { method: 'PATCH', body: { strategy: state.strategyDraft } });
        toast('策略已保存', 'success');
        await loadAll();
      } catch (err) { toast('保存失败：' + err.message, 'error'); }
    });
    saveRow.appendChild(save);
    var reset = el('button', 'comp-btn', '恢复默认');
    reset.type = 'button';
    reset.addEventListener('click', function () {
      state.strategyDraft = strategyDefaults();
      render();
    });
    saveRow.appendChild(reset);
    cons.appendChild(saveRow);
    pane.appendChild(cons);
    return pane;
  }

  function metric(label, value) {
    var box = el('div', 'comp-metric');
    box.appendChild(el('div', 'comp-metric-label', label));
    box.appendChild(el('div', 'comp-metric-value', value));
    return box;
  }

  function slider(key, label, value, min, max, step) {
    var cfg = state.strategyDraft;
    var row = el('label', 'comp-slider');
    var head = el('div', 'comp-slider-head');
    head.appendChild(el('span', '', label));
    var out = el('span', 'comp-slider-value', Number(value).toFixed(2));
    head.appendChild(out);
    row.appendChild(head);
    var input = el('input', '');
    input.type = 'range';
    input.min = String(min);
    input.max = String(max);
    input.step = String(step);
    input.value = String(value);
    input.addEventListener('input', function () {
      cfg[key] = Number(input.value);
      out.textContent = Number(input.value).toFixed(2);
    });
    row.appendChild(input);
    return row;
  }

  function numberField(key, label, value) {
    var cfg = state.strategyDraft;
    var row = el('label', 'comp-field');
    row.appendChild(el('span', '', label));
    var input = el('input', 'comp-input');
    input.type = 'number';
    input.value = String(value);
    input.addEventListener('input', function () {
      cfg[key] = Number(input.value);
    });
    row.appendChild(input);
    return row;
  }

  function textField(key, label, value) {
    var cfg = state.strategyDraft;
    var row = el('label', 'comp-field');
    row.appendChild(el('span', '', label));
    var input = el('input', 'comp-input');
    input.value = String(value || '');
    input.addEventListener('input', function () { cfg[key] = input.value; });
    row.appendChild(input);
    return row;
  }

  // ---------------------------------------------------------------- 右1：全局黑板
  function boardPanel() {
    var aside = el('aside', 'comp-board' + (state.boardCollapsed ? ' collapsed' : ''));
    var head = el('div', 'comp-panel-head');
    head.appendChild(el('span', 'comp-panel-title', '竞赛全局黑板'));
    var fold = el('button', 'comp-icon-btn', state.boardCollapsed ? '‹' : '›');
    fold.type = 'button';
    fold.title = state.boardCollapsed ? '展开黑板' : '折叠黑板';
    fold.addEventListener('click', function () {
      state.boardCollapsed = !state.boardCollapsed;
      render();
    });
    head.appendChild(fold);
    aside.appendChild(head);
    if (state.boardCollapsed) return aside;

    var body = el('div', 'comp-panel-body');
    var counts = (state.board && state.board.counts) || {};
    var facts = (state.board && state.board.facts) || [];
    BOARD_KINDS.forEach(function (kind) {
      var group = el('div', 'comp-fact-group');
      var gh = el('div', 'comp-fact-group-head');
      gh.appendChild(el('span', '', KIND_LABEL[kind] + '（' + (counts[kind] || 0) + '）'));
      group.appendChild(gh);
      facts.filter(function (f) { return f.kind === kind; }).slice(0, 20).forEach(function (f) {
        var item = el('details', 'comp-fact');
        var sum = el('summary', 'comp-fact-sum');
        sum.appendChild(el('span', 'comp-fact-title', f.summary));
        sum.appendChild(el('span', 'comp-fact-src', f.source));
        item.appendChild(sum);
        if (f.body) item.appendChild(el('pre', 'comp-fact-body', f.body));
        group.appendChild(item);
      });
      if (!(counts[kind] || 0)) group.appendChild(el('div', 'comp-empty', '暂无'));
      body.appendChild(group);
    });

    var add = el('div', 'comp-row');
    var input = el('input', 'comp-input comp-input--grow');
    input.placeholder = '人工追加一条事实（默认 notice）…';
    add.appendChild(input);
    var btn = el('button', 'comp-btn', '追加');
    btn.type = 'button';
    btn.addEventListener('click', async function () {
      var text = input.value.trim();
      if (!text) return;
      try {
        var base = '/api/competitions/' + encodeURIComponent(state.currentId);
        await req(base + '/board', { method: 'POST', body: { kind: 'notice', summary: text, body: '' } });
        toast('已追加黑板事实', 'success');
        await loadAll();
      } catch (err) { toast('追加失败：' + err.message, 'error'); }
    });
    add.appendChild(btn);
    body.appendChild(add);
    aside.appendChild(body);
    return aside;
  }

  // ---------------------------------------------------------------- 右2：日志 + 提交
  function logPanel() {
    var aside = el('aside', 'comp-logs' + (state.logsCollapsed ? ' collapsed' : ''));
    var head = el('div', 'comp-panel-head');
    head.appendChild(el('span', 'comp-panel-title', '实时运行日志'));
    var fold = el('button', 'comp-icon-btn', state.logsCollapsed ? '‹' : '›');
    fold.type = 'button';
    fold.title = state.logsCollapsed ? '展开日志' : '折叠日志';
    fold.addEventListener('click', function () {
      state.logsCollapsed = !state.logsCollapsed;
      render();
    });
    head.appendChild(fold);
    aside.appendChild(head);
    if (state.logsCollapsed) return aside;

    var body = el('div', 'comp-panel-body');
    var logBox = el('div', 'comp-log');
    logBox.id = 'comp-log';
    body.appendChild(logBox);

    body.appendChild(el('div', 'comp-panel-sub', '提交记录'));
    var subs = (state.subs && state.subs.submissions) || [];
    if (!subs.length) {
      body.appendChild(el('div', 'comp-empty', '暂无提交'));
    } else {
      var table = el('table', 'comp-table comp-table--subs');
      // 提交记录表同样需要横向滚动容器（列多，窗格窄时溢出会被盖）
      var thead = el('thead');
      var hr = el('tr');
      ['题目', 'flag', '判定', '时间'].forEach(function (t) { hr.appendChild(el('th', '', t)); });
      thead.appendChild(hr);
      table.appendChild(thead);
      var tbody = el('tbody');
      subs.slice(0, 40).forEach(function (s) {
        var tr = el('tr');
        tr.appendChild(el('td', 'comp-td-name', s.exerciseKey));
        tr.appendChild(el('td', 'comp-td-flag', s.flagMasked || '****'));
        tr.appendChild(el('td', 'comp-verdict comp-verdict--' + s.verdict, s.verdict));
        tr.appendChild(el('td', 'comp-td-time', fmtTime(s.createdAt)));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      var wrap = el('div', 'comp-table-wrap');
      wrap.appendChild(table);
      body.appendChild(wrap);
    }
    aside.appendChild(body);
    return aside;
  }

  function paintLogs() {
    var box = doc.getElementById('comp-log');
    if (!box) return;
    clear(box);
    if (!state.logs.length) {
      box.appendChild(el('div', 'comp-empty', '等待日志…（启动自动化后每步都会输出决策理由）'));
      return;
    }
    state.logs.slice(-120).forEach(function (entry) {
      var line = el('div', 'comp-log-line comp-log-line--' + (PHASE_TONE[entry.phase] || 'system'));
      line.appendChild(el('span', 'comp-log-phase', '[' + entry.phase + ']'));
      line.appendChild(el('span', 'comp-log-time', fmtTime(entry.at)));
      line.appendChild(el('span', 'comp-log-msg', entry.message));
      box.appendChild(line);
    });
    box.scrollTop = box.scrollHeight;
  }

  loadWidths();

  CTFA.competitionView = { load: loadAll, render: render, _state: state };
})(typeof window !== 'undefined' ? window : this);
