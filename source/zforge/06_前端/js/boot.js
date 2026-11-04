/* ============================================================================
   ZForge · 启动引导
   ========================================================================== */
(function (ZF) {
  'use strict';
  const U = ZF.util, UI = ZF.ui, API = ZF.api, Store = ZF.store, R = ZF.router;

  const THEME_KEY = 'zf.theme', DENSITY_KEY = 'zf.density';

  /* ---------------- 全局导出 ---------------- */
  ZF.exportAll = function () {
    if (typeof XLSX === 'undefined') { UI.toast('XLSX 未加载', 'bad'); return; }
    const wb = XLSX.utils.book_new();
    const add = (name, rows, cols) => {
      if (!rows || !rows.length) return;
      const aoa = [cols.map((c) => c.label)].concat(rows.map((r) => cols.map((c) => (c.get ? c.get(r) : r[c.key]))));
      const ws = XLSX.utils.aoa_to_sheet(aoa);
      ws['!cols'] = cols.map((c) => ({ wch: c.width || 16 }));
      XLSX.utils.book_append_sheet(wb, ws, name.slice(0, 31));
    };

    const items = Store.items;
    add('采集总表', items, [
      { key: 'source', label: '来源', width: 12 },
      { key: 'keyword', label: '关键词', width: 14 },
      { key: 'title', label: '标题', width: 56 },
      { key: 'price', label: '价格', width: 10 },
      { key: 'metric', label: '主指标', width: 12 },
      { key: 'metric_name', label: '指标名', width: 12 },
      { key: 'crawled_at', label: '采集时间', width: 12, get: (r) => U.ts(r.crawled_at, true) },
      { key: 'url', label: '链接', width: 44 },
    ]);

    const bili = items.filter((r) => r.source === 'bilibili');
    if (bili.length) {
      add('B站视频', bili.map((r) => {
        let e = {}; try { e = JSON.parse(r.extra_json || '{}'); } catch (x) { /* noop */ }
        return Object.assign({}, r, {
          like: e.like, coin: e.coin, favorite: e.favorite, reply: e.reply,
          author: e.author, duration: e.duration_sec,
          like_rate: e.like_rate, triple_rate: e.triple_rate,
        });
      }), [
        { key: 'title', label: '标题', width: 56 },
        { key: 'metric', label: '播放量', width: 12 },
        { key: 'like', label: '点赞', width: 10 },
        { key: 'coin', label: '投币', width: 10 },
        { key: 'favorite', label: '收藏', width: 10 },
        { key: 'reply', label: '评论', width: 10 },
        { key: 'like_rate', label: '点赞率', width: 10 },
        { key: 'triple_rate', label: '三连率', width: 10 },
        { key: 'author', label: 'UP主', width: 18 },
        { key: 'url', label: '链接', width: 44 },
      ]);
    }

    const tb = items.filter((r) => r.source === 'taobao');
    if (tb.length) {
      add('淘宝商品', tb, [
        { key: 'title', label: '标题', width: 56 },
        { key: 'price', label: '价格', width: 10 },
        { key: 'metric', label: '付款人数', width: 12 },
        { key: 'keyword', label: '关键词', width: 14 },
        { key: 'url', label: '链接', width: 44 },
      ]);
    }

    if (!wb.SheetNames.length) { UI.toast('没有数据可导出', 'bad'); return; }
    XLSX.writeFile(wb, `ZForge-数据集-${new Date().toISOString().slice(0, 10)}.xlsx`);
    UI.toast(`已导出 ${wb.SheetNames.length} 个工作表`, 'good');
  };

  /* ---------------- 顶栏 ---------------- */
  function wire() {
    document.getElementById('btn-theme').addEventListener('click', () => {
      const cur = document.documentElement.dataset.theme;
      const next = cur === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      localStorage.setItem(THEME_KEY, next);
      // 图表颜色是从 CSS 变量读的, 必须重建
      R.mount();
    });
    document.getElementById('btn-density').addEventListener('click', () => {
      const cur = document.documentElement.dataset.density;
      const next = cur === 'comfy' ? 'compact' : 'comfy';
      document.documentElement.dataset.density = next;
      localStorage.setItem(DENSITY_KEY, next);
      UI.toast(next === 'compact' ? '已切换到紧凑模式' : '已切换到舒适模式');
    });
    document.getElementById('btn-search').addEventListener('click', () => ZF.palette.open());

    document.addEventListener('keydown', (e) => {
      const k = e.key.toLowerCase();
      const typing = /input|textarea|select/i.test(e.target.tagName);
      if ((e.ctrlKey || e.metaKey) && k === 'k') { e.preventDefault(); ZF.palette.open(); }
      else if (k === '/' && !typing) { e.preventDefault(); ZF.palette.open(); }
      else if (k === 'g' && !typing && !e.ctrlKey && !e.metaKey) {
        const map = { 1: 'dashboard', 2: 'crawler', 3: 'taskflow', 4: 'data', 5: 'sql', 6: 'analysis', 7: 'accounts', 8: 'logs', 9: 'system' };
        const onNext = (ev) => {
          const t = map[ev.key];
          if (t) {
            ev.preventDefault();
            R.go(t);
          }
          document.removeEventListener('keydown', onNext, true);
        };
        document.addEventListener('keydown', onNext, true);
        setTimeout(() => document.removeEventListener('keydown', onNext, true), 1500);
      }
    });
  }

  /* ---------------- 状态指示 ---------------- */
  ZF.setStatus = function (text, ok) {
    const t = document.getElementById('status-text');
    const d = document.getElementById('status-dot');
    if (t) t.textContent = text;
    if (d) d.classList.toggle('bad', ok === false);
  };

  /* ---------------- 启动 ---------------- */
  async function boot() {
    document.documentElement.dataset.theme = localStorage.getItem(THEME_KEY) || 'dark';
    document.documentElement.dataset.density = localStorage.getItem(DENSITY_KEY) || 'comfy';
    wire();

    const host = document.getElementById('view-host');
    host.appendChild(UI.view([
      U.el('div', { class: 'grid g-4' }, [0, 1, 2, 3].map(() => UI.skeleton(96))),
      U.el('div', { style: { height: '16px' } }),
      UI.skeleton(300),
    ]));

    // 探测后端
    const online = await API.check();

    if (!online) {
      ZF.setStatus('后端未连接', false);
      U.clear(host);
      host.appendChild(UI.view([UI.card({
        title: '后端未连接',
        sub: '页面能打开，但拿不到数据。请确认服务在运行：',
        body: U.el('pre', {
          class: 'code', style: { marginTop: '14px' },
          text: 'cd <项目目录>\npython 05_Web服务/run.py',
        }),
      })]));
      return;
    }

    // 首次数据加载
    try {
      await Promise.all([
        Store.loadCrawlers(true),
        Store.loadItems({ limit: 2000 }),
        Store.loadDataStats(true),
        Store.loadAccounts(),
        Store.loadTasks({ limit: 60 }),
      ]);
    } catch (e) {
      UI.toast('数据加载失败: ' + (e.message || e), 'bad');
    }

    const h = API.health || {};
    ZF.setStatus(`Python ${h.python || '?'} · ${(h.crawlers || 0)} 个爬虫`, true);

    const setTxt = (id, v) => { const n = document.getElementById(id); if (n) n.textContent = v; };
    setTxt('foot-backend', '已连接');
    setTxt('foot-crawlers', String(Store.crawlers.length));
    setTxt('foot-items', U.int(Store.itemsTotal));

    // 注册屏幕 (视图文件已在 index.html 里按顺序加载)
    ZF.screens.forEach((s) => R.define(s));
    R.buildNav();
    R.start();

    // 定时刷新任务状态 (侧栏徽标 + 概览)
    setInterval(async () => {
      try {
        await Store.loadTasks({ limit: 20 });
        const badge = Store.taskCounts.active || 0;
        const nav = U.$('#nav .nav-item[data-id="taskflow"] .nav-badge');
        if (badge > 0) {
          if (nav) { nav.textContent = String(badge); nav.classList.add('hot'); }
        } else if (nav) { nav.textContent = ''; nav.classList.remove('hot'); }
      } catch (e) { /* noop */ }
    }, 4000);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})(window.ZF = window.ZF || {});
