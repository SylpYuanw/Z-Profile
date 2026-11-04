(function (ZF) {
  'use strict';
  const U = ZF.util, UI = ZF.ui, API = ZF.api;
  ZF.screens.push({
    id: 'dashboard', title: '选择项目', short: '工作台', group: '总览', accent: 'var(--brand)',
    render(host) {
      let destroyed = false;
      const body = U.el('div');
      host.appendChild(UI.view([UI.section('选择要处理的项目', '先选择平台，再配置采集或查看该平台的数据。每个项目独立进入。', body)]));
      body.appendChild(UI.empty('…', '正在读取项目'));
      API.projects().then((data) => {
        if (destroyed) return;
        U.clear(body); body.appendChild(ZF.projects.projectCards(data.projects || [], 'data'));
      }).catch((e) => { if (!destroyed) { U.clear(body); body.appendChild(UI.card({ title: '项目读取失败', sub: e.message })); } });
      return { destroy() { destroyed = true; } };
    },
  });
})(window.ZF = window.ZF || {});
