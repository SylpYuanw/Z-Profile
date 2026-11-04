# -*- coding: utf-8 -*-
"""
爬虫模块契约
============

每个爬虫文件必须导出两样东西:

    META : dict     我是谁、需要什么参数、要不要账号
    run(ctx)        干活

`ctx` (CrawlContext) 是爬虫与外界**唯一的通道**。爬虫不直接 print、
不直接写文件、不直接连数据库 —— 全走 ctx, 这样才能被调度、被取消、
被实时观测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


class CrawlCanceled(Exception):
    """用户在页面上点了取消。爬虫应该让它冒泡出去, 由调度器处理善后。"""


# ============================================================
# META 结构说明 (写爬虫时照抄)
# ============================================================
META_TEMPLATE: dict[str, Any] = {
    "id": "taobao",                        # 唯一标识, 前端路由用它
    "name": "淘宝商品采集",                 # 显示名
    "site": "s.taobao.com",                # 目标站点
    "desc": "一句话说明这个爬虫干什么",
    "accent": "#ff5000",                   # 前端卡片强调色
    "needs_account": True,                 # 是否需要从账号池取账号
    "account_site": "taobao",              # 账号所属站点；当前尚无命名池 ID
    "collection": {
        "mode": "selenium", "methods": ["selenium"],
        "stages": [{"label": "页面采集", "method": "selenium"}],
    },
    "authentication": {"requirement": "required", "credential_types": ["cookies"]},
    "exclusive": True,                     # 同一爬虫是否只允许一个实例同时跑
    "est_seconds": 60,                     # 预估耗时, 前端用来显示进度条节奏
    "params": [
        {
            "key": "keyword",
            "label": "搜索关键词",
            "type": "text",                # text | number | select | switch
            "required": True,
            "placeholder": "例如：显示器",
            "help": "补充说明, 会显示在输入框下方",
        },
        {
            "key": "pages",
            "label": "翻页数",
            "type": "number",
            "required": False,
            "default": 1,
            "min": 1,
            "max": 20,
        },
    ],
}


# ============================================================
# 上下文
# ============================================================
@dataclass
class CrawlContext:
    """
    爬虫的执行上下文。

    爬虫只应该通过这些方法跟外界交互:
        ctx.log(...)           写日志 (实时推到页面)
        ctx.progress(...)      报进度
        ctx.check_cancel()     检查是否被取消 (长循环里要定期调)
        ctx.emit_item(...)     交出一条数据 (自动入库)
        ctx.take_account()     领一个账号
        ctx.report_account()   回报账号使用结果
    """

    task_id: str
    crawler_id: str
    params: dict[str, Any]

    # 由调度器注入
    _log: Callable[[str, str], None] = field(default=lambda lvl, msg: None, repr=False)
    _progress: Callable[[float, str], None] = field(default=lambda p, m="": None, repr=False)
    _cancel_check: Callable[[], bool] = field(default=lambda: False, repr=False)
    _emit: Callable[[dict], None] = field(default=lambda item: None, repr=False)
    _account_take: Callable[[str], tuple[Any, Any]] | None = field(default=None, repr=False)
    _account_report: Callable[..., None] | None = field(default=None, repr=False)

    # 运行时累积的统计
    started_at: float = field(default_factory=time.time)
    items_count: int = 0
    error_count: int = 0
    last_error: str = ""
    _account: Any = field(default=None, repr=False)

    # ---------------- 日志 ----------------
    def log(self, level: str, msg: str) -> None:
        """level: INFO | OK | WARN | ERR | SQL"""
        lvl = level.upper()
        if lvl not in ("INFO", "OK", "WARN", "ERR", "SQL"):
            lvl = "INFO"
        if lvl == "ERR":
            self.error_count += 1
            self.last_error = str(msg)
        self._log(lvl, str(msg))

    def info(self, msg: str) -> None:
        self.log("INFO", msg)

    def ok(self, msg: str) -> None:
        self.log("OK", msg)

    def warn(self, msg: str) -> None:
        self.log("WARN", msg)

    def err(self, msg: str) -> None:
        self.log("ERR", msg)

    def sql(self, msg: str) -> None:
        self.log("SQL", msg)

    # ---------------- 进度 ----------------
    def progress(self, ratio: float, message: str = "") -> None:
        """ratio: 0.0 ~ 1.0"""
        self._progress(max(0.0, min(1.0, float(ratio))), message)

    def progress_step(self, current: int, total: int, message: str = "") -> None:
        """按第几步/共几步报进度。total<=0 时按 0 处理。"""
        r = (current / total) if total > 0 else 0.0
        self.progress(r, message or f"第 {current}/{total} 步")

    # ---------------- 取消 ----------------
    def check_cancel(self) -> None:
        """被取消就抛 CrawlCanceled。长循环里必须定期调用。"""
        if self._cancel_check():
            raise CrawlCanceled("用户取消了任务")

    @property
    def canceled(self) -> bool:
        return self._cancel_check()

    def sleep(self, seconds: float) -> None:
        """可中断的等待 —— 用这个代替 time.sleep, 否则取消响应会迟钝。"""
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            self.check_cancel()
            time.sleep(min(0.2, max(0.01, end - time.time())))

    # ---------------- 数据 ----------------
    def emit_item(self, item: dict) -> None:
        """交出一条采集结果。调度器负责归一化入库。"""
        if not isinstance(item, dict):
            raise TypeError("emit_item 需要 dict")
        self._emit(item)
        self.items_count += 1

    def emit_items(self, items: Iterable[dict]) -> None:
        for it in items:
            self.check_cancel()
            self.emit_item(it)

    # ---------------- 账号 ----------------
    def take_account(self):
        """
        领一个账号, 返回 (account, credential)。
        爬虫的 META.needs_account=False 时不要调这个。
        """
        if self._account_take is None:
            raise RuntimeError("这个爬虫没有被配置账号池, META.needs_account 应为 False")
        self._account, cred = self._account_take(self.crawler_id)
        return self._account, cred

    def report_account(self, ok: bool, reason: str = "", ban: bool = False, *, login_status: str | None = None) -> None:
        """回报账号使用结果。不调的话账号池不会积累健康数据。"""
        if self._account_report is None or self._account is None:
            return
        extra = {"login_status": login_status} if login_status else {}
        self._account_report(self._account.id, ok, reason=reason, ban=ban, **extra)

    @property
    def account(self):
        """当前正在用的账号 (可能为 None)。"""
        return self._account

    # ---------------- 参数取值 ----------------
    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.params.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_str(self, key: str, default: str = "") -> str:
        v = self.params.get(key, default)
        return str(v).strip() if v is not None else default

    def get_list(self, key: str, sep: str = ",") -> list[str]:
        """把 'a,b,c' 或 'a，b，c' 切成列表。"""
        raw = self.get_str(key)
        if not raw:
            return []
        for s in ("，", ";", "；", "|"):
            raw = raw.replace(s, sep)
        return [x.strip() for x in raw.split(sep) if x.strip()]


# ============================================================
# 参数校验
# ============================================================
class ParamError(ValueError):
    """参数不符合 META 声明。"""


def validate_params(meta: dict, raw: dict[str, Any]) -> dict[str, Any]:
    """
    按 META.params 校验并补默认值。
    返回清洗后的参数字典; 不合法就抛 ParamError (消息是给人看的中文)。
    """
    out: dict[str, Any] = {}
    declared = {p["key"]: p for p in meta.get("params", [])}

    # 未声明的字段直接丢掉, 防止调用方乱塞
    for key, spec in declared.items():
        val = raw.get(key, spec.get("default"))
        label = spec.get("label", key)

        if val is None or (isinstance(val, str) and not val.strip()):
            if spec.get("required"):
                raise ParamError(f"『{label}』是必填项")
            out[key] = spec.get("default")
            continue

        t = spec.get("type", "text")
        try:
            if t == "number":
                num = float(val)
                if num == int(num):
                    num = int(num)
                lo, hi = spec.get("min"), spec.get("max")
                if lo is not None and num < lo:
                    raise ParamError(f"『{label}』不能小于 {lo}")
                if hi is not None and num > hi:
                    raise ParamError(f"『{label}』不能大于 {hi}")
                out[key] = num
            elif t == "switch":
                if isinstance(val, str):
                    out[key] = val.lower() in ("1", "true", "yes", "on")
                else:
                    out[key] = bool(val)
            elif t == "select":
                allowed = [o["value"] if isinstance(o, dict) else o for o in spec.get("options", [])]
                if allowed and val not in allowed:
                    raise ParamError(f"『{label}』只能是 {allowed} 之一, 收到 {val!r}")
                out[key] = val
            else:  # text
                s = str(val).strip()
                mx = spec.get("max_length")
                if mx and len(s) > mx:
                    raise ParamError(f"『{label}』太长 (最多 {mx} 字)")
                out[key] = s
        except ParamError:
            raise
        except (TypeError, ValueError) as e:
            raise ParamError(f"『{label}』格式不对: 期望 {t}, 收到 {val!r}") from e

    return out
