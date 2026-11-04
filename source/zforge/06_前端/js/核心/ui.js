/* ============================================================================
   ZForge · 组件库
   每个组件都是"返回 DOM 节点"的工厂函数。
   ========================================================================== */
(function (ZF) {
  'use strict';
  const U = ZF.util;
  const UI = {};

  /* ---------------- 容器 ---------------- */
  UI.view = (children) => U.el('div', { class: 'view' }, children);

  UI.section = function (title, hint, children, right) {
    return U.frag([
      U.el('div', { class: 'section-head' }, [
        U.el('h2', { text: title }),
        hint ? U.el('span', { class: 'hint', text: hint }) : null,
        U.el('span', { class: 'line' }),
        right || null,
      ]),
      children,
    ]);
  };

  UI.card = function (o) {
    o = o || {};
    return U.el('div', { class: 'card' + (o.class ? ' ' + o.class : ''), style: o.style }, [
      o.title !== undefined
        ? U.el('div', { class: 'card-title' }, [o.icon || null, o.title, o.badge || null])
        : null,
      o.sub ? U.el('p', { class: 'card-sub', text: o.sub }) : null,
      o.body || null,
    ]);
  };

  UI.grid = (cols, children, cls) =>
    U.el('div', { class: `grid ${cls || 'g-' + cols}` }, children);

  /* ---------------- KPI ---------------- */
  UI.kpi = function (label, value, note, accent) {
    const n = U.el('div', { class: 'kpi' }, [
      U.el('div', { class: 'kpi-label', text: label }),
      U.el('div', { class: 'kpi-value' }, value),
      note ? U.el('div', { class: 'kpi-note', text: note }) : null,
    ]);
    if (accent) n.style.setProperty('--accent', accent);
    return n;
  };

  /** 带滚动动画的 KPI */
  UI.kpiAnim = function (label, target, fmt, note, accent) {
    const span = U.el('span', { text: '0' });
    const node = UI.kpi(label, span, note, accent);
    const f = fmt || ((v) => U.smart(v));
    const t0 = performance.now(), dur = 850;
    (function tick(now) {
      const p = U.clamp((now - t0) / dur, 0, 1);
      const e = p === 1 ? 1 : 1 - Math.pow(2, -10 * p);
      span.textContent = f(target * e);
      if (p < 1) requestAnimationFrame(tick);
      else span.textContent = f(target);
    })(performance.now());
    return node;
  };

  /* ---------------- 标签 ---------------- */
  UI.tag = (text, kind) => U.el('span', { class: 'tag' + (kind ? ' ' + kind : ''), text });
  UI.chips = (arr, kind) => U.el('div', { class: 'chips' }, arr.map((t) => UI.tag(t, kind)));

  const STATE_TAG = {
    queued: ['排队中', 'muted'], running: ['运行中', 'accent'],
    success: ['成功', 'ok'], failed: ['失败', 'danger'], canceled: ['已取消', 'warn'],
    healthy: ['健康', 'ok'], cooling: ['冷却中', 'warn'],
    banned: ['已封禁', 'danger'], disabled: ['已停用', 'muted'],
  };
  UI.stateTag = function (state) {
    const [label, kind] = STATE_TAG[state] || [state, 'muted'];
    return UI.tag(label, kind);
  };

  /* ---------------- 表格 ---------------- */
  /**
   * UI.table({ columns, rows, pageSize, defaultSort, onRowClick })
   * column: { key, label, num, width, render(row,i), get(row), sortValue(row), sortable }
   */
  UI.table = function (o) {
    o = o || {};
    const columns = o.columns, all = o.rows || [], pageSize = o.pageSize || 0;
    let sortKey = o.sortKey || null, sortDir = o.sortDir || 'desc', page = 0;

    const wrap = U.el('div', { class: 'table-wrap' });
    const scroll = U.el('div', { class: 'table-scroll' });
    const table = U.el('table');
    const thead = U.el('thead'), tbody = U.el('tbody');
    table.append(thead, tbody);
    scroll.appendChild(table);
    wrap.appendChild(scroll);

    const pager = U.el('div', {
      style: {
        display: 'flex', alignItems: 'center', gap: '10px', padding: '8px 12px',
        borderTop: '1px solid var(--line-0)', fontFamily: 'var(--font-mono)',
        fontSize: '11px', color: 'var(--fg-2)',
      },
    });
    if (pageSize) wrap.appendChild(pager);

    const val = (col, row) => {
      if (col.sortValue) return col.sortValue(row);
      const v = col.get ? col.get(row) : row[col.key];
      return typeof v === 'number' ? v : String(v === null || v === undefined ? '' : v);
    };

    function current() {
      let rows = all;
      if (sortKey) {
        const col = columns.find((c) => c.key === sortKey);
        if (col) {
          rows = rows.slice().sort((a, b) => {
            const va = val(col, a), vb = val(col, b);
            const r = (typeof va === 'number' && typeof vb === 'number')
              ? va - vb : String(va).localeCompare(String(vb), 'zh-CN');
            return sortDir === 'asc' ? r : -r;
          });
        }
      }
      return rows;
    }

    function head() {
      U.clear(thead);
      const tr = U.el('tr');
      columns.forEach((c) => {
        const th = U.el('th', {
          class: [c.num ? 'num' : '', c.sortable === false ? '' : 'sortable',
            sortKey === c.key ? 'sorted' : ''].filter(Boolean).join(' '),
          style: c.width ? { width: c.width } : null,
          title: c.tip || '', text: c.label,
        });
        if (c.sortable !== false) {
          th.appendChild(U.el('span', {
            class: 'arrow',
            text: sortKey === c.key ? (sortDir === 'asc' ? '▲' : '▼') : '⇅',
          }));
          th.addEventListener('click', () => {
            if (sortKey === c.key) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
            else { sortKey = c.key; sortDir = c.num ? 'desc' : 'asc'; }
            page = 0; head(); body();
          });
        }
        tr.appendChild(th);
      });
      thead.appendChild(tr);
    }

    function body() {
      U.clear(tbody);
      const rows = current();
      const start = pageSize ? page * pageSize : 0;
      const slice = pageSize ? rows.slice(start, start + pageSize) : rows;

      if (!slice.length) {
        tbody.appendChild(U.el('tr', {}, [
          U.el('td', { colspan: columns.length }, [UI.empty('∅', '没有匹配的数据')]),
        ]));
      }
      slice.forEach((row, i) => {
        const tr = U.el('tr');
        if (o.onRowClick) {
          tr.style.cursor = 'pointer';
          tr.addEventListener('click', () => o.onRowClick(row));
        }
        columns.forEach((c) => {
          const td = U.el('td', { class: c.num ? 'num' : c.class || '' });
          if (c.render) U.append(td, c.render(row, start + i));
          else {
            const v = c.get ? c.get(row) : row[c.key];
            td.textContent = v === null || v === undefined ? '—' : String(v);
          }
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });

      if (pageSize) {
        const pages = Math.max(1, Math.ceil(rows.length / pageSize));
        U.clear(pager);
        U.append(pager, [
          U.el('span', { text: `共 ${U.int(rows.length)} 行` }),
          U.el('span', { style: { flex: '1' } }),
          U.el('button', {
            class: 'btn', style: { padding: '3px 10px' }, text: '‹', disabled: page === 0,
            onclick: () => { page = Math.max(0, page - 1); body(); },
          }),
          U.el('span', { text: `${page + 1} / ${pages}` }),
          U.el('button', {
            class: 'btn', style: { padding: '3px 10px' }, text: '›', disabled: page >= pages - 1,
            onclick: () => { page = Math.min(pages - 1, page + 1); body(); },
          }),
        ]);
      }
    }

    head(); body();
    return wrap;
  };

  UI.barCell = (value, max, text) => U.el('div', { class: 'bar-cell' }, [
    U.el('span', { text: text !== undefined ? text : U.smart(value) }),
    U.el('div', { class: 'bar-track' }, [
      U.el('div', { class: 'bar-fill', style: { width: `${max ? U.clamp((value / max) * 100, 0, 100) : 0}%` } }),
    ]),
  ]);

  /* ---------------- 终端 ---------------- */
  UI.terminal = function (o) {
    o = o || {};
    const box = U.el('div', { class: 'term', style: o.style });
    box.__n = 0;
    const max = o.max || 600;
    box.write = function (level, msg, ts) {
      const ln = U.el('div', { class: 'ln' }, [
        U.el('span', { class: 'ts', text: ts ? U.clock(ts) : U.clock() }),
        U.el('span', { class: 'lv ' + level, text: level }),
        U.el('span', { text: msg }),
      ]);
      box.appendChild(ln);
      while (box.childElementCount > max) box.removeChild(box.firstChild);
      box.scrollTop = box.scrollHeight;
      return ln;
    };
    box.clear = () => { U.clear(box); box.__n = 0; return box; };
    return box;
  };

  /* ---------------- 进度 ---------------- */
  UI.progress = function () {
    const fill = U.el('i', { style: { width: '0%' } });
    const node = U.el('div', { class: 'progress' }, [fill]);
    node.set = (p) => { fill.style.width = `${U.clamp(p, 0, 1) * 100}%`; };
    return node;
  };

  /* ---------------- Toast ---------------- */
  UI.toast = function (msg, kind) {
    const host = document.getElementById('toasts');
    if (!host) return;
    const node = U.el('div', { class: 'toast' + (kind ? ' ' + kind : ''), text: msg });
    host.appendChild(node);
    setTimeout(() => {
      node.style.transition = 'opacity .3s, transform .3s';
      node.style.opacity = '0';
      node.style.transform = 'translateX(12px)';
      setTimeout(() => node.remove(), 320);
    }, kind === 'bad' ? 5000 : 2800);
  };

  /* ---------------- 键值列表 ---------------- */
  UI.kv = function (pairs) {
    const dl = U.el('dl', { class: 'kv' });
    pairs.forEach(([k, v]) => {
      if (v === null || v === undefined) return;
      dl.append(U.el('dt', { text: k }), U.el('dd', {}, v instanceof Node ? v : String(v)));
    });
    return dl;
  };

  /* ---------------- 空态 / 骨架 ---------------- */
  UI.empty = (icon, title, sub) => U.el('div', { class: 'empty' }, [
    U.el('div', { class: 'big', text: icon || '∅' }),
    U.el('div', { style: { fontWeight: '600', color: 'var(--fg-0)' }, text: title || '暂无数据' }),
    sub ? U.el('div', { style: { marginTop: '6px' }, text: sub }) : null,
  ]);
  UI.skeleton = (h) => U.el('div', { class: 'skeleton', style: { height: (h || 120) + 'px' } });
  UI.code = (src) => U.el('pre', { class: 'code', html: U.highlight(src) });

  /* ---------------- 表单控件 ---------------- */
  UI.field = (label, control, help) =>
    U.el('div', { class: 'field' }, [
      U.el('label', { text: label }),
      control,
      help ? U.el('div', { class: 'field-help', text: help }) : null,
    ]);

  UI.select = function (options, value, onChange) {
    const s = U.el('select');
    options.forEach((o) => {
      const v = typeof o === 'string' ? o : o.value;
      const l = typeof o === 'string' ? o : o.label;
      s.appendChild(U.el('option', { value: v, text: l, selected: v === value }));
    });
    s.addEventListener('change', () => onChange(s.value));
    return s;
  };

  UI.segmented = function (options, value, onChange) {
    const node = U.el('div', { class: 'seg', role: 'group' });
    let cur = value;
    const btns = options.map((o) => {
      const v = typeof o === 'string' ? o : o.value;
      const label = typeof o === 'string' ? o : o.label;
      const b = U.el('button', {
        type: 'button', text: label, 'aria-pressed': String(v === cur),
        onclick: () => {
          cur = v;
          btns.forEach((x) => x.setAttribute('aria-pressed', 'false'));
          b.setAttribute('aria-pressed', 'true');
          onChange(v);
        },
      });
      return b;
    });
    btns.forEach((b) => node.appendChild(b));
    return node;
  };

  /* ---------------- 弹窗 ---------------- */
  UI.modal = function (o) {
    o = o || {};
    const box = U.el('div', {
      class: 'palette',
      style: { width: o.width || 'min(780px, 92vw)', maxHeight: '84vh', display: 'flex', flexDirection: 'column' },
    });
    const backdrop = U.el('div', {
      class: 'palette-backdrop',
      onclick: (e) => { if (e.target === backdrop) close(); },
    });
    let closed = false;
    const close = () => {
      if (closed) return;
      closed = true;
      backdrop.remove();
      document.removeEventListener('keydown', onKey);
      if (o.onClose) o.onClose();
    };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    box.appendChild(U.el('div', {
      style: { display: 'flex', alignItems: 'center', gap: '10px', padding: '12px 18px', borderBottom: '1px solid var(--line-0)' },
    }, [
      U.el('div', { style: { fontWeight: '600', fontSize: 'var(--fs-lg)' }, text: o.title || '' }),
      U.el('div', { style: { flex: '1' } }),
      U.el('button', { class: 'icon-btn', text: '✕', onclick: close }),
    ]));
    box.appendChild(U.el('div', { style: { padding: '18px', overflowY: 'auto' } }, o.body));
    backdrop.appendChild(box);
    document.body.appendChild(backdrop);
    document.addEventListener('keydown', onKey);
    return { close, body: box.lastChild, backdrop };
  };

  /* ---------------- 导出 ---------------- */
  UI.download = function (filename, content, mime) {
    const blob = new Blob([content], { type: mime || 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = U.el('a', { href: url, download: filename });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  };

  UI.toCSV = function (rows, columns) {
    const esc = (v) => {
      const s = v === null || v === undefined ? '' : String(v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    return '\ufeff'
      + columns.map((c) => esc(c.label)).join(',') + '\n'
      + rows.map((r) => columns.map((c) => esc(c.get ? c.get(r) : r[c.key])).join(',')).join('\n');
  };

  UI.toXLSX = function (rows, columns, sheetName) {
    if (typeof XLSX === 'undefined') { UI.toast('XLSX 库未加载，改用 CSV', 'bad'); return false; }
    const aoa = [columns.map((c) => c.label)].concat(
      rows.map((r) => columns.map((c) => (c.get ? c.get(r) : r[c.key])))
    );
    const ws = XLSX.utils.aoa_to_sheet(aoa);
    ws['!cols'] = columns.map((c) => ({ wch: c.width || 18 }));
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, (sheetName || 'Sheet1').slice(0, 31));
    XLSX.writeFile(wb, `${sheetName || 'zforge'}.xlsx`);
    return true;
  };

  ZF.ui = UI;
})(window.ZF = window.ZF || {});
