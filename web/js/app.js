/* ============================================================================
 * app.js — 入口：初始化、路由/标签页、状态管理、SSE 事件接线
 * ----------------------------------------------------------------------------
 * 单一状态源：本文件维护 state 对象；timeline.js 只做 DOM 增量渲染。
 * 数据流：SSE 事件 → handleEvent() 更新 state → 调用 CTFA.timeline.* 更新视图。
 * ========================================================================== */
(function (global) {
  'use strict';

  var CTFA = (global.CTFA = global.CTFA || {});
  var doc = global.document;

  /* ------------------------------- 状态定义 ------------------------------- */

  var state = {
    // 运行配置
    mode: 'multi',              // single | multi
    orchestration: 'deep',       // deep | plan_execute
    health: null,
    online: false,

    // 数据集合
    tools: [],
    agents: [],
    conversations: [],
    challenges: [],
    config: {},

    // 当前会话
    conversationId: null,
    challengeId: null,
    messages: [],
    facts: [],
    factQuery: '',
    factKey: null,
    flags: [],

    // 运行态
    running: false,
    runId: null,
    controller: null,
    cancelled: false,
    toolCards: {},
    todos: [],
    usage: { modelCalls: 0, promptTokens: 0, completionTokens: 0 },

    // 运行中引导（Steering）：任务运行期间发送的消息进入 FIFO 队列，
    // 当前轮结束后的安全点按序注入；气泡角标 queued→delivered 由事件驱动。
    steer: {
      controller: null,     // 会话事件流的 AbortController
      wanted: false,        // 是否需要保持事件流连接（断线自动重连）
      lastEventId: 0        // 已消费的服务端事件序号（重连续传用）
    },
    queuedCount: 0,         // 当前队列积压条数（提示条 + 角标翻转）
    pendingSteerCard: null, // 本地已渲染、等待服务端 id 的引导气泡（防重复渲染）

    // 路由
    route: '/chat'
  };

  var refs = {};
  var localEcho = null;   // 本地已渲染的用户消息（避免 message_saved 重复插入）

  function $(id) { return doc.getElementById(id); }

  /* ------------------------------- 视图渲染 ------------------------------- */

  function renderHealth() {
    var chip = refs.healthChip;
    if (!chip) return;
    if (state.online) {
      chip.textContent = '● 在线 ' + (state.health && state.health.version ? 'v' + state.health.version : '');
      chip.setAttribute('data-tone', 'ok');
    } else {
      chip.textContent = '● 离线';
      chip.setAttribute('data-tone', 'error');
    }
    chip.title = state.online
      ? 'GET /api/health → ' + JSON.stringify(state.health)
      : '无法连接后端 /api/health（界面可离线浏览）';
  }

  function renderMode() {
    Array.prototype.forEach.call(refs.modeSwitch.querySelectorAll('button'), function (b) {
      b.classList.toggle('on', b.getAttribute('data-mode') === state.mode);
    });
    if (state.mode === 'multi') refs.orchField.removeAttribute('hidden');
    else refs.orchField.setAttribute('hidden', '');
    refs.orchestration.value = state.orchestration;
    refs.modeChip.textContent = state.mode === 'multi'
      ? '多代理 · ' + state.orchestration
      : '单代理';
  }

  /* -------------------- 会话列表：按项目分组 + 折叠（U8） -------------------- */

  var UNASSIGNED_KEY = '__unassigned__';
  var GROUP_COLLAPSE_KEY = 'ctfa.convGroupsCollapsed';

  function collapsedGroupKeys() {
    try {
      var raw = global.localStorage.getItem(GROUP_COLLAPSE_KEY);
      var arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr : [];
    } catch (err) { return []; }
  }

  function persistCollapsedGroup(key, collapsed) {
    var set = collapsedGroupKeys().filter(function (k) { return k !== key; });
    if (collapsed) set.push(key);
    try { global.localStorage.setItem(GROUP_COLLAPSE_KEY, JSON.stringify(set)); } catch (err) { /* ignore */ }
  }

  function projectNameById(projectId) {
    var projects = (CTFA.projects && CTFA.projects.state && CTFA.projects.state.projects) || [];
    for (var i = 0; i < projects.length; i++) {
      if (projects[i].id === projectId) return projects[i].name || projects[i].id;
    }
    return projectId;
  }

  function activeConversation() {
    var id = state.conversationId;
    if (!id) return null;
    for (var i = 0; i < state.conversations.length; i++) {
      if (state.conversations[i].id === id) return state.conversations[i];
    }
    return null;
  }

  /** 当前会话所属项目 id（无会话或未归属时返回 ''） */
  function activeProjectId() {
    var conversation = activeConversation();
    return (conversation && conversation.projectId) || '';
  }

  /** 把会话按项目分桶：「未归属项目」恒置顶，其后为各项目（按项目列表顺序） */
  function groupConversations() {
    var projects = (CTFA.projects && CTFA.projects.state && CTFA.projects.state.projects) || [];
    var buckets = {};
    var order = [];
    projects.forEach(function (project) {
      buckets[project.id] = { key: project.id, projectId: project.id, name: project.name || project.id, items: [] };
      order.push(project.id);
    });
    var unassigned = { key: UNASSIGNED_KEY, projectId: '', name: '未归属项目', items: [] };
    state.conversations.forEach(function (conversation) {
      var pid = conversation.projectId || '';
      if (!pid) { unassigned.items.push(conversation); return; }
      if (!buckets[pid]) {   // 项目列表尚未加载 / 项目已被删除
        buckets[pid] = { key: pid, projectId: pid, name: projectNameById(pid), items: [] };
        order.push(pid);
      }
      buckets[pid].items.push(conversation);
    });
    var groups = order
      .map(function (key) { return buckets[key]; })
      .filter(function (group) { return group && group.items.length; });
    // 「未归属项目」恒置顶，便于第一时间看到尚未归类的会话
    if (unassigned.items.length) groups.unshift(unassigned);
    return groups;
  }

  /** 单个会话项（含 U2b 删除/勾选控件与「归属」入口） */
  function conversationItem(conversation) {
    var li = doc.createElement('li');
    li.className = 'list-item conv-item' + (conversation.id === state.conversationId ? ' on' : '');
    li.setAttribute('data-id', conversation.id);
    var main = doc.createElement('div');
    main.className = 'li-main';
    main.appendChild(span('li-title', conversation.title || conversation.id));
    // 时间统一格式化为「YYYY-MM-DD HH:MM:SS」；旧实现直接拼接毫秒时间戳
    var updated = fmtDateTime(conversation.updatedAt || conversation.createdAt);
    var sub = [
      conversation.challengeId ? '题目 ' + conversation.challengeId : '',
      updated
    ].filter(Boolean).join(' · ');
    if (sub) main.appendChild(span('li-sub', sub));
    li.appendChild(main);
    li.title = (conversation.title || conversation.id)
      + (conversation.createdAt ? '\n创建于 ' + fmtDateTime(conversation.createdAt) : '')
      + (conversation.updatedAt ? '\n更新于 ' + fmtDateTime(conversation.updatedAt) : '');
    li.addEventListener('click', function () {
      if (CTFA.conversations && CTFA.conversations.state.mode) return;   // 批量模式下不打开会话
      openConversation(conversation.id);
    });
    if (CTFA.conversations) CTFA.conversations.decorate(li, conversation);  // U2b：删除/勾选控件
    if (!(CTFA.conversations && CTFA.conversations.state.mode)) {
      li.appendChild(moveButton(conversation));                            // U8：归属到项目
    }
    return li;
  }

  /** 一个项目分组（可折叠） */
  function groupItem(group, isCollapsed) {
    var li = doc.createElement('li');
    li.className = 'conv-group' + (isCollapsed ? ' collapsed' : '');
    li.setAttribute('data-group', group.key);

    var head = doc.createElement('button');
    head.type = 'button';
    head.className = 'conv-group-head';
    head.setAttribute('aria-expanded', String(!isCollapsed));
    head.title = isCollapsed ? '展开分组' : '收起分组';
    head.appendChild(span('conv-group-caret', '▾'));
    head.appendChild(span('conv-group-name', group.name));
    head.appendChild(span('conv-group-count', String(group.items.length)));
    head.addEventListener('click', function (evt) {
      evt.preventDefault();
      evt.stopPropagation();
      var nowCollapsed = !li.classList.contains('collapsed');
      li.classList.toggle('collapsed', nowCollapsed);
      head.setAttribute('aria-expanded', String(!nowCollapsed));
      head.title = nowCollapsed ? '展开分组' : '收起分组';
      persistCollapsedGroup(group.key, nowCollapsed);
    });
    li.appendChild(head);

    var ul = doc.createElement('ul');
    ul.className = 'conv-group-list';
    group.items.forEach(function (conversation) { ul.appendChild(conversationItem(conversation)); });
    li.appendChild(ul);
    return li;
  }

  function renderConversations() {
    var box = refs.conversationList;
    box.textContent = '';
    if (!state.conversations.length) {
      box.appendChild(emptyLi('暂无会话，发送第一条消息即可创建'));
      return;
    }
    var collapsed = collapsedGroupKeys();
    // 当前会话所在分组始终展开，避免"切了会话却看不到它"
    var conversation = activeConversation();
    var activeKey = conversation ? (conversation.projectId || UNASSIGNED_KEY) : '';
    if (activeKey && collapsed.indexOf(activeKey) >= 0) {
      collapsed = collapsed.filter(function (k) { return k !== activeKey; });
      persistCollapsedGroup(activeKey, false);
    }
    groupConversations().forEach(function (group) {
      box.appendChild(groupItem(group, collapsed.indexOf(group.key) >= 0));
    });
  }

  function renderFacts() {
    var box = refs.factList;
    var q = state.factQuery.trim().toLowerCase();
    var list = state.facts.filter(function (f) {
      if (!q) return true;
      return [f.key, f.summary, (f.tags || []).join(' ')].join(' ').toLowerCase().indexOf(q) !== -1;
    });
    box.textContent = '';
    if (!list.length) {
      box.appendChild(emptyLi(state.facts.length ? '无匹配 facts' : '黑板为空（选择会话后加载）'));
      return;
    }
    list.forEach(function (f) {
      var li = doc.createElement('li');
      li.className = 'list-item' + (f.key === state.factKey ? ' on' : '');
      var main = doc.createElement('div');
      main.className = 'li-main';
      main.appendChild(span('li-title mono', f.key));
      if (f.summary) main.appendChild(span('li-sub', f.summary));
      var tags = Array.isArray(f.tags) ? f.tags : [];
      if (tags.length) {
        var chips = doc.createElement('div');
        chips.className = 'chips';
        tags.forEach(function (t) { chips.appendChild(badge(String(t), 'chip')); });
        main.appendChild(chips);
      }
      li.appendChild(main);
      li.addEventListener('click', function () { showFact(f.key); });
      box.appendChild(li);
    });
  }

  async function showFact(key) {
    state.factKey = key;
    renderFacts();
    var box = refs.factDetail;
    box.textContent = '加载中…';
    try {
      var res = await CTFA.api.fact(key, state.conversationId);
      var f = (res && res.fact) || {};
      box.textContent = '';
      var head = doc.createElement('div');
      head.className = 'detail-head';
      head.appendChild(span('li-title mono', f.key || key));
      var close = doc.createElement('button');
      close.className = 'link-btn';
      close.textContent = '关闭';
      close.addEventListener('click', function () { state.factKey = null; box.textContent = ''; renderFacts(); });
      head.appendChild(close);
      box.appendChild(head);
      var meta = doc.createElement('div');
      meta.className = 'li-sub';
      meta.textContent = [f.createdAt ? '创建 ' + f.createdAt : '', f.updatedAt ? '更新 ' + f.updatedAt : ''].filter(Boolean).join(' · ');
      box.appendChild(meta);
      var body = doc.createElement('div');
      body.className = 'md detail-body';
      var text = (f.summary ? '**' + f.summary + '**\n\n' : '') + (f.body || '');
      body.innerHTML = CTFA.markdown.render(text);
      box.appendChild(body);
    } catch (err) {
      box.textContent = '加载失败：' + err.message;
    }
  }

  function renderFlags() {
    var box = refs.flagList;
    box.textContent = '';
    if (!state.flags.length) {
      box.appendChild(emptyLi('暂无 flag'));
      return;
    }
    state.flags.forEach(function (f) {
      var li = doc.createElement('li');
      li.className = 'list-item flag-item';
      var main = doc.createElement('div');
      main.className = 'li-main';
      var row = doc.createElement('div');
      row.className = 'li-row';
      row.appendChild(span('li-title mono flag-value', f.value || ''));
      row.appendChild(badge(f.validated ? '已验证' : '未验证', f.validated ? 'on' : 'warn'));
      main.appendChild(row);
      var sub = [f.format, f.challengeId, f.submittedAt].filter(Boolean).join(' · ');
      if (sub) main.appendChild(span('li-sub', sub));
      var copy = doc.createElement('button');
      copy.className = 'link-btn';
      copy.textContent = '复制';
      copy.addEventListener('click', function (e) {
        e.stopPropagation();
        if (global.navigator.clipboard) global.navigator.clipboard.writeText(f.value || '');
      });
      main.appendChild(copy);
      li.appendChild(main);
      box.appendChild(li);
    });
  }

  function renderChallengeSelect() {
    var sel = refs.challengeSelect;
    if (!sel) return;  // 侧边栏「当前题目」入口已移除（题目数据保留供看板/控制台）
    sel.textContent = '';
    var none = doc.createElement('option');
    none.value = '';
    none.textContent = '（未选择）';
    sel.appendChild(none);
    state.challenges.forEach(function (c) {
      var o = doc.createElement('option');
      o.value = c.id;
      o.textContent = (c.name || c.id) + (c.category ? ' · ' + c.category : '');
      sel.appendChild(o);
    });
    sel.value = state.challengeId || '';
  }

  /* ------------------------------ 题目模块（U13） ------------------------------ */
  // 列表 / 创建 / 批量启动自主解题 全部委托给 js/challenges.js
  // （声明式渲染 + 并发调度，并发上限取自配置 challenge.max_concurrent_runs）。
  /** 题目视图渲染：列表 / 作战图 两种模式（模式持久化，切回列表保留滚动与勾选）。 */
  function renderChallengesView() {
    // 作战图模式已移除（U44）：题目视图只有列表模式
    if (refs.chModeBar) {
      Array.prototype.forEach.call(refs.chModeBar.querySelectorAll('button[data-chmode]'), function (btn) {
        btn.classList.toggle('on', btn.getAttribute('data-chmode') === 'list');
      });
    }
    if (refs.chBoardView) refs.chBoardView.hidden = true;
    if (refs.chListView) refs.chListView.hidden = false;
    if (!refs.challengesView) return;  // 题目视图入口已移除
    if (!CTFA.challengesView || typeof CTFA.challengesView.render !== 'function') return;
    CTFA.challengesView.render(refs.challengesView, {
      challenges: state.challenges,
      conversations: state.conversations,
      config: state.config,
      selectedId: state.challengeId,
      reload: loadChallenges,
      onSelect: function (id) { setSelectedChallenge(id); setRoute('/chat'); }
    });
  }

  var CH_MODE_KEY = 'ctfa.challengeMode';
  function challengeMode() {
    return 'list';
  }

  function setChallengeMode(mode) {
    // 作战图模式已移除：清掉历史 localStorage 值，只渲染列表
    try { global.localStorage.removeItem(CH_MODE_KEY); } catch (e) { /* 隐私模式忽略 */ }
    renderChallengesView();
  }

  /** "从当前会话创建题目"卡片：把已有对话（含附件与已有结论）升级为题目，控制台接手不重复劳动。 */
  /** 统一的题目选择入口：state/下拉框/卡片/控制台四处状态一次同步到位。 */
  function setSelectedChallenge(id) {
    state.challengeId = id || null;
    renderChallengeSelect();
    renderChallengesView();
  }


  function renderToolsView() {
    if (CTFA.admin) { CTFA.admin.render(); return; }
    var box = refs.toolsView;
    box.textContent = '';
    if (!state.tools.length) { box.appendChild(emptyLi('暂无工具')); return; }
    var table = doc.createElement('table');
    table.className = 'data-table';
    var thead = doc.createElement('thead');
    var hr = doc.createElement('tr');
    ['名称', '分类', '状态', '说明'].forEach(function (h) {
      var th = doc.createElement('th');
      th.textContent = h;
      hr.appendChild(th);
    });
    thead.appendChild(hr);
    table.appendChild(thead);
    var tbody = doc.createElement('tbody');
    state.tools.forEach(function (t) {
      var tr = doc.createElement('tr');
      [t.name, t.category || '-', (t.available === false ? '不可用' : '可用') + (t.enabled === false ? ' / 已禁用' : ''), t.description || '']
        .forEach(function (v, i) {
          var td = doc.createElement('td');
          td.textContent = String(v);
          if (i === 0) td.className = 'mono';
          tr.appendChild(td);
        });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    box.appendChild(table);
  }

  function renderAgentsView() {
    if (CTFA.admin) { CTFA.admin.render(); return; }
    var box = refs.agentsView;
    box.textContent = '';
    if (!state.agents.length) { box.appendChild(emptyLi('暂无子代理定义')); return; }
    state.agents.forEach(function (a) {
      var card = doc.createElement('article');
      card.className = 'challenge-card';
      card.appendChild(span('li-title', a.name || a.id));
      card.appendChild(span('li-sub mono', a.id || ''));
      if (a.description) card.appendChild(span('li-sub', a.description));
      var tools = Array.isArray(a.tools) ? a.tools : [];
      var chips = doc.createElement('div');
      chips.className = 'chips';
      tools.forEach(function (t) { chips.appendChild(badge(typeof t === 'string' ? t : (t.name || '?'), 'chip')); });
      if (!tools.length) chips.appendChild(badge('继承全部工具', 'chip'));
      card.appendChild(chips);
      if (a.maxIterations) card.appendChild(span('li-sub', 'maxIterations: ' + a.maxIterations));
      box.appendChild(card);
    });
  }

  /** 还没有会话时先建一个（用于"先选项目再开聊"）：返回会话 id */
  function ensureConversation() {
    if (state.conversationId) return Promise.resolve(state.conversationId);
    return CTFA.api.createConversation('新会话').then(function (res) {
      var conversation = (res && res.conversation) || {};
      if (!conversation.id) return '';
      state.conversationId = conversation.id;
      if (CTFA.attachments) CTFA.attachments.setConversation(conversation.id);
      if (CTFA.hitl && typeof CTFA.hitl.loadReviewer === 'function') CTFA.hitl.loadReviewer();
      setRoute('/c/' + encodeURIComponent(conversation.id), true);
      setRoute('/chat', true);
      return loadConversations().then(function () { return conversation.id; });
    });
  }

  /** 当前会话变化后刷新"项目归属"下拉（打开会话/新建会话/收到会话事件时都要调） */
  function syncProjectSelect() {
    if (CTFA.projects && typeof CTFA.projects.renderProjectSelect === 'function') {
      CTFA.projects.renderProjectSelect();
    }
  }

  function renderConfigEditor() {
    // U7：配置页改为表单化渲染，委托给 js/config.js
    if (CTFA.configForm && typeof CTFA.configForm.render === 'function') {
      CTFA.configForm.render(state.config || {});
    }
  }

  function renderRunStatus() {
    if (state.running) {
      refs.runStatus.textContent = '运行中…';
      refs.runStatus.setAttribute('data-tone', 'running');
      refs.btnSend.disabled = false;   // 运行中发送 = 入队引导（不再禁用输入）
      refs.btnStop.disabled = false;
      refs.btnSend.setAttribute('data-mode', 'stop');
      refs.btnSend.title = '停止';
      refs.btnSend.setAttribute('aria-label', '停止');
      refs.input.setAttribute('placeholder', '任务运行中——可继续输入指令，将在当前步骤结束后送达');
    } else {
      refs.runStatus.textContent = state.conversationId ? '就绪 · 会话 ' + state.conversationId : '就绪';
      refs.runStatus.setAttribute('data-tone', 'ok');
      refs.btnSend.disabled = false;
      refs.btnSend.setAttribute('data-mode', 'send');
      refs.btnSend.title = '发送';
      refs.btnSend.setAttribute('aria-label', '发送');
      refs.btnStop.disabled = true;
      refs.input.setAttribute('placeholder', '描述目标或直接粘贴题目… (Ctrl / Cmd + Enter 发送)');
    }
    renderQueueHint();
  }

  /** 队列计数提示：输入框上方"还有 N 条排队指令" */
  function renderQueueHint() {
    if (!refs.queueHint) return;
    if (state.running && state.queuedCount > 0) {
      refs.queueHint.textContent = '⏳ 还有 ' + state.queuedCount + ' 条排队指令，将在当前步骤结束后送达';
      refs.queueHint.hidden = false;
    } else {
      refs.queueHint.hidden = true;
      refs.queueHint.textContent = '';
    }
  }

  function renderUsageBadge() {
    CTFA.timeline.renderUsage(state.usage);
  }

  /* -------------------------------- 路由 --------------------------------- */

  function setRoute(path, silent) {
    var hash = '#' + path;
    if (global.location.hash !== hash) {
      if (silent) global.history.replaceState(null, '', hash);
      else global.location.hash = hash;
    }
    applyRoute(path);
  }

  function applyRoute(path) {
    var m = /^\/c\/(.+)$/.exec(path);
    if (m) {
      var id = decodeURIComponent(m[1]);
      if (id !== state.conversationId) openConversation(id);
      path = '/chat';
    }
    // U4：进入项目视图时刷新项目列表与聚合视图
    if (path === '/projects' && CTFA.projects && typeof CTFA.projects.load === 'function') {
      CTFA.projects.load().then(function () {
        return CTFA.projects.loadSummary(CTFA.projects.state.currentProjectId);
      });
    }
    // M9：进入竞赛指挥台时加载竞赛/题目/黑板/能力
    if (path === '/competition' && CTFA.competitionView && typeof CTFA.competitionView.load === 'function') {
      CTFA.competitionView.load();
    }
    // U14：进入数据看板时拉取一次统计快照
    if (path === '/dashboard' && CTFA.dashboardView) {
      CTFA.dashboardView.render(refs.dashboardView);
      CTFA.dashboardView.load();
    }
    // 题目模块入口已移除：直接访问 /challenges 回落对话页
    // （题目数据、看板统计、单题控制台均不受影响）
    if (path === '/challenges') path = '/chat';
    state.route = path;
    var view = (path.replace(/^\//, '') || 'chat');
    // U37：输入框只在对话视图显示（配置/工具/子代理等页面不出现底部对话框）
    doc.body.classList.toggle('route-chat', view === 'chat');
    // U38：侧边栏仅对话模块显示——离开对话时关闭抽屉（紧凑模式左栏即抽屉，
    // 避免带着抽屉遮罩跳到其他模块）
    if (view !== 'chat') closeDrawer();
    Array.prototype.forEach.call(doc.querySelectorAll('.view'), function (v) {
      if (v.getAttribute('data-view') === view) v.removeAttribute('hidden');
      else v.setAttribute('hidden', '');
    });
    // U9：顶栏导航与抽屉导航同步高亮
    [refs.viewTabs, refs.sideNav].forEach(function (box) {
      if (!box) return;
      Array.prototype.forEach.call(box.querySelectorAll('button[data-route]'), function (b) {
        b.classList.toggle('on', b.getAttribute('data-route') === path);
      });
    });
  }

  function route() {
    var path = global.location.hash.replace(/^#/, '') || '/chat';
    applyRoute(path);
  }

  /* ------------------- 平板 / 移动端抽屉菜单（U9） ------------------- */

  /** 抽屉断点：与 css/style.css 的 @media (max-width:1024px) 保持一致 */
  var DRAWER_MAX_WIDTH = 1024;

  function drawerOpen() { return doc.body.classList.contains('drawer-open'); }

  function setDrawer(open) {
    doc.body.classList.toggle('drawer-open', !!open);
    if (refs.btnMenu) refs.btnMenu.setAttribute('aria-expanded', String(!!open));
  }

  function closeDrawer() { if (drawerOpen()) setDrawer(false); }

  function initDrawer() {
    if (refs.btnMenu) {
      refs.btnMenu.addEventListener('click', function () { setDrawer(!drawerOpen()); });
    }
    if (refs.btnDrawerClose) refs.btnDrawerClose.addEventListener('click', closeDrawer);
    if (refs.drawerBackdrop) refs.drawerBackdrop.addEventListener('click', closeDrawer);
    // 抽屉内点击「导航链接」或「会话项」后自动收起
    var sidebar = doc.querySelector('.sidebar-left');
    if (sidebar) {
      sidebar.addEventListener('click', function (evt) {
        if (!drawerOpen()) return;
        if (evt.target.closest('button[data-route]') || evt.target.closest('.list-item')) closeDrawer();
      });
    }
    doc.addEventListener('keydown', function (evt) {
      if (evt.key === 'Escape') closeDrawer();
    });
    global.addEventListener('resize', function () {
      if (global.innerWidth > DRAWER_MAX_WIDTH) closeDrawer();
    });
  }

  /* ------------------- 极简隐藏模式（U10） ------------------- */

  /**
   * 打开极简隐藏模式：新窗口 + 尽量隐藏浏览器自带界面（地址栏/工具栏/菜单栏）。
   * 固定窗口名，重复点击聚焦同一窗口；新窗口内首次交互会尝试进入全屏。
   */
  function openMinimalMode() {
    var w = 560, h = 720;
    var sx = typeof global.screenX === 'number' ? global.screenX : 0;
    var sy = typeof global.screenY === 'number' ? global.screenY : 0;
    var ow = global.outerWidth || w;
    var oh = global.outerHeight || h;
    var left = Math.max(0, Math.round(sx + (ow - w) / 2));
    var top = Math.max(0, Math.round(sy + (oh - h) / 2));
    var features = 'popup=yes,width=' + w + ',height=' + h +
      ',left=' + left + ',top=' + top +
      ',menubar=no,toolbar=no,location=no,status=no,resizable=yes,scrollbars=no';
    var win = global.open('min.html', 'ctfa_min', features);
    if (win) {
      try { win.focus(); } catch (err) { /* ignore */ }
    } else {
      CTFA.timeline.notice('浏览器拦截了新窗口：请允许弹出窗口后重试', 'warn');
    }
  }

  /* ------------------------------ 数据加载 ------------------------------ */

  async function loadHealth() {
    try {
      state.health = await CTFA.api.health();
      // 只要 /api/health 有响应即视为在线（status 由后端给出，前端仅展示）
      state.online = !!state.health;
    } catch (err) {
      state.health = null;
      state.online = false;
    }
    renderHealth();
  }

  async function loadTools() {
    try {
      var res = await CTFA.api.tools();
      state.tools = (res && res.tools) || [];
    } catch (err) { state.tools = []; }
    renderToolsView();
  }

  async function loadAgents() {
    try {
      var res = await CTFA.api.agents();
      state.agents = (res && res.agents) || [];
    } catch (err) { state.agents = []; }
    renderAgentsView();
  }

  async function loadConversations() {
    try {
      var res = await CTFA.api.conversations();
      state.conversations = (res && res.conversations) || [];
    } catch (err) { state.conversations = []; }
    renderConversations();
    if (CTFA.projects) {
      CTFA.projects.renderProjectSelect();   // U4：项目归属选择器
      // U8：会话按项目分组需要项目名；首次拿到项目后重绘一次分组标题
      if (!CTFA.projects.state.loaded) {
        CTFA.projects.load().then(function () { renderConversations(); });
      }
    }
  }

  async function loadChallenges() {
    try {
      var res = await CTFA.api.challenges();
      state.challenges = (res && res.challenges) || [];
    } catch (err) { state.challenges = []; }
    renderChallengeSelect();
    renderChallengesView();
  }

  async function loadConfig() {
    var res = null;
    try {
      res = await CTFA.api.getConfig();
      state.config = (res && res.config) || {};
    } catch (err) { state.config = {}; }
    state.configPath = (res && res.configPath) || '';
    state.writableConfigPath = (res && res.writableConfigPath) || '';
    state.apiKeyPlaceholders = (res && res.apiKeyPlaceholderChannels) || [];
    renderConfigEditor();
    renderConfigPathHint();
  }

  function renderConfigPathHint() {
    if (!refs.configPathHint) return;
    // 只显示文件名（项目根目录），不在界面上暴露服务器绝对路径
    var current = (state.configPath || '').split('/').pop();
    var writable = (state.writableConfigPath || 'config.yaml').split('/').pop();
    refs.configPathHint.textContent = current
      ? '配置文件：' + current + '（项目根目录，保存会写回这里）'
      : '配置文件：（尚未创建）保存时会创建 ' + (writable || 'config.yaml');
    if (refs.configEnvHint) {
      var placeholders = state.apiKeyPlaceholders || [];
      refs.configEnvHint.hidden = !placeholders.length;
      if (placeholders.length) {
        refs.configEnvHint.textContent =
          '⚠ 检测到 ' + placeholders.join('、') + ' 通道的 api_key 仍是 ${VAR} 写法：'
          + '本版本不再从环境变量读取密钥，请在下方填写真实密钥后点「保存并写入本地」。';
      }
    }
  }

  async function loadFacts() {
    try {
      var res = await CTFA.api.facts(state.conversationId, state.factQuery);
      state.facts = (res && res.facts) || [];
    } catch (err) { state.facts = []; }
    renderFacts();
  }

  async function loadFlags() {
    try {
      // 题目维度聚合优先：flag 常落在 explore 子会话，从题目跟踪进来时
      // 当前会话（主会话）按会话查会是空——按 challenge 拉全部子会话的 flag
      var conv = state.conversations.filter(function (c) {
        return c.id === state.conversationId;
      })[0];
      var cid = (conv && conv.challengeId) || state.challengeId || null;
      var res = cid
        ? await CTFA.api.flagsByChallenge(cid)
        : await CTFA.api.flags(state.conversationId);
      state.flags = (res && res.flags) || [];
    } catch (err) { state.flags = []; }
    renderFlags();
  }

  async function openConversation(id) {
    state.conversationId = id;
    syncProjectSelect();
    if (CTFA.hitl && typeof CTFA.hitl.loadReviewer === 'function') CTFA.hitl.loadReviewer();
    state.factKey = null;
    state.messages = [];
    refs.factDetail.textContent = '';
    // 切换会话：断开旧会话的事件流并复位引导视图状态
    abortSteerStream();
    state.steer.lastEventId = 0;
    state.queuedCount = 0;
    state.cancelled = false;
    state.running = false;
    state.runId = null;
    setRoute('/c/' + encodeURIComponent(id), true);
    setRoute('/chat', true);
    renderConversations();
    renderRunStatus();

    CTFA.timeline.clear();
    try {
      var res = await CTFA.api.messages(id);
      state.messages = (res && res.messages) || [];
      // 会话里若带 challengeId，则同步选题（含控制台模块）
      var conv = state.conversations.filter(function (c) { return c.id === id; })[0];
      if (conv && conv.challengeId) {
        setSelectedChallenge(conv.challengeId);
      }
      // 恢复该会话绑定的模型（未绑定则继承上次选择并补绑）
      if (CTFA.models && typeof CTFA.models.setConversation === 'function') {
        CTFA.models.setConversation(conv || { id: id });
      }
      // U2a：先拉附件再渲染历史，保证气泡里的附件卡片一次到位
      if (CTFA.attachments) {
        CTFA.attachments.setConversation(id);
        await CTFA.attachments.loadForConversation(id);
        var byMessage = CTFA.attachments.state.byMessage;
        state.messages.forEach(function (msg) {
          if (msg && msg.role === 'user' && byMessage[msg.id]) msg.attachments = byMessage[msg.id];
        });
        CTFA.timeline.renderHistory(state.messages);
      } else {
        CTFA.timeline.renderHistory(state.messages);
      }
    } catch (err) {
      CTFA.timeline.notice('加载历史消息失败：' + err.message, 'error');
    }
    loadFacts();
    loadFlags();
    // 刷新/重连恢复：任务运行中 + 队列视图（角标与计数）
    recoverSteerState();
  }

  function newConversation() {
    state.conversationId = null;
    syncProjectSelect();
    if (CTFA.attachments) { CTFA.attachments.setConversation(''); CTFA.attachments.clearPending(); }
    if (CTFA.hitl && typeof CTFA.hitl.loadReviewer === 'function') CTFA.hitl.loadReviewer();
    state.messages = [];
    state.facts = [];
    state.flags = [];
    state.factKey = null;
    state.runId = null;
    abortSteerStream();
    state.steer.lastEventId = 0;
    state.queuedCount = 0;
    state.running = false;
    refs.factDetail.textContent = '';
    CTFA.timeline.clear();
    setRoute('/chat');
    renderConversations();
    renderFacts();
    renderFlags();
    renderRunStatus();
    refs.input.focus();
  }

  var creatingConversation = false;
  /**
   * 「＋ 新建」：立即创建一个真实会话并打开。
   * U8：若当前正停留在某个项目的会话中，新会话**默认归属同一个项目**；
   *     否则归入「未归属项目」。归属成功后同步刷新项目模块的会话数。
   */
  async function createNewConversation() {
    if (creatingConversation) return;
    creatingConversation = true;
    try {
      var inheritProjectId = activeProjectId();   // 当前会话所属项目（无则 ''）
      var res = await CTFA.api.createConversation('新会话');
      var conversation = (res && res.conversation) || {};
      if (!conversation.id) throw new Error('后端未返回会话 ID');
      if (inheritProjectId) {
        try {
          await CTFA.api.request('/api/projects/' + encodeURIComponent(inheritProjectId) + '/assign', {
            method: 'POST', body: { conversationIds: [conversation.id], action: 'assign' }
          });
        } catch (assignErr) {
          CTFA.timeline.notice('新会话未能归属到原项目：' + ((assignErr && assignErr.message) || assignErr), 'warn');
          inheritProjectId = '';
        }
      }
      await loadConversations();
      await syncProjectsModule();                 // 顶栏「项目」模块同步新增会话
      await openConversation(conversation.id);
      CTFA.timeline.notice(
        inheritProjectId
          ? '已新建会话，归属项目：' + projectNameById(inheritProjectId)
          : '已新建会话（未归属项目）',
        'ok'
      );
    } catch (err) {
      CTFA.timeline.notice('新建会话失败：' + ((err && err.message) || err), 'error');
    } finally {
      creatingConversation = false;
    }
  }

  /** 刷新项目模块（列表会话数 + 当前打开项目的聚合视图） */
  async function syncProjectsModule() {
    if (!CTFA.projects || typeof CTFA.projects.load !== 'function') return;
    try {
      await CTFA.projects.load();
      if (CTFA.projects.state.currentProjectId) {
        await CTFA.projects.loadSummary(CTFA.projects.state.currentProjectId);
      }
    } catch (err) { /* 项目模块刷新失败不影响主流程 */ }
  }

  /* ------------------------- 会话「归属到项目」（U8） ------------------------- */

  function moveButton(conversation) {
    var btn = doc.createElement('button');
    btn.type = 'button';
    btn.className = 'li-move';
    btn.textContent = '⇄';
    btn.title = '把该会话归属到其它项目';
    btn.setAttribute('aria-label', '归属会话 ' + (conversation.title || conversation.id));
    btn.addEventListener('click', function (evt) {
      evt.stopPropagation();
      evt.preventDefault();
      openMoveDialog(conversation);
    });
    return btn;
  }

  /** 归属选择弹窗：项目下拉 + 「未归属项目」 */
  function openMoveDialog(conversation) {
    var projects = (CTFA.projects && CTFA.projects.state && CTFA.projects.state.projects) || [];
    var current = conversation.projectId || '';
    var mask = doc.createElement('div');
    mask.className = 'modal-mask';
    var box = doc.createElement('div');
    box.className = 'modal-box';
    box.appendChild(span('modal-title', '归属到项目'));
    var body = doc.createElement('div');
    body.className = 'modal-body';
    body.appendChild(span('modal-line', '会话：' + (conversation.title || conversation.id)));
    var field = doc.createElement('label');
    field.className = 'form-field';
    field.appendChild(span('form-label', '选择归属项目'));
    var select = doc.createElement('select');
    var none = doc.createElement('option');
    none.value = '';
    none.textContent = '未归属项目';
    select.appendChild(none);
    projects.forEach(function (project) {
      var option = doc.createElement('option');
      option.value = project.id;
      option.textContent = project.name || project.id;
      select.appendChild(option);
    });
    select.value = current;
    field.appendChild(select);
    body.appendChild(field);
    if (!projects.length) {
      body.appendChild(span('modal-line', '还没有项目：先到顶栏「项目」里新建一个。'));
    }
    box.appendChild(body);

    var actions = doc.createElement('div');
    actions.className = 'modal-actions';
    var cancel = doc.createElement('button');
    cancel.type = 'button';
    cancel.className = 'btn';
    cancel.textContent = '取消';
    var save = doc.createElement('button');
    save.type = 'button';
    save.className = 'btn primary';
    save.textContent = '保存';
    var onKey = function (evt) { if (evt.key === 'Escape') close(); };
    function close() {
      mask.remove();
      doc.removeEventListener('keydown', onKey);
    }
    cancel.addEventListener('click', close);
    mask.addEventListener('click', function (evt) { if (evt.target === mask) close(); });
    doc.addEventListener('keydown', onKey);
    save.addEventListener('click', function () {
      var target = select.value;
      close();
      if (target === current) return;
      moveConversationToProject(conversation, target);
    });
    actions.appendChild(cancel);
    actions.appendChild(save);
    box.appendChild(actions);
    mask.appendChild(box);
    doc.body.appendChild(mask);
    select.focus();
  }

  /** 把会话移入目标项目（targetProjectId 为空串 = 解除归属） */
  async function moveConversationToProject(conversation, targetProjectId) {
    var from = conversation.projectId || '';
    try {
      if (targetProjectId) {
        await CTFA.api.request('/api/projects/' + encodeURIComponent(targetProjectId) + '/assign', {
          method: 'POST', body: { conversationIds: [conversation.id], action: 'assign' }
        });
      } else if (from) {
        await CTFA.api.request('/api/projects/' + encodeURIComponent(from) + '/assign', {
          method: 'POST', body: { conversationIds: [conversation.id], action: 'unassign' }
        });
      }
      CTFA.timeline.notice(
        targetProjectId ? '已归属到项目：' + projectNameById(targetProjectId) : '已移出项目（归入未归属项目）',
        'ok'
      );
      await loadConversations();
      await syncProjectsModule();
    } catch (err) {
      CTFA.timeline.notice('归属失败：' + ((err && err.message) || err), 'error');
    }
  }

  /* ------------------------------ 发送 / 停止 ------------------------------ */

  function buildConversationMessageBody(text) {
    // 统一消息入口：空闲发送启动任务（可带编排配置/附件）；后端按会话状态分流
    var body = { text: text };
    if (state.challengeId) body.challengeId = state.challengeId;
    body.mode = state.mode;
    if (state.mode === 'multi') body.orchestration = state.orchestration;
    var attachmentIds = CTFA.attachments ? CTFA.attachments.pendingIds() : [];
    if (attachmentIds.length) body.attachmentIds = attachmentIds;
    // 模型选择：带上当前选中的通道（空 = 后端走全局默认 failover 链）
    var modelChannel = CTFA.models ? CTFA.models.current() : '';
    if (modelChannel) body.model = modelChannel;
    return body;
  }

  /** 任务启动时的本地视图复位（与运行绑定的部分） */
  function beginLocalRun() {
    state.running = true;
    state.cancelled = false;
    state.runId = null;
    state.toolCards = {};
    state.todos = [];
    state.usage = { modelCalls: 0, promptTokens: 0, completionTokens: 0 };
    // 先断开旧流：POST 窗口内旧任务的迟到事件不得进入新运行视图
    abortSteerStream();
    renderRunStatus();
    CTFA.timeline.renderTodos([]);
    renderUsageBadge();
    CTFA.timeline.beginRun();
  }

  /** 运行收尾：done / task_done / 失败后统一走这里（事件流是长驻的，不再绑定 finally） */
  function finishLocalRun(stateText, tone) {
    state.running = false;
    state.controller = null;
    state.runId = null;
    state.queuedCount = 0;
    renderRunStatus();
    CTFA.timeline.finishRun(stateText, tone);
    // 收尾清扫：结果回执丢失的工具卡不再永远旋转
    if (CTFA.timeline.sweepRunningCards) CTFA.timeline.sweepRunningCards();
    if (state.conversationId) {
      loadConversations();
      loadFacts();
      loadFlags();
    }
  }

  async function sendMessage() {
    var text = refs.input.value.trim();
    if (!text) return;

    refs.input.value = '';
    autoGrow();

    if (state.running) {
      sendSteerMessage(text);      // 运行中发送 = 入队引导，不打断当前执行
      return;
    }
    await sendRunMessage(text);
  }

  /** 空闲发送：走统一消息入口启动任务，事件经会话级事件流接收 */
  async function sendRunMessage(text) {
    try {
      if (!state.conversationId) {
        var created = await CTFA.api.createConversation('新会话');
        var conversation = (created && created.conversation) || {};
        if (!conversation.id) throw new Error('后端未返回会话 ID');
        await loadConversations();
        state.conversationId = conversation.id;
        if (CTFA.models) CTFA.models.setConversation(conversation);
        if (CTFA.attachments) CTFA.attachments.setConversation(conversation.id);
      }

      beginLocalRun();
      localEcho = text;
      var echoAttachments = CTFA.attachments ? CTFA.attachments.pendingSummary() : [];
      CTFA.timeline.appendUserMessage(text, { attachments: echoAttachments });
      state.messages.push({ id: 'local-' + Date.now(), role: 'user', content: text, createdAt: new Date().toISOString() });

      var resp = await CTFA.api.sendConversationMessage(state.conversationId, buildConversationMessageBody(text));
      if (resp && resp.taskId) state.runId = resp.taskId;
      renderRunStatus();
      // 事件流接管后续 UI 更新（断线由 connectSteerStream 自动重连续传）
      connectSteerStream();
    } catch (err) {
      CTFA.timeline.showError(err && err.message ? err.message : String(err));
      finishLocalRun('发送失败', 'error');
    } finally {
      if (CTFA.attachments) {
        CTFA.attachments.clearPending();
        CTFA.attachments.loadForConversation(state.conversationId);
      }
    }
  }

  /** 运行中发送：入队（FIFO），气泡带"排队中"角标，安全点投递后翻转 */
  function sendSteerMessage(text) {
    if (CTFA.attachments && CTFA.attachments.pendingIds().length) {
      CTFA.timeline.notice('运行中发送的引导消息暂不支持附件，附件已保留待任务结束后发送', 'warn');
      return;
    }
    var card = CTFA.timeline.appendUserMessage(text, { steerStatus: 'queued' });
    // 本地气泡先渲染、后等服务端 id：message_queued 事件与 POST 回执谁先到都能对齐，不重不漏
    state.pendingSteerCard = card;
    state.messages.push({ id: 'local-steer-' + Date.now(), role: 'user', content: text, deliveryStatus: 'queued', createdAt: new Date().toISOString() });

    CTFA.api.sendConversationMessage(state.conversationId, { text: text }).then(function (resp) {
      resp = resp || {};
      if (resp.status === 'queued') {
        bindPendingSteerCard(resp.messageId);
        if (resp.messageId) CTFA.timeline.markSteerStatus(resp.messageId, 'queued');
        if (typeof resp.queueLength === 'number') state.queuedCount = resp.queueLength;
        else state.queuedCount += 1;
        renderQueueHint();
      } else if (resp.status === 'running') {
        // 竞态：发送瞬间任务刚好结束，后端把它作为新任务启动——翻转为普通触发消息
        bindPendingSteerCard(resp.messageId);
        if (resp.messageId) CTFA.timeline.markSteerStatus(resp.messageId, 'delivered');
        state.runId = resp.taskId || state.runId;
      }
    }).catch(function (err) {
      CTFA.timeline.notice('引导消息发送失败：' + ((err && err.message) || err), 'error');
    }).finally(function () {
      state.pendingSteerCard = null;
    });
  }

  /** 把服务端消息 id 绑到本地先渲染的引导气泡上（已绑定/不存在则忽略） */
  function bindPendingSteerCard(messageId) {
    if (!messageId || !state.pendingSteerCard) return false;
    var ok = CTFA.timeline.bindUserMessageId(state.pendingSteerCard, messageId);
    if (ok) state.pendingSteerCard = null;
    return ok;
  }

  function stopRun() {
    if (!state.running) return;
    CTFA.timeline.notice('正在请求取消任务…', 'warn');
    refs.runStatus.textContent = '取消中…';

    if (state.conversationId) {
      // 会话级取消：hard 立即中止（含进行中的工具）；排队消息保留，不静默丢弃
      CTFA.api.cancelConversationTask(state.conversationId, 'hard').catch(function (err) {
        CTFA.timeline.notice('取消请求失败：' + err.message, 'error');
      });
      // 兜底：若后端若干秒内未推送 task_done 并收尾，则本地断开事件流
      global.setTimeout(function () {
        if (state.running && state.steer.controller) {
          state.cancelled = true;
          state.steer.controller.abort();
        }
      }, 5000);
    } else if (state.controller) {
      state.cancelled = true;
      state.controller.abort();
    }
  }

  /* --------------------- 会话级事件流（Steering）管理 --------------------- */

  function abortSteerStream() {
    state.steer.wanted = false;
    if (state.steer.controller) {
      try { state.steer.controller.abort(); } catch (err) { /* ignore */ }
      state.steer.controller = null;
    }
  }

  /** 连接（或重连）会话级事件流；lastEventId 非空时服务端断点补发 */
  function connectSteerStream() {
    if (!state.conversationId) return;
    abortSteerStream();
    state.steer.wanted = true;
    var controller = new global.AbortController();
    state.steer.controller = controller;
    var fromId = state.steer.lastEventId;

    CTFA.api.streamConversationEvents(state.conversationId, steerStreamHandlers(), {
      controller: controller,
      lastEventId: fromId
    }).then(function (info) {
      state.steer.controller = null;
      // 意外断开（非本端 abort）时自动重连续传
      if (state.steer.wanted && info && !info.aborted && !state.cancelled) {
        global.setTimeout(function () {
          if (state.steer.wanted && state.conversationId) connectSteerStream();
        }, 1000);
      }
    }).catch(function () {
      state.steer.controller = null;
      if (state.steer.wanted) {
        global.setTimeout(function () {
          if (state.steer.wanted && state.conversationId) connectSteerStream();
        }, 2000);
      }
    });
  }

  function steerStreamHandlers() {
    var base = streamHandlers();
    // 会话级事件流是长驻的：断点补发/重连时可能重放已结束旧任务的事件。
    // 所有运行级事件都带 runId——与当前运行不符的一律丢弃，避免旧 done/task_done
    // 误杀刚启动的运行状态、旧增量重复渲染。
    var filtered = {};
    Object.keys(base).forEach(function (key) {
      var fn = base[key];
      if (typeof fn !== 'function') { filtered[key] = fn; return; }
      filtered[key] = function (data, raw, id) {
        if (data && data.runId && state.runId && data.runId !== state.runId) return;
        return fn(data, raw, id);
      };
    });
    filtered.onEvent = function (type, data, raw, id) {
      if (id) {
        var n = parseInt(id, 10);
        if (!isNaN(n) && n > state.steer.lastEventId) state.steer.lastEventId = n;
      }
      if (typeof base.onEvent === 'function') base.onEvent(type, data, raw, id);
    };
    return filtered;
  }

  /** 刷新/重开 会话后恢复"任务运行中 + 队列视图"（/state 是持久化真相源） */
  async function recoverSteerState() {
    if (!state.conversationId) return;
    try {
      var st = await CTFA.api.conversationState(state.conversationId);
      st = st || {};
      var queued = st.queuedMessages || [];
      if (st.taskStatus === 'running') {
        state.running = true;
        state.runId = st.runId || null;
        state.queuedCount = queued.length;
        state.steer.lastEventId = st.lastEventId || 0;
        CTFA.timeline.beginRun();
        renderRunStatus();
        queued.forEach(function (m) {
          if (m && m.id) CTFA.timeline.markSteerStatus(m.id, 'queued');
        });
        connectSteerStream();
      } else {
        state.queuedCount = 0;
        renderRunStatus();
        // 空闲时仍为 queued 的行 = 取消/中断遗留（未投递），角标提示用户可重发
        queued.forEach(function (m) {
          if (m && m.id) CTFA.timeline.markSteerStatus(m.id, 'off');
        });
      }
    } catch (err) { /* 状态恢复失败不阻塞会话浏览 */ }
  }

  /* ------------------------------ SSE 事件接线 ------------------------------ */

  function streamHandlers() {
    return {
      /* 会话与运行标识 */
      conversation: function (d) {
        d = d || {};
        if (d.conversationId) {
          state.conversationId = d.conversationId;
          if (CTFA.attachments) CTFA.attachments.setConversation(d.conversationId);
          setRoute('/c/' + encodeURIComponent(d.conversationId), true);
          setRoute('/chat', true);
          renderConversations();
          syncProjectSelect();
        }
        if (d.runId) state.runId = d.runId;
        if (d.challengeId) {
          state.challengeId = d.challengeId;
          renderChallengeSelect();
        }
        renderRunStatus();
      },

      /* 消息落库：仅记录进 state，DOM 已由流式渲染覆盖 */
      message_saved: function (d) {
        d = d || {};
        if (d.role === 'user' && localEcho !== null) { localEcho = null; return; }
        state.messages.push({ id: d.messageId, role: d.role, content: d.content, createdAt: new Date().toISOString() });
      },

      /* ---- 运行中引导（Steering）：队列事件驱动角标与计数 ---- */
      message_queued: function (d) {
        d = d || {};
        if (!d.id) return;
        // 本标签页已渲染（发送回执/重放去重）→ 只对齐角标；否则补一个排队气泡
        if (state.pendingSteerCard && CTFA.timeline.bindUserMessageId(state.pendingSteerCard, d.id)) {
          CTFA.timeline.markSteerStatus(d.id, 'queued');
        } else if (CTFA.timeline.hasUserCard(d.id)) {
          CTFA.timeline.markSteerStatus(d.id, 'queued');
        } else {
          CTFA.timeline.appendUserMessage(d.text || '', { messageId: d.id, steerStatus: 'queued' });
          state.messages.push({ id: d.id, role: 'user', content: d.text || '', deliveryStatus: 'queued', createdAt: new Date().toISOString() });
        }
        state.queuedCount = typeof d.queueLength === 'number' ? d.queueLength : state.queuedCount + 1;
        renderQueueHint();
      },
      message_delivered: function (d) {
        d = d || {};
        var ids = d.ids || [];
        ids.forEach(function (id) { CTFA.timeline.markSteerStatus(id, 'delivered'); });
        state.messages.forEach(function (m) {
          if (m && ids.indexOf(m.id) !== -1) m.deliveryStatus = 'delivered';
        });
        state.queuedCount = Math.max(0, state.queuedCount - ids.length);
        renderQueueHint();
      },
      steer_interrupt: function (d) {
        // 中断并继续：断开被掐断的流式气泡，提示用户消息已注入
        if (CTFA.timeline.breakStream) CTFA.timeline.breakStream();
        CTFA.timeline.notice('已中断当前执行，注入你的消息后继续解题', 'warn');
      },
      task_done: function (d) {
        d = d || {};
        // 取消场景：仍在"排队中"的消息永远不会投递 → 翻成"未投递"（消息保留在历史里）
        CTFA.timeline.setAllSteerStatus('queued', 'off');
        finishLocalRun(d.reason === 'cancelled' ? '已取消' : '已完成', d.reason === 'cancelled' ? 'warn' : 'ok');
      },
      task_error: function (d) {
        CTFA.timeline.showError((d && d.message) || '任务执行失败');
      },

      /* 进度 / 规划 / 任务清单 */
      progress: function (d) {
        d = d || {};
        CTFA.timeline.setProgress(d.stage, d.message, d.iteration);
      },
      planning: function (d) {
        CTFA.timeline.appendPlanning((d && d.content) || '');
      },
      todos: function (d) {
        state.todos = (d && d.todos) || [];
        CTFA.timeline.renderTodos(state.todos);
      },

      /* 思考流 */
      thinking_stream_delta: function (d) {
        CTFA.timeline.appendThinking((d && d.delta) || '');
      },

      /* 正文流 */
      response_start: function () {
        CTFA.timeline.beginResponse();
      },
      response_delta: function (d) {
        CTFA.timeline.appendResponse((d && d.delta) || '');
      },

      /* 工具调用与结果 */
      tool_call: function (d) {
        d = d || {};
        state.toolCards[d.toolCallId] = {
          name: d.name, arguments: d.arguments, agent: d.agent, status: 'running'
        };
        CTFA.timeline.upsertToolCall(d);
      },
      tool_result: function (d) {
        d = d || {};
        var rec = state.toolCards[d.toolCallId] || (state.toolCards[d.toolCallId] = {});
        rec.name = d.name || rec.name;
        rec.result = d.result;
        rec.isError = !!d.isError;
        rec.executionId = d.executionId;
        rec.persistedFile = d.persistedFile;
        rec.status = d.isError ? 'error' : 'ok';
        CTFA.timeline.setToolResult(d);
        // flag_submit 成功后自动刷新 flag 列表
        if (d.name === 'flag_submit' && !d.isError) loadFlags();
      },

      /* 子代理流式文本 */
      sub_agent_reply_stream_delta: function (d) {
        d = d || {};
        CTFA.timeline.appendSubAgentDelta(d.agentId, d.agentName, d.delta || '');
      },

      /* token 用量 */
      usage_summary: function (d) {
        d = d || {};
        state.usage = {
          modelCalls: d.modelCalls || 0,
          promptTokens: d.promptTokens || 0,
          completionTokens: d.completionTokens || 0
        };
        renderUsageBadge();
      },

      /* 人工审批 */
      hitl_request: function (d) {
        d = d || {};
        CTFA.timeline.markToolApproval(d.toolCallId);
        CTFA.timeline.showHitl(d, function (requestId, payload) {
          return CTFA.api.hitl(requestId, payload).then(function () {
            CTFA.timeline.notice('审批已提交：' + payload.decision, 'info');
            loadFacts();
          });
        });
      },

      /* U6c：自动裁决卡片（audit_agent / policy / whitelist） */
      hitl_auto_decision: function (d) {
        CTFA.timeline.appendAutoDecision(d || {});
      },

      /* 异常与取消 */
      error: function (d) {
        CTFA.timeline.showError((d && d.message) || '未知错误');
      },
      cancelled: function (d) {
        state.cancelled = true;
        CTFA.timeline.notice('运行已取消：' + ((d && d.reason) || '未提供原因'), 'warn');
      },

      /* 最终回复与结束 */
      response: function (d) {
        var content = (d && d.content) || '';
        CTFA.timeline.finalizeResponse(content);
        state.messages.push({ id: 'assistant-' + Date.now(), role: 'assistant', content: content, createdAt: new Date().toISOString() });
      },
      done: function (d) {
        d = d || {};
        if (d.conversationId && !state.conversationId) state.conversationId = d.conversationId;
        // 会话级事件流是长驻的：运行结束统一走 finishLocalRun（task_done 会再次幂等收尾）
        finishLocalRun(state.cancelled ? '已取消' : '已完成', state.cancelled ? 'warn' : 'ok');
      },

      /* 网络/解析异常 */
      onError: function (err) {
        CTFA.timeline.notice('事件处理异常：' + (err && err.message ? err.message : err), 'error');
      },
      onClose: function () {
        CTFA.timeline.flush();
      }
    };
  }

  /* ------------------------------ 配置保存 ------------------------------ */
  // U7：配置页已表单化（分区输入框/下拉/开关），「保存 / 撤销 / 重新加载 / 测试通道 /
  // 高级 JSON」按钮全部由 js/config.js 自行接管，此处不再保留处理函数。

  /* ------------------------------ 小工具函数 ------------------------------ */

  /** 毫秒时间戳 → 本地 `YYYY-MM-DD HH:MM:SS`（会话列表等需要精确到秒的场景）。 */
  function fmtDateTime(ms) {
    if (!ms) return '';
    var d = new Date(Number(ms));
    if (isNaN(d.getTime())) return '';
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
      + ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  function span(cls, text) {
    var s = doc.createElement('span');
    s.className = cls;
    s.textContent = text === null || text === undefined ? '' : String(text);
    return s;
  }

  function badge(text, tone) {
    var b = doc.createElement('span');
    b.className = 'badge badge-' + (tone || 'chip');
    b.textContent = String(text);
    return b;
  }

  function emptyLi(text) {
    var li = doc.createElement('li');
    li.className = 'list-empty';
    li.textContent = text;
    return li;
  }

  function autoGrow() {
    var ta = refs.input;
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 220) + 'px';
  }

  /* -------------------------------- 绑定 -------------------------------- */

  function bind() {
    // 视图标签页（U9：顶栏 + 抽屉导航共用 —— 文档级委托，两处按钮都能触发路由）
    doc.addEventListener('click', function (e) {
      var btn = e.target.closest('button[data-route]');
      if (btn) setRoute(btn.getAttribute('data-route'));
      var modeBtn = e.target.closest('button[data-chmode]');
      if (modeBtn) setChallengeMode(modeBtn.getAttribute('data-chmode'));
    });

    // 模式切换
    refs.modeSwitch.addEventListener('click', function (e) {
      var btn = e.target.closest('button[data-mode]');
      if (!btn) return;
      state.mode = btn.getAttribute('data-mode');
      renderMode();
    });
    refs.orchestration.addEventListener('change', function () {
      state.orchestration = refs.orchestration.value;
      renderMode();
    });

    // 会话
    refs.btnNewConv.addEventListener('click', createNewConversation);

    // 右侧栏 tab
    refs.rightTabs.addEventListener('click', function (e) {
      var btn = e.target.closest('button[data-tab]');
      if (!btn) return;
      var tab = btn.getAttribute('data-tab');
      Array.prototype.forEach.call(refs.rightTabs.querySelectorAll('button'), function (b) {
        b.classList.toggle('on', b === btn);
      });
      Array.prototype.forEach.call(doc.querySelectorAll('.tabpane'), function (p) {
        if (p.getAttribute('data-pane') === tab) p.removeAttribute('hidden');
        else p.setAttribute('hidden', '');
      });
    });

    // facts 搜索
    refs.factSearch.addEventListener('input', function () {
      state.factQuery = refs.factSearch.value;
      renderFacts();
    });

    // 输入区
    // U34：单按钮双状态（DSH primaryStops）——运行中点 = 停止，空闲点 = 发送
    refs.btnSend.addEventListener('click', function () {
      if (state.running) { stopRun(); } else { sendMessage(); }
    });

    // U34：hero 悬浮——#tl-inner 无任何子节点时输入框悬浮居中，有消息后下沉
    (function () {
      var tlInner = doc.getElementById('tl-inner');
      if (!tlInner || !window.MutationObserver) return;
      var refresh = function () {
        // 只认真实消息/流式节点；timeline 的 .empty-state 占位不算（否则空会话 hero 闪现后即沉底）
        var has = !!tlInner.querySelector('.msg-user, .msg-assistant, .msg-system, .tool-card, .thinking');
        doc.body.classList.toggle('composer-float', !has);
      };
      // 持久引用，防止 observer 被 GC 后回调失效（页面会卡在悬浮态）
      window.__cfObserver = new MutationObserver(refresh);
      window.__cfObserver.observe(tlInner, { childList: true });
      refresh();
    })();

    // U35：右栏功能面板折叠/展开（向右收起，localStorage 持久化）
    (function () {
      var foldBtn = doc.getElementById('aside-fold');  // 不用类选择器：左栏折叠按钮也带 .aside-fold
      var expandBtn = doc.getElementById('aside-expand');
      function applyAside(off) {
        doc.body.classList.toggle('aside-off', off);
        try { localStorage.setItem('ctfa.asideOff', off ? '1' : ''); } catch (err) { /* ignore */ }
      }
      // U35b：按钮固定于标签栏右端，单按钮 toggle（图标随状态切换）
      if (foldBtn) foldBtn.addEventListener('click', function () {
        applyAside(!doc.body.classList.contains('aside-off'));
      });
      try { if (localStorage.getItem('ctfa.asideOff') === '1') applyAside(true); } catch (err) { /* ignore */ }
    })();

    // U35c：左栏导航面板折叠/展开（向左收起，localStorage 持久化；桌面端专用，
    // ≤1024 抽屉模式由 ☰ 菜单接管，body.aside-left-off 在抽屉断点内无样式）
    (function () {
      var foldBtn = doc.getElementById('aside-left-fold');
      function applyAsideLeft(off) {
        doc.body.classList.toggle('aside-left-off', off);
        try { localStorage.setItem('ctfa.asideLeftOff', off ? '1' : ''); } catch (err) { /* ignore */ }
      }
      if (foldBtn) foldBtn.addEventListener('click', function () {
        applyAsideLeft(!doc.body.classList.contains('aside-left-off'));
      });
      try { if (localStorage.getItem('ctfa.asideLeftOff') === '1') applyAsideLeft(true); } catch (err) { /* ignore */ }
    })();

    // U36：hero 引导模块——分类 tab 过滤快捷标签，点击填入输入框
    (function () {
      var chipsEl = doc.getElementById('hero-chips');
      var tabsEl = doc.getElementById('hero-tabs');
      if (!chipsEl || !tabsEl) return;
      var PROMPTS = {
        all: ['信息收集与端口扫描', '识别 Web 指纹与技术栈', '目录爆破与敏感文件探测', '综合解题思路梳理'],
        web: ['SQL 注入测试与利用', 'XSS 漏洞分析', '命令注入利用', '文件上传绕过'],
        pwn: ['检查二进制保护机制', '栈溢出利用', '格式化字符串漏洞'],
        crypto: ['识别加密算法与密钥', 'RSA 常见攻击分析', '编码与解码分析'],
        reverse: ['静态分析二进制结构', '反调试与脱壳', '关键算法还原'],
        misc: ['隐写术分析', '流量包分析', '压缩包密码破解']
      };
      function renderChips(cat) {
        chipsEl.innerHTML = '';
        (PROMPTS[cat] || PROMPTS.all).forEach(function (text) {
          var b = doc.createElement('button');
          b.type = 'button';
          b.textContent = text;
          b.addEventListener('click', function () {
            var input = doc.getElementById('input');
            if (!input) return;
            input.value = text;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.focus();
          });
          chipsEl.appendChild(b);
        });
      }
      tabsEl.addEventListener('click', function (e) {
        var btn = e.target.closest('button');
        if (!btn) return;
        Array.prototype.forEach.call(tabsEl.querySelectorAll('button'), function (x) {
          x.classList.toggle('on', x === btn);
        });
        renderChips(btn.dataset.cat || 'all');
      });
      renderChips('all');
    })();
    refs.btnStop.addEventListener('click', stopRun);
    refs.input.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        sendMessage();
      }
    });
    refs.input.addEventListener('input', autoGrow);

    // 配置（U7）：表单化模块自行接管「保存 / 撤销 / 重新加载 / 测试通道 / 高级 JSON」按钮
    if (CTFA.configForm && typeof CTFA.configForm.init === 'function') {
      CTFA.configForm.init({
        onConfig: function (cfg, res) {
          state.config = cfg;
          if (res && res.persistedPath) state.configPath = res.persistedPath;
          renderConfigPathHint();
        },
        reload: function () { return loadConfig(); },
        notice: function (msg, tone) { CTFA.timeline.notice(msg, tone); }
      });
    }

    // 全局刷新
    refs.btnRefresh.addEventListener('click', function () {
      loadHealth();
      loadTools();
      loadAgents();
      loadConversations();
      loadChallenges();
      loadConfig();
      loadFacts();
      loadFlags();
      if (CTFA.dashboardView) CTFA.dashboardView.load();
      if (CTFA.models && typeof CTFA.models.refresh === 'function') CTFA.models.refresh();
      CTFA.timeline.notice('已刷新', 'info');
    });

    // U10：极简隐藏模式（新窗口，尽量隐藏浏览器自带界面）
    if (refs.btnMinimal) refs.btnMinimal.addEventListener('click', openMinimalMode);

    // 路由
    global.addEventListener('hashchange', route);

    // U9：平板 / 移动端抽屉菜单
    initDrawer();
  }

  /* -------------------------------- 启动 -------------------------------- */

  function boot() {
    refs = {
      healthChip: $('health-chip'),
      modeChip: $('mode-chip'),
      btnRefresh: $('btn-refresh'),
      btnMinimal: $('btn-minimal'),
      viewTabs: $('view-tabs'),
      sideNav: $('side-nav'),
      btnMenu: $('btn-menu'),
      btnDrawerClose: $('btn-drawer-close'),
      drawerBackdrop: $('drawer-backdrop'),
      modeSwitch: $('mode-switch'),
      orchField: $('orch-field'),
      orchestration: $('orchestration'),
      challengeSelect: $('challenge-select'),
      btnNewConv: $('btn-new-conv'),
      conversationList: $('conversation-list'),
      rightTabs: $('right-tabs'),
      factSearch: $('fact-search'),
      factList: $('fact-list'),
      factDetail: $('fact-detail'),
      flagList: $('flag-list'),
      timeline: $('timeline'),
      tlInner: $('tl-inner'),
      scrollBottom: $('scroll-bottom'),
      todoBar: $('todo-bar'),
      progressEl: $('progress-line'),
      usageBadge: $('usage-badge'),
      notices: $('notices'),
      challengesView: $('ch-list-view'),
      chModeBar: $('ch-mode'),
      chListView: $('ch-list-view'),
      chBoardView: $('ch-board-view'),
      dashboardView: $('dashboard-view'),
      competitionView: $('competition-view'),
      toolsView: $('tools-view'),
      agentsView: $('agents-view'),
      configPathHint: $('config-path-hint'),
      configEnvHint: $('config-env-hint'),
      input: $('input'),
      btnSend: $('btn-send'),
      btnStop: $('btn-stop'),
      btnModel: $('btn-model'),
      modelLabel: $('model-select-label'),
      runStatus: $('run-status'),
      queueHint: $('queue-hint')
    };

    // v2 挑战控制台（blackboard 模式）：独立模块，未加载时静默跳过
    if (CTFA.projects && typeof CTFA.projects.init === 'function') {
      CTFA.projects.init({
        list: $('project-list'),
        detail: $('project-detail'),
        select: $('project-select'),
        getConversationId: function () { return state.conversationId || ''; },
        ensureConversation: ensureConversation,
        getConversations: function () { return state.conversations; },
        onConversationsChanged: function () { loadConversations(); },
        onOpenConversation: function (id) { openConversation(id); },
        onOpenProject: function () { setRoute('/projects'); }
      });
    }

    if (CTFA.admin && typeof CTFA.admin.init === 'function') {
      CTFA.admin.init({
        agentsView: refs.agentsView,
        toolsView: refs.toolsView,
        onAgentsReloaded: loadAgents,
        onToolsReloaded: loadTools
      });
    }

    if (CTFA.conversations && typeof CTFA.conversations.init === 'function') {
      CTFA.conversations.init({
        getConversations: function () { return state.conversations; },
        getConversationId: function () { return state.conversationId || ''; },
        ensureConversation: ensureConversation,
        onRefresh: renderConversations,
        onReload: function () { return loadConversations(); },
        onDeleted: function (id) {
          state.conversations = state.conversations.filter(function (c) { return c.id !== id; });
          if (state.conversationId === id) newConversation();
        }
      });
    }

    if (CTFA.hitl && typeof CTFA.hitl.init === 'function') {
      CTFA.hitl.init({
        getConversationId: function () { return state.conversationId || ''; }
      });
    }

    if (CTFA.attachments && typeof CTFA.attachments.init === 'function') {
      CTFA.attachments.init({
        chips: $('attach-chips'),
        fileInput: $('attach-input'),
        button: $('btn-attach'),
        overlay: $('drop-overlay'),
        textarea: $('input'),
        dropZone: refs.timeline,
        getConversationId: function () { return state.conversationId || ''; },
        ensureConversation: ensureConversation,
        onConversationCreated: function (conversation) {
          if (conversation && conversation.id) {
            state.conversationId = conversation.id;
            setRoute('/c/' + encodeURIComponent(conversation.id), true);
            setRoute('/chat', true);
            loadConversations();
          }
        }
      });
    }

    if (CTFA.theme && typeof CTFA.theme.init === 'function') {
      CTFA.theme.init();   // U1：主题（light/dark/retro），切换即时生效并持久化
    }


    CTFA.timeline.init({
      timeline: refs.timeline,
      inner: refs.tlInner,
      scrollBottom: refs.scrollBottom,
      todoBar: refs.todoBar,
      progressEl: refs.progressEl,
      usageBadge: refs.usageBadge,
      notices: refs.notices
    });

    // 模型选择（composer 发送键旁的模型下拉，DSH ModelSelect）
    if (CTFA.models && typeof CTFA.models.init === 'function') CTFA.models.init();

    bind();
    renderMode();
    renderRunStatus();
    renderUsageBadge();
    route();

    loadHealth();
    loadTools();
    loadAgents();
    loadConversations();
    loadChallenges();
    loadConfig();
    loadFacts();
    loadFlags();

    // 供调试与扩展使用（在控制台可直接查看状态）
    CTFA.state = state;
    CTFA.refs = refs;
    CTFA.reload = function () { refs.btnRefresh.click(); };
  }

  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', boot);
  else boot();
})(window);
