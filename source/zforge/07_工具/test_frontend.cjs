// 按实际 HTML 的脚本顺序加载，防止视图注册早于初始化。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..', '06_前端');
const document = { readyState: 'loading', addEventListener() {} };
const window = {};
const context = vm.createContext({ window, document, console, URLSearchParams, setTimeout, clearTimeout, requestAnimationFrame() {} });
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
for (const match of html.matchAll(/<script src="(js\/[^"]+)"/g)) {
  vm.runInContext(fs.readFileSync(path.join(root, match[1]), 'utf8'), context, { filename: match[1] });
}
assert.deepEqual(Array.from(window.ZF.screens, (s) => s.id).sort(),
  ['accounts', 'analysis', 'crawler', 'dashboard', 'data', 'logs', 'sql', 'system', 'taskflow']);
console.log('PASS: 实际脚本顺序下 9 个视图全部注册');

async function verifyHistoricalAnalysis() {
  const ZF = window.ZF;
  ZF.store.items = [{ source: 'bilibili', title: '测试视频', metric: 10 }];
  ZF.store.itemsTotal = 1;
  const table = { name: '历史商品', columns: ['title', 'price', 'pay_count'].map((name) => ({ name })) };
  ZF.api.tables = async () => ({ tables: [table, { ...table, name: '历史商品_detail' }] });
  ZF.api.rows = async (source, query) => {
    assert.equal(source, 'taobao_legacy');
    assert.equal(query.table, '历史商品');
    return { columns: ['title', 'price', 'pay_count', 'url'], rows: [['测试商品', 20, 5, 'https://example.invalid']], total: 1 };
  };
  await ZF.store.loadTaobaoHistory();
  assert.equal(ZF.store.taobaoSummary().n, 1);
  assert.equal(ZF.store.taobaoSummary().legacy, true);
  assert.equal(ZF.store.biliSummary().n, 1);
  assert.equal(ZF.store.itemsTotal, 1);
  ZF.store.items.push({ source: 'taobao', title: '新采集商品', metric: 8, price: 25 });
  assert.equal(ZF.store.taobaoSummary().legacy, false);
  assert.equal(ZF.store.taobaoSummary().rows[0].title, '新采集商品');
  console.log('PASS: 历史分析保留来源、排除详情副表，且不覆盖全局数据');
}

