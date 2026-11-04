# -*- coding: utf-8 -*-
"""
HTTP 层端到端验收
=================

不是在进程内调函数, 而是像浏览器那样**通过 HTTP 接口**完成整套流程:

  1. 探测后端
  2. 读爬虫清单
  3. POST 派活 → 拿 task_id
  4. GET SSE 流, 实时收日志
  5. 轮询任务状态直到结束
  6. 查数据接口, 确认入库
  7. 跑 SQL 接口, 验证查询
  8. 检查图表所需的数据字段是否齐全

这才是真正的"能不能用"验证。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent


def find_base() -> str:
    for p in range(8765, 8790):
        try:
            r = requests.get(f"http://127.0.0.1:{p}/api/health", timeout=2)
            if r.status_code == 200:
                return f"http://127.0.0.1:{p}"
        except Exception:
            continue
    raise SystemExit("找不到运行中的服务，请先启动: python 05_Web服务/run.py")


def sec(n, title):
    print(f"\n{'─'*72}\n[{n}] {title}\n{'─'*72}")


def main() -> int:
    base = find_base()
    print("=" * 72)
    print(f" HTTP 端到端验收 · {base}")
    print("=" * 72)
    fails = []

    # ---------- 1 ----------
    sec(1, "探测后端")
    h = requests.get(f"{base}/api/health", timeout=10).json()
    print(f"    Python {h['python']} · 版本 {h['version']} · 运行 {h['uptime_s']}s")
    print(f"    爬虫 {h['crawlers']} 个 · 依赖缺失: "
          f"{[k for k, v in h['deps'].items() if not v] or '无'}")
    if not h.get("ok"):
        fails.append("健康检查未通过")

    # ---------- 2 ----------
    sec(2, "读爬虫清单")
    cr = requests.get(f"{base}/api/crawlers", timeout=10).json()
    for c in cr["crawlers"]:
        print(f"    · {c['name']:<16} id={c['id']:<10} 参数 {len(c['params'])} 个 "
              f"账号={'是' if c['needs_account'] else '否'}")
        if c.get("problems"):
            fails.append(f"{c['id']} 配置有问题: {c['problems']}")
    if cr.get("errors"):
        for e in cr["errors"]:
            print(f"    [!] {e['source']}: {e['error']}")
    target = next((c for c in cr["crawlers"] if c["id"] == "bilibili"), None)
    if not target:
        fails.append("没有 bilibili 爬虫")
        return report(fails)

    # ---------- 3 ----------
    sec(3, "派活 (POST /api/tasks)")
    payload = {"crawler": "bilibili",
               "params": {"keyword": sys.argv[1] if len(sys.argv) > 1 else "数据可视化",
                          "pages": 1, "detail_limit": 12, "min_views": 0}}
    print(f"    请求: {json.dumps(payload, ensure_ascii=False)}")
    r = requests.post(f"{base}/api/tasks", json=payload, timeout=20)
    if r.status_code != 202:
        print(f"    [×] HTTP {r.status_code}: {r.text[:200]}")
        return report(["派活失败"])
    task = r.json()
    tid = task["task_id"]
    print(f"    [√] 任务号 {tid}  状态 {task['state']}")

    # ---------- 4 ----------
    sec(4, "SSE 实时日志 (GET /api/tasks/{id}/logs)")
    logs = []
    states = []
    done = {"hit": False, "data": None}
    t0 = time.time()

    try:
        with requests.get(f"{base}/api/tasks/{tid}/logs", stream=True, timeout=600) as resp:
            print(f"    Content-Type: {resp.headers.get('content-type')}")
            event = None
            for raw in resp.iter_lines(decode_unicode=True):
                if raw is None:
                    continue
                if raw.startswith("event:"):
                    event = raw.split(":", 1)[1].strip()
                elif raw.startswith("data:"):
                    try:
                        data = json.loads(raw[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if event == "log":
                        logs.append(data)
                        mark = {"INFO": "·", "OK": "√", "WARN": "!", "ERR": "×"}.get(data["level"], "·")
                        print(f"      {mark} [{data['level']:<4}] {data['msg']}")
                    elif event == "state":
                        states.append(data)
                    elif event == "done":
                        done["hit"] = True
                        done["data"] = data
                        print(f"      ── 结束事件: state={data['state']} items={data.get('items')}")
                        break
                if time.time() - t0 > 540:
                    print("      [!] 超时")
                    break
    except Exception as e:
        print(f"    [×] SSE 出错: {type(e).__name__}: {e}")
        fails.append(f"SSE 失败: {e}")

    print(f"\n    收到日志 {len(logs)} 条 · 状态事件 {len(states)} 条 · 结束事件 {done['hit']}")
    if not logs:
        fails.append("SSE 没有收到任何日志")
    if not done["hit"]:
        fails.append("没有收到 done 事件")

    # ---------- 5 ----------
    sec(5, "任务最终状态")
    for _ in range(60):
        t = requests.get(f"{base}/api/tasks/{tid}", timeout=10).json()
        if t["state"] in ("success", "failed", "canceled"):
            break
        time.sleep(1)
    print(f"    状态   {t['state']}")
    print(f"    采集   {t['items']} 条")
    print(f"    耗时   {t['duration']}s")
    print(f"    错误   {t['errors']}")
    if t["error"]:
        print(f"    原因   {t['error']}")
    if t["state"] != "success":
        fails.append(f"任务状态是 {t['state']}")
    if t["items"] == 0:
        fails.append("任务采集 0 条")

    # ---------- 6 ----------
    sec(6, "数据入库 (GET /api/data/items)")
    items = requests.get(f"{base}/api/data/items",
                         params={"source": "bilibili", "limit": 500}, timeout=15).json()
    print(f"    总数 {items['total']} 条")
    if not items["rows"]:
        fails.append("数据库里查不到数据")
    else:
        print(f"\n    前 5 条:")
        for row in items["rows"][:5]:
            print(f"      · {row['title'][:52]}")
            print(f"          播放={int(row['metric'] or 0):>12,}  关键词={row['keyword']}")

    # ---------- 7 ----------
    sec(7, "SQL 接口 (POST /api/sql)")
    queries = [
        ("分组统计", "SELECT source, keyword, COUNT(*) AS 条数 FROM items GROUP BY source, keyword ORDER BY 条数 DESC"),
        ("聚合函数", "SELECT COUNT(*) AS 总数, ROUND(AVG(metric),0) AS 平均播放, "
                     "MAX(metric) AS 最高播放, SUM(metric) AS 总播放 FROM items WHERE source='bilibili'"),
        ("字符串处理", "SELECT substr(title,1,24) AS 短标题, metric FROM items ORDER BY metric DESC LIMIT 3"),
    ]
    for label, sql in queries:
        r = requests.post(f"{base}/api/sql", json={"source_id": "zforge", "sql": sql}, timeout=20)
        if r.status_code != 200:
            print(f"    [×] {label}: {r.json().get('error')}")
            fails.append(f"SQL {label} 失败")
            continue
        d = r.json()
        print(f"    [√] {label}: {d['row_count']} 行 · {d['elapsed_ms']}ms")
        if d["rows"]:
            print(f"        列 {d['columns']}")
            print(f"        首行 {d['rows'][0]}")

    # 安全性
    sec("7b", "SQL 安全约束")
    attacks = [
        ("DROP TABLE", "DROP TABLE items"),
        ("DELETE", "DELETE FROM items"),
        ("多语句", "SELECT 1; DROP TABLE items"),
        ("PRAGMA", "PRAGMA table_info(items)"),
        ("ATTACH", "ATTACH DATABASE 'x.db' AS x"),
    ]
    for label, sql in attacks:
        r = requests.post(f"{base}/api/sql", json={"source_id": "zforge", "sql": sql}, timeout=10)
        blocked = r.status_code != 200
        print(f"    {'[√]' if blocked else '[×]'} {label:<12} → {'已拦截' if blocked else '竟然执行了!'}")
        if not blocked:
            fails.append(f"SQL 安全漏洞: {label} 没有被拦截")

    # ---------- 8 ----------
    sec(8, "图表所需数据字段")
    if items["rows"]:
        row = items["rows"][0]
        extra = json.loads(row.get("extra_json") or "{}")
        need_norm = ["source", "title", "metric", "metric_name", "url", "crawled_at", "keyword"]
        need_extra = ["like", "coin", "favorite", "reply", "share", "duration_sec",
                      "like_rate", "triple_rate", "author", "danmaku", "detail_ok"]
        print("    归一化字段:")
        for k in need_norm:
            ok = row.get(k) is not None
            print(f"      {'[√]' if ok else '[×]'} {k:<14} {str(row.get(k))[:36]}")
            if not ok and k not in ("price",):
                fails.append(f"缺归一化字段 {k}")
        print("    原始字段 (extra_json):")
        for k in need_extra:
            ok = k in extra
            v = extra.get(k)
            print(f"      {'[√]' if ok else '[×]'} {k:<14} {v}")
            if not ok:
                fails.append(f"缺原始字段 {k}")

    # ---------- 9 ----------
    sec(9, "历史库可读 (为分析页提供数据)")
    for sid, name in (("taobao_legacy", "淘宝历史"), ("bili_legacy", "B站历史")):
        r = requests.get(f"{base}/api/data/{sid}/tables", timeout=10).json()
        tot = sum(t["rows"] for t in r["tables"] if t["rows"] > 0)
        print(f"    [√] {name:<10} {len(r['tables'])} 张表 · {tot} 行")

    # ---------- 10 ----------
    sec(10, "账号池")
    a = requests.get(f"{base}/api/accounts", timeout=10).json()
    s = a["summary"]
    print(f"    账号 {s['total']} 个 · 可用 {s['available']} · 站点 {s['sites']}")
    print(f"    状态分布 {s['by_state']}")
    for acc in a["accounts"]:
        print(f"      · #{acc['id']} {acc['label']}  {acc['state']}  "
              f"今日 {acc['used_today']}/{acc['daily_limit']}  成功率 {acc['success_rate']}%")
        if "credential" in acc:
            fails.append("账号接口泄漏了 credential 字段!")

    return report(fails)


def report(fails) -> int:
    print("\n" + "=" * 72)
    if fails:
        print(f" 验收结果: ❌ 有 {len(fails)} 个问题")
        for f in fails:
            print(f"   · {f}")
        print("=" * 72)
        return 1
    print(" 验收结果: ✅ 全部通过")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
