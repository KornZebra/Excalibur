/* ============================================================================
 * config.js — 配置页「表单化」（U7）
 * ----------------------------------------------------------------------------
 * 把原来「一整块 JSON 文本编辑器」换成分区表单：文本框 / 数字框 / 下拉 / 开关 /
 * 标签编辑器，让用户点几下就能改参数，不用读整份配置代码。
 *
 * 设计要点：
 *   - 只提交**发生变化的字段**（深合并 patch），后端 apply_update 会深合并，
 *     因此不会覆盖未在表单中暴露的字段（custom_agents / extra_body / mcp 等）。
 *   - 保留「高级：完整 JSON」折叠区，作为兜底的全量编辑入口。
 *   - 只读展示不可热更新的段（docker_worker / mcp），并提示需直接改 config.yaml。
 *   - 所有颜色走 css/style.css 的设计变量，不硬编码。
 *
 * 对外接口：CTFA.configForm.init(opts) / render(config) / save()
 *   opts = { getConfig, onConfig, reload, notice }
 * ========================================================================== */
(function (global) {
  'use strict';

  var CTFA = (global.CTFA = global.CTFA || {});
  var doc = global.document;

  /* ------------------------------ 常量 / 模式 ------------------------------ */

  var PROVIDERS = [
    ['openai_compatible', 'OpenAI 兼容端点（通用）'],
    ['openai', 'OpenAI'],
    ['deepseek', 'DeepSeek'],
    ['qwen', '通义千问（DashScope）'],
    ['glm', '智谱 GLM'],
    ['vllm', '本地 vLLM'],
    ['ollama', '本地 Ollama']
  ];

  var REVIEWERS = [
    ['human', '人工审查（逐条确认）'],
    ['audit_agent', 'Agent 快速审查（自动裁决）'],
    ['none', '完全访问（不审查）']
  ];

  var REASONING = [
    ['', '（不设置 / 由服务商默认）'],
    ['low', 'low'],
    ['medium', 'medium'],
    ['high', 'high']
  ];

  var ORCHESTRATIONS = [
    ['deep', 'Deep（深度编排，推荐）'],
    ['plan_execute', 'plan_execute（先规划后执行）']
  ];

  /* 字段类型：text | password | number | bool | select | list | textarea | channel
   * 可选属性：hint / options / min / max / step / unit / nullable / placeholder / wide / monospace */
  var SECTIONS = [
    {
      id: 'server',
      title: '服务器',
      desc: 'Web 服务监听地址与端口。修改后需重启进程才会生效。',
      fields: [
        { path: 'server.host', label: '监听地址', type: 'text', monospace: true,
          hint: '127.0.0.1 = 仅本机；0.0.0.0 = 暴露到局域网（api_key 为明文，注意风险）。' },
        { path: 'server.port', label: '端口', type: 'number', min: 1, max: 65535 }
      ]
    },
    {
      id: 'ai',
      title: 'AI 通道',
      desc: '模型服务的地址、密钥与采样参数。多个通道按顺序 failover。',
      channels: true
    },
    {
      id: 'agent',
      title: 'Agent 运行',
      desc: '单轮 ReAct 迭代上限、工具超时与结果落盘阈值。',
      fields: [
        { path: 'agent.max_iterations', label: '主代理最大迭代', type: 'number', min: 1 },
        { path: 'agent.sub_agent_max_iterations', label: '子代理最大迭代', type: 'number', min: 1 },
        { path: 'agent.tool_timeout_seconds', label: '工具硬超时', type: 'number', min: 1, unit: '秒' },
        { path: 'agent.tool_wait_timeout_seconds', label: '模型侧等待上限', type: 'number', min: 1, unit: '秒' },
        { path: 'agent.no_output_timeout_seconds', label: '无输出终止', type: 'number', min: 1, unit: '秒' },
        { path: 'agent.max_concurrent_tools', label: '并发工具上限', type: 'number', min: 1 },
        { path: 'agent.max_tool_result_bytes', label: '单条结果落盘阈值', type: 'number', min: 0, unit: '字节' },
        { path: 'agent.max_history_messages', label: '历史消息阈值', type: 'number', min: 1 },
        { path: 'agent.max_finalizer_retries', label: '收尾重试次数', type: 'number', min: 0 },
        { path: 'agent.tool_search_enabled', label: '动态工具解锁（tool_search）', type: 'bool' },
        { path: 'agent.finalizer_enabled', label: '证据不足自动续跑', type: 'bool' },
        { path: 'agent.system_prompt_path', label: '系统提示词文件', type: 'text', wide: true,
          hint: '留空 = 使用内置系统提示词。' }
      ]
    },
    {
      id: 'hitl',
      title: '人工审查（HITL）',
      desc: '命令执行前的审查策略、超时与白名单。',
      groups: [
        {
          title: '总开关与模式',
          fields: [
            { path: 'hitl.enabled', label: '启用审查', type: 'bool' },
            { path: 'hitl.default_reviewer', label: '默认审查模式', type: 'select', options: REVIEWERS,
              hint: '会话级可单独覆盖（优先级：会话 > 全局）。' },
            { path: 'hitl.timeout_seconds', label: '人工审批超时', type: 'number', min: 1, unit: '秒',
              hint: '超时自动拒绝（fail-closed）。' },
            { path: 'hitl.cli_auto_approve', label: 'CLI 自动批准', type: 'bool', hint: '仅本地靶场调试使用。' }
          ]
        },
        {
          title: '审计模型（audit_agent）',
          fields: [
            { path: 'hitl.audit_agent.model_channel', label: '审计通道', type: 'channel',
              hint: '留空 = 复用 ai.default_channel。' },
            { path: 'hitl.audit_agent.timeout_seconds', label: '单次审计超时', type: 'number', min: 1, unit: '秒' },
            { path: 'hitl.audit_agent.max_tokens', label: '审计输出上限', type: 'number', min: 1, unit: 'tokens' },
            { path: 'hitl.audit_agent.on_failure', label: '审计失败兜底', type: 'select',
              options: [['reject', '保守拒绝（默认）'], ['fallback_human', '转人工队列']] },
            { path: 'hitl.audit_agent.allow_edit', label: '允许改参后放行（review_edit）', type: 'bool' },
            { path: 'hitl.audit_agent.prompt', label: '审计提示词', type: 'textarea', wide: true,
              hint: '留空 = 使用内置 CTF 审计提示词。' }
          ]
        },
        {
          title: '只读校验（readonly_gate）',
          fields: [
            { path: 'hitl.readonly_gate.enabled', label: '启用只读校验', type: 'bool',
              hint: '关闭后 bash_readonly 等价于 bash（仍受超时/workspace 治理约束）。' },
            { path: 'hitl.readonly_gate.extra_allowlist', label: '追加只读白名单前缀', type: 'list', wide: true,
              hint: '前缀匹配，回车添加；如 sqlite3 -readonly、rabin2 -I。' }
          ]
        },
        {
          title: '免审工具白名单（tool_whitelist）',
          fields: [
            { path: 'hitl.tool_whitelist', label: '工具名单', type: 'list', wide: true,
              hint: '回车添加；名单内的工具执行前免人工审查。' }
          ]
        }
      ]
    },
    {
      id: 'multi_agent',
      title: '多代理编排',
      desc: '子代理并发上限与编排策略。',
      fields: [
        { path: 'multi_agent.enabled', label: '启用多代理', type: 'bool' },
        { path: 'multi_agent.default_orchestration', label: '默认编排策略', type: 'select', options: ORCHESTRATIONS },
        { path: 'multi_agent.max_concurrent_sub_agents', label: '子代理并发上限', type: 'number', min: 1 },
        { path: 'multi_agent.mode', label: '运行模式', type: 'select',
          options: [['roles', 'roles（v1 深度编排）'], ['blackboard', 'blackboard（v2 自主解题）']] }
      ]
    },
    {
      id: 'state_v2',
      title: '状态账本 v2',
      desc: '挑战级 Idea / Memory 账本与渲染式上下文。关闭后行为与 v1 一致。',
      groups: [
        {
          title: '总开关',
          fields: [{ path: 'state_v2.enabled', label: '启用状态账本', type: 'bool' }]
        },
        {
          title: '渲染式上下文（renderer）',
          fields: [
            { path: 'state_v2.renderer.enabled', label: '启用渲染', type: 'bool' },
            { path: 'state_v2.renderer.budget_tokens', label: '状态块预算', type: 'number', min: 0, unit: 'tokens' },
            { path: 'state_v2.renderer.idea_top', label: '渲染 Idea 条数', type: 'number', min: 0 },
            { path: 'state_v2.renderer.memory_top', label: '渲染 Memory 条数', type: 'number', min: 0 },
            { path: 'state_v2.renderer.handoff_max_chars', label: '交接包上限', type: 'number', min: 0, unit: '字符' }
          ]
        }
      ]
    },
    {
      id: 'solver',
      title: 'Solver 生命周期',
      desc: '单个 Solver 的超时与收尾策略。',
      fields: [
        { path: 'solver.soft_timeout_minutes', label: '软超时', type: 'number', min: 1, unit: '分钟' },
        { path: 'solver.hard_timeout_minutes', label: '硬超时', type: 'number', min: 1, unit: '分钟' },
        { path: 'solver.grace_minutes', label: '软超时宽限期', type: 'number', min: 0, unit: '分钟' },
        { path: 'solver.conclude_budget_tokens', label: '收尾调用预算', type: 'number', min: 0, unit: 'tokens' },
        { path: 'solver.max_conclude_iterations', label: '收尾最大迭代', type: 'number', min: 0 },
        { path: 'solver.two_phase_conclude', label: '双阶段收尾（失败即产出）', type: 'bool' }
      ]
    },
    {
      id: 'dispatcher',
      title: '调度器（Dispatcher）',
      desc: '外置控制回路的轮询与并发上限（规则驱动，非 LLM）。',
      fields: [
        { path: 'dispatcher.interval_seconds', label: '轮询间隔', type: 'number', min: 1, step: 1, unit: '秒' },
        { path: 'dispatcher.stability_window_seconds', label: '两次 spawn 最小间隔', type: 'number', min: 0, unit: '秒' },
        { path: 'dispatcher.max_concurrent_solvers', label: '并发 Solver 上限', type: 'number', min: 1 }
      ]
    },
    {
      id: 'challenge',
      title: '题目（并发）',
      desc: '题目模块批量启动自主解题时的并发控制。',
      fields: [
        { path: 'challenge.max_concurrent_runs', label: '同时解题的题目数上限', type: 'number', min: 1, max: 32, unit: '题',
          hint: '默认 3。超出上限的题目会排队，待有题目结束后自动补位。' }
      ]
    },
    {
      id: 'termination',
      title: '终止判定',
      desc: '挑战时间预算与「图静止」判定条件。',
      fields: [
        { path: 'termination.default_time_budget_minutes', label: '默认时间预算', type: 'number', min: 1, unit: '分钟' },
        { path: 'termination.min_advances_before_exhausted', label: '判定静止所需最少推进次数', type: 'number', min: 0 }
      ]
    },
    {
      id: 'observer',
      title: '旁路纠偏（Observer）',
      desc: '只提醒不决策：识别原地打转、漂移与重复踩坑。',
      groups: [
        {
          title: '总开关与阈值',
          fields: [
            { path: 'observer.enabled', label: '启用纠偏', type: 'bool' },
            { path: 'observer.cooldown_per_solver_minutes', label: '同一 Solver 提醒限频', type: 'number', min: 0, unit: '分钟' },
            { path: 'observer.max_reminders_per_solver', label: '单 Solver 提醒上限', type: 'number', min: 0 },
            { path: 'observer.repeat_tool_threshold', label: '重复工具阈值', type: 'number', min: 1 },
            { path: 'observer.no_progress_minutes', label: '无进展判定', type: 'number', min: 1, unit: '分钟' },
            { path: 'observer.failure_similarity_threshold', label: '失败相似度阈值', type: 'number', min: 0, max: 1, step: 0.05 }
          ]
        },
        {
          title: '小模型判官（judge）',
          fields: [
            { path: 'observer.judge.enabled', label: '启用判官', type: 'bool' },
            { path: 'observer.judge.model_channel', label: '判官通道', type: 'channel' }
          ]
        }
      ]
    },
    {
      id: 'blackboard',
      title: '黑板（Blackboard）',
      desc: '事实注入与检索条数上限。',
      fields: [
        { path: 'blackboard.enabled', label: '启用黑板', type: 'bool' },
        { path: 'blackboard.inject_index_limit', label: '注入条数上限', type: 'number', min: 0 },
        { path: 'blackboard.search_limit', label: '检索返回上限', type: 'number', min: 0 }
      ]
    },
    {
      id: 'knowledge',
      title: '本地技巧库（BM25）',
      desc: '基于 skills/**.md 的本地检索，零外部依赖。',
      fields: [
        { path: 'knowledge.enabled', label: '启用技巧库', type: 'bool' },
        { path: 'knowledge.corpus_dir', label: '语料目录', type: 'text', monospace: true },
        { path: 'knowledge.index_path', label: '索引文件', type: 'text', monospace: true },
        { path: 'knowledge.top_k', label: '返回条数', type: 'number', min: 1 },
        { path: 'knowledge.chunk_size', label: '分块大小', type: 'number', min: 1 },
        { path: 'knowledge.chunk_overlap', label: '分块重叠', type: 'number', min: 0 }
      ]
    },
    {
      id: 'vision',
      title: '图像理解（Vision）',
      desc: 'analyze_image 视觉旁路，需要一个支持视觉的模型通道。',
      fields: [
        { path: 'vision.enabled', label: '启用视觉旁路', type: 'bool' },
        { path: 'vision.channel', label: '视觉通道', type: 'channel', hint: '留空 = 不可用。' },
        { path: 'vision.max_image_bytes', label: '单图大小上限', type: 'number', min: 0, unit: '字节' }
      ]
    },
    {
      id: 'attachment',
      title: '附件上传',
      desc: '对话附件的大小与数量限制。',
      fields: [
        { path: 'attachment.enabled', label: '启用附件', type: 'bool' },
        { path: 'attachment.max_file_mb', label: '单文件上限', type: 'number', min: 1, max: 2048, unit: 'MB' },
        { path: 'attachment.max_count', label: '单次数量上限', type: 'number', min: 1, max: 100 }
      ]
    }
  ];

  /* 不可热更新的段（仅展示，提示直接改 config.yaml） */
  var READONLY_SECTIONS = [
    {
      id: 'docker_worker',
      title: '容器化 Worker（docker_worker）',
      desc: '接口不支持热更新，需直接编辑 config.yaml 后重启。',
      fields: [
        { path: 'docker_worker.enabled', label: '启用', type: 'bool' },
        { path: 'docker_worker.image', label: '镜像', type: 'text', monospace: true }
      ]
    },
    {
      id: 'mcp',
      title: 'MCP 工具服务器（mcp）',
      desc: 'servers 为列表结构，接口不支持热更新，需直接编辑 config.yaml 后重启。',
      fields: [
        { path: 'mcp.enabled', label: '启用', type: 'bool' },
        { path: 'mcp.servers', label: '服务器数量', type: 'readonly' }
      ]
    }
  ];

  /* ------------------------------- 内部状态 ------------------------------- */

  var opts = null;
  var baseline = {};   // 最近一次「已加载 / 已保存」的配置
  var draft = {};      // 表单工作副本
  var rootEl = null;   // #config-form
  var editorEl = null; // #config-editor（高级 JSON）
  var dirtyEl = null;  // #config-dirty
  var wired = false;

  /* -------------------------------- 小工具 -------------------------------- */

  function clone(x) { return JSON.parse(JSON.stringify(x == null ? {} : x)); }

  function el(tag, cls, text) {
    var e = doc.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  function getPath(obj, path) {
    var parts = path.split('.'), cur = obj, i;
    for (i = 0; i < parts.length; i++) {
      if (cur == null || typeof cur !== 'object') return undefined;
      cur = cur[parts[i]];
    }
    return cur;
  }

  function setPath(obj, path, value) {
    var parts = path.split('.'), cur = obj, i;
    for (i = 0; i < parts.length - 1; i++) {
      if (typeof cur[parts[i]] !== 'object' || cur[parts[i]] === null) cur[parts[i]] = {};
      cur = cur[parts[i]];
    }
    cur[parts[parts.length - 1]] = value;
  }

  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

  function channelKeys() {
    var chans = (draft.ai && draft.ai.channels) || {};
    return Object.keys(chans);
  }

  function notify(msg, tone) {
    if (opts && typeof opts.notice === 'function') opts.notice(msg, tone || 'info');
  }

  /** 递归求差：只返回发生变化的叶子（数组整体替换）。无变化返回 undefined。 */
  function diff(base, cur) {
    if (cur === undefined) return undefined;
    if (cur === null || typeof cur !== 'object' || Array.isArray(cur)) {
      return same(base, cur) ? undefined : cur;
    }
    var out = {}, changed = false;
    Object.keys(cur).forEach(function (k) {
      var d = diff(base && typeof base === 'object' ? base[k] : undefined, cur[k]);
      if (d !== undefined) { out[k] = d; changed = true; }
    });
    return changed ? out : undefined;
  }

  /* ------------------------------ 控件工厂 ------------------------------ */

  function makeSwitch(path) {
    var wrap = el('label', 'cfg-switch');
    var input = el('input');
    input.type = 'checkbox';
    input.checked = !!getPath(draft, path);
    var track = el('span', 'cfg-track');
    track.appendChild(el('span', 'cfg-thumb'));
    var text = el('span', 'cfg-switch-text', input.checked ? '已开启' : '已关闭');
    input.addEventListener('change', function () {
      setPath(draft, path, input.checked);
      text.textContent = input.checked ? '已开启' : '已关闭';
      updateDirty();
    });
    wrap.appendChild(input);
    wrap.appendChild(track);
    wrap.appendChild(text);
    return wrap;
  }

  function makeSelect(path, options, nullable) {
    var sel = el('select', 'cfg-select');
    options.forEach(function (opt) {
      var o = el('option', null, opt[1]);
      o.value = opt[0];
      sel.appendChild(o);
    });
    var cur = getPath(draft, path);
    sel.value = cur === null || cur === undefined ? '' : String(cur);
    sel.addEventListener('change', function () {
      var v = sel.value;
      if (nullable && v === '') v = null;
      setPath(draft, path, v);
      updateDirty();
    });
    return sel;
  }

  function makeChannelSelect(path) {
    var options = [['', '（留空 / 复用默认通道）']].concat(channelKeys().map(function (k) { return [k, k]; }));
    return makeSelect(path, options, true);
  }

  function makeNumber(path, field) {
    var box = el('div', 'cfg-numbox');
    var input = el('input', 'cfg-input');
    input.type = 'number';
    if (field.min !== undefined) input.min = field.min;
    if (field.max !== undefined) input.max = field.max;
    input.step = field.step !== undefined ? field.step : 1;
    var cur = getPath(draft, path);
    input.value = cur === null || cur === undefined ? '' : cur;
    input.addEventListener('input', function () {
      var raw = input.value.trim();
      var v;
      if (raw === '') v = field.nullable ? null : 0;
      else { v = Number(raw); if (isNaN(v)) return; }
      setPath(draft, path, v);
      updateDirty();
    });
    box.appendChild(input);
    if (field.unit) box.appendChild(el('span', 'cfg-unit', field.unit));
    return box;
  }

  function makeText(path, field) {
    if (field.type === 'password') {
      var box = el('div', 'cfg-secret');
      var input = el('input', 'cfg-input');
      input.type = 'password';
      input.spellcheck = false;
      input.value = getPath(draft, path) || '';
      if (field.placeholder) input.placeholder = field.placeholder;
      var toggle = el('button', 'cfg-reveal', '显示');
      toggle.type = 'button';
      toggle.addEventListener('click', function () {
        var show = input.type === 'password';
        input.type = show ? 'text' : 'password';
        toggle.textContent = show ? '隐藏' : '显示';
      });
      input.addEventListener('input', function () { setPath(draft, path, input.value); updateDirty(); });
      box.appendChild(input);
      box.appendChild(toggle);
      return box;
    }
    var t = el('input', 'cfg-input');
    t.type = 'text';
    t.spellcheck = false;
    if (field.monospace) t.classList.add('mono');
    if (field.placeholder) t.placeholder = field.placeholder;
    t.value = getPath(draft, path) || '';
    t.addEventListener('input', function () { setPath(draft, path, t.value); updateDirty(); });
    return t;
  }

  function makeTextarea(path, field) {
    var ta = el('textarea', 'cfg-textarea');
    ta.spellcheck = false;
    ta.rows = 3;
    if (field.placeholder) ta.placeholder = field.placeholder;
    ta.value = getPath(draft, path) || '';
    ta.addEventListener('input', function () { setPath(draft, path, ta.value); updateDirty(); });
    return ta;
  }

  /** 标签编辑器：chips + 输入框（回车/逗号添加）。 */
  function makeList(path, field) {
    var wrap = el('div', 'cfg-tags');
    var values = getPath(draft, path);
    if (!Array.isArray(values)) values = [];

    function commit() {
      setPath(draft, path, values.slice());
      updateDirty();
    }

    function paint() {
      wrap.textContent = '';
      values.forEach(function (v, idx) {
        var chip = el('span', 'cfg-tag');
        chip.appendChild(el('span', 'cfg-tag-text', v));
        var x = el('button', 'cfg-tag-x', '×');
        x.type = 'button';
        x.title = '移除';
        x.addEventListener('click', function () { values.splice(idx, 1); paint(); commit(); });
        chip.appendChild(x);
        wrap.appendChild(chip);
      });
      var input = el('input', 'cfg-tag-input');
      input.type = 'text';
      input.placeholder = '输入后回车添加…';
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ',') {
          e.preventDefault();
          add(input.value);
        } else if (e.key === 'Backspace' && !input.value && values.length) {
          values.pop(); paint(); commit();
        }
      });
      input.addEventListener('blur', function () { if (input.value.trim()) add(input.value); });
      wrap.appendChild(input);
    }

    function add(raw) {
      var parts = String(raw || '').split(/[,\n]/);
      parts.forEach(function (p) {
        p = p.trim();
        if (p && values.indexOf(p) < 0) values.push(p);
      });
      paint();
      commit();
    }

    paint();
    return wrap;
  }

  function makeReadonly(path) {
    var v = getPath(draft, path);
    if (Array.isArray(v)) v = v.length + ' 项';
    return el('span', 'cfg-readonly', v === undefined || v === null ? '—' : String(v));
  }

  function makeControl(field) {
    switch (field.type) {
      case 'bool': return makeSwitch(field.path);
      case 'select': return makeSelect(field.path, field.options || [], field.nullable);
      case 'channel': return makeChannelSelect(field.path);
      case 'number': return makeNumber(field.path, field);
      case 'password': return makeText(field.path, field);
      case 'list': return makeList(field.path, field);
      case 'textarea': return makeTextarea(field.path, field);
      case 'readonly': return makeReadonly(field.path);
      default: return makeText(field.path, field);
    }
  }

  function makeField(field) {
    var isToggle = field.type === 'bool';
    var row = el('div', 'cfg-field' + (field.wide || field.type === 'list' || field.type === 'textarea' ? ' wide' : '') + (isToggle ? ' toggle' : ''));
    var label = el('label', 'cfg-label');
    label.appendChild(el('span', 'cfg-label-text', field.label));
    row.appendChild(label);
    row.appendChild(makeControl(field));
    if (field.hint) row.appendChild(el('span', 'cfg-hint', field.hint));
    return row;
  }

  function makeFields(fields) {
    var grid = el('div', 'cfg-fields');
    (fields || []).forEach(function (f) { grid.appendChild(makeField(f)); });
    return grid;
  }

  /* --------------------------- AI 模型管理（DSH 设置页模式） --------------------------- */
  /* 模型 = 通道：每张卡一个模型，添加 / 编辑 / 删除 / 设默认都走专用端点即改即存，
     并镜像进 draft（「保存并写入本地」persist 同一状态）。composer 发送键旁的模型
     下拉（js/models.js）与本页共享 GET /api/models 数据源。 */

  function aiChannels() { return (draft.ai && draft.ai.channels) || {}; }

  /* 探测结果仅当次页面有效（刷新即消失）；顺手清掉早期版本的持久化数据 */
  try { global.localStorage.removeItem('ctfa.modelProbe'); } catch (e) { /* 隐私模式 */ }

  function numOrNull(v) {
    var s = String(v == null ? '' : v).trim();
    if (!s) return null;
    var n = Number(s);
    return isNaN(n) ? null : n;
  }

  function makeAiSection(sec) {
    var wrap = el('div', 'cfg-ai');
    wrap.appendChild(el('p', 'cfg-sec-desc',
      '每个模型就是一个可选通道：在这里添加 / 编辑 / 删除，然后在对话输入框的发送键左侧随时切换。会话未指定模型时，按默认通道执行。'));

    var grid = el('div', 'model-cards');
    wrap.appendChild(grid);

    var addCard = el('button', 'model-card model-card-add');
    addCard.type = 'button';
    addCard.appendChild(el('span', 'model-card-add-plus', '＋'));
    addCard.appendChild(el('span', null, '添加模型'));
    addCard.addEventListener('click', function () { openEditor(null); });
    grid.appendChild(addCard);

    // 高级：failover 顺序（沿用草稿 + 「保存并写入本地」）
    var adv = el('details', 'cfg-advanced');
    adv.appendChild(el('summary', null, '高级：failover 顺序'));
    var advBody = el('div', 'cfg-fields');
    advBody.appendChild(makeField({
      path: 'ai.channel_order', label: 'failover 顺序', type: 'list', wide: true,
      hint: '回车添加通道名；默认通道失败后按此顺序切换（仅在会话未指定模型时生效）。'
    }));
    adv.appendChild(advBody);
    wrap.appendChild(adv);

    /* ---------------- 卡片渲染 ---------------- */
    function renderCards(models, defaultChannel) {
      // 只移除 grid 的直接子元素（卡片 / 错误行）：编辑器卡片嵌在
      // .model-editor-wrap 里，对非直接子元素调 removeChild 会抛
      // NotFoundError 中断整个重绘
      Array.prototype.slice.call(grid.children).forEach(function (n) {
        if (n === addCard) return;
        var cls = n.classList || { contains: function () { return false; } };
        if (cls.contains('model-card') || cls.contains('model-cards-error')) {
          grid.removeChild(n);
        }
      });
      (models || []).forEach(function (m) {
        grid.insertBefore(makeModelCard(m, m.id === defaultChannel), addCard);
      });
    }

    function makeModelCard(m, isDefault) {
      var card = el('article', 'model-card');
      var head = el('div', 'model-card-head');
      head.appendChild(el('span', 'model-card-name', m.name || m.id));
      if (isDefault) head.appendChild(el('span', 'cfg-badge ok', '默认'));
      card.appendChild(head);
      card.appendChild(el('div', 'model-card-sub', m.model || ''));
      var meta = el('div', 'model-card-meta');
      meta.appendChild(el('span', 'model-card-provider', m.provider || ''));
      // 密钥已存储是常态，不展示徽标；仅缺失时给出可操作的警告
      if (!m.hasKey) {
        meta.appendChild(el('span', 'model-card-key warn', '⚠ 未设置密钥'));
      }
      card.appendChild(meta);
      var acts = el('div', 'model-card-acts');
      var testBtn = el('button', 'mini-btn', '测试');
      testBtn.type = 'button';
      testBtn.title = '对该模型发一次最小真实请求（探测当前已保存的配置）';
      testBtn.addEventListener('click', function () { probe(m.id, testBtn, card); });
      var editBtn = el('button', 'mini-btn', '编辑');
      editBtn.type = 'button';
      editBtn.addEventListener('click', function () { openEditor(m.id); });
      var defBtn = el('button', 'mini-btn', '设为默认');
      defBtn.type = 'button';
      defBtn.disabled = isDefault;
      defBtn.addEventListener('click', function () { setDefault(m.id); });
      var delBtn = el('button', 'mini-btn danger', '删除');
      delBtn.type = 'button';
      delBtn.addEventListener('click', function () { remove(m.id, m.name || m.id); });
      acts.appendChild(testBtn);
      acts.appendChild(editBtn);
      acts.appendChild(defBtn);
      acts.appendChild(delBtn);
      card.appendChild(acts);
      return card;
    }

    /* ---------------- 编辑器（DSH ProviderEditor：主字段 API Key + 折叠高级） ---------------- */
    function openEditor(key) {
      var oldE = grid.querySelector('.model-editor-wrap');
      if (oldE) grid.removeChild(oldE);
      var existing = key ? aiChannels()[key] : null;
      var isNew = !existing;

      var wrapE = el('div', 'model-editor-wrap');
      var card = el('article', 'model-card model-editor');
      var head = el('div', 'model-card-head');
      head.appendChild(el('span', 'model-card-name', isNew ? '添加模型' : '编辑模型'));
      card.appendChild(head);

      function field(label, input, hint, target) {
        var f = el('label', 'cfg-field');
        f.appendChild(el('span', 'cfg-field-label', label));
        f.appendChild(input);
        if (hint) f.appendChild(el('span', 'cfg-hint', hint));
        (target || body).appendChild(f);
        return f;
      }
      function textIn(value, placeholder, mono) {
        var i = el('input', 'cfg-input' + (mono ? ' mono' : ''));
        i.type = 'text';
        i.value = value || '';
        if (placeholder) i.placeholder = placeholder;
        return i;
      }

      var body = el('div', 'cfg-fields');
      var keyIn = textIn(key, '模型标识，如 glm / local', true);
      if (!isNew) keyIn.disabled = true;
      field('模型标识', keyIn, isNew ? '添加后不可更改。' : '不可更改。');
      var nameIn = textIn(existing && existing.name, '显示名，留空用模型标识');
      field('显示名称', nameIn, '在发送键旁的下拉里显示。');
      var urlIn = textIn(existing && existing.base_url, 'https://api.deepseek.com/v1', true);
      field('Base URL', urlIn);
      var secretIn = el('input', 'cfg-input mono');
      secretIn.type = 'password';
      secretIn.autocomplete = 'off';
      secretIn.placeholder = existing ? '已存储 · 保持不变（输入新值可替换）' : 'sk-...';
      field('API Key', secretIn, '明文仅存本机配置文件，请勿外传。');
      var modelIn = textIn(existing && existing.model, 'deepseek-chat / glm-4.5 等', true);
      field('模型 ID', modelIn);
      card.appendChild(body);

      // 高级参数（折叠）
      var advE = el('details', 'cfg-advanced');
      advE.appendChild(el('summary', null, '高级参数'));
      var advB = el('div', 'cfg-fields');
      var provSel = el('select', 'cfg-input');
      PROVIDERS.forEach(function (p) {
        var o = el('option', null, p[1]);
        o.value = p[0];
        provSel.appendChild(o);
      });
      provSel.value = (existing && existing.provider) || 'openai_compatible';
      field('服务商类型', provSel, null, advB);
      var tempIn = textIn(existing && existing.temperature, '0.3');
      tempIn.type = 'number';
      field('temperature', tempIn, '留空 = 模型默认。', advB);
      var maxTokIn = textIn(existing && existing.max_completion_tokens, '16384');
      maxTokIn.type = 'number';
      field('单次输出上限', maxTokIn, 'tokens。', advB);
      var timeoutIn = textIn(existing && existing.timeout_seconds, '600');
      timeoutIn.type = 'number';
      field('请求超时', timeoutIn, '秒。', advB);
      var reaSel = el('select', 'cfg-input');
      (REASONING || []).forEach(function (r) {
        var o = el('option', null, r[1]);
        o.value = r[0];
        reaSel.appendChild(o);
      });
      reaSel.value = (existing && existing.reasoning_effort) || '';
      field('reasoning_effort', reaSel, null, advB);
      var toolsChk = el('input', 'cfg-check');
      toolsChk.type = 'checkbox';
      toolsChk.checked = existing ? existing.supports_tools !== false : true;
      field('支持工具调用', toolsChk, null, advB);
      var visionChk = el('input', 'cfg-check');
      visionChk.type = 'checkbox';
      visionChk.checked = !!(existing && existing.supports_vision);
      field('支持图像输入', visionChk, null, advB);
      advE.appendChild(advB);
      card.appendChild(advE);

      var acts = el('div', 'model-card-acts');
      var cancelBtn = el('button', 'mini-btn', '取消');
      cancelBtn.type = 'button';
      cancelBtn.addEventListener('click', function () {
        if (wrapE.parentNode) wrapE.parentNode.removeChild(wrapE);
      });
      var saveBtn = el('button', 'btn', '保存');
      saveBtn.type = 'button';
      saveBtn.addEventListener('click', function () {
        var k = keyIn.value.trim();
        if (!/^[A-Za-z0-9_\-]+$/.test(k)) { notify('模型标识只能包含字母、数字、下划线或短横线', 'error'); return; }
        if (isNew && aiChannels()[k]) { notify('模型「' + k + '」已存在', 'error'); return; }
        if (!modelIn.value.trim()) { notify('请填写模型 ID', 'error'); return; }
        var payload = {
          key: k,
          name: nameIn.value.trim() || k,
          provider: provSel.value || 'openai_compatible',
          base_url: urlIn.value.trim(),
          model: modelIn.value.trim(),
          temperature: numOrNull(tempIn.value),
          max_completion_tokens: numOrNull(maxTokIn.value),
          timeout_seconds: numOrNull(timeoutIn.value),
          reasoning_effort: reaSel.value || null,
          supports_tools: toolsChk.checked,
          supports_vision: visionChk.checked
        };
        var secret = secretIn.value.trim();
        if (secret) payload.api_key = secret;
        CTFA.api.request('/api/ai/channels', { method: 'POST', body: payload }).then(function () {
          // 镜像进 draft：与落盘状态一致，「保存并写入本地」persist 同一份
          var merged = {};
          if (existing) { for (var kk in existing) merged[kk] = existing[kk]; }
          merged.name = payload.name;
          merged.provider = payload.provider;
          merged.base_url = payload.base_url;
          merged.model = payload.model;
          merged.temperature = payload.temperature;
          merged.max_completion_tokens = payload.max_completion_tokens;
          merged.timeout_seconds = payload.timeout_seconds;
          merged.reasoning_effort = payload.reasoning_effort;
          merged.supports_tools = payload.supports_tools;
          merged.supports_vision = payload.supports_vision;
          if (payload.api_key) merged.api_key = payload.api_key;
          if (!draft.ai) draft.ai = {};
          if (!draft.ai.channels) draft.ai.channels = {};
          draft.ai.channels[k] = merged;
          notify(isNew ? ('已添加模型「' + k + '」') : ('已更新模型「' + k + '」'), 'ok');
          if (wrapE.parentNode) wrapE.parentNode.removeChild(wrapE);
          refreshAll();
        }).catch(function (err) {
          notify('保存失败：' + (err && err.message ? err.message : err), 'error');
        });
      });
      acts.appendChild(cancelBtn);
      acts.appendChild(saveBtn);
      card.appendChild(acts);

      wrapE.appendChild(card);
      grid.insertBefore(wrapE, addCard);
      var firstIn = card.querySelector('input:not([disabled])');
      if (firstIn) firstIn.focus();
    }

    /* ---------------- 连通性探测（DSH ProviderEditor 的 probe） ---------------- */
    function probe(key, btn, card) {
      var oldText = btn.textContent;
      btn.disabled = true;
      btn.textContent = '探测中…';
      var stale = card.querySelector('.model-card-probe');
      if (stale) card.removeChild(stale);
      var showLine = function (ok, text) {
        var line = el('div', 'model-card-probe ' + (ok ? 'ok' : 'warn'));
        line.textContent = text;
        card.appendChild(line);
      };
      CTFA.api.verifyConfig([key]).then(function (res) {
        var item = ((res && res.results) || [])[0] || { ok: false, error: '端点未返回结果' };
        if (item.ok) {
          showLine(true, '✓ 连通（' + (item.elapsedMs || 0) + 'ms，model=' + (item.model || '') + '）');
        } else {
          showLine(false, '✖ ' + (item.error || '探测失败') +
            (item.hint ? '（建议：' + item.hint + '）' : ''));
        }
      }).catch(function (err) {
        showLine(false, '✖ ' + (err && err.message ? err.message : err));
      }).then(function () {
        btn.disabled = false;
        btn.textContent = oldText;
      });
    }

    /* ---------------- 写操作（即改即存） ---------------- */
    function setDefault(key) {
      CTFA.api.request('/api/ai/default-channel', { method: 'POST', body: { channel: key } }).then(function () {
        if (!draft.ai) draft.ai = {};
        draft.ai.default_channel = key;
        notify('默认模型已切换为「' + key + '」', 'ok');
        refreshAll();
      }).catch(function (err) {
        notify('切换默认失败：' + (err && err.message ? err.message : err), 'error');
      });
    }

    function remove(key, label) {
      if (!global.confirm('确定删除模型「' + label + '」？该通道的配置与密钥将一并移除。')) return;
      CTFA.api.request('/api/ai/channels/' + encodeURIComponent(key), { method: 'DELETE' }).then(function () {
        if (draft.ai && draft.ai.channels) delete draft.ai.channels[key];
        if (draft.ai && draft.ai.default_channel === key) {
          var rest = Object.keys(draft.ai.channels || {});
          draft.ai.default_channel = rest[0] || '';
        }
        notify('已删除模型「' + label + '」', 'ok');
        refreshAll();
      }).catch(function (err) {
        notify('删除失败：' + (err && err.message ? err.message : err), 'error');
      });
    }

    function showCardsError(text) {
      var box = el('div', 'model-cards-error');
      box.textContent = text;
      var retry = el('button', 'mini-btn', '重试');
      retry.type = 'button';
      retry.addEventListener('click', function () { refreshAll(); });
      box.appendChild(retry);
      grid.insertBefore(box, addCard);
    }

    function refreshAll() {
      CTFA.api.models().then(function (d) {
        renderCards((d && d.models) || [], (d && d.defaultChannel) || '');
        // 同步 composer 的模型下拉
        if (CTFA.models && typeof CTFA.models.refresh === 'function') CTFA.models.refresh();
      }).catch(function () {
        // 目录端点不可用（服务重启 / 网络抖动）：退回配置草稿渲染——
        // 配置页本身就持有全部通道数据，不该只剩一个光添加按钮
        var chans = aiChannels();
        var keys = Object.keys(chans);
        if (keys.length) {
          var def = (draft.ai && draft.ai.default_channel) || keys[0];
          renderCards(keys.map(function (k) {
            var ch = chans[k] || {};
            return {
              id: k, name: ch.name || k, model: ch.model || '',
              provider: ch.provider || '', baseUrl: ch.base_url || '',
              hasKey: !!ch.api_key, isDefault: k === def
            };
          }), def);
        } else {
          showCardsError('模型目录加载失败：请确认服务正常运行，然后');
        }
      });
    }

    refreshAll();
    return wrap;
  }

  /* -------------------------------- 渲染 -------------------------------- */

  function sectionBody(sec) {
    if (sec.channels) return makeAiSection(sec);
    if (sec.groups) {
      var g = el('div', 'cfg-groups');
      sec.groups.forEach(function (grp) {
        var box = el('div', 'cfg-group');
        box.appendChild(el('div', 'cfg-group-title', grp.title));
        box.appendChild(makeFields(grp.fields));
        g.appendChild(box);
      });
      return g;
    }
    return makeFields(sec.fields);
  }

  function buildSection(sec, readonly) {
    var s = el('section', 'cfg-sec' + (readonly ? ' readonly' : ''));
    s.id = 'cfg-sec-' + sec.id;
    var head = el('div', 'cfg-sec-head');
    head.appendChild(el('h3', 'cfg-sec-title', sec.title));
    if (readonly) head.appendChild(el('span', 'cfg-badge warn', '仅展示'));
    s.appendChild(head);
    if (sec.desc) s.appendChild(el('p', 'cfg-sec-desc', sec.desc));
    s.appendChild(sectionBody(sec));
    return s;
  }

  function repaint() {
    if (!rootEl) return;
    rootEl.textContent = '';
    var nav = el('div', 'cfg-nav');
    SECTIONS.concat(READONLY_SECTIONS).forEach(function (sec) {
      var s = buildSection(sec, READONLY_SECTIONS.indexOf(sec) >= 0);
      rootEl.appendChild(s);
      var link = el('button', 'cfg-nav-btn', sec.title);
      link.type = 'button';
      link.addEventListener('click', function () {
        s.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
      nav.appendChild(link);
    });
    rootEl.insertBefore(nav, rootEl.firstChild);
    updateDirty();
  }

  function render(config) {
    baseline = clone(config || {});
    draft = clone(config || {});
    if (editorEl) editorEl.value = JSON.stringify(baseline, null, 2);
    repaint();
  }

  function updateDirty() {
    var patch = diff(baseline, draft);
    var dirty = !!patch;
    if (dirtyEl) dirtyEl.hidden = !dirty;
    var saveBtn = doc.getElementById('btn-config-save');
    if (saveBtn) saveBtn.disabled = !dirty;
  }

  /* ------------------------------ 动作（按钮） ------------------------------ */

  function save() {
    var patch = diff(baseline, draft);
    if (!patch) { notify('配置没有改动，无需保存', 'info'); return; }
    var saveBtn = doc.getElementById('btn-config-save');
    if (saveBtn) saveBtn.disabled = true;
    CTFA.api.putConfig(patch).then(function (res) {
      var cfg = (res && res.config) || draft;
      if (opts && typeof opts.onConfig === 'function') opts.onConfig(cfg, res);
      render(cfg);
      var changed = (res && res.changedSections) || [];
      if (res && res.persistedPath && !changed.length) {
        notify('配置无实际变化（未重写文件）：' + res.persistedPath, 'info');
      } else {
        notify(
          res && res.persistedPath
            ? '配置已保存并写入本地：' + res.persistedPath
              + (changed.length ? '（变更：' + changed.join('、') + '）' : '') + '，重启后依然生效'
            : '配置已保存（仅内存，未写盘）',
          'ok'
        );
      }
    }).catch(function (err) {
      notify('保存失败：' + ((err && err.message) || err), 'error');
      updateDirty();
    });
  }

  function reset() {
    if (!diff(baseline, draft)) { notify('没有需要撤销的修改', 'info'); return; }
    render(baseline);
    notify('已撤销未保存的修改', 'info');
  }

  function reload() {
    if (opts && typeof opts.reload === 'function') {
      opts.reload().then(function () { notify('已重新加载配置', 'info'); });
    }
  }

  function saveRaw() {
    if (!editorEl) return;
    var text = editorEl.value.trim();
    var parsed;
    try { parsed = text ? JSON.parse(text) : {}; }
    catch (e) { notify('JSON 解析失败：' + e.message, 'error'); return; }
    CTFA.api.putConfig(parsed).then(function (res) {
      var cfg = (res && res.config) || parsed;
      if (opts && typeof opts.onConfig === 'function') opts.onConfig(cfg, res);
      render(cfg);
      var changed = (res && res.changedSections) || [];
      notify('已按完整 JSON 覆盖保存' + (changed.length ? '（变更：' + changed.join('、') + '）' : ''), 'ok');
    }).catch(function (err) {
      notify('保存失败：' + ((err && err.message) || err), 'error');
    });
  }

  function on(id, fn) {
    var node = doc.getElementById(id);
    if (node) node.addEventListener('click', fn);
    return node;
  }

  function init(options) {
    opts = options || {};
    rootEl = doc.getElementById('config-form');
    editorEl = doc.getElementById('config-editor');
    dirtyEl = doc.getElementById('config-dirty');
    if (wired) return;
    wired = true;
    on('btn-config-save', save);
    on('btn-config-reset', reset);
    on('btn-config-reload', reload);
    on('btn-config-save-raw', saveRaw);
    on('btn-config-raw-reload', function () {
      if (editorEl) editorEl.value = JSON.stringify(baseline, null, 2);
      notify('已重新载入当前配置到 JSON 编辑器', 'info');
    });
  }

  CTFA.configForm = {
    init: init,
    render: render,
    save: save,
    reset: reset,
    /* 供调试/测试：递归求差，只返回发生变化的叶子 */
    _diff: diff
  };
})(window);
