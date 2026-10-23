/* ============================================================================
 * codec.js — CTF 编码 / 解码工具箱（min.html 的「伪装面」）
 * ----------------------------------------------------------------------------
 * 全部为**纯 JavaScript 同步实现**：不依赖 crypto.subtle（页面常以 http 提供，
 * 非安全上下文下 subtle 不可用），因此在任何环境下都可用。
 *
 * 算法：Base64 / Base64URL / Base32 / Base58 / Hex / URL / HTML / Unicode /
 *       ROT13 / 凯撒 / 摩斯 / 二进制 / 十进制 / 八进制 / 反转 / JWT /
 *       MD5 / SHA-1 / SHA-256
 *
 * 对外：CTFA.codec.init()
 * ========================================================================== */
(function (global) {
  'use strict';

  var CTFA = (global.CTFA = global.CTFA || {});
  var doc = global.document;

  /* ------------------------------ 字节工具 ------------------------------ */

  var TE = new global.TextEncoder();
  var TD = new global.TextDecoder('utf-8', { fatal: false });

  function toBytes(str) { return TE.encode(String(str)); }
  function fromBytes(bytes) { return TD.decode(bytes); }

  function bytesToBin(bytes) {
    var s = '';
    for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return s;
  }
  function binToBytes(bin) {
    var out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i) & 0xff;
    return out;
  }

  /** 大端 64 位长度写入（消息小于 2^32 位时高 32 位为 0） */
  function writeLengthBE(buf, at, byteLen) {
    var bits = byteLen * 8;
    var hi = Math.floor(bits / 4294967296);
    var lo = bits >>> 0;
    buf[at] = (hi >>> 24) & 0xff; buf[at + 1] = (hi >>> 16) & 0xff;
    buf[at + 2] = (hi >>> 8) & 0xff; buf[at + 3] = hi & 0xff;
    buf[at + 4] = (lo >>> 24) & 0xff; buf[at + 5] = (lo >>> 16) & 0xff;
    buf[at + 6] = (lo >>> 8) & 0xff; buf[at + 7] = lo & 0xff;
  }
  function writeLengthLE(buf, at, byteLen) {
    var bits = byteLen * 8;
    var hi = Math.floor(bits / 4294967296);
    var lo = bits >>> 0;
    buf[at] = lo & 0xff; buf[at + 1] = (lo >>> 8) & 0xff;
    buf[at + 2] = (lo >>> 16) & 0xff; buf[at + 3] = (lo >>> 24) & 0xff;
    buf[at + 4] = hi & 0xff; buf[at + 5] = (hi >>> 8) & 0xff;
    buf[at + 6] = (hi >>> 16) & 0xff; buf[at + 7] = (hi >>> 24) & 0xff;
  }
  function padTo64(bytes, le) {
    var withOne = bytes.length + 1;
    var total = withOne + ((56 - withOne % 64) + 64) % 64 + 8;
    var buf = new Uint8Array(total);
    buf.set(bytes);
    buf[bytes.length] = 0x80;
    if (le) writeLengthLE(buf, total - 8, bytes.length);
    else writeLengthBE(buf, total - 8, bytes.length);
    return buf;
  }
  function hex32(arr) {
    return arr.map(function (x) { return (x >>> 0).toString(16).padStart(8, '0'); }).join('');
  }

  /* -------------------------------- Base64 -------------------------------- */

  function b64enc(s) { return global.btoa(bytesToBin(toBytes(s))); }
  function b64dec(s) {
    var t = String(s).replace(/[\s\r\n]+/g, '').replace(/-/g, '+').replace(/_/g, '/');
    if (!t) return '';
    while (t.length % 4) t += '=';
    return fromBytes(binToBytes(global.atob(t)));
  }
  function b64uenc(s) { return b64enc(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''); }

  /* -------------------------------- Base32 -------------------------------- */

  var B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  function b32enc(s) {
    var bytes = toBytes(s), bits = 0, value = 0, out = '';
    for (var i = 0; i < bytes.length; i++) {
      value = (value << 8) | bytes[i];
      bits += 8;
      while (bits >= 5) { out += B32[(value >>> (bits - 5)) & 31]; bits -= 5; }
    }
    if (bits > 0) out += B32[(value << (5 - bits)) & 31];
    while (out.length % 8) out += '=';
    return out;
  }
  function b32dec(s) {
    var t = String(s).toUpperCase().replace(/[\s=]+/g, '');
    var bits = 0, value = 0, bytes = [];
    for (var i = 0; i < t.length; i++) {
      var idx = B32.indexOf(t[i]);
      if (idx < 0) throw new Error('非法 Base32 字符：' + t[i]);
      value = (value << 5) | idx;
      bits += 5;
      if (bits >= 8) { bytes.push((value >>> (bits - 8)) & 0xff); bits -= 8; }
    }
    return fromBytes(new Uint8Array(bytes));
  }

  /* -------------------------------- Base58 -------------------------------- */

  var B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
  function b58enc(s) {
    var bytes = toBytes(s);
    if (!bytes.length) return '';
    var digits = [0];
    for (var i = 0; i < bytes.length; i++) {
      var carry = bytes[i];
      for (var j = 0; j < digits.length; j++) {
        carry += digits[j] << 8;
        digits[j] = carry % 58;
        carry = (carry / 58) | 0;
      }
      while (carry) { digits.push(carry % 58); carry = (carry / 58) | 0; }
    }
    var out = '';
    for (var z = 0; z < bytes.length && bytes[z] === 0; z++) out += '1';
    for (var k = digits.length - 1; k >= 0; k--) out += B58[digits[k]];
    return out;
  }
  function b58dec(s) {
    var t = String(s).replace(/\s+/g, '');
    if (!t) return '';
    var bytes = [0];
    for (var i = 0; i < t.length; i++) {
      var idx = B58.indexOf(t[i]);
      if (idx < 0) throw new Error('非法 Base58 字符：' + t[i]);
      var carry = idx;
      for (var j = 0; j < bytes.length; j++) {
        carry += bytes[j] * 58;
        bytes[j] = carry & 0xff;
        carry >>= 8;
      }
      while (carry) { bytes.push(carry & 0xff); carry >>= 8; }
    }
    for (var z = 0; z < t.length && t[z] === '1'; z++) bytes.push(0);
    return fromBytes(new Uint8Array(bytes.reverse()));
  }

  /* ---------------------------------- Hex ---------------------------------- */

  function hexenc(s) {
    var bytes = toBytes(s), out = '';
    for (var i = 0; i < bytes.length; i++) out += bytes[i].toString(16).padStart(2, '0');
    return out;
  }
  function hexdec(s) {
    var t = String(s).replace(/[\s:,\-]+/g, '').replace(/^0x/i, '');
    if (!t) return '';
    if (t.length % 2) throw new Error('十六进制长度必须为偶数');
    var bytes = new Uint8Array(t.length / 2);
    for (var i = 0; i < bytes.length; i++) {
      var b = parseInt(t.substr(i * 2, 2), 16);
      if (isNaN(b)) throw new Error('非法十六进制字符');
      bytes[i] = b;
    }
    return fromBytes(bytes);
  }

  /* ---------------------------------- URL ---------------------------------- */

  function urlenc(s) { return global.encodeURIComponent(String(s)); }
  function urldec(s) {
    try { return global.decodeURIComponent(String(s)); }
    catch (e) { throw new Error('不是合法的 URL 编码'); }
  }

  /* --------------------------------- HTML ---------------------------------- */

  function htmlenc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function htmldec(s) {
    var ta = doc.createElement('textarea');
    ta.innerHTML = String(s);
    return ta.value;
  }

  /* -------------------------------- Unicode -------------------------------- */

  function uniesc(s) {
    return String(s).replace(/[\s\S]/g, function (c) {
      var code = c.charCodeAt(0);
      return code > 127 ? '\\u' + code.toString(16).padStart(4, '0') : c;
    });
  }
  function unidec(s) {
    return String(s)
      .replace(/\\u\{([0-9a-fA-F]+)\}/g, function (m, h) { return String.fromCodePoint(parseInt(h, 16)); })
      .replace(/\\u([0-9a-fA-F]{4})/g, function (m, h) { return String.fromCharCode(parseInt(h, 16)); })
      .replace(/\\x([0-9a-fA-F]{2})/g, function (m, h) { return String.fromCharCode(parseInt(h, 16)); })
      .replace(/U\+([0-9a-fA-F]{4,6})/g, function (m, h) { return String.fromCodePoint(parseInt(h, 16)); });
  }

  /* ----------------------------- ROT13 / 凯撒 ----------------------------- */

  function rotN(s, n) {
    n = ((n % 26) + 26) % 26;
    return String(s).replace(/[a-zA-Z]/g, function (c) {
      var base = c <= 'Z' ? 65 : 97;
      return String.fromCharCode((c.charCodeAt(0) - base + n) % 26 + base);
    });
  }

  /* --------------------------------- 摩斯 --------------------------------- */

  var MORSE = {
    A: '.-', B: '-...', C: '-.-.', D: '-..', E: '.', F: '..-.', G: '--.', H: '....', I: '..', J: '.---',
    K: '-.-', L: '.-..', M: '--', N: '-.', O: '---', P: '.--.', Q: '--.-', R: '.-.', S: '...', T: '-',
    U: '..-', V: '...-', W: '.--', X: '-..-', Y: '-.--', Z: '--..',
    '0': '-----', '1': '.----', '2': '..---', '3': '...--', '4': '....-',
    '5': '.....', '6': '-....', '7': '--...', '8': '---..', '9': '----.',
    '.': '.-.-.-', ',': '--..--', '?': '..--..', "'": '.----.', '!': '-.-.--', '/': '-..-.',
    '(': '-.--.', ')': '-.--.-', '&': '.-...', ':': '---...', ';': '-.-.-.', '=': '-...-',
    '+': '.-.-.', '-': '-....-', '_': '..--.-', '"': '.-..-.', '$': '...-..-', '@': '.--.-.'
  };
  var MORSE_REV = (function () {
    var r = {};
    Object.keys(MORSE).forEach(function (k) { r[MORSE[k]] = k; });
    return r;
  })();

  function morseenc(s) {
    return String(s).toUpperCase().split('').map(function (c) {
      if (c === ' ') return '/';
      return MORSE[c] || '';
    }).filter(Boolean).join(' ');
  }
  function morsedec(s) {
    return String(s).trim().split(/\s+/).map(function (code) {
      if (code === '/') return ' ';
      return MORSE_REV[code] || '';
    }).join('');
  }

  /* ------------------------------ 二进制 / 进制 ------------------------------ */

  function binenc(s) {
    var bytes = toBytes(s), parts = [];
    for (var i = 0; i < bytes.length; i++) parts.push(bytes[i].toString(2).padStart(8, '0'));
    return parts.join(' ');
  }
  function bindec(s) {
    var t = String(s).replace(/[^01]/g, '');
    if (!t) return '';
    if (t.length % 8) throw new Error('二进制位数不是 8 的倍数');
    var bytes = new Uint8Array(t.length / 8);
    for (var i = 0; i < bytes.length; i++) bytes[i] = parseInt(t.substr(i * 8, 8), 2);
    return fromBytes(bytes);
  }
  function radixenc(s, radix) {
    var bytes = toBytes(s), parts = [];
    for (var i = 0; i < bytes.length; i++) parts.push(bytes[i].toString(radix));
    return parts.join(' ');
  }
  function radixdec(s, radix, label) {
    var nums = String(s).trim().split(/[\s,]+/).filter(Boolean);
    if (!nums.length) return '';
    var bytes = new Uint8Array(nums.length);
    for (var i = 0; i < nums.length; i++) {
      var n = parseInt(nums[i], radix);
      if (isNaN(n) || n < 0 || n > 255) throw new Error('非法' + label + '字节：' + nums[i]);
      bytes[i] = n;
    }
    return fromBytes(bytes);
  }

  /* --------------------------------- 反转 --------------------------------- */

  function rev(s) { return String(s).split('').reverse().join(''); }

  /* ---------------------------------- JWT ---------------------------------- */

  function jwtdec(s) {
    var parts = String(s).trim().split('.');
    if (parts.length < 2) throw new Error('不是合法的 JWT（应为 header.payload.signature）');
    function seg(p) {
      try { return JSON.parse(b64dec(p)); }
      catch (e) { return { _raw: p, _error: '解析失败' }; }
    }
    var out = { header: seg(parts[0]), payload: seg(parts[1]) };
    if (parts[2]) out.signature = parts[2];
    return JSON.stringify(out, null, 2);
  }

  /* --------------------------------- 哈希 --------------------------------- */

  var K256 = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
  ];

  function md5hex(s) {
    var bytes = toBytes(s);
    var buf = padTo64(bytes, true);
    var S = [7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
      5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
      4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
      6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21];
    var K = new Uint32Array(64);
    for (var i = 0; i < 64; i++) K[i] = Math.floor(Math.abs(Math.sin(i + 1)) * 4294967296);

    var a0 = 0x67452301, b0 = 0xefcdab89, c0 = 0x98badcfe, d0 = 0x10325476;
    for (var off = 0; off < buf.length; off += 64) {
      var M = new Uint32Array(16);
      for (var j = 0; j < 16; j++) {
        M[j] = buf[off + j * 4] | (buf[off + j * 4 + 1] << 8) |
               (buf[off + j * 4 + 2] << 16) | (buf[off + j * 4 + 3] << 24);
      }
      var A = a0, B = b0, C = c0, D = d0;
      for (var k = 0; k < 64; k++) {
        var F, g;
        if (k < 16) { F = (B & C) | (~B & D); g = k; }
        else if (k < 32) { F = (D & B) | (~D & C); g = (5 * k + 1) % 16; }
        else if (k < 48) { F = B ^ C ^ D; g = (3 * k + 5) % 16; }
        else { F = C ^ (B | ~D); g = (7 * k) % 16; }
        F = (F + A + K[k] + M[g]) | 0;
        A = D; D = C; C = B;
        B = (B + ((F << S[k]) | (F >>> (32 - S[k])))) | 0;
      }
      a0 = (a0 + A) | 0; b0 = (b0 + B) | 0; c0 = (c0 + C) | 0; d0 = (d0 + D) | 0;
    }
    return [a0, b0, c0, d0].map(function (x) {
      var s = '';
      for (var i = 0; i < 4; i++) s += ((x >>> (i * 8)) & 0xff).toString(16).padStart(2, '0');
      return s;
    }).join('');
  }

  function sha1hex(s) {
    var buf = padTo64(toBytes(s), false);
    var h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0];
    for (var off = 0; off < buf.length; off += 64) {
      var w = new Uint32Array(80);
      for (var i = 0; i < 16; i++) {
        w[i] = (buf[off + i * 4] << 24) | (buf[off + i * 4 + 1] << 16) |
               (buf[off + i * 4 + 2] << 8) | buf[off + i * 4 + 3];
      }
      for (var n = 16; n < 80; n++) {
        var v = w[n - 3] ^ w[n - 8] ^ w[n - 14] ^ w[n - 16];
        w[n] = (v << 1) | (v >>> 31);
      }
      var a = h[0], b = h[1], c = h[2], d = h[3], e = h[4];
      for (var t = 0; t < 80; t++) {
        var f, k;
        if (t < 20) { f = (b & c) | (~b & d); k = 0x5A827999; }
        else if (t < 40) { f = b ^ c ^ d; k = 0x6ED9EBA1; }
        else if (t < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8F1BBCDC; }
        else { f = b ^ c ^ d; k = 0xCA62C1D6; }
        var tmp = (((a << 5) | (a >>> 27)) + f + e + k + w[t]) | 0;
        e = d; d = c; c = (b << 30) | (b >>> 2); b = a; a = tmp;
      }
      h[0] = (h[0] + a) | 0; h[1] = (h[1] + b) | 0; h[2] = (h[2] + c) | 0;
      h[3] = (h[3] + d) | 0; h[4] = (h[4] + e) | 0;
    }
    return hex32(h);
  }

  function sha256hex(s) {
    var buf = padTo64(toBytes(s), false);
    var h = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    for (var off = 0; off < buf.length; off += 64) {
      var w = new Uint32Array(64);
      for (var i = 0; i < 16; i++) {
        w[i] = (buf[off + i * 4] << 24) | (buf[off + i * 4 + 1] << 16) |
               (buf[off + i * 4 + 2] << 8) | buf[off + i * 4 + 3];
      }
      for (var n = 16; n < 64; n++) {
        var x = w[n - 15], y = w[n - 2];
        var s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
        var s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
        w[n] = (w[n - 16] + s0 + w[n - 7] + s1) | 0;
      }
      var a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
      for (var t = 0; t < 64; t++) {
        var S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
        var ch = (e & f) ^ (~e & g);
        var t1 = (hh + S1 + ch + K256[t] + w[t]) | 0;
        var S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
        var maj = (a & b) ^ (a & c) ^ (b & c);
        var t2 = (S0 + maj) | 0;
        hh = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
      }
      h[0] = (h[0] + a) | 0; h[1] = (h[1] + b) | 0; h[2] = (h[2] + c) | 0; h[3] = (h[3] + d) | 0;
      h[4] = (h[4] + e) | 0; h[5] = (h[5] + f) | 0; h[6] = (h[6] + g) | 0; h[7] = (h[7] + hh) | 0;
    }
    return hex32(h);
  }

  /* ------------------------------ 算法注册表 ------------------------------ */

  var ALGOS = [
    { id: 'base64', name: 'Base64', enc: b64enc, dec: b64dec },
    { id: 'base64url', name: 'Base64URL', enc: b64uenc, dec: b64dec },
    { id: 'base32', name: 'Base32', enc: b32enc, dec: b32dec },
    { id: 'base58', name: 'Base58', enc: b58enc, dec: b58dec },
    { id: 'hex', name: 'Hex', enc: hexenc, dec: hexdec },
    { id: 'url', name: 'URL', enc: urlenc, dec: urldec },
    { id: 'html', name: 'HTML', enc: htmlenc, dec: htmldec },
    { id: 'unicode', name: 'Unicode', enc: uniesc, dec: unidec },
    { id: 'rot13', name: 'ROT13', enc: function (s) { return rotN(s, 13); }, dec: function (s) { return rotN(s, 13); } },
    { id: 'caesar', name: '凯撒', enc: function (s, p) { return rotN(s, p); }, dec: function (s, p) { return rotN(s, -p); }, param: 'shift' },
    { id: 'morse', name: '摩斯', enc: morseenc, dec: morsedec },
    { id: 'binary', name: '二进制', enc: binenc, dec: bindec },
    { id: 'decimal', name: '十进制', enc: function (s) { return radixenc(s, 10); }, dec: function (s) { return radixdec(s, 10, '十进制'); } },
    { id: 'octal', name: '八进制', enc: function (s) { return radixenc(s, 8); }, dec: function (s) { return radixdec(s, 8, '八进制'); } },
    { id: 'reverse', name: '反转', enc: rev, dec: rev },
    { id: 'jwt', name: 'JWT', enc: null, dec: jwtdec },
    { id: 'md5', name: 'MD5', enc: md5hex, dec: null },
    { id: 'sha1', name: 'SHA-1', enc: sha1hex, dec: null },
    { id: 'sha256', name: 'SHA-256', enc: sha256hex, dec: null }
  ];

  /* ---------------------------------- UI ---------------------------------- */

  var refs = {};
  var currentId = 'base64';

  function current() {
    for (var i = 0; i < ALGOS.length; i++) if (ALGOS[i].id === currentId) return ALGOS[i];
    return ALGOS[0];
  }

  function paramValue() {
    var n = parseInt((refs.shift && refs.shift.value) || '3', 10);
    return isNaN(n) ? 3 : n;
  }

  function setStatus(text, tone) {
    if (!refs.status) return;
    refs.status.textContent = text;
    refs.status.setAttribute('data-tone', tone || 'idle');
  }

  function updateCounts() {
    if (refs.inCount) refs.inCount.textContent = ((refs.input && refs.input.value) || '').length + ' 字符';
    if (refs.outCount) refs.outCount.textContent = ((refs.output && refs.output.value) || '').length + ' 字符';
  }

  function selectAlgo(id) {
    currentId = id;
    Array.prototype.forEach.call(refs.tabs.querySelectorAll('.cd-tab'), function (b) {
      b.classList.toggle('on', b.getAttribute('data-algo') === id);
    });
    var algo = current();
    if (refs.param) refs.param.hidden = algo.param !== 'shift';
    if (refs.enc) refs.enc.disabled = !algo.enc;
    if (refs.dec) refs.dec.disabled = !algo.dec;
    setStatus('已选择 ' + algo.name, 'idle');
  }

  function renderTabs() {
    refs.tabs.textContent = '';
    ALGOS.forEach(function (algo) {
      var b = doc.createElement('button');
      b.type = 'button';
      b.className = 'cd-tab';
      b.setAttribute('data-algo', algo.id);
      b.textContent = algo.name;
      b.addEventListener('click', function () { selectAlgo(algo.id); });
      refs.tabs.appendChild(b);
    });
  }

  function run(direction) {
    var algo = current();
    var fn = direction === 'enc' ? algo.enc : algo.dec;
    if (!fn) {
      setStatus(algo.name + ' 不支持' + (direction === 'enc' ? '编码' : '解码'), 'error');
      return;
    }
    try {
      var out = fn(refs.input.value, paramValue());
      refs.output.value = out === undefined || out === null ? '' : String(out);
      setStatus('完成 · ' + algo.name + (direction === 'enc' ? ' 编码' : ' 解码'), 'ok');
    } catch (err) {
      refs.output.value = '';
      setStatus('失败：' + ((err && err.message) || err), 'error');
    }
    updateCounts();
  }

  function init() {
    refs.tabs = doc.getElementById('cd-tabs');
    refs.param = doc.getElementById('cd-param');
    refs.shift = doc.getElementById('cd-shift');
    refs.input = doc.getElementById('cd-in');
    refs.output = doc.getElementById('cd-out');
    refs.inCount = doc.getElementById('cd-in-count');
    refs.outCount = doc.getElementById('cd-out-count');
    refs.status = doc.getElementById('cd-status');
    refs.enc = doc.getElementById('cd-enc');
    refs.dec = doc.getElementById('cd-dec');
    if (!refs.tabs || !refs.input) return;

    renderTabs();
    selectAlgo(currentId);

    if (refs.input) refs.input.addEventListener('input', updateCounts);
    if (refs.enc) refs.enc.addEventListener('click', function () { run('enc'); });
    if (refs.dec) refs.dec.addEventListener('click', function () { run('dec'); });

    var swap = doc.getElementById('cd-swap');
    if (swap) swap.addEventListener('click', function () {
      var t = refs.input.value;
      refs.input.value = refs.output.value;
      refs.output.value = t;
      updateCounts();
      setStatus('已交换输入与输出', 'idle');
    });

    var clear = doc.getElementById('cd-clear');
    if (clear) clear.addEventListener('click', function () {
      refs.input.value = '';
      refs.output.value = '';
      updateCounts();
      setStatus('已清空', 'idle');
    });

    var copy = doc.getElementById('cd-copy');
    if (copy) copy.addEventListener('click', function () {
      var text = refs.output.value || '';
      if (!text) { setStatus('没有可复制的内容', 'error'); return; }
      var done = function () { setStatus('已复制到剪贴板', 'ok'); };
      if (global.navigator && global.navigator.clipboard && global.navigator.clipboard.writeText) {
        global.navigator.clipboard.writeText(text).then(done, function () { setStatus('复制失败', 'error'); });
      } else {
        refs.output.removeAttribute('readonly');
        refs.output.select();
        try { doc.execCommand('copy'); done(); } catch (e) { setStatus('复制失败', 'error'); }
        refs.output.setAttribute('readonly', 'readonly');
      }
    });

    var full = doc.getElementById('cd-full');
    if (full) full.addEventListener('click', function () { CTFA.codec.onFullscreen && CTFA.codec.onFullscreen(); });
    var close = doc.getElementById('cd-close');
    if (close) close.addEventListener('click', function () { try { global.close(); } catch (e) { /* ignore */ } });

    updateCounts();
  }

  CTFA.codec = {
    init: init,
    ALGOS: ALGOS,
    /* 供测试使用 */
    _hash: { md5: md5hex, sha1: sha1hex, sha256: sha256hex },
    _enc: {
      base64: b64enc, base64url: b64uenc, base32: b32enc, base58: b58enc,
      hex: hexenc, url: urlenc, html: htmlenc, unicode: uniesc,
      morse: morseenc, binary: binenc, reverse: rev, jwt: jwtdec
    },
    _dec: {
      base64: b64dec, base32: b32dec, base58: b58dec, hex: hexdec,
      url: urldec, html: htmldec, unicode: unidec, morse: morsedec, binary: bindec
    },
    _rot: rotN
  };
})(window);
