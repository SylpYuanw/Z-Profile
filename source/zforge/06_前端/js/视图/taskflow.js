/* ============================================================================
   视图 · 任务与工作流
   任务列表 + 工作流编排 (挂机跑批)
   ========================================================================== */
(function (ZF) {
  'use strict';
  const U = ZF.util, UI = ZF.ui, API = ZF.api, Store = ZF.store, R = ZF.router;

  ZF.screens.push({
    id: 'taskflow',
    title: '任务与工作流',
    short: '任务流',
    group: '采集',
    accent: 'var(--brand)',
    badge: () => (Store.taskCounts && Store.taskCounts.active) || null,

    render(host, params) {
      const view = UI.view([]);
      host.appendChild(view);
      const taskHost = U.el('div');
      const wfHost = U.el('div');
      let destroyed = false, taskTimer = null, runTimer = null;
      let loadDraft = () => {};
      let refreshPoolSteps = null;
      let poolChoices = [], poolsLoaded = false;
      const dialogs = new Set();

      function scheduleRuns(delay) {
        clearTimeout(runTimer);
        if (!destroyed) runTimer = setTimeout(renderRuns, delay);
      }

      view.appendChild(UI.section('工作流', '把多个采集串成一条流水线，点一次开始，然后去睡觉', wfHost));
      view.appendChild(UI.section('任务列表', '每一次采集的完整记录', taskHost));

      /* ================= 任务列表 ================= */
      async function renderTasks() {
        clearTimeout(taskTimer);
        if (destroyed) return;
        await Store.loadTasks({ limit: 60 });
        if (destroyed) return;
        U.clear(taskHost);
        const counts = Store.taskCounts;

        taskHost.appendChild(UI.grid(5, [
          UI.kpiAnim('总任务', counts.total || 0, (v) => U.int(v), '', 'var(--brand)'),
          UI.kpi('进行中', String(counts.active || 0), '排队 + 运行', counts.active ? 'var(--warn)' : 'var(--fg-2)'),
          UI.kpi('成功', String(counts.success || 0), '', 'var(--ok)'),
          UI.kpi('失败', String(counts.failed || 0), '', 'var(--danger)'),
          UI.kpi('已取消', String(counts.canceled || 0), '', 'var(--warn)'),
        ]));

        taskHost.appendChild(U.el('div', { style: { height: '16px' } }));

        if (!Store.tasks.length) {
          taskHost.appendChild(UI.card({ body: UI.empty('∅', '还没有任务', '去采集页点一次「开始采集」') }));
          return;
        }

        taskHost.appendChild(UI.card({
          body: UI.table({
            pageSize: 20,
            rows: Store.tasks,
            columns: [
              { key: 'task_id', label: '任务号', width: '130px', render: (r) => U.el('span', { style: { fontFamily: 'var(--font-mono)', fontSize: '11px' }, text: r.task_id }) },
              { key: 'crawler_name', label: '爬虫' },
              { key: 'params', label: '参数', get: (r) => Object.values(r.params || {}).join(' · ').slice(0, 30), sortable: false },
              { key: 'state', label: '状态', render: (r) => UI.stateTag(r.state) },
              { key: 'items', label: '采集', num: true, render: (r) => (r.items ? U.int(r.items) : '—') },
              { key: 'duration', label: '耗时', num: true, render: (r) => (r.duration ? U.dur(r.duration) : '—') },
              { key: 'created_at', label: '创建时间', num: true, render: (r) => U.ts(r.created_at, true), sortValue: (r) => r.created_at },
              {
                key: '_act', label: '操作', sortable: false, render: (r) => U.el('div', { style: { display: 'flex', gap: '6px' } }, [
                  (r.state === 'running' || r.state === 'queued')
                    ? U.el('button', { class: 'btn', style: { padding: '2px 8px' }, text: '取消', onclick: async (e) => {
                      e.stopPropagation();
                      try { await API.cancelTask(r.task_id); UI.toast('已取消', 'good'); setTimeout(renderTasks, 600); }
                      catch (err) { UI.toast(String(err.message || err), 'bad'); }
                    } })
                    : null,
                  r.state !== 'running' && r.state !== 'queued'
                    ? U.el('button', { class: 'btn', style: { padding: '2px 8px' }, text: '详情', onclick: (e) => { e.stopPropagation(); showDetail(r); } })
                    : null,
                ]),
              },
            ],
            sortKey: 'created_at', sortDir: 'desc',
          }),
        }));

        // 有活跃任务就自动刷新
        if (counts.active > 0) taskTimer = setTimeout(renderTasks, 2500);
      }

      function showDetail(task) {
        let stream = null;
        const term = UI.terminal({ max: 2000, style: { height: '420px' } });
        const m = UI.modal({
          title: `任务 ${task.task_id} · ${task.crawler_name}`,
          width: 'min(900px, 94vw)',
          onClose: () => { if (stream) stream.close(); dialogs.delete(m); },
          body: U.frag([
            UI.kv([
              ['状态', UI.stateTag(task.state)],
              ['参数', JSON.stringify(task.params)],
              ['账号池', task.account_pool_id ? '#' + task.account_pool_id : '无需账号'],
              ['实际账号', task.account ? `${task.account} (#${task.account_id})` : '—'],
              ['采集条数', U.int(task.items)],
              ['错误数', U.int(task.errors)],
              ['耗时', U.dur(task.duration)],
              ['创建', U.ts(task.created_at, true)],
              ['开始', task.started_at ? U.ts(task.started_at, true) : '—'],
              ['结束', task.finished_at ? U.ts(task.finished_at, true) : '—'],
              ['失败原因', task.error || '—'],
            ]),
            U.el('div', { style: { height: '14px' } }),
            term,
          ]),
        });
        dialogs.add(m);
        stream = API.taskStream(task.task_id, {
          onLog: (l) => term.write(l.level, l.msg, l.ts),
          onState: () => {},
          onDone: () => {},
        });
      }

      /* ================= 工作流 ================= */
      let draft = [];   // [{crawler, params, on_fail, retries, label}]
      let editingWorkflowId = null;

      function renderWorkflows() {
        U.clear(wfHost);

        /* ---- 编排器 ---- */
        const stepHost = U.el('div');

        function renderSteps() {
          U.clear(stepHost);
          if (!draft.length) {
            stepHost.appendChild(UI.empty('∅', '还没有步骤', '从下面选一个爬虫加进来'));
          }
          draft.forEach((st, i) => {
            const c = Store.crawler(st.crawler);
            const paramInputs = {};
            const fields = (c ? c.params : []).map((p) => {
              let ctl;
              if (p.type === 'number') ctl = U.el('input', { type: 'number', value: st.params[p.key] !== undefined ? st.params[p.key] : (p.default !== undefined ? p.default : ''), style: { width: '110px' } });
              else if (p.type === 'switch') ctl = U.el('input', { type: 'checkbox', checked: st.params[p.key] !== undefined ? !!st.params[p.key] : !!p.default });
              else if (p.type === 'select') ctl = UI.select(p.options || [], st.params[p.key] !== undefined ? st.params[p.key] : p.default, (v) => { st.params[p.key] = v; });
              else ctl = U.el('input', { type: 'text', value: st.params[p.key] !== undefined ? st.params[p.key] : (p.default || ''), style: { width: '150px' } });
              ctl.setAttribute('aria-label', `第${i + 1}步 ${p.label}`);
              paramInputs[p.key] = { ctl, spec: p };
              return U.el('div', { style: { display: 'flex', alignItems: 'center', gap: '6px' } }, [
                U.el('span', { class: 'task-meta', text: p.label }),
                ctl,
              ]);
            });

            if (c && c.needs_account) {
              const compatible = poolChoices.filter((p) => p.site === c.account_site && p.enabled);
              if (st.account_pool_id == null && poolsLoaded) st.account_pool_id = (compatible.find((p) => p.is_default) || compatible[0] || {}).id || null;
              const options = compatible.map((p) => ({ value: String(p.id), label: `${p.name} · ${p.available}/${p.members} 可领取` }));
              if (st.account_pool_id && !compatible.some((p) => p.id === st.account_pool_id)) options.unshift({ value: String(st.account_pool_id), label: '原池已停用或不兼容，请重新选择' });
              if (!options.length) options.push({ value: '', label: poolsLoaded ? '请先创建账号池' : '正在加载账号池' });
              const poolLink = U.el('a', { class: 'pool-link', text: '管理账号池', href: '#/accounts' + (st.account_pool_id ? '?pool=' + st.account_pool_id : '?site=' + c.account_site) });
              const poolCtl = UI.select(options, String(st.account_pool_id || ''), (v) => {
                st.account_pool_id = Number(v) || null;
                poolLink.href = '#/accounts' + (st.account_pool_id ? '?pool=' + st.account_pool_id : '?site=' + c.account_site);
              });
              poolCtl.setAttribute('aria-label', `第${i + 1}步 账号池`);
              fields.push(UI.field('账号池', poolCtl)); fields.push(poolLink);
            }

            stepHost.appendChild(U.el('div', { class: 'wf-step' }, [
              U.el('div', { class: 'wf-num', text: String(i + 1) }),
              U.el('div', { style: { flex: '1', minWidth: '0' } }, [
                U.el('div', { style: { display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' } }, [
                  U.el('span', { style: { fontWeight: '550' }, text: c ? c.name : st.crawler }),
                  ...fields,
                  U.el('span', { class: 'task-meta', text: '失败时' }),
                  UI.select([{ value: 'continue', label: '继续' }, { value: 'abort', label: '中止' }],
                    st.on_fail || 'continue', (v) => { st.on_fail = v; }),
                  U.el('span', { class: 'task-meta', text: '重试' }),
                  U.el('input', {
                    type: 'number', value: st.retries || 0, min: 0, max: 5, style: { width: '60px' },
                    onchange: (e) => { st.retries = Number(e.target.value) || 0; },
                  }),
                ]),
              ]),
              U.el('div', { style: { display: 'flex', gap: '4px' } }, [
                U.el('button', { class: 'btn', style: { padding: '2px 8px' }, text: '↑', disabled: i === 0, onclick: () => { [draft[i - 1], draft[i]] = [draft[i], draft[i - 1]]; renderSteps(); } }),
                U.el('button', { class: 'btn', style: { padding: '2px 8px' }, text: '↓', disabled: i === draft.length - 1, onclick: () => { [draft[i + 1], draft[i]] = [draft[i], draft[i + 1]]; renderSteps(); } }),
                U.el('button', { class: 'btn danger', style: { padding: '2px 8px' }, text: '✕', onclick: () => { draft.splice(i, 1); renderSteps(); } }),
              ]),
            ]));

            // 绑定参数输入
            Object.entries(paramInputs).forEach(([k, o]) => {
              const update = () => { st.params[k] = o.spec.type === 'switch' ? o.ctl.checked : o.spec.type === 'number' ? Number(o.ctl.value) : o.ctl.value; };
              o.ctl.addEventListener('input', update);
              o.ctl.addEventListener('change', update);
            });
          });
        }

        const addSelect = UI.select(
          [{ value: '', label: '＋ 添加一个采集步骤…' }].concat(
            Store.crawlers.map((c) => ({ value: c.id, label: c.name }))),
          '', (v) => {
            if (!v) return;
            const c = Store.crawler(v);
            const p = {};
            (c ? c.params : []).forEach((x) => { if (x.default !== undefined) p[x.key] = x.default; });
            draft.push({ crawler: v, params: p, on_fail: 'continue', retries: 0 });
            addSelect.value = '';
            renderSteps();
          });
        refreshPoolSteps = renderSteps;

        const nameInput = U.el('input', { type: 'text', placeholder: '工作流名称，例如「每日 B站 + 淘宝」', style: { width: '280px' } });
        loadDraft = (w) => {
          draft = JSON.parse(JSON.stringify(w.steps));
          editingWorkflowId = w.id;
          nameInput.value = w.name;
          renderSteps();
        };
        const runBtn = U.el('button', { class: 'btn primary', text: '▶ 保存并开始跑' });
        const saveBtn = U.el('button', { class: 'btn', text: '仅保存配置' });

        async function saveDraft(start) {
          if (!draft.length) { UI.toast('至少加一个步骤', 'bad'); return; }
          if (draft.some((s) => { const c = Store.crawler(s.crawler); return c && c.needs_account && (!poolsLoaded || !s.account_pool_id); })) {
            UI.toast('请先为需要登录的步骤选择账号池', 'bad'); return;
          }
          try {
            const id = editingWorkflowId || 'wf-' + Date.now();
            await API.saveWorkflow({ id, name: nameInput.value.trim() || '未命名工作流', steps: draft });
            editingWorkflowId = id;
            if (start) {
              const r = await API.runWorkflow(id);
              UI.toast(`工作流已启动: ${r.run_id}`, 'good');
            } else UI.toast('已保存步骤参数和账号池选择', 'good');
            await Store.loadWorkflows();
            renderRuns();
          } catch (e) { UI.toast(String(e.message || e), 'bad'); }
        }
        runBtn.addEventListener('click', () => saveDraft(true));
        saveBtn.addEventListener('click', () => saveDraft(false));

        U.append(wfHost, [
          UI.card({
            title: '编排',
            sub: '步骤按顺序执行。每步可以单独设置失败策略和重试次数。',
            body: U.frag([
              stepHost,
              U.el('div', { style: { display: 'flex', gap: '10px', marginTop: '14px', flexWrap: 'wrap', alignItems: 'center' } }, [
                addSelect, nameInput, saveBtn, runBtn,
              ]),
            ]),
          }),
          U.el('div', { style: { height: '16px' } }),
          U.el('div', { id: 'wf-runs' }),
        ]);
        renderSteps();
        renderRuns();
      }

      async function renderRuns() {
        clearTimeout(runTimer);
        if (destroyed) return;
        const holder = document.getElementById('wf-runs');
        if (!holder) return;
        await Store.loadWorkflows();
        if (destroyed || !holder.isConnected) return;
        U.clear(holder);

        const saved = Store.workflows || [];
        if (saved.length) {
          holder.appendChild(UI.card({
            title: '已保存的工作流',
            body: U.frag(saved.map((w) => U.el('div', { class: 'task-row' }, [
              U.el('div', {}, [
                U.el('div', { class: 'task-name', text: w.name }),
                U.el('div', { class: 'task-meta', text: `${w.steps.length} 步 · ${w.steps.map((s) => Store.crawler(s.crawler) ? Store.crawler(s.crawler).name : s.crawler).join(' → ')}` }),
              ]),
              U.el('div', { style: { display: 'flex', gap: '6px' } }, [
                U.el('button', { class: 'btn primary', style: { padding: '3px 10px' }, text: '开始', onclick: async () => {
                  try { const r = await API.runWorkflow(w.id); UI.toast('已启动 ' + r.run_id, 'good'); scheduleRuns(1200); }
                  catch (e) { UI.toast(String(e.message || e), 'bad'); }
                } }),
                U.el('button', { class: 'btn', style: { padding: '3px 10px' }, text: '载入编辑', onclick: () => {
                  loadDraft(w);
                  UI.toast('已载入到编排器');
                } }),
                U.el('button', { class: 'btn danger', style: { padding: '3px 10px' }, text: '删除', onclick: async () => {
                  await API.deleteWorkflow(w.id); scheduleRuns(400);
                } }),
              ]),
            ]))),
          }));
          holder.appendChild(U.el('div', { style: { height: '16px' } }));
        }

        const runs = Store.workflowRuns || [];
        if (runs.length) {
          holder.appendChild(UI.card({
            title: '运行记录',
            body: U.frag(runs.map((r) => {
              const done = r.total_steps ? r.step_results.length : 0;
              return U.el('div', { class: 'task-row' }, [
                U.el('div', { style: { minWidth: '0' } }, [
                  U.el('div', { class: 'task-name', text: `${r.workflow_name || r.workflow_id}` }),
                  U.el('div', { class: 'task-meta', text: `${r.run_id} · 第 ${r.current_step}/${r.total_steps} 步 · ${U.ago(r.started_at)}` }),
                  U.el('div', { style: { display: 'flex', gap: '4px', marginTop: '6px', flexWrap: 'wrap' } },
                    (r.step_results || []).map((s, i) => UI.tag(
                      `${i + 1}. ${s.label || ''} ${s.items !== undefined ? s.items + '条' : ''}`,
                      s.state === 'success' ? 'ok' : s.state === 'failed' ? 'danger' : 'muted'))),
                ]),
                U.el('div', { style: { display: 'flex', gap: '8px', alignItems: 'center' } }, [
                  UI.stateTag(r.state === 'running' ? 'running' : r.state === 'success' ? 'success' : r.state === 'canceled' ? 'canceled' : 'failed'),
                  r.state === 'running'
                    ? U.el('button', { class: 'btn danger', style: { padding: '3px 10px' }, text: '取消', onclick: async () => {
                      try { await API.cancelWorkflowRun(r.run_id); UI.toast('已取消', 'good'); }
                      catch (e) { UI.toast(String(e.message || e), 'bad'); }
                    } })
                    : null,
                ]),
              ]);
            })),
          }));
        } else {
          holder.appendChild(UI.card({ body: UI.empty('∅', '还没有工作流运行记录', '配好步骤后点「保存并开始跑」') }));
        }
        if (runs.some((r) => r.state === 'running')) scheduleRuns(2500);
      }

      renderTasks();
      renderWorkflows();
      API.accountPools().then((data) => {
        if (destroyed) return;
        poolChoices = data.pools || []; poolsLoaded = true;
        // 仅刷新步骤，保留已输入的工作流名称和参数。
        if (refreshPoolSteps) refreshPoolSteps();
      }).catch((e) => { if (!destroyed) UI.toast('账号池加载失败：' + e.message, 'bad'); });

      return {
        destroy() {
          destroyed = true;
          clearTimeout(taskTimer);
          clearTimeout(runTimer);
          dialogs.forEach((m) => m.close());
        },
      };
    },
  });
})(window.ZF = window.ZF || {});
