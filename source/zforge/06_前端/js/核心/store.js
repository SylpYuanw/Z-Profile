/* ============================================================================
   ZForge · 数据仓
   把后端接口的数据缓存起来, 并提供派生统计。
   ========================================================================== */
(function (ZF) {
  'use strict';
  const U = ZF.util, S = ZF.stats, API = ZF.api;

  const Store = {
    crawlers: [],
    crawlerErrors: [],
    sources: [],
    items: [],
    itemsTotal: 0,
    dataStats: null,
    tasks: [],
    taskCounts: {},
    accounts: [],
    accountSummary: {},
    workflows: [],
    workflowRuns: [],
    system: null,
    loaded: {},
    taobaoHistory: null,
  };

  /* ---------------- 加载 ---------------- */
  Store.loadCrawlers = async function (force) {
    if (Store.loaded.crawlers && !force) return Store.crawlers;
    const r = await API.crawlers();
    Store.crawlers = r.crawlers || [];
    Store.crawlerErrors = r.errors || [];
    Store.loaded.crawlers = true;
    return Store.crawlers;
  };

  Store.loadSources = async function (force) {
    if (Store.loaded.sources && !force) return Store.sources;
    const r = await API.sources();
    Store.sources = r.sources || [];
    Store.loaded.sources = true;
    return Store.sources;
  };

  Store.loadItems = async function (q) {
    const r = await API.items(q);
    Store.items = r.rows || [];
    Store.itemsTotal = r.total || 0;
    Store.loaded.items = true;
    return r;
  };

  Store.loadDataStats = async function (force) {
    if (Store.loaded.dataStats && !force) return Store.dataStats;
    Store.dataStats = await API.dataStats();
    Store.loaded.dataStats = true;
    return Store.dataStats;
  };

  Store.loadTasks = async function (q) {
    const r = await API.tasks(q);
    Store.tasks = r.tasks || [];
    Store.taskCounts = r.counts || {};
    return r;
  };

  Store.loadAccounts = async function (site) {
    const r = await API.accounts(site);
    Store.accounts = r.accounts || [];
    Store.accountSummary = r.summary || {};
    return r;
  };

  Store.loadWorkflows = async function () {
    const r = await API.workflows();
    Store.workflows = r.workflows || [];
    Store.workflowRuns = r.runs || [];
    return r;
  };

  Store.loadSystem = async function () {
    Store.system = await API.systemInfo();
    return Store.system;
  };

  Store.crawler = (id) => Store.crawlers.find((c) => c.id === id) || null;

  Store.loadTaobaoHistory = async function () {
    if (Store.taobaoHistory !== null) return Store.taobaoHistory;
    const tables = (await API.tables('taobao_legacy')).tables || [];
    const goods = tables.filter((t) => !t.name.endsWith('_detail') &&
      ['title', 'price', 'pay_count'].every((key) => t.columns.some((c) => c.name === key)));
    const datasets = await Promise.all(goods.map(async (t) => {
      const rows = [];
      let offset = 0;
      while (true) {
        const result = await API.rows('taobao_legacy', { table: t.name, limit: 2000, offset });
        result.rows.forEach((values) => {
          const row = Object.fromEntries(result.columns.map((key, i) => [key, values[i]]));
          rows.push({ source: 'taobao', keyword: t.name, title: row.title, url: row.url,
            price: Number(row.price), metric: Number(row.pay_count), metric_name: '付款人数',
            extra_json: JSON.stringify(row), _legacy: true });
        });
        offset += result.rows.length;
        if (offset >= result.total || !result.rows.length) break;
      }
      return rows;
    }));
    Store.taobaoHistory = datasets.flat();
    return Store.taobaoHistory;
  };

  /* ======================================================================
     派生分析 —— B站
     ====================================================================== */
  Store.biliSummary = function () {
    const rows = Store.items.filter((r) => r.source === "bilibili");
    if (!rows.length) return null;

    const extra = rows.map((r) => {
      try { return JSON.parse(r.extra_json || "{}"); } catch (e) { return {}; }
    });
    const views = rows.map((r) => Number(r.metric) || 0);
    const withDetail = rows.filter((_, i) => extra[i].detail_ok === 1);
    const dExtra = extra.filter((e) => e.detail_ok === 1);
    const dViews = withDetail.map((r) => Number(r.metric) || 0);

    const likes = S.clean(dExtra.map((e) => e.like));
    const coins = S.clean(dExtra.map((e) => e.coin));
    const favs = S.clean(dExtra.map((e) => e.favorite));
    const replies = S.clean(dExtra.map((e) => e.reply));
    const shares = S.clean(dExtra.map((e) => e.share));
    const danmaku = S.clean(extra.map((e) => Number(e.danmaku) || 0));
    const durations = S.clean(extra.map((e) => Number(e.duration_sec) || 0));

    // 互动率 = (点赞+投币+收藏) / 播放量 * 100
    const interactionRates = dExtra.map((e, i) => {
      const v = dViews[i] || 0;
      const tot = (e.like || 0) + (e.coin || 0) + (e.favorite || 0);
      return v ? (tot / v) * 100 : 0;
    });

    return {
      rows,
      extra,
      n: rows.length,
      nDetail: withDetail.length,
      views,
      totalViews: S.sum(views),
      avgViews: S.mean(views),
      medianViews: S.median(views),
      box: S.boxStats(views),
      hist: S.logHistogram(views, 12),
      gini: S.gini(views),
      top5Share: S.topShare(views, 5),
      top10Share: S.topShare(views, 10),
      pareto20: S.pareto(views, 0.2),
      totals: {
        like: S.sum(likes), coin: S.sum(coins), favorite: S.sum(favs),
        reply: S.sum(replies), share: S.sum(shares), danmaku: S.sum(danmaku),
      },
      avgLikeRate: dExtra.length ? S.mean(dExtra.map((e) => (e.like_rate || 0) * 100)) : 0,
      avgTripleRate: dExtra.length ? S.mean(dExtra.map((e) => (e.triple_rate || 0) * 100)) : 0,
      interactionRates,
      // 播放量 vs 互动率的相关系数 (播放量取对数, 因为分布长尾)
      corrViewsInteraction: S.pearson(dViews.map((v) => Math.log10(v || 1)), interactionRates),
      // 时长与播放量的关系 (长视频一定更火吗?)
      corrDurationViews: S.pearson(durations, views.slice(0, durations.length)),
      avgDuration: S.mean(durations),
      totalDuration: S.sum(durations),
      topByViews: rows.slice().sort((a, b) => b.metric - a.metric).slice(0, 20),
      topByInteraction: withDetail.map((r, i) => {
        const e = dExtra[i];
        return Object.assign({}, r, {
          _ir: interactionRates[i],
          _like: e.like, _coin: e.coin, _fav: e.favorite,
          _triple_rate: (e.triple_rate || 0) * 100,
          _author: e.author,
        });
      }).sort((a, b) => b._ir - a._ir).slice(0, 20),
    };
  };

  /* ---------------- 淘宝 ---------------- */
  Store.taobaoSummary = function (selectedRows, selectedLegacy) {
    const fresh = Store.items.filter((r) => r.source === "taobao");
    const rows = selectedRows !== undefined ? selectedRows : fresh.length ? fresh : (Store.taobaoHistory || []);
    if (!rows.length) return null;

    const prices = S.clean(rows.map((r) => Number(r.price)));
    const sales = S.clean(rows.map((r) => Number(r.metric)));
    const groups = S.groupBy(rows, (r) => r.keyword || "未分类");

    const byKeyword = [];
    groups.forEach((list, kw) => {
      const p = S.clean(list.map((x) => Number(x.price)));
      const s = S.clean(list.map((x) => Number(x.metric)));
      byKeyword.push({
        keyword: kw,
        n: list.length,
        avgPrice: S.mean(p),
        medianPrice: S.median(p),
        minPrice: S.min(p),
        maxPrice: S.max(p),
        totalSales: S.sum(s),
        avgSales: S.mean(s),
        box: S.boxStats(p),
        corrPriceSales: S.pearson(p, s),
        topItem: list.slice().sort((a, b) => b.metric - a.metric)[0],
      });
    });
    byKeyword.sort((a, b) => b.n - a.n);

    return {
      rows, n: rows.length, prices, sales, legacy: selectedRows !== undefined ? !!selectedLegacy : !fresh.length,
      avgPrice: S.mean(prices), medianPrice: S.median(prices),
      totalSales: S.sum(sales),
      box: S.boxStats(prices),
      hist: S.logHistogram(prices, 14),
      // 价格 vs 销量: 期望是负相关 (越便宜卖越多)
      corrPriceSales: S.pearson(prices, sales),
      fit: S.linearFit(prices, sales),
      gini: S.gini(sales),
      top10Share: S.topShare(sales, 10),
      pareto20: S.pareto(sales, 0.2),
      byKeyword,
      topBySales: rows.slice().sort((a, b) => b.metric - a.metric).slice(0, 20),
      topByPrice: rows.slice().sort((a, b) => b.price - a.price).slice(0, 20),
      bestValue: S.composite(rows.slice(), [
        { key: "metric", weight: 0.6 },
        { key: "price", weight: 0.4, invert: true },
      ]).sort((a, b) => b.score - a.score).slice(0, 15),
    };
  };

  /* ---------------- SQL 实验室预设 ---------------- */
  Store.SQL_PRESETS = [
    {
      label: "各来源采集量",
      sql: "SELECT source, COUNT(*) AS 条数, ROUND(AVG(metric),0) AS 平均指标\nFROM items\nGROUP BY source\nORDER BY 条数 DESC",
      chart: "pie",
    },
    {
      label: "B站播放量 TOP10",
      sql: "SELECT title AS 标题, metric AS 播放量\nFROM items\nWHERE source='bilibili'\nORDER BY metric DESC\nLIMIT 10",
      chart: "hbar",
    },
    {
      label: "淘宝各类目价格分布",
      sql: "SELECT keyword AS 类目, COUNT(*) AS 数量,\n       ROUND(AVG(price),1) AS 均价,\n       ROUND(MIN(price),1) AS 最低价,\n       ROUND(MAX(price),1) AS 最高价\nFROM items\nWHERE source='taobao'\nGROUP BY keyword\nORDER BY 数量 DESC",
      chart: "table",
    },
    {
      label: "价格与销量关系",
      sql: "SELECT title AS 标题, price AS 价格, metric AS 付款人数\nFROM items\nWHERE source='taobao'\nORDER BY metric DESC\nLIMIT 60",
      chart: "scatter",
    },
    {
      label: "跨来源的采集时间线",
      sql: "SELECT substr(datetime(crawled_at,'unixepoch','localtime'),1,13) AS 小时,\n       source AS 来源, COUNT(*) AS 条数\nFROM items\nGROUP BY 小时, 来源\nORDER BY 小时 DESC\nLIMIT 40",
      chart: "line",
    },
    {
      label: "历史库: 淘宝显示器全部商品",
      source: "taobao_legacy",
      sql: 'SELECT title AS 标题, price AS 价格, pay_count AS 付款人数\nFROM 显示器\nORDER BY pay_count DESC',
      chart: "hbar",
    },
    {
      label: "历史库: B站PYTHON类目",
      source: "bili_legacy",
      sql: 'SELECT 标题, 播放量, 点赞, 投币, 收藏, 点赞率, 三连率\nFROM PYTHON\nORDER BY 播放量 DESC',
      chart: "table",
    },
  ];

  ZF.store = Store;
})(window.ZF = window.ZF || {});
