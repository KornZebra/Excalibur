/**
 * 模型选择（composer 发送键旁的模型下拉）——移植 DSH（deepseek-harness）ModelSelect。
 *
 * 形态对应 DSH ui-model-selection/ModelSelect.tsx：
 * - trigger 是 28px 高的次级色胶囊（图标 + 模型名 + chevron），位于 composer 底行
 *   发送键左侧；窄容器时文字让位给图标（本项目 composer 行恒宽，省略号截断即可）；
 * - 下拉卡片 portal 到 body（composer 的 overflow 不会裁掉它），固定定位、
 *   右缘与 trigger 对齐、向上展开，视口内钳制；
 * - 选项按 provider 分组（sticky 组标题），每项显示 模型名 + 模型 ID，
 *   当前项右侧打勾；底部「管理模型…」跳配置页 AI 分区。
 *
 * 数据：GET /api/models（ai.channels 的目录投影）；选择按会话持久化
 * （PUT /api/conversations/{id}/model → meta.modelChannel），新会话继承上次
 * 选择（localStorage 兜底）。发送时 app.js 经 buildConversationMessageBody
 * 带上 model 字段；后端据此构建只含被选通道的专用客户端（不跨模型 failover）。
 */
(function (global) {
  'use strict';
  var doc = global.document;

  var LS_KEY = 'ctfa.model';
  var state = { models: [], defaultChannel: '', current: '', open: false, convId: '' };
  var refs = { btn: null, label: null };
  var menu = null;

  function $(id) { return doc.getElementById(id); }

  function find(id) {
    for (var i = 0; i < state.models.length; i++) {
      if (state.models[i].id === id) return state.models[i];
    }
    return null;
  }

  /** 当前生效的模型：会话选择 > 全局默认 > 第一个。 */
  function currentModel() {
    return find(state.current) || find(state.defaultChannel) || state.models[0] || null;
  }

  function currentKey() {
    var m = currentModel();
    return m ? m.id : '';
  }

  function renderLabel() {
    if (!refs.label) return;
    var m = currentModel();
    refs.label.textContent = m ? (m.name || m.id) : '选择模型';
    if (refs.btn) {
      refs.btn.title = m
        ? ('当前模型：' + (m.name || m.id) + '（' + m.model + ' · ' + m.provider + '）'
           + (state.current && state.current !== state.defaultChannel ? '' : ' · 全局默认'))
        : '选择模型';
    }
  }

  /** 持久化到当前会话（无会话时只落 localStorage）。 */
  function persist() {
    try { global.localStorage.setItem(LS_KEY, state.current); } catch (e) { /* 隐私模式 */ }
    if (!state.convId) return;
    if (CTFA.api && typeof CTFA.api.setConversationModel === 'function') {
      CTFA.api.setConversationModel(state.convId, state.current || null).catch(function () { /* 网络噪声 */ });
    }
  }

  function select(key) {
    state.current = key || '';
    renderLabel();
    persist();
    closeMenu();
    if (refs.btn) refs.btn.focus();
  }

  function closeMenu() {
    state.open = false;
    if (refs.btn) refs.btn.setAttribute('aria-expanded', 'false');
    if (menu && menu.parentNode) menu.parentNode.removeChild(menu);
    menu = null;
  }

  function placeMenu() {
    if (!menu || !refs.btn) return;
    var r = refs.btn.getBoundingClientRect();
    var mw = menu.offsetWidth || 240;
    var mh = menu.offsetHeight || 320;
    var MARGIN = 12;
    // DSH 同款：向上展开、右缘对齐 trigger，视口内钳制
    var x = r.right - mw;
    var y = r.top - 8 - mh;
    x = Math.min(Math.max(x, MARGIN), Math.max(MARGIN, global.innerWidth - mw - MARGIN));
    y = Math.min(Math.max(y, MARGIN), Math.max(MARGIN, global.innerHeight - mh - MARGIN));
    menu.style.left = x + 'px';
    menu.style.top = y + 'px';
  }

  function openMenu() {
    closeMenu();
    state.open = true;
    if (refs.btn) refs.btn.setAttribute('aria-expanded', 'true');

    menu = doc.createElement('div');
    menu.className = 'model-menu';
    menu.setAttribute('role', 'listbox');
    menu.setAttribute('aria-label', '选择模型');

    // 按 provider 分组（保持声明顺序）
    var groups = {};
    var order = [];
    state.models.forEach(function (m) {
      var g = m.provider || '自定义';
      if (!groups[g]) { groups[g] = []; order.push(g); }
      groups[g].push(m);
    });

    if (!state.models.length) {
      var empty = doc.createElement('div');
      empty.className = 'model-menu-empty';
      empty.textContent = '尚未配置任何模型——点下方「管理模型…」添加';
      menu.appendChild(empty);
    }

    var key = currentKey();
    order.forEach(function (g) {
      var title = doc.createElement('div');
      title.className = 'model-menu-group';
      title.textContent = g;
      menu.appendChild(title);
      groups[g].forEach(function (m) {
        var on = m.id === key;
        var opt = doc.createElement('button');
        opt.type = 'button';
        opt.className = 'model-menu-opt' + (on ? ' on' : '');
        opt.setAttribute('role', 'option');
        opt.setAttribute('aria-selected', on ? 'true' : 'false');
        opt.title = m.name + '（' + m.model + '）';
        var box = doc.createElement('span');
        box.className = 'model-menu-opt-box';
        var name = doc.createElement('span');
        name.className = 'model-menu-opt-name';
        name.textContent = m.name || m.id;
        var sub = doc.createElement('span');
        sub.className = 'model-menu-opt-sub';
        sub.textContent = m.model + (m.id === state.defaultChannel ? ' · 默认' : '');
        box.appendChild(name);
        box.appendChild(sub);
        var check = doc.createElement('span');
        check.className = 'model-menu-check';
        check.textContent = on ? '✓' : '';
        opt.appendChild(box);
        opt.appendChild(check);
        opt.addEventListener('click', function () { select(m.id); });
        menu.appendChild(opt);
      });
    });

    // 底部入口：管理模型（跳配置页 AI 分区并高亮）
    var foot = doc.createElement('button');
    foot.type = 'button';
    foot.className = 'model-menu-foot';
    foot.textContent = '管理模型…';
    foot.addEventListener('click', function () {
      closeMenu();
      global.location.hash = '#/config';
      setTimeout(function () {
        var sec = $('cfg-sec-ai');
        if (sec && sec.scrollIntoView) {
          sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
          sec.classList.add('flash');
          setTimeout(function () { sec.classList.remove('flash'); }, 1800);
        }
      }, 150);
    });
    menu.appendChild(foot);

    // 键盘导航（DSH 同款：上下移动、Esc 关闭）
    menu.addEventListener('keydown', function (e) {
      var opts = Array.prototype.slice.call(menu.querySelectorAll('.model-menu-opt'));
      if (!opts.length) return;
      var idx = opts.indexOf(doc.activeElement);
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        var next = (idx + (e.key === 'ArrowDown' ? 1 : -1) + opts.length) % opts.length;
        opts[next].focus();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        closeMenu();
        if (refs.btn) refs.btn.focus();
      }
    });

    doc.body.appendChild(menu);
    placeMenu();
    var first = menu.querySelector('.model-menu-opt.on') || menu.querySelector('.model-menu-opt');
    if (first) first.focus();
  }

  function toggleMenu() {
    if (state.open) { closeMenu(); return; }
    refresh().then(openMenu);
  }

  /** 拉取模型目录并刷新标签；始终返回 Promise（调用方可靠地接 openMenu）。 */
  var refreshTries = 0;
  function refresh() {
    if (!CTFA.api || typeof CTFA.api.models !== 'function') return Promise.resolve();
    return CTFA.api.models().then(function (d) {
      refreshTries = 0;
      state.models = (d && d.models) || [];
      state.defaultChannel = (d && d.defaultChannel) || '';
      if (state.current && !find(state.current)) state.current = '';
      renderLabel();
      if (state.open && menu) openMenu();  // 打开态下目录变化 → 重绘保持打开
    }).catch(function () {
      // 自愈：首次拉取失败（服务重启窗口 / 脚本顺序）时退避重试两次，
      // 不把 chip 标签困在兜底文案上（重试仍失败则保持现状，由下次交互刷新）
      if (refreshTries >= 2) return null;
      refreshTries += 1;
      return new Promise(function (resolve) { setTimeout(resolve, 1200); }).then(refresh);
    });
  }

  /** 打开会话时恢复其绑定的模型；未绑定时继承上次选择并补绑。 */
  function setConversation(conv) {
    conv = conv || {};
    state.convId = conv.id || '';
    var bound = (conv.meta && conv.meta.modelChannel) || '';
    if (bound) {
      state.current = bound;
    } else {
      var last = '';
      try { last = global.localStorage.getItem(LS_KEY) || ''; } catch (e) { /* 隐私模式 */ }
      state.current = last && find(last) ? last : '';
      if (state.current && state.convId) persist();  // 继承即补绑，保证耐久
    }
    renderLabel();
  }

  function init() {
    refs.btn = $('btn-model');
    refs.label = $('model-select-label');
    if (!refs.btn) return;
    try { state.current = global.localStorage.getItem(LS_KEY) || ''; } catch (e) { /* 隐私模式 */ }
    refs.btn.addEventListener('click', function (e) {
      e.stopPropagation();
      toggleMenu();
    });
    doc.addEventListener('mousedown', function (e) {
      if (!state.open) return;
      if (refs.btn.contains(e.target)) return;
      if (menu && menu.contains(e.target)) return;
      closeMenu();
    });
    doc.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && state.open) {
        closeMenu();
        if (refs.btn) refs.btn.focus();
      }
    });
    global.addEventListener('resize', function () { if (state.open) placeMenu(); });
    refresh();
  }

  global.CTFA = global.CTFA || {};
  global.CTFA.models = {
    init: init,
    refresh: refresh,
    setConversation: setConversation,
    select: select,
    current: function () { return state.current; },
    currentModel: currentModel
  };
})(window);
