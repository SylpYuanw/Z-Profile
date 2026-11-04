# -*- coding: utf-8 -*-
"""
任务调度内核
============

把"点一下按钮"变成"一个可观测、可取消、可追溯的任务"。

    queued ──► running ──┬──► success
                         ├──► failed
                         └──► canceled

职责:
  · 建任务、排队、交给线程池执行
  · 把爬虫的 ctx.log / ctx.progress 转成总线事件
  · 支持取消 (爬虫在 check_cancel() 处退出)
  · 汇总任务结果 (采了多少条、报了多少错、耗时多久)
  · 保留任务历史 (内存 + 落库)
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .accounts import AccountPool, AccountError, NoAccountAvailable
from .contract import CrawlCanceled, CrawlContext, ParamError, validate_params
from .logbus import LogBus
from .registry import Registry, RegistryError

# 任务状态
QUEUED = "queued"
RUNNING = "running"
SUCCESS = "success"
FAILED = "failed"
CANCELED = "canceled"
FINISHED = (SUCCESS, FAILED, CANCELED)


@dataclass
class Task:
    task_id: str
    crawler_id: str
    crawler_name: str
    params: dict[str, Any]
    state: str = QUEUED
    progress: float = 0.0
    message: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    items: int = 0
    errors: int = 0
    error: str = ""
    account_label: str = ""
    account_pool_id: int | None = None
    account_id: int | None = None
    workflow_id: str | None = None
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _lease_token: str = field(default="", repr=False)
    _lease_stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _lease_thread: Any = field(default=None, repr=False)
    _lease_lost: bool = field(default=False, repr=False)

    @property
    def duration(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at else time.time()
        return round(end - self.started_at, 2)

    def to_dict(self, *, with_params: bool = True) -> dict:
        d = {
            "task_id": self.task_id,
            "crawler": self.crawler_id,
            "crawler_name": self.crawler_name,
            "state": self.state,
            "progress": round(self.progress, 4),
            "message": self.message,
            "created_at": int(self.created_at),
            "started_at": int(self.started_at) if self.started_at else None,
            "finished_at": int(self.finished_at) if self.finished_at else None,
            "duration": self.duration,
            "items": self.items,
            "errors": self.errors,
            "error": self.error,
            "account": self.account_label,
            "account_pool_id": self.account_pool_id,
            "account_id": self.account_id,
            "workflow_id": self.workflow_id,
        }
        if with_params:
            d["params"] = self.params
        return d


class TaskError(Exception):
    """派活失败 (参数不对、爬虫不存在、没有可用账号...)。"""


class TaskRunner:
    """
    任务调度器。

    线程模型: 一个固定大小的线程池。同一个爬虫如果 META.exclusive=True,
    同时只允许一个实例在跑 —— 避免两个 Selenium 抢同一个账号。
    """

    def __init__(
        self,
        registry: Registry,
        bus: LogBus,
        *,
        account_pool: AccountPool | None = None,
        on_item: Callable[[Task, dict], None] | None = None,
        on_task_change: Callable[[Task], None] | None = None,
        max_workers: int = 4,
    ) -> None:
        self.registry = registry
        self.bus = bus
        self.pool = account_pool
        self.on_item = on_item
        self.on_task_change = on_task_change
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="crawl")
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        # 每个爬虫当前有没有实例在跑 (exclusive 判断用)
        self._running_by_crawler: dict[str, str] = {}

    # ================= 派活 =================
    def restore(self, history: list[dict]) -> None:
        """启动时恢复持久记录；遗留活跃任务只标记中断，不自动重跑。"""
        for record in sorted(history, key=lambda r: r.get("created_at") or 0):
            task = Task(
                task_id=record["task_id"], crawler_id=record["crawler"],
                crawler_name=record.get("crawler_name") or record["crawler"],
                params=record.get("params") or {}, state=record["state"],
                progress=record.get("progress") or 0, message=record.get("message") or "",
                created_at=record.get("created_at") or 0,
                started_at=record.get("started_at") or None,
                finished_at=record.get("finished_at") or None,
                items=record.get("items") or 0, errors=record.get("errors") or 0,
                error=record.get("error") or "", account_label=record.get("account") or "",
                workflow_id=record.get("workflow_id"),
                account_pool_id=record.get("account_pool_id"), account_id=record.get("account_id"),
            )
            if task.state in (QUEUED, RUNNING):
                task.state = FAILED
                task.error = task.message = "服务重启，任务已中断；请手动重新运行"
                task.finished_at = time.time()
                self._changed(task)
            self._tasks[task.task_id] = task
            self._order.append(task.task_id)

    def submit(self, crawler_id: str, raw_params: dict, *,
               workflow_id: str | None = None, _internal: bool = False,
               account_pool_id: int | None = None) -> Task:
        """
        建一个任务并排队。返回 Task (state=queued)。
        参数不合法 / 爬虫不存在 / 被独占占用 都会抛 TaskError, 消息是中文。
        """
        if not isinstance(raw_params, dict):
            raise TaskError("params 必须是 JSON 对象")
        try:
            info = self.registry.require(crawler_id)
        except RegistryError as e:
            raise TaskError(str(e)) from e

        try:
            params = validate_params(info.meta, raw_params or {})
        except ParamError as e:
            raise TaskError(f"参数不合法: {e}") from e

        account_pool_id = self.validate_pool(info, account_pool_id)

        with self._lock:
            if info.meta.get("exclusive", True):
                running = self._running_by_crawler.get(crawler_id)
                if running and self._tasks.get(running) and self._tasks[running].state in (QUEUED, RUNNING):
                    raise TaskError(
                        f"『{info.meta.get('name', crawler_id)}』正在运行中 (任务 {running}), "
                        f"等它跑完或先取消它"
                    )

            task = Task(
                task_id=self._new_id(),
                crawler_id=crawler_id,
                crawler_name=info.meta.get("name", crawler_id),
                params=params,
                workflow_id=workflow_id,
                account_pool_id=account_pool_id,
            )
            self._tasks[task.task_id] = task
            self._order.append(task.task_id)
            self._running_by_crawler[crawler_id] = task.task_id

        self.bus.state(task.task_id, QUEUED, 0.0, "已排队")
        self.bus.log(task.task_id, "INFO",
                     f"任务已创建 · {task.crawler_name} · 参数 {self._fmt_params(params)}")
        self._changed(task)
        self._executor.submit(self._run, task.task_id)
        return task

    def validate_pool(self, info, pool_id):
        if not info.meta.get("needs_account"):
            if pool_id is not None:
                raise TaskError("该采集器无需账号，请勿绑定账号池")
            return None
        if self.pool is None:
            raise TaskError("账号池服务不可用")
        site = info.meta.get("account_site") or info.id
        try:
            return self.pool.resolve_pool(site, pool_id)
        except AccountError as error:
            raise TaskError(str(error)) from error

    @staticmethod
    def _fmt_params(params: dict) -> str:
        parts = []
        for k, v in params.items():
            if isinstance(v, str) and len(v) > 24:
                v = v[:24] + "…"
            parts.append(f"{k}={v}")
        return " ".join(parts) or "(无)"

    def _new_id(self) -> str:
        return "t-" + time.strftime("%y%m%d") + "-" + uuid.uuid4().hex[:6]

    def _changed(self, task: Task) -> None:
        if self.on_task_change:
            try:
                self.on_task_change(task)
            except Exception:
                pass

    # ================= 执行 =================
    def _run(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        info = self.registry.get(task.crawler_id)
        if info is None:
            self._finish(task, FAILED, error="爬虫模块已不存在 (可能被删了)")
            return

        task.state = RUNNING
        task.started_at = time.time()
        self.bus.state(task_id, RUNNING, 0.0, "启动中")
        self._changed(task)

        ctx = CrawlContext(
            task_id=task_id,
            crawler_id=task.crawler_id,
            params=task.params,
            _log=lambda lvl, msg: self.bus.log(task_id, lvl, msg),
            _progress=lambda p, m="": self._on_progress(task, p, m),
            _cancel_check=lambda: task._cancel.is_set(),
            _emit=lambda item: self._on_item(task, item),
        )

        # 挂上账号池
        if info.meta.get("needs_account") and self.pool is not None:
            site = info.meta.get("account_site") or task.crawler_id
            acquired = None

            def _take(_cid: str):
                nonlocal acquired
                ctx.check_cancel()
                if acquired:
                    return acquired
                account, credential, token = self.pool.lease_account(site, task.account_pool_id, task_id)
                task.account_label = account.label
                task.account_id = account.id
                task._lease_token = token
                acquired = account, credential
                task._lease_thread = threading.Thread(target=self._renew_account, args=(task,), daemon=True)
                task._lease_thread.start()
                ctx.log("INFO", f"账号池 #{task.account_pool_id} · 领取『{account.label}』，本任务保持同一账号")
                self._changed(task)
                return account, credential

            def _report(acc_id: int, ok: bool, reason: str = "", ban: bool = False, login_status=None):
                if acc_id != task.account_id:
                    raise AccountError("只能回报本任务领取的账号")
                self.pool.report(acc_id, ok, task_id=task_id, reason=reason, ban=ban)
                if login_status:
                    self.pool.mark_login(acc_id, login_status)

            ctx._account_take = _take
            ctx._account_report = _report

        try:
            ctx.check_cancel()
            info.run(ctx)
            ctx.check_cancel()
        except CrawlCanceled:
            ctx.log("WARN", "任务已被取消")
            self._finish(task, FAILED if task._lease_lost else CANCELED,
                         error="账号占用续约失败，任务已停止" if task._lease_lost else "",
                         items=ctx.items_count, errors=ctx.error_count)
        except NoAccountAvailable as e:
            ctx.err(f"没有可用账号: {e}")
            self._finish(task, FAILED, error=str(e), items=ctx.items_count, errors=ctx.error_count)
        except Exception as e:
            tb = traceback.format_exc()
            ctx.err(f"{type(e).__name__}: {e}")
            for line in tb.strip().splitlines()[-6:]:
                ctx.log("ERR", "  " + line)
            self._finish(task, FAILED, error=f"{type(e).__name__}: {e}",
                         items=ctx.items_count, errors=ctx.error_count)
        else:
            state = FAILED if ctx.error_count and ctx.items_count == 0 else SUCCESS
            self._finish(task, state, items=ctx.items_count, errors=ctx.error_count,
                         error=ctx.last_error if state == FAILED else "")

    def _renew_account(self, task):
        while not task._lease_stop.wait(25):
            try:
                renewed = self.pool.renew_lease(task.account_id, task._lease_token)
            except Exception:
                renewed = False
            if not renewed:
                task._lease_lost = True
                task._cancel.set()
                self.bus.log(task.task_id, "ERR", "账号占用续约失败，正在停止任务")
                return

    def _on_progress(self, task: Task, ratio: float, message: str) -> None:
        task.progress = max(0.0, min(1.0, ratio))
        if message:
            task.message = message
        self.bus.state(task.task_id, RUNNING, task.progress, task.message)
        self._changed(task)

    def _on_item(self, task: Task, item: dict) -> None:
        if self.on_item:
            self.on_item(task, item)
        task.items += 1

    def _finish(self, task: Task, state: str, *, items: int | None = None,
                errors: int | None = None, error: str = "") -> None:
        # 采集器退出（含浏览器清理）后才能释放；先释放再发布结束状态。
        task._lease_stop.set()
        if task._lease_thread:
            task._lease_thread.join(timeout=16)
        if task._lease_token and self.pool:
            self.pool.release_lease(task.account_id, task._lease_token)
            task._lease_token = ""
        task.state = state
        task.finished_at = time.time()
        if items is not None:
            task.items = items
        if errors is not None:
            task.errors = errors
        task.error = error
        if state == SUCCESS:
            task.progress = 1.0

        with self._lock:
            if self._running_by_crawler.get(task.crawler_id) == task.task_id:
                self._running_by_crawler.pop(task.crawler_id, None)

        label = {SUCCESS: "OK", FAILED: "ERR", CANCELED: "WARN"}.get(state, "INFO")
        summary = f"任务{ {'success':'完成','failed':'失败','canceled':'已取消'}.get(state, state) }" \
                  f" · 采集 {task.items} 条 · 耗时 {task.duration}s"
        if error:
            summary += f" · 原因: {error}"
        task.message = summary
        self.bus.log(task.task_id, label, summary)
        self.bus.state(task.task_id, state, 1.0 if state == SUCCESS else task.progress, summary)
        self.bus.done(task.task_id, state, items=task.items, errors=task.errors,
                      duration=task.duration, error=error)
        self._changed(task)

    # ================= 取消 / 查询 =================
    def cancel(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskError(f"任务不存在: {task_id}")
        if task.state in FINISHED:
            raise TaskError(f"任务已经结束了 ({task.state}), 无法取消")
        task._cancel.set()
        self.bus.log(task_id, "WARN", "收到取消指令, 等待爬虫在当前步骤退出…")
        self.bus.state(task_id, task.state, task.progress, "正在取消…")
        self._changed(task)
        return task

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def require(self, task_id: str) -> Task:
        t = self._tasks.get(task_id)
        if t is None:
            raise TaskError(f"任务不存在: {task_id}")
        return t

    def list(self, *, crawler: str | None = None, state: str | None = None,
             limit: int = 50, workflow_id: str | None = None) -> list[Task]:
        with self._lock:
            ids = list(reversed(self._order))
        out = []
        for tid in ids:
            t = self._tasks.get(tid)
            if t is None:
                continue
            if crawler and t.crawler_id != crawler:
                continue
            if state and t.state != state:
                continue
            if workflow_id and t.workflow_id != workflow_id:
                continue
            out.append(t)
            if len(out) >= limit:
                break
        return out

    def delete(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None:
            return False
        if task.state not in FINISHED:
            raise TaskError("只能删除已结束的任务, 运行中的请先取消")
        with self._lock:
            self._tasks.pop(task_id, None)
            if task_id in self._order:
                self._order.remove(task_id)
        self.bus.drop(task_id)
        return True

    def counts(self) -> dict:
        with self._lock:
            tasks = list(self._tasks.values())
        c = {s: 0 for s in (QUEUED, RUNNING, SUCCESS, FAILED, CANCELED)}
        for t in tasks:
            c[t.state] = c.get(t.state, 0) + 1
        c["total"] = len(tasks)
        c["active"] = c[QUEUED] + c[RUNNING]
        return c

    def shutdown(self, wait: bool = False) -> None:
        for t in list(self._tasks.values()):
            if t.state in (QUEUED, RUNNING):
                t._cancel.set()
        self._executor.shutdown(wait=wait, cancel_futures=False)


if __name__ == "__main__":
    import sys
    from pathlib import Path

    # --- 自测: 造两个假爬虫验证调度 ---
    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root / "05_Web服务"))
    from server.core.logbus import LogBus as LB
    from server.core.registry import Registry as Reg
    from types import ModuleType

    def make_mod(cid, sleep_per_step=0.05, fail=False, emit=3, exclusive=True):
        m = ModuleType(cid)
        m.META = {
            "id": cid, "name": f"测试爬虫{cid}", "site": "test.local",
            "params": [
                {"key": "n", "label": "数量", "type": "number", "default": 3, "required": False},
                {"key": "kw", "label": "关键词", "type": "text", "required": True},
            ],
            "exclusive": exclusive,
        }

        def run(ctx):
            ctx.log("INFO", f"开始, kw={ctx.get_str('kw')}")
            total = ctx.get_int("n", 3)
            for i in range(total):
                ctx.check_cancel()
                ctx.sleep(sleep_per_step)
                ctx.progress_step(i + 1, total)
                ctx.emit_item({"title": f"{ctx.get_str('kw')}-{i}", "price": i * 10})
            if fail:
                raise RuntimeError("故意失败")
            ctx.ok("干完了")

        m.run = run
        return m

    reg = Reg(Path("__nonexistent__"))
    reg._items = {}
    for cid, kw in (("aaa", {}), ("bbb", {"fail": True})):
        info_cls = __import__("server.core.registry", fromlist=["CrawlerInfo"]).CrawlerInfo
        mod = make_mod(cid, fail=(cid == "bbb"))
        info = info_cls(mod, Path(f"<test/{cid}>"))
        reg._items[cid] = info

    print("=" * 60)
    print(" TaskRunner 自测")
    print("=" * 60)

    bus = LB()
    items: list[dict] = []
    runner = TaskRunner(reg, bus, on_item=lambda t, it: items.append(it))

    # 1) 正常任务
    t1 = runner.submit("aaa", {"kw": "hello", "n": 4})
    print(f"\n[1] 提交 {t1.task_id} state={t1.state}")
    for _ in range(200):
        if t1.state in FINISHED:
            break
        time.sleep(0.03)
    assert t1.state == SUCCESS, f"应成功, 实际 {t1.state} {t1.error}"
    assert t1.items == 4, f"应采 4 条, 实际 {t1.items}"
    assert len(items) == 4
    print(f"[ok] 成功任务: items={t1.items} 耗时={t1.duration}s")
    logs = bus.history(t1.task_id)
    assert any("干完了" in l["msg"] for l in logs)
    print(f"[ok] 日志 {len(logs)} 条, 含收尾信息")

    # 2) 参数校验
    try:
        runner.submit("aaa", {})
        raise AssertionError("缺必填项竟然通过了")
    except TaskError as e:
        print(f"[ok] 缺必填项被拦截: {e}")
    try:
        runner.submit("aaa", {"kw": "x", "n": "abc"})
        raise AssertionError("类型错误竟然通过了")
    except TaskError as e:
        print(f"[ok] 类型错误被拦截: {e}")
    try:
        runner.submit("nope", {})
        raise AssertionError("不存在的爬虫竟然通过了")
    except TaskError as e:
        print(f"[ok] 未知爬虫被拦截: {str(e)[:50]}")

    # 3) 失败任务
    t2 = runner.submit("bbb", {"kw": "boom"})
    for _ in range(200):
        if t2.state in FINISHED:
            break
        time.sleep(0.03)
    assert t2.state == FAILED
    assert "故意失败" in t2.error
    print(f"[ok] 失败任务: {t2.error}")

    # 4) 取消
    t3 = runner.submit("aaa", {"kw": "cancelme", "n": 50})
    time.sleep(0.12)
    runner.cancel(t3.task_id)
    for _ in range(300):
        if t3.state in FINISHED:
            break
        time.sleep(0.03)
    assert t3.state == CANCELED, f"应被取消, 实际 {t3.state}"
    ran = [l for l in bus.history(t3.task_id) if "任务已创建" in l["msg"]]
    print(f"[ok] 取消生效: state={t3.state}, 采集到中断为止 {t3.items} 条")

    # 5) 独占
    t4 = runner.submit("aaa", {"kw": "hold", "n": 40})
    time.sleep(0.05)
    try:
        runner.submit("aaa", {"kw": "second"})
        raise AssertionError("独占没生效")
    except TaskError as e:
        print(f"[ok] 独占生效: {str(e)[:46]}")
    runner.cancel(t4.task_id)
    for _ in range(300):
        if t4.state in FINISHED:
            break
        time.sleep(0.03)

    # 6) 汇总
    print(f"[ok] 任务统计: {runner.counts()}")
    print(f"[ok] 列表返回 {len(runner.list(limit=10))} 条")

    runner.shutdown()
    print("\n[TaskRunner] 全部自测通过")
