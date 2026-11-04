# -*- coding: utf-8 -*-
"""
工作流
======

把 N 个任务串成一条流水线, 点一次开始, 然后去睡觉。

    [B站爬 python] → [B站爬 mysql] → [淘宝爬 显示器]
           ↓                ↓                ↓
        成功              成功         失败(按策略处理)

失败策略 (每步可单独设):
  continue  这一步失败也继续跑后面的 (默认)
  abort     这一步失败就中止整个工作流
  retry     先重试 N 次, 还失败再按 abort 处理
"""

from __future__ import annotations

import threading
import time
import uuid

from .logbus import LogBus
from .taskrunner import FINISHED, FAILED, SUCCESS, TaskError, TaskRunner


class WorkflowError(Exception):
    """工作流层面的错误 (配置不对、找不到、已在跑)。"""


class WorkflowRunner:
    """工作流执行器。一个工作流跑在一个独立线程里, 串行执行各步骤。"""

    def __init__(self, runner: TaskRunner, store, bus: LogBus) -> None:
        self.runner = runner
        self.store = store
        self.bus = bus
        self._runs: dict[str, dict] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    def restore(self, history: list[dict]) -> None:
        for record in history:
            run = dict(record)
            if run["state"] == "running":
                run["state"] = "failed"
                run["error"] = "服务重启，工作流已中断；请手动重新运行"
                run["finished_at"] = int(time.time())
                self.store.save_workflow_run(run)
            self._runs[run["run_id"]] = run

    # ---------------- 启动 ----------------
    def start(self, workflow: dict) -> dict:
        steps = workflow.get("steps") or []
        if not steps:
            raise WorkflowError("工作流没有步骤")
        # 启动前核对所有步骤；已停用的池不能悄悄改用默认池。
        try:
            for step in steps:
                info = self.runner.registry.get(step["crawler"])
                if info is None:
                    raise TaskError("采集器已不存在")
                self.runner.validate_pool(info, step.get("account_pool_id"))
        except TaskError as error:
            raise WorkflowError(str(error)) from error

        run_id = "run-" + time.strftime("%y%m%d") + "-" + uuid.uuid4().hex[:6]
        run = {
            "run_id": run_id,
            "workflow_id": workflow["id"],
            "workflow_name": workflow.get("name", ""),
            "state": "running",
            "current_step": 0,
            "total_steps": len(steps),
            "step_results": [],
            "task_ids": [],
            "started_at": int(time.time()),
            "finished_at": 0,
            "error": "",
        }
        with self._lock:
            self._runs[run_id] = run
            self._cancel[run_id] = threading.Event()

        self.store.save_workflow_run(run)
        self.bus.state(run_id, "running", 0.0,
                       f"工作流『{workflow.get('name')}』开始，共 {len(steps)} 步")
        self.bus.log(run_id, "INFO",
                     f"=== 工作流启动: {workflow.get('name')} ({len(steps)} 步) ===")

        threading.Thread(target=self._run, args=(run_id, workflow), daemon=True,
                         name=f"wf-{run_id}").start()
        return run

    def _run(self, run_id: str, workflow: dict) -> None:
        steps = workflow.get("steps") or []
        cancel = self._cancel[run_id]
        total = len(steps)

        try:
            for idx, step in enumerate(steps):
                if cancel.is_set():
                    self._finish(run_id, "canceled", error="用户取消")
                    return

                cid = step.get("crawler")
                params = step.get("params") or {}
                on_fail = (step.get("on_fail") or "continue").lower()
                retries = int(step.get("retries") or 0)
                label = step.get("label") or cid

                run = self._runs[run_id]
                run["current_step"] = idx + 1
                self.bus.state(
                    run_id, "running", idx / total,
                    f"第 {idx+1}/{total} 步 · {label}",
                    current_step=idx + 1, step_label=label,
                )
                self.bus.log(run_id, "INFO", f"--- 第 {idx+1}/{total} 步: {label} ({cid}) ---")

                task = None
                attempt = 0
                while True:
                    attempt += 1
                    try:
                        task = self.runner.submit(
                            cid, params, workflow_id=run_id, _internal=True,
                            account_pool_id=step.get("account_pool_id"),
                        )
                    except TaskError as e:
                        self.bus.log(run_id, "ERR", f"派活失败: {e}")
                        if attempt <= retries:
                            if cancel.wait(min(30, 3 * attempt)):
                                self._finish(run_id, "canceled", error="用户取消")
                                return
                            continue
                        run["step_results"].append(
                            {"step": idx + 1, "label": label, "state": "failed", "error": str(e)})
                        if on_fail in ("abort", "retry"):
                            self._finish(run_id, "failed", error=f"第 {idx+1} 步派活失败: {e}")
                            return
                        break

                    run["task_ids"].append(task.task_id)
                    self.bus.log(run_id, "INFO", f"已派活 → {task.task_id}")

                    # 等这一步跑完
                    while task.state not in FINISHED:
                        if cancel.is_set():
                            try:
                                self.runner.cancel(task.task_id)
                            except TaskError:
                                pass
                            self._finish(run_id, "canceled", error="用户取消")
                            return
                        time.sleep(0.25)

                    if task.state == SUCCESS:
                        run["step_results"].append({
                            "step": idx + 1, "label": label, "task_id": task.task_id,
                            "state": "success", "items": task.items, "duration": task.duration,
                        })
                        self.bus.log(run_id, "OK",
                                     f"第 {idx+1} 步完成 · 采集 {task.items} 条 · {task.duration}s")
                        break

                    # 失败
                    if attempt <= retries:
                        self.bus.log(run_id, "WARN",
                                     f"第 {idx+1} 步失败，准备第 {attempt+1} 次尝试: {task.error}")
                        if cancel.wait(min(30, 3 * attempt)):
                            self._finish(run_id, "canceled", error="用户取消")
                            return
                        continue

                    run["step_results"].append({
                        "step": idx + 1, "label": label, "task_id": task.task_id,
                        "state": "failed", "error": task.error, "attempts": attempt,
                    })
                    self.bus.log(run_id, "ERR", f"第 {idx+1} 步失败: {task.error}")
                    if on_fail in ("abort", "retry"):
                        self._finish(run_id, "failed",
                                     error=f"第 {idx+1} 步失败: {task.error}")
                        return
                    break

                self.store.save_workflow_run(self._runs[run_id])

            # 全部跑完
            results = self._runs[run_id]["step_results"]
            ok = sum(1 for r in results if r.get("state") == "success")
            if ok == total:
                self._finish(run_id, "success")
            elif ok == 0:
                self._finish(run_id, "failed", error="所有步骤都失败了")
            else:
                self._finish(run_id, "success", error=f"部分成功: {ok}/{total} 步")

        except Exception as e:
            self._finish(run_id, "failed", error=f"{type(e).__name__}: {e}")

    def _finish(self, run_id: str, state: str, error: str = "") -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        run["state"] = state
        run["finished_at"] = int(time.time())
        run["error"] = error
        self.store.save_workflow_run(run)

        dur = run["finished_at"] - run["started_at"]
        ok = sum(1 for r in run["step_results"] if r.get("state") == "success")
        items = sum(r.get("items", 0) for r in run["step_results"])

        msg = f"工作流{'完成' if state == 'success' else ('已取消' if state == 'canceled' else '失败')}" \
              f" · {ok}/{run['total_steps']} 步成功 · 共采集 {items} 条 · 耗时 {dur}s"
        if error:
            msg += f" · {error}"
        self.bus.log(run_id, "OK" if state == "success" else "ERR", msg)
        self.bus.state(run_id, state, 1.0, msg)
        self.bus.done(run_id, state, items=items, duration=dur, error=error)

    # ---------------- 查询 / 控制 ----------------
    def cancel(self, run_id: str) -> dict:
        with self._lock:
            ev = self._cancel.get(run_id)
            run = self._runs.get(run_id)
        if ev is None or run is None:
            raise WorkflowError(f"工作流实例不存在: {run_id}")
        if run["state"] != "running":
            raise WorkflowError(f"工作流已经结束了 ({run['state']})")
        ev.set()
        self.bus.log(run_id, "WARN", "收到取消指令，将在当前步骤结束后中止")
        return run

    def get(self, run_id: str) -> dict | None:
        return self._runs.get(run_id)

    def list_runs(self, limit: int = 30) -> list[dict]:
        with self._lock:
            runs = sorted(self._runs.values(), key=lambda r: -r["started_at"])
        return runs[:limit]


