(function (ZF) {
  'use strict';
  const U = ZF.util, UI = ZF.ui, API = ZF.api;
  ZF.screens.push({
    id: 'data', title: '项目数据', short: '数据', group: '数据', accent: 'var(--brand-2)',
    render(host, params) {
      let destroyed = false;
      const body = U.el('div');
      host.appendChild(UI.view([UI.section('项目数据', '选择一个平台及数据集，再查看该数据集的表和记录', body)]));
      async function load() {
        try {
          const projects = (await API.projects()).projects || [];
          if (destroyed) return;
          if (!params.project) { body.appendChild(ZF.projects.projectCards(projects, 'data')); return; }
          const project = projects.find((p) => p.id === params.project);
          if (!project) { body.appendChild(UI.empty('∅', '项目不存在')); return; }
          if (!params.dataset) { body.appendChild(UI.section(project.name, '选择该项目的数据集', ZF.projects.datasetCards(project, 'data'))); return; }
          const data = await API.projectDataset(project.id, params.dataset);
          if (destroyed) return;
          body.appendChild(ZF.projects.context(data, 'data'));
          const columns = data.columns.map((name, index) => ({ key: name, label: name, get: (r) => r[index], sortValue: (r) => r[index] }));
          const table = U.el('div');
          table.appendChild(UI.table({ rows: data.rows, columns, pageSize: 30 }));
          const search = U.el('input', { type: 'search', placeholder: '筛选当前数据集已加载的记录', 'aria-label': '筛选数据集记录' });
          search.addEventListener('input', () => {
            const query = search.value.trim().toLocaleLowerCase();
            const rows = data.rows.filter((r) => !query || r.some((value) => String(value == null ? '' : value).toLocaleLowerCase().includes(query)));
            U.clear(table); table.appendChild(UI.table({ rows, columns, pageSize: 30 }));
          });
          body.appendChild(U.el('div', { class: 'pool-actions', style: { margin: '16px 0' } }, [
            U.el('button', { class: 'btn', text: '导出当前数据集 CSV', onclick: () => UI.download(`${project.name}-${data.dataset.name}.csv`, UI.toCSV(data.rows, columns)) }),
            U.el('button', { class: 'btn', text: '导出当前数据集 Excel', onclick: () => UI.toXLSX(data.rows, columns, project.name + '-' + data.dataset.name) }),
          ]));
          body.appendChild(UI.field('查找记录', search, '只在当前选定数据集的已加载记录中筛选，不搜索其他平台。'));
          body.appendChild(UI.card({ title: '数据明细', sub: `已加载 ${U.int(data.rows.length)} 条；点表头排序`,
            body: data.rows.length ? table : UI.empty('∅', '这个数据集暂时没有记录') }));
        } catch (e) { if (!destroyed) body.appendChild(UI.card({ title: '数据读取失败', sub: e.message })); }
      }
      load();
      return { destroy() { destroyed = true; } };
    },
  });
})(window.ZF = window.ZF || {});
