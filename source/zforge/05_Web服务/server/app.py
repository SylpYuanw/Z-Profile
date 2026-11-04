# -*- coding: utf-8 -*-
"""
ZForge Web 服务
===============

把注册表 / 调度内核 / 账号池 / 数据层 装成一个 Flask 应用。

路由分四组:
  /api/system/*    健康检查、环境自检
  /api/crawlers/*  爬虫清单 (前端据此自动渲染界面)
  /api/tasks/*     派活、查状态、取消、SSE 日志流
  /api/data/*      数据源、表、行、SQL、导出
  /api/accounts/*  账号池
"""

from __future__ import annotations

import json
import io
import os
import queue
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

from flask import Flask, Request, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

# 让 `python 05_Web服务/run.py` 和 `python -m server.app` 都能跑
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.core.accounts import AccountError, AccountPool, NoAccountAvailable
from server.core.contract import ParamError, validate_params
from server.core.logbus import LogBus
from server.core.registry import Registry, default_crawlers_root
from server.core.taskrunner import TaskError, TaskRunner
from server.core.workflow import WorkflowError, WorkflowRunner
from server.core.vault import VaultError
from server.core.browser_login import BrowserLogins
from server.store.db import Store
from server.store.projects import HISTORY_PROJECTS, ProjectData

# ============================================================
# 路径
# ============================================================
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "03_统一数据库"
ACCOUNT_DB = PROJECT_ROOT / "02_账号池" / "数据" / "accounts.db"
WEB_DB = PROJECT_ROOT / "05_Web服务" / "data" / "zforge.db"
FRONTEND = PROJECT_ROOT / "06_前端"

# 历史库 (只读展示用)
LEGACY_SOURCES = [
    ("taobao_legacy", "淘宝历史采集", DATA_DIR / "taobao_data.db"),
    ("bili_legacy", "B站历史采集", DATA_DIR / "视频简单信息.db"),
]


