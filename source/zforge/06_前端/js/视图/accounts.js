/* 账号池：分组管理、文件导入和任务占用。 */
(function (ZF) {
  'use strict';
  const U = ZF.util, UI = ZF.ui, API = ZF.api, R = ZF.router;
  const sites = { taobao: '淘宝', bilibili: 'B站' };
  const login = { unknown: '待检测', expired: '过期待检测', valid: '检测有效' };
  const strategyOptions = [{ value: 'least_used', label: '优先今日使用最少' }, { value: 'round_robin', label: '优先最久未使用' }];
  const row = (children) => U.el('div', { class: 'pool-actions' }, children);
  const btn = (text, action, kind) => U.el('button', { class: 'btn ' + (kind || ''), text, onclick: action });

  ZF.screens.push({
    id: 'accounts', title: '账号池', short: '账号池', group: '采集', accent: 'var(--ok)',
    render(host, params) {
      const body = U.el('div');
      host.appendChild(UI.view([UI.section('账号池', '选择用途分组，导入账号，再在采集任务或工作流中选用', body)]));
      let destroyed = false, pools = [], selected = null, accounts = [], all = [], filter = params.site || '';
      let loginJobs = [], loginTimer = null, promptedLogin = false;
      const loginBox = U.el('div');
      const loginNames = { opening: '打开中', waiting: '等待你登录', saving: '保存中', saved: '已保存', failed: '未完成', expired: '已超时', canceled: '已取消' };
      const dialogs = new Set();
      const fail = (e) => UI.toast(String(e.message || e), 'bad');
      const modal = (options) => {
        const m = UI.modal({ ...options, onClose: () => dialogs.delete(m) });
        dialogs.add(m);
        return m;
      };
      async function change(action) {
        try { await action(); await draw(); } catch (e) { fail(e); }
      }
      async function draw() {
        try {
          const [groups, data, jobs] = await Promise.all([API.accountPools(), API.accounts(), API.accountLogins()]);
          if (destroyed) return;
          pools = groups.pools || []; all = data.accounts || [];
          loginJobs = jobs.logins || [];
          ZF.store.accounts = all; ZF.store.accountSummary = data.summary || {};
          selected = pools.find((p) => String(p.id) === String(params.pool));
          if (!selected) selected = pools.find((p) => p.members && (!filter || p.site === filter)) || pools.find((p) => !filter || p.site === filter) || null;
          if (selected) {
            params.pool = String(selected.id);
            accounts = all.filter((a) => (a.pool_ids || []).includes(selected.id));
          } else accounts = [];
          render();
          scheduleLogins();
          if (params.login && !promptedLogin && selected) { promptedLogin = true; loginDialog(); }
        } catch (e) {
          if (destroyed) return;
          U.clear(body);
          body.appendChild(UI.card({ title: '账号池读取失败', sub: String(e.message || e), body: btn('重新加载', draw) }));
        }
      }
      function render() {
        U.clear(body);
        body.appendChild(row([
          UI.select([{ value: '', label: '全部站点' }, ...Object.entries(sites).map(([value, label]) => ({ value, label }))], filter,
            (site) => { filter = site; params.pool = ''; R.go('accounts', site ? { site } : {}); }),
          btn('＋ 新建账号池', () => editPool()), btn('刷新状态', draw),
          U.el('span', { class: 'field-help', text: '账号按站点共享身份、额度和占用；一个账号可加入多个池' }),
        ]));
        const list = U.el('div', { class: 'pool-list', role: 'navigation', 'aria-label': '账号池列表' });
        pools.filter((p) => !filter || p.site === filter).forEach((p) => list.appendChild(U.el('button', {
          class: 'pool-choice' + (selected && p.id === selected.id ? ' selected' : ''),
          'aria-current': selected && p.id === selected.id ? 'page' : 'false',
          onclick: () => R.go('accounts', { pool: p.id }),
        }, [U.el('b', { text: p.name }), U.el('span', { text: `${sites[p.site] || p.site}${p.is_default ? ' · 默认' : ''}${p.enabled ? '' : ' · 已停用'}` }),
          U.el('span', { text: `${p.members} 个账号 · ${p.available} 个可领取` })])));
        const detail = U.el('div', { class: 'pool-detail' });
        body.appendChild(U.el('div', { class: 'pool-layout' }, [list, detail]));
        if (!selected) {
          detail.appendChild(UI.card({ title: '建立第一个账号池', sub: '按站点和用途命名，然后打开官方登录页，扫码或手机登录即可录入。', body: btn('新建账号池', () => editPool(), 'primary') }));
          return;
        }
        detail.appendChild(UI.card({ title: selected.name,
          sub: `${sites[selected.site] || selected.site} · ${strategyOptions.find((o) => o.value === selected.strategy).label} · ${selected.enabled ? '已启用' : '已停用'}`,
          body: U.frag([
            row([btn('打开登录页添加账号', () => loginDialog(), 'primary'), btn('加入已有账号', existingDialog), btn('池设置', () => editPool(selected)), btn('文件导入（高级）', () => importDialog())]),
            U.el('div', { class: 'pool-summary' }, [UI.tag(`成员 ${accounts.length}`), UI.tag(`可领取 ${selected.available}`, 'ok'),
              UI.tag(`占用 ${accounts.filter((a) => a.busy).length}`), UI.tag(`过期待检测 ${accounts.filter((a) => a.login_status === 'expired').length}`, 'warn')]),
            U.el('p', { class: 'field-help', text: '任务从选定池自动领取一个账号，全程保持同一账号；结束后释放。待检测表示尚未确认网站接受登录态。' }),
          ]) }));
        detail.appendChild(loginBox); renderLogins();
        if (!accounts.length) {
          detail.appendChild(UI.card({ title: '这个池还没有账号', sub: '点击「打开登录页添加账号」，填写手机号，然后在弹出的窗口扫码或手机登录。无需手动寻找 Cookie。' }));
        } else {
          detail.appendChild(UI.card({ title: '池内账号', body: UI.table({ pageSize: 15, rows: accounts, columns: [
            { key: 'label', label: '账号', render: (a) => U.el('div', {}, [U.el('b', { text: a.label }), U.el('div', { class: 'task-meta', text: a.phone ? `手机号：${a.phone}` : '手机号待填写' })]) },
            { key: 'login_status', label: '登录状态', render: (a) => U.el('div', {}, [UI.tag(login[a.login_status] || '待检测', a.login_status === 'expired' ? 'warn' : ''),
              a.login_checked_at ? U.el('div', { class: 'task-meta', text: U.ts(a.login_checked_at, true) }) : null]) },
            { key: 'state', label: '轮换状态', render: (a) => UI.stateTag(a.state) },
            { key: 'busy', label: '占用', get: (a) => a.busy ? '任务使用中' : '空闲' },
            { key: 'used_today', label: '今日额度', get: (a) => `${a.used_today} / ${a.daily_limit}` },
            { key: '_action', label: '管理', sortable: false, render: (a) => row([
              btn('历史', () => history(a)),
              btn('账号资料 / 改名', () => profileDialog(a)),
              U.el('button', { class: 'btn', text: '重新登录', disabled: a.busy, onclick: () => loginDialog(a) }),
              U.el('button', { class: 'btn', text: '文件更新', disabled: a.busy, onclick: () => importDialog(a) }),
              btn(a.state === 'disabled' ? '启用' : '停用', () => change(() => API.setAccountState(a.id, a.state === 'disabled' ? 'healthy' : 'disabled'))),
              U.el('button', { class: 'btn', text: '移出本池', disabled: a.busy, onclick: () => removeDialog(a) }),
            ]) },
          ] }) }));
        }
        const bindings = ZF.store.crawlers.filter((c) => c.needs_account && c.account_site === selected.site);
        detail.appendChild(UI.card({ title: '兼容采集器', sub: '采集方式由脚本实现，账号凭据与采集方式分别配置。工作流可保存每一步的池选择。',
          body: bindings.length ? row(bindings.map((c) => btn(`${c.name} · ${U.collectionLabel(c)}`, () => R.go('crawler', { id: c.id, pool: selected.id }))))
            : U.el('p', { class: 'field-help', text: '当前没有必须使用该站点账号的采集器；B站现有采集路线无需登录。' }) }));
      }
      function scheduleLogins() {
        clearTimeout(loginTimer);
        if (!destroyed && loginJobs.some((j) => ['opening', 'waiting', 'saving'].includes(j.state))) loginTimer = setTimeout(refreshLogins, 1500);
      }
      async function refreshLogins() {
        if (destroyed) return;
        try {
          const jobs = (await API.accountLogins()).logins || [];
          if (destroyed) return;
          const newlySaved = jobs.some((j) => j.state === 'saved' && !loginJobs.some((old) => old.id === j.id && old.state === 'saved'));
          loginJobs = jobs;
          if (newlySaved) { await draw(); return; }
          renderLogins(); scheduleLogins();
        } catch (e) { if (!destroyed) { fail(e); loginTimer = setTimeout(refreshLogins, 4000); } }
      }
      function renderLogins() {
        U.clear(loginBox);
        const jobs = loginJobs.filter((j) => selected && j.pool_id === selected.id).slice(0, 5);
        if (!jobs.length) return;
        loginBox.appendChild(UI.card({ title: '网页登录录入', sub: '在弹出的独立浏览器中完成授权；这里显示获取和保存进度', body: U.frag(jobs.map((job) => {
          const actions = [];
          if (['opening', 'waiting'].includes(job.state)) actions.push(btn('取消本次登录', () => change(() => API.cancelAccountLogin(job.id))));
          if (job.state === 'saved') {
            const crawler = ZF.store.crawlers.find((c) => c.id === job.site);
            if (crawler) actions.push(btn('配置采集任务', () => R.go('crawler', { id: crawler.id, ...(crawler.needs_account ? { pool: job.pool_id } : {}) }), 'primary'));
            actions.push(btn('继续添加下一个账号', () => loginDialog()));
          }
          return U.el('div', { class: 'login-progress' }, [
            U.el('b', { text: `${job.label} · ${loginNames[job.state] || job.state}` }),
            U.el('p', { class: 'login-status', text: job.message, role: 'status' }), row(actions),
          ]);
        })) }));
      }
      function loginDialog(account) {
        const target = selected.id;
        const phone = U.el('input', { type: 'tel', value: account ? account.phone || '' : '', placeholder: '填写完整手机号', autocomplete: 'off', 'aria-label': '管理手机号' });
        const alias = U.el('input', { type: 'text', placeholder: '可不填，默认使用手机号', maxLength: 60, 'aria-label': '自定义账号名称' });
        const error = U.el('p', { class: 'pool-error', role: 'alert' });
        const start = btn('打开官方登录页', async () => {
          start.disabled = true; error.textContent = '';
          try {
            await API.startAccountLogin({ pool_id: target, phone: phone.value.trim(), label: alias.value.trim(), update_account_id: account ? account.id : null });
            m.close(); await draw();
          } catch (e) { error.textContent = e.message; } finally { start.disabled = false; }
        }, 'primary');
        const m = modal({ title: account ? `重新登录 · ${account.label}` : `网页登录添加 · ${selected.name}`, body: U.frag([
          U.el('p', { class: 'metric-example', text: '1. 填写用于管理的手机号。2. 点击下方按钮，在弹出的官方页面扫码或手机登录。3. 获取登录态后自动保存，无需导出或粘贴 Cookie。' }),
          UI.field('管理手机号', phone, '网站通常不提供完整手机号；这里由你填写，仅用于账号管理，不会更改网站绑定手机号。'),
          account ? null : UI.field('自定义名称（可选）', alias, '不填时用手机号命名；以后可以改名，详情仍保留手机号。'),
          U.el('p', { class: 'field-help', text: '密码和短信验证码只在官方页面输入。每次使用独立会话，当前先逐个账号登录；等待超过 10 分钟会结束。' }),
          error, row([start, btn('取消', () => m.close())]),
        ]) });
      }
      function profileDialog(account) {
        const phone = U.el('input', { type: 'tel', value: account.phone || '', 'aria-label': '账号资料手机号' });
        const alias = U.el('input', { type: 'text', value: account.label, placeholder: '留空则使用手机号', maxLength: 60, 'aria-label': '账号显示名称' });
        const error = U.el('p', { class: 'pool-error', role: 'alert' });
        const m = modal({ title: '账号资料 / 修改名称', body: U.frag([
          UI.field('手机号', phone, '仅用于本地管理，不会更改网站绑定信息。'), UI.field('显示名称', alias, '留空使用手机号；修改名称不改变平台身份、凭据及使用历史。'),
          U.el('p', { class: 'field-help', text: `内部账号 ID：${account.id} · 站点：${sites[account.site] || account.site}` }), error,
          row([btn('保存账号资料', async () => {
            try { await API.updateAccountProfile(account.id, { phone: phone.value.trim(), label: alias.value.trim() }); m.close(); await draw(); }
            catch (e) { error.textContent = e.message; }
          }, 'primary'), btn('取消', () => m.close())]),
        ]) });
      }
      function editPool(pool) {
        const name = U.el('input', { type: 'text', value: pool ? pool.name : '', placeholder: '例如：淘宝日常采集', maxLength: 60, 'aria-label': '账号池名称' });
        const site = UI.select(Object.entries(sites).map(([value, label]) => ({ value, label })), pool ? pool.site : filter || 'taobao', () => {});
        site.disabled = !!pool;
        const strategy = UI.select(strategyOptions, pool ? pool.strategy : 'least_used', () => {});
        const enabled = U.el('input', { type: 'checkbox', checked: pool ? pool.enabled : true });
        const isDefault = U.el('input', { type: 'checkbox', checked: !!(pool && pool.is_default) });
        if (pool && pool.is_default) isDefault.disabled = true;
        const error = U.el('div', { class: 'pool-error', role: 'alert' });
        const save = btn('保存', async () => {
          save.disabled = true; error.textContent = '';
          try {
            const result = pool ? await API.updateAccountPool(pool.id, { name: name.value.trim(), strategy: strategy.value, enabled: enabled.checked, is_default: isDefault.checked })
              : await API.createAccountPool({ site: site.value, name: name.value.trim(), strategy: strategy.value });
            m.close();
            if (params.pool === String(result.id)) await draw();
            else R.go('accounts', { pool: result.id });
          } catch (e) { error.textContent = e.message; } finally { save.disabled = false; }
        }, 'primary');
        const m = modal({ title: pool ? '账号池设置' : '新建账号池', width: 'min(540px, 94vw)', body: U.frag([
          UI.field('站点', site), UI.field('池名称', name), UI.field('自动选号', strategy),
          pool ? row([UI.field('启用此池', enabled), UI.field('设为站点默认池', isDefault)]) : null,
          error, row([save, btn('取消', () => m.close())]),
        ]) });
      }
      function importDialog(account) {
        const target = selected.id;
        const files = U.el('input', { type: 'file', accept: '.json,application/json', multiple: !account, 'aria-label': 'Cookie JSON 文件' });
        const label = U.el('input', { type: 'text', placeholder: '可选；批量导入使用文件名', maxLength: 60, value: account ? account.label : '' });
        label.disabled = !!account;
        const results = U.el('div', { role: 'status', 'aria-live': 'polite' });
        const submit = btn(account ? '更新此账号' : '导入所选文件', async () => {
          const chosen = Array.from(files.files || []);
          if (!chosen.length) { results.textContent = '请先选择 Cookie JSON 文件。'; return; }
          submit.disabled = true;
          try {
            const response = await API.importPoolAccounts(target, chosen, label.value.trim(), account && account.id);
            files.value = ''; U.clear(results);
            response.results.forEach((r) => results.appendChild(U.el('p', { class: r.status === 'failed' ? 'pool-error' : 'pool-result',
              text: `${r.file}：${r.status === 'failed' ? r.error : r.status === 'updated' ? '已更新凭据' : '已导入账号 ' + r.account.label}` })));
            await draw();
          } catch (e) { results.textContent = e.message; } finally { submit.disabled = false; }
        }, 'primary');
        const m = modal({ title: account ? `更新凭据 · ${account.label}` : `导入账号 · ${selected.name}`, width: 'min(620px, 94vw)', body: U.frag([
          U.el('p', { class: 'field-help', text: '选择浏览器导出的完整 Cookie 数组，或包含 cookies 数组的 JSON。单次最多 20 个文件、合计 2 MB。每个文件代表一个账号。' }),
          UI.field('Cookie JSON 文件', files), UI.field('单账号名称', label),
          U.el('p', { class: 'field-help', text: account ? '将核对账号身份；身份不一致会拒绝更新。' : '凭据加密保存。已存在的身份不会重复新增，可通过「加入已有账号」关联其他池。导入后显示待检测，不自动登录。' }),
          results, row([submit, btn('关闭', () => { files.value = ''; m.close(); })]),
        ]) });
      }
      function existingDialog() {
        const candidates = all.filter((a) => a.site === selected.site && !(a.pool_ids || []).includes(selected.id));
        if (!candidates.length) { UI.toast('没有可加入的同站点账号；请先打开登录页添加', 'bad'); return; }
        const ctl = UI.select(candidates.map((a) => ({ value: String(a.id), label: `${a.label} · ${login[a.login_status] || '待检测'}` })), String(candidates[0].id), () => {});
        const target = selected.id;
        const m = modal({ title: '加入已有账号', body: U.frag([UI.field('同站点账号', ctl), row([btn('加入本池', async () => {
          try { await API.addPoolMember(target, Number(ctl.value)); m.close(); await draw(); } catch (e) { fail(e); }
        }, 'primary'), btn('取消', () => m.close())])]) });
      }
      function removeDialog(account) {
        const target = selected.id;
        const m = modal({ title: `移出本池 · ${account.label}`, body: U.frag([
          U.el('p', { text: '只移除当前池的成员关系。账号凭据、使用历史及其他池的成员关系保留。' }),
          row([btn('确认移出', async () => {
            try { await API.removePoolMember(target, account.id); m.close(); await draw(); } catch (e) { fail(e); }
          }, 'danger'), btn('取消', () => m.close())]),
        ]) });
      }
      async function history(account) {
        try {
          const data = await API.accountHistory(account.id);
          if (destroyed) return;
          modal({ title: `使用历史 · ${account.label}`, body: data.history.length ? UI.table({ rows: data.history, pageSize: 10, columns: [
            { key: 'used_at', label: '时间', render: (r) => U.ts(r.used_at, true) },
            { key: 'ok', label: '结果', get: (r) => r.ok ? '成功' : '失败' }, { key: 'reason', label: '说明' }, { key: 'task_id', label: '任务' },
          ] }) : UI.empty('∅', '暂无使用记录') });
        } catch (e) { fail(e); }
      }
      draw();
      return { destroy() { destroyed = true; clearTimeout(loginTimer); dialogs.forEach((m) => m.close()); } };
    },
  });
})(window.ZF = window.ZF || {});
