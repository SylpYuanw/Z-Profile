/* ============================================================================
   视图 · SQL 实验室
   真执行 SQL（后端只读连接 + 前端出图）
   ========================================================================== */
(function (ZF) {
  'use strict';
  const U = ZF.util, UI = ZF.ui, Ch = ZF.charts, API = ZF.api, Store = ZF.store, S = ZF.stats;

  ZF.screens.push({
    id: 'sql',
    title: 'SQL 实验室',
    short: 'SQL',
    group: '数据',
    accent: 'var(--brand-3)',

    render(host) {
      const view = UI.view([]);
      host.appendChild(view);

      let sources = [], curSource = 'zforge', lastResult = null;

      const input = U.el('textarea', {
        rows: 8, spellcheck: 'false',
        placeholder: '写一条 SELECT 语句，Ctrl/⌘ + Enter 执行',
        style: { width: '100%', minHeight: '150px' },
      });
      input.value = Store.SQL_PRESETS[0].sql;

      const srcSelect = U.el('select', { style: { minWidth: '190px' } });
      const runBtn = U.el('button', { class: 'btn primary', text: '▶ 执行  ⌘↵' });
      const resultHost = U.el('div');
      const presetHost = U.el('div');

      /* ---------------- 顶部 ---------------- */
      view.appendChild(UI.section('SQL 实验室',
        '后端用只读连接执行，只允许 SELECT / WITH / EXPLAIN，结果最多返回 2000 行',
        U.frag([
          UI.card({
            body: U.frag([
              U.el('div', { style: { display: 'flex', gap: '10px', alignItems: 'center', marginBottom: '12px', flexWrap: 'wrap' } }, [
                U.el('span', { class: 'task-meta', text: '数据源' }),
                srcSelect,
                U.el('span', { style: { flex: '1' } }),
                runBtn,
                U.el('button', {
                  class: 'btn', text: '清空',
                  onclick: () => { input.value = ''; input.focus(); },
                }),
              ]),
              input,
            ]),
          }),
          U.el('div', { style: { height: '16px' } }),
          presetHost,
          U.el('div', { style: { height: '16px' } }),
          resultHost,
        ])));
      resultHost.appendChild(UI.card({ body: UI.empty('⌗', '还没有执行', '点上面的预设，或自己写一条 SQL') }));

      /* ---------------- 数据源 ---------------- */
      (async () => {
        sources = await Store.loadSources(true);
        U.clear(srcSelect);
        sources.filter((s) => s.exists).forEach((s) => {
          srcSelect.appendChild(U.el('option', { value: s.id, text: s.name, selected: s.id === curSource }));
        });
        srcSelect.addEventListener('change', () => { curSource = srcSelect.value; });
      })();

      /* ---------------- 预设 ---------------- */
      const presetCard = UI.card({
        title: '查询预设',
        sub: '点一下填进编辑器，再点执行',
        body: U.el('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '8px', marginTop: '8px' } },
          Store.SQL_PRESETS.map((p) => U.el('button', {
            class: 'btn',
            text: p.label,
            onclick: () => {
              input.value = p.sql;
              curSource = p.source || 'zforge';
              srcSelect.value = curSource;
              run();
            },
          }))),
      });
      presetHost.appendChild(presetCard);

      /* ---------------- 执行 ---------------- */
      async function run() {
        const sql = input.value.trim();
        if (!sql) { UI.toast('SQL 不能为空', 'bad'); return; }
        Ch.destroyAll(resultHost);
        U.clear(resultHost);
        resultHost.appendChild(UI.card({ body: U.el('div', { style: { padding: '20px' } }, [UI.skeleton(80)]) }));

        try {
          const r = await API.runSQL(curSource, sql, 2000);
          lastResult = r;
          renderResult(sql, r);
        } catch (e) {
          U.clear(resultHost);
          resultHost.appendChild(UI.card({
            title: '执行失败',
            sub: String(e.message || e),
            body: e.detail ? U.el('pre', { class: 'code', text: String(e.detail), style: { marginTop: '12px' } }) : null,
          }));
        }
      }

      function renderResult(sql, r) {
        U.clear(resultHost);
        const viewMode = { chart: 'table' };

        const modeHost = U.el('div');
        const bodyHost = U.el('div');

        function draw() {
          Ch.destroyAll(bodyHost);
          U.clear(bodyHost);
          if (!r.rows.length) { bodyHost.appendChild(UI.empty('∅', '查询没有返回任何行')); return; }

          if (viewMode.chart === 'table') {
            const cols = r.columns.map((c, ci) => ({
              key: c, label: c,
              num: r.rows.some((row) => typeof row[ci] === 'number'),
              render: (row) => {
                const v = row[r.columns.indexOf(c)];
                if (v === null || v === undefined) return '—';
                if (typeof v === 'number') return Number.isInteger(v) ? U.int(v) : v.toLocaleString('en-US', { maximumFractionDigits: 2 });
                let str = String(v);
                if (/^https?:\/\//.test(str)) return U.el('a', { class: 'cell-title', href: str, target: '_blank', rel: 'noopener', text: str });
                // 处理不可见字符, 避免单元格看起来是空的
                if (!str.trim()) str = '(空)';
                return str.length > 80 ? U.el('span', { class: 'cell-title', title: str, text: str }) : str;
              },
              sortValue: (row) => row[r.columns.indexOf(c)],
            }));
            bodyHost.appendChild(UI.table({ columns: cols, rows: r.rows, pageSize: 50 }));
            return;
          }

          // 图表模式
          const numericIdx = r.columns.map((c, i) => ({ c, i }))
            .filter((x) => r.rows.some((row) => typeof row[x.i] === 'number'));
          const labelIdx = r.columns.findIndex((c, i) => r.rows.some((row) => typeof row[i] === 'string'));

          if (!numericIdx.length) { bodyHost.appendChild(UI.empty('∅', '结果里没有数值列', '图表模式需要至少一个数值列')); return; }
          const yIdx = numericIdx[numericIdx.length - 1].i;
          const labels = r.rows.map((row, i) => {
            const s = labelIdx >= 0 ? String(row[labelIdx]) : `#${i + 1}`;
            return s.length > 26 ? s.slice(0, 25) + '…' : s;
          });
          const vals = r.rows.map((row) => Number(row[yIdx]) || 0);

          const box = Ch.box(360);
          bodyHost.appendChild(box.box);
          requestAnimationFrame(() => {
            if (viewMode.chart === 'pie') {
              const agg = new Map();
              labels.forEach((l, i) => agg.set(l, (agg.get(l) || 0) + vals[i]));
              Ch.pie(box.canvas, [...agg.keys()].slice(0, 12), [...agg.values()].slice(0, 12), { fmt: (v) => U.smart(v) });
            } else if (viewMode.chart === 'scatter' && numericIdx.length >= 2) {
              const xIdx = numericIdx[0].i;
              const xs = r.rows.map((row) => Number(row[xIdx]) || 0);
              const ys = r.rows.map((row) => Number(row[yIdx]) || 0);
              const fit = S.linearFit(xs, ys);
              Ch.scatter(box.canvas, r.rows.map((row, i) => ({ x: xs[i], y: ys[i], r: 5 })),
                {
                  xLabel: r.columns[xIdx], yLabel: r.columns[yIdx],
                  xFmt: U.smart, yFmt: U.smart,
                  trend: [Math.min(...xs), Math.max(...xs)].map((x) => ({ x, y: fit.a + fit.b * x })),
                });
            } else if (viewMode.chart === 'line') {
              Ch.line(box.canvas, labels, [{ label: r.columns[yIdx], data: vals }], { fmt: U.smart });
            } else {
              Ch.hbar(box.canvas, labels.slice().reverse(), vals.slice().reverse(),
                { label: r.columns[yIdx], fmt: U.smart });
            }
          });
        }

        const modeBtns = ['table', 'hbar', 'pie', 'scatter', 'line'].map((m) => {
          const label = { table: '表格', hbar: '条形', pie: '饼图', scatter: '散点', line: '折线' }[m];
          const b = U.el('button', {
            class: 'btn', text: label, style: { padding: '3px 10px' },
            onclick: () => {
              viewMode.chart = m;
              U.$$('button', modeHost).forEach((x) => x.style.borderColor = 'var(--line-1)');
              b.style.borderColor = 'var(--accent)';
              draw();
            },
          });
          if (m === 'table') b.style.borderColor = 'var(--accent)';
          return b;
        });

        U.append(modeHost, [
          U.el('div', { style: { display: 'flex', gap: '6px', alignItems: 'center', flexWrap: 'wrap', marginBottom: '12px' } }, [
            U.el('span', { class: 'task-meta', text: '视图' }),
            ...modeBtns,
            U.el('span', { style: { flex: '1' } }),
            U.el('span', { class: 'task-meta', text: `${r.row_count} 行 · ${r.elapsed_ms} ms` }),
            U.el('button', {
              class: 'btn', text: '导出 CSV',
              onclick: () => UI.download('query.csv', UI.toCSV(r.rows, r.columns.map((c, i) => ({ key: i, label: c }))), 'text/csv;charset=utf-8'),
            }),
            U.el('button', {
              class: 'btn', text: '导出 Excel',
              onclick: () => {
                if (typeof XLSX === 'undefined') { UI.toast('XLSX 未加载', 'bad'); return; }
                const ws = XLSX.utils.aoa_to_sheet([r.columns].concat(r.rows));
                const wb = XLSX.utils.book_new();
                XLSX.utils.book_append_sheet(wb, ws, 'query');
                XLSX.writeFile(wb, 'query.xlsx');
              },
            }),
          ]),
        ]);

        resultHost.appendChild(UI.card({
          title: '结果',
          badge: U.el('span', { style: { marginLeft: 'auto' } }),
          body: U.frag([modeHost, bodyHost, U.el('pre', {
            class: 'code',
            style: { marginTop: '14px', maxHeight: '140px', fontSize: '11px' },
            text: sql,
          })]),
        }));
        draw();
      }

      runBtn.addEventListener('click', run);
      input.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); run(); }
      });

      return null;
    },
  });
})(window.ZF = window.ZF || {});
