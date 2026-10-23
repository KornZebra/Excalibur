/* ============================================================================
 * timeline.js — 时间线渲染层（事件 → DOM 增量更新）
 * ----------------------------------------------------------------------------
 * 设计要点：
 * 1) 只做「视图」，不做业务状态；业务状态由 app.js 的 state 维护，本模块只接收
 *    事件并增量更新 DOM，绝不整段重绘。
 * 2) 三类流式事件（response_delta / thinking_stream_delta /
 *    sub_agent_reply_stream_delta）先写入缓冲区，再用 requestAnimationFrame
 *    批量刷新（Markdown 目标额外做 ~70ms 节流，避免长文本每帧全量重排）。
 * 3) 所有流式更新后自动滚动到底部；用户手动上滚超过 80px 则暂停自动滚动并显示
 *    「回到底部」按钮。
 * 4) 工具卡片按 toolCallId 增量更新（running → ok/error/awaiting-approval）；
 *    多代理模式下 name === 'task' 的调用会创建子代理分组，其内部的流式文本与
 *    后续工具卡片按 agent 字段归属到该分组。
 * ========================================================================== */
(function (global) {
  'use strict';

  var CTFA = (global.CTFA = global.CTFA || {});

  /* ------------------------------- 常量配置 ------------------------------- */

  var SCROLL_NEAR_BOTTOM = 80;   // 距底部 80px 内视为「贴底」
  var MD_RENDER_INTERVAL = 70;   // Markdown 目标的重绘节流（ms）
  var TRUNCATE_CHARS = 2600;     // 工具输出超过该字符数折叠显示
  var TRUNCATE_LINES = 40;       // 工具输出超过该行数折叠显示

  var TOOL_STATE = {
    'running': { icon: '◐', text: '运行中' },
    'ok': { icon: '✔', text: '完成' },
    'error': { icon: '✖', text: '失败' },
    'awaiting-approval': { icon: '⏳', text: '等待审批' },
    'cancelled': { icon: '⊘', text: '已取消' },
    'done': { icon: '✔', text: '已结束' }
  };

  /* ------------------------------- 内部状态 ------------------------------- */

  var refs = {};        // 外部注入的 DOM 引用
  var view = {
    inner: null,          // 时间线内容容器
    host: null,           // 当前 assistant 卡片的 .msg-blocks（工具/思考等块挂这里）
    bodyEl: null,         // 当前 assistant 卡片的 .msg-body（最终 Markdown）
    thinking: null,       // { wrap, bodyEl, buf, headEl }
    planning: null,       // { wrap, bodyEl, buf }
    response: null,       // { el, buf, kind:'md' }
    agentGroups: {},      // agentName -> group
    pendingTaskGroups: [],// 由 task 工具调用创建、尚未绑定子代理名的分组
    taskAgentAlias: {},   // toolCallId -> agentName
    toolCards: {},        // toolCallId -> card 记录
    dirty: [],            // 待刷新目标队列
    raf: 0,
    lastMdRender: 0,
    userScrolledUp: false,
    hitl: null,           // 当前审批弹窗
    runActive: false
  };

  /* ----------------------- 轮次导航轨（DSH TurnNavigator 移植） ------------
   * 参照 deepseek-harness packages/client/ui-chat TurnNavigator 实现：
   * - slot（零高度定位容器）> frame(nav，统一捕获指针) > scroller（内部滚动，
   *   不链到 transcript）> marks（固定节距梯子）> markPosition > mark(button)
   * - 固定节距 TURN_SPACING=10px：markPosition top = idx*10 + RAIL_INSET
   * - tick 由 .mark::before 绘制（12×2；preview 18px；active 20px）
   * - 指针由 frame 统一处理：pointermove 算 index → preview；click → 跳转；
   *   mark 自身 pointer-events:none，只作键盘/AT 目标
   * - scroller 内部滚动；canScrollUp/Down 动态切 fade class（24px 渐隐带）
   * - active mark 离开 scroller 视口（含渐隐带余量）时平滑居中；
   *   指针在轨内时不跟随（DSH pointerInsideRef 同款语义）
   * ---------------------------------------------------------------------- */
  var TURN_SPACING = 10;   // 相邻 mark 固定节距（DSH TURN_SPACING_PX）
  var RAIL_INSET = 6;      // 梯子上下内边距（DSH RAIL_INSET_PX）
  var RAIL_FADE = 24;      // 渐隐带宽度（DSH FADE_PX）
  var RAIL_MAX_H = 420;    // 轨道最大高度（DSH 420px）

  var rail = {
    host: null,        // slot（定位容器，top 由 JS 设为当前视口中部）
    frame: null,       // nav（指针捕获 + preview 挂载）
    scroller: null,    // 内部滚动容器
    marksBox: null,    // 梯子容器
    preview: null,     // frame 级 preview 面板（单节点复用）
    markers: [],       // mark button 列表
    anchors: [],       // user 卡锚点
    prompts: [],       // 每轮用户消息摘要
    responses: [],     // 每轮首条 assistant 回复摘要
    count: 0, active: -1,
    raf: 0, pointerInside: false, previewIdx: -1
  };

  function ensureRail() {
    if (rail.host || !refs.timeline) return;
    var slot = el('div', 'chat-turn-rail-markers');
    slot.id = 'chat-turn-rail-markers';
    var frame = el('nav', 'chat-turn-rail-frame');
    frame.setAttribute('aria-label', '对话轮次');
    var scroller = el('div', 'chat-turn-rail-scroller');
    var marks = el('div', 'chat-turn-rail-marks');
    scroller.appendChild(marks);
    frame.appendChild(scroller);
    slot.appendChild(frame);
    refs.timeline.appendChild(slot);
    rail.host = slot;
    rail.frame = frame;
    rail.scroller = scroller;
    rail.marksBox = marks;
    // frame 级 preview（单节点复用，贴 DSH frame-level preview）
    rail.preview = el('div', 'chat-turn-rail-preview');
    rail.preview.setAttribute('role', 'tooltip');
    rail.preview.hidden = true;
    frame.appendChild(rail.preview);
    // 指针：frame 统一捕获（mark 自身 pointer-events:none）
    frame.addEventListener('pointermove', function (evt) {
      showPreviewAt(evt.clientY);
    });
    frame.addEventListener('pointerenter', function () {
      rail.pointerInside = true;
    });
    frame.addEventListener('pointerleave', function () {
      rail.pointerInside = false;
      hidePreview();
    });
    frame.addEventListener('click', function (evt) {
      var idx = indexAtPointer(evt.clientY);
      if (idx >= 0) jumpToTurn(idx);
    });
    scroller.addEventListener('scroll', function () { syncFade(); }, { passive: true });
  }

  function resetRail() {
    rail.markers = [];
    rail.anchors = [];
    rail.prompts = [];
    rail.responses = [];
    rail.count = 0;
    rail.active = -1;
    rail.previewIdx = -1;
    rail.pointerInside = false;
    if (rail.raf) { global.cancelAnimationFrame(rail.raf); rail.raf = 0; }
    if (rail.marksBox) rail.marksBox.textContent = '';
    if (rail.preview) rail.preview.hidden = true;
    if (rail.scroller) {
      rail.scroller.className = 'chat-turn-rail-scroller';
      rail.scroller.scrollTop = 0;
    }
  }

  function railNaturalHeight() {
    return Math.max(0, (rail.count - 1) * TURN_SPACING + 2 * RAIL_INSET);
  }

  function turnSummary(content, max) {
    var text = String(content || '').replace(/\s+/g, ' ').trim();
    var limit = max || 50;
    return text.length > limit ? text.slice(0, limit) + '…' : text;
  }

  /** mark 索引 ← 指针 Y（DSH itemAtPointer：含 scroller 滚动补偿与 inset）。 */
  function indexAtPointer(clientY) {
    if (!rail.count || !rail.frame) return -1;
    var rect = rail.frame.getBoundingClientRect();
    var scrollTop = rail.scroller ? rail.scroller.scrollTop : 0;
    var offset = clientY - rect.top + scrollTop - RAIL_INSET;
    return Math.max(0, Math.min(rail.count - 1, Math.round(offset / TURN_SPACING)));
  }

  function jumpToTurn(idx) {
    // 注意：timeline.js 内没有 doc 简写，必须用 global.document
    var target = global.document.getElementById('chat-turn-' + idx);
    var box = refs.timeline;
    if (target && box) {
      var delta = target.getBoundingClientRect().top - box.getBoundingClientRect().top;
      box.scrollTo({ top: box.scrollTop + delta - 8, behavior: 'smooth' });
    }
  }

  function showPreviewFor(idx) {
    if (idx < 0 || idx >= rail.count) return;
    rail.previewIdx = idx;
    rail.markers.forEach(function (btn, i) {
      btn.classList.toggle('is-preview', i === idx);
    });
    var prompt = rail.prompts[idx] || ('第 ' + (idx + 1) + ' 轮');
    var response = rail.responses[idx] || '';
    rail.preview.textContent = '';
    rail.preview.appendChild(el('div', 'chat-turn-rail-preview-prompt', prompt));
    if (response) {
      rail.preview.appendChild(el('div', 'chat-turn-rail-preview-response', response));
    }
    rail.preview.hidden = false;
    // 重触发 enter 动画
    rail.preview.style.animation = 'none';
    void rail.preview.offsetHeight;
    rail.preview.style.animation = '';
    positionPreview();
  }

  function showPreviewAt(clientY) {
    var idx = indexAtPointer(clientY);
    if (idx === rail.previewIdx) {
      positionPreview();
      return;
    }
    showPreviewFor(idx);
  }

  function positionPreview() {
    if (rail.previewIdx < 0 || !rail.preview || !rail.frame || !rail.scroller) return;
    var pos = rail.previewIdx * TURN_SPACING + RAIL_INSET;
    var h = rail.preview.offsetHeight || 100;
    var top = pos - rail.scroller.scrollTop - h / 2;
    top = Math.max(0, Math.min(top, (rail.frame.offsetHeight || h) - h));
    rail.preview.style.top = top + 'px';
  }

  function hidePreview() {
    rail.previewIdx = -1;
    if (rail.preview) rail.preview.hidden = true;
    rail.markers.forEach(function (btn) { btn.classList.remove('is-preview'); });
  }

  function syncFade() {
    if (!rail.scroller) return;
    var s = rail.scroller;
    var up = s.scrollTop > 1;
    var down = s.scrollTop < s.scrollHeight - s.clientHeight - 1;
    s.classList.toggle('is-fade-top', up);
    s.classList.toggle('is-fade-bottom', down);
  }

  /** 给一条 user 消息卡打轮次锚点，并在梯子上追加对应 mark（DSH 结构）。 */
  function markTurn(card, prompt, response) {
    if (!card) return;
    var idx = rail.count++;
    card.id = 'chat-turn-' + idx;
    card.setAttribute('data-turn-index', String(idx));
    rail.anchors.push(card);
    rail.prompts.push(turnSummary(prompt));
    rail.responses.push(turnSummary(response, 120));
    var label = '第 ' + (idx + 1) + ' 轮' + (rail.prompts[idx] ? '：' + rail.prompts[idx] : '');
    // markPosition：固定节距绝对定位
    var pos = el('div', 'chat-turn-rail-markpos');
    pos.style.top = (idx * TURN_SPACING + RAIL_INSET) + 'px';
    var btn = el('button', 'chat-turn-rail-mark');
    btn.type = 'button';
    btn.setAttribute('data-turn-index', String(idx));
    btn.setAttribute('aria-label', label);
    btn.addEventListener('click', function (evt) {
      evt.stopPropagation();  // 键盘/AT 目标；鼠标命中走 frame 统一计算
      jumpToTurn(idx);
    });
    btn.addEventListener('focus', function () { showPreviewFor(idx); });
    btn.addEventListener('blur', hidePreview);
    pos.appendChild(btn);
    if (rail.marksBox) rail.marksBox.appendChild(pos);
    rail.markers.push(btn);
    // 梯子自然高度 + frame 显式高度（DSH: height=min(natural, band-64, 420)）
    // 注意：frame 必须设明确 height——其内容（scroller）是 absolute 撑不开父元素，
    // 只设 max-height 会导致 frame 高 0、scroller 把全部 tick 裁掉（不可见）。
    var natural = railNaturalHeight();
    if (rail.marksBox) rail.marksBox.style.height = natural + 'px';
    applyRailHeight();
    if (rail.host) rail.host.hidden = rail.count < 2;  // 单轮对话不显示轨道
    positionRail();
    syncFade();
  }

  /** frame 显式高度（DSH height:min(natural, band-64, 420) 的 JS 版）。 */
  function applyRailHeight() {
    if (!rail.frame) return;
    var natural = railNaturalHeight();
    var band = refs.timeline ? refs.timeline.clientHeight : 600;
    var h = Math.max(2, Math.min(natural, band - 64, RAIL_MAX_H));  // 与 DSH min() 对齐，仅防 0
    rail.frame.style.height = h + 'px';
    rail.frame.style.maxHeight = h + 'px';
  }

  /** 把轨道定位到「当前视口中部」（内容坐标 = scrollTop + 视口中心）。 */
  function positionRail() {
    if (!rail.host || !rail.frame || !refs.timeline) return;
    var box = refs.timeline;
    applyRailHeight();
    // frame 以 translateY(-50%) 自居中：slot.top 即 frame 中心，
    // 直接取视口中心（内容坐标）；h/2+12 兜底防 frame 顶溢出容器
    var h = rail.frame.offsetHeight || 0;
    var top = box.scrollTop + Math.max(box.clientHeight / 2, h / 2 + 12);
    rail.host.style.top = top + 'px';
  }

  /** DSH：active mark 离开 scroller 视口（含渐隐带余量）时平滑居中；
   *  指针正在轨内操作时不跟随（避免在手下移动）。 */
  function keepActiveVisible() {
    if (!rail.scroller || rail.pointerInside || rail.active < 0) return;
    var markTop = rail.active * TURN_SPACING + RAIL_INSET;
    var viewTop = rail.scroller.scrollTop;
    var viewH = rail.scroller.clientHeight;
    if (viewH <= 0) return;
    if (markTop >= viewTop + RAIL_FADE && markTop <= viewTop + viewH - RAIL_FADE) return;
    var target = Math.max(0, markTop - viewH / 2);
    rail.scroller.scrollTo({ top: target, behavior: 'smooth' });
  }

  /** 滚动高亮：视口顶部所属的最后一轮为当前轮（rAF 节流）。 */
  function updateActiveTurn() {
    if (rail.raf) return;
    rail.raf = global.requestAnimationFrame(function () {
      rail.raf = 0;
      if (!refs.timeline || !rail.anchors.length) return;
      positionRail();
      var top = refs.timeline.getBoundingClientRect().top + 56;
      var active = 0;
      for (var i = 0; i < rail.anchors.length; i++) {
        if (rail.anchors[i].getBoundingClientRect().top <= top) active = i;
        else break;
      }
      if (active !== rail.active) {
        rail.active = active;
        rail.markers.forEach(function (btn, i) {
          var on = i === active;
          btn.classList.toggle('is-active', on);
          if (on) btn.setAttribute('aria-current', 'step');
          else btn.removeAttribute('aria-current');
        });
        keepActiveVisible();
      }
      positionPreview();
    });
  }

  /* ------------------------------- DOM 工具 ------------------------------- */

  function el(tag, className, text) {
    var node = global.document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function mdRender(src) {
    return CTFA.markdown ? CTFA.markdown.render(src) : String(src || '');
  }

  function nowTime(createdAtMs) {
    var d = createdAtMs ? new Date(createdAtMs) : new Date();
    function p(n) { return n < 10 ? '0' + n : String(n); }
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  function scrollEl() { return refs.timeline; }

  function isNearBottom() {
    var box = scrollEl();
    if (!box) return true;
    return box.scrollHeight - box.scrollTop - box.clientHeight <= SCROLL_NEAR_BOTTOM;
  }

  function autoScroll(force) {
    var box = scrollEl();
    if (!box) return;
    if (force) view.userScrolledUp = false;
    if (view.userScrolledUp) { updateScrollButton(); return; }
    box.scrollTop = box.scrollHeight;
    updateScrollButton();
  }

  function updateScrollButton() {
    if (!refs.scrollBottom) return;
    if (view.userScrolledUp) refs.scrollBottom.removeAttribute('hidden');
    else refs.scrollBottom.setAttribute('hidden', '');
  }

  function onScroll() {
    view.userScrolledUp = !isNearBottom();
    updateScrollButton();
    updateActiveTurn();
  }

  /* --------------------------- 流式增量刷新调度 --------------------------- */

  /** 把一段增量写入目标缓冲，并安排下一帧刷新 */
  function queueDelta(target, text) {
    if (!target || text === undefined || text === null || text === '') return;
    target.buf += String(text);
    if (view.dirty.indexOf(target) === -1) view.dirty.push(target);
    schedule();
  }

  function schedule() {
    if (view.raf) return;
    view.raf = global.requestAnimationFrame(flushDirty);
  }

  function applyTarget(t) {
    if (!t || !t.el) return;
    if (t.kind === 'md') t.el.innerHTML = mdRender(t.buf);
    else t.el.textContent = t.buf;
  }

  /** rAF 批量刷新：Markdown 目标做时间节流，未到时间的留到下一帧 */
  function flushDirty() {
    view.raf = 0;
    var due = Date.now() - view.lastMdRender >= MD_RENDER_INTERVAL;
    var list = view.dirty;
    var rest = [];
    for (var i = 0; i < list.length; i++) {
      var t = list[i];
      if (t.kind === 'md' && !due) { rest.push(t); continue; }
      applyTarget(t);
    }
    if (due) view.lastMdRender = Date.now();
    view.dirty = rest;
    if (rest.length) schedule();
    autoScroll(false);
  }

  /** 立即冲刷（用于收尾，保证最后一段文本完整上屏） */
  function flushNow() {
    if (view.raf) { global.cancelAnimationFrame(view.raf); view.raf = 0; }
    var list = view.dirty;
    view.dirty = [];
    for (var i = 0; i < list.length; i++) applyTarget(list[i]);
    view.lastMdRender = Date.now();
  }

  /* ------------------------------ 卡片骨架 ------------------------------ */

  /** 引导（Steering）投递状态角标：queued=排队中 / delivered=已送达 / off=未投递 */
  function steerBadgeEl(status) {
    var b = el('span', 'steer-badge');
    b.setAttribute('data-steer', status || 'queued');
    b.textContent = status === 'delivered' ? '已送达' : (status === 'off' ? '未投递' : '排队中');
    return b;
  }

  function findUserCard(messageId) {
    if (!messageId || !view.inner) return null;
    return view.inner.querySelector('article.msg-user[data-message-id="' + String(messageId).replace(/"/g, '\\"') + '"]');
  }

  function setUserSteerStatus(messageId, status) {
    var card = findUserCard(messageId);
    if (!card) return false;
    var old = card.querySelector('.steer-badge');
    if (old) old.remove();
    card.querySelector('.msg-head').appendChild(steerBadgeEl(status));
    return true;
  }

  function makeUserCard(text, messageId, opts, createdAtMs) {
    var card = el('article', 'msg msg-user');
    if (messageId) card.setAttribute('data-message-id', messageId);
    var head = el('header', 'msg-head');
    var userAvatar = el('img', 'avatar avatar-user');
    userAvatar.src = 'avatar-user.jpg';
    userAvatar.alt = '你';
    head.appendChild(userAvatar);
    head.appendChild(el('span', 'msg-role', '你'));
    head.appendChild(el('span', 'msg-time', nowTime(createdAtMs)));
    // 运行中引导：任务运行期间发送的消息带"排队中"角标，投递后翻转
    if (opts && opts.steerStatus) {
      head.appendChild(steerBadgeEl(opts.steerStatus));
    }
    card.appendChild(head);
    var body = el('div', 'msg-body md');
    body.textContent = String(text || '');
    card.appendChild(body);
    var attachments = el('div', 'msg-attachments');
    attachments.hidden = true;
    card.appendChild(attachments);
    return card;
  }

  function cardForAttachment(row) {
    if (CTFA.attachments && CTFA.attachments.renderCardInto) {
      // 复用 attachments.js 的卡片样式（返回其内部节点）
      var probe = el('span', 'attach-card');
      var fake = { id: row.id, filename: row.filename, size: row.size, kind: row.kind,
        mimeWarning: row.mimeWarning };
      if (CTFA.api && CTFA.api.attachmentUrl) probe.href = CTFA.api.attachmentUrl(row.id);
      probe.setAttribute('download', row.filename || '');
      probe.appendChild(el('span', 'attach-icon', CTFA.attachments.kindIcon(fake.kind)));
      var meta = el('span', 'attach-meta');
      meta.appendChild(el('span', 'attach-name', row.filename || '附件'));
      meta.appendChild(el('span', 'attach-sub',
        (row.kind || 'unknown') + ' · ' + CTFA.attachments.humanSize(row.size)));
      probe.appendChild(meta);
      return probe;
    }
    return el('span', 'attach-card', row.filename || '附件');
  }

  function makeAgentCard(createdAtMs) {
    var card = el('article', 'msg msg-agent');
    var head = el('header', 'msg-head');
    head.appendChild(makeAgentAvatar());
    head.appendChild(el('span', 'msg-role', '湖中剑'));
    var state = el('span', 'msg-state', '思考中');
    state.setAttribute('data-role', 'state');
    head.appendChild(state);
    head.appendChild(el('span', 'msg-time', nowTime(createdAtMs)));
    card.appendChild(head);

    var blocks = el('div', 'msg-blocks');
    var body = el('div', 'msg-body md');
    card.appendChild(blocks);
    card.appendChild(body);
    return { card: card, blocks: blocks, body: body, stateEl: state };
  }

  /** agent 头像：品牌 logo 图片（与页面 logo 同源） */
  function makeAgentAvatar() {
    var img = el('img', 'avatar avatar-agent');
    img.src = 'logo.png';
    img.alt = '湖中剑';
    return img;
  }

  function ensureHost() {
    if (view.host && view.host.isConnected) return;
    var made = makeAgentCard();
    view.inner.appendChild(made.card);
    view.host = made.blocks;
    view.bodyEl = made.body;
    view.stateEl = made.stateEl;
  }

  function setAgentState(text, tone) {
    if (!view.stateEl) return;
    view.stateEl.textContent = text || '';
    view.stateEl.setAttribute('data-tone', tone || '');
  }

  /* ------------------------------ 思考 / 规划 ------------------------------ */

  function ensureThinking() {
    ensureHost();
    if (view.thinking && view.thinking.wrap.isConnected) return view.thinking;
    var wrap = el('section', 'block block-thinking');
    var head = el('button', 'block-head');
    head.setAttribute('type', 'button');
    // 极简窗（refs.expandThinking）默认展开，便于直接观察思考过程；主界面仍默认折叠
    var expanded = !!refs.expandThinking;
    head.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    head.appendChild(el('span', 'block-icon', '💭'));
    var title = el('span', 'block-title', '思考中');
    head.appendChild(title);
    head.appendChild(el('span', 'dots', ''));
    head.appendChild(el('span', 'block-caret', expanded ? '▾' : '▸'));
    var body = el('div', 'block-body');
    var pre = el('div', 'thinking-text');
    body.appendChild(pre);
    wrap.appendChild(head);
    wrap.appendChild(body);
    if (expanded) wrap.classList.add('open');

    head.addEventListener('click', function () {
      var open = wrap.classList.toggle('open');
      head.setAttribute('aria-expanded', open ? 'true' : 'false');
      head.querySelector('.block-caret').textContent = open ? '▾' : '▸';
    });

    view.host.appendChild(wrap);
    // 注意：el 必须指向真正的渲染容器，否则 queueDelta/applyTarget 会因缺 el 而丢弃思考文本。
    view.thinking = { wrap: wrap, headEl: head, titleEl: title, bodyEl: pre, el: pre, buf: '', kind: 'text' };
    return view.thinking;
  }

  function appendThinking(delta) {
    var t = ensureThinking();
    queueDelta(t, delta);
    setAgentState('思考中', 'thinking');
  }

  function endThinking(doneText) {
    var t = view.thinking;
    if (!t) return;
    flushNow();
    t.wrap.classList.add('done');
    t.titleEl.textContent = doneText || ('思考过程（' + t.buf.length + ' 字）');
    t.wrap.querySelector('.dots').textContent = '';
    view.thinking = null;
  }

  function appendPlanning(text) {
    ensureHost();
    if (!view.planning || !view.planning.wrap.isConnected) {
      var wrap = el('section', 'block block-planning');
      var head = el('button', 'block-head');
      head.setAttribute('type', 'button');
      head.appendChild(el('span', 'block-icon', '🗺'));
      head.appendChild(el('span', 'block-title', '规划'));
      head.appendChild(el('span', 'block-caret', '▾'));
      var body = el('div', 'block-body');
      var inner = el('div', 'planning-text md');
      body.appendChild(inner);
      wrap.appendChild(head);
      wrap.appendChild(body);
      wrap.classList.add('open');
      head.addEventListener('click', function () {
        var open = wrap.classList.toggle('open');
        head.querySelector('.block-caret').textContent = open ? '▾' : '▸';
      });
      view.host.appendChild(wrap);
      view.planning = { wrap: wrap, bodyEl: inner, buf: '', kind: 'md' };
    }
    // planning 事件为整块内容，直接覆盖
    view.planning.buf = String(text || '');
    if (view.planning.buf) {
      view.planning.el = view.planning.bodyEl;
      if (view.dirty.indexOf(view.planning) === -1) view.dirty.push(view.planning);
      schedule();
    }
  }

  /* -------------------------------- 正文流 -------------------------------- */

  function beginResponse() {
    ensureHost();
    if (view.thinking) endThinking();
    if (!view.response || !view.response.el.isConnected) {
      var box = el('div', 'md stream-response');
      view.host.appendChild(box);
      view.response = { el: box, buf: '', kind: 'md' };
    }
    setAgentState('生成回复', 'running');
    return view.response;
  }

  function appendResponse(delta) {
    var target = view.response && view.response.el.isConnected ? view.response : beginResponse();
    queueDelta(target, delta);
  }

  /** 最终完整回复（response 事件）：以权威内容覆盖流式缓冲 */
  function finalizeResponse(content) {
    flushNow();
    content = sanitizePathText(content);
    if (typeof content === 'string' && content) {
      // 流式内容与最终内容一致时无需重绘；不一致则以最终内容为准
      if (!view.response || view.response.buf !== content) {
        if (!view.response || !view.response.el.isConnected) {
          ensureHost();
          var box = el('div', 'md stream-response');
          view.host.appendChild(box);
          view.response = { el: box, buf: '', kind: 'md' };
        }
        view.response.buf = content;
        applyTarget(view.response);
      }
      if (view.bodyEl) view.bodyEl.innerHTML = '';
    }
    setAgentState('已完成', 'ok');
    flushNow();
    autoScroll(false);
  }

  /* ------------------------------ 子代理分组 ------------------------------ */

  function newAgentGroup(name) {
    var wrap = el('section', 'subagent');
    wrap.setAttribute('data-agent', name);

    var head = el('header', 'subagent-head');
    head.appendChild(el('span', 'subagent-icon', '◈'));
    var nameEl = el('span', 'subagent-name', name);
    head.appendChild(nameEl);
    var state = el('span', 'subagent-state', '运行中');
    head.appendChild(state);
    head.appendChild(el('span', 'block-caret', '▾'));
    wrap.appendChild(head);

    var body = el('div', 'subagent-body');
    var stream = el('div', 'md subagent-stream');
    var tools = el('div', 'subagent-tools');
    body.appendChild(stream);
    body.appendChild(tools);
    wrap.appendChild(body);
    wrap.classList.add('open');

    head.addEventListener('click', function () {
      var open = wrap.classList.toggle('open');
      head.querySelector('.block-caret').textContent = open ? '▾' : '▸';
    });

    ensureHost();
    view.host.appendChild(wrap);

    var group = {
      el: wrap,
      name: name,
      nameEl: nameEl,
      stateEl: state,
      toolsEl: tools,
      stream: { el: stream, buf: '', kind: 'md' }
    };
    view.agentGroups[name] = group;
    return group;
  }

  function ensureAgentGroup(name) {
    if (!name) name = 'sub-agent';
    if (view.agentGroups[name]) return view.agentGroups[name];
    // 若存在尚未绑定名字的 task 分组，则把收到的第一个子代理名绑定给它
    if (view.pendingTaskGroups.length) {
      var pending = view.pendingTaskGroups.shift();
      pending.name = name;
      pending.el.setAttribute('data-agent', name);
      pending.nameEl.textContent = name;
      view.agentGroups[name] = pending;
      return pending;
    }
    return newAgentGroup(name);
  }

  /** 由 task 工具调用创建分组（名字优先从 arguments 推断） */
  function createTaskGroup(toolCall) {
    var args = parseMaybeJson(toolCall.arguments) || {};
    var guess = '';
    if (args && typeof args === 'object') {
      guess = args.agent || args.agent_name || args.agentName ||
        args.subagent_type || args.subagent || args.name || args.type || '';
    }
    var group;
    if (guess) {
      // 已按名字推断出来：直接按名取用/新建，避免误绑定其他待定分组
      group = view.agentGroups[guess] || newAgentGroup(String(guess));
    } else {
      group = newAgentGroup('子代理');
      view.pendingTaskGroups.push(group);
    }

    if (toolCall.toolCallId) view.taskAgentAlias[toolCall.toolCallId] = group.name;
    group.stateEl.textContent = '运行中';
    group.stateEl.setAttribute('data-tone', 'running');
    return group;
  }

  function appendSubAgentDelta(agentId, agentName, delta) {
    var name = agentName || agentId || '子代理';
    var group = ensureAgentGroup(name);
    queueDelta(group.stream, delta);
    group.stateEl.textContent = '流式输出中';
    group.stateEl.setAttribute('data-tone', 'running');
    setAgentState('子代理运行中', 'running');
    autoScroll(false);
  }

  function settleAgentGroups() {
    Object.keys(view.agentGroups).forEach(function (k) {
      var g = view.agentGroups[k];
      flushNow();
      if (g.stateEl) {
        g.stateEl.textContent = '完成';
        g.stateEl.setAttribute('data-tone', 'ok');
      }
    });
  }

  /* ------------------------------- 工具卡片 ------------------------------- */

  function parseMaybeJson(value) {
    if (typeof value === 'string') {
      var s = value.trim();
      if (!s) return value;
      if (s.charAt(0) === '{' || s.charAt(0) === '[') {
        try { return JSON.parse(s); } catch (e) { return value; }
      }
      return value;
    }
    return value;
  }

  function prettyJson(value) {
    if (value === null || value === undefined) return '';
    if (typeof value === 'string') return value;
    try { return JSON.stringify(value, null, 2); } catch (e) { return String(value); }
  }

  /** 工具结果可能是字符串、对象或 {output|content|text|...} 包装，统一取文本 */
  function resultToText(result) {
    if (result === null || result === undefined) return '';
    if (typeof result === 'string') return result;
    if (typeof result === 'object') {
      var keys = ['output', 'content', 'text', 'result', 'stdout', 'stderr', 'message', 'error'];
      for (var i = 0; i < keys.length; i++) {
        var v = result[keys[i]];
        if (typeof v === 'string' && v) return v;
      }
      return prettyJson(result);
    }
    return String(result);
  }

  function shouldTruncate(text) {
    if (!text) return false;
    if (text.length > TRUNCATE_CHARS) return true;
    return text.split('\n').length > TRUNCATE_LINES;
  }

  function truncate(text) {
    var out = text;
    if (out.length > TRUNCATE_CHARS) out = out.slice(0, TRUNCATE_CHARS);
    var lines = out.split('\n');
    if (lines.length > TRUNCATE_LINES) out = lines.slice(0, TRUNCATE_LINES).join('\n');
    return out.replace(/\s+$/, '') + '\n\n…（输出已截断）';
  }

  /**
   * 渲染一段等宽文本；超长时先截断，提供「查看完整输出」按钮，
   * 若带 persistedFile 则额外提供「下载完整输出」（走 GET /api/files）。
   */
  /** 显示层路径归一化：把「服务器前缀 + /workspace/」归一为 workspace/…
   *  仅作用于渲染文本；数据库与模型看到的原始内容不变。
   *  流式增量不做（分块可能截断路径），最终态以 finalizeResponse 的权威内容覆盖。 */
  function sanitizePathText(text) {
    var t = String(text || '');
    if (t.indexOf('/workspace/') < 0 && t.indexOf('/home/') < 0) return t;
    return t.replace(/(?:\/[A-Za-z0-9_.\-]+)+\/workspace\//g, 'workspace/');
  }

  function renderPre(container, text, persistedFile) {
    text = sanitizePathText(text);
    container.textContent = '';
    var full = String(text === null || text === undefined ? '' : text);
    if (!full) {
      container.appendChild(el('span', 'muted', '（无输出）'));
      return;
    }
    var pre = el('pre', 'tool-pre');
    var truncated = shouldTruncate(full);
    var showingFull = false;

    function paint() {
      pre.textContent = truncated && !showingFull ? truncate(full) : full;
      if (btn) btn.textContent = showingFull ? '收起' : '查看完整输出';
    }

    var btn = null;
    if (truncated) {
      btn = el('button', 'link-btn');
      btn.setAttribute('type', 'button');
      btn.addEventListener('click', function () {
        showingFull = !showingFull;
        paint();
      });
    }

    paint();
    container.appendChild(pre);

    var actions = null;
    if (truncated || persistedFile) {
      actions = el('div', 'tool-actions');
      if (btn) actions.appendChild(btn);
      if (persistedFile) {
        var dl = el('button', 'link-btn');
        dl.setAttribute('type', 'button');
        dl.textContent = '下载/打开完整输出 ↗';
        dl.addEventListener('click', function () {
          try { CTFA.api.openFile(persistedFile); }
          catch (e) { notice(e.message, 'error'); }
        });
        actions.appendChild(dl);
        var pathEl = el('code', 'file-path', CTFA.api.displayPath(persistedFile));
        actions.appendChild(pathEl);
      }
      container.appendChild(actions);
    }
  }

  function setToolState(card, status) {
    var meta = TOOL_STATE[status] || TOOL_STATE.running;
    card.el.setAttribute('data-state', status);
    card.stateEl.textContent = meta.text;
    card.iconEl.textContent = meta.icon;
  }

  function copyText(text, btn) {
    function done() {
      var old = btn.textContent;
      btn.textContent = '已复制 ✓';
      global.setTimeout(function () { btn.textContent = old; }, 1200);
    }
    if (global.navigator && global.navigator.clipboard && global.navigator.clipboard.writeText) {
      global.navigator.clipboard.writeText(text).then(done, function () { fallback(); });
    } else { fallback(); }

    function fallback() {
      try {
        var ta = el('textarea', 'copy-tmp');
        ta.value = text;
        global.document.body.appendChild(ta);
        ta.select();
        global.document.execCommand('copy');
        global.document.body.removeChild(ta);
        done();
      } catch (e) { notice('复制失败', 'error'); }
    }
  }

  /** 创建工具卡片（参数未知时先占位，tool_result 到达后再回填） */
  function createToolCard(call, hostEl) {
    var card = el('section', 'tool-card');
    card.setAttribute('data-tool', call.name || 'unknown');

    var head = el('header', 'tool-head');
    var icon = el('span', 'tool-icon', '◐');
    var nameEl = el('span', 'tool-name', call.name || 'tool');
    var agentEl = el('span', 'tool-agent');
    if (call.agent) agentEl.textContent = '◈ ' + call.agent;
    var stateEl = el('span', 'tool-state', '运行中');
    var caret = el('span', 'block-caret', '▸');
    var previewEl = el('span', 'tool-preview');
    head.appendChild(icon);
    head.appendChild(nameEl);
    head.appendChild(agentEl);
    head.appendChild(previewEl);
    head.appendChild(stateEl);
    head.appendChild(caret);

    var body = el('div', 'tool-body');
    var argsSection = el('div', 'tool-section');
    argsSection.appendChild(el('div', 'tool-label', '参数'));
    var argsBox = el('div', 'tool-args');
    argsSection.appendChild(argsBox);
    body.appendChild(argsSection);

    var resSection = el('div', 'tool-section');
    resSection.appendChild(el('div', 'tool-label', '结果'));
    var resBox = el('div', 'tool-result');
    resBox.appendChild(el('span', 'muted', '等待执行…'));
    resSection.appendChild(resBox);
    body.appendChild(resSection);

    var metaRow = el('div', 'tool-meta');
    body.appendChild(metaRow);

    card.appendChild(head);
    card.appendChild(body);

    head.addEventListener('click', function () {
      var open = card.classList.toggle('open');
      caret.textContent = open ? '▾' : '▸';
    });

    var record = {
      el: card, nameEl: nameEl, agentEl: agentEl, stateEl: stateEl, iconEl: icon,
      argsBox: argsBox, resBox: resBox, metaRow: metaRow, previewEl: previewEl,
      name: call.name, agent: call.agent || '', args: call.arguments,
      status: 'running', fullResult: '', persistedFile: '', executionId: ''
    };

    renderPre(argsBox, prettyJson(parseMaybeJson(call.arguments)), '');
    setToolState(record, 'running');

    if (hostEl) {
      hostEl.appendChild(card);
    } else if (call.agent && view.agentGroups[call.agent]) {
      view.agentGroups[call.agent].toolsEl.appendChild(card);
    } else {
      ensureHost();
      view.host.appendChild(card);
    }
    return record;
  }

  function upsertToolCall(call) {
    if (!call) return null;
    var id = call.toolCallId || call.id || (call.name + ':' + Math.random().toString(36).slice(2));

    // 多代理：task 调用创建子代理分组，卡片本身嵌入分组内部
    if (call.name === 'task') {
      var group = createTaskGroup(call);
      var taskCard = createToolCard(Object.assign({}, call, { agent: group.name }));
      taskCard.el.classList.add('tool-card-task');
      view.toolCards[id] = taskCard;
      autoScroll(false);
      return taskCard;
    }

    var card = view.toolCards[id];
    if (!card) {
      card = createToolCard(call);
      view.toolCards[id] = card;
    } else {
      // 重复的 tool_call：刷新参数
      card.args = call.arguments;
      renderPre(card.argsBox, prettyJson(parseMaybeJson(call.arguments)), '');
      if (call.agent && !card.agent) {
        card.agent = call.agent;
        card.agentEl.textContent = '◈ ' + call.agent;
      }
    }
    autoScroll(false);
    return card;
  }

  function setToolResult(res) {
    if (!res) return;
    var id = res.toolCallId || res.id;
    var card = view.toolCards[id];
    if (!card) {
      // 没收到 tool_call 直接来了结果：补建卡片
      card = upsertToolCall({ toolCallId: id, name: res.name, arguments: null, agent: res.agent });
      if (!card) return;
    }
    var text = resultToText(res.result);
    card.fullResult = text;
    card.persistedFile = res.persistedFile || '';
    card.executionId = res.executionId || '';
    renderPre(card.resBox, text, card.persistedFile);
    setToolState(card, res.isError ? 'error' : 'ok');
    // 折叠态的单行预览：只露结果首行，点开看全文
    if (card.previewEl) {
      var safe = sanitizePathText(text);
      card.previewEl.textContent = safe.split('\n')[0].slice(0, 90);
      card.previewEl.title = safe.length > 90 ? safe.slice(0, 400) : '';
    }

    card.metaRow.textContent = '';
    if (card.executionId) {
      card.metaRow.appendChild(el('span', 'meta-label', 'executionId：'));
      card.metaRow.appendChild(el('code', 'meta-code', card.executionId));
      var copyBtn = el('button', 'link-btn', '复制');
      copyBtn.setAttribute('type', 'button');
      copyBtn.addEventListener('click', function () { copyText(card.executionId, copyBtn); });
      card.metaRow.appendChild(copyBtn);
    }
    if (card.persistedFile) {
      card.metaRow.appendChild(el('span', 'meta-label', '已落盘：'));
      card.metaRow.appendChild(el('code', 'meta-code', CTFA.api.displayPath(card.persistedFile)));
    }

    autoScroll(false);
    return card;
  }

  /** 标记某工具卡片进入「等待审批」状态 */
  function markToolApproval(toolCallId) {
    var card = view.toolCards[toolCallId];
    if (!card) return;
    setToolState(card, 'awaiting-approval');
    card.el.classList.add('open');
  }

  /* --------------------------------- Todo --------------------------------- */

  function renderTodos(todos) {
    if (!refs.todoBar) return;
    var list = Array.isArray(todos) ? todos : [];
    if (!list.length) {
      refs.todoBar.setAttribute('hidden', '');
      refs.todoBar.textContent = '';
      return;
    }
    refs.todoBar.removeAttribute('hidden');
    refs.todoBar.textContent = '';

    var done = list.filter(function (t) { return t.status === 'completed'; }).length;
    var pct = Math.round((done / list.length) * 100);

    var head = el('div', 'todo-head');
    head.appendChild(el('span', 'todo-title', '任务进度'));
    head.appendChild(el('span', 'todo-count', done + ' / ' + list.length));
    var toggle = el('button', 'link-btn todo-toggle', '折叠');
    head.appendChild(toggle);
    refs.todoBar.appendChild(head);

    var track = el('div', 'todo-track');
    var fill = el('div', 'todo-fill');
    fill.style.width = pct + '%';
    track.appendChild(fill);
    refs.todoBar.appendChild(track);

    var ul = el('ul', 'todo-items');
    list.forEach(function (t) {
      var li = el('li', 'todo-item');
      li.setAttribute('data-status', t.status || 'pending');
      li.appendChild(el('span', 'todo-check', t.status === 'completed' ? '✔' : t.status === 'in_progress' ? '◐' : '○'));
      li.appendChild(el('span', 'todo-content', t.content || t.id || ''));
      ul.appendChild(li);
    });
    refs.todoBar.appendChild(ul);

    toggle.addEventListener('click', function () {
      var collapsed = refs.todoBar.classList.toggle('collapsed');
      toggle.textContent = collapsed ? '展开' : '折叠';
    });
  }

  /* ------------------------------ 进度 / 用量 ------------------------------ */

  function setProgress(stage, message, iteration) {
    if (!refs.progressEl) return;
    var bits = [];
    if (iteration) bits.push('第 ' + iteration + ' 轮');
    if (stage) bits.push(stage);
    if (message) bits.push(message);
    refs.progressEl.textContent = bits.join(' · ');
    refs.progressEl.removeAttribute('hidden');
  }

  function clearProgress() {
    if (!refs.progressEl) return;
    refs.progressEl.textContent = '';
    refs.progressEl.setAttribute('hidden', '');
  }

  function renderUsage(usage) {
    if (!refs.usageBadge || !usage) return;
    var calls = usage.modelCalls || 0;
    var pin = usage.promptTokens || 0;
    var pout = usage.completionTokens || 0;
    refs.usageBadge.textContent = '⚡ 模型调用 ' + calls + ' · 输入 ' + pin + ' · 输出 ' + pout +
      ' · 合计 ' + (pin + pout);
  }

  /* ------------------------------- 历史消息 ------------------------------- */

  function renderHistory(messages) {
    clear();
    var list = Array.isArray(messages) ? messages : [];
    // 工具结果索引：role=tool 消息携带 tool_call_id，与 assistant 的 toolCalls[id] 一一对应。
    // 没有这一步，历史里的工具卡拿不到结果回执 → 永远显示"运行中"。
    var toolResults = {};
    list.forEach(function (msg) {
      if (msg && msg.role === 'tool' && msg.toolCallId) {
        var content = String(msg.content || '');
        toolResults[msg.toolCallId] = {
          result: content,
          isError: content.indexOf('[TOOL_ERROR]') === 0
        };
      }
    });
    // 轮次预览数据（DSH preview 的 prompt/response 双段）：
    // 每条 user 消息开一轮，response 取该轮首条 assistant 回复
    var turnPreviews = [];
    list.forEach(function (msg) {
      if (!msg) return;
      var role = msg.role || 'assistant';
      if (role === 'user') {
        turnPreviews.push({ prompt: msg.content || '', response: '' });
      } else if ((role === 'assistant' || role === 'agent') && turnPreviews.length
                 && !turnPreviews[turnPreviews.length - 1].response) {
        turnPreviews[turnPreviews.length - 1].response = msg.content || '';
      }
    });
    list.forEach(function (msg) {
      if (!msg) return;
      var role = msg.role || 'assistant';
      if (role === 'user') {
        var userCard = makeUserCard(msg.content || '', msg.id, {
          // deliveryStatus=queued：已落库但从未投递（取消/中断后遗留）→ "未投递"；
          // 若任务仍在运行，recoverSteerState 会把活跃队列里的消息翻回"排队中"
          steerStatus: msg.deliveryStatus === 'queued' ? 'off' : null
        }, msg.createdAt);
        var pv = turnPreviews.shift() || {};
        markTurn(userCard, pv.prompt, pv.response);
        view.inner.appendChild(userCard);
        if (Array.isArray(msg.attachments) && msg.attachments.length && CTFA.attachments) {
          CTFA.attachments.renderCardInto(msg.id, msg.attachments);
        }
        return;
      }
      if (role === 'assistant' || role === 'agent') {
        var isFinal = String(msg.content || '').indexOf('## 解题结论') === 0;
        var made = makeAgentCard(msg.createdAt);
        if (isFinal) {
          made.card.classList.add('msg-final');
          var badge = el('span', 'msg-final-badge', '最终结论');
          made.card.insertBefore(badge, made.card.firstChild);
        }
        var text = sanitizePathText(msg.content || '');
        if (text) made.body.innerHTML = mdRender(text);
        var calls = Array.isArray(msg.toolCalls) ? msg.toolCalls : [];
        calls.forEach(function (tc) {
          var histId = tc.id || tc.toolCallId || ('hist-' + Math.random().toString(36).slice(2));
          var histCard = createToolCard({
            toolCallId: histId,
            name: tc.name || tc.tool || 'tool',
            arguments: tc.arguments !== undefined ? tc.arguments : tc.args,
            agent: tc.agent
          }, made.blocks);
          // 历史记录里的工具卡片也注册进索引，方便后续事件继续更新
          view.toolCards[histId] = histCard;
          // 回填结果回执（role=tool 消息），否则卡片永远停在"运行中"
          var receipt = toolResults[histId];
          if (receipt) {
            setToolResult({
              toolCallId: histId,
              name: tc.name || tc.tool || 'tool',
              result: receipt.result,
              isError: receipt.isError
            });
          }
          var res = tc.result !== undefined ? tc.result : tc.output;
          if (res !== undefined && res !== null) {
            setToolResult({
              toolCallId: histId,
              name: tc.name || tc.tool,
              result: res,
              isError: !!(tc.isError || tc.error),
              executionId: tc.executionId,
              persistedFile: tc.persistedFile
            });
          }
          return;
        });
        made.stateEl.textContent = '历史';
        view.inner.appendChild(made.card);
        return;
      }
      if (role === 'tool') {
        // 工具结果已按 tool_call_id 回填进 assistant 的工具卡，不再整块平铺；
        // 无法匹配的孤儿输出折叠为单行（默认收起）。
        if (msg.toolCallId && toolResults[msg.toolCallId]) return;
        var out = String(sanitizePathText(msg.content || ''));
        var isErr = out.indexOf('[TOOL_ERROR]') === 0;
        var wrap = el('details', 'msg msg-tool-orphan' + (isErr ? ' err' : ''));
        wrap.appendChild(el('summary', null,
          (isErr ? '⚠ 工具输出（失败） · ' : '工具输出 · ')
          + out.split('\n')[0].slice(0, 70) + ' · 点击展开'));
        var body = el('div', 'msg-body md');
        body.innerHTML = mdRender(out);
        wrap.appendChild(body);
        view.inner.appendChild(wrap);
        return;
      }
      // 其他角色（system 等）以中性卡片呈现
      var other = el('article', 'msg msg-other');
      var head = el('header', 'msg-head');
      head.appendChild(el('span', 'msg-role', role));
      head.appendChild(el('span', 'msg-time', msg.createdAt || ''));
      other.appendChild(head);
      var body = el('div', 'msg-body md');
      body.innerHTML = mdRender(sanitizePathText(msg.content || ''));
      other.appendChild(body);
      view.inner.appendChild(other);
    });
    if (!list.length) showEmpty();
    sweepRunningCards();
    autoScroll(true);
  }

  /** 收尾清扫：把仍处于"运行中"的工具卡翻成"已结束"。
   *  覆盖两类场景：结果回执事件丢失（SSE 断缝/旧任务遗留）、运行已结束但回执从未到达。 */
  function sweepRunningCards() {
    Object.keys(view.toolCards).forEach(function (id) {
      var card = view.toolCards[id];
      if (card && card.el && card.el.getAttribute('data-state') === 'running') {
        setToolState(card, 'done');
      }
    });
  }

  function showEmpty() {
    if (!view.inner) return;
    if (view.inner.querySelector('.empty-state')) return;
    var box = el('div', 'empty-state');
    var logo = el('img', 'empty-logo');
    logo.src = 'logo.png';
    logo.alt = '湖中剑 logo';
    box.appendChild(logo);
    box.appendChild(el('div', 'empty-title', '您好，Excalibur已就绪！'));
    box.appendChild(el('p', ''));
    box.appendChild(el('p', 'empty-desc', '无数长夜之下，骑士挥剑奔赴黎明，自幽沉湖心递来的锋芒，曾许诺予执剑者破晓的权柄。'));
    view.inner.appendChild(box);
  }

  /* ------------------------------- 提示 / 错误 ------------------------------- */

  function notice(text, kind) {
    if (!refs.notices) return;
    var item = el('div', 'notice notice-' + (kind || 'info'));
    item.appendChild(el('span', 'notice-text', text));
    var close = el('button', 'notice-close', '×');
    close.setAttribute('type', 'button');
    close.addEventListener('click', function () { item.remove(); });
    item.appendChild(close);
    refs.notices.appendChild(item);
    global.setTimeout(function () { if (item.isConnected) item.remove(); }, kind === 'error' ? 12000 : 6000);
  }

  function showError(message) {
    ensureHost();
    var box = el('div', 'error-box');
    box.appendChild(el('span', 'error-icon', '✖'));
    box.appendChild(el('span', 'error-text', String(message || '未知错误')));
    view.host.appendChild(box);
    setAgentState('出错', 'error');
    notice(String(message || '未知错误'), 'error');
    autoScroll(false);
  }

  /* ------------------------------ HITL 审批弹窗 ------------------------------ */

  /**
   * U6c：自动裁决卡片（hitl_auto_decision 事件）。
   * audit_agent=虚线描边卡片；policy/whitelist=灰字徽标行；review_edit 附改参 diff。
   */
  function appendAutoDecision(d) {
    d = d || {};
    ensureHost();
    var decidedBy = d.decidedBy || 'policy';
    var approve = d.decision === 'approve';

    var card = el('section', 'hitl-auto-card');
    card.setAttribute('data-decided-by', decidedBy);
    card.setAttribute('data-decision', d.decision || '');

    var row = el('div', 'hitl-auto-row');
    var icon = decidedBy === 'audit_agent' ? '🤖' : (decidedBy === 'policy' ? '⚡' : '✓');
    var label = decidedBy === 'audit_agent' ? 'Agent 审计'
      : (decidedBy === 'policy' ? '完全访问' : '白名单免审');
    row.appendChild(el('span', 'hitl-auto-icon', icon));
    row.appendChild(el('span', 'hitl-auto-label', label));
    var verdict = el('span', 'hitl-auto-verdict', approve ? '放行' : '拒绝');
    verdict.setAttribute('data-tone', approve ? 'ok' : 'error');
    row.appendChild(verdict);
    if (d.comment) row.appendChild(el('span', 'hitl-auto-comment', String(d.comment)));
    if (d.elapsedMs) row.appendChild(el('span', 'hitl-auto-elapsed', (d.elapsedMs / 1000).toFixed(1) + 's'));
    if (d.cached) row.appendChild(el('span', 'hitl-auto-cached', '缓存复用'));
    card.appendChild(row);

    // review_edit 改参 diff（原参数 → 审计调整后的完整参数）
    if (d.edited && (d.originalArguments || d.editedArguments)) {
      var diff = el('details', 'hitl-auto-diff');
      diff.appendChild(el('summary', 'hitl-auto-diff-summary', '参数已被审计调整（点击查看 diff）'));
      var diffBody = el('div', 'hitl-auto-diff-body');
      diffBody.appendChild(el('div', 'hitl-auto-diff-col', prettyJsonText(d.originalArguments)));
      diffBody.appendChild(el('span', 'hitl-auto-diff-arrow', '→'));
      diffBody.appendChild(el('div', 'hitl-auto-diff-col hitl-auto-diff-new', prettyJsonText(d.editedArguments)));
      diff.appendChild(diffBody);
      card.appendChild(diff);
    }

    view.host.appendChild(card);
    autoScroll(true);
  }

  function prettyJsonText(value) {
    if (value === undefined || value === null || value === '') return '—';
    try { return JSON.stringify(value, null, 2); } catch (e) { return String(value); }
  }

  function closeHitl(mark) {
    var h = view.hitl;
    if (!h) return;
    if (h.timer) global.clearInterval(h.timer);
    if (mark && h.statusEl) {
      h.statusEl.textContent = mark;
      h.statusEl.setAttribute('data-tone', mark.indexOf('超时') >= 0 ? 'error' : 'ok');
    }
    var wrap = h.overlay;
    view.hitl = null;
    global.setTimeout(function () { if (wrap && wrap.isConnected) wrap.remove(); }, mark ? 700 : 0);
  }

  /**
   * 弹出人机协同审批框。
   * @param {object} req  { requestId, toolCallId, name, arguments, timeoutAt, agent }
   * @param {function} onDecision  (requestId, payload) => Promise，payload 形如
   *        { decision:'approve'|'reject', reason, arguments }；arguments 非 null 表示改参批准
   */
  function showHitl(req, onDecision) {
    if (!req) return;
    if (view.hitl) closeHitl('被新的审批请求替换');

    var overlay = el('div', 'modal-overlay');
    var modal = el('div', 'modal');
    overlay.appendChild(modal);

    var head = el('header', 'modal-head');
    head.appendChild(el('span', 'modal-icon', '⏳'));
    head.appendChild(el('span', 'modal-title', '工具调用需要人工审批'));
    var countdown = el('span', 'modal-countdown', '');
    head.appendChild(countdown);
    modal.appendChild(head);

    var body = el('div', 'modal-body');
    var meta = el('div', 'modal-meta');
    meta.appendChild(el('span', 'meta-label', '工具：'));
    meta.appendChild(el('code', 'meta-code', req.name || '未知工具'));
    if (req.agent) {
      meta.appendChild(el('span', 'meta-label', '代理：'));
      meta.appendChild(el('code', 'meta-code', req.agent));
    }
    meta.appendChild(el('span', 'meta-label', 'requestId：'));
    meta.appendChild(el('code', 'meta-code', req.requestId || ''));
    body.appendChild(meta);

    var original = parseMaybeJson(req.arguments);
    if (original === undefined || original === null) original = {};
    var originalText = typeof original === 'string' ? original : prettyJson(original);

    body.appendChild(el('div', 'tool-label', '参数（可编辑 JSON，用于「改参后批准」）'));
    var ta = el('textarea', 'modal-editor');
    ta.value = originalText;
    ta.spellcheck = false;
    body.appendChild(ta);

    var errEl = el('div', 'modal-error');
    errEl.setAttribute('hidden', '');
    body.appendChild(errEl);

    var reasonWrap = el('div', 'modal-reason');
    reasonWrap.setAttribute('hidden', '');
    reasonWrap.appendChild(el('div', 'tool-label', '拒绝理由（必填）'));
    var reasonInput = el('input', 'modal-input');
    reasonInput.setAttribute('type', 'text');
    reasonInput.placeholder = '例如：该命令会修改靶场外文件，请改用只读方式';
    reasonWrap.appendChild(reasonInput);
    body.appendChild(reasonWrap);

    var statusEl = el('div', 'modal-status');
    body.appendChild(statusEl);
    modal.appendChild(body);

    var foot = el('footer', 'modal-foot');
    var approveBtn = el('button', 'btn primary', '批准');
    var rejectBtn = el('button', 'btn danger', '拒绝');
    var editApproveBtn = el('button', 'btn warn', '改参批准');
    foot.appendChild(approveBtn);
    foot.appendChild(rejectBtn);
    foot.appendChild(editApproveBtn);
    modal.appendChild(foot);

    overlay.addEventListener('click', function (ev) {
      if (ev.target === overlay) { tryClose('已忽略（点击遮罩）'); }
    });

    var settled = false;

    function fail(msg) {
      errEl.textContent = msg;
      errEl.removeAttribute('hidden');
    }

    async function settle(payload, label) {
      if (settled) return;
      settled = true;
      setBusy(true);
      try {
        if (typeof onDecision === 'function') await onDecision(req.requestId, payload);
        closeHitl(label);
      } catch (err) {
        settled = false;
        setBusy(false);
        fail('提交失败：' + (err && err.message ? err.message : err));
      }
    }

    function setBusy(busy) {
      [approveBtn, rejectBtn, editApproveBtn].forEach(function (b) { b.disabled = !!busy; });
      countdown.textContent = busy ? '提交中…' : countdown.textContent;
    }

    approveBtn.addEventListener('click', function () {
      errEl.setAttribute('hidden', '');
      settle({ decision: 'approve', reason: '', arguments: null }, '已批准');
    });

    rejectBtn.addEventListener('click', function () {
      errEl.setAttribute('hidden', '');
      var reason = reasonInput.value.trim();
      if (!reason) {
        reasonWrap.removeAttribute('hidden');
        reasonInput.focus();
        fail('拒绝必须填写理由');
        return;
      }
      settle({ decision: 'reject', reason: reason, arguments: null }, '已拒绝');
    });

    editApproveBtn.addEventListener('click', function () {
      errEl.setAttribute('hidden', '');
      var text = ta.value.trim();
      var parsed;
      try {
        parsed = text ? JSON.parse(text) : null;
      } catch (e) {
        fail('JSON 解析失败：' + e.message);
        return;
      }
      var changed = text !== originalText.trim();
      if (!changed || parsed === null) {
        // 内容未变化：按普通批准处理
        settle({ decision: 'approve', reason: '', arguments: null }, '已批准（参数未变）');
        return;
      }
      settle({ decision: 'approve', reason: '', arguments: parsed }, '已改参批准');
    });

    // 拒绝按钮首次点击时露出理由输入框
    rejectBtn.addEventListener('focus', function () { reasonWrap.removeAttribute('hidden'); });

    global.document.body.appendChild(overlay);
    global.requestAnimationFrame(function () { overlay.classList.add('show'); ta.focus(); });

    var deadline = Number(req.timeoutAt) || 0;

    function tick() {
      if (settled) return;
      if (!deadline) { countdown.textContent = '无超时限制'; return; }
      var left = deadline - Date.now();
      if (left <= 0) {
        countdown.textContent = '已超时（自动拒绝）';
        closeHitl('已超时（自动拒绝）');
        if (typeof onDecision === 'function') {
          // 后端通常会在超时后自行拒绝；此处仅做兜底提示，不再重复提交
          notice('审批请求已超时（自动拒绝）：' + (req.name || ''), 'error');
        }
        return;
      }
      countdown.textContent = '剩余 ' + Math.ceil(left / 1000) + 's';
    }

    var timer = global.setInterval(tick, 250);
    tick();

    view.hitl = {
      overlay: overlay,
      timer: timer,
      statusEl: statusEl,
      requestId: req.requestId,
      toolCallId: req.toolCallId
    };
  }

  function tryClose(mark) {
    if (view.hitl) closeHitl(mark);
  }

  /* ------------------------------- 生命周期 ------------------------------- */

  /**
   * 重置视图索引（DOM 节点引用、流式缓冲、卡片索引）。
   * clear() 与 init() 共用：保证重复初始化时不会把内容写进上一次的容器。
   */
  function resetView() {
    if (view.raf) { global.cancelAnimationFrame(view.raf); view.raf = 0; }
    view.host = null;
    view.bodyEl = null;
    view.stateEl = null;
    view.thinking = null;
    view.planning = null;
    view.response = null;
    view.agentGroups = {};
    view.pendingTaskGroups = [];
    view.taskAgentAlias = {};
    view.toolCards = {};
    view.dirty = [];
  }

  function clear() {
    flushNow();
    if (view.inner) view.inner.textContent = '';
    resetView();
    resetRail();
    clearProgress();
    renderTodos([]);
    autoScroll(true);
  }

  function beginRun() {
    view.runActive = true;
    view.userScrolledUp = false;
    var empty = view.inner && view.inner.querySelector('.empty-state');
    if (empty) empty.remove();
    ensureHost();
    setAgentState('思考中', 'thinking');
    autoScroll(true);
  }

  function finishRun(stateText, tone) {
    flushNow();
    if (view.thinking) endThinking();
    settleAgentGroups();
    setAgentState(stateText || '已完成', tone || 'ok');
    clearProgress();
    view.runActive = false;
    flushNow();
    autoScroll(false);
  }

  function init(domRefs) {
    refs = domRefs || {};
    view.inner = refs.inner || null;
    resetView(); // 清空上一次的节点索引与流式缓冲
    if (refs.timeline) {
      refs.timeline.addEventListener('scroll', onScroll, { passive: true });
      ensureRail();
    }
    if (global.addEventListener) {
      global.addEventListener('resize', positionRail, { passive: true });
    }
    if (refs.scrollBottom) {
      refs.scrollBottom.addEventListener('click', function () { autoScroll(true); });
    }
    showEmpty();
  }

  CTFA.timeline = {
    init: init,
    clear: clear,
    beginRun: beginRun,
    sweepRunningCards: sweepRunningCards,
    breakStream: function () {
      /* 「中断并继续」：掐断当前流式气泡与思考块，下一轮从新气泡开始 */
      try { flushNow(); } catch (err) { /* ignore */ }
      if (view.thinking) endThinking();
      view.response = null;
    },
    finishRun: finishRun,
    appendUserMessage: function (text, opts) {
      ensureEmptyRemoved();
      var options = opts || {};
      var card = makeUserCard(text, options.messageId, { steerStatus: options.steerStatus });
      markTurn(card, text, '');
      view.inner.appendChild(card);
      // U2a：把已上传的附件画在气泡里（无附件时不做任何事）
      if (CTFA.attachments && options.attachments && options.attachments.length) {
        if (options.messageId) {
          CTFA.attachments.state.byMessage[options.messageId] = options.attachments;
          CTFA.attachments.renderCardInto(options.messageId, options.attachments);
        } else {
          var host = card.querySelector('.msg-attachments');
          if (host) {
            host.hidden = false;
            host.textContent = '';
            options.attachments.forEach(function (row) { host.appendChild(cardForAttachment(row)); });
          }
        }
      }
      autoScroll(true);
      return card;
    },
    /** 引导消息角标翻转：message.delivered 到达后把"排队中"翻成"已送达" */
    markSteerStatus: function (messageId, status) {
      return setUserSteerStatus(messageId, status);
    },
    /** 是否已存在指定 id 的用户气泡（多标签页/事件重放去重用） */
    hasUserCard: function (messageId) {
      return !!findUserCard(messageId);
    },
    /** 给"本地先渲染、后拿到服务端 id"的用户气泡补绑 data-message-id */
    bindUserMessageId: function (card, messageId) {
      if (card && card.classList && card.classList.contains('msg-user') && messageId) {
        card.setAttribute('data-message-id', messageId);
        return true;
      }
      return false;
    },
    /** 把所有处于 from 状态的引导角标批量翻为 to（任务结束时排队→未投递） */
    setAllSteerStatus: function (from, to) {
      if (!view.inner) return;
      var badges = view.inner.querySelectorAll('.steer-badge[data-steer="' + from + '"]');
      Array.prototype.forEach.call(badges, function (badge) {
        var card = badge.closest ? badge.closest('article.msg-user') : null;
        if (!card) return;
        var id = card.getAttribute('data-message-id');
        if (id) setUserSteerStatus(id, to);
        else {
          badge.setAttribute('data-steer', to);
          badge.textContent = to === 'delivered' ? '已送达' : '未投递';
        }
      });
    },
    beginAssistant: ensureHost,
    appendThinking: appendThinking,
    endThinking: endThinking,
    appendPlanning: appendPlanning,
    beginResponse: beginResponse,
    appendResponse: appendResponse,
    finalizeResponse: finalizeResponse,
    appendSubAgentDelta: appendSubAgentDelta,
    upsertToolCall: upsertToolCall,
    setToolResult: setToolResult,
    markToolApproval: markToolApproval,
    renderTodos: renderTodos,
    setProgress: setProgress,
    clearProgress: clearProgress,
    renderUsage: renderUsage,
    renderHistory: renderHistory,
    showError: showError,
    notice: notice,
    showHitl: showHitl,
    appendAutoDecision: appendAutoDecision,
    closeHitl: closeHitl,
    scrollToBottom: function () { autoScroll(true); },
    flush: flushNow,
    getToolCard: function (id) { return view.toolCards[id]; },
    turnRail: {
      refresh: updateActiveTurn,
      count: function () { return rail.count; }
    }
  };

  function ensureEmptyRemoved() {
    var empty = view.inner && view.inner.querySelector('.empty-state');
    if (empty) empty.remove();
  }
})(window);
