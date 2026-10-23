/* ============================================================================
 * theme.js — 主题系统（U1）
 * ----------------------------------------------------------------------------
 * 三套主题：
 *   light  蓝白（默认，遵循系统 prefers-color-scheme）
 *   dark   蓝暗
 *   retro  绿色复古 —— 保留改造前的原有界面风格
 * 约定：
 *   - 颜色唯一来源是 css/style.css 里的设计变量，本文件只切换 <html data-theme>；
 *   - 选择持久化在 localStorage['ctfa.theme']；
 *   - 切换即时生效，无需刷新。
 * 为了避免首屏闪白/闪暗，index.html 的 <head> 里有一小段内联脚本先行设置属性。
 * ========================================================================== */
(function (global) {
  'use strict';

  var CTFA = (global.CTFA = global.CTFA || {});
  var doc = global.document;

  var KEY = 'ctfa.theme';
  var THEMES = ['light', 'dark', 'retro'];
  var LABELS = { light: '明亮', dark: '暗色', retro: '复古' };
  var DEFAULT_THEME = 'light';

  function systemTheme() {
    try {
      if (global.matchMedia && global.matchMedia('(prefers-color-scheme: dark)').matches) {
        return 'dark';
      }
    } catch (err) { /* ignore */ }
    return DEFAULT_THEME;
  }

  function stored() {
    try {
      var value = global.localStorage.getItem(KEY);
      return THEMES.indexOf(value) >= 0 ? value : null;
    } catch (err) {
      return null;
    }
  }

  function current() {
    var attr = doc.documentElement.getAttribute('data-theme');
    return THEMES.indexOf(attr) >= 0 ? attr : systemTheme();
  }

  function apply(theme, persist) {
    var name = THEMES.indexOf(theme) >= 0 ? theme : DEFAULT_THEME;
    doc.documentElement.setAttribute('data-theme', name);
    var meta = doc.querySelector('meta[name="color-scheme"]');
    if (meta) {
      meta.setAttribute('content', name === 'light' ? 'light' : 'dark');
    }
    if (persist) {
      try { global.localStorage.setItem(KEY, name); } catch (err) { /* ignore */ }
    }
    renderSwitcher();
    return name;
  }

  /** 渲染主题切换控件（顶栏右侧，三选一）。 */
  function renderSwitcher() {
    var host = doc.getElementById('theme-switch');
    if (!host) return;
    var active = current();
    if (host.getAttribute('data-active') !== active || !host.childNodes.length) {
      host.setAttribute('data-active', active);
      host.textContent = '';
      THEMES.forEach(function (name) {
        var btn = doc.createElement('button');
        btn.type = 'button';
        btn.textContent = LABELS[name];
        btn.title = '切换到' + LABELS[name] + '主题';
        btn.setAttribute('data-theme-option', name);
        btn.setAttribute('aria-pressed', String(name === active));
        if (name === active) btn.className = 'on';
        btn.addEventListener('click', function () {
          apply(name, true);
          if (CTFA.timeline && CTFA.timeline.notice) {
            CTFA.timeline.notice('主题已切换：' + LABELS[name], 'info');
          }
        });
        host.appendChild(btn);
      });
    }
  }

  function init() {
    apply(current(), false);
    renderSwitcher();
    // 未手动选择过时，跟随系统主题变化
    try {
      if (global.matchMedia) {
        var mq = global.matchMedia('(prefers-color-scheme: dark)');
        var onChange = function () { if (!stored()) apply(systemTheme(), false); };
        if (mq.addEventListener) mq.addEventListener('change', onChange);
        else if (mq.addListener) mq.addListener(onChange);
      }
    } catch (err) { /* ignore */ }
  }

  CTFA.theme = {
    THEMES: THEMES,
    LABELS: LABELS,
    init: init,
    current: current,
    apply: apply,
    set: function (name) { return apply(name, true); },
    /** 三态循环：明亮 → 暗色 → 复古绿 → 明亮 */
    toggle: function () {
      var idx = THEMES.indexOf(current());
      return apply(THEMES[(idx + 1) % THEMES.length], true);
    }
  };
})(window);