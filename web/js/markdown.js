/* ============================================================================
 * markdown.js — 极简 Markdown 渲染器（零依赖 / 无外部库 / 自行 HTML 转义防 XSS）
 * ----------------------------------------------------------------------------
 * 支持：标题、粗体、斜体、删除线、行内代码、围栏代码块、无序/有序列表（含一层
 *       嵌套）、引用块、表格、水平线、链接（target=_blank rel=noopener）、换行。
 * 安全：先转义再渲染；行内代码以占位符保护；链接协议白名单，禁止 javascript: /
 *       data: / vbscript:；任何原始 HTML 一律被转义，不可能注入。
 * 对外：CTFA.markdown.render(src) / renderInline(src) / escapeHtml(str)
 * ========================================================================== */
(function (global) {
  'use strict';

  var CTFA = (global.CTFA = global.CTFA || {});

  /* ------------------------------ 1. HTML 转义 ------------------------------ */

  var ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  /** 转义 HTML 特殊字符（渲染一切用户/模型可控文本的第一道防线） */
  function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"']/g, function (ch) { return ESC_MAP[ch]; });
  }

  /**
   * 链接地址安全校验（输入为「已转义」文本）。
   * 允许 http/https/mailto/tel/ftp、锚点与相对路径；其余协议一律拒绝。
   */
  function safeUrl(escapedUrl) {
    var u = String(escapedUrl || '').trim();
    if (!u) return '';
    if (/^[a-zA-Z][a-zA-Z0-9+.\-]*:/.test(u)) {
      return /^(https?|mailto|tel|ftp):/i.test(u) ? u : '';
    }
    if (u.indexOf('//') === 0) return ''; // 协议相对地址，拒绝
    return u;                             // 相对路径 / #锚点
  }

  /* ------------------------------ 2. 行内解析 ------------------------------ */

  /**
   * 行内渲染。流程：抽出行内代码 → 整体转义 → 在转义后的文本上套用格式 → 还原代码。
   * 因为所有格式标记都是在转义后的文本上匹配的，所以不可能拼出可执行的 HTML。
   */
  function renderInline(text) {
    var src = text === null || text === undefined ? '' : String(text);
    var codes = [];

    // (1) 行内代码占位（\u0000 序号 \u0000，转义不会影响这两个字符）
    src = src.replace(/`([^`]+)`/g, function (_, code) {
      codes.push(code);
      return '\u0000' + (codes.length - 1) + '\u0000';
    });

    // (2) 全部转义
    src = escapeHtml(src);

    // (3) 链接 [文本](地址)
    src = src.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (whole, label, url) {
      var safe = safeUrl(url);
      if (!safe) return label; // 危险协议：只显示文本
      return '<a href="' + safe + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
    });

    // (4) 粗体 / 斜体 / 删除线
    src = src.replace(/\*\*([^\n]+?)\*\*/g, '<strong>$1</strong>');
    src = src.replace(/__([^\n_]+?)__/g, '<strong>$1</strong>');
    src = src.replace(/(^|[^*\w])\*([^*\n]+?)\*(?!\*)/g, '$1<em>$2</em>');
    src = src.replace(/(^|[^_\w])_([^_\n]+?)_(?![\w_])/g, '$1<em>$2</em>');
    src = src.replace(/~~([^\n]+?)~~/g, '<del>$1</del>');

    // (5) 还原行内代码（代码内容单独转义，不受上面格式影响）
    src = src.replace(/\u0000(\d+)\u0000/g, function (_, idx) {
      return '<code>' + escapeHtml(codes[Number(idx)]) + '</code>';
    });

    return src;
  }

  /* ------------------------------ 3. 块级解析 ------------------------------ */

  var RE_FENCE = /^\s*(`{3,}|~{3,})\s*([^\s`]*)\s*$/;
  var RE_HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
  var RE_HR = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
  var RE_QUOTE = /^\s*>\s?(.*)$/;
  var RE_UL = /^([ \t]*)[-*+]\s+(.*)$/;
  var RE_OL = /^([ \t]*)(\d+)[.)]\s+(.*)$/;
  var RE_TABLE_CELL = /^\s*:?-{1,}:?\s*$/;

  /**
   * 判断一行是否为表格分隔行（如 |---|、|---|---|、--- | ---）。
   * 必须含至少一个 `|` 才能与水平线 `---` 区分；去掉首尾竖线后逐格校验，
   * 因此单列表格 `| a |\n|---|` 也能正确识别。
   */
  function isTableSep(line) {
    var s = String(line).trim();
    if (s.indexOf('|') === -1) return false;
    if (s.charAt(0) === '|') s = s.slice(1);
    if (s.charAt(s.length - 1) === '|') s = s.slice(0, -1);
    var cells = s.split('|');
    if (!cells.length) return false;
    return cells.every(function (c) { return RE_TABLE_CELL.test(c); });
  }

  /** 是否为块级结构的起始行（段落收集时用来判断断点） */
  function isBlockStart(lines, i) {
    var line = lines[i];
    return RE_FENCE.test(line) || RE_HEADING.test(line) || RE_HR.test(line) ||
      RE_QUOTE.test(line) || RE_UL.test(line) || RE_OL.test(line) || isTableStart(lines, i);
  }

  /** 围栏代码块 */
  function emitCode(lines, start, fence, out) {
    var marker = fence[1].charAt(0);
    var lang = fence[2] || '';
    var closeRe = new RegExp('^\\s*' + (marker === '`' ? '`{3,}' : '~{3,}') + '\\s*$');
    var buf = [];
    var i = start + 1;
    while (i < lines.length && !closeRe.test(lines[i])) { buf.push(lines[i]); i++; }
    if (i < lines.length) i++; // 跳过闭合围栏
    out.push(
      '<pre class="md-pre"' + (lang ? ' data-lang="' + escapeHtml(lang) + '"' : '') + '>' +
      '<code>' + escapeHtml(buf.join('\n')) + '</code></pre>'
    );
    return i;
  }

  /** 表格辅助 */
  function splitRow(line) {
    var s = String(line).trim();
    if (s.charAt(0) === '|') s = s.slice(1);
    if (s.charAt(s.length - 1) === '|') s = s.slice(0, -1);
    return s.split('|').map(function (c) { return c.trim(); });
  }

  function isTableStart(lines, i) {
    return i + 1 < lines.length &&
      String(lines[i]).indexOf('|') !== -1 &&
      isTableSep(lines[i + 1]);
  }

  function emitTable(lines, start, out) {
    var head = splitRow(lines[start]);
    var body = [];
    var i = start + 2;
    while (i < lines.length && !/^\s*$/.test(lines[i]) && String(lines[i]).indexOf('|') !== -1) {
      body.push(splitRow(lines[i]));
      i++;
    }
    var html = '<div class="md-table-wrap"><table class="md-table"><thead><tr>';
    head.forEach(function (cell) { html += '<th>' + renderInline(cell) + '</th>'; });
    html += '</tr></thead><tbody>';
    body.forEach(function (row) {
      html += '<tr>';
      for (var k = 0; k < head.length; k++) {
        html += '<td>' + renderInline(row[k] === undefined ? '' : row[k]) + '</td>';
      }
      html += '</tr>';
    });
    html += '</tbody></table></div>';
    out.push(html);
    return i;
  }

  /**
   * 列表解析（支持一层及以上缩进嵌套）。
   * 返回 { html, next }，next 为下一个未消费的行号。
   */
  function parseList(lines, start, ordered) {
    var re = ordered ? RE_OL : RE_UL;
    var first = lines[start].match(re);
    var baseIndent = first[1].length;
    var tag = ordered ? 'ol' : 'ul';
    var html = '<' + tag + '>';
    var i = start;

    while (i < lines.length) {
      var m = lines[i].match(re);
      if (!m || m[1].length !== baseIndent) break;

      var text = ordered ? m[3] : m[2];
      i++;

      // 收集本项的续行 / 嵌套列表
      var extra = [];
      while (i < lines.length) {
        var line = lines[i];

        if (/^\s*$/.test(line)) {
          // 空行：抢看下一非空行是否仍是更深层级的列表项（宽松续接）
          var j = i + 1;
          while (j < lines.length && /^\s*$/.test(lines[j])) j++;
          var after = j < lines.length ? lines[j].match(re) : null;
          if (after && after[1].length > baseIndent) { i = j; continue; }
          break;
        }

        var indent = line.match(/^[ \t]*/)[0].length;
        var nm = line.match(re);
        if (nm && nm[1].length === baseIndent) break;           // 同级下一项
        if (nm && nm[1].length > baseIndent) {                  // 嵌套子列表
          var sub = parseList(lines, i, ordered);
          extra.push(sub.html);
          i = sub.next;
          continue;
        }
        if (indent > baseIndent) {                              // 普通续行
          extra.push(renderInline(line.trim()));
          i++;
          continue;
        }
        break;
      }

      // 组装续行：嵌套列表原样保留，纯文本以 <br> 连接
      var tail = '';
      var pending = [];
      extra.forEach(function (piece) {
        if (/^<(ul|ol)/.test(piece)) {
          if (pending.length) { tail += '<br>' + pending.join('<br>'); pending = []; }
          tail += piece;
        } else {
          pending.push(piece);
        }
      });
      if (pending.length) {
        tail = /^<(ul|ol)/.test(tail)
          ? '<br>' + pending.join('<br>') + tail
          : tail + '<br>' + pending.join('<br>');
      }

      html += '<li>' + renderInline(text) + tail + '</li>';
    }

    html += '</' + tag + '>';
    return { html: html, next: i };
  }

  /** 块级渲染主循环 */
  function renderBlocks(lines) {
    var out = [];
    var i = 0;
    var n = lines.length;

    while (i < n) {
      var line = lines[i];

      if (/^\s*$/.test(line)) { i++; continue; }                  // 空行

      var fence = line.match(RE_FENCE);
      if (fence) { i = emitCode(lines, i, fence, out); continue; } // 代码块

      var heading = line.match(RE_HEADING);
      if (heading) {                                              // 标题
        var lvl = heading[1].length;
        out.push('<h' + lvl + '>' + renderInline(heading[2]) + '</h' + lvl + '>');
        i++;
        continue;
      }

      if (RE_HR.test(line)) { out.push('<hr>'); i++; continue; }   // 水平线

      if (RE_QUOTE.test(line)) {                                   // 引用块
        var quote = [];
        while (i < n && (RE_QUOTE.test(lines[i]) ||
          (/^\s*$/.test(lines[i]) && i + 1 < n && RE_QUOTE.test(lines[i + 1])))) {
          var qm = lines[i].match(RE_QUOTE);
          quote.push(qm ? qm[1] : '');
          i++;
        }
        out.push('<blockquote>' + renderBlocks(quote) + '</blockquote>');
        continue;
      }

      if (isTableStart(lines, i)) { i = emitTable(lines, i, out); continue; } // 表格

      if (RE_UL.test(line)) {                                      // 无序列表
        var ul = parseList(lines, i, false);
        out.push(ul.html);
        i = ul.next;
        continue;
      }

      if (RE_OL.test(line)) {                                      // 有序列表
        var ol = parseList(lines, i, true);
        out.push(ol.html);
        i = ol.next;
        continue;
      }

      // 段落：连续非空且非块起始的行，单换行渲染为 <br>
      var para = [];
      while (i < n && !/^\s*$/.test(lines[i]) && !isBlockStart(lines, i)) {
        para.push(lines[i]);
        i++;
      }
      if (para.length) {
        out.push('<p>' + para.map(function (l) { return renderInline(l); }).join('<br>') + '</p>');
      } else {
        i++; // 兜底：绝不死循环
      }
    }

    return out.join('\n');
  }

  /** 对外主入口：把 Markdown 源文本渲染为安全的 HTML 字符串 */
  function render(src) {
    if (src === null || src === undefined) return '';
    var text = String(src).replace(/\r\n?/g, '\n');
    return renderBlocks(text.split('\n'));
  }

  CTFA.markdown = {
    render: render,
    renderInline: renderInline,
    escapeHtml: escapeHtml
  };
})(window);