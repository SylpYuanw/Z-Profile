# -*- coding: utf-8 -*-
"""
统一数据层
==========

所有爬虫采到的数据都进这里。分两张表存:

  items          归一化表 —— 跨网站可 JOIN, 分析页和 SQL 实验室用它
  <crawler>_raw  各爬虫原始表 —— 完整字段, 不丢信息

为什么两张都要:
  · 归一化才能做跨站点分析 (比如"B站和淘宝哪个关键词更热")
  · 但归一化一定会丢字段, 原始表兜底

SQLite 用 WAL 模式 + 显式关连接 (Windows 上不关会锁文件)。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable


SCHEMA = """
-- ============ 归一化表: 跨网站分析用 ============
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT    NOT NULL,        -- bilibili / taobao / ...
    task_id     TEXT,
    workflow_id TEXT,
    keyword     TEXT,                    -- 采集时用的关键词
    title       TEXT,
    price       REAL,                    -- 价格 (已清洗成数值)
    metric      REAL,                    -- 主指标: 播放量 / 销量
    metric_name TEXT,                    -- 主指标叫什么
    url         TEXT,
    extra_json  TEXT,                    -- 该爬虫特有的字段
    crawled_at  INTEGER NOT NULL,
    UNIQUE(source, url, title)
);
CREATE INDEX IF NOT EXISTS idx_items_source   ON items(source);
CREATE INDEX IF NOT EXISTS idx_items_keyword  ON items(source, keyword);
CREATE INDEX IF NOT EXISTS idx_items_crawled  ON items(crawled_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_metric   ON items(metric DESC);

-- ============ 任务历史 ============
CREATE TABLE IF NOT EXISTS task_history (
    task_id      TEXT PRIMARY KEY,
    crawler      TEXT NOT NULL,
    crawler_name TEXT,
    params_json  TEXT,
    state        TEXT NOT NULL,
    progress     REAL DEFAULT 0,
    message      TEXT DEFAULT '',
    items        INTEGER DEFAULT 0,
    errors       INTEGER DEFAULT 0,
    error        TEXT DEFAULT '',
    account      TEXT DEFAULT '',
    workflow_id  TEXT,
    created_at   INTEGER,
    started_at   INTEGER,
    finished_at  INTEGER,
    duration     REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_task_crawler ON task_history(crawler, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_state   ON task_history(state);

-- ============ 工作流 ============
CREATE TABLE IF NOT EXISTS workflows (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    steps_json  TEXT NOT NULL,          -- [{crawler, params, on_fail}]
    created_at  INTEGER,
    updated_at  INTEGER
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id      TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    state       TEXT NOT NULL,          -- running | success | failed | canceled
    current_step INTEGER DEFAULT 0,
    total_steps INTEGER DEFAULT 0,
    task_ids    TEXT DEFAULT '[]',
    payload_json TEXT DEFAULT '{}',
    started_at  INTEGER,
    finished_at INTEGER,
    error       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_wrun ON workflow_runs(workflow_id, started_at DESC);
"""


class Store:
    """数据访问层。线程安全。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    # ---------------- 基础设施 ----------------
    @contextmanager
    def _conn(self, *, readonly: bool = False):
        """
        每次操作独立连接, 用完立刻关。

        `with sqlite3.connect()` 只管事务, 不关连接 —— Windows 上会锁文件。
        """
        if readonly:
            uri = f"file:{self.db_path.as_posix()}?mode=ro"
            c = sqlite3.connect(uri, uri=True, timeout=15)
        else:
            c = sqlite3.connect(self.db_path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            if not readonly:
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA synchronous=NORMAL")
            yield c
            if not readonly:
                c.commit()
        except Exception:
            if not readonly:
                c.rollback()
            raise
        finally:
            c.close()

    def _init(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)
            columns = {r[1] for r in c.execute("PRAGMA table_info(workflow_runs)")}
            if "payload_json" not in columns:
                c.execute("ALTER TABLE workflow_runs ADD COLUMN payload_json TEXT DEFAULT '{}'")
            task_columns = {r[1] for r in c.execute("PRAGMA table_info(task_history)")}
            for name in ("account_pool_id", "account_id"):
                if name not in task_columns:
                    c.execute(f"ALTER TABLE task_history ADD COLUMN {name} INTEGER")

    def close(self) -> None:
        with self._conn() as c:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # ---------------- 归一化写入 ----------------
    # 每个爬虫声明自己的字段怎么映射到 items 表
    NORMALIZERS: dict[str, dict[str, Any]] = {
        "bilibili": {
            "title": ("title", "标题"),
            "metric": ("views", "播放量"),
            "price": None,
        },
        "taobao": {
            "title": ("title", "标题"),
            "metric": ("pay_count", "付款人数"),
            "price": ("price", "价格"),
        },
    }

    def insert_item(self, *, source: str, item: dict, task_id: str = "",
                    workflow_id: str = "", keyword: str = "") -> bool:
        """
        写入一条归一化记录。返回 True 表示新插入, False 表示已存在被跳过。

        归一化规则:
          · title  直接取
          · price  按爬虫声明取对应字段
          · metric 主指标 + 名字
          · 其余字段全塞进 extra_json
        """
        spec = self.NORMALIZERS.get(source, {})

        title = str(item.get("title") or item.get("标题") or "").strip()
        if not title:
            return False

        price = None
        p_spec = spec.get("price")
        if p_spec:
            raw = item.get(p_spec[0])
            try:
                price = float(raw) if raw is not None and str(raw) != "" else None
            except (TypeError, ValueError):
                price = None

        metric = None
        metric_name = ""
        m_spec = spec.get("metric")
        if m_spec:
            raw = item.get(m_spec[0])
            metric_name = m_spec[1]
            try:
                metric = float(raw) if raw is not None and str(raw) != "" else None
            except (TypeError, ValueError):
                metric = None

        url = str(item.get("url") or "").strip()
        # 已知字段之外的全部塞 extra
        known = {"title", "url"}
        extra = {k: v for k, v in item.items() if k not in known}

        with self._lock, self._conn() as c:
            try:
                c.execute(
                    """INSERT INTO items(source, task_id, workflow_id, keyword, title, price,
                                         metric, metric_name, url, extra_json, crawled_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (source, task_id, workflow_id, keyword, title, price,
                     metric, metric_name, url,
                     json.dumps(extra, ensure_ascii=False), int(time.time())),
                )
                return True
            except sqlite3.IntegrityError:
                # UNIQUE(source,url,title) 冲突 = 这条已经采过了
                return False

    def insert_items(self, source: str, items: Iterable[dict], **kw) -> tuple[int, int]:
        """批量写入。返回 (新增, 重复跳过)。"""
        new = dup = 0
        for it in items:
            if self.insert_item(source=source, item=it, **kw):
                new += 1
            else:
                dup += 1
        return new, dup

    # ---------------- 原始表 ----------------
    def ensure_raw_table(self, crawler_id: str, columns: list[str]) -> str:
        """
        给爬虫建自己的原始表。表名 `raw_<crawler>`, 列名按传入的来。
        已存在则不动 (不迁移, 免得踩坏数据)。
        """
        table = f"raw_{crawler_id}"
        cols_sql = ", ".join(f'"{c}" TEXT' for c in columns if c)
        with self._lock, self._conn() as c:
            c.execute(
                f'CREATE TABLE IF NOT EXISTS "{table}" ('
                f'  id INTEGER PRIMARY KEY AUTOINCREMENT,'
                f'  task_id TEXT, keyword TEXT, payload TEXT, crawled_at INTEGER'
                f')'
            )
        return table

    def insert_raw(self, crawler_id: str, item: dict, task_id: str = "", keyword: str = "") -> None:
        """把完整原始记录存一份 (JSON 塞 payload, 不丢字段)。"""
        table = f"raw_{crawler_id}"
        with self._lock, self._conn() as c:
            c.execute(
                f'CREATE TABLE IF NOT EXISTS "{table}" ('
                f'  id INTEGER PRIMARY KEY AUTOINCREMENT,'
                f'  task_id TEXT, keyword TEXT, payload TEXT, crawled_at INTEGER)'
            )
            c.execute(
                f'INSERT INTO "{table}"(task_id, keyword, payload, crawled_at) VALUES (?,?,?,?)',
                (task_id, keyword, json.dumps(item, ensure_ascii=False), int(time.time())),
            )

    # ---------------- 查询 ----------------
    def stats(self) -> dict:
        with self._conn(readonly=True) as c:
            total = c.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            by_source = [
                {"source": r[0], "n": r[1]}
                for r in c.execute(
                    "SELECT source, COUNT(*) FROM items GROUP BY source ORDER BY COUNT(*) DESC"
                )
            ]
            by_keyword = [
                {"source": r[0], "keyword": r[1], "n": r[2]}
                for r in c.execute(
                    "SELECT source, keyword, COUNT(*) FROM items GROUP BY source, keyword ORDER BY COUNT(*) DESC LIMIT 40"
                )
            ]
            recent = [
                dict(r) for r in c.execute(
                    "SELECT source, keyword, title, price, metric, metric_name, crawled_at "
                    "FROM items ORDER BY crawled_at DESC LIMIT 10"
                )
            ]
            tasks = c.execute("SELECT COUNT(*) FROM task_history").fetchone()[0]
        return {
            "items_total": total,
            "items_by_source": by_source,
            "items_by_keyword": by_keyword,
            "recent": recent,
            "tasks_total": tasks,
            "db_size": self.db_path.stat().st_size if self.db_path.exists() else 0,
        }

    def query_items(self, *, source: str | None = None, keyword: str | None = None,
                    q: str | None = None, order: str = "metric",
                    limit: int = 200, offset: int = 0) -> dict:
        allowed_order = {"metric", "price", "crawled_at", "title", "id"}
        if order not in allowed_order:
            order = "metric"
        sql = "SELECT * FROM items WHERE 1=1"
        args: list[Any] = []
        if source:
            sql += " AND source=?"
            args.append(source)
        if keyword:
            sql += " AND keyword=?"
            args.append(keyword)
        if q:
            sql += " AND title LIKE ?"
            args.append(f"%{q}%")

        with self._conn(readonly=True) as c:
            total = c.execute(sql.replace("SELECT *", "SELECT COUNT(*)"), args).fetchone()[0]
            sql += f" ORDER BY {order} DESC NULLS LAST LIMIT ? OFFSET ?"
            rows = c.execute(sql, args + [limit, offset]).fetchall()
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "rows": [dict(r) for r in rows],
        }

    # ---------------- 任务历史 ----------------
    def save_task(self, task_dict: dict) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO task_history(task_id, crawler, crawler_name, params_json, state,
                       progress, message, items, errors, error, account, workflow_id,
                       created_at, started_at, finished_at, duration, account_pool_id, account_id)
                   VALUES (:task_id,:crawler,:crawler_name,:params_json,:state,:progress,:message,
                           :items,:errors,:error,:account,:workflow_id,:created_at,:started_at,
                           :finished_at,:duration,:account_pool_id,:account_id)
                   ON CONFLICT(task_id) DO UPDATE SET
                       state=excluded.state, progress=excluded.progress, message=excluded.message,
                       items=excluded.items, errors=excluded.errors, error=excluded.error,
                       started_at=excluded.started_at, finished_at=excluded.finished_at,
                       duration=excluded.duration, account=excluded.account,
                       account_pool_id=excluded.account_pool_id, account_id=excluded.account_id""",
                {
                    "task_id": task_dict["task_id"],
                    "crawler": task_dict["crawler"],
                    "crawler_name": task_dict.get("crawler_name", ""),
                    "params_json": json.dumps(task_dict.get("params", {}), ensure_ascii=False),
                    "state": task_dict["state"],
                    "progress": task_dict.get("progress", 0),
                    "message": task_dict.get("message", ""),
                    "items": task_dict.get("items", 0),
                    "errors": task_dict.get("errors", 0),
                    "error": task_dict.get("error", ""),
                    "account": task_dict.get("account", ""),
                    "workflow_id": task_dict.get("workflow_id"),
                    "account_pool_id": task_dict.get("account_pool_id"),
                    "account_id": task_dict.get("account_id"),
                    "created_at": task_dict.get("created_at") or 0,
                    "started_at": task_dict.get("started_at") or 0,
                    "finished_at": task_dict.get("finished_at") or 0,
                    "duration": task_dict.get("duration") or 0,
                },
            )

    def task_history(self, limit: int | None = 100, crawler: str | None = None) -> list[dict]:
        sql = "SELECT * FROM task_history"
        args: list[Any] = []
        if crawler:
            sql += " WHERE crawler=?"
            args.append(crawler)
        sql += " ORDER BY created_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        with self._conn(readonly=True) as c:
            rows = c.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["params"] = json.loads(d.pop("params_json") or "{}")
            except json.JSONDecodeError:
                d["params"] = {}
            out.append(d)
        return out

    def delete_task(self, task_id: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM task_history WHERE task_id=?", (task_id,)).rowcount > 0

    # ---------------- 工作流 ----------------
    def save_workflow(self, wf_id: str, name: str, steps: list[dict]) -> None:
        now = int(time.time())
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO workflows(id, name, steps_json, created_at, updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                       steps_json=excluded.steps_json, updated_at=excluded.updated_at""",
                (wf_id, name, json.dumps(steps, ensure_ascii=False), now, now),
            )

    def list_workflows(self) -> list[dict]:
        with self._conn(readonly=True) as c:
            rows = c.execute("SELECT * FROM workflows ORDER BY updated_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["steps"] = json.loads(d.pop("steps_json") or "[]")
            except json.JSONDecodeError:
                d["steps"] = []
            out.append(d)
        return out

    def delete_workflow(self, wf_id: str) -> bool:
        with self._lock, self._conn() as c:
            return c.execute("DELETE FROM workflows WHERE id=?", (wf_id,)).rowcount > 0

    def save_workflow_run(self, run: dict) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO workflow_runs(run_id, workflow_id, state, current_step, total_steps,
                       task_ids, started_at, finished_at, error, payload_json)
                   VALUES (:run_id,:workflow_id,:state,:current_step,:total_steps,
                           :task_ids,:started_at,:finished_at,:error,:payload_json)
                   ON CONFLICT(run_id) DO UPDATE SET state=excluded.state,
                       current_step=excluded.current_step, task_ids=excluded.task_ids,
                       finished_at=excluded.finished_at, error=excluded.error,
                       payload_json=excluded.payload_json""",
                {
                    "run_id": run["run_id"],
                    "workflow_id": run["workflow_id"],
                    "state": run["state"],
                    "current_step": run.get("current_step", 0),
                    "total_steps": run.get("total_steps", 0),
                    "task_ids": json.dumps(run.get("task_ids", []), ensure_ascii=False),
                    "started_at": run.get("started_at", 0),
                    "finished_at": run.get("finished_at", 0),
                    "error": run.get("error", ""),
                    "payload_json": json.dumps(run, ensure_ascii=False),
                },
            )

    def list_workflow_runs(self) -> list[dict]:
        with self._conn(readonly=True) as c:
            rows = c.execute("SELECT r.*, w.name AS workflow_name FROM workflow_runs r "
                             "LEFT JOIN workflows w ON w.id=r.workflow_id ORDER BY r.started_at DESC").fetchall()
        out = []
        for row in rows:
            record = dict(row)
            payload = json.loads(record.pop("payload_json") or "{}")
            record["task_ids"] = json.loads(record["task_ids"] or "[]")
            record["step_results"] = payload.get("step_results", [])
            record["workflow_name"] = payload.get("workflow_name") or record["workflow_name"] or record["workflow_id"]
            out.append(record)
        return out


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "t.db")
        print("=" * 60)
        print(" Store 自测")
        print("=" * 60)

        # 1) 归一化写入
        n, d = st.insert_items("taobao", [
            {"title": "AOC 27英寸显示器", "price": 1299.0, "pay_count": 5000, "url": "https://item.taobao.com/1"},
            {"title": "小米显示器", "price": 899.0, "pay_count": 12000, "url": "https://item.taobao.com/2"},
        ], keyword="显示器", task_id="t-1")
        assert (n, d) == (2, 0), f"应新增 2 条, 实际 {(n, d)}"
        print(f"[ok] 淘宝写入 {n} 条")

        # 2) 去重
        n2, d2 = st.insert_items("taobao", [
            {"title": "AOC 27英寸显示器", "price": 1299.0, "pay_count": 5000, "url": "https://item.taobao.com/1"},
        ], keyword="显示器")
        assert (n2, d2) == (0, 1), f"重复应被跳过, 实际 {(n2, d2)}"
        print(f"[ok] 重复数据被跳过 ({d2} 条)")

        # 3) 跨源
        st.insert_items("bilibili", [
            {"title": "Python 教程", "views": 15910000, "url": "https://www.bilibili.com/video/BV1"},
        ], keyword="python")
        s = st.stats()
        assert s["items_total"] == 3, s
        assert len(s["items_by_source"]) == 2
        print(f"[ok] 跨源统计: {s['items_total']} 条, 来源 {[x['source'] for x in s['items_by_source']]}")

        # 4) 归一化字段正确
        q = st.query_items(source="taobao", order="metric")
        top = q["rows"][0]
        assert top["title"] == "小米显示器", top["title"]
        assert top["metric"] == 12000.0
        assert top["metric_name"] == "付款人数"
        assert top["price"] == 899.0
        print(f"[ok] 归一化正确: metric={top['metric']} ({top['metric_name']}) price={top['price']}")

        # 5) extra 字段没丢
        import json as _j
        extra = _j.loads(q["rows"][1]["extra_json"])
        assert extra.get("pay_count") == 5000, extra
        print(f"[ok] 原始字段进 extra_json, 未丢失")

        # 6) 搜索
        q2 = st.query_items(q="显示器")
        assert q2["total"] == 2, q2
        print(f"[ok] 模糊搜索: {q2['total']} 条")

        # 7) 任务历史
        st.save_task({"task_id": "t-1", "crawler": "taobao", "crawler_name": "淘宝",
                      "params": {"keyword": "显示器"}, "state": "success", "items": 2,
                      "created_at": 1, "duration": 1.5})
        st.save_task({"task_id": "t-1", "crawler": "taobao", "crawler_name": "淘宝",
                      "params": {"keyword": "显示器"}, "state": "success", "items": 5,
                      "created_at": 1, "duration": 2.5})
        h = st.task_history()
        assert len(h) == 1 and h[0]["items"] == 5, h
        print(f"[ok] 任务历史 upsert 正确 (items 更新为 {h[0]['items']})")

        # 8) 原始表
        st.insert_raw("taobao", {"title": "x", "anything": [1, 2, 3]}, task_id="t-1")
        with st._conn(readonly=True) as c:
            n_raw = c.execute("SELECT COUNT(*) FROM raw_taobao").fetchone()[0]
        assert n_raw == 1
        print(f"[ok] 原始表写入 {n_raw} 条")

        # 9) 工作流
        st.save_workflow("wf1", "每日采集", [{"crawler": "taobao", "params": {"keyword": "显示器"}}])
        wfs = st.list_workflows()
        assert len(wfs) == 1 and wfs[0]["steps"][0]["crawler"] == "taobao"
        print(f"[ok] 工作流存读正常: {wfs[0]['name']}, {len(wfs[0]['steps'])} 步")

        st.close()
        print("\n[Store] 全部自测通过")
