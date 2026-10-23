/* ============================================================================
 * projects.js — 项目（比赛）维度组织（U4 前端）
 * ----------------------------------------------------------------------------
 * - 顶栏「项目」视图：项目列表（名称/状态/会话数）+ 新建 / 编辑 / 删除（解绑 or 连带删除）；
 * - 项目详情：统计行 + 会话列表 + 聚合 flag / 挑战 / 黑板事实 / 附件 + 一键生成项目报告；
 * - 对话归属：对话输入区上方可把「当前会话」加入 / 移出项目；
 *   项目页也能把未归属会话批量加入本项目；
 * - 所有删除与连带删除都走二次确认弹窗，且"连带删除会话"必须二次确认。
 * 对外：CTFA.projects.*
 * ========================================================================== */
(function (global) {
  'use strict';

  var CTFA = (global.CTFA = global.CTFA || {});
  var doc = global.document;

  var state = {
    projects: [],
    currentProjectId: '',
    summary: null,
    conversations: [],
    loaded: false
  };

  var refs = {};
  var hooks = {};

  function el(tag, cls, text) {
    var node = doc.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function notice(text, tone) {
    if (CTFA.timeline && typeof CTFA.timeline.notice === 'function') {
      CTFA.timeline.notice(text, tone || 'info');
    }
  }

  function confirm(options) {
    if (CTFA.conversations && CTFA.conversations.confirmDialog) {
      return CTFA.conversations.confirmDialog(options);
    }
    return Promise.resolve(global.confirm((options && options.title) || '请确认'));
  }

  function api() { return CTFA.api.request.apply(CTFA.api, arguments); }

  function humanSize(bytes) {
    return CTFA.attachments ? CTFA.attachments.humanSize(bytes) : String(bytes || 0) + 'B';
  }

  function currentConversationId() {
    return typeof hooks.getConversationId === 'function' ? hooks.getConversationId() || '' : '';
  }

  /* ------------------------------ 数据加载 ------------------------------ */

  function load() {
    return CTFA.api.request('/api/projects').then(function (data) {
      state.projects = (data && data.projects) || [];
      state.loaded = true;
      renderList();
      renderProjectSelect();
      return state.projects;
    }).catch(function (err) {
      notice('加载项目失败：' + err.message, 'error');
      return [];
    });
  }

  function loadSummary(projectId) {
    state.currentProjectId = projectId || '';
    if (!projectId) {
      state.summary = null;
      renderDetail();
      return Promise.resolve(null);
    }
    return CTFA.api.request('/api/projects/' + encodeURIComponent(projectId) + '/summary')
      .then(function (data) {
        state.summary = data;
        renderDetail();
        return data;
      }).catch(function (err) {
        notice('加载项目详情失败：' + err.message, 'error');
        return null;
      });
  }

  /* ------------------------------- 渲染 ------------------------------- */

  function renderList() {
    var box = refs.list;
    if (!box) return;
    box.textContent = '';
    if (!state.projects.length) {
      box.appendChild(el('p', 'admin-empty', '还没有项目。用「＋ 新建项目」把一次比赛的会话组织到一起。'));
      return;
    }
    var grid = el('div', 'admin-grid');
    state.projects.forEach(function (project) {
      var card = el('article', 'admin-card project-card'
        + (project.id === state.currentProjectId ? ' on' : ''));
      var head = el('div', 'admin-card-head');
      head.appendChild(el('span', 'li-title', project.name || project.id));
      head.appendChild(el('span', 'chip', project.status || 'active'));
      card.appendChild(head);
      if (project.description) card.appendChild(el('span', 'li-sub', project.description));
      card.appendChild(el('span', 'li-sub', '会话 ' + (project.conversationCount || 0)
        + ' · 更新 ' + (project.updatedAt || '')));
      var row = el('div', 'admin-actions');
      var open = el('button', 'mini-btn', '打开');
      open.type = 'button';
      open.addEventListener('click', function () { openProject(project.id); });
      var edit = el('button', 'mini-btn', '编辑');
      edit.type = 'button';
      edit.addEventListener('click', function () { editProject(project); });
      var del = el('button', 'mini-btn danger', '删除');
      del.type = 'button';
      del.addEventListener('click', function () { deleteProject(project); });
      row.appendChild(open);
      row.appendChild(edit);
      row.appendChild(del);
      card.appendChild(row);
      grid.appendChild(card);
    });
    box.appendChild(grid);
  }

  function renderDetail() {
    var box = refs.detail;
    if (!box) return;
    box.textContent = '';
    var summary = state.summary;
    if (!summary || !summary.project) {
      box.appendChild(el('p', 'admin-empty', '选择（或新建）一个项目查看聚合视图。'));
      return;
    }
    var project = summary.project;
    box.appendChild(section(project.name + ' · 概览', statsRow(summary.stats || {})));

    if (Array.isArray(summary.flags) && summary.flags.length) {
      var flags = el('ul', 'list');
      summary.flags.forEach(function (flag) {
        var li = el('li', 'list-item');
        li.appendChild(el('span', 'li-title mono', flag.value));
        li.appendChild(el('span', 'li-sub', '会话 ' + (flag.conversationId || '') + ' · ' + (flag.createdAt || '')));
        flags.appendChild(li);
      });
      box.appendChild(section('聚合 flag（' + summary.flags.length + '）', flags));
    }

    var convList = el('ul', 'list');
    (summary.conversations || []).forEach(function (conversation) {
      var li = el('li', 'list-item');
      var main = el('div', 'li-main');
      main.appendChild(el('span', 'li-title', conversation.title || conversation.id));
      main.appendChild(el('span', 'li-sub', '消息 ' + (conversation.messageCount || 0)
        + ' · flag ' + (conversation.flagCount || 0)
        + (conversation.challengeId ? ' · 题目 ' + conversation.challengeId : '')
        + ' · ' + (conversation.updatedAt || '')));
      li.appendChild(main);
      var unassign = el('button', 'mini-btn', '移出项目');
      unassign.type = 'button';
      unassign.addEventListener('click', function (event) {
        event.stopPropagation();
        assign([conversation.id], 'unassign');
      });
      li.appendChild(unassign);
      li.addEventListener('click', function () {
        if (typeof hooks.onOpenConversation === 'function') hooks.onOpenConversation(conversation.id);
      });
      convList.appendChild(li);
    });
    if (!(summary.conversations || []).length) {
      convList.appendChild(el('li', 'list-item', '该项目暂时没有会话'));
    }
    box.appendChild(section('会话（' + (summary.conversations || []).length + '）', convList));

    if (Array.isArray(summary.challenges) && summary.challenges.length) {
      var challenges = el('ul', 'list');
      summary.challenges.forEach(function (challenge) {
        var li = el('li', 'list-item');
        li.appendChild(el('span', 'li-title', challenge.title || challenge.challengeId));
        li.appendChild(el('span', 'li-sub mono', (challenge.challengeId || '') + ' · '
          + (challenge.status || '') + ' · 会话 ' + (challenge.conversationCount || 0)));
        challenges.appendChild(li);
      });
      box.appendChild(section('挑战（' + summary.challenges.length + '）', challenges));
    }

    if (Array.isArray(summary.facts) && summary.facts.length) {
      var facts = el('ul', 'list');
      summary.facts.slice(0, 50).forEach(function (fact) {
        var li = el('li', 'list-item');
        li.appendChild(el('span', 'li-title mono', fact.key || ''));
        li.appendChild(el('span', 'li-sub', (fact.summary || '') + ' — ' + (fact.conversationId || '')));
        facts.appendChild(li);
      });
      box.appendChild(section('黑板事实（' + summary.facts.length + '）', facts));
    }

    if (Array.isArray(summary.attachments) && summary.attachments.length) {
      var files = el('ul', 'list');
      summary.attachments.forEach(function (item) {
        var li = el('li', 'list-item');
        li.appendChild(el('span', 'li-title', item.filename || ''));
        li.appendChild(el('span', 'li-sub', (item.kind || '') + ' · ' + humanSize(item.size)
          + ' · 会话 ' + (item.conversationId || '')));
        files.appendChild(li);
      });
      box.appendChild(section('附件（' + summary.attachments.length + '）', files));
    }

    var actions = el('div', 'admin-actions');
    var report = el('button', 'btn primary', '生成项目报告');
    report.type = 'button';
    report.addEventListener('click', function () { generateReport(); });
    var assignBtn = el('button', 'btn', '把未归属会话加入本项目');
    assignBtn.type = 'button';
    assignBtn.addEventListener('click', function () { assignUnassigned(); });
    actions.appendChild(report);
    actions.appendChild(assignBtn);
    box.appendChild(section('操作', actions));
  }

  function statsRow(stats) {
    var row = el('div', 'stats-row');
    [['会话', stats.conversationCount], ['消息', stats.messageCount], ['flag', stats.flagCount],
      ['黑板', stats.factCount], ['附件', stats.attachmentCount], ['挑战', stats.challengeCount]
    ].forEach(function (pair) {
      var cell = el('span', 'stat-cell');
      cell.appendChild(el('span', 'stat-value', String(pair[1] || 0)));
      cell.appendChild(el('span', 'stat-label', pair[0]));
      row.appendChild(cell);
    });
    return row;
  }

  function section(title, node) {
    var wrap = el('section', 'admin-section');
    wrap.appendChild(el('h3', 'admin-section-title', title));
    wrap.appendChild(node);
    return wrap;
  }

  function renderProjectSelect() {
    var select = refs.select;
    if (!select) return;
    var conversationId = currentConversationId();
    var conversation = currentConversation();
    var current = (conversation && conversation.projectId) || '';
    select.textContent = '';
    var none = doc.createElement('option');
    none.value = '';
    none.textContent = conversationId ? '未归属项目' : '未归属项目（选择后自动新建会话）';
    select.appendChild(none);
    state.projects.forEach(function (project) {
      var option = doc.createElement('option');
      option.value = project.id;
      option.textContent = project.name || project.id;
      if (project.id === current) option.selected = true;
      select.appendChild(option);
    });
    // 没有会话也能选：选中项目时会先建一个会话再归属（见 ensureConversation）
    var canCreate = typeof hooks.ensureConversation === 'function';
    select.disabled = !state.projects.length && !canCreate;
    select.title = !state.projects.length
      ? '还没有项目：先到顶栏「项目」里新建一个'
      : (conversationId
        ? '把当前会话归属到某个项目'
        : '把这条新对话归属到某个项目（会自动新建会话）');
  }

  function currentConversation() {
    var conversations = typeof hooks.getConversations === 'function' ? hooks.getConversations() || [] : [];
    var id = currentConversationId();
    return conversations.filter(function (c) { return c.id === id; })[0] || null;
  }

  /* ------------------------------- 动作 ------------------------------- */

  function projectFields(row) {
    return [
      { name: 'name', label: '项目名称（必填）', value: row && row.name, placeholder: '2024 某某杯' },
      { name: 'description', label: '描述', type: 'textarea', rows: 3, value: row && row.description },
      { name: 'status', label: '状态', value: (row && row.status) || 'active', placeholder: 'active / archived' }
    ];
  }

  function createProject() {
    openForm('新建项目', projectFields(null), function (values) {
      return CTFA.api.request('/api/projects', { method: 'POST', body: values }).then(function (data) {
        var project = (data && data.project) || {};
        notice('已创建项目：' + (project.name || ''), 'ok');
        return load().then(function () { return loadSummary(project.id); });
      });
    }, { submitText: '创建' });
  }

  function editProject(project) {
    openForm('编辑项目 ' + project.name, projectFields(project), function (values) {
      return CTFA.api.request('/api/projects/' + encodeURIComponent(project.id), {
        method: 'PUT', body: values
      }).then(function () {
        notice('已保存项目', 'ok');
        return load();
      });
    });
  }

  function deleteProject(project) {
    return choiceDialog({
      title: '删除项目「' + (project.name || project.id) + '」',
      lines: [
        '仅解绑会话：项目消失，会话保留，只清空它们的项目归属（可恢复）。',
        '连带删除会话：会先取消运行中的任务，再永久删除全部会话及其消息、附件与磁盘目录。'
      ]
    }).then(function (mode) {
      if (mode === 'cancel') return false;
      if (mode === 'detach') return doDelete(project, 'detach');
      // 破坏性操作：二次确认
      return confirm({
        title: '再次确认：连带删除全部会话',
        lines: [
          '将永久删除项目「' + (project.name || project.id) + '」下的 '
            + (project.conversationCount || 0) + ' 个会话及相关数据。',
          '此操作不可恢复。'
        ],
        confirmText: '我确认，全部删除'
      }).then(function (again) {
        if (!again) return false;
        return doDelete(project, 'delete');
      });
    });
  }

  /** 三选一弹窗（取消 / 仅解绑 / 连带删除）：confirmDialog 只有两态，破坏性操作需要区分。 */
  function choiceDialog(options) {
    var opts = options || {};
    return new Promise(function (resolve) {
      var mask = el('div', 'modal-mask');
      var box = el('div', 'modal-box');
      box.appendChild(el('h3', 'modal-title', opts.title || '请选择'));
      var body = el('div', 'modal-body');
      (opts.lines || []).forEach(function (line) { body.appendChild(el('p', 'modal-line', line)); });
      box.appendChild(body);
      var actions = el('div', 'modal-actions');
      var done = function (value) { mask.remove(); resolve(value); };
      var cancel = el('button', 'btn', opts.cancelText || '取消');
      cancel.type = 'button';
      cancel.addEventListener('click', function () { done('cancel'); });
      var detach = el('button', 'btn primary', opts.detachText || '仅解绑会话');
      detach.type = 'button';
      detach.addEventListener('click', function () { done('detach'); });
      var cascade = el('button', 'btn danger', opts.deleteText || '连带删除会话');
      cascade.type = 'button';
      cascade.addEventListener('click', function () { done('delete'); });
      mask.addEventListener('click', function (evt) { if (evt.target === mask) done('cancel'); });
      actions.appendChild(cancel);
      actions.appendChild(detach);
      actions.appendChild(cascade);
      box.appendChild(actions);
      mask.appendChild(box);
      doc.body.appendChild(mask);
      detach.focus();
    });
  }

  function doDelete(project, mode) {
    var query = '?mode=' + mode + '&confirm=true';
    return CTFA.api.request('/api/projects/' + encodeURIComponent(project.id) + query, { method: 'DELETE' })
      .then(function (data) {
        var failed = (data && data.failed) || 0;
        notice('项目已删除（' + (mode === 'delete' ? '连带删除会话' : '仅解绑会话') + '）'
          + (failed ? '，' + failed + ' 个会话因运行中未删除' : ''), failed ? 'warn' : 'ok');
        if (state.currentProjectId === project.id) state.currentProjectId = '';
        return load().then(function () { return loadSummary(''); });
      }).catch(function (err) {
        notice('删除项目失败：' + err.message, 'error');
        return false;
      });
  }

  /** 把会话加入 / 移出项目 */
  function assign(conversationIds, action) {
    var projectId = state.currentProjectId;
    if (!projectId) { notice('请先打开一个项目', 'warn'); return Promise.resolve(false); }
    return CTFA.api.request('/api/projects/' + encodeURIComponent(projectId) + '/assign', {
      method: 'POST',
      body: { conversationIds: conversationIds, action: action || 'assign' }
    }).then(function (data) {
      var failed = (data && data.failed) || 0;
      notice((action === 'unassign' ? '已移出项目' : '已加入项目') + (failed ? '（失败 ' + failed + '）' : ''), failed ? 'warn' : 'ok');
      if (typeof hooks.onConversationsChanged === 'function') hooks.onConversationsChanged();
      return loadSummary(projectId);
    }).catch(function (err) {
      notice('操作失败：' + err.message, 'error');
      return false;
    });
  }

  function assignUnassigned() {
    var conversations = typeof hooks.getConversations === 'function' ? hooks.getConversations() || [] : [];
    var free = conversations.filter(function (c) { return !c.projectId; }).map(function (c) { return c.id; });
    if (!free.length) { notice('没有未归属的会话', 'info'); return Promise.resolve(false); }
    return confirm({
      title: '把未归属会话加入本项目',
      lines: ['将把 ' + free.length + ' 个未归属会话加入「' + currentProjectName() + '」。'],
      confirmText: '加入'
    }).then(function (okay) {
      if (!okay) return false;
      return assign(free, 'assign');
    });
  }

  function currentProjectName() {
    var project = state.projects.filter(function (p) { return p.id === state.currentProjectId; })[0];
    return project ? project.name || project.id : '';
  }

  function assignCurrentConversation(projectId) {
    // 还没开聊就选项目：先自动建一个会话再归属（不能要求用户先上传附件/发消息）
    var ready = (projectId && !currentConversationId() && typeof hooks.ensureConversation === 'function')
      ? Promise.resolve(hooks.ensureConversation() || '')
      : Promise.resolve(currentConversationId());
    return ready.then(function (conversationId) {
      if (!conversationId) { notice('当前没有会话可归属', 'warn'); return false; }
      if (!projectId) {
        // 从当前所属项目里移出
        var conversation = currentConversation();
        var from = (conversation && conversation.projectId) || state.currentProjectId;
        if (!from) return false;
        return CTFA.api.request('/api/projects/' + encodeURIComponent(from) + '/assign', {
          method: 'POST', body: { conversationIds: [conversationId], action: 'unassign' }
        }).then(function () {
          notice('已移出项目', 'ok');
          if (typeof hooks.onConversationsChanged === 'function') hooks.onConversationsChanged();
          return load();
        });
      }
      return CTFA.api.request('/api/projects/' + encodeURIComponent(projectId) + '/assign', {
        method: 'POST', body: { conversationIds: [conversationId], action: 'assign' }
      }).then(function () {
        notice('会话已归属到项目：' + currentProjectNameById(projectId), 'ok');
        if (typeof hooks.onConversationsChanged === 'function') hooks.onConversationsChanged();
        return load();
      }).catch(function (err) {
        notice('归属失败：' + err.message, 'error');
        return false;
      });
    });
  }

  function currentProjectNameById(projectId) {
    var project = state.projects.filter(function (p) { return p.id === projectId; })[0];
    return project ? (project.name || project.id) : projectId;
  }

  function generateReport() {
    if (!state.currentProjectId) return Promise.resolve(false);
    return CTFA.api.request('/api/projects/' + encodeURIComponent(state.currentProjectId) + '/report', {
      method: 'POST', body: {}
    }).then(function (data) {
      if (!data || !data.path) { notice('报告已生成但未返回路径', 'warn'); return data; }
      notice('项目报告已生成：' + data.path, 'ok');
      var box = refs.reportBox;
      if (box) {
        box.hidden = false;
        box.textContent = '';
        box.appendChild(el('span', 'li-sub', '报告：' + data.path + ' （' + humanSize(data.bytes) + '）'));
        var link = el('a', 'mini-btn', '下载');
        link.href = '/api/files?path=' + encodeURIComponent(data.path);
        link.setAttribute('download', '');
        link.target = '_blank';
        box.appendChild(link);
      }
      return data;
    }).catch(function (err) {
      notice('生成报告失败：' + err.message, 'error');
      return false;
    });
  }

  function openProject(projectId) {
    state.currentProjectId = projectId;
    renderList();
    if (typeof hooks.onOpenProject === 'function') hooks.onOpenProject(projectId);
    return loadSummary(projectId);
  }

  /* ------------------------------ 表单 ------------------------------ */

  function openForm(title, fields, onSubmit, options) {
    var opts = options || {};
    var mask = el('div', 'modal-mask');
    var box = el('div', 'modal-box admin-form');
    box.appendChild(el('h3', 'modal-title', title));
    var body = el('div', 'modal-body');
    var inputs = {};
    fields.forEach(function (spec) {
      var input = spec.type === 'textarea' ? doc.createElement('textarea') : doc.createElement('input');
      if (spec.type === 'textarea') {
        input.rows = spec.rows || 4;
        input.value = spec.value || '';
      } else {
        input.type = 'text';
        input.value = spec.value === undefined || spec.value === null ? '' : String(spec.value);
        if (spec.placeholder) input.placeholder = spec.placeholder;
      }
      inputs[spec.name] = input;
      var wrap = el('label', 'form-field');
      wrap.appendChild(el('span', 'form-label', spec.label));
      wrap.appendChild(input);
      body.appendChild(wrap);
    });
    box.appendChild(body);
    var error = el('p', 'form-error');
    error.hidden = true;
    box.appendChild(error);
    var actions = el('div', 'modal-actions');
    var cancel = el('button', 'btn', '取消');
    cancel.type = 'button';
    var submit = el('button', 'btn primary', opts.submitText || '保存');
    submit.type = 'button';
    cancel.addEventListener('click', function () { mask.remove(); });
    mask.addEventListener('click', function (event) { if (event.target === mask) mask.remove(); });
    submit.addEventListener('click', function () {
      var values = {};
      Object.keys(inputs).forEach(function (key) { values[key] = inputs[key].value; });
      submit.disabled = true;
      Promise.resolve(onSubmit(values)).then(function (okay) {
        if (okay === false) { submit.disabled = false; return; }
        mask.remove();
      }).catch(function (err) {
        submit.disabled = false;
        error.hidden = false;
        error.textContent = (err && err.message) || String(err);
      });
    });
    actions.appendChild(cancel);
    actions.appendChild(submit);
    box.appendChild(actions);
    mask.appendChild(box);
    doc.body.appendChild(mask);
    if (inputs.name) inputs.name.focus();
  }

  function init(options) {
    hooks = options || {};
    refs.list = options.list || doc.getElementById('project-list');
    refs.detail = options.detail || doc.getElementById('project-detail');
    refs.select = options.select || doc.getElementById('project-select');
    refs.reportBox = doc.getElementById('project-report');
    bind('btn-project-new', createProject);
    bind('btn-project-refresh', function () { return load().then(function () { return loadSummary(state.currentProjectId); }); });
    if (refs.select) {
      refs.select.addEventListener('change', function () { assignCurrentConversation(refs.select.value); });
    }
    return load();
  }

  function bind(id, handler) {
    var node = doc.getElementById(id);
    if (node) node.addEventListener('click', function () { handler(); });
  }

  CTFA.projects = {
    init: init,
    load: load,
    loadSummary: loadSummary,
    openProject: openProject,
    renderList: renderList,
    renderDetail: renderDetail,
    renderProjectSelect: renderProjectSelect,
    assign: assign,
    assignCurrentConversation: assignCurrentConversation,
    generateReport: generateReport,
    deleteProject: deleteProject,
    choiceDialog: choiceDialog,
    doDelete: doDelete,
    state: state
  };
})(window);