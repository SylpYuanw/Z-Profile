/* ============================================================================
   ZForge · 工具函数
   ========================================================================== */
(function (ZF) {
  'use strict';
  const U = {};

  U.collectionLabel = function (crawler) {
    const labels = { selenium: 'Selenium', http: 'HTTP 请求' };
    const collection = crawler.collection;
    if (!collection || !Array.isArray(collection.methods)) return '采集方式未声明';
    return collection.methods.map((method) => labels[method] || method).join(' + ');
  };
  U.collectionStages = function (crawler) {
    const labels = { selenium: 'Selenium', http: 'HTTP 请求' };
    return ((crawler.collection || {}).stages || [])
      .map((stage) => `${stage.label}：${labels[stage.method] || stage.method}`).join('；');
  };

  /* ---------- 数字 ---------- */
  U.num = function (n, d) {
    if (n === null || n === undefined || Number.isNaN(n)) return '—';
    const v = Number(n), a = Math.abs(v);
    if (a >= 1e8) return (v / 1e8).toFixed(d === undefined ? 1 : d) + '亿';
    if (a >= 1e4) return (v / 1e4).toFixed(d === undefined ? 1 : d) + '万';
    return String(Math.round(v * 100) / 100);
  };
  U.int = function (n) {
    if (n === null || n === undefined || Number.isNaN(n)) return '—';
    return Math.round(Number(n)).toLocaleString('en-US');
  };
  U.smart = (n) => (n === null || n === undefined ? '—' : Math.abs(Number(n)) >= 1e4 ? U.num(n) : U.int(n));
  U.pct = (n, d) => (n === null || n === undefined ? '—' : Number(n).toFixed(d === undefined ? 1 : d) + '%');
  U.money = (n, d) => (n === null || n === undefined ? '—' : '¥' + Number(n).toFixed(d === undefined ? 2 : d));
  U.bytes = function (b) {
    if (!b) return '0 B';
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    return (b / 1048576).toFixed(2) + ' MB';
  };
  U.dur = function (s) {
    if (s === null || s === undefined) return '—';
    s = Number(s);
    if (s < 60) return s.toFixed(0) + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm' + Math.round(s % 60) + 's';
    const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
    return h + 'h' + m + 'm';
  };
  U.durText = function (s) {
    s = Number(s) || 0;
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const p = (x) => String(x).padStart(2, '0');
    return h ? `${h}:${p(m)}:${p(sec)}` : `${m}:${p(sec)}`;
  };
  U.ts = function (sec, withTime) {
    if (!sec) return '—';
    const d = new Date(Number(sec) * 1000);
    const p = (x) => String(x).padStart(2, '0');
    const b = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
    return withTime ? `${b} ${p(d.getHours())}:${p(d.getMinutes())}` : b;
  };
  U.ago = function (sec) {
    if (!sec) return '—';
    const diff = Date.now() / 1000 - Number(sec);
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    if (diff < 2592000) return Math.floor(diff / 86400) + ' 天前';
    return U.ts(sec);
  };
  U.clock = function (sec) {
    const d = new Date((sec || Date.now() / 1000) * 1000);
    const p = (x) => String(x).padStart(2, '0');
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  };

  /* ---------- DOM ---------- */
  U.el = function (tag, attrs, children) {
    const n = document.createElement(tag);
    if (attrs) for (const k in attrs) {
      const v = attrs[k];
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') n.className = v;
      else if (k === 'html') n.innerHTML = v;
      else if (k === 'text') n.textContent = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(n.style, v);
      else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
      else if (k === 'dataset') Object.assign(n.dataset, v);
      else n.setAttribute(k, v === true ? '' : v);
    }
    U.append(n, children);
    return n;
  };
  U.append = function (node, c) {
    if (c === null || c === undefined || c === false) return node;
    if (Array.isArray(c)) { c.forEach((x) => U.append(node, x)); return node; }
    node.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    return node;
  };
  U.frag = (c) => U.append(document.createDocumentFragment(), c);
  U.clear = function (n) { while (n.firstChild) n.removeChild(n.firstChild); return n; };
  U.$ = (s, r) => (r || document).querySelector(s);
  U.$$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  U.debounce = function (fn, wait) {
    let t;
    return function (...a) { clearTimeout(t); t = setTimeout(() => fn.apply(this, a), wait); };
  };
  U.esc = (s) => String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  U.sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  U.clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  /** 语法着色 (用于展示代码片段) */
  U.highlight = function (code) {
    return U.esc(code)
      .replace(/(^|\n)(\s*)(#[^\n]*)/g, '$1$2<span class="cm">$3</span>')
      .replace(/("""[\s\S]*?"""|'[^'\n]*'|"[^"\n]*")/g, '<span class="st">$1</span>')
      .replace(/\b(def|class|return|if|elif|else|for|while|in|not|and|or|import|from|as|with|try|except|finally|lambda|yield|global|None|True|False|const|let|var|function|await|async|new|this)\b/g,
        '<span class="kw">$1</span>')
      .replace(/\b(\d+(?:\.\d+)?)\b/g, '<span class="nu">$1</span>');
  };

  ZF.util = U;
})(window.ZF = window.ZF || {});
