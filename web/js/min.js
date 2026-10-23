/* ============================================================================
 * min.js — 极简隐藏模式（U10 / U11 / U12）
 * ----------------------------------------------------------------------------
 * 一个全屏、无干扰的解题窗口：只有「会话列表折叠按钮 / 输入框 / 附件按钮 / 发送按钮」。
 *
 * 约定（写死，界面不暴露）：
 *   - 审查模式 = 完全访问（reviewer=none）；
 *   - 运行模式 = multi + 深度编排（orchestration=deep）。
 *
 * 行为（U12 起）：
 *   - 解题过程**正常输出**：思考流 / 规划 / 工具调用 / 子代理 / Todo / 正文流，
 *     复用主界面的 CTFA.timeline 渲染层，视觉与主界面一致；
 *   - 命中 flag（flag_submit 工具成功）仍**立即**在顶部高亮输出；
 *   - 断线自动重连（带 Last-Event-ID），并按 runId 过滤旧任务补发事件。
 *
 * 依赖：js/api.js（REST + SSE）、js/markdown.js、js/timeline.js。
 * ========================================================================== */
(function (global) {
  'use strict';

  var CTFA = (global.CTFA = global.CTFA || {});
  var doc = global.document;
  var api = CTFA.api;

  var KEY_CONV = 'ctfa.minConv';
  var REVIEWER = 'none';        // 完全访问
  var MODE = 'multi';
  var ORCHESTRATION = 'deep';   // 深度编排
  var RECONNECT_MS = 2500;
  var STALL_MS = 8000;          // 发送后若迟迟收不到运行事件，判定游标失效并强制全量重放

  var state = {
    conversationId: '',
    runId: null,
    working: false,
    lastEventId: 0,
    cursorConv: '',           // lastEventId 所属会话（服务端事件序号是「每会话」的，切会话必须归零）
    sawRunEvent: false,       // 本轮是否已收到任何运行事件
    stallTimer: null,
    settled: false,           // 本轮是否已由历史记录收尾
    gotResponse: false,       // 本轮是否已收到正文/最终回复
    flags: [],
    pending: [],              // 待发送附件 [{id, name}]
    conversations: [],
    streamCtrl: null,
    streamConvId: ''
  };

  var refs = {};

  /* -------------------------------- 工具 -------------------------------- */

  function $(id) { return doc.getElementById(id); }

  function mk(tag, cls, text) {
    var node = doc.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  /** 毫秒时间戳 → 本地 `YYYY-MM-DD HH:MM:SS`（会话列表显示精确时间）。 */
  function fmtDateTime(ms) {
    if (!ms) return '';
    var d = new Date(Number(ms));
    if (isNaN(d.getTime())) return '';
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
      + ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  function setStatus(text, tone) {
    if (!refs.status) return;
    refs.status.textContent = text;
    refs.status.setAttribute('data-tone', tone || 'idle');
  }

  function rememberConversation(id) {
    try { global.localStorage.setItem(KEY_CONV, id || ''); } catch (e) { /* ignore */ }
  }

  function rememberedConversation() {
    try { return global.localStorage.getItem(KEY_CONV) || ''; } catch (e) { return ''; }
  }

  /** timeline 的 init / renderHistory 会塞入主界面的空态卡片，极简窗不需要。 */
  function stripEmpty() {
    if (!refs.streamInner) return;
    var empty = refs.streamInner.querySelector('.empty-state');
    if (empty) empty.remove();
  }

  /* ------------------------------ 视图切换 ------------------------------ */

  function showStream() {
    if (refs.stream) refs.stream.hidden = false;
    if (refs.hint) refs.hint.hidden = true;
  }

  function showHint() {
    if (refs.hint) refs.hint.hidden = false;
    if (refs.stream) refs.stream.hidden = true;
  }

  function setWorking(on) {
    state.working = !!on;
    if (refs.working) refs.working.hidden = !on;
    if (on) {
      setStatus('正在解题中', 'running');
      if (refs.send) {
        refs.send.textContent = '停止';
        refs.send.setAttribute('data-mode', 'stop');
      }
    } else {
      if (refs.send) {
        refs.send.textContent = '发送';
        refs.send.removeAttribute('data-mode');
      }
    }
  }

  function showError(message) {
    showStream();
    if (CTFA.timeline && typeof CTFA.timeline.showError === 'function') {
      CTFA.timeline.showError(message || '出错了');
    } else if (refs.streamInner) {
      refs.streamInner.appendChild(mk('div', 'mn-err', message || '出错了'));
    }
    setStatus('出错', 'error');
  }

  /* ------------------------------ flag 输出 ------------------------------ */

  function renderFlags() {
    var box = refs.flags;
    if (!box) return;
    box.textContent = '';
    if (!state.flags.length) { box.hidden = true; return; }
    box.hidden = false;
    state.flags.forEach(function (flag) {
      var item = mk('div', 'mn-flag');
      item.appendChild(mk('span', 'mn-flag-label', 'FLAG'));
      item.appendChild(doc.createTextNode(flag.value || ''));
      box.appendChild(item);
    });
  }

  function refreshFlags(immediate) {
    var id = state.conversationId;
    if (!id) return Promise.resolve();
    return api.flags(id).then(function (res) {
      var flags = (res && res.flags) || [];
      if (!flags.length) return;
      var seen = {};
      state.flags = flags.filter(function (f) {
        var v = (f && f.value) || '';
        if (!v || seen[v]) return false;
        seen[v] = true;
        return true;
      });
      renderFlags();
      if (immediate) showStream();
    }).catch(function () { /* ignore */ });
  }

  /* ------------------------------ 会话列表 ------------------------------ */

  function loadConversations() {
    return api.conversations().then(function (res) {
      state.conversations = (res && res.conversations) || [];
      renderConversations();
      return state.conversations;
    }).catch(function () {
      state.conversations = [];
      renderConversations();
      return [];
    });
  }

  function renderConversations() {
    var list = refs.list;
    if (!list) return;
    list.textContent = '';
    if (!state.conversations.length) {
      list.appendChild(mk('li', 'mn-empty', '暂无会话'));
      return;
    }
    state.conversations.forEach(function (c) {
      var li = mk('li', 'mn-item' + (c.id === state.conversationId ? ' on' : ''));
      li.appendChild(mk('span', 'mn-item-title', c.title || c.id));
      var when = fmtDateTime(c.updatedAt || c.createdAt);
      var sub = [c.projectId ? '已归属项目' : '未归属', when].filter(Boolean).join(' · ');
      li.appendChild(mk('span', 'mn-item-sub', sub));
      li.title = (c.title || c.id)
        + (c.createdAt ? '\n创建于 ' + fmtDateTime(c.createdAt) : '')
        + (c.updatedAt ? '\n更新于 ' + fmtDateTime(c.updatedAt) : '');
      li.addEventListener('click', function () {
        closeDrawer();
        selectConversation(c.id);
      });
      list.appendChild(li);
    });
  }

  /** 切换会话时必须归零事件游标：服务端序号是「每会话」的，沿用旧游标会被整段丢弃。 */
  function resetCursor(convId) {
    state.lastEventId = 0;
    state.cursorConv = convId || '';
  }

  function ensureConversation() {
    if (state.conversationId) return Promise.resolve(state.conversationId);
    return api.createConversation('新会话').then(function (res) {
      var conversation = (res && res.conversation) || {};
      if (!conversation.id) throw new Error('后端未返回会话 ID');
      state.conversationId = conversation.id;
      rememberConversation(conversation.id);
      resetCursor(conversation.id);          // 新会话：序号从 1 开始
      return loadConversations().then(function () { return conversation.id; });
    });
  }

  /** 清空时间线并回到「空闲提示」状态。 */
  function resetStream() {
    if (CTFA.timeline) {
      CTFA.timeline.clear();
      CTFA.timeline.renderTodos([]);
      CTFA.timeline.clearProgress();
    }
    stripEmpty();
    showHint();
  }

  /** 载入会话历史（含工具卡片与正文），便于切换会话后继续查看。 */
  function loadHistory(id) {
    if (!id) return Promise.resolve();
    return api.messages(id).then(function (res) {
      var messages = (res && res.messages) || [];
      if (!messages.length) return;
      if (CTFA.timeline) {
        CTFA.timeline.renderHistory(messages);
        stripEmpty();
      }
      showStream();
      setStatus('就绪', 'idle');
    }).catch(function () { /* ignore */ });
  }

  function selectConversation(id) {
    if (!id) { openStream(state.conversationId); return; }
    if (id !== state.conversationId) {
      closeStream();
      state.conversationId = id;
      rememberConversation(id);
      state.runId = null;
      resetCursor(id);
      state.flags = [];
      state.gotResponse = false;
      renderFlags();
      setWorking(false);
      resetStream();
      setStatus('就绪', 'idle');
      renderConversations();
      loadHistory(id);
    }
    openStream(id);
    recoverState(id);
  }

  function newConversation() {
    closeDrawer();
    api.createConversation('新会话').then(function (res) {
      var conversation = (res && res.conversation) || {};
      if (!conversation.id) return;
      closeStream();
      state.conversationId = conversation.id;
      rememberConversation(conversation.id);
      state.runId = null;
      resetCursor(conversation.id);
      state.flags = [];
      state.gotResponse = false;
      renderFlags();
      setWorking(false);
      resetStream();
      setStatus('就绪', 'idle');
      return loadConversations().then(function () { openStream(conversation.id); });
    }).catch(function (err) {
      showError('新建会话失败：' + ((err && err.message) || err));
    });
  }

  /** 刷新恢复：任务可能仍在运行（换页/重开窗口）。 */
  function recoverState(id) {
    if (!id) return;
    api.conversationState(id).then(function (st) {
      st = st || {};
      if (st.taskStatus === 'running') {
        state.runId = st.runId || null;
        state.lastEventId = st.lastEventId || state.lastEventId;
        showStream();
        if (CTFA.timeline) CTFA.timeline.beginRun();
        setWorking(true);
      }
    }).catch(function () { /* ignore */ });
  }

  /* ------------------------------- 附件 ------------------------------- */

  function renderChips() {
    var box = refs.chips;
    if (!box) return;
    box.textContent = '';
    box.hidden = !state.pending.length;
    state.pending.forEach(function (att, idx) {
      var chip = mk('span', 'mn-chip');
      chip.appendChild(mk('span', 'mn-chip-name', att.name || att.id));
      var x = mk('button', 'mn-chip-x', '×');
      x.type = 'button';
      x.title = '移除';
      x.addEventListener('click', function () { state.pending.splice(idx, 1); renderChips(); });
      chip.appendChild(x);
      box.appendChild(chip);
    });
  }

  function uploadFiles(files) {
    var list = Array.prototype.slice.call(files || []);
    if (!list.length) return Promise.resolve();
    setStatus('上传中…', 'running');
    return ensureConversation().then(function (cid) {
      return api.uploadAttachments(cid, list, null);
    }).then(function (res) {
      var items = (res && res.attachments) || [];
      items.forEach(function (att) {
        if (att && att.id) state.pending.push({ id: att.id, name: att.filename || att.id });
      });
      renderChips();
      setStatus('就绪', 'idle');
      return loadConversations();
    }).catch(function (err) {
      setStatus('上传失败', 'error');
      showError('附件上传失败：' + ((err && err.message) || err));
    });
  }

  /* ------------------------------ 事件流 ------------------------------ */

  function closeStream() {
    if (state.streamCtrl) {
      try { state.streamCtrl.abort(); } catch (e) { /* ignore */ }
      state.streamCtrl = null;
    }
    state.streamConvId = '';
  }

  /** 只接受当前运行的事件（会话流会补发旧任务事件）。 */
  function acceptRun(data) {
    if (!state.runId) return false;
    if (data && data.runId && data.runId !== state.runId) return false;
    return true;
  }

  /** timeline 方法的安全代理（timeline 未加载时退化为空操作）。 */
  function T(name) {
    if (CTFA.timeline && typeof CTFA.timeline[name] === 'function') return CTFA.timeline[name];
    return function () {};
  }

  function handlers(id) {
    // 运行级事件统一闸门：任务收尾（settled）后忽略迟到/重放事件，避免把历史视图写乱。
    function g(fn) {
      return function (d, raw, evId) {
        if (state.settled && !state.working) return;
        markRunEvent();
        return fn(d, raw, evId);
      };
    }
    return {
      /* ---- 思考 / 规划 / 清单 / 进度 ---- */
      thinking_stream_delta: g(function (d) { T('appendThinking')((d && d.delta) || ''); }),
      planning: g(function (d) { T('appendPlanning')((d && d.content) || ''); }),
      todos: g(function (d) { T('renderTodos')((d && d.todos) || []); }),
      progress: g(function (d) {
        d = d || {};
        T('setProgress')(d.stage, d.message, d.iteration);
      }),

      /* ---- 正文流 ---- */
      response_start: g(function () { T('beginResponse')(); }),
      response_delta: g(function (d) {
        state.gotResponse = true;
        T('appendResponse')((d && d.delta) || '');
      }),
      response: g(function (d) {
        var content = (d && d.content) || '';
        if (content) state.gotResponse = true;
        T('finalizeResponse')(content);
      }),

      /* ---- 工具调用与结果 ---- */
      tool_call: g(function (d) { T('upsertToolCall')(d || {}); }),
      tool_result: g(function (d) {
        d = d || {};
        T('setToolResult')(d);
        // 拿到 flag 立刻输出，不等任务结束
        if (d.name === 'flag_submit' && !d.isError && acceptRun(d)) refreshFlags(true);
      }),

      /* ---- 子代理流式文本 ---- */
      sub_agent_reply_stream_delta: g(function (d) {
        d = d || {};
        T('appendSubAgentDelta')(d.agentId, d.agentName, d.delta || '');
      }),

      /* ---- token 用量 ---- */
      usage_summary: g(function (d) { T('renderUsage')(d || {}); }),

      /* ---- 人工审批（reviewer=none 下一般不会触发，保留以兼容） ---- */
      hitl_request: g(function (d) {
        d = d || {};
        T('markToolApproval')(d.toolCallId);
        if (CTFA.timeline && typeof CTFA.timeline.showHitl === 'function') {
          CTFA.timeline.showHitl(d, function (requestId, payload) {
            return CTFA.api.hitl(requestId, payload);
          });
        }
      }),
      hitl_auto_decision: g(function (d) { T('appendAutoDecision')(d || {}); }),

      /* ---- 异常与结束 ---- */
      error: function (d) { showError((d && d.message) || '未知错误'); },
      cancelled: function (d) {
        // 本地切换会话导致的 abort：streamConvId 已被清空，直接忽略
        if (!state.streamConvId) return;
        if (d && d.reason === 'aborted') return;
        state.runId = null;
        clearStallWatch();
        setWorking(false);
        setStatus('已取消', 'warn');
        if (CTFA.timeline) CTFA.timeline.notice('运行已取消：' + ((d && d.reason) || '未提供原因'), 'warn');
      },
      task_done: function (d) {
        if (!acceptRun(d)) return;
        state.runId = null;
        clearStallWatch();
        setWorking(false);
        var cancelled = d && d.reason === 'cancelled';
        setStatus(cancelled ? '已取消' : '完成', cancelled ? 'warn' : 'done');
        T('finishRun')(cancelled ? '已取消' : '已完成', cancelled ? 'warn' : 'ok');
        finalize();
      },
      task_error: function (d) {
        if (!acceptRun(d)) return;
        state.runId = null;
        clearStallWatch();
        setWorking(false);
        setStatus('出错', 'error');
        T('finishRun')('出错', 'error');
        showError((d && d.message) || '任务执行失败');
        finalize();
      },
      /* 兜底：部分后端只在流末尾发 done */
      done: function (d) {
        if (state.runId && d && d.runId && d.runId !== state.runId) return;
        if (!state.runId) return;             // 已由 task_done 收尾
        state.runId = null;
        clearStallWatch();
        setWorking(false);
        setStatus('完成', 'done');
        T('finishRun')('已完成', 'ok');
        finalize();
      },

      onEvent: function (type, data, raw, evId) {
        if (!evId) return;
        var n = parseInt(evId, 10);
        if (!isNaN(n) && n > state.lastEventId) state.lastEventId = n;
      },
      onClose: function (info) {
        if (info && info.aborted) return;
        if (state.streamConvId !== id) return;
        global.setTimeout(function () {
          if (state.streamConvId === id) openStream(id);
        }, RECONNECT_MS);
      }
    };
  }

  function openStream(id) {
    if (!id) return;
    closeStream();
    state.streamConvId = id;
    // 游标只在同一会话内有效；跨会话沿用旧值会被服务端整段丢弃（seq <= last_seq）
    if (state.cursorConv !== id) resetCursor(id);
    var ctrl = new global.AbortController();
    state.streamCtrl = ctrl;
    api.streamConversationEvents(id, handlers(id), {
      controller: ctrl,
      lastEventId: state.lastEventId
    });
  }

  /* ------------------------- 运行事件看门狗 ------------------------- */

  function clearStallWatch() {
    if (state.stallTimer) { global.clearTimeout(state.stallTimer); state.stallTimer = null; }
  }

  function markRunEvent() {
    state.sawRunEvent = true;
    clearStallWatch();
  }

  /**
   * 发送后若迟迟收不到任何运行事件（典型原因：事件游标失效或连接未接上），
   * 强制把游标归零并重开会话流，让服务端全量重放——避免「全程空白」。
   */
  function armStallWatch(cid) {
    clearStallWatch();
    state.sawRunEvent = false;
    state.stallTimer = global.setTimeout(function () {
      state.stallTimer = null;
      if (!state.working || state.sawRunEvent) return;
      if (state.streamConvId !== cid) return;
      resetCursor(cid);
      openStream(cid);
      if (CTFA.timeline) CTFA.timeline.notice('事件流无响应，已重新同步会话', 'warn');
    }, STALL_MS);
  }

  /**
   * 任务结束：以「会话历史」为权威收尾。
   * 无论流式过程是否完整（断线、游标失效、事件被服务端按游标丢弃），
   * 这里都用 /messages 重建完整过程与最终结论 —— 保证不会出现空白页。
   */
  function finalize() {
    var id = state.conversationId;
    state.settled = true;
    clearStallWatch();
    var tasks = [refreshFlags(false)];
    if (id) {
      tasks.push(api.messages(id).then(function (res) {
        var messages = (res && res.messages) || [];
        if (!messages.length) return;
        if (CTFA.timeline) {
          CTFA.timeline.renderHistory(messages);
          stripEmpty();
        }
        if (!state.gotResponse) {
          for (var i = messages.length - 1; i >= 0; i--) {
            var m = messages[i];
            if (m && (m.role === 'assistant' || m.role === 'agent') && m.content) {
              state.gotResponse = true;
              break;
            }
          }
        }
      }).catch(function () { /* ignore */ }));
    }
    Promise.all(tasks).then(function () {
      stripEmpty();
      showStream();
      if (refs.stream) refs.stream.scrollTop = refs.stream.scrollHeight;
    });
  }

  /* ------------------------------- 发送 ------------------------------- */

  /** 开始一轮运行：清空上一轮视图、进入时间线。 */
  function beginRun() {
    state.gotResponse = false;
    state.settled = false;
    state.flags = [];
    renderFlags();
    if (CTFA.timeline) {
      CTFA.timeline.clear();
      CTFA.timeline.renderTodos([]);
      CTFA.timeline.clearProgress();
    }
    stripEmpty();
    showStream();
    if (CTFA.timeline) CTFA.timeline.beginRun();
    setWorking(true);
  }

  function send() {
    if (state.working) return;
    var text = (refs.input.value || '').trim();
    var attachmentIds = state.pending.map(function (a) { return a.id; });
    if (!text && !attachmentIds.length) return;

    beginRun();
    refs.input.value = '';
    autoGrow();

    ensureConversation().then(function (cid) {
      // 每次发送都确保「完全访问」；失败不阻断（后端可能已是该模式）
      return api.setConversationHitl(cid, REVIEWER).catch(function () { /* ignore */ }).then(function () {
        armStallWatch(cid);          // 事件流无响应时强制重新同步
        openStream(cid);
        return api.sendConversationMessage(cid, {
          text: text || '请分析附件内容并解题',
          mode: MODE,
          orchestration: ORCHESTRATION,
          attachmentIds: attachmentIds
        });
      });
    }).then(function (res) {
      state.runId = (res && (res.taskId || res.runId)) || null;
      state.pending = [];
      renderChips();
      loadConversations();
    }).catch(function (err) {
      state.runId = null;
      clearStallWatch();
      setWorking(false);
      showError('发送失败：' + ((err && err.message) || err));
    });
  }

  function stop() {
    if (!state.conversationId) return;
    setStatus('正在停止…', 'running');
    api.cancelConversationTask(state.conversationId, 'hard').catch(function () { /* ignore */ });
  }

  function onSendClick() {
    if (state.working) stop();
    else send();
  }

  /* ------------------------------ 抽屉 / 全屏 ------------------------------ */

  function openDrawer() {
    if (refs.drawer) refs.drawer.hidden = false;
    if (refs.backdrop) refs.backdrop.hidden = false;
    if (refs.menu) refs.menu.setAttribute('aria-expanded', 'true');
    loadConversations();
  }

  function closeDrawer() {
    if (refs.drawer) refs.drawer.hidden = true;
    if (refs.backdrop) refs.backdrop.hidden = true;
    if (refs.menu) refs.menu.setAttribute('aria-expanded', 'false');
  }

  function drawerOpen() { return !!(refs.drawer && !refs.drawer.hidden); }

  function toggleFullscreen() {
    var root = doc.documentElement;
    var active = doc.fullscreenElement || doc.webkitFullscreenElement;
    if (active) {
      var exit = doc.exitFullscreen || doc.webkitExitFullscreen;
      if (exit) { try { exit.call(doc); } catch (e) { /* ignore */ } }
      return;
    }
    var req = root.requestFullscreen || root.webkitRequestFullscreen;
    if (!req) return;
    try {
      var p = req.call(root);
      if (p && typeof p.catch === 'function') p.catch(function () { /* 用户拒绝：忽略 */ });
    } catch (e) { /* ignore */ }
  }

  function autoGrow() {
    var ta = refs.input;
    if (!ta) return;
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 170) + 'px';
  }

  /* --------------------- 双面切换（U11） --------------------- */

  /* 面 A：AI 解题（控制台）；面 B：CTF 编码解码工具箱（伪装） */
  var FACE_TITLES = { ai: '控制台', codec: 'CTF Codec Toolbox' };
  /* 落在这些元素上时双击不切换（避免影响选中/编辑/点按） */
  var BLANK_GUARD = 'input, textarea, button, select, a, label, .mn-item, .mn-chip, .mn-stream';

  function currentFace() {
    return (refs.root && refs.root.getAttribute('data-face')) || 'ai';
  }

  function setFace(name) {
    var face = FACE_TITLES[name] ? name : 'ai';
    if (refs.root) refs.root.setAttribute('data-face', face);
    try { doc.title = FACE_TITLES[face]; } catch (e) { /* ignore */ }
    if (face === 'codec') {
      closeDrawer();
      if (refs.codecInput) { try { refs.codecInput.focus(); } catch (e) { /* ignore */ } }
    }
    return face;
  }

  function toggleFace() {
    return setFace(currentFace() === 'ai' ? 'codec' : 'ai');
  }

  function onBlankDblClick(e) {
    var t = e.target;
    if (t && t.closest && t.closest(BLANK_GUARD)) return;   // 落在控件上：不切换
    e.preventDefault();
    toggleFace();
  }

  /* ------------------------------- 启动 ------------------------------- */

  function bind() {
    refs.root = $('min');
    refs.codecInput = $('cd-in');
    refs.menu = $('mn-menu');
    refs.status = $('mn-status');
    refs.drawer = $('mn-drawer');
    refs.backdrop = $('mn-backdrop');
    refs.list = $('mn-list');
    refs.newBtn = $('mn-new');
    refs.fullBtn = $('mn-full');
    refs.hint = $('mn-hint');
    refs.working = $('mn-working');
    refs.flags = $('mn-flags');
    refs.stream = $('mn-stream');
    refs.streamInner = $('mn-stream-inner');
    refs.chips = $('mn-chips');
    refs.attach = $('mn-attach');
    refs.file = $('mn-file');
    refs.input = $('mn-input');
    refs.send = $('mn-send');

    if (refs.menu) refs.menu.addEventListener('click', function () { drawerOpen() ? closeDrawer() : openDrawer(); });
    if (refs.backdrop) refs.backdrop.addEventListener('click', closeDrawer);
    if (refs.newBtn) refs.newBtn.addEventListener('click', newConversation);
    if (refs.fullBtn) refs.fullBtn.addEventListener('click', toggleFullscreen);
    if (refs.send) refs.send.addEventListener('click', onSendClick);

    if (refs.attach && refs.file) {
      refs.attach.addEventListener('click', function () { refs.file.click(); });
      refs.file.addEventListener('change', function () {
        uploadFiles(refs.file.files).then(function () { refs.file.value = ''; });
      });
    }

    if (refs.input) {
      refs.input.addEventListener('input', autoGrow);
      refs.input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
          e.preventDefault();
          if (!state.working) send();
        }
      });
    }

    doc.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeDrawer();
    });

    // U11：双击空白处切换「AI 解题 / CTF 工具箱」
    doc.addEventListener('dblclick', onBlankDblClick);

    // 小窗**不提供拖拽上传**（附件仅通过 📎 按钮选择）。
    // 仍拦截浏览器默认的「拖入即打开文件」行为，避免误拖导致窗口跳走。
    doc.addEventListener('dragover', function (e) { e.preventDefault(); });
    doc.addEventListener('drop', function (e) { e.preventDefault(); });
  }

  function boot() {
    bind();

    // U12：接入主界面的时间线渲染层（思考 / 工具 / 子代理 / Todo / 正文）
    if (CTFA.timeline && typeof CTFA.timeline.init === 'function') {
      CTFA.timeline.init({
        timeline: refs.stream,
        inner: refs.streamInner,
        scrollBottom: $('mn-scroll-bottom'),
        todoBar: $('mn-todo'),
        progressEl: $('mn-progress'),
        notices: $('mn-notices'),
        expandThinking: true        // 极简窗：思考过程默认展开
      });
      stripEmpty();
    }

    // U11：伪装面（CTF 编码解码工具箱）初始化
    if (CTFA.codec && typeof CTFA.codec.init === 'function') {
      CTFA.codec.onFullscreen = toggleFullscreen;
      CTFA.codec.init();
    }
    setFace('ai');
    showHint();
    setStatus('就绪', 'idle');
    renderChips();
    renderFlags();
    autoGrow();
    loadConversations().then(function (list) {
      var remembered = rememberedConversation();
      var exists = list.some(function (c) { return c.id === remembered; });
      if (exists) {
        state.conversationId = remembered;
        renderConversations();
        loadHistory(remembered);
        openStream(remembered);
        recoverState(remembered);
      }
      if (refs.input) refs.input.focus();
    });
  }

  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', boot);
  else boot();
})(window);
