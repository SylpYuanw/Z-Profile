# -*- coding: utf-8 -*-
"""
端到端验收测试
==============

不启动 Web 服务, 直接跑完整链路:

    注册表 → 调度内核 → 爬虫 → 归一化入库 → 查询

这是最有价值的一个测试: 它证明"真抓 -> 真存 -> 真查"整条路是通的。

用法:
    python 07_工具/test_e2e.py bilibili "python" 1 20
    python 07_工具/test_e2e.py taobao   "显示器" 1
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "05_Web服务"))

from server.core.accounts import AccountPool            # noqa: E402
from server.core.logbus import LogBus                   # noqa: E402
from server.core.registry import Registry, default_crawlers_root  # noqa: E402
from server.core.taskrunner import FINISHED, TaskError, TaskRunner  # noqa: E402
from server.store.db import Store                       # noqa: E402

WEB_DB = ROOT / "05_Web服务" / "data" / "zforge_e2e.db"
ACCOUNT_DB = ROOT / "02_账号池" / "数据" / "accounts.db"


def main() -> int:
    crawler_id = sys.argv[1] if len(sys.argv) > 1 else "bilibili"
    keyword = sys.argv[2] if len(sys.argv) > 2 else "python"
    pages = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    detail = int(sys.argv[4]) if len(sys.argv) > 4 else 20

    print("=" * 74)
    print(f" 端到端验收 · 爬虫={crawler_id} 关键词=『{keyword}』 页数={pages} 补详情={detail}")
    print("=" * 74)

    # ---------- 1. 注册表 ----------
    print("\n[1] 扫描爬虫")
    reg = Registry(default_crawlers_root()).scan()
    print(f"    已注册 {len(reg)} 个: {', '.join(c.id for c in reg.items)}")
    for e in reg.errors:
        print(f"    [!] {Path(e['source']).name}: {e['error']}")
    if crawler_id not in reg:
        print(f"    [×] 没有找到爬虫 {crawler_id}")
        return 1
    info = reg.require(crawler_id)
    print(f"    META: {info.meta['name']} · 需要账号={info.meta.get('needs_account')}")
    print(f"    参数声明 {len(info.meta['params'])} 个: "
          f"{[p['key'] for p in info.meta['params']]}")

    # ---------- 2. 组装 ----------
    print("\n[2] 组装调度内核")
    if WEB_DB.exists():
        WEB_DB.unlink()
    store = Store(WEB_DB)
    bus = LogBus()
    pool = AccountPool(ACCOUNT_DB)
    counters = {"items": 0, "inserted": 0}

    def on_item(task, item):
        kw = str(item.get("keyword") or task.params.get("keyword") or "")
        ins = store.insert_item(source=task.crawler_id, item=item,
                                task_id=task.task_id, keyword=kw)
        store.insert_raw(task.crawler_id, item, task_id=task.task_id, keyword=kw)
        counters["items"] += 1
        if ins:
            counters["inserted"] += 1

    runner = TaskRunner(reg, bus, account_pool=pool, on_item=on_item,
                        on_task_change=lambda t: store.save_task(t.to_dict()))
    print(f"    数据库 {WEB_DB}")
    print(f"    账号池 {[a.label for a in pool.list()]}")

    # ---------- 3. 派活 ----------
    print(f"\n[3] 派活")
    params = {"keyword": keyword, "pages": pages}
    if crawler_id == "bilibili":
        params["detail_limit"] = detail
    try:
        task = runner.submit(crawler_id, params)
    except TaskError as e:
        print(f"    [×] 派活失败: {e}")
        return 1
    print(f"    任务号 {task.task_id}")
    print(f"    参数   {task.params}")

    # ---------- 4. 实时日志 ----------
    print(f"\n[4] 实时日志 (每行都是爬虫真写出来的)")
    print("    " + "-" * 68)
    shown = 0
    last_state = ""
    t0 = time.time()
    while task.state not in FINISHED:
        for line in bus.history(task.task_id)[shown:]:
            lvl = line["level"]
            mark = {"INFO": "·", "OK": "√", "WARN": "!", "ERR": "×", "SQL": "$"}.get(lvl, "·")
            print(f"    {mark} [{lvl:<4}] {line['msg']}")
            shown += 1
        st = bus.last_state(task.task_id)
        if st and st["message"] and st["message"] != last_state and st.get("progress"):
            last_state = st["message"]
        if time.time() - t0 > 600:
            print("    [×] 超时 600 秒，放弃")
            runner.cancel(task.task_id)
            break
        time.sleep(0.4)

    for line in bus.history(task.task_id)[shown:]:
        mark = {"INFO": "·", "OK": "√", "WARN": "!", "ERR": "×"}.get(line["level"], "·")
        print(f"    {mark} [{line['level']:<4}] {line['msg']}")
    print("    " + "-" * 68)

    # ---------- 5. 结果 ----------
    print(f"\n[5] 任务结果")
    print(f"    状态     {task.state}")
    print(f"    耗时     {task.duration}s")
    print(f"    采集     {task.items} 条")
    print(f"    入库     {counters['inserted']} 条 (去重后)")
    print(f"    错误     {task.errors}")
    if task.error:
        print(f"    失败原因 {task.error}")

    # ---------- 6. 查库验证 ----------
    print(f"\n[6] 数据库验证")
    stats = store.stats()
    print(f"    items 总数      {stats['items_total']}")
    print(f"    按来源          {stats['items_by_source']}")
    print(f"    按关键词        {stats['items_by_keyword']}")
    print(f"    数据库大小      {stats['db_size']:,} B")

    if stats["items_total"] == 0:
        print("\n    [×] 数据库是空的 —— 采集没成功")
        runner.shutdown()
        return 1

    print(f"\n[7] 取前 5 条看内容")
    rows = store.query_items(source=crawler_id, limit=5, order="metric")
    show_cols = ["title", "metric", "metric_name", "price", "keyword", "url"]
    for r in rows["rows"]:
        print(f"    · {r['title'][:52]}")
        vals = "  ".join(f"{c}={str(r[c])[:22]}" for c in show_cols[1:5] if r.get(c) is not None)
        print(f"        {vals}")

    # ---------- 8. 派生指标检查 ----------
    import json as _json
    print(f"\n[8] 派生字段检查 (证明原始字段没丢)")
    one = rows["rows"][0]
    extra = _json.loads(one.get("extra_json") or "{}")
    print(f"    extra 字段: {sorted(extra.keys())[:12]}")
    if crawler_id == "bilibili":
        for k in ("like", "coin", "favorite", "like_rate", "triple_rate"):
            print(f"      {k:<14} {extra.get(k)}")

    # ---------- 9. SQL 实验室等价查询 ----------
    print(f"\n[9] SQL 查询验证")
    import sqlite3
    conn = sqlite3.connect(f"file:{WEB_DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sql = ("SELECT source, keyword, COUNT(*) n, ROUND(AVG(metric),0) avg_metric "
           "FROM items GROUP BY source, keyword ORDER BY n DESC")
    print(f"    {sql}")
    for r in conn.execute(sql):
        print(f"      {dict(r)}")
    conn.close()

    runner.shutdown()
    store.close()
    pool.close()

    print("\n" + "=" * 74)
    ok = task.state == "success" and counters["inserted"] > 0
    print(f" 验收结果: {'✅ 通过' if ok else '❌ 未通过'}")
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
