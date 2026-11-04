/* ============================================================================
   视图 · 采集页 (通用模板)
   ----------------------------------------------------------------------------
   同一份代码服务所有爬虫 —— 表单按 META.params 自动生成。
   加新爬虫不用改这里。
   ========================================================================== */
(function (ZF) {
  'use strict';
  const U = ZF.util, UI = ZF.ui, Ch = ZF.charts, API = ZF.api, Store = ZF.store, R = ZF.router, S = ZF.stats;

  ZF.screens.push({
    id: 'crawler',
    title: '采集',
    short: '采集',
    group: '采集',
    accent: 'var(--brand)',
    badge: () => (Store.taskCounts && Store.taskCounts.active) || null,

    render(host, params) {
      const cid = params.id;
      if (!cid) {
        host.appendChild(UI.view([UI.section('选择采集项目', '各平台分别配置任务、账号池和结果', U.el('div', { class: 'project-grid' }, Store.crawlers.map((c) => UI.card({
          title: c.name, sub: U.collectionLabel(c), body: U.frag([
            U.el('p', { class: 'project-description', text: c.desc }),
            U.el('button', { class: 'btn primary', text: '配置该项目采集', onclick: () => R.go('crawler', { id: c.id }) }),
          ]),
        }))))]));
        return null;
      }
      const crawler = Store.crawler(cid);

      if (!crawler) {
        host.appendChild(UI.view([UI.empty('⚙', '没有找到这个爬虫',
          cid ? `id = ${cid} 不在注册表里` : '注册表是空的')]));
        return null;
      }
      document.documentElement.style.setProperty('--accent', crawler.accent);

      /* ---------------- 参数表单 ---------------- */
      const inputs = {};
      const formFields = crawler.params.map((p) => {
        let ctl;
        if (p.type === 'number') {
          ctl = U.el('input', {
            type: 'number', value: p.default !== undefined ? p.default : '',
            min: p.min, max: p.max, placeholder: p.placeholder || '',
          });
        } else if (p.type === 'select') {
          ctl = UI.select(p.options || [], p.default, () => {});
        } else if (p.type === 'switch') {
          ctl = U.el('input', { type: 'checkbox', checked: !!p.default });
        } else {
          ctl = U.el('input', { type: 'text', value: p.default || '', placeholder: p.placeholder || '' });
        }
        inputs[p.key] = ctl;
        return UI.field(
          p.label + (p.required ? ' *' : ''),
          ctl,
          p.help
        );
      });

      let poolReady = !crawler.needs_account, running = false;
      const poolHost = U.el('div');
      let poolSelect = null;
      const startBtn = U.el('button', { class: 'btn primary', text: '▶ 开始采集', disabled: !poolReady });
      const cancelBtn = U.el('button', { class: 'btn danger', text: '■ 取消', disabled: true });

      const formCard = UI.card({
        title: crawler.name,
        sub: crawler.desc,
        body: U.frag([
          U.el('div', { class: 'chips', style: { margin: '12px 0 16px' } }, [
            UI.tag(crawler.site, 'accent'),
            U.el('span', { class: 'tag', text: U.collectionLabel(crawler), title: U.collectionStages(crawler) }),
            UI.tag(crawler.needs_account ? `账号池: ${crawler.account_site}` : '免登录'),
            UI.tag(crawler.exclusive ? '独占运行' : '可并发'),
            UI.tag(`预估 ${crawler.est_seconds}s`),
          ]),
          U.el('div', { class: 'grid g-2', style: { gap: '14px' } }, formFields),
          poolHost,
          U.el('div', { style: { display: 'flex', gap: '10px', marginTop: '18px', alignItems: 'center' } }, [
            startBtn, cancelBtn,
            U.el('span', { class: 'field-help', text: '采集过程中可以关掉这个页面，任务会继续跑完（后端在跑）' }),
          ]),
        ]),
      });

      /* ---------------- 进度 + 日志 ---------------- */
      const progress = UI.progress();
      const progressText = U.el('span', { class: 'task-meta', text: '待运行' });
      const term = UI.terminal({ max: 800 });

      const runCard = UI.card({
        title: '运行状态',
        badge: U.el('span', { style: { marginLeft: 'auto' }, id: 'run-state' }),
        body: U.frag([
          U.el('div', { style: { display: 'flex', alignItems: 'center', gap: '12px', margin: '10px 0 14px' } }, [
            U.el('div', { style: { flex: '1' } }, [progress]),
            progressText,
          ]),
          term,
        ]),
      });

      /* ---------------- 结果区 ---------------- */
      const resultHost = U.el('div');

      host.appendChild(UI.view([
        U.el('div', { class: 'grid', style: { gridTemplateColumns: 'minmax(0,1fr) minmax(0,1.1fr)', gap: '16px' } }, [
          formCard, runCard,
        ]),
        resultHost,
      ]));

      /* ---------------- 逻辑 ---------------- */
      let stream = null;
      let currentTask = null;
      let destroyed = false;

      async function loadPools() {
        if (!crawler.needs_account) return;
        const chosen = poolSelect ? poolSelect.value : params.pool;
        try {
          const data = await API.accountPools(crawler.account_site);
          if (destroyed) return;
          const pools = data.pools.filter((p) => p.enabled);
          const value = chosen || String((pools.find((p) => p.is_default) || pools[0] || {}).id || '');
          U.clear(poolHost);
          const options = pools.map((p) => ({ value: String(p.id), label: `${p.name}${p.is_default ? '（默认）' : ''} · ${p.available}/${p.members} 可领取` }));
          if (value && !pools.some((p) => String(p.id) === value)) options.unshift({ value, label: '原选择已停用或不兼容，请重新选择' });
          if (!options.length) options.push({ value: '', label: '暂无启用的兼容账号池' });
          const hint = U.el('div', { class: 'field-help' });
          const update = () => {
            const p = pools.find((p) => String(p.id) === poolSelect.value);
            poolReady = !!p && p.available > 0;
            startBtn.disabled = running || !poolReady;
            U.clear(hint);
            hint.appendChild(U.el('span', { text: p && !p.available ? '当前没有可领取账号，请检查过期状态、额度或占用。 ' : '任务内保持同一账号。 ' }));
            hint.appendChild(U.el('a', { class: 'pool-link', href: '#/accounts' + (p ? '?pool=' + p.id + '&login=1' : '?site=' + crawler.account_site + '&login=1'), text: '打开登录页添加账号' }));
          };
          poolSelect = UI.select(options, value, update);
          poolSelect.setAttribute('aria-label', '任务账号池');
          poolHost.appendChild(UI.field('账号池', poolSelect)); poolHost.appendChild(hint); update();
          poolHost.appendChild(U.el('button', { class: 'btn', style: { marginTop: '8px' }, text: '刷新账号状态', onclick: loadPools }));
        } catch (e) {
          poolReady = false; startBtn.disabled = true;
          U.clear(poolHost); poolHost.appendChild(U.el('p', { class: 'pool-error', text: '账号池加载失败：' + e.message }));
        }
      }
      loadPools();

      function collectParams() {
        const out = {};
        crawler.params.forEach((p) => {
          const ctl = inputs[p.key];
          if (p.type === 'switch') out[p.key] = ctl.checked;
          else if (p.type === 'number') {
            const v = ctl.value.trim();
            out[p.key] = v === '' ? (p.default !== undefined ? p.default : null) : Number(v);
          } else out[p.key] = ctl.value.trim();
        });
        return out;
      }

      function setBusy(busy) {
        running = busy;
        startBtn.disabled = busy || !poolReady;
        if (poolSelect) poolSelect.disabled = busy;
        cancelBtn.disabled = !busy;
        startBtn.textContent = busy ? '运行中…' : '▶ 开始采集';
      }

      function follow(taskId) {
        if (stream) stream.close();
        term.clear();
        progress.set(0);
        progressText.textContent = '排队中…';
        const stateTag = document.getElementById('run-state');
        if (stateTag) { U.clear(stateTag); stateTag.appendChild(UI.stateTag('queued')); }

        stream = API.taskStream(taskId, {
          onLog: (l) => term.write(l.level, l.msg, l.ts),
          onState: (s) => {
            progress.set(s.progress || 0);
            if (s.message) progressText.textContent = s.message;
            if (stateTag && s.state) { U.clear(stateTag); stateTag.appendChild(UI.stateTag(s.state)); }
          },
          onDone: async (d) => {
            if (destroyed) return;
            setBusy(false);
            loadPools();
            if (d.state === 'success') progress.set(1);
            const label = { success: '完成', failed: '失败', canceled: '已取消' }[d.state] || d.state;
            progressText.textContent = `${label} · 采集 ${d.items || 0} 条 · 耗时 ${U.dur(d.duration || 0)}`;
            if (stateTag) { U.clear(stateTag); stateTag.appendChild(UI.stateTag(d.state)); }
            if (d.state === 'success') UI.toast(`采集完成，共提交 ${d.items || 0} 条，入库时自动去重`, 'good');
            else if (d.state === 'failed') UI.toast('采集失败: ' + (d.error || ''), 'bad');
            // 刷新数据并展示结果
            await refreshData();
            if (destroyed) return;
            renderResult();
          },
          onError: () => { /* SSE 断开会自动重连; done 后会 close */ },
        });
      }

      async function refreshData() {
        try {
          await Promise.all([
            Store.loadItems({ limit: 2000 }),
            Store.loadDataStats(true),
            Store.loadTasks({ limit: 60 }),
            API.check(),
          ]);
        } catch (e) { /* noop */ }
      }

      startBtn.addEventListener('click', async () => {
        if (!API.online) { UI.toast('后端未连接，无法派活', 'bad'); return; }
        const p = collectParams();
        const missing = crawler.params.filter((x) => x.required && !String(p[x.key] || '').trim());
        if (missing.length) { UI.toast('请填写: ' + missing.map((x) => x.label).join('、'), 'bad'); return; }

        setBusy(true);
        term.clear();
        try {
          const task = await API.createTask(cid, p, crawler.needs_account && poolSelect ? Number(poolSelect.value) : null);
          currentTask = task.task_id;
          term.write('INFO', `任务已创建: ${task.task_id}`);
          follow(task.task_id);
        } catch (e) {
          setBusy(false);
          term.write('ERR', String(e.message || e));
          UI.toast(String(e.message || e), 'bad');
        }
      });

      cancelBtn.addEventListener('click', async () => {
        if (!currentTask) return;
        try {
          await API.cancelTask(currentTask);
          term.write('WARN', '已发出取消指令');
        } catch (e) { UI.toast(String(e.message || e), 'bad'); }
      });

      /* ---------------- 结果渲染 ---------------- */
      function renderResult() {
        Ch.destroyAll(resultHost);
        U.clear(resultHost);
        const rows = Store.items.filter((r) => r.source === cid);
        if (!rows.length) {
          resultHost.appendChild(UI.section('累计采集结果', '', UI.empty('∅', '还没有采集到数据', '填好参数点上面的「开始采集」')));
          return;
        }

        const extra = rows.map((r) => { try { return JSON.parse(r.extra_json || '{}'); } catch (e) { return {}; } });
        const metrics = rows.map((r) => Number(r.metric) || 0);
        const prices = S.clean(rows.map((r) => Number(r.price)));

        if (cid === 'bilibili') {
          renderBiliResult(rows, extra, metrics);
        } else if (cid === 'taobao') {
          renderTaobaoResult(rows, extra, metrics, prices);
        } else {
          renderGenericResult(rows, metrics);
        }
      }

      function renderBiliResult(rows, extra, views) {
        resultHost.appendChild(UI.section('本项目累计结果', `${rows.length} 个视频；采集控制与详细分析分别查看`, U.frag([
          U.el('div', { class: 'pool-actions', style: { marginBottom: '16px' } }, [
            U.el('button', { class: 'btn primary', text: '查看 B站数据分析', onclick: () => R.go('analysis', { project: cid, dataset: 'current' }) }),
            U.el('button', { class: 'btn', text: '选择 B站其他数据集', onclick: () => R.go('data', { project: cid }) }),
          ]),
          UI.card({ title: '视频结果明细', body: UI.table({ rows, pageSize: 20, columns: [
            { key: 'title', label: '标题', width: '55%' },
            { key: 'metric', label: '播放次数', render: (r) => U.int(r.metric) },
            { key: 'keyword', label: '采集关键词' },
          ] }) }),
        ])));
      }

      function renderTaobaoResult(rows) {
        resultHost.appendChild(UI.section('本项目累计结果', `${rows.length} 个商品`, U.frag([
          U.el('div', { class: 'pool-actions', style: { marginBottom: '16px' } }, [
            U.el('button', { class: 'btn primary', text: '查看淘宝数据分析', onclick: () => R.go('analysis', { project: cid, dataset: 'current' }) }),
            U.el('button', { class: 'btn', text: '选择淘宝其他数据集', onclick: () => R.go('data', { project: cid }) }),
          ]),
          UI.card({ title: '商品结果明细', body: UI.table({ rows, pageSize: 20, columns: [
            { key: 'title', label: '标题', width: '50%' }, { key: 'price', label: '价格', render: (r) => U.money(r.price) },
            { key: 'metric', label: '付款人数', render: (r) => U.int(r.metric) }, { key: 'keyword', label: '采集关键词' },
          ] }) }),
        ])));
      }

      function renderGenericResult(rows, metrics) {
        resultHost.appendChild(UI.section('累计采集结果', `${rows.length} 条`,
          UI.card({
            body: UI.table({
              pageSize: 25,
              columns: [
                { key: 'title', label: '标题', width: '50%' },
                { key: 'metric', label: '主指标', num: true, render: (r) => U.int(r.metric) },
                { key: 'price', label: '价格', num: true, render: (r) => (r.price === null ? '—' : U.money(r.price)) },
              ],
              rows,
            }),
          })));
      }

      // 进页面就展示已有数据
      (async () => {
        await refreshData();
        if (destroyed) return;
        renderResult();
        // 有正在跑的任务就自动接上
        const active = (Store.tasks || []).find((t) => t.crawler === cid && (t.state === 'running' || t.state === 'queued'));
        if (active) {
          currentTask = active.task_id;
          setBusy(true);
          follow(active.task_id);
        }
      })();

      return {
        destroy() { destroyed = true; if (stream) { stream.close(); stream = null; } },
      };
    },
  });
})(window.ZF = window.ZF || {});
