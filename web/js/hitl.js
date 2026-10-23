/* ============================================================================
 * hitl.js — 多模式审查（U6c）：会话审查模式切换器 + 审查历史弹窗
 * ----------------------------------------------------------------------------
 * - 审查模式三选项：🧑 人工审查 / 🤖 Agent 快速审查 / ⚡ 完全访问；
 *   切换条常驻在输入区（composer）下方，未选会话时点击会提示先选会话；
 * - 会话级覆盖 > 全局默认（inherited=true 显示"跟随全局"提示）；
 * - 切"完全访问"必须过确认框（"所有命令将不经审查直接执行"）；
 * - 完全访问生效时切换条旁常驻黄色 ⚡ 徽标；
 * - 「📜 审查历史」按钮打开弹窗：按会话/审查者/裁决过滤 GET /api/hitl/decisions，
 *   点开看参数 JSON、改参 diff 与执行结果摘要（审计闭环）。
 * 对外：CTFA.hitl.*
 * ========================================================================== */
(function (global) {
  'use strict';

  var CTFA = (global.CTFA = global.CTFA || {});
  var doc = global.document;

  var REVIEWER_LABELS = {
    human: '🧑 人工审查',
    audit_agent: '🤖 Agent 快速审查',
    none: '⚡ 完全访问'
  };

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

  function conversationId() {
    return typeof hooks.getConversationId === 'function' ? hooks.getConversationId() || '' : '';
  }

  /* --------------------------- 审查模式切换器 --------------------------- */

  /** 渲染切换条；payload 为空（未选会话/尚未拉取）时显示中性提示。 */
  function renderReviewer(payload) {
    var current = payload && payload.reviewer ? String(payload.reviewer) : '';
    var inherited = !!(payload && payload.inherited);
    Array.prototype.forEach.call(doc.querySelectorAll('#reviewer-switch button'), function (btn) {
      var on = !!current && btn.getAttribute('data-reviewer') === current;
      btn.classList.toggle('on', on);
      btn.setAttribute('aria-pressed', String(on));
    });
    var badge = doc.getElementById('reviewer-none-badge');
    if (badge) badge.hidden = current !== 'none';
    var cur = doc.getElementById('reviewer-current');
    if (cur) cur.textContent = current ? (REVIEWER_LABELS[current] || current) : '审查模式';
    var hint = doc.getElementById('reviewer-hint');
    if (hint) {
      if (!current) {
        hint.textContent = '选择会话后可切换';
      } else if (inherited) {
        hint.textContent = '当前跟随全局（' + REVIEWER_LABELS[current] + '），切换后对本会话立即生效';
      } else {
        hint.textContent = '本会话已覆盖，切换后对后续调用立即生效';
      }
    }
  }

  function loadReviewer() {
    var id = conversationId();
    if (!id) { renderReviewer(null); return Promise.resolve(); }
    return CTFA.api.conversationHitl(id).then(renderReviewer).catch(function () {
      /* 会话刚创建尚未落库等场景：保持中性提示 */
    });
  }

  function setReviewer(reviewer) {
    var id = conversationId();
    if (!id) { notice('请先选择或创建会话，再切换审查模式', 'warn'); return Promise.resolve(false); }
    var ask = Promise.resolve(true);
    if (reviewer === 'none' && CTFA.conversations && typeof CTFA.conversations.confirmDialog === 'function') {
      ask = CTFA.conversations.confirmDialog({
        title: '开启完全访问模式',
        lines: [
          '所有命令将不经审查直接执行（本会话）。',
          '执行治理仍然生效：硬超时、无输出终止、workspace 隔离不会被绕过。',
          '确认开启？'
        ],
        confirmText: '确认开启',
        danger: false
      });
    }
    return ask.then(function (ok) {
      if (!ok) { loadReviewer(); return false; }   // 取消 → 回显当前状态
      return CTFA.api.setConversationHitl(id, reviewer === 'inherit' ? null : reviewer)
        .then(function (payload) {
          renderReviewer(payload);
          var panel = doc.getElementById('reviewer-panel');
          var toggle = doc.getElementById('reviewer-toggle');
          if (panel) panel.hidden = true;
          if (toggle) toggle.setAttribute('aria-expanded', 'false');
          notice('审查模式已切换：' + (REVIEWER_LABELS[payload.reviewer] || payload.reviewer), 'ok');
          return true;
        })
        .catch(function (err) {
          notice('切换失败：' + ((err && err.message) || String(err)), 'error');
          return false;
        });
    });
  }

  /* ------------------------------ 审查历史弹窗 ------------------------------ */

  function prettyJson(value) {
    if (value === undefined || value === null || value === '') return '—';
    try { return JSON.stringify(value, null, 2); } catch (e) { return String(value); }
  }

  function reviewerLabel(value) {
    return { human: '🧑 人工', audit_agent: '🤖 Agent 审计', policy: '⚡ 完全访问', whitelist: '✓ 白名单' }[value] || value || '—';
  }

  function renderHistory(root, decisions) {
    var box = root.querySelector('.hitl-history-list');
    if (!box) return;
    box.textContent = '';
    if (!decisions || !decisions.length) {
      box.appendChild(el('div', 'hitl-history-empty muted', '暂无裁决记录'));
      return;
    }
    decisions.forEach(function (d) {
      var row = el('div', 'hitl-row');
      row.setAttribute('data-decision', d.decision || '');
      row.setAttribute('data-reviewer', d.reviewer || '');
      var head = el('div', 'hitl-row-head');
      head.appendChild(el('span', 'hitl-row-verdict', d.decision === 'approve' ? '放行' : '拒绝'));
      head.appendChild(el('span', 'hitl-row-tool', d.toolName || '—'));
      head.appendChild(el('span', 'hitl-row-reviewer', reviewerLabel(d.reviewer)));
      head.appendChild(el('span', 'hitl-row-conv mono', d.conversationId || ''));
      if (d.elapsedMs !== null && d.elapsedMs !== undefined) {
        head.appendChild(el('span', 'hitl-row-elapsed', (d.elapsedMs / 1000).toFixed(1) + 's'));
      }
      head.appendChild(el('span', 'hitl-row-time', String(d.createdAt || '').replace('T', ' ').slice(0, 19)));
      row.appendChild(head);
      if (d.comment) row.appendChild(el('div', 'hitl-row-comment', d.comment));

      var detail = el('pre', 'hitl-row-detail mono');
      detail.hidden = true;
      var lines = ['参数：' + prettyJson(d.arguments)];
      if (d.editedArguments) lines.push('审计改参（完整替换后）：' + prettyJson(d.editedArguments));
      if (d.executionId) {
        lines.push('执行闭环：' + d.executionId);
        lines.push('结果摘要：' + (d.executionResultDigest || '（无）'));
      }
      detail.textContent = lines.join('\n');
      row.appendChild(detail);
      head.addEventListener('click', function () { detail.hidden = !detail.hidden; });
      box.appendChild(row);
    });
  }

  function loadHistory(root) {
    var convInput = root.querySelector('.hitl-filter-conv');
    var reviewerSel = root.querySelector('.hitl-filter-reviewer');
    var decisionSel = root.querySelector('.hitl-filter-decision');
    var box = root.querySelector('.hitl-history-list');
    if (!box) return Promise.resolve();
    box.textContent = '';
    box.appendChild(el('div', 'hitl-history-empty muted', '加载中…'));
    return CTFA.api.hitlDecisions({
      conversationId: convInput ? convInput.value.trim() : '',
      reviewer: reviewerSel ? reviewerSel.value : '',
      decision: decisionSel ? decisionSel.value : '',
      limit: 100
    }).then(function (res) {
      renderHistory(root, (res && res.decisions) || []);
    }).catch(function (err) {
      box.textContent = '';
      box.appendChild(el('div', 'hitl-history-empty', '加载失败：' + ((err && err.message) || err)));
    });
  }

  /** 打开审查历史弹窗（动态构建，Esc / 点遮罩 / 关闭按钮均可关）。 */
  function openHistory() {
    if (doc.querySelector('.modal-overlay.hitl-history-overlay')) return; // 已打开
    var overlay = el('div', 'modal-overlay hitl-history-overlay');
    var modal = el('div', 'modal modal-wide');
    overlay.appendChild(modal);

    var head = el('header', 'modal-head');
    head.appendChild(el('span', 'modal-icon', '📜'));
    head.appendChild(el('span', 'modal-title', '审查历史'));
    head.appendChild(el('span', 'modal-subtitle', 'GET /api/hitl/decisions · 裁决留痕与执行闭环'));
    var close = el('button', 'modal-close', '×');
    close.type = 'button';
    close.setAttribute('aria-label', '关闭审查历史');
    head.appendChild(close);
    modal.appendChild(head);

    var body = el('div', 'modal-body hitl-history');
    var filters = el('div', 'hitl-history-filters');
    var convInput = el('input', 'hitl-filter-conv');
    convInput.type = 'text';
    convInput.placeholder = '会话 ID（留空=全部）';
    convInput.spellcheck = false;
    if (conversationId()) convInput.value = conversationId(); // 默认过滤当前会话
    var reviewerSel = el('select', 'hitl-filter-reviewer');
    [['', '全部审查者'], ['human', '🧑 人工'], ['audit_agent', '🤖 Agent 审计'],
     ['policy', '⚡ 完全访问'], ['whitelist', '✓ 白名单']].forEach(function (pair) {
      var opt = el('option', '', pair[1]);
      opt.value = pair[0];
      reviewerSel.appendChild(opt);
    });
    var decisionSel = el('select', 'hitl-filter-decision');
    [['', '全部裁决'], ['approve', '放行'], ['reject', '拒绝']].forEach(function (pair) {
      var opt = el('option', '', pair[1]);
      opt.value = pair[0];
      decisionSel.appendChild(opt);
    });
    var loadBtn = el('button', 'btn hitl-history-load', '查询');
    loadBtn.type = 'button';
    filters.appendChild(convInput);
    filters.appendChild(reviewerSel);
    filters.appendChild(decisionSel);
    filters.appendChild(loadBtn);
    body.appendChild(filters);

    var list = el('div', 'hitl-history-list');
    list.appendChild(el('div', 'hitl-history-empty muted', '加载中…'));
    body.appendChild(list);
    modal.appendChild(body);
    doc.body.appendChild(overlay);
    overlay.classList.add('show'); // .modal-overlay 默认 opacity:0，必须加 .show 才可见

    function dismiss() { overlay.remove(); doc.removeEventListener('keydown', onKey); }
    function onKey(evt) { if (evt.key === 'Escape') dismiss(); }
    close.addEventListener('click', dismiss);
    overlay.addEventListener('click', function (evt) { if (evt.target === overlay) dismiss(); });
    doc.addEventListener('keydown', onKey);
    loadBtn.addEventListener('click', function () { loadHistory(modal); });
    reviewerSel.addEventListener('change', function () { loadHistory(modal); });
    decisionSel.addEventListener('change', function () { loadHistory(modal); });

    loadHistory(modal);
  }

  /* -------------------------------- 初始化 -------------------------------- */

  function init(options) {
    hooks = options || {};
    Array.prototype.forEach.call(doc.querySelectorAll('#reviewer-switch button'), function (btn) {
      btn.addEventListener('click', function () {
        setReviewer(btn.getAttribute('data-reviewer'));
      });
    });
    var historyBtn = doc.getElementById('btn-hitl-history');
    if (historyBtn) historyBtn.addEventListener('click', function () { openHistory(); });
    // 面板开合：点击小按钮向上展开；点击面板外自动收起
    var toggle = doc.getElementById('reviewer-toggle');
    var panel = doc.getElementById('reviewer-panel');
    if (toggle && panel) {
      toggle.addEventListener('click', function (e) {
        e.stopPropagation();
        var show = panel.hidden;
        panel.hidden = !show;
        toggle.setAttribute('aria-expanded', String(show));
      });
      doc.addEventListener('click', function (e) {
        if (panel.hidden) return;
        if (e.target.closest('#reviewer-panel') || e.target.closest('#reviewer-toggle')) return;
        panel.hidden = true;
        toggle.setAttribute('aria-expanded', 'false');
      });
    }
    renderReviewer(null); // 初始中性态：无高亮 + "选择会话后可切换"
  }

  CTFA.hitl = {
    init: init,
    loadReviewer: loadReviewer,
    setReviewer: setReviewer,
    openHistory: openHistory,
    loadHistory: loadHistory,
    renderReviewer: renderReviewer,
    REVIEWER_LABELS: REVIEWER_LABELS
  };
})(window);
