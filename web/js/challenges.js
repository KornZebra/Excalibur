/* ============================================================================
 * challenges.js — 题目模块（U13）
 * ----------------------------------------------------------------------------
 * 对应需求：
 *   1) 用户可在题目模块**自主创建题目**：名称 / 类型 / 描述 / 提示词 / 附件（可选）；
 *   2) 勾选多道题后**一键启动自主解题**；并发上限取自配置
 *      ``challenge.max_concurrent_runs``（默认 3），超出的题目排队，有题目结束后自动补位；
 *   3) 状态一目了然：解题中 / 已解答 / 未解答；
 *   4) 题目按来源分组：「模块创建」与「会话升级」分开呈现。
 *
 * 数据来源（全部为既有/新增 REST + SSE）：
 *   GET    /api/challenges                    列表（含 status / verdict / solved / flagCount）
 *   POST   /api/challenges                    创建（meta.source=manual、meta.prompt）
 *   POST   /api/challenges/{id}/attachments   附件上传（落题目工作区）
 *   POST   /api/challenges/{id}/hint          提示词注入（hint Memory，最高优先级）
 *   POST   /api/challenges/{id}/run           SSE 自主解题
 *
 * 不改变对话视图与挑战控制台（challenge.js）的既有逻辑。
 * 所有颜色走 css/style.css 的设计变量，不硬编码。
 * ========================================================================== */
