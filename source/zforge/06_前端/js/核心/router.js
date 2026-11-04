/* ============================================================================
   ZForge · 路由
   每个屏幕: { id, title, short, group, accent, badge(), render(host, params) }
   ========================================================================== */
(function (ZF) {
  'use strict';
  const U = ZF.util, UI = ZF.ui;

  // 视图在 boot.js 之前加载，注册表必须由路由层提前创建。
  ZF.screens = ZF.screens || [];

  const R = { screens: [], map: {}, current: null, cleanups: [] };

  R.define = function (s) { R.screens.push(s); R.map[s.id] = s; return s; };

  R.go = function (id, params) {
    const q = params && Object.keys(params).length
      ? '?' + new URLSearchParams(params).toString() : '';
    location.hash = `#/${id}${q}`;
  };

  R.parse = function () {
    const raw = location.hash.replace(/^#\/?/, '');
    const [path, query] = raw.split('?');
    const params = {};
    if (query) new URLSearchParams(query).forEach((v, k) => { params[k] = v; });
    return { id: path || 'dashboard', params };
  };

  R.mount = function () {
    const { id, params } = R.parse();
    const screen = R.map[id] || R.map.dashboard;
    const host = document.getElementById('view-host');

    R.cleanups.forEach((f) => { try { f(); } catch (e) { /* noop */ } });
    R.cleanups = [];
    ZF.charts.destroyAll(host);
    U.clear(host);
    host.scrollTop = 0;
    R.current = screen;

    U.$$('#nav .nav-item').forEach((b) => {
      if (b.dataset.id === screen.id) b.setAttribute('aria-current', 'page');
      else b.removeAttribute('aria-current');
    });

    const crumb = document.getElementById('crumb');
    U.clear(crumb);
    U.append(crumb, [
      U.el('span', { text: 'ZForge' }),
      U.el('span', { class: 'crumb-sep', text: '/' }),
      U.el('span', { text: screen.group || '工作台' }),
      U.el('span', { class: 'crumb-sep', text: '/' }),
      U.el('b', { text: screen.title }),
    ]);
    document.title = `${screen.title} · ZForge`;
    document.documentElement.style.setProperty('--accent', screen.accent || 'var(--brand)');

    let result;
    try {
      result = screen.render(host, params);
    } catch (e) {
      console.error(e);
      host.appendChild(UI.view([UI.card({
        title: '⚠ 这个页面渲染失败',
        sub: String(e && e.message || e),
        body: U.el('pre', { class: 'code', text: String(e && e.stack || e), style: { marginTop: '14px' } }),
      })]));
      return;
    }
    if (result && typeof result.then === 'function') {
      result.catch((e) => {
        console.error(e);
        UI.toast('页面加载失败: ' + (e.message || e), 'bad');
      });
    }
    if (result && typeof result.destroy === 'function') R.cleanups.push(result.destroy);
    if (screen.onMount) screen.onMount(host, params);
  };

  R.start = function () {
    window.addEventListener('hashchange', R.mount);
    R.mount();
  };

  R.onCleanup = (fn) => R.cleanups.push(fn);

  R.buildNav = function () {
    const nav = document.getElementById('nav');
    U.clear(nav);
    const groups = [];
    R.screens.forEach((s) => {
      let g = groups.find((x) => x.name === (s.group || '工作台'));
      if (!g) { g = { name: s.group || '工作台', items: [] }; groups.push(g); }
      g.items.push(s);
    });
    groups.forEach((g) => {
      nav.appendChild(U.el('div', { class: 'nav-group-label', text: g.name }));
      g.items.forEach((s) => {
        let badge = null;
        try { badge = typeof s.badge === 'function' ? s.badge() : s.badge; } catch (e) { badge = null; }
        nav.appendChild(U.el('button', {
          class: 'nav-item', dataset: { id: s.id }, title: s.title,
          onclick: () => R.go(s.id),
        }, [
          U.el('span', { class: 'nav-dot', style: s.accent ? { background: s.accent } : null }),
          U.el('span', { class: 'nav-label', text: s.short || s.title }),
          badge !== null && badge !== undefined
            ? U.el('span', { class: 'nav-badge', text: String(badge) }) : null,
        ]));
      });
    });
  };

  ZF.router = R;
})(window.ZF = window.ZF || {});
