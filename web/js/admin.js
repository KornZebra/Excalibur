/* ============================================================================
 * admin.js — 子代理与工具管理板块（U3 前端）
 * ----------------------------------------------------------------------------
 * 在既有「工具」「子代理」两个视图里加上管理能力（内置只读、custom 可改）：
 *   - 子代理：列表（source 徽标 / 工具引用 / 解析错误）＋ 新建 / 编辑 / 另存为副本 / 删除 / 热重载；
 *   - 工具：列表（source/kind/错误/路径）＋ 新建 / 编辑 / 复制 / 删除 / 热重载，
 *     参数编辑器支持 positional / flag / template 三种格式，危险命令需勾选"已知风险"；
 *   - 删除一律走二次确认弹窗（复用 CTFA.conversations.confirmDialog）。
 * 对外：CTFA.admin.*
 * ========================================================================== */
(function (global) {
  'use strict';

  var CTFA = (global.CTFA = global.CTFA || {});
  var doc = global.document;

  var state = {
    agents: [],
    agentErrors: [],
    tools: [],
    toolErrors: [],
    toolsSummary: {},
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

  function isCustom(row) {
    return (row && row.source) === 'custom';
  }

  function badge(text, tone) {
    var node = el('span', tone || 'chip', text);
    if (tone && tone.indexOf('chip') !== 0) node.className = tone;
    return node;
  }

  function sourceBadge(row) {
    return isCustom(row) ? badge('自定义', 'chip chip-ok') : badge('内置', 'chip');
  }

  function errorRow(err) {
    var li = el('li', 'list-item admin-error');
    li.appendChild(el('span', 'li-title', '⚠ ' + (err.path || err.id || err.file || '解析失败')));
    li.appendChild(el('span', 'li-sub mono', err.error || String(err)));
    return li;
  }

  /* ------------------------------- 表单 ------------------------------- */

  function field(labelText, input) {
    var wrap = el('label', 'form-field');
    wrap.appendChild(el('span', 'form-label', labelText));
    wrap.appendChild(input);
    return wrap;
  }

  function textInput(value, placeholder) {
    var input = doc.createElement('input');
    input.type = 'text';
    input.value = value === undefined || value === null ? '' : String(value);
    if (placeholder) input.placeholder = placeholder;
    return input;
  }

  function textArea(value, rows) {
    var input = doc.createElement('textarea');
    input.rows = rows || 8;
    input.spellcheck = false;
    input.value = value === undefined || value === null ? '' : String(value);
    return input;
  }

  function openForm(title, fields, onSubmit, options) {
    var opts = options || {};
    var mask = el('div', 'modal-mask');
    var box = el('div', 'modal-box admin-form');
    box.appendChild(el('h3', 'modal-title', title));
    var body = el('div', 'modal-body');
    var inputs = {};
    fields.forEach(function (spec) {
      var input = spec.type === 'textarea'
        ? textArea(spec.value, spec.rows)
        : (spec.type === 'select' ? selectInput(spec) : textInput(spec.value, spec.placeholder));
      if (spec.type === 'checkbox') {
        input = doc.createElement('input');
        input.type = 'checkbox';
        input.checked = !!spec.value;
      }
      inputs[spec.name] = input;
      body.appendChild(field(spec.label, input));
    });
    box.appendChild(body);
    var status = el('p', 'form-error');
    status.hidden = true;
    box.appendChild(status);
    var actions = el('div', 'modal-actions');
    var cancel = el('button', 'btn', '取消');
    cancel.type = 'button';
    var submit = el('button', 'btn primary', opts.submitText || '保存');
    submit.type = 'button';
    var close = function () { mask.remove(); };
    cancel.addEventListener('click', close);
    mask.addEventListener('click', function (evt) { if (evt.target === mask) close(); });
    submit.addEventListener('click', function () {
      var values = {};
      Object.keys(inputs).forEach(function (key) {
        values[key] = inputs[key].type === 'checkbox' ? inputs[key].checked : inputs[key].value;
      });
      submit.disabled = true;
      Promise.resolve(onSubmit(values)).then(function (okay) {
        if (okay === false) { submit.disabled = false; return; }
        close();
      }).catch(function (err) {
        submit.disabled = false;
        status.hidden = false;
        status.textContent = (err && err.message) || String(err);
      });
    });
    actions.appendChild(cancel);
    actions.appendChild(submit);
    box.appendChild(actions);
    mask.appendChild(box);
    doc.body.appendChild(mask);
    var first = Object.keys(inputs)[0];
    if (first && inputs[first].focus) inputs[first].focus();
  }

  function selectInput(spec) {
    var select = doc.createElement('select');
    (spec.options || []).forEach(function (option) {
      var value = typeof option === 'string' ? option : option.value;
      var text = typeof option === 'string' ? option : option.label;
      var node = doc.createElement('option');
      node.value = value;
      node.textContent = text;
      if (String(spec.value) === String(value)) node.selected = true;
      select.appendChild(node);
    });
    return select;
  }

  /* ----------------------------- 数据加载 ----------------------------- */

  function load() {
    return Promise.all([
      CTFA.api.request('/api/agents/markdown-agents').catch(function () { return {}; }),
      CTFA.api.tools().catch(function () { return {}; })
    ]).then(function (res) {
      var agentData = res[0] || {};
      var toolData = res[1] || {};
      state.agents = agentData.agents || [];
      state.agentErrors = agentData.errors || [];
      state.tools = toolData.tools || [];
      state.toolErrors = toolData.errors || [];
      state.toolsSummary = toolData.summary || {};
      state.loaded = true;
      render();
      return state;
    });
  }

  /* ------------------------------- 渲染 ------------------------------- */

  function render() {
    renderAgents();
    renderTools();
    renderCounts();
  }

  function renderCounts() {
    if (refs.agentsCount) refs.agentsCount.textContent = String(state.agents.length || 0);
    if (refs.toolsCount) refs.toolsCount.textContent = String(state.tools.length || 0);
    if (refs.adminInfo) {
      var custom = state.tools.filter(isCustom).length;
      refs.adminInfo.textContent = '内置 ' + (state.tools.length - custom) + ' · 自定义 ' + custom
        + ' · 解析错误 ' + ((state.toolErrors.length || 0) + (state.agentErrors.length || 0));
    }
  }

  function renderAgents() {
    var box = refs.agentsView;
    if (!box) return;
    box.textContent = '';
    if (state.agentErrors.length) {
      var list = el('ul', 'list admin-errors');
      state.agentErrors.forEach(function (err) { list.appendChild(errorRow(err)); });
      box.appendChild(section('解析失败的子代理文件', list));
    }
    if (!state.agents.length) {
      box.appendChild(el('p', 'admin-empty', '暂无子代理定义'));
      return;
    }
    var grid = el('div', 'admin-grid');
    state.agents.forEach(function (agent) {
      var card = el('article', 'admin-card');
      var head = el('div', 'admin-card-head');
      head.appendChild(el('span', 'li-title', agent.name || agent.id));
      head.appendChild(sourceBadge(agent));
      card.appendChild(head);
      card.appendChild(el('span', 'li-sub mono', agent.id || ''));
      if (agent.description) card.appendChild(el('span', 'li-sub', agent.description));
      var tools = Array.isArray(agent.tools) ? agent.tools : [];
      var chips = el('div', 'chips');
      if (tools.length) {
        tools.slice(0, 8).forEach(function (t) {
          chips.appendChild(badge(typeof t === 'string' ? t : (t.name || '?'), 'chip'));
        });
        if (tools.length > 8) chips.appendChild(badge('+' + (tools.length - 8), 'chip'));
      } else {
        chips.appendChild(badge('继承全部工具', 'chip'));
      }
      card.appendChild(chips);
      var meta = [];
      if (agent.maxIterations) meta.push('maxIterations ' + agent.maxIterations);
      if (agent.systemPromptLength) meta.push('提示词 ' + agent.systemPromptLength + ' 字');
      meta.push('工具 ' + tools.length + ' 个');
      card.appendChild(el('span', 'li-sub', meta.join(' · ')));
      card.appendChild(cardActions(agent));
      grid.appendChild(card);
    });
    box.appendChild(grid);
  }

  function cardActions(agent) {
    var row = el('div', 'admin-actions');
    var edit = el('button', 'mini-btn', '编辑');
    edit.type = 'button';
    edit.addEventListener('click', function () { editAgent(agent); });
    var copy = el('button', 'mini-btn', '另存为副本');
    copy.type = 'button';
    copy.addEventListener('click', function () { duplicateAgent(agent); });
    var del = el('button', 'mini-btn danger', '删除');
    del.type = 'button';
    del.disabled = !isCustom(agent);
    if (!isCustom(agent)) del.title = '内置子代理不可删除（可另存为副本后再改）';
    del.addEventListener('click', function () { deleteAgent(agent); });
    row.appendChild(edit);
    row.appendChild(copy);
    row.appendChild(del);
    return row;
  }

  function renderTools() {
    var box = refs.toolsView;
    if (!box) return;
    box.textContent = '';
    if (state.toolErrors.length) {
      var list = el('ul', 'list admin-errors');
      state.toolErrors.forEach(function (err) { list.appendChild(errorRow(err)); });
      box.appendChild(section('解析失败的配方文件', list));
    }
    var grid = el('div', 'admin-grid');
    state.tools.forEach(function (tool) {
      var card = el('article', 'admin-card');
      var head = el('div', 'admin-card-head');
      head.appendChild(el('span', 'li-title mono', tool.name || '?'));
      head.appendChild(sourceBadge(tool));
      if (tool.kind && tool.kind !== 'builtin') head.appendChild(badge(tool.kind, 'chip'));
      if (tool.enabled === false) head.appendChild(badge('已禁用', 'chip chip-warn'));
      card.appendChild(head);
      if (tool.description || tool.shortDescription) {
        card.appendChild(el('span', 'li-sub', tool.description || tool.shortDescription));
      }
      card.appendChild(el('span', 'li-sub mono', tool.command || ''));
      var params = Array.isArray(tool.parameters) ? tool.parameters : [];
      card.appendChild(el('span', 'li-sub', '参数 ' + params.length + ' 个'
        + (tool.category ? ' · 分类 ' + tool.category : '')));
      var row = el('div', 'admin-actions');
      var edit = el('button', 'mini-btn', '编辑');
      edit.type = 'button';
      edit.addEventListener('click', function () { editTool(tool); });
      var copy = el('button', 'mini-btn', '复制为自定义');
      copy.type = 'button';
      copy.addEventListener('click', function () { copyTool(tool); });
      var del = el('button', 'mini-btn danger', '删除');
      del.type = 'button';
      del.disabled = !isCustom(tool);
      if (!isCustom(tool)) del.title = '内置工具不可删除（可复制为自定义后再改）';
      del.addEventListener('click', function () { deleteTool(tool); });
      row.appendChild(edit);
      row.appendChild(copy);
      row.appendChild(del);
      card.appendChild(row);
      grid.appendChild(card);
    });
    box.appendChild(grid);
  }

  function section(title, node) {
    var wrap = el('section', 'admin-section');
    wrap.appendChild(el('h3', 'admin-section-title', title));
    wrap.appendChild(node);
    return wrap;
  }

  /* ---------------------------- 子代理动作 ---------------------------- */

  function agentFields(row) {
    return [
      { name: 'id', label: 'ID（留空从名称生成；仅小写字母/数字/连字符）', value: row && row.id, placeholder: 'recon-copy' },
      { name: 'name', label: '名称', value: row && row.name, placeholder: '侦察副本' },
      { name: 'description', label: '描述（必填）', value: row && row.description },
      { name: 'maxIterations', label: '最大迭代（可空）', value: row && row.maxIterations },
      { name: 'tools', label: '工具（逗号分隔，可空=继承全部）', value: row && (row.tools || []).join(', ') },
      { name: 'body', label: 'Markdown 正文（必填）', type: 'textarea', rows: 10, value: row && row.body }
    ];
  }

  function createAgent() {
    openForm('新建自定义子代理', agentFields(null), function (values) {
      return CTFA.api.request('/api/agents/markdown-agents', {
        method: 'POST',
        body: buildAgentPayload(values)
      }).then(function () {
        notice('已创建子代理：' + values.name, 'ok');
        return load();
      });
    }, { submitText: '创建' });
  }

  function editAgent(row) {
    openForm('编辑子代理 ' + (row.name || row.id), agentFields(row), function (values) {
      return CTFA.api.request('/api/agents/markdown-agents/' + encodeURIComponent(row.id), {
        method: 'PUT',
        body: buildAgentPayload(values)
      }).then(function () {
        notice('已保存子代理：' + values.name, 'ok');
        return load();
      });
    });
  }

  function buildAgentPayload(values) {
    var payload = {
      name: String(values.name || '').trim(),
      description: String(values.description || '').trim(),
      body: String(values.body || '')
    };
    var id = String(values.id || '').trim();
    if (id) payload.id = id;
    var iterations = String(values.maxIterations || '').trim();
    if (iterations) payload.maxIterations = Number(iterations);
    var tools = String(values.tools || '').split(',').map(function (t) { return t.trim(); }).filter(Boolean);
    if (tools.length) payload.tools = tools;
    return payload;
  }

  function duplicateAgent(row) {
    openForm('另存为副本：' + (row.name || row.id), agentFields(row), function (values) {
      var payload = buildAgentPayload(values);
      if (!String(values.id || '').trim()) payload.id = (row.id || 'agent') + '-copy';
      return CTFA.api.request(
        '/api/agents/markdown-agents/' + encodeURIComponent(row.id) + '/duplicate',
        { method: 'POST', body: payload }
      ).then(function () {
        notice('已创建副本', 'ok');
        return load();
      });
    }, { submitText: '另存为副本' });
  }

  function deleteAgent(row) {
    return confirm({
      title: '删除子代理',
      lines: ['将删除文件 agents/custom/' + row.id + '.md', '内置子代理不可删除；此操作不可恢复。'],
      confirmText: '删除'
    }).then(function (okay) {
      if (!okay) return false;
      return CTFA.api.request(
        '/api/agents/markdown-agents/' + encodeURIComponent(row.id) + '?confirm=true',
        { method: 'DELETE' }
      ).then(function () {
        notice('已删除子代理：' + (row.name || row.id), 'ok');
        return load();
      }).catch(function (err) {
        notice('删除失败：' + err.message, 'error');
        return false;
      });
    });
  }

  /* ----------------------------- 工具动作 ----------------------------- */

  function toolFields(row) {
    var params = (row && row.parameters) || [];
    return [
      { name: 'name', label: '工具名（唯一）', value: row && row.name },
      { name: 'command', label: '命令', value: row && row.command },
      { name: 'shortDescription', label: '一句话描述', value: row && (row.shortDescription || row.description) },
      { name: 'description', label: '详细描述', type: 'textarea', rows: 3, value: row && row.description },
      { name: 'parameters', label: '参数（每行一个：name|type|required|kind|position/flag/template|default|说明）',
        type: 'textarea', rows: 6, value: paramsToText(params) },
      { name: 'acknowledge_risk', label: '我已知晓该命令的风险（危险命令必须勾选）', type: 'checkbox' }
    ];
  }

  function paramsToText(params) {
    return (params || []).map(function (p) {
      var format = p.format || {};
      var kind = format.kind || (
        p.flag ? 'flag' : (p.template ? 'template' : (p.position !== undefined && p.position !== null ? 'positional' : 'positional'))
      );
      var extra = kind === 'flag' ? (format.flag || p.flag || '')
        : kind === 'template' ? (format.template || p.template || '')
          : (format.position !== undefined && format.position !== null ? format.position
            : (p.position === undefined || p.position === null ? '' : p.position));
      return [p.name, p.type || 'string', p.required ? 'required' : 'optional', kind, extra,
        p.default === undefined ? '' : p.default, p.description || ''].join('|');
    }).join('\n');
  }

  function textToParams(text) {
    var params = [];
    String(text || '').split('\n').forEach(function (line) {
      var raw = line.trim();
      if (!raw) return;
      var parts = raw.split('|').map(function (p) { return p.trim(); });
      var param = {
        name: parts[0] || '',
        type: parts[1] || 'string',
        required: (parts[2] || '').toLowerCase() === 'required',
        description: parts[6] || ''
      };
      if (!param.name) return;
      var kind = parts[3] || 'positional';
      var format = { kind: kind };
      if (kind === 'flag') format.flag = parts[4] || ('--' + param.name);
      else if (kind === 'template') format.template = parts[4] || ('{' + param.name + '}');
      else format.position = parts[4] === '' ? params.length : Number(parts[4]);
      param.format = format;
      if (parts[5] !== undefined && parts[5] !== '') param.default = parts[5];
      params.push(param);
    });
    return params;
  }

  function buildToolPayload(values) {
    var payload = {
      name: String(values.name || '').trim(),
      command: String(values.command || '').trim(),
      short_description: String(values.shortDescription || '').trim(),
      description: String(values.description || '').trim(),
      parameters: textToParams(values.parameters)
    };
    if (values.acknowledge_risk) payload.acknowledge_risk = true;
    return payload;
  }

  function createTool() {
    openForm('新建自定义工具', toolFields(null), function (values) {
      return CTFA.api.request('/api/tools', { method: 'POST', body: buildToolPayload(values) })
        .then(function (data) {
          if (data && data.error) {
            notice('文件已写入但解析失败：' + (data.error.message || data.error), 'error');
          } else {
            notice('已创建工具：' + values.name, 'ok');
          }
          return load();
        });
    }, { submitText: '创建' });
  }

  function editTool(row) {
    openForm('编辑工具 ' + row.name, toolFields(row), function (values) {
      return CTFA.api.request('/api/tools/' + encodeURIComponent(row.name), {
        method: 'PUT',
        body: buildToolPayload(values)
      }).then(function (data) {
        if (data && data.error) notice('保存后解析失败：' + (data.error.message || data.error), 'error');
        else notice('已保存工具：' + values.name, 'ok');
        return load();
      });
    });
  }

  function copyTool(row) {
    openForm('复制为自定义工具', toolFields(row), function (values) {
      var payload = buildToolPayload(values);
      return CTFA.api.request('/api/tools/copy', {
        method: 'POST',
        body: { name: row.name, new_name: payload.name }
      }).then(function () {
        notice('已复制为自定义工具：' + payload.name, 'ok');
        return load();
      });
    }, { submitText: '复制' });
  }

  function deleteTool(row) {
    return confirm({
      title: '删除工具',
      lines: ['将删除文件 tools/custom/' + row.name + '.yaml', '内置工具不可删除；此操作不可恢复。'],
      confirmText: '删除'
    }).then(function (okay) {
      if (!okay) return false;
      return CTFA.api.request(
        '/api/tools/' + encodeURIComponent(row.name) + '?confirm=true',
        { method: 'DELETE' }
      ).then(function () {
        notice('已删除工具：' + row.name, 'ok');
        return load();
      }).catch(function (err) {
        notice('删除失败：' + err.message, 'error');
        return false;
      });
    });
  }

  /* ------------------------------ 热重载 ------------------------------ */

  function reloadAgents() {
    return CTFA.api.request('/api/agents/reload', { method: 'POST', body: {} }).then(function (data) {
      notice('子代理已热重载：新增 ' + (data.added || 0) + ' / 更新 ' + (data.updated || 0)
        + ' / 移除 ' + (data.removed || 0), 'ok');
      if (typeof hooks.onAgentsReloaded === 'function') hooks.onAgentsReloaded();
      return load();
    }).catch(function (err) {
      notice('热重载失败：' + err.message, 'error');
      return false;
    });
  }

  function reloadTools() {
    return CTFA.api.request('/api/tools/reload', { method: 'POST', body: {} }).then(function (data) {
      notice('工具已热重载：新增 ' + (data.added || 0) + ' / 更新 ' + (data.updated || 0)
        + ' / 移除 ' + (data.removed || 0), 'ok');
      if (typeof hooks.onToolsReloaded === 'function') hooks.onToolsReloaded();
      return load();
    }).catch(function (err) {
      notice('热重载失败：' + err.message, 'error');
      return false;
    });
  }

  function init(options) {
    hooks = options || {};
    refs.agentsView = options.agentsView || doc.getElementById('agents-view');
    refs.toolsView = options.toolsView || doc.getElementById('tools-view');
    refs.agentsCount = options.agentsCount || doc.getElementById('agents-count');
    refs.toolsCount = options.toolsCount || doc.getElementById('tools-count');
    refs.adminInfo = doc.getElementById('admin-info');
    bind('btn-agent-new', createAgent);
    bind('btn-agent-reload', reloadAgents);
    bind('btn-tool-new', createTool);
    bind('btn-tool-reload', reloadTools);
    return load();
  }

  function bind(id, handler) {
    var node = doc.getElementById(id);
    if (node) node.addEventListener('click', function () { handler(); });
  }

  CTFA.admin = {
    init: init,
    load: load,
    render: render,
    state: state,
    textToParams: textToParams,
    paramsToText: paramsToText,
    buildToolPayload: buildToolPayload,
    buildAgentPayload: buildAgentPayload
  };
})(window);