// 在视图的真实调用处验证编辑闭包、数组字段映射及查询预设，图表交给浏览器验收。
async function verifyViewInteractions() {
  const ZF = window.ZF, nodes = [], tables = [];
  const el = (tag, attrs = {}, children = []) => {
    const node = { tag, attrs, style: {}, value: attrs.value || '', isConnected: true, children: [],
      appendChild(child) { this.children.push(child); return child; },
      append(...items) { this.children.push(...items); },
      addEventListener(name, fn) { this.attrs['on' + name] = fn; },
      setAttribute(name, value) { this.attrs[name] = value; }, remove() { this.isConnected = false; } };
    node.append(...(Array.isArray(children) ? children : [children]));
    nodes.push(node);
    return node;
  };
  Object.assign(ZF.util, { el, clear(node) { node.children = []; }, frag: (items) => el('fragment', {}, items),
    append: (node, items) => node.append(...items) });
  Object.assign(document, { documentElement: { style: { setProperty() {} } },
    getElementById: (id) => nodes.findLast((n) => n.attrs.id === id) });
  for (const name of ['view', 'grid', 'kpi', 'kpiAnim', 'tag', 'stateTag', 'empty', 'segmented']) {
    ZF.ui[name] = () => el(name);
  }
  Object.assign(ZF.ui, { card: (o) => el('card', {}, o.body), section: (a, b, body) => el('section', {}, body),
    field: (a, ctl) => ctl, select: () => el('select'), table: (o) => { tables.push(o); return el('table'); },
    toast() {}, download(name, content) { ZF.testDownload = content; } });
  ZF.api.tasks = async () => ({ tasks: [], counts: {} });
  ZF.api.accountPools = async () => ({ pools: [{ id: 11, site: 'taobao', name: '隔离账号池', enabled: true, is_default: true, available: 1, members: 1 }] });
  ZF.api.workflows = async () => ({ workflows: [{ id: 'test', name: '编辑测试',
    steps: [{ crawler: 'taobao', params: { keyword: 'python' }, account_pool_id: 11, on_fail: 'abort' }] }], runs: [] });
  ZF.store.crawlers = [{ id: 'bilibili', name: 'B站', params: [{ key: 'keyword', type: 'text' }] },
    { id: 'taobao', name: '淘宝', needs_account: true, account_site: 'taobao', params: [{ key: 'keyword', label: '搜索关键词', type: 'text' }] }];
  const workflow = ZF.screens.find((s) => s.id === 'taskflow').render(el('host'), {});
  await new Promise((resolve) => setImmediate(resolve));
  nodes.find((n) => n.attrs.text === '载入编辑').attrs.onclick();
  assert.equal(nodes.find((n) => n.attrs.placeholder?.startsWith('工作流名称')).value, '编辑测试');
  const keyword = nodes.findLast((n) => n.attrs['aria-label'] === '第1步 搜索关键词');
  keyword.value = '输入即保存'; keyword.attrs.oninput();
  let saved = null, started = false;
  ZF.api.saveWorkflow = async (wf) => { saved = JSON.parse(JSON.stringify(wf)); };
  ZF.api.runWorkflow = async () => { started = true; };
  await nodes.find((n) => n.attrs.text === '仅保存配置').attrs.onclick();
  assert.equal(saved.id, 'test');
  assert.equal(saved.steps[0].params.keyword, '输入即保存');
  assert.equal(saved.steps[0].account_pool_id, 11);
  assert.equal(started, false);
  workflow.destroy();
  console.log('PASS: 工作流编辑、输入同步、保存账号池配置且不执行采集');

  assert.ok(ZF.store.SQL_PRESETS.every((p) => ['zforge', 'bili_legacy', 'taobao_legacy'].includes(p.source || 'zforge')));
  assert.ok(!ZF.store.SQL_PRESETS.some((p) => /LLM|request_records|circuit_states/.test(p.label + p.sql)));
  ZF.api.projects = async () => ({ projects: [
    { id: 'bilibili', name: 'B站视频', crawler_id: 'bilibili', datasets: [] },
    { id: 'taobao', name: '淘宝商品', crawler_id: 'taobao', datasets: [] },
  ] });
  console.log('PASS: SQL 预设仅包含采集数据，不接入模型调用记录');

  const calls = [];
  ZF.api.sources = async () => ({ sources: [{ id: 'zforge', exists: true }, { id: 'bili_legacy', exists: true }] });
  ZF.api.runSQL = async (source) => { calls.push(source); return { columns: ['名称', '数量'], rows: [['测试结果', 3]], row_count: 1 }; };
  ZF.screens.find((s) => s.id === 'sql').render(el('host'));
  nodes.findLast((n) => n.attrs.text === '历史库: B站PYTHON类目').attrs.onclick();
  await new Promise((resolve) => setImmediate(resolve));
  nodes.findLast((n) => n.attrs.text === '各来源采集量').attrs.onclick();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls, ['bili_legacy', 'zforge']);
  nodes.findLast((n) => n.attrs.text === '导出 CSV').attrs.onclick();
  assert.ok(ZF.testDownload.includes('测试结果,3'));
  console.log('PASS: SQL 预设切换数据源与 CSV 内容');

  let loadedDetail = false;
  ZF.api.projectDataset = async () => { loadedDetail = true; };
  const chooser = ZF.screens.find((s) => s.id === 'analysis').render(el('host'), {});
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(loadedDetail, false);
  chooser.destroy();
  const metrics = ZF.videos.model([
    { title: '示例', metric: 1000, extra_json: JSON.stringify({ like: 100, coin: 20, favorite: 30 }) },
    { title: '零播放', metric: 0, extra_json: JSON.stringify({ like: 0, coin: 0, favorite: 0 }) },
    { title: '缺少收藏', metric: 100, extra_json: JSON.stringify({ like: 5, coin: 2 }) },
  ]);
  assert.equal(metrics.comparable.length, 1);
  assert.equal(metrics.comparable[0].actionsPer100, 15);
  assert.equal(metrics.totals.find((r) => r.name === '评论').count, null);
  assert.equal(metrics.totals.find((r) => r.name === '收藏').sample, 2);
  console.log('PASS: 分析入口不默认打开 B站；互动口径排除缺失和零播放，不将次数当人数');
}
verifyHistoricalAnalysis().then(verifyViewInteractions).catch((error) => { console.error(error); process.exitCode = 1; });
