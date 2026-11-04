# -*- coding: utf-8 -*-
"""
日志总线
========

爬虫写的日志不直接落盘、也不直接推给前端, 而是先进这里, 由这里做两件事:

  1. 环形缓冲 —— 每个任务留最近 N 条, 供新连上来的客户端补历史
  2. 订阅广播 —— 正在监听的 SSE 连接立刻收到新日志

为什么要环形缓冲: 用户可能在任务跑到一半才打开页面。没有缓冲的话
他只能看到"从此刻起"的日志, 前面的全丢了。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

DEFAULT_BUFFER = 800       # 每个任务保留多少条日志
GLOBAL_BUFFER = 4000       # 全局最近日志 (运行日志页用)


@dataclass
class LogLine:
    task_id: str
    ts: float
    level: str
    msg: str

    def to_dict(self) -> dict:
        return {"ts": int(self.ts), "level": self.level, "msg": self.msg, "task_id": self.task_id}


@dataclass
class StateEvent:
    """任务状态变化 (排队/运行/成功/失败/取消) + 进度。"""
    task_id: str
    ts: float
    state: str
    progress: float = 0.0
    message: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "ts": int(self.ts),
            "task_id": self.task_id,
            "state": self.state,
            "progress": round(self.progress, 4),
            "message": self.message,
        }
        d.update(self.extra)
        return d


class LogBus:
    """
    线程安全的日志与事件总线。

    一个订阅者 = 一个回调函数 + 一个可选的 task_id 过滤。
    回调签名: fn(kind: str, payload: dict) -> None
        kind = "log" | "state" | "done"
    """

    def __init__(self, per_task_buffer: int = DEFAULT_BUFFER) -> None:
        self._lock = threading.RLock()
        self._buffers: dict[str, deque[LogLine]] = {}
        self._states: dict[str, StateEvent] = {}
        self._global: deque[LogLine] = deque(maxlen=GLOBAL_BUFFER)
        self._subs: dict[int, tuple[Callable, str | None]] = {}
        self._next_sub = 1
        self._per_task = per_task_buffer

    # ---------------- 写入 ----------------
    def log(self, task_id: str, level: str, msg: str) -> LogLine:
        # msg 原样进入内存缓冲与 SSE，不自动脱敏；禁止传入 Cookie、认证头或手机号。
        # 账号显示名也可能是手机号，分享日志页面截图前须检查。
        line = LogLine(task_id=task_id, ts=time.time(), level=level.upper(), msg=str(msg))
        with self._lock:
            buf = self._buffers.get(task_id)
            if buf is None:
                buf = deque(maxlen=self._per_task)
                self._buffers[task_id] = buf
            buf.append(line)
            self._global.append(line)
            subs = list(self._subs.values())
        payload = line.to_dict()
        for fn, filt in subs:
            if filt is None or filt == task_id:
                self._safe(fn, "log", payload)
        return line

    def state(self, task_id: str, state: str, progress: float = 0.0,
              message: str = "", **extra: Any) -> StateEvent:
        ev = StateEvent(task_id=task_id, ts=time.time(), state=state,
                        progress=progress, message=message, extra=extra)
        with self._lock:
            self._states[task_id] = ev
            subs = list(self._subs.values())
        payload = ev.to_dict()
        for fn, filt in subs:
            if filt is None or filt == task_id:
                self._safe(fn, "state", payload)
        return ev

    def done(self, task_id: str, state: str, **extra: Any) -> None:
        payload = {"ts": int(time.time()), "task_id": task_id, "state": state}
        payload.update(extra)
        with self._lock:
            subs = list(self._subs.values())
        for fn, filt in subs:
            if filt is None or filt == task_id:
                self._safe(fn, "done", payload)

    @staticmethod
    def _safe(fn: Callable, kind: str, payload: dict) -> None:
        try:
            fn(kind, payload)
        except Exception:
            # 一个订阅者出问题不能影响其他人, 更不能影响爬虫
            pass

    # ---------------- 读取 ----------------
    def history(self, task_id: str, limit: int | None = None) -> list[dict]:
        with self._lock:
            buf = self._buffers.get(task_id)
            if not buf:
                return []
            lines = list(buf)
        if limit:
            lines = lines[-limit:]
        return [l.to_dict() for l in lines]

    def last_state(self, task_id: str) -> dict | None:
        with self._lock:
            ev = self._states.get(task_id)
        return ev.to_dict() if ev else None

    def recent(self, limit: int = 200, level: str | None = None) -> list[dict]:
        """全局最近日志 (运行日志页用)。"""
        with self._lock:
            lines = list(self._global)
        if level:
            lines = [l for l in lines if l.level == level.upper()]
        return [l.to_dict() for l in lines[-limit:]]

    # ---------------- 订阅 ----------------
    def subscribe(self, fn: Callable, task_id: str | None = None) -> int:
        with self._lock:
            sid = self._next_sub
            self._next_sub += 1
            self._subs[sid] = (fn, task_id)
        return sid

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            self._subs.pop(sid, None)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    # ---------------- 清理 ----------------
    def drop(self, task_id: str) -> None:
        """任务被删除时清掉它的缓冲。"""
        with self._lock:
            self._buffers.pop(task_id, None)
            self._states.pop(task_id, None)

    def stats(self) -> dict:
        with self._lock:
            return {
                "tasks_buffered": len(self._buffers),
                "global_lines": len(self._global),
                "subscribers": len(self._subs),
            }


if __name__ == "__main__":
    import threading as th

    bus = LogBus(per_task_buffer=5)
    got: list[tuple[str, dict]] = []
    lock = th.Lock()

    sid = bus.subscribe(lambda k, p: (lock.acquire(), got.append((k, p)), lock.release()), "t1")

    for i in range(8):
        bus.log("t1", "INFO", f"第 {i} 条")
    bus.log("t2", "ERR", "别的任务的日志")
    bus.state("t1", "running", 0.5, "进行中")

    lines = bus.history("t1")
    assert len(lines) == 5, f"环形缓冲应为 5, 实际 {len(lines)}"
    assert lines[-1]["msg"] == "第 7 条"
    print(f"[ok] 环形缓冲生效: 写 8 条只留 5 条, 最后一条 = {lines[-1]['msg']}")

    kinds = [k for k, _ in got]
    assert "state" in kinds
    assert all(p.get("task_id") == "t1" for _, p in got), "订阅过滤失效: 收到了 t2 的日志"
    print(f"[ok] 订阅过滤生效: 收到 {len(got)} 条事件, 全部属于 t1")

    bus.log("t1", "WARN", "又来一条")
    assert len(bus.history("t1")) == 5
    print("[ok] 超限后仍为 5 条")

    bus.done("t1", "success", stats={"items": 3})
    assert kinds[-1] != "done"
    print("[ok] done 事件已发出")

    print(f"[ok] 全局日志 {len(bus.recent())} 条, 统计 {bus.stats()}")
    bus.unsubscribe(sid)
    assert bus.subscriber_count == 0
    bus.drop("t1")
    assert bus.history("t1") == []
    print("[ok] 退订与清理正常")
    print("\n[LogBus] 全部自测通过")
