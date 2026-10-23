/* ============================================================================
 * conversations.js — 会话删除 / 批量删除（U2b）+ 重命名（U2c）
 * ----------------------------------------------------------------------------
 * - 单条删除：会话项悬停出现「×」，弹确认框（二次确认，明确列出将被清理的内容）；
 * - 批量删除：切到"批量管理"后勾选，支持全选/清空，一次最多 100 个（后端同样限制）；
 * - 重命名：会话项悬停出现「✎」，标题原地变成输入框，Enter/失焦提交、Esc 取消；
 * - 运行中保护：确认框会提示"运行中的任务会先被取消"，后端取消失败时返回 409 并原样展示；
 * - 无任何静默删除：所有删除入口都必须经过确认框。
 * 对外：CTFA.conversations.*
 * ========================================================================== */
(function (global) {
  'use strict';

  var CTFA = (global.CTFA = global.CTFA || {});
  var doc = global.document;

  var MAX_BATCH = 100;

  var state = { mode: false, selected: {}, busy: false };
  var hooks = {};

  function el(tag, cls, text) {
    var node = doc.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function conversations() {
    return typeof hooks.getConversations === 'function' ? hooks.getConversations() || [] : [];
  }

  function activeId() {
    return typeof hooks.getConversationId === 'function' ? hooks.getConversationId() : '';
  }

  function titleOf(conversation) {
    return (conversation && (conversation.title || conversation.id)) || '未命名会话';
  }

  /* ---------------------------- 确认对话框 ---------------------------- */

  function confirmDialog(options) {
    var opts = options || {};
    return new Promise(function (resolve) {
      var mask = el('div', 'modal-mask');
      var box = el('div', 'modal-box');
      box.appendChild(el('h3', 'modal-title', opts.title || '请确认'));
      var body = el('div', 'modal-body');
      (opts.lines || []).forEach(function (line) { body.appendChild(el('p', 'modal-line', line)); });
      box.appendChild(body);
      var actions = el('div', 'modal-actions');
      var cancel = el('button', 'btn', opts.cancelText || '取消');
      cancel.type = 'button';
      var confirm = el('button', 'btn ' + (opts.danger === false ? 'primary' : 'danger'), opts.confirmText || '确认删除');
      confirm.type = 'button';
      var done = function (value) {
        mask.remove();
        doc.removeEventListener('keydown', onKey);
        resolve(value);
      };
      var onKey = function (evt) { if (evt.key === 'Escape') done(false); };
      cancel.addEventListener('click', function () { done(false); });
      confirm.addEventListener('click', function () { done(true); });
      mask.addEventListener('click', function (evt) { if (evt.target === mask) done(false); });
      doc.addEventListener('keydown', onKey);
      actions.appendChild(cancel);
      actions.appendChild(confirm);
      box.appendChild(actions);
      mask.appendChild(box);
      doc.body.appendChild(mask);
      confirm.focus();
    });
  }

  function notice(text, tone) {
    if (CTFA.timeline && typeof CTFA.timeline.notice === 'function') {
      CTFA.timeline.notice(text, tone || 'info');
    }
  }

  /* ------------------------------ 单条删除 ------------------------------ */

  function deleteOne(conversation) {
    if (state.busy) return Promise.resolve(false);
    var id = conversation.id;
    var running = isRunning(id);
    return confirmDialog({
      title: '删除会话',
      lines: [
        '将永久删除：' + titleOf(conversation),
        '清理内容：全部消息、黑板事实、执行记录、运行记录、上传的附件与磁盘目录。',
        running
          ? '⚠ 该会话正在运行任务：会先取消运行，取消成功后再删除。'
          : '此操作不可恢复（数据库与磁盘文件都会被移除）。'
      ],
      confirmText: '永久删除'
    }).then(function (ok) {
      if (!ok) return false;
      state.busy = true;
      return CTFA.api.deleteConversation(id).then(function () {
        notice('已删除会话：' + titleOf(conversation), 'ok');
        if (activeId() === id && typeof hooks.onDeleted === 'function') hooks.onDeleted(id);
        return reload();
      }).catch(function (err) {
        notice('删除失败：' + ((err && err.message) || String(err)), 'error');
        return false;
      }).finally(function () { state.busy = false; });
    });
  }

  /* ------------------------------ 批量删除 ------------------------------ */

  function selectedIds() {
    return Object.keys(state.selected).filter(function (id) { return state.selected[id]; });
  }

  function toggleSelect(id, on) {
    state.selected[id] = !!on;
    renderBulkBar();
  }

  function batchDelete() {
    var ids = selectedIds();
    if (!ids.length) { notice('请先勾选要删除的会话', 'warn'); return Promise.resolve(false); }
    if (ids.length > MAX_BATCH) { notice('单批最多删除 ' + MAX_BATCH + ' 个会话', 'warn'); return Promise.resolve(false); }
    var running = ids.filter(isRunning);
    return confirmDialog({
      title: '批量删除 ' + ids.length + ' 个会话',
      lines: [
        '将永久删除这 ' + ids.length + ' 个会话及其消息、黑板事实、执行记录、附件与磁盘目录。',
        running.length
          ? '⚠ 其中 ' + running.length + ' 个正在运行任务：会先取消运行，取消失败的那几个将保留。'
          : '此操作不可恢复，请确认已不再需要这些内容。'
      ],
      confirmText: '删除 ' + ids.length + ' 个'
    }).then(function (ok) {
      if (!ok) return false;
      state.busy = true;
      return CTFA.api.batchDeleteConversations(ids).then(function (data) {
        var deleted = (data && data.deleted) || 0;
        var failed = (data && data.failed) || 0;
        var failedIds = ((data && data.results) || []).filter(function (r) { return !r.ok; });
        notice('批量删除完成：成功 ' + deleted + '，失败 ' + failed,
          failed ? 'warn' : 'ok');
        failedIds.forEach(function (r) { notice('未删除 ' + r.id + '：' + (r.error || '未知原因'), 'warn'); });
        state.selected = {};
        var current = activeId();
        if (current && ids.indexOf(current) >= 0 && typeof hooks.onDeleted === 'function') hooks.onDeleted(current);
        return reload();
      }).catch(function (err) {
        notice('批量删除失败：' + ((err && err.message) || String(err)), 'error');
        return false;
      }).finally(function () { state.busy = false; });
    });
  }

  /* ------------------------------ 重命名（U2c） ------------------------------ */

  /**
   * 内联重命名：把会话项的标题换成输入框。
   * Enter / 失焦提交；Esc 取消还原；点输入框不会触发"打开会话"。
   */
  function startRename(conversation, li) {
    if (!li || !conversation) return;
    var titleSpan = li.querySelector('.li-title');
    if (!titleSpan || li.querySelector('.li-title-input')) return; // 已在编辑中
    var original = conversation.title || '';

    var input = doc.createElement('input');
    input.type = 'text';
    input.className = 'li-title-input';
    input.value = original;
    input.maxLength = 200;
    input.setAttribute('aria-label', '会话新标题');

    var finished = false;
    function restore() {
      if (input.parentNode) input.parentNode.replaceChild(titleSpan, input);
    }
    function commit() {
      if (finished) return;
      finished = true;
      var value = input.value.trim();
      if (!value) { notice('标题不能为空', 'warn'); restore(); return; }
      if (value === original) { restore(); return; }
      CTFA.api.renameConversation(conversation.id, value).then(function (data) {
        var title = (data && data.conversation && data.conversation.title) || value;
        notice('已重命名：' + title, 'ok');
        return reload();
      }).catch(function (err) {
        notice('重命名失败：' + ((err && err.message) || String(err)), 'error');
        restore();
      });
    }
    input.addEventListener('click', function (evt) { evt.stopPropagation(); });
    input.addEventListener('keydown', function (evt) {
      evt.stopPropagation();
      if (evt.key === 'Enter') { evt.preventDefault(); commit(); }
      else if (evt.key === 'Escape') { finished = true; restore(); }
    });
    input.addEventListener('blur', commit);

    titleSpan.parentNode.replaceChild(input, titleSpan);
    input.focus();
    input.select();
  }

  /* ------------------------------ 渲染装饰 ------------------------------ */

  /** app.js 渲染每个会话项后调用：批量模式下加勾选框，否则加悬停重命名/删除按钮。 */
  function decorate(li, conversation) {
    if (!li || !conversation) return;
    if (state.mode) {
      var label = el('label', 'bulk-check');
      var box = doc.createElement('input');
      box.type = 'checkbox';
      box.checked = !!state.selected[conversation.id];
      box.addEventListener('click', function (evt) { evt.stopPropagation(); });
      box.addEventListener('change', function () { toggleSelect(conversation.id, box.checked); });
      label.appendChild(box);
      li.insertBefore(label, li.firstChild);
      li.classList.add('bulk');
      li.addEventListener('click', function (evt) {
        evt.preventDefault();
        box.checked = !box.checked;
        toggleSelect(conversation.id, box.checked);
      }, true);
      return;
    }
    var ren = el('button', 'li-rename', '✎');
    ren.type = 'button';
    ren.title = '重命名该会话';
    ren.setAttribute('aria-label', '重命名会话 ' + titleOf(conversation));
    ren.addEventListener('click', function (evt) {
      evt.stopPropagation();
      evt.preventDefault();
      startRename(conversation, li);
    });
    li.appendChild(ren);
    var del = el('button', 'li-del', '×');
    del.type = 'button';
    del.title = '删除该会话（需二次确认）';
    del.setAttribute('aria-label', '删除会话 ' + titleOf(conversation));
    del.addEventListener('click', function (evt) {
      evt.stopPropagation();
      evt.preventDefault();
      deleteOne(conversation);
    });
    li.appendChild(del);
  }

  function isRunning(conversationId) {
    var runs = typeof hooks.getActiveRuns === 'function' ? hooks.getActiveRuns() || [] : [];
    return runs.some(function (run) {
      return run && run.conversationId === conversationId && run.status === 'running';
    });
  }

  function renderBulkBar() {
    var bar = doc.getElementById('bulk-bar');
    if (!bar) return;
    bar.hidden = !state.mode;
    if (!state.mode) return;
    var count = selectedIds().length;
    var countEl = doc.getElementById('bulk-count');
    if (countEl) countEl.textContent = '已选 ' + count + ' / 最多 ' + MAX_BATCH;
    var delBtn = doc.getElementById('btn-bulk-delete');
    if (delBtn) delBtn.disabled = count === 0;
    var toggle = doc.getElementById('btn-bulk-mode');
    if (toggle) {
      toggle.textContent = state.mode ? '退出批量' : '批量管理';
      toggle.setAttribute('aria-pressed', String(state.mode));
    }
    var list = doc.getElementById('conversation-list');
    if (list) list.classList.toggle('bulk-mode', state.mode);
  }

  function setMode(on) {
    state.mode = !!on;
    if (!state.mode) state.selected = {};
    renderBulkBar();
    if (typeof hooks.onRefresh === 'function') hooks.onRefresh();
  }

  function selectAll(on) {
    state.selected = {};
    if (on) {
      conversations().slice(0, MAX_BATCH).forEach(function (c) { state.selected[c.id] = true; });
      if (conversations().length > MAX_BATCH) notice('只勾选前 ' + MAX_BATCH + ' 个（单批上限）', 'warn');
    }
    if (typeof hooks.onRefresh === 'function') hooks.onRefresh();
  }

  function reload() {
    return typeof hooks.onReload === 'function' ? Promise.resolve(hooks.onReload()) : Promise.resolve();
  }

  function init(options) {
    hooks = options || {};
    var toggle = doc.getElementById('btn-bulk-mode');
    if (toggle) toggle.addEventListener('click', function () { setMode(!state.mode); });
    var all = doc.getElementById('btn-bulk-all');
    if (all) {
      all.addEventListener('click', function () {
        selectAll(selectedIds().length < conversations().length);
      });
    }
    var del = doc.getElementById('btn-bulk-delete');
    if (del) del.addEventListener('click', function () { batchDelete(); });
    renderBulkBar();
  }

  CTFA.conversations = {
    init: init,
    decorate: decorate,
    deleteOne: deleteOne,
    batchDelete: batchDelete,
    startRename: startRename,
    confirmDialog: confirmDialog,
    isRunning: isRunning,
    setMode: setMode,
    state: state,
    MAX_BATCH: MAX_BATCH
  };
})(window);