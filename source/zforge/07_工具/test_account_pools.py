# -*- coding: utf-8 -*-
"""临时库验证命名池、导入校验、租约与任务绑定，不访问外站。"""
import io
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "05_Web服务"))
from server.app import create_app
from server.core.accounts import AccountPool, AccountError, NoAccountAvailable
from server.core.logbus import LogBus
from server.core.registry import CrawlerInfo, Registry
from server.core.taskrunner import TaskRunner, TaskError
from server.core.vault import Vault
from server.store.db import Store


def cookies(identity="test-1", site="taobao", expiry=None):
    keys = ("unb", "cookie2") if site == "taobao" else ("DedeUserID", "SESSDATA")
    result = [{"name": keys[0], "value": identity, "domain": "." + site + ".com"},
              {"name": keys[1], "value": "isolated-secret", "domain": "." + site + ".com"}]
    if expiry is not None:
        result[1]["expirationDate"] = expiry
    return result


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = Vault("isolated", key_file=self.root / "key")
        self.vault.salt_file = self.root / "salt"
        self.pool = AccountPool(self.root / "accounts.db", vault=self.vault)
        self.addCleanup(self.pool.close)
        self.pid = self.pool.resolve_pool("taobao")

    def add(self, identity="test-1", label="账号一", pid=None, **kwargs):
        return self.pool.import_cookies(pid or self.pid, label, cookies(identity, **kwargs))["account"]

    def test_import_encrypted_and_safe_response(self):
        account = self.add()
        self.assertEqual(account["login_status"], "unknown")
        self.assertEqual(account["pool_ids"], [self.pid])
        self.assertNotIn("isolated-secret", json.dumps(account))
        with self.pool._conn() as c:
            blob = c.execute("SELECT credential FROM accounts").fetchone()[0]
        self.assertNotIn("isolated-secret", blob)
        self.assertEqual(self.pool.get_credential(account["id"])["cookies"][1]["value"], "isolated-secret")

    def test_duplicate_and_wrong_update_identity_rejected(self):
        a = self.add()
        with self.assertRaises(AccountError):
            self.add(label="新名称")
        with self.assertRaises(AccountError):
            self.pool.import_cookies(self.pid, "账号一", cookies("other"), a["id"])
        self.pool.set_state(a["id"], "disabled")
        updated = self.pool.import_cookies(self.pid, "账号一", cookies(), a["id"])
        self.assertEqual(updated["status"], "updated")
        self.assertEqual(updated["account"]["state"], "disabled")

    def test_bad_cookie_domain_schema_and_expiry(self):
        for payload in ({}, None, [], [{"name": "unb", "value": 42}], cookies(site="bilibili")):
            with self.subTest(payload=payload), self.assertRaises(AccountError):
                self.pool.import_cookies(self.pid, "错误", payload)
        a = self.add(expiry=int(time.time()) - 1)
        self.assertEqual(a["login_status"], "expired")
        self.assertEqual(self.pool.pool_info(self.pid)["available"], 0)
        with self.assertRaises(NoAccountAvailable):
            self.pool.lease_account("taobao", self.pid, "test")

    def test_session_cookie_export_and_local_login_mark(self):
        account = self.add(expiry=-1)
        self.assertEqual(account["login_status"], "unknown")
        self.assertNotIn("expiry", self.pool.get_credential(account["id"])["cookies"][1])
        self.pool.mark_login(account["id"], "expired")
        marked = self.pool.get(account["id"])
        self.assertGreater(marked.login_checked_at, 0)
        self.assertFalse(marked.available)
        with self.assertRaises(NoAccountAvailable):
            self.pool.lease_account("taobao", self.pid, "test")

    def test_membership_isolation_shared_lease_and_quota(self):
        a = self.add()
        second = self.pool.create_pool("taobao", "详情池")["id"]
        with self.assertRaises(NoAccountAvailable):
            self.pool.lease_account("taobao", second, "wrong-pool")
        self.pool.add_member(second, a["id"])
        account, _, token = self.pool.lease_account("taobao", self.pid, "first")
        with self.assertRaises(NoAccountAvailable):
            self.pool.lease_account("taobao", second, "second")
        for action in (lambda: self.pool.remove(a["id"]),
                       lambda: self.pool.remove_member(second, a["id"]),
                       lambda: self.pool.import_cookies(second, "账号一", cookies(), a["id"])):
            with self.assertRaises(AccountError):
                action()
        self.assertFalse(self.pool.renew_lease(a["id"], "wrong-token"))
        self.assertTrue(self.pool.renew_lease(a["id"], token))
        self.pool.release_lease(a["id"], "wrong-token")
        self.assertTrue(self.pool.account_details(account)["busy"])
        self.pool.release_lease(a["id"], token)
        account, _, token2 = self.pool.lease_account("taobao", second, "next")
        self.pool.report(a["id"], True)
        self.assertEqual(self.pool.get(a["id"]).used_today, 2)
        self.pool.release_lease(a["id"], token2)

    def test_cross_site_disabled_pool_and_removed_members_survive_restart(self):
        a = self.add()
        bili = self.pool.resolve_pool("bilibili")
        with self.assertRaises(AccountError):
            self.pool.add_member(bili, a["id"])
        with self.assertRaises(AccountError):
            self.pool.resolve_pool("bilibili", self.pid)
        other = self.pool.create_pool("taobao", "停用池")["id"]
        self.pool.update_pool(other, {"enabled": False})
        with self.assertRaises(AccountError):
            self.pool.lease_account("taobao", other, "stopped")
        self.pool.remove_member(self.pid, a["id"])
        restored = AccountPool(self.pool.db_path, vault=self.vault)
        self.assertEqual(restored.members(self.pid), [])
        restored.close()

    def test_atomic_lease_across_two_pool_instances(self):
        self.add()
        second = AccountPool(self.pool.db_path, vault=self.vault)
        self.addCleanup(second.close)
        gate = threading.Barrier(2)
        def acquire(pool):
            gate.wait()
            try:
                account, _, token = pool.lease_account("taobao", self.pid, "thread")
                return account.id, token
            except NoAccountAvailable:
                return None
        with ThreadPoolExecutor(2) as executor:
            outcomes = list(executor.map(acquire, (self.pool, second)))
        self.assertEqual(sum(x is not None for x in outcomes), 1)
        self.assertEqual(self.pool.list()[0].used_today, 1)

    def test_expired_lease_reclaimed_without_stale_token_release(self):
        a = self.add()
        _, _, old = self.pool.lease_account("taobao", self.pid, "old")
        with self.pool._conn() as c:
            c.execute("UPDATE account_leases SET lease_until=0")
        _, _, new = self.pool.lease_account("taobao", self.pid, "new")
        self.pool.release_lease(a["id"], old)
        self.assertTrue(self.pool.renew_lease(a["id"], new))

    def test_legacy_migration_preserves_ciphertext_and_usage(self):
        path = self.root / "legacy.db"
        blob = self.vault.encrypt({"cookies": cookies()})
        with sqlite3.connect(path) as c:
            c.execute("CREATE TABLE accounts(id INTEGER PRIMARY KEY, site TEXT, label TEXT, state TEXT, "
                      "credential TEXT, credential_kind TEXT, cooldown_until INTEGER, backoff_level INTEGER,"
                      "success_count INTEGER, fail_count INTEGER, consecutive_fail INTEGER, daily_limit INTEGER,"
                      "used_today INTEGER, quota_date TEXT, last_used_at INTEGER, created_at INTEGER, note TEXT,meta TEXT)")
            c.execute("INSERT INTO accounts VALUES (1,'taobao','原账号','healthy',?,'cookie',0,0,2,1,0,200,7,?,0,0,'','{}')",
                      (blob, self.pool._today()))
        c.close()
        migrated = AccountPool(path, vault=self.vault)
        self.addCleanup(migrated.close)
        with migrated._conn() as c:
            self.assertEqual(c.execute("SELECT credential FROM accounts").fetchone()[0], blob)
        self.assertEqual((migrated.get(1).used_today, migrated.get(1).login_status), (7, "expired"))
        self.assertEqual(migrated.pools("taobao")[0]["members"], 1)

    def test_task_keeps_one_account_and_releases_after_cleanup(self):
        a = self.add()
        store = Store(self.root / "web.db")
        self.addCleanup(store.close)
        cleanup = threading.Event()
        def run(ctx):
            first, _ = ctx.take_account()
            same, _ = ctx.take_account()
            self.assertEqual(first.id, same.id)
            self.assertTrue(self.pool.account_details(first)["busy"])
            ctx.report_account(True)
            self.assertTrue(self.pool.account_details(first)["busy"])
            cleanup.set()
        module = ModuleType("isolated")
        module.META = {"id": "isolated", "name": "隔离", "params": [], "needs_account": True, "account_site": "taobao", "exclusive": False}
        module.run = run
        reg = Registry(Path("__none__"))
        reg._items["isolated"] = CrawlerInfo(module, Path("<test>"))
        runner = TaskRunner(reg, LogBus(), account_pool=self.pool, on_task_change=lambda t: store.save_task(t.to_dict()))
        task = runner.submit("isolated", {}, account_pool_id=self.pid)
        runner._executor.shutdown(wait=True)
        self.assertTrue(cleanup.is_set())
        self.assertEqual(task.state, "success", task.error)
        self.assertFalse(self.pool.account_details(self.pool.get(a["id"]))["busy"])
        self.assertEqual(self.pool.get(a["id"]).used_today, 1)
        history = store.task_history()[0]
        self.assertEqual((history["account_pool_id"], history["account_id"], history["account"]), (self.pid, a["id"], "账号一"))

    def test_failure_and_cancellation_release_without_account_penalty(self):
        a = self.add()
        for mode in ("failed", "canceled"):
            with self.subTest(mode=mode):
                entered, exited = threading.Event(), threading.Event()
                def run(ctx):
                    ctx.take_account()
                    entered.set()
                    try:
                        if mode == "failed":
                            raise RuntimeError("解析失败，不是账号问题")
                        ctx._account_take(ctx.crawler_id)
                        ctx._cancel_check = lambda: True
                        ctx.check_cancel()
                    finally:
                        self.assertTrue(self.pool.account_details(self.pool.get(a["id"]))["busy"])
                        exited.set()
                module = ModuleType("release")
                module.META = {"id": "release", "name": "退出测试", "params": [], "needs_account": True,
                               "account_site": "taobao", "exclusive": False}
                module.run = run
                reg = Registry(Path("__none__"))
                reg._items["release"] = CrawlerInfo(module, Path("<test>"))
                runner = TaskRunner(reg, LogBus(), account_pool=self.pool)
                task = runner.submit("release", {}, account_pool_id=self.pid)
                runner._executor.shutdown(wait=True)
                self.assertTrue(entered.is_set() and exited.is_set())
                self.assertEqual(task.state, mode, task.error)
                self.assertFalse(self.pool.account_details(self.pool.get(a["id"]))["busy"])
                self.assertEqual(self.pool.get(a["id"]).fail_count, 0)


class APIImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.app = create_app(web_db=root / "web.db", account_db=root / "accounts.db", legacy_sources=[])
        ext = self.app.extensions["zforge"]
        self.addCleanup(ext["store"].close)
        self.addCleanup(ext["pool"].close)
        self.addCleanup(lambda: ext["runner"].shutdown(wait=True))
        self.client = self.app.test_client()
        self.pid = ext["pool"].resolve_pool("taobao")

    def test_batch_partial_failure_and_json_errors(self):
        response = self.client.post(f"/api/account-pools/{self.pid}/import", data={"files": [
            (io.BytesIO(json.dumps(cookies()).encode()), "账号一.json"),
            (io.BytesIO(b"broken-secret"), "错误.json")]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["status"] for r in response.json["results"]], ["imported", "failed"])
        self.assertNotIn("isolated-secret", response.text)
        self.assertNotIn("broken-secret", response.text)
        self.assertEqual(self.client.get(f"/api/account-pools/{self.pid}").json["pool"]["members"], 1)
        self.assertEqual(self.client.patch(f"/api/account-pools/{self.pid}", json={"enabled": False}).status_code, 400)
        large = self.client.post(f"/api/account-pools/{self.pid}/import", data=b" " * (2*1024*1024+1), content_type="multipart/form-data; boundary=isolated")
        self.assertEqual(large.status_code, 413)
        large.close()

    def test_task_and_workflow_validate_and_persist_pool(self):
        client = self.client
        bili = self.app.extensions["zforge"]["pool"].resolve_pool("bilibili")
        for invalid in (bili, "1", True, 0):
            self.assertEqual(client.post("/api/tasks", json={"crawler": "taobao", "params": {"keyword": "测试"}, "account_pool_id": invalid}).status_code, 400)
        self.assertEqual(client.post("/api/tasks", json={"crawler": "bilibili", "params": {"keyword": "测试"}, "account_pool_id": self.pid}).status_code, 400)
        payload = {"id": "wf-isolated", "name": "隔离配置", "steps": [{"crawler": "taobao", "params": {"keyword": "测试"}, "account_pool_id": self.pid}]}
        response = client.post("/api/workflows", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        steps = client.get("/api/workflows").json["workflows"][0]["steps"]
        self.assertEqual(steps[0]["account_pool_id"], self.pid)


if __name__ == "__main__":
    unittest.main()
