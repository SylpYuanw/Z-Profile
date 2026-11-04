/* 视频分析统一口径；缺失值保留为缺失，不当作零次互动。 */
(function (ZF) {
  'use strict';
  const U = ZF.util, UI = ZF.ui, Ch = ZF.charts;
  const actions = [['like', '点赞'], ['coin', '投币'], ['favorite', '收藏'], ['reply', '评论'], ['share', '分享']];
  const number = (v) => v === null || v === undefined || v === '' || !Number.isFinite(Number(v)) || Number(v) < 0 ? null : Number(v);
  const V = {};
  V.model = (rows) => {
    const videos = rows.map((r) => {
      let e = {}; try { e = JSON.parse(r.extra_json || '{}'); } catch (_) { /* 缺失详情按未采集处理。 */ }
      const item = { ...r, title: String(r.title || '未命名视频'), views: number(r.metric) };
      actions.forEach(([key]) => { item[key] = number(e[key]); });
      item.actionsPer100 = item.views > 0 && ['like', 'coin', 'favorite'].every((k) => item[k] !== null)
        ? (item.like + item.coin + item.favorite) / item.views * 100 : null;
      return item;
    });
    return { videos, comparable: videos.filter((v) => v.actionsPer100 !== null),
      totals: actions.map(([key, name]) => { const sample = videos.filter((v) => v[key] !== null); return { name, count: sample.length ? sample.reduce((sum, v) => sum + v[key], 0) : null, sample: sample.length }; }),
    };
  };
  V.render = (host, rows) => {
    const model = V.model(rows), videos = model.videos;
    if (!videos.length) { host.appendChild(UI.empty('∅', '这个数据集暂无视频', '返回选择其他数据集，或配置采集任务')); return; }
    const top = videos.filter((v) => v.views !== null).sort((a, b) => b.views - a.views).slice(0, 10).reverse();
    const rank = model.comparable.slice().sort((a, b) => b.actionsPer100 - a.actionsPer100).slice(0, 10).reverse();
    const plays = Ch.box(350), totals = Ch.box(260), interaction = Ch.box(350);
    host.appendChild(UI.grid(3, [
      UI.kpi('当前数据集', `${U.int(videos.length)} 个视频`, '只统计当前选择的数据集'),
      UI.kpi('总播放次数', U.smart(videos.reduce((sum, v) => sum + (v.views || 0), 0)), '播放次数不是独立观看人数'),
      UI.kpi('可比较互动的视频', `${U.int(model.comparable.length)} / ${U.int(videos.length)}`, '播放量大于零，且点赞、投币、收藏均已采集'),
    ]));
    host.appendChild(U.el('div', { class: 'analysis-stack' }, [
      UI.card({ title: '哪些视频播放次数最多？', sub: '当前数据集前 10 名；悬停查看完整标题和播放次数', body: plays.box }),
      UI.card({ title: '观众主要进行了哪些互动？', sub: '每种动作分别累计；同一个人可以执行多个动作，因此这些数字不是人数。', body: U.frag([
        totals.box,
        UI.table({ pageSize: 0, rows: model.totals, columns: [
          { key: 'name', label: '互动动作' }, { key: 'count', label: '累计次数', get: (r) => r.count === null ? '未采集' : U.int(r.count) },
          { key: 'sample', label: '有该字段的视频数', get: (r) => `${r.sample} / ${videos.length}` },
        ] }),
        U.el('p', { class: 'field-help', text: '用于比较本批数据中的动作总量。各字段覆盖视频数不同时，应结合覆盖数阅读，不能直接推断观众偏好。' }),
      ]) }),
      UI.card({ title: '哪些视频相对播放次数获得了更多互动？',
        sub: '每 100 次播放的三类互动次数 =（点赞 + 投币 + 收藏）÷ 播放次数 × 100',
        body: rank.length ? U.frag([
          interaction.box,
          U.el('p', { class: 'metric-example', text: '例如：1,000 次播放、100 次点赞、20 次投币、30 次收藏，得到每 100 次播放 15 次互动。一个人可重复或执行多种动作，因此不代表 15% 的观众参与，也不代表同时完成“三连”。' }),
          UI.table({ pageSize: 10, rows: model.comparable.slice().sort((a, b) => b.actionsPer100 - a.actionsPer100), columns: [
            { key: 'title', label: '视频标题', width: '38%' }, { key: 'views', label: '播放次数', get: (v) => U.int(v.views) },
            { key: 'like', label: '点赞', get: (v) => U.int(v.like) }, { key: 'coin', label: '投币', get: (v) => U.int(v.coin) },
            { key: 'favorite', label: '收藏', get: (v) => U.int(v.favorite) }, { key: 'actionsPer100', label: '每百次播放互动', get: (v) => v.actionsPer100.toFixed(2) + ' 次' },
          ] }),
          U.el('p', { class: 'field-help', text: '用于找出相对播放量互动较多的视频。低播放量的比值容易波动，请同时比较播放次数；这项指标不能证明内容质量或因果关系。' }),
        ]) : UI.empty('∅', '缺少可比较的完整互动数据', '需要同一视频的播放量、点赞、投币和收藏'),
      }),
    ]));
    requestAnimationFrame(() => {
      if (!host.isConnected) return;
      if (top.length) Ch.hbar(plays.canvas, top.map((v) => v.title.slice(0, 24)), top.map((v) => v.views), { label: '播放次数', fmt: U.smart, fullLabels: top.map((v) => v.title) });
      const collected = model.totals.filter((r) => r.count !== null);
      if (collected.length) Ch.hbar(totals.canvas, collected.map((r) => r.name), collected.map((r) => r.count), { label: '互动次数', fmt: U.int });
      if (rank.length) Ch.hbar(interaction.canvas, rank.map((v) => v.title.slice(0, 24)), rank.map((v) => v.actionsPer100), { label: '每百次播放互动次数', fmt: (v) => Number(v).toFixed(1) + ' 次', fullLabels: rank.map((v) => v.title) });
    });
  };
  ZF.videos = V;
})(window.ZF = window.ZF || {});
