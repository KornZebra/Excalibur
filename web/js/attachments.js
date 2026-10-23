/* ============================================================================
 * attachments.js — 对话附件（U2a）
 * ----------------------------------------------------------------------------
 * 能力：
 *   - 回形针按钮选择文件 + 拖拽到对话区 + 输入框粘贴，三种入口统一走上传队列；
 *   - 上传前本地预校验（数量/体积/扩展名白名单，白名单来自 GET /api/config）；
 *   - 待发送区 chips：文件名/大小/类型/进度/失败重试/移除；
 *   - 上传完成把附件 ID 交给发送逻辑（`pendingIds()`），随消息一起提交；
 *   - 历史与实时消息里渲染附件卡片（下载/删除），数据来自 GET /api/conversations/{id}/attachments。
 * 约定：无附件时本模块完全不介入发送流程（行为与改造前一致）。
 * 对外：CTFA.attachments.*
 * ========================================================================== */
(function (global) {
  'use strict';

  var CTFA = (global.CTFA = global.CTFA || {});
  var doc = global.document;

  var state = {
    config: null,          // attachment 配置（lazy：GET /api/config）
    conversationId: '',
    pending: [],           // [{tempId, file, name, size, kind, status, progress, id, error}]
    byMessage: {},         // messageId -> [attachment]
    byId: {},              // attachmentId -> attachment
    uploading: false,
    dialogOpen: false
  };

  var refs = {};
  var hooks = {};

  /* ------------------------------ 小工具 ------------------------------ */

  function el(tag, cls, text) {
    var node = doc.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function humanSize(bytes) {
    var value = Number(bytes) || 0;
    var units = ['B', 'KB', 'MB', 'GB'];
    var i = 0;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
    return (i === 0 ? value.toFixed(0) : value.toFixed(1)) + units[i];
  }

  function extOf(name) {
    var parts = String(name || '').split('.');
    return parts.length > 1 ? parts.pop().toLowerCase() : '';
  }

  function kindIcon(kind) {
    return ({ traffic: '🌐', archive: '🗜', binary: '⚙', disk: '💽', memory: '🧠', image: '🖼',
      audio: '🎧', video: '🎬', text: '📄', code: '📜', doc: '📕', database: '🗃', key: '🔑' })[kind] || '📎';
  }

  function notice(text, tone) {
    if (CTFA.timeline && typeof CTFA.timeline.notice === 'function') {
      CTFA.timeline.notice(text, tone || 'info');
    }
  }

  function tempId() {
    return 'tmp-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 7);
  }

  /* --------------------------- 配置与预校验 --------------------------- */

  function cfg() {
    return (state.config && state.config.attachment) || { enabled: true, max_file_mb: 50, max_count: 10, allowed_extensions: [] };
  }

  function loadConfig() {
    if (state.config) return Promise.resolve(state.config);
    return CTFA.api.getConfig().then(function (data) {
      state.config = data || {};
      return state.config;
    }).catch(function () {
      state.config = {};
      return state.config;
    });
  }

  /** 本地预校验：返回 {ok, reason}。后端仍会二次校验（不信任前端）。 */
  function precheck(file) {
    var c = cfg();
    if (!c.enabled) return { ok: false, reason: '附件上传未启用（attachment.enabled=false）' };
    var limit = (c.max_file_mb || 50) * 1024 * 1024;
    if (file.size > limit) return { ok: false, reason: '超过单文件上限 ' + c.max_file_mb + 'MB（' + humanSize(file.size) + '）' };
    if (!file.size) return { ok: false, reason: '文件为空' };
    var ext = extOf(file.name);
    if (!ext) return { ok: false, reason: '文件没有扩展名，无法识别类型' };
    var allowed = (c.allowed_extensions || []).map(function (e) { return String(e).toLowerCase().replace(/^\./, ''); });
    if (allowed.length && allowed.indexOf(ext) < 0) return { ok: false, reason: '类型 .' + ext + ' 不在白名单内' };
    var done = state.pending.filter(function (p) { return p.status !== 'error'; }).length;
    if (done + 1 > (c.max_count || 10)) return { ok: false, reason: '本条消息最多 ' + c.max_count + ' 个附件' };
    return { ok: true };
  }

  /* ------------------------------- 会话 ------------------------------- */

  function currentConversationId() {
    if (state.conversationId) return state.conversationId;
    if (typeof hooks.getConversationId === 'function') return hooks.getConversationId() || '';
    return '';
  }

  /** 上传前确保有会话：没有就新建一个（与"新建会话"按钮同一条路径）。 */
  function ensureConversation() {
    var existing = currentConversationId();
    if (existing) return Promise.resolve(existing);
    return CTFA.api.createConversation('新会话（附件）').then(function (data) {
      var conversation = (data && data.conversation) || {};
      var id = conversation.id || '';
      state.conversationId = id;
      if (typeof hooks.onConversationCreated === 'function') hooks.onConversationCreated(conversation);
      return id;
    });
  }

  /* ------------------------------ 上 传 ------------------------------ */

  function addFiles(fileList) {
    var files = Array.prototype.slice.call(fileList || []);
    if (!files.length) return Promise.resolve();
    if (state.uploading) { notice('上一批附件还在上传中，请稍候', 'warn'); return Promise.resolve(); }
    return loadConfig().then(function () {
      var accepted = [];
      files.forEach(function (file) {
        var check = precheck(file);
        if (!check.ok) {
          notice(file.name + '：' + check.reason, 'warn');
          return;
        }
        var item = {
          tempId: tempId(), file: file, name: file.name, size: file.size,
          kind: kindOfName(file.name), status: 'pending', progress: 0, id: '', error: ''
        };
        state.pending.push(item);
        accepted.push(item);
      });
      renderChips();
      if (!accepted.length) return null;
      state.uploading = true;
      return ensureConversation().then(function (conversationId) {
        if (!conversationId) throw new Error('无法创建会话，附件未上传');
        return CTFA.api.uploadAttachments(conversationId, accepted.map(function (i) { return i.file; }),
          function (percent) {
            accepted.forEach(function (item) {
              if (item.status !== 'done') { item.status = 'uploading'; item.progress = percent; }
            });
            renderChips();
          }).then(function (data) {
          var uploaded = (data && data.attachments) || [];
          accepted.forEach(function (item, index) {
            var row = uploaded[index];
            if (!row) {
              item.status = 'error';
              item.error = '服务端未返回该文件';
              return;
            }
            item.status = 'done';
            item.progress = 100;
            item.id = row.id;
            item.kind = row.kind || item.kind;
          });
          state.uploading = false;
          renderChips();
          var failed = state.pending.filter(function (p) { return p.status === 'error'; });
          if (!failed.length) notice('已上传 ' + uploaded.length + ' 个附件', 'ok');
        }).catch(function (err) {
          state.uploading = false;
          var message = (err && err.message) || String(err);
          accepted.forEach(function (item) {
            if (item.status !== 'done') { item.status = 'error'; item.error = message; }
          });
          renderChips();
          notice('附件上传失败：' + message, 'error');
        });
      });
    });
  }

  function kindOfName(name) {
    var ext = extOf(name);
    var map = {
      pcap: 'traffic', pcapng: 'traffic', cap: 'traffic',
      zip: 'archive', rar: 'archive', '7z': 'archive', tar: 'archive', gz: 'archive',
      exe: 'binary', elf: 'binary', bin: 'binary', so: 'binary', dex: 'binary', apk: 'binary',
      jpg: 'image', jpeg: 'image', png: 'image', bmp: 'image', gif: 'image', webp: 'image', svg: 'image',
      py: 'code', c: 'code', cpp: 'code', js: 'code', sh: 'code',
      txt: 'text', md: 'text', log: 'text', json: 'text', yaml: 'text', yml: 'text', csv: 'text'
    };
    return map[ext] || 'unknown';
  }

  function removePending(item) {
    if (item.id) {
      // 已上传的：删掉服务端文件，避免留下孤儿附件
      CTFA.api.deleteAttachment(item.id).catch(function () { /* 忽略：本地先移除 */ });
    }
    state.pending = state.pending.filter(function (p) { return p !== item; });
    renderChips();
  }

  function retryPending(item) {
    state.pending = state.pending.filter(function (p) { return p !== item; });
    renderChips();
    return addFiles([item.file]);
  }

  function pendingIds() {
    return state.pending.filter(function (p) { return p.status === 'done' && p.id; })
      .map(function (p) { return p.id; });
  }

  function pendingSummary() {
    return state.pending.filter(function (p) { return p.status === 'done'; }).map(function (p) {
      return { id: p.id, filename: p.name, size: p.size, kind: p.kind, pending: true };
    });
  }

  /** 发送后：清空待发送区（附件已归属到消息）。 */
  function clearPending() {
    state.pending = [];
    renderChips();
  }

  /* --------------------------- 待发送 chips --------------------------- */

  function renderChips() {
    var host = refs.chips;
    if (!host) return;
    host.textContent = '';
    if (!state.pending.length) {
      host.hidden = true;
      return;
    }
    host.hidden = false;
    state.pending.forEach(function (item) {
      var chip = el('span', 'attach-chip');
      chip.setAttribute('data-status', item.status);
      chip.appendChild(el('span', 'attach-icon', kindIcon(item.kind)));
      chip.appendChild(el('span', 'attach-name', item.name));
      chip.appendChild(el('span', 'attach-size', humanSize(item.size)));
      if (item.status === 'uploading' || item.status === 'pending') {
        var bar = el('span', 'attach-bar');
        var fill = el('span', 'attach-bar-fill');
        fill.style.width = Math.max(4, item.progress || 0) + '%';
        bar.appendChild(fill);
        chip.appendChild(bar);
        chip.appendChild(el('span', 'attach-pct', (item.progress || 0) + '%'));
      } else if (item.status === 'error') {
        chip.setAttribute('title', item.error || '上传失败');
        chip.appendChild(el('span', 'attach-error', '失败'));
        var retry = el('button', 'attach-x', '↻');
        retry.type = 'button';
        retry.title = '重试上传';
        retry.addEventListener('click', function () { retryPending(item); });
        chip.appendChild(retry);
      } else {
        chip.appendChild(el('span', 'attach-ok', '✓'));
      }
      var remove = el('button', 'attach-x', '×');
      remove.type = 'button';
      remove.title = '移除该附件';
      remove.addEventListener('click', function () { removePending(item); });
      chip.appendChild(remove);
      host.appendChild(chip);
    });
  }

  /* --------------------------- 消息内卡片 --------------------------- */

  function cardFor(row) {
    var card = el('a', 'attach-card');
    card.href = CTFA.api.attachmentUrl(row.id);
    card.setAttribute('download', row.filename || '');
    card.title = CTFA.api.displayPath(row.storedPath) + (row.mimeWarning ? '\n⚠ ' + row.mimeWarning : '');
    card.setAttribute('data-id', row.id);
    card.appendChild(el('span', 'attach-icon', kindIcon(row.kind)));
    var meta = el('span', 'attach-meta');
    meta.appendChild(el('span', 'attach-name', row.filename || '附件'));
    meta.appendChild(el('span', 'attach-sub',
      (row.kind || 'unknown') + ' · ' + humanSize(row.size) + (row.sha256 ? ' · ' + row.sha256.slice(0, 8) : '')));
    card.appendChild(meta);
    return card;
  }

  function renderCardInto(messageId, rows) {
    if (!doc.querySelector) return;
    var host = doc.querySelector('[data-message-id="' + messageId + '"] .msg-attachments');
    if (!host) return;
    host.textContent = '';
    rows.forEach(function (row) { host.appendChild(cardFor(row)); });
    host.hidden = !rows.length;
  }

  /** 依据分组数据渲染全部消息卡片 */
  function renderAll() {
    Object.keys(state.byMessage).forEach(function (messageId) {
      if (!messageId || messageId === 'null') return;
      renderCardInto(messageId, state.byMessage[messageId]);
    });
  }

  /** 拉取会话附件并按 messageId 分组（历史加载/发送完成后调用） */
  function loadForConversation(conversationId) {
    var id = conversationId || currentConversationId();
    if (!id) { state.byMessage = {}; state.byId = {}; return Promise.resolve([]); }
    return CTFA.api.attachments(id).then(function (data) {
      var rows = (data && data.attachments) || [];
      state.byMessage = {};
      state.byId = {};
      rows.forEach(function (row) {
        state.byId[row.id] = row;
        var key = row.messageId || 'unlinked';
        (state.byMessage[key] = state.byMessage[key] || []).push(row);
      });
      renderAll();
      return rows;
    }).catch(function () { return []; });
  }

  /** 实时消息：已知 messageId 时把 pending 里已上传的附件挂到该消息 */
  function attachPendingTo(messageId) {
    var rows = state.byId ? Object.keys(state.byId).map(function (k) { return state.byId[k]; }) : [];
    if (!messageId || !rows.length) return;
    var mine = state.pending.filter(function (p) { return p.status === 'done' && p.id; })
      .map(function (p) { return state.byId[p.id] || { id: p.id, filename: p.name, size: p.size, kind: p.kind }; });
    if (!mine.length) return;
    state.byMessage[messageId] = mine;
    renderCardInto(messageId, mine);
  }

  /* ------------------------------ 交互绑定 ------------------------------ */

  function bindDropZone(node) {
    if (!node) return;
    var depth = 0;
    function over(evt) {
      if (!evt.dataTransfer) return;
      var types = evt.dataTransfer.types || [];
      if (types.indexOf && types.indexOf('Files') < 0) return;
      evt.preventDefault();
      evt.dataTransfer.dropEffect = 'copy';
      if (!state.dialogOpen) {
        state.dialogOpen = true;
        doc.body.classList.add('drop-active');
      }
    }
    node.addEventListener('dragenter', function (evt) { depth += 1; over(evt); });
    node.addEventListener('dragover', over);
    node.addEventListener('dragleave', function () {
      depth = Math.max(0, depth - 1);
      if (depth === 0) { state.dialogOpen = false; doc.body.classList.remove('drop-active'); }
    });
    node.addEventListener('drop', function (evt) {
      if (!evt.dataTransfer) return;
      evt.preventDefault();
      depth = 0;
      state.dialogOpen = false;
      doc.body.classList.remove('drop-active');
      var files = evt.dataTransfer.files;
      if (files && files.length) addFiles(files);
    });
  }

  function init(options) {
    var opts = options || {};
    hooks = opts;
    refs.chips = opts.chips || doc.getElementById('attach-chips');
    refs.input = opts.fileInput || doc.getElementById('attach-input');
    refs.button = opts.button || doc.getElementById('btn-attach');
    refs.overlay = opts.overlay || doc.getElementById('drop-overlay');
    refs.textarea = opts.textarea || doc.getElementById('input');

    if (refs.button && refs.input) {
      refs.button.addEventListener('click', function () { refs.input.click(); });
      refs.input.addEventListener('change', function () {
        addFiles(refs.input.files);
        refs.input.value = '';       // 允许再次选择同一文件
      });
    }
    if (refs.textarea) {
      refs.textarea.addEventListener('paste', function (evt) {
        var items = (evt.clipboardData && evt.clipboardData.files) || [];
        if (items && items.length) {
          evt.preventDefault();
          addFiles(items);
        }
      });
    }
    bindDropZone(opts.dropZone || doc.getElementById('timeline'));
    bindDropZone(opts.composer || doc.querySelector('.composer'));
    renderChips();
  }

  CTFA.attachments = {
    init: init,
    addFiles: addFiles,
    state: state,
    cfg: cfg,
    loadConfig: loadConfig,
    pendingIds: pendingIds,
    pendingSummary: pendingSummary,
    clearPending: clearPending,
    ensureConversation: ensureConversation,
    loadForConversation: loadForConversation,
    renderAll: renderAll,
    renderCardInto: renderCardInto,
    attachPendingTo: attachPendingTo,
    setConversation: function (id) { state.conversationId = id || ''; },
    humanSize: humanSize,
    kindIcon: kindIcon
  };
})(window);