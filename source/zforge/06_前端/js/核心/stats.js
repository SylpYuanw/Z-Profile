/* ============================================================================
   ZForge · 统计内核 (前端计算, 不调后端)
   与 05_Web服务/server/core/stats.py 口径保持一致。
   ========================================================================== */
(function (ZF) {
  'use strict';
  const U = ZF.util;
  const S = {};

  S.clean = (arr) => arr.filter((v) => v !== null && v !== undefined && !Number.isNaN(Number(v))).map(Number);
  S.sum = (a) => a.reduce((x, y) => x + y, 0);
  S.mean = (a) => (a.length ? S.sum(a) / a.length : 0);
  S.min = (a) => (a.length ? Math.min(...a) : 0);
  S.max = (a) => (a.length ? Math.max(...a) : 0);

  S.median = function (a) {
    if (!a.length) return 0;
    const s = a.slice().sort((x, y) => x - y), m = Math.floor(s.length / 2);
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  };

  /** q: 0~1 */
  S.quantile = function (a, q) {
    if (!a.length) return 0;
    const s = a.slice().sort((x, y) => x - y);
    const pos = (s.length - 1) * q, b = Math.floor(pos), r = pos - b;
    return s[b + 1] !== undefined ? s[b] + r * (s[b + 1] - s[b]) : s[b];
  };

  S.stdev = function (a, sample) {
    if (a.length < 2) return 0;
    const m = S.mean(a);
    const ss = a.reduce((acc, v) => acc + (v - m) ** 2, 0);
    return Math.sqrt(ss / (a.length - (sample === false ? 0 : 1)));
  };

  /** 变异系数: 标准差/均值, 衡量离散程度 (无量纲, 可跨量纲比较) */
  S.cv = function (a) {
    const m = S.mean(a);
    return m ? S.stdev(a) / m : 0;
  };

  /** 箱线统计 (含 1.5*IQR 离群点判定) */
  S.boxStats = function (values) {
    const s = S.clean(values).sort((a, b) => a - b);
    if (!s.length) return null;
    const q1 = S.quantile(s, 0.25), q2 = S.quantile(s, 0.5), q3 = S.quantile(s, 0.75);
    const iqr = q3 - q1, lo = q1 - 1.5 * iqr, hi = q3 + 1.5 * iqr;
    const inside = s.filter((v) => v >= lo && v <= hi);
    return {
      min: inside[0], max: inside[inside.length - 1], q1, q2, q3, iqr,
      mean: S.mean(s), total: s.length,
      outliers: s.filter((v) => v < lo || v > hi).length,
      outlierValues: s.filter((v) => v < lo || v > hi).slice(0, 20),
    };
  };

  /** 皮尔逊相关系数 */
  S.pearson = function (xs, ys) {
    const n = Math.min(xs.length, ys.length);
    if (n < 2) return 0;
    const mx = S.mean(xs.slice(0, n)), my = S.mean(ys.slice(0, n));
    let num = 0, dx = 0, dy = 0;
    for (let i = 0; i < n; i++) {
      const a = xs[i] - mx, b = ys[i] - my;
      num += a * b; dx += a * a; dy += b * b;
    }
    const den = Math.sqrt(dx * dy);
    return den === 0 ? 0 : num / den;
  };

  /** 线性回归 y = a + b·x */
  S.linearFit = function (xs, ys) {
    const n = Math.min(xs.length, ys.length);
    if (n < 2) return { a: 0, b: 0, r2: 0 };
    const mx = S.mean(xs.slice(0, n)), my = S.mean(ys.slice(0, n));
    let num = 0, den = 0;
    for (let i = 0; i < n; i++) { num += (xs[i] - mx) * (ys[i] - my); den += (xs[i] - mx) ** 2; }
    const b = den === 0 ? 0 : num / den, a = my - b * mx;
    let ssTot = 0, ssRes = 0;
    for (let i = 0; i < n; i++) {
      ssTot += (ys[i] - my) ** 2;
      ssRes += (ys[i] - (a + b * xs[i])) ** 2;
    }
    return { a, b, r2: ssTot === 0 ? 0 : 1 - ssRes / ssTot };
  };

  /** 基尼系数: 0=完全平均, 1=完全集中。衡量流量/销量是否被少数吃掉 */
  S.gini = function (values) {
    const a = S.clean(values).filter((v) => v >= 0).sort((x, y) => x - y);
    const n = a.length, tot = S.sum(a);
    if (!n || !tot) return 0;
    let cum = 0;
    a.forEach((v, i) => { cum += v * (i + 1); });
    return (2 * cum) / (n * tot) - (n + 1) / n;
  };

  /** 帕累托: 前 p 比例的项目贡献了多少占比 */
  S.pareto = function (values, p) {
    const a = S.clean(values).sort((x, y) => y - x);
    const tot = S.sum(a);
    if (!a.length || !tot) return 0;
    const k = Math.max(1, Math.round(a.length * (p || 0.2)));
    return (S.sum(a.slice(0, k)) / tot) * 100;
  };

  /** 头部集中度: 前 n 名占比 */
  S.topShare = function (values, n) {
    const a = S.clean(values).sort((x, y) => y - x);
    const tot = S.sum(a);
    return tot ? (S.sum(a.slice(0, n)) / tot) * 100 : 0;
  };

  /** 直方图分箱 */
  S.histogram = function (values, bins) {
    const a = S.clean(values);
    if (!a.length) return { labels: [], counts: [], lo: 0, hi: 0, step: 1 };
    const lo = S.min(a), hi = S.max(a);
    const n = bins || U.clamp(Math.round(Math.sqrt(a.length) * 1.6), 6, 26);
    const step = (hi - lo) / n || 1;
    const counts = new Array(n).fill(0);
    a.forEach((v) => {
      let i = Math.floor((v - lo) / step);
      if (i >= n) i = n - 1;
      if (i < 0) i = 0;
      counts[i]++;
    });
    const f = (x) => (Math.abs(x) >= 1000 ? Math.round(x).toLocaleString('en-US') : Math.round(x * 10) / 10);
    return { labels: counts.map((_, i) => `${f(lo + i * step)}~${f(lo + (i + 1) * step)}`), counts, lo, hi, step };
  };

  /** 对数分箱 (价格/播放量这种长尾分布用对数分箱更有信息量) */
  S.logHistogram = function (values, bins) {
    const a = S.clean(values).filter((v) => v > 0);
    if (!a.length) return { labels: [], counts: [] };
    const logs = a.map(Math.log10);
    const h = S.histogram(logs, bins || 10);
    return {
      labels: h.labels.map((l, i) => {
        const lo = Math.pow(10, h.lo + i * h.step), hi = Math.pow(10, h.lo + (i + 1) * h.step);
        const f = (x) => (x >= 1e4 ? (x / 1e4).toFixed(0) + '万' : x >= 1000 ? (x / 1000).toFixed(1) + 'k' : Math.round(x));
        return `${f(lo)}~${f(hi)}`;
      }),
      counts: h.counts,
    };
  };

  /** 百分位排名: 某个值在集合里处于什么位置 */
  S.percentileRank = function (values, v) {
    const a = S.clean(values);
    if (!a.length) return 0;
    return (a.filter((x) => x <= v).length / a.length) * 100;
  };

  /** min-max 归一化到 0~100 */
  S.normalize100 = function (values, v) {
    const a = S.clean(values);
    const lo = S.min(a), hi = S.max(a);
    return hi === lo ? 50 : ((v - lo) / (hi - lo)) * 100;
  };

  /** 综合评分 (加权多指标) */
  S.composite = function (rows, specs) {
    // specs: [{key, weight, invert}]  invert=true 表示越小越好
    return rows.map((r) => {
      let score = 0;
      specs.forEach((sp) => {
        const vals = rows.map((x) => Number(x[sp.key]) || 0);
        let p = S.normalize100(vals, Number(r[sp.key]) || 0);
        if (sp.invert) p = 100 - p;
        score += p * sp.weight;
      });
      return Object.assign({}, r, { score: Math.round(score * 10) / 10 });
    });
  };

  /** 按列分组聚合 */
  S.groupBy = function (rows, keyFn) {
    const m = new Map();
    rows.forEach((r) => {
      const k = keyFn(r);
      if (!m.has(k)) m.set(k, []);
      m.get(k).push(r);
    });
    return m;
  };

  /** 简单移动平均 (给时间序列做平滑) */
  S.movingAvg = function (values, window) {
    const w = window || 3, out = [];
    for (let i = 0; i < values.length; i++) {
      const s = Math.max(0, i - w + 1);
      const slice = values.slice(s, i + 1);
      out.push(S.mean(slice));
    }
    return out;
  };

  ZF.stats = S;
})(window.ZF = window.ZF || {});
