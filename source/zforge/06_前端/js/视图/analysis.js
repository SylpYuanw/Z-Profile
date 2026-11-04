/* ============================================================================
   视图 · 数据分析
   用真实采集数据出 10 种专业图表
   ========================================================================== */
(function (ZF) {
  'use strict';
  const U = ZF.util, UI = ZF.ui, Ch = ZF.charts, Store = ZF.store, API = ZF.api, S = ZF.stats, R = ZF.router;

  ZF.screens.push({
    id: 'analysis',
    title: '数据分析',
    short: '分析',
    group: '数据',
    accent: 'var(--brand-2)',

    render(host, params) {
      const view = UI.view([]);
      host.appendChild(view);
      let destroyed = false, selectedData = null;
      const body = U.el('div');
      view.appendChild(UI.section('数据分析', '先选择平台和数据集，再看该数据集的指标与明细', body));
      async function load() {
        try {
          const projects = (await API.projects()).projects || [];
          if (destroyed) return;
          const pid = params.project || params.tab;
          if (!pid) { body.appendChild(ZF.projects.projectCards(projects, 'analysis')); return; }
          const project = projects.find((p) => p.id === pid);
          if (!project) { body.appendChild(UI.empty('∅', '项目不存在')); return; }
          if (!params.dataset) { body.appendChild(UI.section(project.name, '选择一个数据集，当前与历史分别查看', ZF.projects.datasetCards(project, 'analysis'))); return; }
          selectedData = await API.projectDataset(project.id, params.dataset);
          if (destroyed) return;
          body.appendChild(ZF.projects.context(selectedData, 'analysis'));
          body.appendChild(U.el('div', { style: { height: '18px' } }));
          const mode = selectedData.dataset.analysis;
          if (mode === 'bilibili') ZF.videos.render(body, selectedData.items);
          else if (mode === 'taobao') drawTaobao();
          else body.appendChild(UI.empty('∅', '此数据集提供明细浏览', '使用上方「查看原始明细」查看记录'));
        } catch (e) { if (!destroyed) body.appendChild(UI.card({ title: '分析数据读取失败', sub: e.message })); }
      }

      /* ==================== 淘宝 ==================== */
      /* 一图一指标：顶部 KPI 概览固定，下方每次只渲染一张图，
         通过分段选择器切换。切换只重建图表面板，不重算统计。 */
      const TAOBAO_CHARTS = [
        { id: 'price', label: '价格分布', sub: '按对数分箱统计商品落在哪些价格区间' },
        { id: 'sales', label: '销量排行', sub: '付款人数 TOP15，看哪些商品卖得最多' },
        { id: 'scatter', label: '价格 × 销量', sub: '每个点是一个商品，拟合线看价格与销量的关系' },
        { id: 'category', label: '类目对比', sub: '按关键词分组，比较商品数量、均价与总付款人数' },
        { id: 'pareto', label: '销量集中度', sub: '帕累托曲线，看销量是否集中在少数商品' },
        { id: 'value', label: '性价比榜', sub: '综合分排序，销量与价格竞争力的加权' },
      ];

      function drawTaobao() {
        const d = Store.taobaoSummary(selectedData.items, selectedData.dataset.origin === 'history');
        if (!d) {
          body.appendChild(UI.card({ body: UI.empty('∅', '还没有淘宝数据', '可返回选择该项目的数据集') }));
          return;
        }

        // ---- KPI 概览（全局，不随图切换） ----
        body.appendChild(UI.grid(4, [
          UI.kpiAnim('商品总数', d.n, (v) => U.int(v), `${d.byKeyword.length} 个关键词类目`, '#ff5000'),
          UI.kpi('均价', U.money(d.avgPrice, 0), `中位数 ${U.money(d.medianPrice, 0)}`),
          UI.kpiAnim('总付款人数', d.totalSales, (v) => U.smart(v), '所有商品之和'),
          UI.kpi('价格-销量相关', d.corrPriceSales.toFixed(3),
            d.corrPriceSales < -0.2 ? '本数据集中呈负相关' : d.corrPriceSales > 0.2 ? '本数据集中呈正相关' : '几乎无关',
            d.corrPriceSales < -0.2 ? 'var(--ok)' : 'var(--warn)'),
        ]));
        body.appendChild(U.el('div', { style: { height: '18px' } }));

        // ---- 图表切换条 ----
        const panel = U.el('div');
        const current = TAOBAO_CHARTS.find((c) => c.id === params.chart) || TAOBAO_CHARTS[0];
        body.appendChild(U.el('div', { class: 'seg-wrap' }, [
          UI.segmented(
            TAOBAO_CHARTS.map((c) => ({ value: c.id, label: c.label })),
            current.id,
            (id) => {
              params.chart = id;
              R.go('analysis', { ...params, chart: id });
            },
          ),
        ]));
        body.appendChild(U.el('div', { style: { height: '14px' } }));
        body.appendChild(panel);

        function renderChart(id) {
          U.clear(panel);
          const spec = TAOBAO_CHARTS.find((c) => c.id === id) || TAOBAO_CHARTS[0];

          if (spec.id === 'value') {
            panel.appendChild(UI.card({
              title: spec.label, sub: spec.sub,
              body: UI.table({
                pageSize: 15,
                rows: d.bestValue,
                columns: [
                  { key: 'title', label: '标题', width: '46%', render: (r) => U.el('a', { class: 'cell-title', href: r.url, target: '_blank', rel: 'noopener', text: r.title, title: r.title }) },
                  { key: 'score', label: '综合分', num: true, render: (r) => r.score.toFixed(1) },
                  { key: 'price', label: '价格', num: true, render: (r) => U.money(r.price, 1) },
                  { key: 'metric', label: '付款人数', num: true, render: (r) => U.smart(r.metric) },
                ],
              }),
            }));
            return;
          }

          if (spec.id === 'category') {
            // 类目通常只有几个，竖柱会显得空旷；用横向条形 + 独立卡片承载
            // "商品数" 与 "均价" 两个指标，避免单位不同的数值挤在同一根轴上。
            const boxN = Ch.box(220), boxP = Ch.box(220);
            panel.appendChild(UI.grid(2, [
              UI.card({ title: '各类目商品数', sub: '每个关键词采到多少个商品', body: boxN.box }),
              UI.card({ title: '各类目均价', sub: '柱越长说明该类目越贵', body: boxP.box }),
            ]));
            panel.appendChild(U.el('div', { style: { height: '14px' } }));
            panel.appendChild(UI.card({
              title: '类目明细',
              body: UI.table({
                pageSize: 0, rows: d.byKeyword,
                columns: [
                  { key: 'keyword', label: '关键词' },
                  { key: 'n', label: '商品数', num: true },
                  { key: 'avgPrice', label: '均价', num: true, render: (r) => U.money(r.avgPrice, 1) },
                  { key: 'medianPrice', label: '中位价', num: true, render: (r) => U.money(r.medianPrice, 1) },
                  { key: 'totalSales', label: '总付款人数', num: true, render: (r) => U.smart(r.totalSales) },
                  { key: 'avgSales', label: '平均付款', num: true, render: (r) => U.smart(r.avgSales) },
                ],
              }),
            }));
            requestAnimationFrame(() => {
              const kws = d.byKeyword.map((k) => k.keyword);
              Ch.hbar(boxN.canvas, kws, d.byKeyword.map((k) => k.n),
                { color: '#ff5000', label: '商品数', fmt: U.int });
              Ch.hbar(boxP.canvas, kws, d.byKeyword.map((k) => Math.round(k.avgPrice)),
                { color: '#ff8a4c', label: '均价', fmt: (v) => U.money(v, 0) });
            });
            return;
          }

          const box = Ch.box(spec.id === 'scatter' ? 400 : 380);
          const sub = spec.id === 'price'
              ? `${spec.sub} · 箱线 Q1=${U.money(d.box.q1, 0)} 中位=${U.money(d.box.q2, 0)} Q3=${U.money(d.box.q3, 0)}`
            : spec.id === 'scatter'
              ? `${spec.sub} · 斜率 ${d.fit.b.toFixed(2)}，R² = ${d.fit.r2.toFixed(3)}`
            : spec.id === 'pareto'
              ? `${spec.sub} · 基尼 ${d.gini.toFixed(3)}，前 20% 商品占 ${d.pareto20.toFixed(1)}%`
              : spec.sub;
          panel.appendChild(UI.card({ title: spec.label, sub, body: box.box }));

          requestAnimationFrame(() => {
            if (spec.id === 'price') {
              Ch.hist(box.canvas, d.hist.labels, d.hist.counts, { color: '#ff5000' });
            } else if (spec.id === 'sales') {
              const top = d.rows.slice().sort((a, b) => b.metric - a.metric).slice(0, 15).reverse();
              Ch.hbar(box.canvas, top.map((r) => r.title.slice(0, 30)), top.map((r) => r.metric),
                { color: '#ff5000', label: '付款人数', fmt: U.smart });
            } else if (spec.id === 'scatter') {
              const pts = d.rows.map((r) => ({ x: Number(r.price) || 0, y: Number(r.metric) || 0, r: 5, color: '#ff5000' }));
              const xs = pts.map((p) => p.x);
              Ch.scatter(box.canvas, pts, {
                xLabel: '价格', yLabel: '付款人数', xFmt: (v) => U.money(v, 0), yFmt: U.smart,
                trend: [Math.min(...xs), Math.max(...xs)].map((x) => ({ x, y: d.fit.a + d.fit.b * x })),
              });
            } else if (spec.id === 'pareto') {
              const sorted = d.rows.slice().sort((a, b) => b.metric - a.metric).slice(0, 22);
              Ch.pareto(box.canvas, sorted.map((_, i) => `#${i + 1}`), sorted.map((r) => Number(r.metric) || 0),
                { barLabel: '付款人数', fmt: U.smart });
            }
          });
        }

        renderChart(current.id);
      }

      load();

      return {
        destroy() { destroyed = true; document.documentElement.style.setProperty('--accent', 'var(--brand-2)'); },
      };
    },
  });
})(window.ZF = window.ZF || {});
