# -*- coding: utf-8 -*-
"""
ZForge / 账号池
===============

同一个网站多个账号轮换, 避免触发风控。这是把"玩具"变成"系统"的关键模块之一。

────────────────────────────────────────────────────────────────
它解决什么
────────────────────────────────────────────────────────────────
之前的做法: 每个爬虫脚本各自硬编码一个 Cookie / 账号, 用废了改代码。

现在的做法:
  · 账号统一登记在池子里, 所有爬虫共用
  · 领取时自动挑最健康的那个 (健康的 → 用得最少的 → 最久没用过的)
  · 用完回报结果; 失败自动进冷却, 冷却时长随连续失败次数递增
  · 每日使用次数上限, 超了自动跳过
  · 凭据加密存储 (走 vault.py), 数据库里只有密文

────────────────────────────────────────────────────────────────
状态机
────────────────────────────────────────────────────────────────
    healthy ──(连续失败达阈值)──► cooling ──(冷却到期)──► healthy
       │                            │
       └──(凭据失效/被风控)──► banned ──(人工恢复)──► healthy
       │
       └──(人工停用)──────────► disabled ──(人工启用)──► healthy
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .vault import Vault, VaultError
from .account_groups import AccountGroups, AccountError, NoAccountAvailable

# ============================================================
# 状态常量
# ============================================================
HEALTHY = "healthy"
COOLING = "cooling"
BANNED = "banned"
DISABLED = "disabled"

ALL_STATES = (HEALTHY, COOLING, BANNED, DISABLED)

# 连续失败多少次进冷却
FAIL_THRESHOLD = 3
# 冷却时长基数 (秒), 实际时长 = 基数 * 2^(连续失败次数 - 阈值), 封顶
COOLDOWN_BASE = 300          # 5 分钟
COOLDOWN_MAX = 6 * 3600      # 上限 6 小时
# 默认每日上限
DEFAULT_DAILY_LIMIT = 200


# ============================================================
# 数据结构
# ============================================================
@dataclass
class Account:
    """一个账号在数据库里的完整画像。"""

    id: int
    site: str
    label: str
    state: str = HEALTHY
    cooldown_until: int = 0
    backoff_level: int = 0
    success_count: int = 0
    fail_count: int = 0
    consecutive_fail: int = 0
    daily_limit: int = DEFAULT_DAILY_LIMIT
    used_today: int = 0
    last_used_at: int = 0
    created_at: int = 0
    note: str = ""
    meta: dict = field(default_factory=dict)
    credential_kind: str = "cookie"
    login_status: str = "unknown"
    login_checked_at: int = 0
    phone: str = ""

    @property
    def available(self) -> bool:
        """现在能不能用 (不含每日额度判断)。"""
        if self.login_status == "expired":
            return False
        if self.state == HEALTHY:
            return True
        if self.state == COOLING and self.cooldown_until <= time.time():
            return True
        return False

    @property
    def cooldown_remain(self) -> int:
        return max(0, self.cooldown_until - int(time.time()))

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return round(self.success_count / total * 100, 1) if total else 0.0

    def to_dict(self, *, mask: bool = True) -> dict:
        """对外表示。mask=True 绝不带出凭据。"""
        return {
            "id": self.id,
            "site": self.site,
            "label": self.label,
            "state": self.state,
            "available": self.available,
            "cooldown_remain": self.cooldown_remain,
            "backoff_level": self.backoff_level,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "consecutive_fail": self.consecutive_fail,
            "success_rate": self.success_rate,
            "daily_limit": self.daily_limit,
            "used_today": self.used_today,
            "quota_left": max(0, self.daily_limit - self.used_today),
            "last_used_at": self.last_used_at,
            "created_at": self.created_at,
            "note": self.note,
            "has_credential": True,
            "credential_kind": self.credential_kind,
            "login_status": self.login_status,
            "login_checked_at": self.login_checked_at,
            "phone": self.phone,
            "meta": self.meta if not mask else {k: v for k, v in self.meta.items() if k != "secret"},
        }


# ============================================================
# 账号池
# ============================================================
class AccountPool(AccountGroups):
    """
    按站点分组的账号池，通过内部锁串行更新领取计数与状态。
    命名池与成员关系由 AccountGroups 管理；生产任务通过租约领取账号。

    数据库表由 `ensure_schema()` 建; 凭据单独加密存在 accounts 表的
    credential 字段, 只有 `take()` 返回的凭据对象里才有明文。
    """

    def __init__(self, db_path: str | Path, vault: Vault | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._vault = vault or Vault("accounts")
        self._lock = threading.RLock()   # 取号/回报要原子, 多个爬虫线程会同时来
        self.ensure_schema()
        self.ensure_groups_schema()

    # ---------------- 基础设施 ----------------
    @contextmanager
    def _conn(self):
        """
        每次操作开一条独立连接, 用完**立刻关掉**。

        py 的 `with sqlite3.connect(...)` 只管事务提交/回滚, **不会关闭连接**。
        不显式关会让 SQLite 一直持有文件句柄, Windows 上表现为
        "另一个程序正在使用此文件" —— 删库、备份、迁移全都会失败。
        """
        c = sqlite3.connect(self.db_path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def close(self) -> None:
        """WAL 模式下正常关闭后, -wal/-shm 会自动合并回主库文件。"""
        with self._conn() as c:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def ensure_schema(self) -> None:
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    site              TEXT    NOT NULL,
                    label             TEXT    NOT NULL,
                    state             TEXT    NOT NULL DEFAULT 'healthy',
                    credential        TEXT,              -- 加密后的凭据
                    credential_kind   TEXT    DEFAULT 'cookie',
                    cooldown_until    INTEGER DEFAULT 0,
                    backoff_level     INTEGER DEFAULT 0,
                    success_count     INTEGER DEFAULT 0,
                    fail_count        INTEGER DEFAULT 0,
                    consecutive_fail  INTEGER DEFAULT 0,
                    daily_limit       INTEGER DEFAULT 200,
                    used_today        INTEGER DEFAULT 0,
                    quota_date        TEXT,
                    last_used_at      INTEGER DEFAULT 0,
                    created_at        INTEGER DEFAULT 0,
                    note              TEXT    DEFAULT '',
                    meta              TEXT    DEFAULT '{}',
                    UNIQUE(site, label)
                );

                CREATE INDEX IF NOT EXISTS idx_accounts_site  ON accounts(site);
                CREATE INDEX IF NOT EXISTS idx_accounts_state ON accounts(state);

                -- 使用记录, 便于事后分析哪个账号在什么时候被用过
                CREATE TABLE IF NOT EXISTS account_usage (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id  INTEGER NOT NULL,
                    task_id     TEXT,
                    used_at     INTEGER NOT NULL,
                    ok          INTEGER NOT NULL,
                    reason      TEXT DEFAULT '',
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_usage_acc ON account_usage(account_id, used_at DESC);
                """
            )

    # ---------------- 内部工具 ----------------
    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    def _row_to_account(self, r: sqlite3.Row) -> Account:
        try:
            meta = json.loads(r["meta"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        return Account(
            id=r["id"],
            site=r["site"],
            label=r["label"],
            state=r["state"],
            cooldown_until=r["cooldown_until"] or 0,
            backoff_level=r["backoff_level"] or 0,
            success_count=r["success_count"] or 0,
            fail_count=r["fail_count"] or 0,
            consecutive_fail=r["consecutive_fail"] or 0,
            daily_limit=r["daily_limit"] or DEFAULT_DAILY_LIMIT,
            used_today=r["used_today"] or 0,
            last_used_at=r["last_used_at"] or 0,
            created_at=r["created_at"] or 0,
            note=r["note"] or "",
            meta=meta,
            credential_kind=r["credential_kind"] or "cookie",
            login_status=r["login_status"],
            login_checked_at=r["login_checked_at"],
            phone=r["phone"],
        )

    def _reset_daily_if_needed(self, c: sqlite3.Connection) -> None:
        """跨天了就把 used_today 清零。"""
        today = self._today()
        c.execute(
            "UPDATE accounts SET used_today = 0, quota_date = ? "
            "WHERE quota_date IS NULL OR quota_date <> ?",
            (today, today),
        )

    def refresh(self) -> int:
        """
        把冷却到期、连续失败清零。返回恢复健康的账号数。
        每次取号前调用一次, 保证状态是最新的。
        """
        now = int(time.time())
        with self._conn() as c:
            self._reset_daily_if_needed(c)
            cur = c.execute(
                "UPDATE accounts SET state = ?, consecutive_fail = 0, backoff_level = 0, cooldown_until = 0 "
                "WHERE state = ? AND cooldown_until > 0 AND cooldown_until <= ?",
                (HEALTHY, COOLING, now),
            )
            return cur.rowcount

    # ---------------- 增删改 ----------------
    def add(
        self,
        site: str,
        label: str,
        credential: Any,
        *,
        kind: str = "cookie",
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        note: str = "",
        meta: dict | None = None,
    ) -> Account:
        """登记一个账号。credential 会被加密后存库。"""
        if not site or not label:
            raise AccountError("site 和 label 不能为空")
        blob = self._vault.encrypt(credential)
        now = int(time.time())
        with self._conn() as c:
            try:
                cur = c.execute(
                    "INSERT INTO accounts(site, label, credential, credential_kind, daily_limit, "
                    "note, meta, created_at, quota_date) VALUES (?,?,?,?,?,?,?,?,?)",
                    (site, label, blob, kind, daily_limit, note,
                     json.dumps(meta or {}, ensure_ascii=False), now, self._today()),
                )
            except sqlite3.IntegrityError as e:
                raise AccountError(f"账号已存在: {site} / {label}") from e
            acc_id = cur.lastrowid
            pool_id = self._default_pool(c, site)
            c.execute("INSERT INTO account_pool_members VALUES (?,?)", (pool_id, acc_id))
        return self.get(acc_id)  # type: ignore[return-value]

    def update_credential(self, account_id: int, credential: Any, *, kind: str | None = None) -> None:
        """更新凭据 (比如重新扫码登录后)。顺带把状态恢复成健康。"""
        blob = self._vault.encrypt(credential)
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self.require_idle(c, account_id)
            if kind:
                c.execute(
                    "UPDATE accounts SET credential=?, credential_kind=?, state=?, consecutive_fail=0, "
                    "backoff_level=0, cooldown_until=0 WHERE id=?",
                    (blob, kind, HEALTHY, account_id),
                )
            else:
                c.execute(
                    "UPDATE accounts SET credential=?, state=?, consecutive_fail=0, "
                    "backoff_level=0, cooldown_until=0 WHERE id=?",
                    (blob, HEALTHY, account_id),
                )

    def get_credential(self, account_id: int) -> Any:
        """取出明文凭据。**只给爬虫执行时用, 绝不经接口层返回。**"""
        with self._conn() as c:
            r = c.execute("SELECT credential FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not r or not r["credential"]:
            raise AccountError(f"账号 {account_id} 没有凭据")
        try:
            return self._vault.decrypt(r["credential"])
        except VaultError as e:
            raise AccountError(f"账号 {account_id} 凭据解密失败: {e}") from e

    def get(self, account_id: int) -> Account | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return self._row_to_account(r) if r else None

    def list(self, site: str | None = None, state: str | None = None) -> list[Account]:
        sql = "SELECT * FROM accounts WHERE 1=1"
        args: list[Any] = []
        if site:
            sql += " AND site=?"
            args.append(site)
        if state:
            sql += " AND state=?"
            args.append(state)
        sql += " ORDER BY site, id"
        with self._conn() as c:
            self._reset_daily_if_needed(c)
            rows = c.execute(sql, args).fetchall()
        return [self._row_to_account(r) for r in rows]

    def remove(self, account_id: int) -> bool:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self.require_idle(c, account_id)
            return c.execute("DELETE FROM accounts WHERE id=?", (account_id,)).rowcount > 0

    def set_state(self, account_id: int, state: str, *, note: str | None = None) -> None:
        """人工切换状态 (停用 / 恢复 / 标记封禁)。"""
        if state not in ALL_STATES:
            raise AccountError(f"未知状态: {state} (可选 {ALL_STATES})")
        with self._conn() as c:
            if note is not None:
                c.execute(
                    "UPDATE accounts SET state=?, note=?, consecutive_fail=0, backoff_level=0, cooldown_until=0 WHERE id=?",
                    (state, note, account_id),
                )
            else:
                c.execute(
                    "UPDATE accounts SET state=?, consecutive_fail=0, backoff_level=0, cooldown_until=0 WHERE id=?",
                    (state, account_id),
                )

    def sites(self) -> list[str]:
        with self._conn() as c:
            return [r[0] for r in c.execute("SELECT DISTINCT site FROM accounts ORDER BY site")]

    # ---------------- 领号 ----------------
    def take(self, site: str) -> tuple[Account, Any]:
        """
        领一个可用账号, 返回 (账号, 明文凭据)。

        挑选顺序: 状态健康 → 今日没用满 → 总用得最少 → 最久没用过。
        没有可用账号时抛 NoAccountAvailable, 调用方应给出友好提示而不是崩掉。

        加锁保证挑选与用量计数原子化；当前未实现账号独占租约。
        """
        with self._lock:
            self.refresh()
            with self._conn() as c:
                r = c.execute(
                    """
                    SELECT * FROM accounts
                     WHERE site = ?
                       AND state = ?
                       AND login_status <> 'expired'
                       AND used_today < daily_limit
                       AND NOT EXISTS(SELECT 1 FROM account_leases l WHERE l.account_id=accounts.id AND l.lease_until>strftime('%s','now'))
                     ORDER BY used_today ASC, last_used_at ASC, id ASC
                     LIMIT 1
                    """,
                    (site, HEALTHY),
                ).fetchone()

                if not r:
                    # 给出"为什么没有"的具体原因, 而不是一句干巴巴的失败
                    all_acc = self.list(site)
                    if not all_acc:
                        raise NoAccountAvailable(f"『{site}』账号池是空的, 请先录入账号")
                    reasons = []
                    for a in all_acc:
                        if a.state == COOLING:
                            reasons.append(f"{a.label} 冷却中(还剩 {a.cooldown_remain//60} 分钟)")
                        elif a.state == BANNED:
                            reasons.append(f"{a.label} 已被标记封禁")
                        elif a.state == DISABLED:
                            reasons.append(f"{a.label} 已停用")
                        elif a.used_today >= a.daily_limit:
                            reasons.append(f"{a.label} 今日额度已用完({a.used_today}/{a.daily_limit})")
                        elif a.login_status == "expired":
                            reasons.append(f"{a.label} 登录态过期待检测")
                        elif self.account_details(a)["busy"]:
                            reasons.append(f"{a.label} 正在使用")
                    raise NoAccountAvailable(f"『{site}』没有可用账号: " + "; ".join(reasons))

                acc = self._row_to_account(r)
                cred = self.get_credential(acc.id)
                # 领取时计入当日用量，不代表账号已被独占。
                c.execute(
                    "UPDATE accounts SET used_today = used_today + 1, last_used_at = ? WHERE id = ?",
                    (int(time.time()), acc.id),
                )
        return acc, cred

    def take_or_none(self, site: str) -> tuple[Account | None, Any]:
        """不抛异常的版本, 适合"有没有都行"的场景。"""
        try:
            return self.take(site)
        except NoAccountAvailable:
            return None, None

    # ---------------- 回报结果 ----------------
    def report(
        self,
        account_id: int,
        ok: bool,
        *,
        task_id: str | None = None,
        reason: str = "",
        ban: bool = False,
    ) -> Account | None:
        """
        用完回报。这是轮换机制的心脏。

        ok=True   → 成功计数 +1, 连续失败清零
        ok=False  → 失败计数 +1; 连续失败达阈值就进冷却, 时长随次数指数增长
        ban=True  → 直接标记封禁, 不再自动恢复

        注意: `used_today` 在 take() 里就已经 +1 了 (领号即占位)。
        这里不再自增, 否则一次使用会被记两次。
        """
        now = int(time.time())
        with self._conn() as c:
            self._reset_daily_if_needed(c)
            r = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not r:
                raise AccountError(f"账号 {account_id} 不存在")

            if ok:
                c.execute(
                    "UPDATE accounts SET success_count = success_count + 1, consecutive_fail = 0, "
                    "backoff_level = 0, last_used_at = ?, "
                    "state = CASE WHEN state = ? THEN ? ELSE state END, cooldown_until = 0 WHERE id = ?",
                    (now, COOLING, HEALTHY, account_id),
                )
            else:
                consec = (r["consecutive_fail"] or 0) + 1
                if ban:
                    c.execute(
                        "UPDATE accounts SET fail_count = fail_count + 1, consecutive_fail = ?, "
                        "last_used_at = ?, state = ?, note = ? WHERE id = ?",
                        (consec, now, BANNED, reason or "已标记封禁", account_id),
                    )
                elif consec >= FAIL_THRESHOLD:
                    level = max(0, consec - FAIL_THRESHOLD)
                    cool = min(COOLDOWN_BASE * (2 ** level), COOLDOWN_MAX)
                    c.execute(
                        "UPDATE accounts SET fail_count = fail_count + 1, consecutive_fail = ?, "
                        "backoff_level = ?, last_used_at = ?, "
                        "state = ?, cooldown_until = ? WHERE id = ?",
                        (consec, level, now, COOLING, now + cool, account_id),
                    )
                else:
                    c.execute(
                        "UPDATE accounts SET fail_count = fail_count + 1, consecutive_fail = ?, "
                        "last_used_at = ? WHERE id = ?",
                        (consec, now, account_id),
                    )

            c.execute(
                "INSERT INTO account_usage(account_id, task_id, used_at, ok, reason) VALUES (?,?,?,?,?)",
                (account_id, task_id, now, 1 if ok else 0, reason),
            )

        return self.get(account_id)

    # ---------------- 观测 ----------------
    def summary(self, site: str | None = None) -> dict:
        """给页面用的汇总。"""
        accs = self.list(site)
        by_state = {s: 0 for s in ALL_STATES}
        for a in accs:
            by_state[a.state] = by_state.get(a.state, 0) + 1
        return {
            "total": len(accs),
            "sites": self.sites(),
            "by_state": by_state,
            "available": sum(1 for a in accs if a.available and a.used_today < a.daily_limit
                             and not self.account_details(a)["busy"]),
            "used_today": sum(a.used_today for a in accs),
            "quota_total": sum(a.daily_limit for a in accs),
        }

    def history(self, account_id: int, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT used_at, ok, reason, task_id FROM account_usage "
                "WHERE account_id=? ORDER BY used_at DESC LIMIT ?",
                (account_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


if __name__ == "__main__":
    import tempfile
    from .vault import Vault as V

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        v = V("pooltest", key_file=td / "k.key")
        v.salt_file = td / "k.salt"

        pool = AccountPool(td / "test.db", vault=v)

        # --- 登记 ---
        a1 = pool.add("taobao", "主号", {"cookies": [{"name": "s", "value": "secret-1"}]}, note="日常")
        a2 = pool.add("taobao", "备用号", {"cookies": [{"name": "s", "value": "secret-2"}]})
        pool.add("bilibili", "B站号", {"cookies": [{"name": "SESSDATA", "value": "s3"}]})
        print(f"[ok] 登记 3 个账号, taobao 有 {len(pool.list('taobao'))} 个")

        # --- 凭据加密 ---
        cred = pool.get_credential(a1.id)
        assert cred["cookies"][0]["value"] == "secret-1", "凭据取回失败"
        raw = (td / "test.db").read_bytes()
        assert b"secret-1" not in raw, "数据库里出现了明文凭据!"
        print("[ok] 凭据已加密, 数据库里搜不到明文")

        # --- 领号轮换: 应该挑用得少的 ---
        acc, c = pool.take("taobao")
        assert acc.label == "主号"
        pool.report(acc.id, True)                       # 主号用一次
        acc2, _ = pool.take("taobao")
        assert acc2.label == "备用号", f"轮换失败, 又领到 {acc2.label}"
        pool.report(acc2.id, True)
        print("[ok] 轮换: 优先挑今日用得少的")

        # --- 失败进冷却 ---
        for i in range(FAIL_THRESHOLD):
            pool.report(a1.id, False, reason=f"第{i+1}次超时")
        a1b = pool.get(a1.id)
        assert a1b.state == COOLING, f"连败 {FAIL_THRESHOLD} 次却没进冷却"
        assert a1b.cooldown_remain > 0
        print(f"[ok] 连败 {FAIL_THRESHOLD} 次 → 冷却 {a1b.cooldown_remain} 秒")

        # 只剩备用号可用
        acc3, _ = pool.take("taobao")
        assert acc3.label == "备用号"
        print("[ok] 冷却中的账号被跳过")

        # --- 全部不可用时给明确原因 ---
        pool.report(a2.id, True)
        pool.set_state(a2.id, DISABLED)
        try:
            pool.take("taobao")
            raise AssertionError("应该抛 NoAccountAvailable")
        except NoAccountAvailable as e:
            print(f"[ok] 无可用账号时给出具体原因: {e}")

        # --- 封禁 ---
        pool.set_state(a1.id, HEALTHY)
        pool.report(a1.id, False, ban=True, reason="出现验证码")
        assert pool.get(a1.id).state == BANNED
        print("[ok] 可标记封禁")

        # --- 汇总 ---
        s = pool.summary()
        print(f"[ok] 汇总: {s['total']} 个账号, 状态分布 {s['by_state']}, 站点 {s['sites']}")

        # --- 对外表示不含凭据 ---
        d = pool.list()[0].to_dict()
        assert "credential" not in d
        print("[ok] to_dict() 不泄漏凭据")

        # --- 使用历史 ---
        hist = pool.history(a1.id)
        assert len(hist) >= 4
        print(f"[ok] 使用历史 {len(hist)} 条")

        # --- 一次使用只算一次额度 (领号占位, 回报不再自增) ---
        p2 = AccountPool(td / "quota.db", vault=v)
        q = p2.add("x", "q1", {"k": 1}, daily_limit=10)
        for _ in range(3):
            a, _c = p2.take("x")
            p2.report(a.id, True)
        assert p2.get(q.id).used_today == 3, f"额度算错: {p2.get(q.id).used_today} != 3"
        print("[ok] 额度计数正确 (领 3 次 → used_today = 3)")

        # --- 并发领号不会撞车 ---
        import threading as _th

        p3 = AccountPool(td / "race.db", vault=v)
        for i in range(5):
            p3.add("y", f"r{i}", {"k": i}, daily_limit=1000)
        got: list[int] = []
        lock = _th.Lock()

        def worker():
            for _ in range(20):
                a, _c = p3.take("y")
                with lock:
                    got.append(a.id)

        ts = [_th.Thread(target=worker) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert len(got) == 120, f"领号次数不对: {len(got)}"
        counts = {i: got.count(i) for i in set(got)}
        spread = max(counts.values()) - min(counts.values())
        assert spread <= 2, f"轮换不均匀: {counts}"
        print(f"[ok] 并发 6 线程领 120 次, 5 个账号负载均衡 (极差 {spread})")

        p3.close()
        p2.close()
        pool.close()

    print("\n[AccountPool] 全部自测通过")
