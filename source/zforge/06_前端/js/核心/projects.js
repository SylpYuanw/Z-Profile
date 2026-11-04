/* 每个项目独立选择数据集，不自动合并当前库与历史库。 */
(function (ZF) {
  'use strict';
  const U = ZF.util, UI = ZF.ui, R = ZF.router;
  const P = {};
  P.projectCards = (projects, screen) => U.el('div', { class: 'project-grid' }, projects.map((p) => UI.card({
    title: p.name,
    sub: '该平台的采集结果与历史记录',
    body: U.frag([
      U.el('p', { class: 'project-description', text: p.datasets.length ?
        (p.crawler_id ? `当前采集 ${U.int((p.datasets.find((d) => d.origin === 'current') || {}).rows || 0)} 条。` : '') +
        `另有 ${p.datasets.filter((d) => d.origin === 'history').length} 个历史数据集，进入后分别选择。` : '尚未连接数据集' }),
      U.el('div', { class: 'pool-actions' }, [
        U.el('button', { class: 'btn primary', text: screen === 'analysis' ? '选择数据进行分析' : '查看该项目数据', onclick: () => R.go(screen || 'data', { project: p.id }) }),
        p.crawler_id ? U.el('button', { class: 'btn', text: '配置采集任务', onclick: () => R.go('crawler', { id: p.crawler_id }) }) : null,
      ]),
    ]),
  })));
  P.datasetCards = (project, screen) => U.frag([
    U.el('div', { class: 'pool-actions', style: { marginBottom: '18px' } }, [U.el('button', { class: 'btn', text: '← 返回项目选择', onclick: () => R.go(screen) })]),
    U.el('div', { class: 'project-grid' }, project.datasets.map((d) => UI.card({
      title: d.name, sub: `${d.origin === 'current' ? '当前采集' : '历史记录'} · ${d.database} · ${d.table}`,
      body: U.frag([U.el('p', { class: 'project-count', text: `${U.int(d.rows)} 条记录` }),
        U.el('button', { class: 'btn primary', text: screen === 'analysis' && d.analysis !== 'table' ? '查看分析' : '查看明细',
          onclick: () => R.go(screen === 'analysis' && d.analysis === 'table' ? 'data' : screen, { project: project.id, dataset: d.id }) }),
      ]),
    }))),
  ]);
  P.context = (data, screen) => UI.card({ title: `${data.project.name} / ${data.dataset.name}`,
    sub: `${data.dataset.database} · ${data.dataset.table} · ${data.dataset.origin === 'current' ? '当前采集结果' : '历史数据'} · ${U.int(data.total)} 条记录`,
    body: U.frag([
      U.el('div', { class: 'pool-actions' }, [
        U.el('button', { class: 'btn', text: '← 切换本项目数据集', onclick: () => R.go(screen, { project: data.project.id }) }),
        U.el('button', { class: 'btn', text: screen === 'data' ? '查看分析' : '查看原始明细', disabled: screen === 'data' && data.dataset.analysis === 'table',
          onclick: () => R.go(screen === 'data' ? 'analysis' : 'data', { project: data.project.id, dataset: data.dataset.id }) }),
      ]),
      data.truncated ? U.el('p', { class: 'pool-error', text: `仅分析本数据集前 ${U.int(data.limit)} 条，未代表全部 ${U.int(data.total)} 条。` }) : null,
    ]),
  });
  ZF.projects = P;
})(window.ZF = window.ZF || {});
