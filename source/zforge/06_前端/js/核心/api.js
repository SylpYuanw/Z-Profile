/* ============================================================================
   ZForge · 接口层
   ----------------------------------------------------------------------------
   所有 fetch 调用集中在这里。视图层不直接调 fetch。

   同时负责"后端可用性探测": 探测失败时切到离线模式 (静态展示版)。
   ========================================================================== */
(function (ZF) {
  'use strict';

  const API = {
    base: '',
    online: false,
    health: null,
    _checked: false,
  };

  async function req(method, path, body, opts) {
    const o = Object.assign({ method, headers: {} }, opts || {});
    if (body !== undefined) {
      o.headers['Content-Type'] = 'application/json';
      o.body = JSON.stringify(body);
    }
    const res = await fetch(API.base + path, o);
    let data = null;
    const ct = res.headers.get('content-type') || '';
    if (ct.includes('application/json')) {
      data = await res.json().catch(() => null);
    } else {
      data = await res.text().catch(() => null);
    }
    if (!res.ok) {
      const msg = (data && data.error) || `HTTP ${res.status}`;
      const err = new Error(msg);
      err.status = res.status;
      err.detail = data && data.detail;
      throw err;
    }
    return data;
  }

  const get = (p) => req('GET', p);
  const post = (p, b) => req('POST', p, b || {});
  const del = (p) => req('DELETE', p);

  /* ---------------- 健康 ----------------
     启动时调一次。失败就进离线模式, 页面照常能看数据,
     只是不能派活 —— 这是"静态展示版"的工作方式。 */
  API.check = async function () {
    try {
      API.health = await get('/api/health');
      API.online = true;
    } catch (e) {
      API.online = false;
      API.health = null;
    }
    API._checked = true;
    return API.online;
  };

  /* ---------------- 爬虫 ---------------- */
  API.crawlers = () => get('/api/crawlers');
  API.reloadCrawlers = () => post('/api/crawlers/reload');

  /* ---------------- 任务 ---------------- */
  API.createTask = (crawler, params, accountPoolId) => post('/api/tasks', { crawler, params, account_pool_id: accountPoolId || null });
  API.tasks = (q) => get('/api/tasks' + (q ? '?' + new URLSearchParams(q) : ''));
  API.task = (id) => get(`/api/tasks/${id}`);
  API.cancelTask = (id) => post(`/api/tasks/${id}/cancel`);
  API.deleteTask = (id) => del(`/api/tasks/${id}`);
  API.recentLogs = (q) => get('/api/logs/recent' + (q ? '?' + new URLSearchParams(q) : ''));

  /** SSE 日志流。返回一个可 close 的句柄。 */
  API.taskStream = function (taskId, handlers) {
    const h = handlers || {};
    const es = new EventSource(`${API.base}/api/tasks/${taskId}/logs`);
    es.addEventListener('log', (e) => h.onLog && h.onLog(JSON.parse(e.data)));
    es.addEventListener('state', (e) => h.onState && h.onState(JSON.parse(e.data)));
    es.addEventListener('done', (e) => {
      if (h.onDone) h.onDone(JSON.parse(e.data));
      es.close();
    });
    es.onerror = () => { if (h.onError) h.onError(); };
    return { close: () => es.close(), source: es };
  };

  /* ---------------- 数据 ---------------- */
  API.sources = () => get('/api/data/sources');
  API.projects = () => get('/api/data/projects');
  API.projectDataset = (project, dataset) => get(`/api/data/projects/${encodeURIComponent(project)}/datasets/${encodeURIComponent(dataset)}`);
  API.tables = (sid) => get(`/api/data/${sid}/tables`);
  API.rows = (sid, q) => get(`/api/data/${sid}/rows?` + new URLSearchParams(q));
  API.items = (q) => get('/api/data/items' + (q ? '?' + new URLSearchParams(q) : ''));
  API.dataStats = () => get('/api/data/stats');
  API.runSQL = (sourceId, sql, limit) => post('/api/sql', { source_id: sourceId, sql, limit });

  /* ---------------- 账号池 ---------------- */
  API.accounts = (site) => get('/api/accounts' + (site ? '?site=' + encodeURIComponent(site) : ''));
  API.accountHistory = (id) => get(`/api/accounts/${id}/history`);
  API.setAccountState = (id, state, note) => post(`/api/accounts/${id}/state`, { state, note });
  API.deleteAccount = (id) => del(`/api/accounts/${id}`);
  API.updateAccountProfile = (id, body) => req('PATCH', `/api/accounts/${id}`, body);
  API.accountLogins = () => get('/api/account-logins');
  API.startAccountLogin = (body) => post('/api/account-logins', body);
  API.cancelAccountLogin = (id) => post(`/api/account-logins/${id}/cancel`);
  API.accountPools = (site) => get('/api/account-pools' + (site ? '?site=' + encodeURIComponent(site) : ''));
  API.accountPool = (id) => get(`/api/account-pools/${id}`);
  API.createAccountPool = (body) => post('/api/account-pools', body);
  API.updateAccountPool = (id, body) => req('PATCH', `/api/account-pools/${id}`, body);
  API.addPoolMember = (id, aid) => post(`/api/account-pools/${id}/members`, { account_id: aid });
  API.removePoolMember = (id, aid) => del(`/api/account-pools/${id}/members/${aid}`);
  API.importPoolAccounts = (id, files, label, updateId) => {
    const data = new FormData();
    Array.from(files).forEach((file) => data.append('files', file));
    if (label) data.append('label', label);
    if (updateId) data.append('update_account_id', String(updateId));
    return req('POST', `/api/account-pools/${id}/import`, undefined, { body: data });
  };

  /* ---------------- 工作流 ---------------- */
  API.workflows = () => get('/api/workflows');
  API.saveWorkflow = (wf) => post('/api/workflows', wf);
  API.deleteWorkflow = (id) => del(`/api/workflows/${id}`);
  API.runWorkflow = (id) => post(`/api/workflows/${id}/run`);
  API.workflowRuns = () => get('/api/workflows/runs');
  API.cancelWorkflowRun = (id) => post(`/api/workflows/runs/${id}/cancel`);

  /* ---------------- 系统 ---------------- */
  API.systemInfo = () => get('/api/system/info');

  ZF.api = API;
})(window.ZF = window.ZF || {});