# ============================================================
# 应用装配
# ============================================================
def create_app(*, web_db: Path = WEB_DB, account_db: Path = ACCOUNT_DB,
               legacy_sources: list | None = None, login_browser_factory=None) -> Flask:
    web_db, account_db = Path(web_db), Path(account_db)
    legacy_sources = LEGACY_SOURCES if legacy_sources is None else legacy_sources
    # 所有列举、明细与 SQL 接口共用采集历史库白名单，拒绝无关来源。
    legacy_sources = [(sid, name, Path(path)) for sid, name, path in legacy_sources if sid in HISTORY_PROJECTS]
    app = Flask(__name__, static_folder=None)
    class MemoryUploadRequest(Request):
        def _get_file_stream(self, total_content_length, content_type, filename=None, content_length=None):
            # 请求有总大小上限；上传凭据只保存在内存，不产生明文临时文件。
            return io.BytesIO()
    app.request_class = MemoryUploadRequest
    app.config["JSON_AS_ASCII"] = False
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
    app.json.ensure_ascii = False

    # ---- 组件 ----
    bus = LogBus()
    registry = Registry(default_crawlers_root()).scan()
    store = Store(web_db)
    pool = AccountPool(account_db)
    logins = BrowserLogins(pool, browser_factory=login_browser_factory)
    projects = ProjectData(store, registry, legacy_sources)

    def on_item(task, item):
        """爬虫每交一条数据, 归一化入库 + 存原始记录。"""
        keyword = str(item.get("keyword") or task.params.get("keyword") or "")
        inserted = store.insert_item(
            source=task.crawler_id, item=item, task_id=task.task_id,
            workflow_id=task.workflow_id or "", keyword=keyword,
        )
        store.insert_raw(task.crawler_id, item, task_id=task.task_id, keyword=keyword)
        return inserted

    def on_task_change(task):
        try:
            store.save_task(task.to_dict())
        except Exception:
            pass

    runner = TaskRunner(registry, bus, account_pool=pool,
                        on_item=on_item, on_task_change=on_task_change)
    workflows = WorkflowRunner(runner, store, bus)
    runner.restore(store.task_history(limit=None))
    workflows.restore(store.list_workflow_runs())
    app.extensions["zforge"] = {
        "bus": bus, "registry": registry, "store": store,
        "pool": pool, "runner": runner, "workflows": workflows,
        "logins": logins, "projects": projects,
    }

    # ========================================================
    # 通用
    # ========================================================
    def err(msg: str, code: int = 400, detail: str = ""):
        return jsonify({"error": msg, "detail": detail}), code

    @app.errorhandler(BadRequest)
    def bad_request(error):
        return err(error.description, 400)

    @app.errorhandler(AccountError)
    def account_error(error):
        return err(str(error), 400)

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(error):
        return err("上传内容超过 2 MB，请分批导入", 413)

    def body_json() -> dict:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise BadRequest("请求体必须是 JSON 对象")
        return body

    def number(value, name: str, default: int, minimum: int = 0, maximum: int = 5000) -> int:
        if value is None:
            return default
        text = str(value)
        if isinstance(value, bool) or len(text) > 16 or not re.fullmatch(r"-?\d+", text):
            raise BadRequest(f"{name} 必须是整数")
        result = int(text)
        if result < minimum:
            raise BadRequest(f"{name} 不能小于 {minimum}")
        return min(result, maximum)

    @app.after_request
    def no_cache(resp):
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ========================================================
    # 系统
    # ========================================================
    @app.get("/api/health")
    def health():
        import importlib.util
        deps = {m: importlib.util.find_spec(m) is not None
                for m in ("selenium", "lxml", "requests", "openpyxl", "flask")}
        return jsonify({
            "ok": True,
            "project_root": str(PROJECT_ROOT),
            "pid": os.getpid(),
            "version": "0.2.0",
            "data_scope": "crawler-only-v1",
            "python": sys.version.split()[0],
            "uptime_s": int(time.time() - STARTED_AT),
            "crawlers": len(registry),
            "crawler_errors": registry.errors,
            "deps": deps,
            "logbus": bus.stats(),
            "tasks": runner.counts(),
        })

    @app.get("/api/system/info")
    def system_info():
        import importlib.util
        deps = []
        for m in ("selenium", "lxml", "requests", "openpyxl", "flask"):
            spec = importlib.util.find_spec(m)
            ver = "?"
            if spec:
                try:
                    from importlib import metadata
                    ver = metadata.version(m)
                except Exception:
                    ver = "已安装"
            deps.append({"name": m, "installed": spec is not None, "version": ver})

        sources = [{"id": "zforge", "name": "工作台数据库", "path": str(web_db),
                    "exists": web_db.exists(), "writable": True}]
        for sid, name, p in legacy_sources:
            sources.append({"id": sid, "name": name, "path": str(p),
                            "exists": p.exists(), "writable": False})

        return jsonify({
            "project_root": str(PROJECT_ROOT),
            "python": sys.executable,
            "python_version": sys.version.split()[0],
            "deps": deps,
            "sources": sources,
            "crawlers_root": str(default_crawlers_root()),
            "account_db": {"path": str(account_db), "exists": account_db.exists()},
            "web_db": {"path": str(web_db), "exists": web_db.exists()},
        })

    # ========================================================
    # 爬虫
    # ========================================================
    @app.get("/api/crawlers")
    def list_crawlers():
        return jsonify({"crawlers": [c.to_dict() for c in registry.items],
                        "errors": registry.errors})

    @app.post("/api/crawlers/reload")
    def reload_crawlers():
        registry.scan()
        return jsonify({"count": len(registry), "ids": list(registry._items),
                        "errors": registry.errors})

    # ========================================================
    # 任务
    # ========================================================
    @app.post("/api/tasks")
    def create_task():
        body = body_json()
        cid = body.get("crawler")
        if not isinstance(cid, str) or not cid:
            return err("缺少 crawler 字段")
        try:
            task = runner.submit(cid, body.get("params", {}), account_pool_id=body.get("account_pool_id"))
        except TaskError as e:
            return err(str(e), 400)
        return jsonify(task.to_dict()), 202

    @app.get("/api/tasks")
    def list_tasks():
        tasks = runner.list(
            crawler=request.args.get("crawler"),
            state=request.args.get("state"),
            limit=number(request.args.get("limit"), "limit", 50, 1),
        )
        return jsonify({"tasks": [t.to_dict() for t in tasks], "counts": runner.counts()})

    @app.get("/api/tasks/<task_id>")
    def get_task(task_id):
        t = runner.get(task_id)
        if t is None:
            return err("任务不存在", 404)
        return jsonify(t.to_dict())

    @app.post("/api/tasks/<task_id>/cancel")
    def cancel_task(task_id):
        try:
            t = runner.cancel(task_id)
        except TaskError as e:
            return err(str(e), 400)
        return jsonify(t.to_dict())

    @app.delete("/api/tasks/<task_id>")
    def delete_task(task_id):
        try:
            ok = runner.delete(task_id)
        except TaskError as e:
            return err(str(e), 400)
        if not ok:
            return err("任务不存在", 404)
        store.delete_task(task_id)
        return jsonify({"deleted": task_id})

    # ---- SSE 实时日志 ----
    @app.get("/api/tasks/<task_id>/logs")
    def task_logs(task_id):
        t = runner.get(task_id)
        if t is None:
            return err("任务不存在", 404)

        def stream():
            q: queue.Queue = queue.Queue(maxsize=1000)

            def on_event(kind, payload):
                try:
                    q.put_nowait((kind, payload))
                except queue.Full:
                    pass

            # 先补历史, 再订阅 —— 用户可能中途才打开页面
            for line in bus.history(task_id):
                yield _sse("log", line)
            last = bus.last_state(task_id)
            if last:
                yield _sse("state", last)
            if t.state in ("success", "failed", "canceled"):
                yield _sse("done", t.to_dict())
                return

            sid = bus.subscribe(on_event, task_id)
            try:
                deadline = time.time() + 3600
                while time.time() < deadline:
                    try:
                        kind, payload = q.get(timeout=15)
                    except queue.Empty:
                        yield ": keepalive\n\n"          # 心跳, 防代理断连
                        if t.state in ("success", "failed", "canceled"):
                            break
                        continue
                    yield _sse(kind, payload)
                    if kind == "done":
                        break
            finally:
                bus.unsubscribe(sid)

        return Response(stream(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        })

    @app.get("/api/logs/recent")
    def recent_logs():
        return jsonify({"lines": bus.recent(
            limit=number(request.args.get("limit"), "limit", 200, 1),
            level=request.args.get("level"),
        )})

    # ========================================================
    # 数据
    # ========================================================
    @app.get("/api/data/sources")
    def data_sources():
        out = [{"id": "zforge", "name": "工作台数据库", "kind": "sqlite",
                "path": str(web_db), "writable": True, "exists": web_db.exists()}]
        for sid, name, p in legacy_sources:
            out.append({"id": sid, "name": name, "kind": "sqlite",
                        "path": str(p), "writable": False, "exists": p.exists()})
        return jsonify({"sources": out})

    @app.get("/api/data/projects")
    def data_projects():
        return jsonify({"projects": projects.catalog()})

    @app.get("/api/data/projects/<project_id>/datasets/<dataset_id>")
    def project_dataset(project_id, dataset_id):
        data = projects.load(project_id, dataset_id, number(request.args.get("limit"), "limit", 5000, 1, 5000))
        return jsonify(data) if data else err("该项目中没有此数据集", 404)

    def _db_for(source_id: str) -> Path | None:
        if source_id == "zforge":
            return web_db
        for sid, _n, p in legacy_sources:
            if sid == source_id:
                return p
        return None

    @app.get("/api/data/<source_id>/tables")
    def data_tables(source_id):
        p = _db_for(source_id)
        if p is None or not p.exists():
            return err(f"数据源不存在: {source_id}", 404)
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            tables = []
            for n in names:
                quoted = n.replace('"', '""')
                try:
                    cnt = conn.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0]
                    cols = [{"name": r[1], "type": r[2] or "TEXT"}
                            for r in conn.execute(f'PRAGMA table_info("{quoted}")')]
                except Exception as e:
                    cnt, cols = -1, []
                tables.append({"name": n, "rows": cnt, "columns": cols})
        finally:
            conn.close()
        return jsonify({"source": source_id, "tables": tables})

    @app.get("/api/data/<source_id>/rows")
    def data_rows(source_id):
        p = _db_for(source_id)
        if p is None or not p.exists():
            return err(f"数据源不存在: {source_id}", 404)
        table = request.args.get("table")
        if not table:
            return err("缺少 table 参数")
        limit = number(request.args.get("limit"), "limit", 100, 1, 2000)
        offset = number(request.args.get("offset"), "offset", 0, 0, 2**63 - 1)
        order = request.args.get("order")
        direction = "DESC" if request.args.get("dir", "desc").lower() == "desc" else "ASC"
        q = request.args.get("q")

        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if table not in names:
                return err(f"表不存在: {table}", 404)
            table_sql = table.replace('"', '""')
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table_sql}")')]
            if not cols:
                return err(f"表不存在: {table}", 404)
            where, args = "", []
            if q:
                text_cols = [c for c in cols if c.lower() in
                             ("title", "标题", "name", "keyword", "author", "shop_name")]
                if text_cols:
                    where = " WHERE " + " OR ".join(f'"{c}" LIKE ?' for c in text_cols)
                    args = [f"%{q}%"] * len(text_cols)
            total = conn.execute(f'SELECT COUNT(*) FROM "{table_sql}"{where}', args).fetchone()[0]
            order_sql = ""
            if order and order in cols:
                order_sql = ' ORDER BY "' + order.replace('"', '""') + '" ' + direction
            rows = conn.execute(
                f'SELECT * FROM "{table_sql}"{where}{order_sql} LIMIT ? OFFSET ?',
                args + [limit, offset]).fetchall()
        finally:
            conn.close()
        return jsonify({"table": table, "columns": cols, "total": total,
                        "offset": offset, "limit": limit,
                        "rows": [list(r) for r in rows]})

    # ---- SQL 实验室 ----
    READ_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                    sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}

    def read_only(action, arg1, arg2, database, trigger):
        if action not in READ_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in ("load_extension", "readfile", "writefile"):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @app.post("/api/sql")
    def run_sql():
        body = body_json()
        sid = body.get("source_id", "zforge")
        sql = body.get("sql")
        if not isinstance(sql, str):
            return err("sql 必须是字符串")
        sql = sql.strip()
        limit = number(body.get("limit"), "limit", 1000, 1)

        if not sql:
            return err("SQL 不能为空")
        # 编码体检: 客户端没按 UTF-8 发, 中文会变成 U+FFFD 或 '?',
        # 这时候报 "near \"?\": syntax error" 让人一头雾水。提前拦住并说清楚。
        if "\ufffd" in sql:
            return err("SQL 里出现了乱码字符 U+FFFD —— "
                       "客户端没有按 UTF-8 发送请求体。请确认 Content-Type 带 charset=utf-8。")
        p = _db_for(sid)
        if p is None or not p.exists():
            return err(f"数据源不存在: {sid}", 404)

        if not re.match(r"\A(?:\s|--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/)*(select|with|explain)\b", sql, re.I):
            return err("只允许 SELECT / WITH / EXPLAIN")

        t0 = time.time()
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        conn.set_authorizer(read_only)
        try:
            cur = conn.execute(sql)
            cols = [d[0] for d in (cur.description or [])]
            rows = [list(r) for r in cur.fetchmany(limit)]
        except sqlite3.Error as e:
            conn.close()
            return err(f"SQL 执行失败: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

        return jsonify({"columns": cols, "rows": rows, "row_count": len(rows),
                        "elapsed_ms": round((time.time() - t0) * 1000, 2)})

    @app.get("/api/data/stats")
    def data_stats():
        return jsonify(store.stats())

    @app.get("/api/data/items")
    def data_items():
        return jsonify(store.query_items(
            source=request.args.get("source"),
            keyword=request.args.get("keyword"),
            q=request.args.get("q"),
            order=request.args.get("order", "metric"),
            limit=number(request.args.get("limit"), "limit", 200, 1, 2000),
            offset=number(request.args.get("offset"), "offset", 0, 0, 2**63 - 1),
        ))

    # ========================================================
    # 账号池
    # ========================================================
    @app.get("/api/accounts")
    def accounts_list():
        site = request.args.get("site")
        return jsonify({
            "accounts": [pool.account_details(a) for a in pool.list(site)],
            "summary": pool.summary(site),
            "sites": pool.sites(),
        })

    @app.get("/api/accounts/<int:aid>/history")
    def account_history(aid):
        return jsonify({"history": pool.history(aid, limit=number(request.args.get("limit"), "limit", 50, 1))})

    @app.patch("/api/accounts/<int:aid>")
    def account_profile(aid):
        return jsonify(pool.update_profile(aid, body_json()))

    @app.get("/api/account-logins")
    def account_logins():
        return jsonify({"logins": logins.list()})

    @app.post("/api/account-logins")
    def account_login_start():
        body = body_json()
        pid = number(body.get("pool_id"), "pool_id", 0, 1, 2**31 - 1)
        aid = body.get("update_account_id")
        aid = number(aid, "update_account_id", 0, 1, 2**31 - 1) if aid is not None else None
        return jsonify(logins.start(pid, body.get("phone"), body.get("label", ""), aid)), 202

    @app.get("/api/account-logins/<job_id>")
    def account_login_get(job_id):
        job = logins.get(job_id)
        return jsonify(job) if job else err("登录任务不存在；服务重启后请重新打开", 404)

    @app.post("/api/account-logins/<job_id>/cancel")
    def account_login_cancel(job_id):
        return jsonify(logins.cancel(job_id))

    @app.post("/api/accounts/<int:aid>/state")
    def account_set_state(aid):
        body = body_json()
        state = body.get("state")
        try:
            pool.set_state(aid, state, note=body.get("note"))
        except AccountError as e:
            return err(str(e), 400)
        acc = pool.get(aid)
        return jsonify(acc.to_dict() if acc else {})

    @app.delete("/api/accounts/<int:aid>")
    def account_delete(aid):
        return jsonify({"deleted": pool.remove(aid)})

    @app.get("/api/account-pools")
    def account_pools_list():
        return jsonify({"pools": pool.pools(request.args.get("site"))})

    @app.post("/api/account-pools")
    def account_pool_create():
        body = body_json()
        return jsonify(pool.create_pool(body.get("site"), body.get("name"),
                                        body.get("strategy", "least_used"))), 201

    @app.get("/api/account-pools/<int:pid>")
    def account_pool_get(pid):
        record = pool.pool_info(pid)
        if record is None:
            return err("账号池不存在", 404)
        return jsonify({"pool": record, "accounts": pool.members(pid)})

    @app.patch("/api/account-pools/<int:pid>")
    def account_pool_update(pid):
        return jsonify(pool.update_pool(pid, body_json()))

    @app.post("/api/account-pools/<int:pid>/members")
    def account_pool_add_member(pid):
        aid = number(body_json().get("account_id"), "account_id", 0, 1, 2**31 - 1)
        pool.add_member(pid, aid)
        return jsonify({"added": aid})

    @app.delete("/api/account-pools/<int:pid>/members/<int:aid>")
    def account_pool_remove_member(pid, aid):
        pool.remove_member(pid, aid)
        return jsonify({"removed": aid})

    @app.post("/api/account-pools/<int:pid>/import")
    def account_pool_import(pid):
        pool.require_pool(pid)
        files = request.files.getlist("files")
        if not 1 <= len(files) <= 20:
            return err("每次请选择 1～20 个 Cookie JSON 文件")
        aid = request.form.get("update_account_id")
        aid = number(aid, "update_account_id", 0, 1, 2**31 - 1) if aid else None
        if aid and len(files) != 1:
            return err("更新已有账号时只能选择一个文件")
        results = []
        for uploaded in files:
            filename = (uploaded.filename or "未命名.json").replace("\\", "/").rsplit("/", 1)[-1]
            label = request.form.get("label") if len(files) == 1 else None
            label = label or Path(filename).stem
            try:
                payload = json.loads(uploaded.read().decode("utf-8-sig"))
                result = pool.import_cookies(pid, label, payload, aid)
                results.append({"file": filename, **result})
            except (ValueError, UnicodeError):
                results.append({"file": filename, "status": "failed", "error": "不是有效的 UTF-8 JSON 文件"})
            except AccountError as error:
                results.append({"file": filename, "status": "failed", "error": str(error)})
            except VaultError:
                results.append({"file": filename, "status": "failed", "error": "凭据加解密失败，请检查本机密钥"})
        return jsonify({"results": results})

    # ========================================================
    # 工作流
    # ========================================================
    @app.get("/api/workflows")
    def workflows_list():
        return jsonify({"workflows": store.list_workflows(), "runs": workflows.list_runs()})

    @app.post("/api/workflows")
    def workflow_save():
        body = body_json()
        wf_id = body.get("id") or ("wf-" + str(int(time.time())))
        steps = body.get("steps") or []
        if not isinstance(steps, list) or not steps:
            return err("工作流至少要有一个步骤")
        cleaned = []
        for index, step in enumerate(steps, 1):
            if not isinstance(step, dict) or not isinstance(step.get("crawler"), str):
                return err(f"第 {index} 步必须提供 crawler")
            info = registry.get(step["crawler"])
            if info is None:
                return err(f"第 {index} 步爬虫不存在")
            params = step.get("params", {})
            if not isinstance(params, dict):
                return err(f"第 {index} 步 params 必须是 JSON 对象")
            policy = step.get("on_fail", "continue")
            if policy not in ("continue", "abort", "retry"):
                return err(f"第 {index} 步失败策略无效")
            try:
                params = validate_params(info.meta, params)
                account_pool_id = runner.validate_pool(info, step.get("account_pool_id"))
            except ParamError as error:
                return err(f"第 {index} 步参数不合法: {error}")
            except TaskError as error:
                return err(f"第 {index} 步账号配置不合法: {error}")
            cleaned.append({**step, "params": params, "on_fail": policy,
                            "account_pool_id": account_pool_id,
                            "retries": number(step.get("retries"), "retries", 0, 0, 10)})
        try:
            store.save_workflow(wf_id, body.get("name") or "未命名工作流", cleaned)
        except Exception as e:
            return err(f"保存失败: {e}", 500)
        return jsonify({"id": wf_id})

    @app.delete("/api/workflows/<wf_id>")
    def workflow_delete(wf_id):
        return jsonify({"deleted": store.delete_workflow(wf_id)})

    @app.post("/api/workflows/<wf_id>/run")
    def workflow_run(wf_id):
        wf = next((w for w in store.list_workflows() if w["id"] == wf_id), None)
        if wf is None:
            return err("工作流不存在", 404)
        try:
            run = workflows.start(wf)
        except WorkflowError as e:
            return err(str(e), 400)
        return jsonify(run), 202

    @app.post("/api/workflows/runs/<run_id>/cancel")
    def workflow_cancel(run_id):
        try:
            r = workflows.cancel(run_id)
        except WorkflowError as e:
            return err(str(e), 400)
        return jsonify(r)

    @app.get("/api/workflows/runs")
    def workflow_runs():
        return jsonify({"runs": workflows.list_runs()})

    # ========================================================
    # 前端静态文件
    # ========================================================
    @app.get("/")
    def index():
        return send_from_directory(FRONTEND, "index.html")

    @app.get("/<path:path>")
    def static_files(path):
        if path.startswith("api/"):
            return err("接口不存在", 404)
        full = FRONTEND / path
        if full.exists() and full.is_file():
            return send_from_directory(FRONTEND, path)
        # 未知路径回落到首页 (前端是 hash 路由)
        return send_from_directory(FRONTEND, "index.html")

    return app


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


STARTED_AT = time.time()
if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=8765, debug=False, threaded=True)
