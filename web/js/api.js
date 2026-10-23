/* ============================================================================
 * api.js — 后端 REST + SSE 客户端封装（零依赖）
 * ----------------------------------------------------------------------------
 * - 同源基址，全部 JSON 接口走 request()；
 * - streamAgent(url, body, handlers, options) 用 fetch + ReadableStream 手写 SSE
 *   解析（EventSource 不支持 POST，故不使用）；支持 AbortController 中断；
 * - 对外：CTFA.api.*
 * ========================================================================== */
(function (global) {
  'use strict';

  var CTFA = (global.CTFA = global.CTFA || {});

  /** 把查询参数对象编码为 query string（跳过 null/undefined/空串） */
  function qs(params) {
    if (!params) return '';
    var parts = [];
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v === null || v === undefined || v === '') return;
      parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
    });
    return parts.length ? '?' + parts.join('&') : '';
  }

  /** 通用 JSON 请求；非 2xx 抛出带 status 的 Error */
  async function request(path, options) {
    var opts = Object.assign({ headers: {} }, options || {});
    opts.headers = Object.assign({ 'Accept': 'application/json' }, opts.headers);
    if (opts.body !== undefined && opts.body !== null && typeof opts.body !== 'string') {
      opts.headers['Content-Type'] = opts.headers['Content-Type'] || 'application/json';
      opts.body = JSON.stringify(opts.body);
    }

    var res;
    try {
      res = await fetch(path, opts);
    } catch (err) {
      var netErr = new Error('网络请求失败：' + path);
      netErr.cause = err;
      netErr.network = true;
      throw netErr;
    }

    var raw = await res.text();
    var data = null;
    if (raw) {
      try { data = JSON.parse(raw); } catch (e) { data = raw; }
    }

    if (!res.ok) {
      var detail = (data && (data.detail || data.message || data.error)) ||
        (typeof data === 'string' && data) || res.statusText;
      if (typeof detail !== 'string') { try { detail = JSON.stringify(detail); } catch (e2) { detail = String(detail); } }
      var err = new Error('HTTP ' + res.status + '：' + detail);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  /* ----------------------------- REST 接口封装 ----------------------------- */

  var api = {
    request: request,

    health: function () { return request('/api/health'); },
    tools: function () { return request('/api/tools'); },
    agents: function () { return request('/api/agents'); },

    getConfig: function () { return request('/api/config'); },
    // 默认写回本地配置文件（?persist=true）：下次启动直接读回，不再被重置。
    // 传 { persist: false } 可以只做内存热更新（不落盘）。
    putConfig: function (partial, options) {
      var persist = !(options && options.persist === false);
      return request('/api/config' + (persist ? '?persist=true' : ''), {
        method: 'PUT',
        body: partial || {}
      });
    },
    verifyConfig: function (channels) {
      return request('/api/config/verify', { method: 'POST', body: channels ? { channels: channels } : {} });
    },

    conversations: function () { return request('/api/conversations'); },
    createConversation: function (title) {
      return request('/api/conversations', { method: 'POST', body: { title: title || '新会话' } });
    },
    // 破坏性操作：界面上已经弹过二次确认弹窗，这里把确认结果显式带给后端
    // （后端要求 confirm=true，缺少时返回 400，绝不静默删除）
    deleteConversation: function (id) {
      return request('/api/conversations/' + encodeURIComponent(id) + '?confirm=true', { method: 'DELETE' });
    },
    batchDeleteConversations: function (ids) {
      return request('/api/conversations/batch-delete', {
        method: 'POST',
        body: { ids: ids || [], confirm: true }
      });
    },
    // 重命名（U2c）：只改标题，成功返回更新后的 conversation
    renameConversation: function (id, title) {
      return request('/api/conversations/' + encodeURIComponent(id), {
        method: 'PATCH',
        body: { title: title }
      });
    },

    /* ------------------------ U6 多模式审查 ------------------------ */
    /** 查看会话审查模式 → {reviewer, defaultReviewer, whitelist, inherited} */
    conversationHitl: function (id) {
      return request('/api/conversations/' + encodeURIComponent(id) + '/hitl');
    },
    /** 设置会话审查模式：reviewer = human | audit_agent | null（null 跟随全局） */
    setConversationHitl: function (id, reviewer) {
      return request('/api/conversations/' + encodeURIComponent(id) + '/hitl', {
        method: 'PATCH',
        body: { reviewer: reviewer }
      });
    },
    /** 审查裁决历史：filters = {conversationId, reviewer, decision, limit, offset} */
    hitlDecisions: function (filters) {
      var f = filters || {};
      return request('/api/hitl/decisions' + qs({
        conversation_id: f.conversationId,
        reviewer: f.reviewer,
        decision: f.decision,
        limit: f.limit,
        offset: f.offset
      }));
    },
    messages: function (id) { return request('/api/conversations/' + encodeURIComponent(id) + '/messages'); },

    /* ------------------------ 模型选择（DSH ModelSelect） ------------------------ */
    /** 模型目录：{models:[{id,name,model,provider,baseUrl,hasKey,isDefault}], defaultChannel} */
    models: function () { return request('/api/models'); },
    /** 设置会话使用的模型通道（channel=null 跟随全局默认） */
    setConversationModel: function (id, channel) {
      return request('/api/conversations/' + encodeURIComponent(id) + '/model', {
        method: 'PUT',
        body: { channel: channel || null }
      });
    },

    /* ---------------------------- U2a 附件 ---------------------------- */
    attachments: function (conversationId) {
      return request('/api/conversations/' + encodeURIComponent(conversationId) + '/attachments');
    },
    deleteAttachment: function (attachmentId) {
      return request('/api/attachments/' + encodeURIComponent(attachmentId), { method: 'DELETE' });
    },
    attachmentUrl: function (attachmentId) {
      return '/api/attachments/' + encodeURIComponent(attachmentId) + '/download';
    },
    /** 上传附件（XHR：fetch 无法上报上传进度） */
    uploadAttachments: function (conversationId, files, onProgress) {
      return new Promise(function (resolve, reject) {
        var form = new global.FormData();
        Array.prototype.forEach.call(files || [], function (file) { form.append('files', file, file.name); });
        var xhr = new global.XMLHttpRequest();
        xhr.open('POST', '/api/conversations/' + encodeURIComponent(conversationId) + '/attachments');
        xhr.setRequestHeader('Accept', 'application/json');
        if (xhr.upload && typeof onProgress === 'function') {
          xhr.upload.onprogress = function (evt) {
            if (evt.lengthComputable) onProgress(Math.round((evt.loaded / evt.total) * 100));
          };
        }
        xhr.onload = function () {
          var data = null;
          try { data = JSON.parse(xhr.responseText || 'null'); } catch (e) { data = null; }
          if (xhr.status >= 200 && xhr.status < 300) { resolve(data); return; }
          var detail = (data && (data.detail || data.message)) || xhr.statusText || '上传失败';
          var err = new Error('HTTP ' + xhr.status + '：' + (typeof detail === 'string' ? detail : JSON.stringify(detail)));
          err.status = xhr.status;
          err.data = data;
          reject(err);
        };
        xhr.onerror = function () { reject(new Error('上传失败：网络错误')); };
        xhr.onabort = function () { reject(new Error('上传已取消')); };
        xhr.send(form);
        api._lastUploadXhr = xhr;
      });
    },

    facts: function (conversationId, query) {
      return request('/api/facts' + qs({ conversation_id: conversationId, query: query }));
    },
    fact: function (key, conversationId) {
      return request('/api/facts/' + encodeURIComponent(key) + qs({ conversation_id: conversationId }));
    },

    flags: function (conversationId) {
      return request('/api/flags' + qs({ conversation_id: conversationId }));
    },
    flagsByChallenge: function (challengeId) {
      return request('/api/flags' + qs({ challenge_id: challengeId }));
    },

    challenges: function () { return request('/api/challenges'); },

    /** 创建题目（conversation_id 非空时从该会话升级：绑定会话+迁移附件+播种黑板） */
    createChallenge: function (payload) {
      return request('/api/challenges', { method: 'POST', body: payload || {} });
    },

    /** 设置题目归属项目（projectId 为空表示取消归属）；只改 meta.projectId。 */
    setChallengeProject: function (challengeId, projectId) {
      return request('/api/challenges/' + encodeURIComponent(challengeId) + '/project', {
        method: 'POST',
        body: { project_id: projectId || null }
      });
    },

    /**
     * 上传题目附件（multipart）：落盘到题目隔离工作区，并把绝对路径登记进
     * challenges.attachments —— 供 Solver 与 challenge_info 直接访问。
     */
    uploadChallengeAttachments: function (challengeId, files) {
      return new Promise(function (resolve, reject) {
        var form = new global.FormData();
        Array.prototype.forEach.call(files || [], function (file) { form.append('files', file, file.name); });
        var xhr = new global.XMLHttpRequest();
        xhr.open('POST', '/api/challenges/' + encodeURIComponent(challengeId) + '/attachments');
        xhr.setRequestHeader('Accept', 'application/json');
        xhr.onload = function () {
          var data = null;
          try { data = JSON.parse(xhr.responseText || 'null'); } catch (e) { data = null; }
          if (xhr.status >= 200 && xhr.status < 300) { resolve(data); return; }
          var detail = (data && (data.detail || data.message)) || xhr.statusText || '上传失败';
          var err = new Error('HTTP ' + xhr.status + '：' + (typeof detail === 'string' ? detail : JSON.stringify(detail)));
          err.status = xhr.status;
          err.data = data;
          reject(err);
        };
        xhr.onerror = function () { reject(new Error('上传失败：网络错误')); };
        xhr.send(form);
      });
    },

    /** 删除题目（级联清理账本/Solver/flag 与题目工作区；绑定的会话只解绑） */
    deleteChallenge: function (id) {
      return request('/api/challenges/' + encodeURIComponent(id) + qs({ confirm: true }), {
        method: 'DELETE'
      });
    },

    /** 批量删除题目（单批最多 100 个，逐条返回结果） */
    batchDeleteChallenges: function (ids) {
      return request('/api/challenges/batch-delete', {
        method: 'POST',
        body: { ids: ids, confirm: true }
      });
    },

    /** 构造文件下载 URL（path 必须以 workspace/ 开头） */
    fileUrl: function (path) {
      var p = String(path || '').trim();
      if (p.indexOf('workspace/') !== 0) return '';
      return '/api/files' + qs({ path: p });
    },

    /** 在新标签打开/下载产物文件 */
    openFile: function (path) {
      var url = api.fileUrl(path);
      if (!url) { throw new Error('仅允许下载 workspace/ 下的文件：' + path); }
      global.open(url, '_blank', 'noopener');
      return url;
    },

    /** HITL 审批：decision = approve | reject；arguments 非 null 表示「改参后批准」 */
    hitl: function (requestId, payload) {
      return request('/api/hitl/' + encodeURIComponent(requestId), {
        method: 'POST',
        body: {
          decision: payload && payload.decision ? payload.decision : 'approve',
          reason: (payload && payload.reason) || '',
          arguments: payload && payload.arguments !== undefined ? payload.arguments : null
        }
      });
    },

    /* --------------------- 运行中引导（Steering） --------------------- */
    /**
     * 统一消息入口：运行中发送 → 入队（status=queued）；空闲 → 启动任务（status=running）。
     * 前端不判断该发哪个接口，由后端按会话状态分流。
     */
    sendConversationMessage: function (id, body) {
      return request('/api/conversations/' + encodeURIComponent(id) + '/messages', {
        method: 'POST',
        body: body || {}
      });
    },
    /** UI 展示用的安全路径：workspace 相对路径（绝不暴露服务器绝对路径） */
    displayPath: function (path) {
      var p = String(path || '');
      var idx = p.indexOf('workspace/');
      if (idx >= 0) return p.slice(idx);
      var sep = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
      return sep >= 0 ? p.slice(sep + 1) : p;
    },

    /** 会话任务状态：{taskStatus, round, runId, queuedMessages, lastEventId} */
    conversationState: function (id) {
      return request('/api/conversations/' + encodeURIComponent(id) + '/state');
    },
    /** 取消会话任务：mode = 'hard'（立即中止含工具）| 'graceful'（跑完当前轮） */
    cancelConversationTask: function (id, mode) {
      return request('/api/conversations/' + encodeURIComponent(id) + '/cancel', {
        method: 'POST',
        body: { mode: mode || 'hard' }
      });
    },

    cancelRun: function (runId) {
      return request('/api/runs/' + encodeURIComponent(runId) + '/cancel', { method: 'POST' });
    }
  };

  /* ------------------------------- SSE 解析 ------------------------------- */

  function isAbortError(err) {
    return !!err && (err.name === 'AbortError' || err.code === 20 ||
      /aborted|abort/i.test(String(err.message || '')));
  }

  /**
   * 手写 SSE 解析器。
   * 规则：按空行（\n\n 或 \r\n\r\n）切分事件块；块内逐行解析 `field: value`；
   *       `data:` 可多行，以 \n 拼接；以 `:` 开头的行是注释；data 尝试 JSON.parse，
   *       失败则原样作为字符串下发。
   */
  function createSseParser(onEvent) {
    var buffer = '';

    function handleBlock(block) {
      if (!block) return;
      var lines = block.split(/\r?\n/);
      var type = 'message';
      var dataLines = [];
      var id = null;

      for (var i = 0; i < lines.length; i++) {
        var line = lines[i];
        if (line === '' || line.charAt(0) === ':') continue; // 空行/注释
        var colon = line.indexOf(':');
        var field, value;
        if (colon === -1) { field = line; value = ''; }
        else {
          field = line.slice(0, colon);
          value = line.slice(colon + 1);
          if (value.charAt(0) === ' ') value = value.slice(1);
        }
        if (field === 'event') type = value;
        else if (field === 'data') dataLines.push(value);
        else if (field === 'id') id = value;
        // retry 字段按协议忽略
      }

      if (!dataLines.length && type === 'message') return; // 心跳/空块
      var raw = dataLines.join('\n');
      var data = raw;
      if (raw !== '') {
        try { data = JSON.parse(raw); } catch (e) { data = raw; }
      }
      onEvent(type, data, raw, id);
    }

    return {
      /** 追加一段解码后的文本，自动切分出完整事件块 */
      feed: function (chunk) {
        buffer += chunk;
        for (;;) {
          var m = /\r?\n\r?\n/.exec(buffer);
          if (!m) break;
          var block = buffer.slice(0, m.index);
          buffer = buffer.slice(m.index + m[0].length);
          handleBlock(block);
        }
      },
      /** 流结束时冲刷残留缓冲 */
      flush: function () {
        var rest = buffer;
        buffer = '';
        if (rest.replace(/\s/g, '')) handleBlock(rest);
      }
    };
  }

  /**
   * 发起一次流式（SSE）请求。
   * @param {string} url       /api/agent/stream 或 /api/multi-agent/stream
   * @param {object} body      请求体
   * @param {object} handlers  事件回调集合，支持两种写法：
   *                            - handlers.onEvent(type, data, raw)：收到任意事件
   *                            - handlers[type](data, raw)：按事件名分发，
   *                              如 handlers['response_delta']、handlers.cancelled
   *                          另有 handlers.onError(err)、handlers.onClose(info)
   * @param {object} options   { signal, controller }：AbortController 中断支持
   * @returns {Promise<{ok:boolean, aborted?:boolean}>}
   */
  async function streamAgent(url, body, handlers, options) {
    handlers = handlers || {};
    options = options || {};
    var signal = options.signal || (options.controller && options.controller.signal) || null;

    function emit(type, data, raw, id) {
      try {
        if (typeof handlers.onEvent === 'function') handlers.onEvent(type, data, raw, id);
        var fn = handlers[type];
        if (typeof fn === 'function' && fn !== handlers.onEvent) fn(data, raw, id);
      } catch (err) {
        if (typeof handlers.onError === 'function') handlers.onError(err);
        else if (global.console) global.console.error('[sse handler]', type, err);
      }
    }

    var res;
    try {
      res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
        body: JSON.stringify(body || {}),
        signal: signal
      });
    } catch (err) {
      if (isAbortError(err)) {
        emit('cancelled', { reason: 'aborted' });
        if (typeof handlers.onClose === 'function') handlers.onClose({ aborted: true });
        return { ok: false, aborted: true };
      }
      throw err;
    }

    if (!res.ok) {
      var text = '';
      try { text = await res.text(); } catch (e) { /* ignore */ }
      emit('error', { message: 'HTTP ' + res.status + '：' + (text || res.statusText) });
      if (typeof handlers.onClose === 'function') handlers.onClose({ ok: false });
      return { ok: false };
    }

    var parser = createSseParser(emit);

    // 极端环境（无 ReadableStream）退化为整体文本解析
    if (!res.body || typeof res.body.getReader !== 'function') {
      var all = await res.text();
      parser.feed(all);
      parser.flush();
      if (typeof handlers.onClose === 'function') handlers.onClose({ ok: true });
      return { ok: true };
    }

    var reader = res.body.getReader();
    var decoder = new global.TextDecoder('utf-8');

    try {
      for (;;) {
        var chunk = await reader.read();
        if (chunk.done) break;
        parser.feed(decoder.decode(chunk.value, { stream: true }));
      }
      parser.feed(decoder.decode()); // 冲刷解码器尾部
      parser.flush();               // 冲刷残留事件块
    } catch (err) {
      if (isAbortError(err)) {
        try { await reader.cancel(); } catch (e) { /* ignore */ }
        emit('cancelled', { reason: 'aborted' });
        if (typeof handlers.onClose === 'function') handlers.onClose({ aborted: true });
        return { ok: false, aborted: true };
      }
      throw err;
    }

    if (typeof handlers.onClose === 'function') handlers.onClose({ ok: true });
    return { ok: true };
  }

  /**
   * 便捷包装：自行创建 AbortController，返回 { controller, promise }。
   * 调用方用 controller.abort() 即可中断流。
   */
  function createStream(url, body, handlers) {
    var controller = new global.AbortController();
    var promise = streamAgent(url, body, handlers, { controller: controller });
    return { controller: controller, promise: promise };
  }

  /**
   * 会话级事件流（GET + SSE）：运行中引导（Steering）用。
   * - 与 streamAgent 不同，这是长驻流：跨任务持续接收会话事件，不随单次运行结束；
   * - lastEventId 非空时通过 Last-Event-ID 头请求断点补发（后端回放缓冲）；
   * - 事件回调收到 (type, data, raw, id)，id 为服务端单调序号，调用方记录后可断线续传。
   */
  async function streamConversationEvents(conversationId, handlers, options) {
    handlers = handlers || {};
    options = options || {};
    var signal = options.signal || (options.controller && options.controller.signal) || null;
    var lastEventId = options.lastEventId || 0;

    function emit(type, data, raw, id) {
      try {
        if (id) {
          var n = parseInt(id, 10);
          if (!isNaN(n) && n > lastEventId) lastEventId = n;
        }
        if (typeof handlers.onEvent === 'function') handlers.onEvent(type, data, raw, id);
        var fn = handlers[type];
        if (typeof fn === 'function' && fn !== handlers.onEvent) fn(data, raw, id);
      } catch (err) {
        if (typeof handlers.onError === 'function') handlers.onError(err);
        else if (global.console) global.console.error('[sse handler]', type, err);
      }
    }

    var headers = { 'Accept': 'text/event-stream' };
    if (lastEventId > 0) headers['Last-Event-ID'] = String(lastEventId);

    var res;
    try {
      res = await fetch('/api/conversations/' + encodeURIComponent(conversationId) + '/events', {
        method: 'GET',
        headers: headers,
        signal: signal
      });
    } catch (err) {
      if (isAbortError(err)) {
        if (typeof handlers.onClose === 'function') handlers.onClose({ aborted: true });
        return { ok: false, aborted: true };
      }
      throw err;
    }

    if (!res.ok) {
      var text = '';
      try { text = await res.text(); } catch (e) { /* ignore */ }
      emit('error', { message: 'HTTP ' + res.status + '：' + (text || res.statusText) });
      if (typeof handlers.onClose === 'function') handlers.onClose({ ok: false });
      return { ok: false };
    }

    var parser = createSseParser(emit);
    if (!res.body || typeof res.body.getReader !== 'function') {
      var all = await res.text();
      parser.feed(all);
      parser.flush();
      if (typeof handlers.onClose === 'function') handlers.onClose({ ok: true });
      return { ok: true, lastEventId: lastEventId };
    }

    var reader = res.body.getReader();
    var decoder = new global.TextDecoder('utf-8');
    try {
      for (;;) {
        var chunk = await reader.read();
        if (chunk.done) break;
        parser.feed(decoder.decode(chunk.value, { stream: true }));
      }
      parser.feed(decoder.decode());
      parser.flush();
    } catch (err) {
      if (isAbortError(err)) {
        try { await reader.cancel(); } catch (e) { /* ignore */ }
        if (typeof handlers.onClose === 'function') handlers.onClose({ aborted: true });
        return { ok: false, aborted: true, lastEventId: lastEventId };
      }
      throw err;
    }
    if (typeof handlers.onClose === 'function') handlers.onClose({ ok: true });
    return { ok: true, lastEventId: lastEventId };
  }

  api.createSseParser = createSseParser;
  api.streamAgent = streamAgent;
  api.createStream = createStream;
  api.streamConversationEvents = streamConversationEvents;
  api.isAbortError = isAbortError;

  CTFA.api = api;
})(window);