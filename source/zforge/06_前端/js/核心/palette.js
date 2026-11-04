/* ============================================================================
   ZForge · 命令面板 (Ctrl/⌘ + K)
   跳页面 + 搜数据集 + 触发动作
   ========================================================================== */
(function (ZF) {
  'use strict';
  const U = ZF.util;

  function build() {
    const cmds = [];
    ZF.router.screens.forEach((s) => cmds.push({
      kind: 'page', title: s.title, sub: s.group, icon: '▸', run: () => ZF.router.go(s.id),
    }));

    ZF.store.crawlers.forEach((c) => cmds.push({
      kind: '爬虫', title: c.name, sub: c.site, icon: '⚙',
      run: () => ZF.router.go('crawler', { id: c.id }),
    }));

    cmds.push(
      { kind: '动作', title: '切换主题（深色 / 浅色）', icon: '◐', run: () => document.getElementById('btn-theme').click() },
      { kind: '动作', title: '切换信息密度', icon: '≡', run: () => document.getElementById('btn-density').click() },
      { kind: '动作', title: '打印 / 导出 PDF', icon: '⎙', run: () => window.print() },
      { kind: '动作', title: '导出全部数据为 Excel', icon: '⤓', run: () => ZF.exportAll() },
      { kind: '动作', title: '重新扫描爬虫目录', icon: '↻', run: async () => {
        await ZF.api.reloadCrawlers();
        await ZF.store.loadCrawlers(true);
        ZF.router.mount();
        ZF.ui.toast('爬虫目录已重新扫描', 'good');
      } },
    );

    ZF.store.items.slice(0, 300).forEach((it) => cmds.push({
      kind: '数据', title: it.title, sub: `${it.source} · ${it.keyword || ''}`,
      icon: it.source === 'bilibili' ? 'B' : '淘',
      run: () => { if (it.url) window.open(it.url, '_blank', 'noopener'); },
    }));

    return cmds;
  }

  const Palette = {
    open() {
      if (document.querySelector('.palette-backdrop')) return;
      const cmds = build();
      let filtered = cmds.slice(0, 50), active = 0;

      const input = U.el('input', { type: 'text', placeholder: '搜页面、爬虫、数据条目、动作…', spellcheck: 'false' });
      const list = U.el('div', { class: 'palette-list' });
      const backdrop = U.el('div', { class: 'palette-backdrop', onclick: (e) => { if (e.target === backdrop) close(); } });
      const box = U.el('div', { class: 'palette' }, [
        input, list,
        U.el('div', { class: 'palette-hint' }, [
          U.el('span', { text: '↑↓ 选择' }), U.el('span', { text: '↵ 执行' }),
          U.el('span', { text: 'esc 关闭' }),
          U.el('span', { style: { marginLeft: 'auto' }, text: `${cmds.length} 条索引` }),
        ]),
      ]);
      backdrop.appendChild(box);

      function render() {
        U.clear(list);
        if (!filtered.length) { list.appendChild(U.el('div', { class: 'empty', text: '没有匹配的条目' })); return; }
        filtered.slice(0, 50).forEach((c, i) => {
          list.appendChild(U.el('button', {
            class: 'palette-item', dataset: { active: String(i === active) },
            onclick: () => run(c),
            onmousemove: () => {
              if (active !== i) {
                active = i;
                U.$$('.palette-item', list).forEach((n, j) => { n.dataset.active = String(j === active); });
              }
            },
          }, [
            U.el('span', { style: { fontFamily: 'var(--font-mono)', width: '20px', textAlign: 'center', color: 'var(--fg-2)', flex: 'none' }, text: c.icon }),
            U.el('span', { style: { flex: '1', minWidth: '0' } }, [
              U.el('div', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, text: c.title }),
              c.sub ? U.el('div', { style: { fontSize: '11px', color: 'var(--fg-2)', fontFamily: 'var(--font-mono)' }, text: c.sub }) : null,
            ]),
            U.el('span', { class: 'k', text: c.kind }),
          ]));
        });
      }

      function run(c) { close(); setTimeout(() => c.run(), 10); }
      function close() { backdrop.remove(); document.removeEventListener('keydown', onKey); }

      function onKey(e) {
        if (e.key === 'Escape') return close();
        if (e.key === 'ArrowDown') {
          e.preventDefault(); active = Math.min(filtered.length - 1, active + 1); render();
          const n = U.$$('.palette-item', list)[active]; if (n) n.scrollIntoView({ block: 'nearest' });
        } else if (e.key === 'ArrowUp') {
          e.preventDefault(); active = Math.max(0, active - 1); render();
          const n = U.$$('.palette-item', list)[active]; if (n) n.scrollIntoView({ block: 'nearest' });
        } else if (e.key === 'Enter') {
          e.preventDefault(); if (filtered[active]) run(filtered[active]);
        }
      }

      input.addEventListener('input', U.debounce(() => {
        const q = input.value.trim().toLowerCase();
        active = 0;
        if (!q) filtered = cmds.slice(0, 50);
        else {
          const scored = [];
          cmds.forEach((c) => {
            const hay = (c.title + ' ' + (c.sub || '')).toLowerCase();
            const idx = hay.indexOf(q);
            if (idx >= 0) scored.push([idx + hay.length * 0.001, c]);
          });
          scored.sort((a, b) => a[0] - b[0]);
          filtered = scored.map((x) => x[1]);
        }
        render();
      }, 90));

      document.addEventListener('keydown', onKey);
      document.body.appendChild(backdrop);
      render();
      input.focus();
    },
  };

  ZF.palette = Palette;
})(window.ZF = window.ZF || {});
