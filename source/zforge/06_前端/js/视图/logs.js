/* ============================================================================
   视图 · 运行日志 / 环境自检
   ========================================================================== */
(function (ZF) {
  'use strict';
  const U = ZF.util, UI = ZF.ui, API = ZF.api, Store = ZF.store;

  /* ==================== 运行日志 ==================== */
  ZF.screens.push({
    id: 'logs',
    title: '运行日志',
    short: '日志',
    group: '系统',
    accent: 'var(--brand-3)',

    render(host) {
      const view = UI.view([]);
      host.appendChild(view);
      let level = '', timer = null;
      const term = UI.terminal({ max: 1500, style: { height: '62vh' } });
      const levelHost = U.el('div');

      view.appendChild(UI.section('运行日志',
        '所有任务的历史日志（后端内存缓冲，最近 4000 条）',
        U.frag([
          U.el('div', { style: { display: 'flex', gap: '10px', alignItems: 'center', marginBottom: '12px', flexWrap: 'wrap' } }, [
            levelHost,
            U.el('span', { style: { flex: '1' } }),
            U.el('button', { class: 'btn', text: '刷新', onclick: load }),
            U.el('button', { class: 'btn', text: '清屏', onclick: () => term.clear() }),
          ]),
          UI.card({ body: term }),
        ])));

      levelHost.appendChild(UI.segmented(
        [{ value: '', label: '全部' }, { value: 'INFO', label: 'INFO' }, { value: 'OK', label: 'OK' },
         { value: 'WARN', label: 'WARN' }, { value: 'ERR', label: 'ERR' }],
        level, (v) => { level = v; term.clear(); load(); }));

      async function load() {
        try {
          const r = await API.recentLogs({ limit: 500, level: level || undefined });
          term.clear();
          (r.lines || []).forEach((l) => term.write(l.level, l.msg, l.ts));
          if (!(r.lines || []).length) term.write('INFO', '（暂无日志）');
        } catch (e) {
          term.write('ERR', String(e.message || e));
        }
      }

      load();
      timer = setInterval(load, 5000);
      return { destroy() { if (timer) clearInterval(timer); } };
    },
  });

  /* ==================== 环境自检 ==================== */
  ZF.screens.push({
    id: 'system',
    title: '环境自检',
    short: '自检',
    group: '系统',
    accent: 'var(--fg-1)',

    render(host) {
      const view = UI.view([]);
      host.appendChild(view);
      const body = U.el('div');
      view.appendChild(UI.section('环境自检', '检查运行环境、依赖、数据源和采集模块的可用状态', body));

      (async () => {
        let info;
        try { info = await Store.loadSystem(); }
        catch (e) { body.appendChild(UI.card({ title: '读取失败', sub: String(e.message || e) })); return; }

        const deps = info.deps || [];
        const missing = deps.filter((d) => !d.installed);
        const required = new Set(['flask', 'selenium', 'lxml', 'requests']);
        const missingRequired = missing.filter((d) => required.has(d.name));

        body.appendChild(UI.grid(4, [
          UI.kpi('Python', info.python_version, info.python, 'var(--brand)'),
          UI.kpi('依赖完备度', `${deps.length - missing.length} / ${deps.length}`,
            missing.length ? `${missingRequired.length ? '缺必需依赖' : '缺可选依赖'} ${missing.map((m) => m.name).join(', ')}` : '全部就绪',
            missing.length ? 'var(--warn)' : 'var(--ok)'),
          UI.kpi('已注册爬虫', String(Store.crawlers.length), (Store.crawlers.map((c) => c.name).join(' / ') || '').slice(0, 40)),
          UI.kpi('数据库', U.bytes((Store.dataStats || {}).db_size || 0), `${U.int(Store.itemsTotal)} 条记录`),
        ]));
        body.appendChild(U.el('div', { style: { height: '16px' } }));

        body.appendChild(UI.grid(2, [
          UI.card({
            title: '依赖清单',
            sub: missingRequired.length ? '缺少必需依赖，相关采集功能不可用'
              : missing.length ? '当前仅缺可选依赖；前端 CSV / Excel 导出使用 SheetJS，不依赖 openpyxl' : '全部就绪',
            body: UI.table({
              pageSize: 0,
              rows: deps,
              columns: [
                { key: 'name', label: '包名' },
                { key: 'version', label: '版本', num: true },
                {
                  key: 'installed', label: '状态',
                  render: (r) => UI.tag(r.installed ? '已安装' : required.has(r.name) ? '必需，缺失' : '可选，未安装',
                    r.installed ? 'ok' : required.has(r.name) ? 'danger' : 'warn'),
                },
              ],
            }),
          }),
          UI.card({
            title: '路径',
            body: U.frag([
              U.el('div', { style: { height: '10px' } }),
              UI.kv([
                ['项目根目录', info.project_root],
                ['Python 解释器', info.python],
                ['爬虫扫描目录', info.crawlers_root],
                ['账号库', info.account_db.path + (info.account_db.exists ? '' : '（不存在）')],
                ['工作台数据库', info.web_db.path],
              ]),
            ]),
          }),
        ]));

        body.appendChild(U.el('div', { style: { height: '16px' } }));
        body.appendChild(UI.card({
          title: '数据源',
          sub: '工作台数据库可写，历史库只读',
          body: UI.table({
            pageSize: 0,
            rows: info.sources || [],
            columns: [
              { key: 'name', label: '名称' },
              { key: 'id', label: 'ID' },
              {
                key: 'writable', label: '权限',
                render: (r) => UI.tag(r.writable ? '可写' : '只读', r.writable ? 'accent' : 'muted'),
              },
              {
                key: 'exists', label: '状态',
                render: (r) => UI.tag(r.exists ? '存在' : '不存在', r.exists ? 'ok' : 'danger'),
              },
              { key: 'path', label: '路径', render: (r) => U.el('span', { style: { fontFamily: 'var(--font-mono)', fontSize: '11px' }, text: r.path }) },
            ],
          }),
        }));

        /* ---------- 爬虫注册详情 ---------- */
        body.appendChild(U.el('div', { style: { height: '16px' } }));
        body.appendChild(UI.card({
          title: '爬虫注册表',
          sub: '查看已接入模块的采集方式、登录要求和配置状态',
          body: U.frag([
            U.el('div', { style: { height: '10px' } }),
            UI.table({
              pageSize: 0,
              rows: Store.crawlers,
              columns: [
                { key: 'name', label: '名称' },
                { key: 'id', label: 'ID' },
                { key: 'site', label: '站点' },
                { key: 'collection', label: '采集方式', render: U.collectionLabel },
                {
                  key: 'needs_account', label: '账号',
                  render: (r) => r.needs_account ? UI.tag('走账号池: ' + r.account_site, 'warn') : UI.tag('免登录', 'muted'),
                },
                { key: 'params', label: '参数数', num: true, render: (r) => r.params.length },
                { key: 'est_seconds', label: '预估耗时', num: true, render: (r) => r.est_seconds + 's' },
                {
                  key: 'source', label: '源文件',
                  render: (r) => U.el('span', { style: { fontFamily: 'var(--font-mono)', fontSize: '11px' }, text: r.source.replace(info.project_root + '\\', '') }),
                },
              ],
            }),
            (Store.crawlerErrors || []).length
              ? U.frag([
                U.el('div', { style: { height: '14px' } }),
                UI.card({
                  title: '加载异常的爬虫',
                  sub: '这些文件存在但没能注册成功',
                  body: U.frag(Store.crawlerErrors.map((e) => U.el('div', { class: 'task-row' }, [
                    U.el('div', {}, [
                      U.el('div', { class: 'task-name', text: e.source }),
                      U.el('div', { class: 'task-meta', text: e.error }),
                    ]),
                  ]))),
                }),
              ])
              : null,
          ]),
        }));

        /* ---------- 快捷键 ---------- */
        body.appendChild(U.el('div', { style: { height: '16px' } }));
        body.appendChild(UI.card({
          title: '快捷键',
          body: U.frag([
            U.el('div', { style: { height: '10px' } }),
            UI.kv([
              ['Ctrl / ⌘ + K', '打开命令面板（搜页面、爬虫、数据、动作）'],
              ['/', '同上，单手操作'],
              ['g 然后 1~9', '快速跳转：1工作台 2采集 3任务流 4数据 5SQL 6分析 7账号池 8日志 9自检'],
              ['Ctrl / ⌘ + Enter', 'SQL 实验室里执行查询'],
              ['Esc', '关闭弹窗'],
            ]),
          ]),
        }));
      })();

      return null;
    },
  });
})(window.ZF = window.ZF || {});
