# -*- coding: utf-8 -*-
"""
爬虫注册表
==========

扫描 `01_网页爬虫/` 下的模块目录, 读取每个爬虫的 META, 建立索引。

**加一个新爬虫不需要改这个文件, 也不改前端。**
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path
from types import ModuleType
from typing import Any

from .contract import META_TEMPLATE


class RegistryError(Exception):
    pass


class CrawlerInfo:
    """一个已注册爬虫的元信息。"""

    def __init__(self, module: ModuleType, source: Path) -> None:
        self.module = module
        self.source = source
        self.meta: dict[str, Any] = dict(module.META)
        self.run = module.run

    # ---- 校验 ----
    def validate(self) -> list[str]:
        problems: list[str] = []
        m = self.meta
        for key in ("id", "name", "params"):
            if key not in m:
                problems.append(f"缺少必需字段 META['{key}']")
        if not isinstance(m.get("params"), list):
            problems.append("META['params'] 必须是 list")
        if m.get("needs_account") and not m.get("account_site"):
            problems.append("needs_account=True 时必须给 account_site")
        collection = m.get("collection")
        if collection is not None:
            if not isinstance(collection, dict) or collection.get("mode") not in ("http", "selenium", "hybrid"):
                problems.append("collection 必须声明有效的 mode")
            elif not isinstance(collection.get("methods"), list) or not collection["methods"]:
                problems.append("collection.methods 必须是非空列表")
            elif any(method not in ("http", "selenium") for method in collection["methods"]):
                problems.append("collection.methods 包含未支持的采集方式")
        authentication = m.get("authentication")
        if authentication is not None:
            expected = "required" if m.get("needs_account") else "none"
            if not isinstance(authentication, dict) or authentication.get("requirement") != expected:
                problems.append("authentication.requirement 与 needs_account 不一致")
        for i, p in enumerate(m.get("params") or []):
            if "key" not in p:
                problems.append(f"params[{i}] 缺少 key")
            if p.get("type") not in (None, "text", "number", "select", "switch"):
                problems.append(f"params[{i}] type 不合法: {p.get('type')}")
            if p.get("type") == "select" and not p.get("options"):
                problems.append(f"params[{i}] 是 select 但没给 options")
        if not callable(getattr(self.run, "__call__", None)):
            problems.append("模块必须导出可调用的 run")
        return problems

    @property
    def id(self) -> str:
        return self.meta.get("id", "")

    def to_dict(self) -> dict:
        """给接口层用的表示 —— 只带安全字段。"""
        m = self.meta
        return {
            "id": m.get("id"),
            "name": m.get("name"),
            "site": m.get("site", ""),
            "desc": m.get("desc", ""),
            "accent": m.get("accent", "#4f8cff"),
            "collection": m.get("collection"),
            "authentication": m.get("authentication") or {
                "requirement": "required" if m.get("needs_account") else "none",
                "credential_types": [],
            },
            "needs_account": bool(m.get("needs_account")),
            "account_site": m.get("account_site"),
            "exclusive": bool(m.get("exclusive", True)),
            "est_seconds": m.get("est_seconds", 60),
            "params": m.get("params", []),
            "source": str(self.source),
            "problems": self.validate(),
        }


class Registry:
    """
    爬虫注册表。

    扫描规则: `搜索引擎根目录/01_网页爬虫/<名字>/crawler.py`
    也兼容 `<名字>/<名字>.py`。下划线开头的目录 (如 `_模板`) 自动跳过。
    """

    def __init__(self, crawlers_root: Path) -> None:
        self.root = Path(crawlers_root)
        self._items: dict[str, CrawlerInfo] = {}
        self._errors: list[dict] = []

    # ---------------- 扫描 ----------------
    def _candidate_files(self) -> list[Path]:
        if not self.root.exists():
            return []
        found: list[Path] = []
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or d.name.startswith(("_", ".")):
                continue
            for name in ("crawler.py", f"{d.name}.py", "main.py"):
                f = d / name
                if f.exists():
                    found.append(f)
                    break
        return found

    @staticmethod
    def _load_module(path: Path) -> ModuleType:
        mod_name = f"zforge_crawler_{path.parent.name}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise RegistryError(f"无法加载 {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        return mod

    def scan(self) -> "Registry":
        """重新扫描。可以随时调用 (热重载)。"""
        self._items.clear()
        self._errors.clear()

        for f in self._candidate_files():
            try:
                mod = self._load_module(f)
            except Exception as e:
                self._errors.append({
                    "source": str(f),
                    "error": f"导入失败: {type(e).__name__}: {e}",
                    "trace": traceback.format_exc(limit=4),
                })
                continue

            if not hasattr(mod, "META"):
                self._errors.append({"source": str(f), "error": "模块没有导出 META"})
                continue
            if not hasattr(mod, "run") or not callable(mod.run):
                self._errors.append({"source": str(f), "error": "模块没有导出可调用的 run"})
                continue

            info = CrawlerInfo(mod, f)
            problems = info.validate()
            if problems:
                self._errors.append({"source": str(f), "error": "META 校验失败: " + "; ".join(problems)})
                continue

            if info.id in self._items:
                self._errors.append({
                    "source": str(f),
                    "error": f"爬虫 id 重复: {info.id} (已被 {self._items[info.id].source} 占用)",
                })
                continue

            self._items[info.id] = info

        return self

    # ---------------- 查询 ----------------
    @property
    def items(self) -> list[CrawlerInfo]:
        return list(self._items.values())

    @property
    def errors(self) -> list[dict]:
        return list(self._errors)

    def get(self, crawler_id: str) -> CrawlerInfo | None:
        return self._items.get(crawler_id)

    def require(self, crawler_id: str) -> CrawlerInfo:
        info = self._items.get(crawler_id)
        if info is None:
            known = ", ".join(self._items) or "(一个都没有)"
            raise RegistryError(f"没有名为『{crawler_id}』的爬虫。已注册: {known}")
        return info

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, crawler_id: object) -> bool:
        return crawler_id in self._items

    def summary(self) -> dict:
        return {
            "count": len(self._items),
            "ids": list(self._items),
            "errors": self._errors,
        }


def default_crawlers_root() -> Path:
    """从本文件位置反推 `01_网页爬虫/` 目录。"""
    # 05_Web服务/server/core/registry.py -> 上溯三层到项目根
    project_root = Path(__file__).resolve().parents[3]
    return project_root / "01_网页爬虫"
