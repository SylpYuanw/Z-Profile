# -*- coding: utf-8 -*-
"""隔离验证任务失败、持久化历史和只读 SQL，不访问采集站点。"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "05_Web服务"))

from server.core.logbus import LogBus
from server.core.registry import CrawlerInfo, Registry
from server.core.taskrunner import TaskRunner
from server.app import create_app


class TaskRegressionTests(unittest.TestCase):
    def make_runner(self, run, **kwargs):
        module = ModuleType("probe")
        module.META = {"id": "probe", "name": "隔离测试", "params": [], "exclusive": False}
        module.run = run
        registry = Registry(Path("__none__"))
        registry._items["probe"] = CrawlerInfo(module, Path("<test>"))
        runner = TaskRunner(registry, LogBus(), **kwargs)
        self.addCleanup(lambda: runner._executor.shutdown(wait=True))
        return runner

    def finish(self, runner):
        task = runner.submit("probe", {})
        runner._executor.shutdown(wait=True)
        return task

    def test_error_without_results_is_failed(self):
        task = self.finish(self.make_runner(lambda ctx: ctx.err("登录态无效")))
        self.assertEqual(task.state, "failed")
        self.assertEqual(task.errors, 1)
        self.assertIn("登录态无效", task.error)

    def test_storage_failure_is_failed_and_not_counted(self):
        def broken_sink(task, item):
            raise RuntimeError("入库失败")

        runner = self.make_runner(lambda ctx: ctx.emit_item({"title": "隔离测试"}), on_item=broken_sink)
        task = self.finish(runner)
        self.assertEqual(task.state, "failed")
        self.assertEqual(task.items, 0)
        self.assertGreater(task.errors, 0)
        self.assertIn("入库失败", task.error)

    def test_finished_progress_is_persisted(self):
        task = self.finish(self.make_runner(lambda ctx: ctx.emit_item({"title": "隔离测试"})))
        self.assertEqual(task.state, "success")
        self.assertEqual(task.items, 1)
        self.assertEqual(task.progress, 1)

    def test_partial_result_keeps_nonfatal_error_count(self):
        def run(ctx):
            ctx.err("某一页失败")
            ctx.emit_item({"title": "其他页结果"})

        task = self.finish(self.make_runner(run))
        self.assertEqual((task.state, task.items, task.errors), ("success", 1, 1))


class APIRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.make_app()
        self.client = self.app.test_client()
        self.store = self.app.extensions["zforge"]["store"]

    def make_app(self):
        app = create_app(web_db=self.root / "web.db", account_db=self.root / "accounts.db", legacy_sources=[])
        ext = app.extensions["zforge"]
        self.addCleanup(ext["store"].close)
        self.addCleanup(ext["pool"].close)
        self.addCleanup(ext["runner"].shutdown)
        return app

    def save_task(self, state="success", task_id="history-test"):
        self.store.save_task({"task_id": task_id, "crawler": "bilibili", "state": state,
                              "params": {"keyword": "历史参数"}, "created_at": 100,
                              "started_at": 101, "finished_at": 102 if state == "success" else 0,
                              "items": 7, "progress": 1 if state == "success" else .5})

    def test_restart_restores_task_and_sse_terminal_state(self):
        self.save_task()
        client = self.make_app().test_client()
        task = client.get("/api/tasks").json["tasks"][0]
        self.assertEqual((task["task_id"], task["items"], task["params"]["keyword"]),
                         ("history-test", 7, "历史参数"))
        self.assertEqual(client.get("/api/tasks/history-test").status_code, 200)
        self.assertIn("event: done", client.get("/api/tasks/history-test/logs").text)

    def test_registered_collection_methods_and_authentication(self):
        crawlers = {c["id"]: c for c in self.client.get("/api/crawlers").json["crawlers"]}
        self.assertEqual(crawlers["bilibili"]["collection"]["methods"], ["selenium", "http"])
        self.assertEqual(crawlers["bilibili"]["authentication"]["requirement"], "none")
        self.assertEqual(crawlers["taobao"]["collection"]["methods"], ["selenium"])
        self.assertEqual(crawlers["taobao"]["authentication"]["credential_types"], ["cookies"])

    def test_interrupted_task_is_failed_on_restart(self):
        self.save_task("running")
        client = self.make_app().test_client()
        task = client.get("/api/tasks/history-test").json
        self.assertEqual(task["state"], "failed")
        self.assertIn("服务重启", task["error"])
        self.assertEqual(client.get("/api/tasks").json["counts"]["active"], 0)

    def test_deleted_task_does_not_reappear(self):
        self.save_task()
        client = self.make_app().test_client()
        self.assertEqual(client.delete("/api/tasks/history-test").status_code, 200)
        self.assertEqual(self.make_app().test_client().get("/api/tasks").json["tasks"], [])

    def test_sql_literals_and_single_statement_are_allowed(self):
        for sql in ["SELECT 'updated; ?' AS note", "-- 中文注释\nSELECT 1;", "SELECT replace('a','a','b')", "EXPLAIN SELECT 1"]:
            with self.subTest(sql=sql):
                r = self.client.post("/api/sql", json={"sql": sql})
                self.assertEqual(r.status_code, 200, r.json)

    def test_sql_writes_and_multiple_statements_are_rejected(self):
        for sql in ["DROP TABLE items", "SELECT 1; SELECT 2", "PRAGMA table_info(items)",
                    "WITH x AS (SELECT 1) DELETE FROM items", "EXPLAIN DELETE FROM items"]:
            with self.subTest(sql=sql):
                r = self.client.post("/api/sql", json={"sql": sql})
                self.assertEqual(r.status_code, 400, r.json)
                self.assertIsNotNone(r.json["error"])

    def test_bad_parameters_and_unknown_api_are_json_errors(self):
        for path in ["/api/tasks?limit=abc", "/api/data/zforge/rows?table=items&limit=abc",
                     "/api/data/items?limit=-1", "/api/data/items?offset=abc"]:
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 400)
                self.assertTrue(r.is_json)
        self.assertEqual(self.client.post("/api/tasks", json=[1]).status_code, 400)
        r = self.client.get("/api/not-a-route")
        self.assertEqual(r.status_code, 404)
        self.assertTrue(r.is_json)

    def test_workflow_run_details_survive_restart(self):
        record = {"run_id": "run-test", "workflow_id": "wf-test", "workflow_name": "历史工作流",
                  "state": "success", "current_step": 1, "total_steps": 1,
                  "started_at": 100, "finished_at": 101, "task_ids": ["history-test"],
                  "step_results": [{"step": 1, "state": "success", "items": 7}]}
        self.store.save_workflow_run(record)
        r = self.make_app().test_client().get("/api/workflows/runs").json["runs"][0]
        self.assertEqual(r["workflow_name"], "历史工作流")
        self.assertEqual(r["step_results"], record["step_results"])

    def test_old_workflow_schema_is_migrated_without_deleting_data(self):
        with self.store._conn() as conn:
            conn.execute("ALTER TABLE workflow_runs DROP COLUMN payload_json")
        self.save_task()
        client = self.make_app().test_client()
        self.assertEqual(len(client.get("/api/tasks").json["tasks"]), 1)
        self.assertEqual(client.get("/api/workflows/runs").status_code, 200)

    def test_sql_return_limit_and_quoted_table_name(self):
        r = self.client.post("/api/sql", json={
            "sql": "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<5) SELECT x FROM n", "limit": 2})
        self.assertEqual(r.json["row_count"], 2)
        with self.store._conn() as conn:
            conn.execute('CREATE TABLE "含""引号" (title TEXT)')
        r = self.client.get("/api/data/zforge/rows", query_string={"table": '含"引号'})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(r.json["table"], '含"引号')

    def test_workflow_parameters_are_validated_before_saving(self):
        for steps in ["bad", [{"crawler": "missing"}], [{"crawler": "bilibili", "params": []}],
                      [{"crawler": "bilibili", "params": {}}]]:
            with self.subTest(steps=steps):
                self.assertEqual(self.client.post("/api/workflows", json={"steps": steps}).status_code, 400)
        self.assertEqual(self.store.list_workflows(), [])


if __name__ == "__main__":
    unittest.main()