if __name__ == "__main__":
    """自测: 造一个两步工作流, 验证串行执行 + 失败策略 + 取消。"""
    import sys
    from pathlib import Path
    from types import ModuleType

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from server.core.logbus import LogBus as LB
    from server.core.registry import CrawlerInfo, Registry
    from server.core.taskrunner import TaskRunner

    class _NullStore:
        def save_workflow_run(self, run):
            pass

    def make_mod(cid, fail=False, emit=2, delay=0.03):
        m = ModuleType(cid)
        m.META = {"id": cid, "name": f"爬虫{cid}", "site": "t", "exclusive": False,
                  "params": [{"key": "kw", "label": "词", "type": "text", "default": "x"}]}

        def run(ctx):
            for i in range(emit):
                ctx.check_cancel()
                ctx.sleep(delay)
                ctx.progress_step(i + 1, emit)
                ctx.emit_item({"title": f"{cid}-{ctx.get_str('kw')}-{i}", "price": 1.0, "pay_count": 10})
            if fail:
                raise RuntimeError("故意失败")

        m.run = run
        return m

    reg = Registry(Path("__none__"))
    reg._items = {}
    for cid, kw in (("ok", {}), ("bad", {"fail": True})):
        reg._items[cid] = CrawlerInfo(make_mod(cid, **kw), Path(f"<t/{cid}>"))

    bus = LB()
    runner = TaskRunner(reg, bus, max_workers=2)
    wf = WorkflowRunner(runner, store=_NullStore(), bus=bus)

    print("=" * 60)
    print(" WorkflowRunner 自测")
    print("=" * 60)

    # 1) 正常两步
    run = wf.start({"id": "w1", "name": "两步", "steps": [
        {"crawler": "ok", "params": {"kw": "a"}, "label": "第一步"},
        {"crawler": "ok", "params": {"kw": "b"}, "label": "第二步"},
    ]})
    while wf.get(run["run_id"])["state"] == "running":
        time.sleep(0.05)
    r = wf.get(run["run_id"])
    assert r["state"] == "success", r
    assert len(r["step_results"]) == 2
    assert all(s["state"] == "success" for s in r["step_results"])
    assert r["current_step"] == 2
    print(f"[ok] 两步串行完成: {r['state']}, 步骤 {[s['items'] for s in r['step_results']]}")

    # 2) continue 策略: 中间失败仍继续
    run2 = wf.start({"id": "w2", "name": "容忍失败", "steps": [
        {"crawler": "ok", "params": {"kw": "a"}},
        {"crawler": "bad", "params": {"kw": "b"}, "on_fail": "continue"},
        {"crawler": "ok", "params": {"kw": "c"}},
    ]})
    while wf.get(run2["run_id"])["state"] == "running":
        time.sleep(0.05)
    r2 = wf.get(run2["run_id"])
    assert len(r2["step_results"]) == 3, r2
    assert r2["step_results"][1]["state"] == "failed"
    assert r2["step_results"][2]["state"] == "success", "continue 策略没有继续执行"
    print(f"[ok] continue 策略: 第2步失败, 第3步仍执行 → {r2['state']}")

    # 3) abort 策略: 失败即停
    run3 = wf.start({"id": "w3", "name": "失败即停", "steps": [
        {"crawler": "bad", "params": {"kw": "a"}, "on_fail": "abort"},
        {"crawler": "ok", "params": {"kw": "b"}},
    ]})
    while wf.get(run3["run_id"])["state"] == "running":
        time.sleep(0.05)
    r3 = wf.get(run3["run_id"])
    assert r3["state"] == "failed", r3
    assert len(r3["step_results"]) == 1, "abort 后不应再执行后续步骤"
    print(f"[ok] abort 策略: 第1步失败即停 → {r3['state']}")

    # 4) 取消
    runner2 = TaskRunner(reg, bus, max_workers=2)
    wf2 = WorkflowRunner(runner2, store=_NullStore(), bus=bus)

    slow = ModuleType("slow")
    slow.META = {"id": "slow", "name": "慢", "site": "t", "exclusive": False, "params": []}
    def slow_run(ctx):
        for i in range(100):
            ctx.check_cancel()
            ctx.sleep(0.05)
    slow.run = slow_run
    reg._items["slow"] = CrawlerInfo(slow, Path("<t/slow>"))

    run4 = wf2.start({"id": "w4", "name": "取消", "steps": [{"crawler": "slow", "params": {}}]})
    time.sleep(0.3)
    wf2.cancel(run4["run_id"])
    for _ in range(200):
        if wf2.get(run4["run_id"])["state"] != "running":
            break
        time.sleep(0.05)
    r4 = wf2.get(run4["run_id"])
    assert r4["state"] == "canceled", r4
    print(f"[ok] 取消生效 → {r4['state']}")

    print(f"[ok] 实例列表 {len(wf.list_runs())} 条")
    runner.shutdown()
    runner2.shutdown()
    print("\n[WorkflowRunner] 全部自测通过")
