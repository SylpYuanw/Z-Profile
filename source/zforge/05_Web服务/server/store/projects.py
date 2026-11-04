# -*- coding: utf-8 -*-
"""按采集项目列出数据集；每次读取一个明确的来源与表。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


# 历史库必须明确归属于采集平台；目录中存在其他数据库不代表允许接入。
HISTORY_PROJECTS = {"bili_legacy": "bilibili", "taobao_legacy": "taobao"}


class ProjectData:
    def __init__(self, store, registry, legacy_sources):
        self.store, self.registry = store, registry
        self.legacy = {sid: (name, Path(path)) for sid, name, path in legacy_sources if sid in HISTORY_PROJECTS}

    def catalog(self):
        result = []
        for crawler in self.registry.items:
            cid = crawler.id
            with self.store._conn(readonly=True) as c:
                count = c.execute("SELECT COUNT(*) FROM items WHERE source=?", (cid,)).fetchone()[0]
            datasets = [{"id": "current", "name": "当前采集结果", "source_id": "zforge", "database": self.store.db_path.name,
                         "table": "items", "rows": count, "origin": "current", "analysis": cid if cid in ("bilibili", "taobao") else "table"}]
            legacy_id = next((sid for sid, pid in HISTORY_PROJECTS.items() if pid == cid), None)
            if legacy_id in self.legacy:
                datasets.extend(self._tables(legacy_id, cid))
            result.append({"id": cid, "name": {"bilibili": "B站视频", "taobao": "淘宝商品"}.get(cid, crawler.meta.get("name", cid)),
                           "description": crawler.meta.get("desc", ""), "crawler_id": cid, "datasets": datasets})
        return result

    def _tables(self, source_id, project_id):
        _, path = self.legacy[source_id]
        if not path.exists():
            return []
        datasets = []
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as c:
            tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            for table in tables:
                quoted = table.replace('"', '""')
                columns = {r[1] for r in c.execute(f'PRAGMA table_info("{quoted}")')}
                count = c.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0]
                analysis = "table"
                if project_id == "bilibili" and {"标题", "播放量"} <= columns:
                    analysis = "bilibili"
                elif project_id == "taobao" and {"title", "price", "pay_count"} <= columns:
                    analysis = "taobao"
                datasets.append({"id": source_id + "-" + hashlib.sha256(table.encode()).hexdigest()[:12],
                                 "name": table, "table": table, "source_id": source_id, "database": path.name,
                                 "rows": count, "origin": "history", "analysis": analysis})
        return datasets

    def load(self, project_id, dataset_id, limit=5000):
        project = next((p for p in self.catalog() if p["id"] == project_id), None)
        if project is None:
            return None
        dataset = next((d for d in project["datasets"] if d["id"] == dataset_id), None)
        if dataset is None:
            return None
        if dataset["origin"] == "current":
            with self.store._conn(readonly=True) as c:
                cursor = c.execute("SELECT * FROM items WHERE source=? ORDER BY id DESC LIMIT ?", (project_id, limit))
                columns = [d[0] for d in cursor.description]
                records = [dict(r) for r in cursor.fetchall()]
            items = records
        else:
            path = self.legacy[dataset["source_id"]][1]
            quoted = dataset["table"].replace('"', '""')
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as c:
                c.row_factory = sqlite3.Row
                cursor = c.execute(f'SELECT * FROM "{quoted}" LIMIT ?', (limit,))
                columns = [d[0] for d in cursor.description]
                records = [dict(r) for r in cursor.fetchall()]
            items = [self._normalize(project_id, dataset["table"], r) for r in records] if dataset["analysis"] in ("bilibili", "taobao") else []
        return {"project": {k: v for k, v in project.items() if k != "datasets"}, "dataset": dataset,
                "columns": columns, "rows": [[r.get(k) for k in columns] for r in records], "items": items,
                "total": dataset["rows"], "limit": limit, "truncated": dataset["rows"] > len(records)}

    @staticmethod
    def _normalize(project, table, row):
        if project == "bilibili":
            extra = {new: row[old] for old, new in {"点赞": "like", "投币": "coin", "收藏": "favorite", "弹幕数": "danmaku", "点赞率": "like_rate", "三连率": "triple_rate"}.items() if old in row and row[old] is not None}
            extra["detail_ok"] = int(all(k in extra for k in ("like", "coin", "favorite")))
            return {"source": project, "keyword": table, "title": row.get("标题"), "url": row.get("URL"), "metric": row.get("播放量"), "metric_name": "播放量", "extra_json": json.dumps(extra, ensure_ascii=False)}
        return {"source": project, "keyword": table, "title": row.get("title"), "url": row.get("url"), "price": row.get("price"), "metric": row.get("pay_count"), "metric_name": "付款人数", "extra_json": json.dumps(row, ensure_ascii=False), "_legacy": True}
