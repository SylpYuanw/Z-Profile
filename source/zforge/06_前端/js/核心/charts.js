/* ============================================================================
   ZForge · 图表层
   统一 Chart.js 观感 + 10 种专业图表预设 + 切页自动销毁
   ========================================================================== */
(function (ZF) {
  'use strict';
  const U = ZF.util;
  const live = new Set();

  const css = (n, f) => getComputedStyle(document.documentElement).getPropertyValue(n).trim() || f;

  function theme() {
    return {
      text: css('--fg-1', '#9aa6b8'), strong: css('--fg-0', '#e8edf5'),
      grid: css('--line-0', 'rgba(255,255,255,.06)'),
      accent: css('--accent', '#4f8cff'), brand: css('--brand', '#4f8cff'),
      b2: css('--brand-2', '#22d3ee'), b3: css('--brand-3', '#a855f7'),
      ok: css('--ok', '#34d399'), warn: css('--warn', '#fbbf24'), danger: css('--danger', '#f87171'),
    };
  }

  const PALETTE = ['#4f8cff', '#22d3ee', '#a855f7', '#34d399', '#fbbf24',
    '#f87171', '#f472b6', '#60a5fa', '#fb923c', '#4ade80', '#818cf8', '#2dd4bf'];

  const Ch = { palette: PALETTE, theme };

  Ch.alpha = function (hex, a) {
    const h = String(hex).replace('#', '');
    const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  };

  const FONT = 'Inter, "Microsoft YaHei", sans-serif';
  const MONO = 'JetBrains Mono, Consolas, monospace';

  function base(extra) {
    const t = theme();
    return Object.assign({
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 480, easing: 'easeOutQuart' },
      interaction: { mode: 'nearest', intersect: false },
      plugins: {
        legend: {
          labels: {
            color: t.text, boxWidth: 9, boxHeight: 9, usePointStyle: true,
            pointStyle: 'rectRounded', font: { size: 11, family: FONT }, padding: 14,
          },
        },
        tooltip: {
          backgroundColor: 'rgba(5,7,10,.96)', borderColor: t.grid, borderWidth: 1,
          titleColor: t.strong, bodyColor: t.text, padding: 10, cornerRadius: 8,
          titleFont: { size: 12, weight: '600', family: FONT },
          bodyFont: { size: 11.5, family: MONO },
        },
      },
      scales: {
        x: {
          grid: { color: t.grid, drawBorder: false },
          ticks: { color: t.text, font: { size: 10.5, family: FONT }, maxRotation: 0, autoSkipPadding: 10 },
          border: { color: t.grid },
        },
        y: {
          grid: { color: t.grid, drawBorder: false },
          ticks: { color: t.text, font: { size: 10.5, family: MONO } },
          border: { display: false }, beginAtZero: true,
        },
      },
    }, extra || {});
  }

  /** 建图并登记, 切页时统一销毁 */
  Ch.make = function (canvas, config) {
    canvas.style.width = '100%';
    canvas.style.height = '100%';
    const c = new Chart(canvas, config);
    live.add(c);
    return c;
  };

  Ch.destroyAll = function (root) {
    live.forEach((c) => {
      if (!root || (c.canvas && root.contains(c.canvas))) {
        try { c.destroy(); } catch (e) { /* noop */ }
        live.delete(c);
      }
    });
  };

  /** 统一创建一个带 .chart-box 的容器 + canvas */
  Ch.box = function (h, cls) {
    const canvas = U.el('canvas');
    const box = U.el('div', { class: 'chart-box ' + (cls || '') }, [canvas]);
    if (typeof h === 'number') box.style.height = h + 'px';
    return { box, canvas };
  };

  /* ======================================================================
     10 种图表预设
     ====================================================================== */

  /** ① 横向条形排行 */
  Ch.hbar = function (canvas, labels, values, o) {
    o = o || {};
    const t = theme(), color = o.color || t.accent;
    const max = Math.max(...values, 1);
    return Ch.make(canvas, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          label: o.label || '数值', data: values,
          backgroundColor: values.map((v) => Ch.alpha(color, 0.35 + 0.55 * (v / max))),
          borderColor: color, borderWidth: 1, borderRadius: 3, barPercentage: 0.8,
        }],
      },
      options: base({
        indexAxis: 'y',
        plugins: { legend: { display: false }, tooltip: Object.assign({}, base().plugins.tooltip, {
          callbacks: { title: (items) => items.length && o.fullLabels ? o.fullLabels[items[0].dataIndex] : (items[0] ? items[0].label : ''),
            label: (c) => `${o.label || '数值'}：${o.fmt ? o.fmt(c.parsed.x) : U.smart(c.parsed.x)}` },
        }) },
        scales: {
          x: {
            grid: { color: t.grid },
            ticks: { color: t.text, font: { size: 10, family: MONO }, callback: (v) => (o.fmt ? o.fmt(v) : U.smart(v)) },
          },
          y: { grid: { display: false }, ticks: { color: t.text, font: { size: 10.5, family: FONT }, autoSkip: false } },
        },
      }),
    });
  };

  /** ② 纵向分组柱状图 */
  Ch.groupBar = function (canvas, labels, series, o) {
    o = o || {};
    const t = theme();
    return Ch.make(canvas, {
      type: 'bar',
      data: {
        labels,
        datasets: series.map((s, i) => {
          const c = s.color || PALETTE[i % PALETTE.length];
          return {
            label: s.label, data: s.data,
            backgroundColor: Ch.alpha(c, 0.7), borderColor: c, borderWidth: 1,
            borderRadius: 3, barPercentage: 0.82,
          };
        }),
      },
      options: base({
        plugins: {
          legend: { display: series.length > 1, labels: base().plugins.legend.labels },
          tooltip: base().plugins.tooltip,
        },
        scales: {
          x: { grid: { display: false }, ticks: { color: t.text, font: { size: 10.5, family: FONT } } },
          y: {
            grid: { color: t.grid },
            ticks: { color: t.text, font: { size: 10, family: MONO }, callback: (v) => (o.fmt ? o.fmt(v) : U.smart(v)) },
            beginAtZero: true,
          },
        },
      }),
    });
  };

  /** ③ 堆叠柱状图 */
  Ch.stackBar = function (canvas, labels, series, o) {
    o = o || {};
    const t = theme();
    return Ch.make(canvas, {
      type: 'bar',
      data: {
        labels,
        datasets: series.map((s, i) => {
          const c = s.color || PALETTE[i % PALETTE.length];
          return {
            label: s.label, data: s.data,
            backgroundColor: Ch.alpha(c, 0.72), borderColor: c, borderWidth: 1,
            borderRadius: 2, barPercentage: 0.72,
          };
        }),
      },
      options: base({
        plugins: {
          legend: { display: true, labels: base().plugins.legend.labels },
          tooltip: Object.assign({}, base().plugins.tooltip, {
            callbacks: {
              label: (c) => ` ${c.dataset.label}: ${o.fmt ? o.fmt(c.parsed.y) : U.smart(c.parsed.y)}`,
            },
          }),
        },
        scales: {
          x: { stacked: true, grid: { display: false }, ticks: { color: t.text, font: { size: 10.5, family: FONT } } },
          y: {
            stacked: true, grid: { color: t.grid },
            ticks: { color: t.text, font: { size: 10, family: MONO }, callback: (v) => (o.fmt ? o.fmt(v) : U.smart(v)) },
          },
        },
      }),
    });
  };

  /** ④ 折线 / 面积图 (可双轴) */
  Ch.line = function (canvas, labels, series, o) {
    o = o || {};
    const t = theme();
    const dual = series.some((s) => s.axis === 'y1');
    return Ch.make(canvas, {
      type: 'line',
      data: {
        labels,
        datasets: series.map((s, i) => {
          const c = s.color || PALETTE[i % PALETTE.length];
          return {
            label: s.label, data: s.data, borderColor: c,
            backgroundColor: s.fill === false ? undefined : Ch.alpha(c, 0.13),
            fill: s.fill !== false, tension: 0.34, borderWidth: 1.9,
            pointRadius: s.points === false ? 0 : 2.4, pointHoverRadius: 5,
            pointBackgroundColor: c, yAxisID: s.axis || 'y',
          };
        }),
      },
      options: base({
        plugins: {
          legend: { display: series.length > 1, labels: base().plugins.legend.labels },
          tooltip: Object.assign({}, base().plugins.tooltip, {
            callbacks: { label: (c) => ` ${c.dataset.label}: ${o.fmt ? o.fmt(c.parsed.y) : U.smart(c.parsed.y)}` },
          }),
        },
        scales: {
          x: { grid: { display: false }, ticks: { color: t.text, font: { size: 10.5, family: FONT }, maxTicksLimit: 14 } },
          y: {
            grid: { color: t.grid },
            ticks: { color: t.text, font: { size: 10, family: MONO }, callback: (v) => (o.fmt ? o.fmt(v) : U.smart(v)) },
            beginAtZero: true,
          },
          ...(dual ? {
            y1: {
              position: 'right', grid: { display: false },
              ticks: { color: t.text, font: { size: 10, family: MONO }, callback: (v) => U.smart(v) },
            },
          } : {}),
        },
      }),
    });
  };

  /** ⑤ 直方图 */
  Ch.hist = function (canvas, binLabels, counts, o) {
    o = o || {};
    const t = theme(), color = o.color || t.b2;
    return Ch.make(canvas, {
      type: 'bar',
      data: {
        labels: binLabels,
        datasets: [{
          label: o.label || '频数', data: counts,
          backgroundColor: counts.map((c, i) => Ch.alpha(color, 0.3 + 0.6 * (c / Math.max(...counts, 1)))),
          borderColor: color, borderWidth: 1, borderRadius: 3,
          barPercentage: 1, categoryPercentage: 0.95,
        }],
      },
      options: base({
        plugins: { legend: { display: false }, tooltip: base().plugins.tooltip },
        scales: {
          x: { grid: { display: false }, ticks: { color: t.text, font: { size: 9.5, family: MONO }, maxRotation: 45, autoSkipPadding: 4 } },
          y: { grid: { color: t.grid }, ticks: { color: t.text, font: { size: 10, family: MONO } }, beginAtZero: true },
        },
      }),
    });
  };

  /** ⑥ 散点图 + 趋势线 */
  Ch.scatter = function (canvas, points, o) {
    o = o || {};
    const t = theme();
    const datasets = [{
      label: o.label || '样本', data: points,
      backgroundColor: points.map((p) => Ch.alpha(p.color || t.accent, 0.5)),
      borderColor: points.map((p) => p.color || t.accent),
      pointRadius: points.map((p) => p.r || 4.5), pointHoverRadius: 7,
    }];
    if (o.trend) {
      datasets.push({
        type: 'line', label: o.trendLabel || '趋势线', data: o.trend,
        borderColor: t.warn, borderWidth: 1.6, borderDash: [6, 4], pointRadius: 0, fill: false,
      });
    }
    return Ch.make(canvas, {
      type: 'scatter',
      data: { datasets },
      options: base({
        plugins: {
          legend: { display: !!o.trend, labels: base().plugins.legend.labels },
          tooltip: Object.assign({}, base().plugins.tooltip, {
            callbacks: {
              label: (c) => c.dataset.label === (o.trendLabel || '趋势线') ? null
                : ` ${o.xLabel || 'X'}=${o.xFmt ? o.xFmt(c.parsed.x) : c.parsed.x} · ${o.yLabel || 'Y'}=${o.yFmt ? o.yFmt(c.parsed.y) : U.smart(c.parsed.y)}`,
            },
          }),
        },
        scales: {
          x: {
            grid: { color: t.grid },
            ticks: { color: t.text, font: { size: 10, family: MONO }, callback: (v) => (o.xFmt ? o.xFmt(v) : U.smart(v)) },
          },
          y: {
            grid: { color: t.grid },
            ticks: { color: t.text, font: { size: 10, family: MONO }, callback: (v) => (o.yFmt ? o.yFmt(v) : U.smart(v)) },
            beginAtZero: true,
          },
        },
      }),
    });
  };

  /** ⑦ 饼图 / 环形图 */
  Ch.pie = function (canvas, labels, values, o) {
    o = o || {};
    const colors = o.colors || labels.map((_, i) => PALETTE[i % PALETTE.length]);
    return Ch.make(canvas, {
      type: o.doughnut === false ? 'pie' : 'doughnut',
      data: {
        labels,
        datasets: [{
          data: values,
          backgroundColor: colors.map((c) => Ch.alpha(c, 0.75)),
          borderColor: 'transparent', borderWidth: 0, hoverOffset: 8,
        }],
      },
      options: base({
        cutout: o.doughnut === false ? 0 : (o.cutout || '58%'),
        plugins: {
          legend: Object.assign({}, base().plugins.legend, { position: o.legendPos || 'right' }),
          tooltip: Object.assign({}, base().plugins.tooltip, {
            callbacks: {
              label: (c) => {
                const tot = c.dataset.data.reduce((a, b) => a + b, 0);
                const p = tot ? ((c.parsed / tot) * 100).toFixed(1) : '0';
                return ` ${c.label}: ${o.fmt ? o.fmt(c.parsed) : U.smart(c.parsed)} (${p}%)`;
              },
            },
          }),
        },
        scales: {},
      }),
    });
  };

  /** ⑧ 雷达图 */
  Ch.radar = function (canvas, labels, datasets, o) {
    o = o || {};
    const t = theme();
    return Ch.make(canvas, {
      type: 'radar',
      data: {
        labels,
        datasets: datasets.map((d, i) => {
          const c = d.color || PALETTE[i % PALETTE.length];
          return {
            label: d.label, data: d.data, borderColor: c,
            backgroundColor: Ch.alpha(c, 0.18), borderWidth: 1.9,
            pointRadius: 3, pointBackgroundColor: c,
          };
        }),
      },
      options: base({
        scales: {
          r: {
            angleLines: { color: t.grid }, grid: { color: t.grid },
            pointLabels: { color: t.text, font: { size: 11, family: FONT } },
            ticks: { color: t.text, backdropColor: 'transparent', font: { size: 9, family: MONO }, stepSize: o.step },
            suggestedMin: 0, suggestedMax: o.max || 100,
          },
        },
      }),
    });
  };

  /** ⑨ 帕累托图 (柱 + 累计折线双轴) —— 头部集中度 */
  Ch.pareto = function (canvas, labels, values, o) {
    o = o || {};
    const t = theme();
    const total = values.reduce((a, b) => a + b, 0) || 1;
    let acc = 0;
    const cum = values.map((v) => { acc += v; return (acc / total) * 100; });
    return Ch.make(canvas, {
      type: 'bar',
      data: {
        labels,
        datasets: [
          {
            label: o.barLabel || '数量', data: values, order: 2,
            backgroundColor: Ch.alpha(t.accent, 0.62), borderColor: t.accent,
            borderWidth: 1, borderRadius: 3, yAxisID: 'y', barPercentage: 0.85,
          },
          {
            type: 'line', label: '累计占比', data: cum, order: 1,
            borderColor: t.warn, backgroundColor: 'transparent',
            borderWidth: 2, pointRadius: 2.5, pointBackgroundColor: t.warn,
            tension: 0.25, yAxisID: 'y1', fill: false,
          },
        ],
      },
      options: base({
        plugins: {
          legend: { display: true, labels: base().plugins.legend.labels },
          tooltip: Object.assign({}, base().plugins.tooltip, {
            callbacks: {
              label: (c) => c.datasetIndex === 1
                ? ` 累计: ${c.parsed.y.toFixed(1)}%`
                : ` ${c.dataset.label}: ${o.fmt ? o.fmt(c.parsed.y) : U.smart(c.parsed.y)}`,
            },
          }),
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: { color: t.text, font: { size: 9.5, family: FONT }, maxRotation: 45, autoSkipPadding: 6 },
          },
          y: {
            grid: { color: t.grid },
            ticks: { color: t.text, font: { size: 10, family: MONO }, callback: (v) => (o.fmt ? o.fmt(v) : U.smart(v)) },
            beginAtZero: true, position: 'left',
          },
          y1: {
            position: 'right', grid: { display: false }, min: 0, max: 100,
            ticks: { color: t.warn, font: { size: 10, family: MONO }, callback: (v) => v + '%' },
          },
        },
      }),
    });
  };

  /** ⑩ 热力图 (用矩阵方块手工画, Chart.js 没有原生热力图) */
  Ch.heatmap = function (host, rowLabels, colLabels, matrix, o) {
    o = o || {};
    const t = theme();
    const colors = o.colors || ['#0b0e14', '#1e3a8a', '#2563eb', '#22d3ee', '#34d399'];
    const flat = matrix.flat().filter((v) => v !== null && v !== undefined);
    const max = Math.max(...flat, 1);
    const min = Math.min(...flat, 0);

    const colorFor = (v) => {
      if (v === null || v === undefined) return 'transparent';
      const p = max === min ? 0.5 : (v - min) / (max - min);
      const idx = Math.min(colors.length - 2, Math.floor(p * (colors.length - 1)));
      return colors[idx + 1];
    };

    const table = U.el('table', { style: { borderCollapse: 'separate', borderSpacing: '2px', width: '100%' } });
    const thead = U.el('thead');
    const htr = U.el('tr');
    htr.appendChild(U.el('th', { style: { width: '90px' } }));
    colLabels.forEach((c) => {
      htr.appendChild(U.el('th', {
        style: {
          fontFamily: 'var(--font-mono)', fontSize: '10px', color: t.text,
          fontWeight: '400', textAlign: 'center', padding: '2px', whiteSpace: 'nowrap',
        },
        text: c,
      }));
    });
    thead.appendChild(htr);
    const tbody = U.el('tbody');
    rowLabels.forEach((r, ri) => {
      const tr = U.el('tr');
      tr.appendChild(U.el('td', {
        style: { fontFamily: 'var(--font-mono)', fontSize: '10.5px', color: t.text, paddingRight: '6px', whiteSpace: 'nowrap' },
        text: r,
      }));
      colLabels.forEach((_, ci) => {
        const v = matrix[ri][ci];
        tr.appendChild(U.el('td', {
          title: `${r} · ${colLabels[ci]}\n${v === null || v === undefined ? '无数据' : U.int(v)}`,
          style: {
            background: colorFor(v), height: '22px', borderRadius: '3px',
            textAlign: 'center', fontFamily: 'var(--font-mono)', fontSize: '9px',
            color: 'rgba(255,255,255,.82)', cursor: 'default',
          },
          text: v === null || v === undefined ? '' : U.smart(v),
        }));
      });
      tbody.appendChild(tr);
    });
    table.append(thead, tbody);
    host.appendChild(table);
    return table;
  };

  /** 箱线图 (Chart.js 无原生支持, 用 floating bar 实现) */
  Ch.boxplot = function (canvas, labels, stats, o) {
    o = o || {};
    const t = theme();
    // Chart.js 的 bar 支持 [min,max] 浮动条
    return Ch.make(canvas, {
      type: 'bar',
      data: {
        labels,
        datasets: [
          {
            label: '四分位区间 (Q1~Q3)',
            data: stats.map((s) => [s.q1, s.q3]),
            backgroundColor: stats.map(() => Ch.alpha(t.b2, 0.55)),
            borderColor: t.b2, borderWidth: 1, borderRadius: 2, barPercentage: 0.5,
          },
          {
            label: '中位数',
            data: stats.map((s) => [s.q2 - (s.q3 - s.q1) * 0.008, s.q2 + (s.q3 - s.q1) * 0.008]),
            backgroundColor: stats.map(() => t.warn),
            borderColor: t.warn, borderWidth: 0, barPercentage: 0.5,
          },
          {
            label: '全距 (Min~Max)',
            data: stats.map((s) => [s.min, s.max]),
            backgroundColor: stats.map(() => Ch.alpha(t.accent, 0.13)),
            borderColor: Ch.alpha(t.accent, 0.5), borderWidth: 1, borderRadius: 2,
            barPercentage: 0.16,
          },
        ],
      },
      options: base({
        plugins: {
          legend: { display: true, labels: base().plugins.legend.labels },
          tooltip: Object.assign({}, base().plugins.tooltip, {
            callbacks: {
              label: (c) => {
                const s = stats[c.dataIndex];
                if (c.datasetIndex === 1) return ` 中位数: ${o.fmt ? o.fmt(s.q2) : U.smart(s.q2)}`;
                if (c.datasetIndex === 0) return ` Q1=${U.smart(s.q1)}  Q3=${U.smart(s.q3)}`;
                return ` Min=${U.smart(s.min)}  Max=${U.smart(s.max)}  离群点 ${s.outliers} 个`;
              },
            },
          }),
        },
        scales: {
          x: { grid: { display: false }, ticks: { color: t.text, font: { size: 10.5, family: FONT } } },
          y: {
            grid: { color: t.grid },
            ticks: { color: t.text, font: { size: 10, family: MONO }, callback: (v) => (o.fmt ? o.fmt(v) : U.smart(v)) },
          },
        },
      }),
    });
  };

  ZF.charts = Ch;
})(window.ZF = window.ZF || {});