(function (global) {
  'use strict';

  var CTFA = (global.CTFA = global.CTFA || {});
  var doc = global.document;

  var CATEGORIES = [
    ['web', 'Web'],
    ['pwn', 'Pwn'],
    ['crypto', 'Crypto'],
    ['reverse', 'Reverse'],
    ['forensics', 'Forensics'],
    ['misc', 'Misc']
  ];

  var DEFAULT_LIMIT = 3;
  var KEY_COLLAPSED = 'ctfa.challengeGroupsCollapsed';

  var view = { box: null, ctx: {} };
  var state = {
    challenges: [],
    conversations: [],
    selection: {},
    formOpen: false,
    formEl: null,
    busy: false,
    collapsed: { manual: false, conversation: false },   // 分组折叠状态（localStorage 持久化）
    collapsedLoaded: false,
    running: {},        // id -> { controller, startedAt }
    ticker: null
  };

  /* --------------------------- 折叠状态持久化 --------------------------- */

  function loadCollapsed() {
    if (state.collapsedLoaded) return;
    state.collapsedLoaded = true;
    try {
      var raw = global.localStorage.getItem(KEY_COLLAPSED);
      if (!raw) return;
      var obj = JSON.parse(raw);
      if (obj && typeof obj === 'object') {
        state.collapsed = { manual: !!obj.manual, conversation: !!obj.conversation };
      }
    } catch (e) { /* 读不到就用默认展开 */ }
  }

  function saveCollapsed() {
    try {
      global.localStorage.setItem(KEY_COLLAPSED, JSON.stringify(state.collapsed));
    } catch (e) { /* 忽略（隐私模式等） */ }
  }

  /* ------------------------------ 小工具 ------------------------------ */

  function el(tag, cls, text) {
    var node = doc.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

  function badge(text, tone) {
    return el('span', 'badge badge-' + (tone || 'chip'), text);
  }

  /** 可归属的项目列表（来自 projects 模块的共享状态）。 */
  function projects() {
    var st = (CTFA.projects && CTFA.projects.state) || {};
    return st.projects || [];
  }

  function projectName(id) {
    if (!id) return '';
    var hit = projects().filter(function (p) { return p.id === id; })[0];
    return hit ? (hit.name || id) : id;
  }

  function optionEl(value, label) {
    var o = el('option', null, label);
    o.value = value;
    return o;
  }

  function chip(text, tone) {
    return el('span', 'chip' + (tone ? ' chip-' + tone : ''), text);
  }

  function fmtClock(ms) {
    var total = Math.max(0, Math.floor(ms / 1000));
    var m = Math.floor(total / 60);
    var s = total % 60;
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  function notify(message, tone) {
    if (CTFA.timeline && typeof CTFA.timeline.notice === 'function') {
      CTFA.timeline.notice(message, tone);
      return;
    }
    if (global.console) global.console.log('[challenges]', message);
  }

  function statusOf(c) {
    if (state.running[c.id]) return { text: '解题中', tone: 'info' };
    if (c.status === 'running') return { text: '解题中', tone: 'info' };
    if (c.solved) return { text: '已解答', tone: 'on' };
    return { text: '未解答', tone: 'chip' };
  }

  /** 题目来源：优先 meta.source（新数据），否则按会话绑定 / 附件路径推断（历史数据）。 */
  function sourceOf(c) {
    var s = ((c.meta || {}).source) || '';
    if (s === 'manual' || s === 'conversation') return s;
    var bound = state.conversations.some(function (conv) { return conv.challengeId === c.id; });
    if (bound) return 'conversation';
    var atts = Array.isArray(c.attachments) ? c.attachments : [];
    if (atts.some(function (p) { return String(p).indexOf('conversations/') >= 0; })) return 'conversation';
    return 'manual';
  }

  function groupBySource() {
    var groups = { manual: [], conversation: [] };
    state.challenges.forEach(function (c) { groups[sourceOf(c)].push(c); });
    return groups;
  }

  function selectedIds() {
    return Object.keys(state.selection).filter(function (id) { return state.selection[id]; });
  }

  function runningCount() { return Object.keys(state.running).length; }

  /* ------------------------------ 渲染 ------------------------------ */

  function render(box, ctx) {
    view.box = box;
    view.ctx = ctx || {};
    loadCollapsed();
    state.challenges = view.ctx.challenges || [];
    state.conversations = view.ctx.conversations || [];
    // 归属项目下拉需要项目列表：未加载时拉一次并重绘（不阻塞首屏）
    if (CTFA.projects && !CTFA.projects.state.loaded && typeof CTFA.projects.load === 'function') {
      CTFA.projects.load().then(function () { paint(); }).catch(function () { /* 忽略 */ });
    }
    // 清理已被删除题目的勾选/运行态
    var alive = {};
    state.challenges.forEach(function (c) { alive[c.id] = true; });
    Object.keys(state.selection).forEach(function (id) { if (!alive[id]) delete state.selection[id]; });
    Object.keys(state.running).forEach(function (id) { if (!alive[id]) delete state.running[id]; });
    paint();
    ensureTicker();
  }

  function paint() {
    var box = view.box;
    if (!box) return;
    clear(box);
    box.appendChild(headerEl());
    if (state.formOpen) box.appendChild(ensureForm());
    box.appendChild(toolbarEl());
    var panel = runPanelEl();
    if (panel) box.appendChild(panel);
    var groups = groupBySource();
    box.appendChild(groupEl('manual', '模块创建', '在题目模块中手工创建', groups.manual));
    box.appendChild(groupEl('conversation', '会话升级', '由对话升级而来（绑定会话）', groups.conversation));
    if (!state.challenges.length) {
      box.appendChild(el('div', 'ch-empty', '暂无题目：点击「＋ 新建题目」手工创建，或在对话页把当前会话升级为题目。'));
    }
  }

  function headerEl() {
    var head = el('div', 'ch-head');
    var left = el('div', 'ch-head-main');
    // 标题由 index.html 的 .view-title 提供，这里只放说明与统计，避免重复标题
    left.appendChild(el('span', 'view-hint',
      '自主创建题目 → 打开题目控制台启动解题（并发由多个会话承担，不再有批量队列）'));
    head.appendChild(left);
    var stats = el('div', 'chips');
    var solved = state.challenges.filter(function (c) { return c.solved; }).length;
    stats.appendChild(chip('共 ' + state.challenges.length + ' 题', 'chip'));
    stats.appendChild(chip('已解答 ' + solved, 'ok'));
    stats.appendChild(chip('未解答 ' + (state.challenges.length - solved), 'muted'));
    if (runningCount()) stats.appendChild(chip('解题中 ' + runningCount(), 'info'));
    head.appendChild(stats);
    return head;
  }

  /** 修改题目归属项目：成功则整体重载（看板与分组都要刷新）。 */
  async function assignProject(c, select) {
    var previous = (c.meta || {}).projectId || '';
    var pid = select.value;
    select.disabled = true;
    try {
      await CTFA.api.setChallengeProject(c.id, pid);
      notify(pid ? ('已归属：' + projectName(pid)) : '已取消项目归属', 'ok');
      if (view.ctx.reload) await view.ctx.reload();
      else paint();
    } catch (err) {
      notify('设置归属失败：' + ((err && err.message) || err), 'error');
      select.disabled = false;
      select.value = previous;
    }
  }

  /* ---------------------------- 创建表单 ---------------------------- */

  function ensureForm() {
    if (state.formEl) return state.formEl;
    var card = el('article', 'challenge-card ch-form');

    var row1 = el('div', 'ch-form-row');
    var nameInput = el('input', 'upgrade-input ch-name');
    nameInput.type = 'text';
    nameInput.placeholder = '题目名称（必填）';
    var catSelect = el('select', 'upgrade-input ch-cat');
    CATEGORIES.forEach(function (pair) {
      var opt = el('option', null, pair[1]);
      opt.value = pair[0];
      catSelect.appendChild(opt);
    });
    row1.appendChild(nameInput);
    row1.appendChild(catSelect);
    card.appendChild(row1);

    var desc = el('textarea', 'upgrade-input ch-textarea');
    desc.rows = 3;
    desc.placeholder = '题目描述（题干、目标地址、约束、flag 格式…）';
    card.appendChild(desc);

    var prompt = el('textarea', 'upgrade-input ch-textarea');
    prompt.rows = 2;
    prompt.placeholder = '提示词（可选）：给 AI 的额外指引，会以「人类提示」注入题目账本';
    card.appendChild(prompt);

    // 归属项目（可选）：决定该题计入哪个项目的看板统计
    var projRow = el('div', 'ch-form-row ch-form-proj');
    var projLabel = el('span', 'ch-form-label', '归属项目');
    var projSelect = el('select', 'upgrade-input ch-proj-select');
    projSelect.appendChild(optionEl('', '不归属项目'));
    projects().forEach(function (p) { projSelect.appendChild(optionEl(p.id, p.name)); });
    var currentProject = (CTFA.projects && CTFA.projects.state && CTFA.projects.state.currentProjectId) || '';
    projSelect.value = currentProject;
    var projHint = el('span', 'li-sub', '可选：归属后计入该项目看板');
    projRow.appendChild(projLabel);
    projRow.appendChild(projSelect);
    projRow.appendChild(projHint);
    card.appendChild(projRow);

    var fileRow = el('div', 'ch-form-row');
    var fileInput = el('input', 'ch-file');
    fileInput.type = 'file';
    fileInput.multiple = true;
    var fileLabel = el('span', 'li-sub', '未选择附件');
    fileInput.addEventListener('change', function () {
      var n = fileInput.files ? fileInput.files.length : 0;
      fileLabel.textContent = n ? ('已选择 ' + n + ' 个附件') : '未选择附件';
    });
    var pick = el('label', 'btn ch-filebtn', '📎 选择附件（可选）');
    pick.appendChild(fileInput);
    fileRow.appendChild(pick);
    fileRow.appendChild(fileLabel);
    card.appendChild(fileRow);

    var actions = el('div', 'ch-form-actions');
    var submit = el('button', 'btn primary', '创建题目');
    submit.addEventListener('click', function () { createChallenge(card, submit); });
    var cancel = el('button', 'btn', '取消');
    cancel.addEventListener('click', function () {
      state.formOpen = false;
      state.formEl = null;
      paint();
    });
    actions.appendChild(submit);
    actions.appendChild(cancel);
    card.appendChild(actions);

    card._fields = { name: nameInput, category: catSelect, description: desc, prompt: prompt, files: fileInput, project: projSelect };
    state.formEl = card;
    return card;
  }

  async function createChallenge(card, submitBtn) {
    var f = card._fields;
    var name = (f.name.value || '').trim();
    if (!name) { notify('请填写题目名称', 'warn'); f.name.focus(); return; }
    var promptText = (f.prompt.value || '').trim();
    var files = f.files && f.files.files ? f.files.files : null;

    state.busy = true;
    submitBtn.disabled = true;
    submitBtn.textContent = '创建中…';
    try {
      var projectId = (f.project && f.project.value) || '';
      var res = await CTFA.api.createChallenge({
        name: name,
        category: f.category.value || 'misc',
        description: (f.description.value || '').trim(),
        project_id: projectId || null,
        meta: { source: 'manual', prompt: promptText }
      });
      var cid = (res && res.challenge && res.challenge.id) || '';
      if (!cid) throw new Error('后端未返回题目 ID');

      var uploaded = 0;
      if (files && files.length) {
        var up = await CTFA.api.uploadChallengeAttachments(cid, files);
        uploaded = ((up && up.attachments) || []).length;
      }
      if (promptText) {
        await CTFA.api.request('/api/challenges/' + encodeURIComponent(cid) + '/hint', {
          method: 'POST',
          body: { text: promptText, author: 'human' }
        }).catch(function () { /* 提示注入失败不阻断创建 */ });
      }

      notify('题目已创建：' + name
        + (uploaded ? '（附件 ' + uploaded + ' 个）' : '')
        + (projectId ? '（归属 ' + projectName(projectId) + '）' : '')
        + (promptText ? '（提示词已注入账本）' : ''), 'ok');
      state.formOpen = false;
      state.formEl = null;
      if (view.ctx.reload) await view.ctx.reload();
      else paint();
    } catch (err) {
      notify('创建题目失败：' + ((err && err.message) || err), 'error');
      submitBtn.disabled = false;
      submitBtn.textContent = '创建题目';
    } finally {
      state.busy = false;
    }
  }

  /* ------------------------------ 工具栏 ------------------------------ */

  function toolbarEl() {
    var bar = el('div', 'ch-toolbar');
    var ids = selectedIds();

    var newBtn = el('button', 'btn primary', state.formOpen ? '× 收起表单' : '＋ 新建题目');
    newBtn.addEventListener('click', function () {
      state.formOpen = !state.formOpen;
      if (!state.formOpen) state.formEl = null;
      paint();
    });
    bar.appendChild(newBtn);

    var all = el('button', 'btn', '全选');
    all.addEventListener('click', function () {
      state.challenges.forEach(function (c) { state.selection[c.id] = true; });
      paint();
    });
    var none = el('button', 'btn', '清空');
    none.disabled = !ids.length;
    none.addEventListener('click', function () { state.selection = {}; paint(); });
    bar.appendChild(all);
    bar.appendChild(none);

    var del = el('button', 'btn danger', '删除已勾选' + (ids.length ? '（' + ids.length + '）' : ''));
    del.disabled = !ids.length || state.busy;
    del.addEventListener('click', deleteSelected);
    bar.appendChild(del);

    bar.appendChild(el('span', 'ch-toolbar-hint',
      ids.length ? ('已选 ' + ids.length + ' 题') : '勾选题目后可批量删除'));
    return bar;
  }

  /* --------------------------- 运行面板（并发） --------------------------- */

  function runPanelEl() {
    var runningIds = Object.keys(state.running);
    if (!runningIds.length) return null;
    var panel = el('section', 'ch-run-panel');
    var head = el('div', 'ch-run-head');
    head.appendChild(el('span', 'ch-run-title', '自主解题'));
    head.appendChild(chip('运行中 ' + runningIds.length, 'info'));
    panel.appendChild(head);

    runningIds.forEach(function (id) {
      var c = challengeById(id);
      var row = el('div', 'ch-run-row');
      row.appendChild(el('span', 'ch-run-name', c ? (c.name || id) : id));
      var span = el('span', 'ch-run-elapsed mono');
      span.setAttribute('data-elapsed', String(state.running[id].startedAt));
      span.textContent = fmtClock(Date.now() - state.running[id].startedAt);
      row.appendChild(span);
      var stop = el('button', 'btn danger ch-run-stop', '■ 停止');
      stop.addEventListener('click', function () { stopOne(id); });
      row.appendChild(stop);
      panel.appendChild(row);
    });

    return panel;
  }

  function challengeById(id) {
    return state.challenges.filter(function (c) { return c.id === id; })[0];
  }

  function ensureTicker() {
    if (state.ticker) return;
    state.ticker = global.setInterval(function () {
      if (doc.hidden) return;
      var nodes = doc.querySelectorAll('[data-elapsed]');
      if (!nodes.length) return;
      Array.prototype.forEach.call(nodes, function (node) {
        var started = Number(node.getAttribute('data-elapsed')) || 0;
        node.textContent = fmtClock(Date.now() - started);
      });
    }, 1000);
  }

  /* ------------------------------ 启动解题 ------------------------------ */

  function launch(id) {
    var c = challengeById(id);
    var handle = CTFA.api.createStream(
      '/api/challenges/' + encodeURIComponent(id) + '/run',
      {},
      {
        onError: function (err) {
          notify('题目「' + (c ? c.name : id) + '」启动失败：' + ((err && err.message) || err), 'error');
        },
        onClose: function () { finish(id); }
      }
    );
    state.running[id] = { controller: handle.controller, startedAt: Date.now() };
    notify('已启动自主解题：' + (c ? (c.name || id) : id), 'ok');
  }

  function finish(id) {
    if (!state.running[id]) return;
    delete state.running[id];
    var c = challengeById(id);
    notify('题目「' + (c ? (c.name || id) : id) + '」解题会话已结束', 'info');
    if (view.ctx.reload) {
      view.ctx.reload().then(pump);
    } else {
    }
  }

  function stopOne(id) {
    if (!state.running[id]) return;
    notify('正在停止…', 'warn');
    CTFA.api.request('/api/challenges/' + encodeURIComponent(id) + '/stop', { method: 'POST' })
      .catch(function (err) {
        notify('停止失败：' + ((err && err.message) || err), 'error');
        finish(id);
      });
  }

  function stopAll() {
    Object.keys(state.running).forEach(stopOne);
  }

  /* ------------------------------ 删除 ------------------------------ */

  function confirmDialog(opts) {
    if (CTFA.conversations && typeof CTFA.conversations.confirmDialog === 'function') {
      return CTFA.conversations.confirmDialog(opts);
    }
    return Promise.resolve(global.confirm ? global.confirm((opts && opts.title) || '确认？') : false);
  }

  async function deleteSelected() {
    var ids = selectedIds();
    if (!ids.length || state.busy) return;
    state.busy = true;
    try {
      var ok = await confirmDialog({
        title: '批量删除 ' + ids.length + ' 个题目',
        lines: [
          '将永久删除这 ' + ids.length + ' 个题目及其账本（Idea/Memory）、Solver 运行记录、flag 记录与题目工作区。',
          '绑定的会话只会解绑，不会删除。此操作不可恢复。'
        ],
        confirmText: '删除 ' + ids.length + ' 个',
        danger: true
      });
      if (!ok) return;
      var data = await CTFA.api.batchDeleteChallenges(ids);
      var deleted = (data && data.deleted) || 0;
      var failed = (data && data.failed) || 0;
      notify('批量删除完成：成功 ' + deleted + '，失败 ' + failed, failed ? 'warn' : 'ok');
      state.selection = {};
      if (view.ctx.reload) await view.ctx.reload();
      else paint();
    } catch (err) {
      notify('批量删除失败：' + ((err && err.message) || err), 'error');
    } finally {
      state.busy = false;
    }
  }

  async function deleteOne(c) {
    if (state.busy) return;
    state.busy = true;
    try {
      var ok = await confirmDialog({
        title: '删除题目',
        lines: [
          '将永久删除：' + (c.name || c.id),
          '清理内容：题目账本（Idea/Memory）、Solver 运行记录、flag 记录与题目工作区目录。',
          '绑定的会话只会解绑，不会删除。此操作不可恢复。'
        ],
        confirmText: '永久删除',
        danger: true
      });
      if (!ok) return;
      await CTFA.api.deleteChallenge(c.id);
      notify('已删除题目：' + (c.name || c.id), 'ok');
      if (view.ctx.reload) await view.ctx.reload();
      else paint();
    } catch (err) {
      notify('删除失败：' + ((err && err.message) || err), 'error');
    } finally {
      state.busy = false;
    }
  }

  /* ------------------------------ 分组列表 ------------------------------ */

  /**
   * 分组：标题即折叠开关（▾/▸）。折叠状态持久化到 localStorage，
   * 且折叠时**只切换 body 的 hidden**（不整块重绘），避免列表滚动位置跳动。
   */
  function groupEl(key, title, desc, list) {
    var collapsed = !!state.collapsed[key];
    var section = el('section', 'ch-group' + (collapsed ? ' collapsed' : ''));

    var head = el('button', 'ch-group-head');
    head.type = 'button';
    head.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    head.title = collapsed ? '展开分组' : '折叠分组';
    var caret = el('span', 'ch-caret', collapsed ? '▸' : '▾');
    head.appendChild(caret);
    head.appendChild(el('span', 'ch-group-title', title));
    head.appendChild(badge(String(list.length), 'chip'));
    head.appendChild(el('span', 'ch-group-desc', desc));
    section.appendChild(head);

    var body = el('div', 'ch-group-body');
    if (collapsed) body.hidden = true;
    if (!list.length) body.appendChild(el('div', 'ch-empty ch-empty-sm', '暂无'));
    else list.forEach(function (c) { body.appendChild(challengeCard(c)); });
    section.appendChild(body);

    head.addEventListener('click', function () {
      var next = !state.collapsed[key];
      state.collapsed[key] = next;
      body.hidden = next;
      caret.textContent = next ? '▸' : '▾';
      head.setAttribute('aria-expanded', next ? 'false' : 'true');
      head.title = next ? '展开分组' : '折叠分组';
      section.classList.toggle('collapsed', next);
      saveCollapsed();
    });
    return section;
  }

  function challengeCard(c) {
    var card = el('article', 'challenge-card ch-item');

    var pick = el('input', 'challenge-pick ch-pick');
    pick.type = 'checkbox';
    pick.checked = !!state.selection[c.id];
    pick.title = '勾选后可批量启动解题 / 删除';
    pick.addEventListener('change', function () {
      state.selection[c.id] = pick.checked;
      if (!pick.checked) delete state.selection[c.id];
      paint();
    });
    card.appendChild(pick);

    var body = el('div', 'ch-item-body');
    var head = el('div', 'ch-item-head');
    head.appendChild(el('span', 'li-title', c.name || c.id));
    var st = statusOf(c);
    head.appendChild(badge(st.text, st.tone));
    body.appendChild(head);

    var chips = el('div', 'chips');
    if (c.category) chips.appendChild(badge(c.category, 'cat'));
    if (c.verdict) chips.appendChild(badge(c.verdict, c.verdict === 'TERMINATE_SUCCESS' ? 'on' : 'chip'));
    if (c.flagCount) chips.appendChild(badge('flag × ' + c.flagCount, 'on'));
    chips.appendChild(badge(c.id, 'chip'));
    // 归属项目：下拉即改（题目表无 project_id 列，归属存 meta.projectId）
    if (projects().length) {
      var projSel = el('select', 'ch-proj');
      projSel.title = '归属项目：决定该题计入哪个项目的看板统计';
      projSel.appendChild(optionEl('', '未归属项目'));
      projects().forEach(function (p) { projSel.appendChild(optionEl(p.id, p.name)); });
      projSel.value = (c.meta || {}).projectId || '';
      projSel.addEventListener('change', function () { assignProject(c, projSel); });
      chips.appendChild(projSel);
    }
    body.appendChild(chips);

    if (c.description) {
      var d = el('div', 'md detail-body ch-desc');
      d.innerHTML = CTFA.markdown.render(c.description);
      body.appendChild(d);
    }
    var promptText = (c.meta || {}).prompt;
    if (promptText) {
      var p = el('div', 'ch-prompt');
      p.appendChild(el('span', 'ch-prompt-label', '提示词'));
      p.appendChild(el('span', 'ch-prompt-text', String(promptText)));
      body.appendChild(p);
    }
    var atts = Array.isArray(c.attachments) ? c.attachments : [];
    if (atts.length) {
      var attRow = el('div', 'ch-atts');
      atts.forEach(function (a) {
        var name = String(a).split('/').pop();
        attRow.appendChild(el('span', 'ch-att mono', name));
      });
      body.appendChild(attRow);
    }

    var actions = el('div', 'chips ch-actions');
    var board = el('button', 'btn', '打开控制台');
    board.addEventListener('click', function () {
      if (view.ctx.onOpenConsole) view.ctx.onOpenConsole(c.id);
    });
    actions.appendChild(board);

    var sel = el('button', 'btn', c.id === view.ctx.selectedId ? '已选择' : '选为当前题目');
    sel.disabled = c.id === view.ctx.selectedId;
    sel.addEventListener('click', function () {
      if (view.ctx.onSelect) view.ctx.onSelect(c.id);
    });
    actions.appendChild(sel);

    if (state.running[c.id]) {
      var stop = el('button', 'btn danger', '■ 停止');
      stop.addEventListener('click', function () { stopOne(c.id); });
      actions.appendChild(stop);
    } else {
      var run = el('button', 'btn', '▶ 单题解题');
      run.addEventListener('click', function () { startOne(c.id); });
      actions.appendChild(run);
    }

    var del = el('button', 'btn danger', '删除');
    del.addEventListener('click', function () { deleteOne(c); });
    actions.appendChild(del);
    body.appendChild(actions);

    card.appendChild(body);
    return card;
  }

  function startOne(id) {
    var c = challengeById(id);
    if (state.running[id] || (c && c.status === 'running')) {
      notify('该题目已在解题中', 'warn');
      return;
    }
    launch(id);
    paint();
  }

  CTFA.challengesView = {
    render: render,
    stopAll: stopAll,
    _state: state
  };
})(typeof window !== 'undefined' ? window : this);
