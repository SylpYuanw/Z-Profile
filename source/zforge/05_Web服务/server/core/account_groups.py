# -*- coding: utf-8 -*-
"""命名账号池、成员、Cookie 导入和跨池共享的账号占用。"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid


class AccountError(Exception):
    """可向使用者展示的账号配置错误。"""


class NoAccountAvailable(AccountError):
    """当前池没有符合条件且未占用的账号。"""


class AccountGroups:
    def ensure_groups_schema(self):
        with self._conn() as c:
            first_migration = not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_pools'").fetchone()
            columns = {row[1] for row in c.execute("PRAGMA table_info(accounts)")}
            for name, declaration in {
                "login_status": "TEXT NOT NULL DEFAULT 'unknown'",
                "login_checked_at": "INTEGER NOT NULL DEFAULT 0",
                "identity_hash": "TEXT NOT NULL DEFAULT ''",
                "phone": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in columns:
                    c.execute(f"ALTER TABLE accounts ADD COLUMN {name} {declaration}")
            # 仅首次迁移已有淘宝账号时记录已确认的过期问题，不污染未来新增账号。
            if "login_status" not in columns:
                c.execute("UPDATE accounts SET login_status='expired' WHERE site='taobao'")
            if "phone" not in columns:
                for row in c.execute("SELECT id,site,label FROM accounts WHERE label IN ('主号','备用号')").fetchall():
                    label = {"taobao": "淘宝账号", "bilibili": "B站账号"}.get(row["site"], "账号") + f" #{row['id']}"
                    if not c.execute("SELECT 1 FROM accounts WHERE site=? AND label=?", (row["site"], label)).fetchone():
                        c.execute("UPDATE accounts SET label=? WHERE id=?", (label, row["id"]))
            c.executescript("""
                CREATE TABLE IF NOT EXISTS account_pools (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, site TEXT NOT NULL, name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, is_default INTEGER NOT NULL DEFAULT 0,
                    strategy TEXT NOT NULL DEFAULT 'least_used', created_at INTEGER NOT NULL,
                    UNIQUE(site,name));
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pool_default ON account_pools(site) WHERE is_default=1;
                CREATE TABLE IF NOT EXISTS account_pool_members (
                    pool_id INTEGER NOT NULL REFERENCES account_pools(id) ON DELETE CASCADE,
                    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    PRIMARY KEY(pool_id,account_id));
                CREATE TABLE IF NOT EXISTS account_leases (
                    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
                    owner TEXT NOT NULL, token TEXT NOT NULL, lease_until INTEGER NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_account_identity ON accounts(site,identity_hash)
                    WHERE identity_hash<>'';
            """)
            sites = {row[0] for row in c.execute("SELECT DISTINCT site FROM accounts")}
            for site in sorted(sites | {"taobao", "bilibili"}):
                self._default_pool(c, site)
            if first_migration:
                c.execute("""INSERT OR IGNORE INTO account_pool_members(pool_id,account_id)
                         SELECT p.id,a.id FROM accounts a JOIN account_pools p ON p.site=a.site AND p.is_default=1
                         WHERE NOT EXISTS(SELECT 1 FROM account_pool_members m WHERE m.account_id=a.id)""")

    def _default_pool(self, c, site):
        row = c.execute("SELECT id FROM account_pools WHERE site=? AND is_default=1", (site,)).fetchone()
        if row:
            return row[0]
        name = {"taobao": "淘宝默认池", "bilibili": "B站默认池"}.get(site, site + "默认池")
        c.execute("INSERT INTO account_pools(site,name,is_default,created_at) VALUES (?,?,1,?)",
                  (site, name, int(time.time())))
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def pools(self, site=None):
        self.refresh()
        with self._conn() as c:
            rows = c.execute("SELECT * FROM account_pools" + (" WHERE site=?" if site else "") +
                             " ORDER BY site,is_default DESC,id", (site,) if site else ()).fetchall()
            result = []
            for row in rows:
                record = dict(row)
                accounts = c.execute("""SELECT a.*, EXISTS(SELECT 1 FROM account_leases l
                    WHERE l.account_id=a.id AND l.lease_until>?) AS busy FROM accounts a
                    JOIN account_pool_members m ON m.account_id=a.id WHERE m.pool_id=?""",
                                     (int(time.time()), row["id"])).fetchall()
                record["members"] = len(accounts)
                record["available"] = sum(1 for a in accounts if record["enabled"] and a["credential_kind"] == "cookie" and a["state"] == "healthy" and
                    a["login_status"] != "expired" and not a["busy"] and a["used_today"] < a["daily_limit"])
                record["enabled"] = bool(record["enabled"])
                record["is_default"] = bool(record["is_default"])
                result.append(record)
            return result

    def pool_info(self, pool_id):
        return next((p for p in self.pools() if p["id"] == pool_id), None)

    def require_pool(self, pool_id, site=None):
        record = self.pool_info(pool_id)
        if record is None:
            raise AccountError("账号池不存在")
        if site and record["site"] != site:
            raise AccountError("账号池站点与采集器不兼容")
        if not record["enabled"]:
            raise AccountError("账号池已停用")
        return record

    def create_pool(self, site, name, strategy="least_used"):
        if not isinstance(site, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", site):
            raise AccountError("站点标识无效")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise AccountError("池名称需要 1～60 个字符")
        if strategy not in ("least_used", "round_robin"):
            raise AccountError("选号策略无效")
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM account_pools WHERE site=? AND name=?", (site, name.strip())).fetchone():
                raise AccountError("同站点已存在同名账号池")
            default = not c.execute("SELECT 1 FROM account_pools WHERE site=?", (site,)).fetchone()
            cur = c.execute("INSERT INTO account_pools(site,name,strategy,is_default,created_at) VALUES (?,?,?,?,?)",
                            (site, name.strip(), strategy, int(default), int(time.time())))
            pool_id = cur.lastrowid
        return self.pool_info(pool_id)

    def update_pool(self, pool_id, changes):
        record = self.pool_info(pool_id)
        if record is None:
            raise AccountError("账号池不存在")
        name = changes.get("name", record["name"])
        strategy = changes.get("strategy", record["strategy"])
        enabled = changes.get("enabled", record["enabled"])
        default = changes.get("is_default", record["is_default"])
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise AccountError("池名称需要 1～60 个字符")
        if strategy not in ("least_used", "round_robin") or type(enabled) is not bool or type(default) is not bool:
            raise AccountError("策略或启停参数无效")
        if default and not enabled:
            raise AccountError("默认池不能停用；请先设置另一个默认池")
        if record["is_default"] and not default:
            raise AccountError("请通过设置另一个默认池来更换默认池")
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM account_pools WHERE site=? AND name=? AND id<>?",
                         (record["site"], name.strip(), pool_id)).fetchone():
                raise AccountError("同站点已存在同名账号池")
            if default:
                c.execute("UPDATE account_pools SET is_default=0 WHERE site=?", (record["site"],))
            c.execute("UPDATE account_pools SET name=?,strategy=?,enabled=?,is_default=? WHERE id=?",
                      (name.strip(), strategy, int(enabled), int(default), pool_id))
        return self.pool_info(pool_id)

    def add_member(self, pool_id, account_id):
        record = self.pool_info(pool_id)
        account = self.get(account_id)
        if record is None or account is None:
            raise AccountError("账号池或账号不存在")
        if record["site"] != account.site:
            raise AccountError("不能将其他站点的账号加入该池")
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO account_pool_members VALUES (?,?)", (pool_id, account_id))

    def remove_member(self, pool_id, account_id):
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self.require_idle(c, account_id)
            c.execute("DELETE FROM account_pool_members WHERE pool_id=? AND account_id=?", (pool_id, account_id))

    def require_idle(self, c, account_id):
        if c.execute("SELECT 1 FROM account_leases WHERE account_id=? AND lease_until>?",
                     (account_id, int(time.time()))).fetchone():
            raise AccountError("账号正在使用，请结束任务后再操作")

    def account_details(self, account):
        result = account.to_dict()
        with self._conn() as c:
            result["pool_ids"] = [r[0] for r in c.execute("SELECT pool_id FROM account_pool_members WHERE account_id=?", (account.id,))]
            lease = c.execute("SELECT owner,lease_until FROM account_leases WHERE account_id=? AND lease_until>?",
                              (account.id, int(time.time()))).fetchone()
            result["busy"] = bool(lease)
            result["lease_until"] = lease["lease_until"] if lease else 0
        result["available"] = account.available and account.used_today < account.daily_limit and not result["busy"]
        return result

    def members(self, pool_id):
        with self._conn() as c:
            ids = {r[0] for r in c.execute("SELECT account_id FROM account_pool_members WHERE pool_id=?", (pool_id,))}
        return [self.account_details(a) for a in self.list() if a.id in ids]

    def import_cookies(self, pool_id, label, payload, update_account_id=None, *, phone=None):
        group = self.require_pool(pool_id)
        site = group["site"]
        if site not in ("taobao", "bilibili"):
            raise AccountError("当前仅支持淘宝和 B站 Cookie 文件导入")
        cookies = payload if isinstance(payload, list) else payload.get("cookies") if isinstance(payload, dict) else None
        if not isinstance(cookies, list) or not cookies or len(cookies) > 500:
            raise AccountError("文件应为 Cookie 数组或含 cookies 数组的 JSON 对象")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 60:
            raise AccountError("账号名称需要 1～60 个字符")
        if phone is not None:
            phone = self.validate_phone(phone)
        allowed = ("taobao.com", "tmall.com") if site == "taobao" else ("bilibili.com",)
        clean = []
        for cookie in cookies:
            if not isinstance(cookie, dict) or not isinstance(cookie.get("name"), str) or not isinstance(cookie.get("value"), str):
                raise AccountError("Cookie 项需要字符串 name 和 value")
            domain = cookie.get("domain", "." + allowed[0])
            if not isinstance(domain, str) or not any(domain.lstrip(".") == d or domain.endswith("." + d) for d in allowed):
                raise AccountError("Cookie 域名与所选池的站点不符")
            entry = {k: cookie[k] for k in ("name", "value", "path", "secure", "httpOnly", "sameSite") if k in cookie}
            entry["domain"] = domain
            expiry = cookie.get("expiry", cookie.get("expirationDate"))
            if expiry is not None:
                if type(expiry) not in (int, float) or not math.isfinite(expiry) or not (expiry in (-1, 0) or 0 < expiry < 2**53):
                    raise AccountError("Cookie 有效期无效")
                # 常见导出工具以 -1 或 0 表示会话 Cookie，Selenium 应省略 expiry。
                if expiry > 0:
                    entry["expiry"] = int(expiry)
            clean.append(entry)
        values = {c["name"]: c["value"] for c in clean if c["value"]}
        identity_key = "unb" if site == "taobao" else "DedeUserID"
        auth_keys = ("cookie2",) if site == "taobao" else ("SESSDATA",)
        if not values.get(identity_key) or not any(values.get(key) for key in auth_keys):
            raise AccountError("缺少账号身份或关键登录 Cookie；请导出完整登录态")
        identity = hashlib.sha256((site + ":" + values[identity_key]).encode()).hexdigest()
        now = int(time.time())
        expired = any(c["name"] in auth_keys and 0 < c.get("expiry", 0) <= now for c in clean)
        login_status = "expired" if expired else "unknown"
        blob = self._vault.encrypt({"kind": "cookie", "cookies": clean, "imported_at": now})
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            if update_account_id is not None:
                row = c.execute("SELECT * FROM accounts WHERE id=? AND site=?", (update_account_id, site)).fetchone()
                if not row:
                    raise AccountError("待更新账号不存在或站点不匹配")
                if c.execute("SELECT 1 FROM account_leases WHERE account_id=? AND lease_until>?", (update_account_id, now)).fetchone():
                    raise AccountError("账号正在使用，不能更新凭据")
                old_identity = row["identity_hash"]
                if not old_identity:
                    old = self._vault.decrypt(row["credential"])
                    old_values = self.cookie_values(old)
                    old_identity = hashlib.sha256((site + ":" + old_values.get(identity_key, "")).encode()).hexdigest()
                if old_identity != identity:
                    raise AccountError("导入身份与原账号不符；请选择新增账号")
                account_id = row["id"]
                c.execute("""UPDATE accounts SET credential=?,identity_hash=?,login_status=?,login_checked_at=0,
                    credential_kind='cookie',state=CASE WHEN state='disabled' THEN state ELSE 'healthy' END,
                    consecutive_fail=0,backoff_level=0,cooldown_until=0 WHERE id=?""",
                          (blob, identity, login_status, account_id))
                if phone is not None:
                    c.execute("UPDATE accounts SET phone=? WHERE id=?", (phone, account_id))
                action = "updated"
            else:
                if c.execute("SELECT 1 FROM accounts WHERE site=? AND identity_hash=?", (site, identity)).fetchone():
                    raise AccountError("该平台账号已登记，请对已有账号选择「重新登录」或「文件更新」；只需加入另一池时使用「加入已有账号」")
                if c.execute("SELECT 1 FROM accounts WHERE site=? AND label=?", (site, label.strip())).fetchone():
                    raise AccountError("同站点已有这个显示名称，请填写不同的自定义名称；若为同一账号，请选择「重新登录」")
                # 旧库未保存身份哈希，导入时也核对旧凭据，避免同身份以新名称重复加入。
                for old in c.execute("SELECT credential FROM accounts WHERE site=? AND identity_hash=''", (site,)).fetchall():
                    decoded = self._vault.decrypt(old["credential"])
                    old_values = self.cookie_values(decoded)
                    if old_values.get(identity_key) == values[identity_key]:
                        raise AccountError("该平台账号已登记，请对已有账号选择「重新登录」或「文件更新」")
                cur = c.execute("""INSERT INTO accounts(site,label,credential,credential_kind,created_at,quota_date,
                    identity_hash,login_status) VALUES (?,?,?,'cookie',?,?,?,?)""",
                                (site, label.strip(), blob, now, self._today(), identity, login_status))
                account_id, action = cur.lastrowid, "imported"
                if phone is not None:
                    c.execute("UPDATE accounts SET phone=? WHERE id=?", (phone, account_id))
            c.execute("INSERT OR IGNORE INTO account_pool_members VALUES (?,?)", (pool_id, account_id))
        return {"status": action, "account": self.account_details(self.get(account_id))}

    @staticmethod
    def validate_phone(value):
        if not isinstance(value, str):
            raise AccountError("手机号必须为文本")
        phone = re.sub(r"[\s-]", "", value.strip())
        if phone and not re.fullmatch(r"\+?\d{6,20}", phone):
            raise AccountError("请填写完整手机号，不能包含星号")
        return phone

    def update_profile(self, account_id, changes):
        account = self.get(account_id)
        if account is None:
            raise AccountError("账号不存在")
        phone = self.validate_phone(changes.get("phone", account.phone))
        label = changes.get("label", account.label)
        if not isinstance(label, str):
            raise AccountError("显示名称必须为文本")
        label = label.strip() or phone
        if not 1 <= len(label) <= 60:
            raise AccountError("请填写手机号或 1～60 字的显示名称")
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM accounts WHERE site=? AND label=? AND id<>?", (account.site, label, account_id)).fetchone():
                raise AccountError("同站点已有该显示名称，请使用其他名称")
            c.execute("UPDATE accounts SET phone=?,label=? WHERE id=?", (phone, label, account_id))
        return self.account_details(self.get(account_id))

    @staticmethod
    def cookie_values(payload):
        cookies = payload.get("cookies", []) if isinstance(payload, dict) else []
        return {item["name"]: item["value"] for item in cookies if isinstance(item, dict)
                and isinstance(item.get("name"), str) and isinstance(item.get("value"), str)}

    def mark_login(self, account_id, status):
        if status not in ("valid", "expired", "unknown"):
            raise AccountError("登录检测状态无效")
        with self._conn() as c:
            c.execute("UPDATE accounts SET login_status=?,login_checked_at=? WHERE id=?",
                      (status, int(time.time()), account_id))

    def resolve_pool(self, site, pool_id=None):
        if pool_id is None:
            with self._conn() as c:
                pool_id = self._default_pool(c, site)
        if type(pool_id) is not int or pool_id < 1:
            raise AccountError("account_pool_id 必须为正整数")
        return self.require_pool(pool_id, site)["id"]

    def lease_account(self, site, pool_id, owner, ttl=90):
        pool_id = self.resolve_pool(site, pool_id)
        group = self.require_pool(pool_id, site)
        self.refresh()
        now, token = int(time.time()), uuid.uuid4().hex
        order = "a.used_today,a.last_used_at,a.id" if group["strategy"] == "least_used" else "a.last_used_at,a.used_today,a.id"
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute("SELECT enabled FROM account_pools WHERE id=? AND site=?", (pool_id, site)).fetchone()
            if not current or not current["enabled"]:
                raise AccountError("账号池不存在或已停用")
            c.execute("DELETE FROM account_leases WHERE lease_until<=?", (now,))
            row = c.execute("""SELECT a.* FROM accounts a JOIN account_pool_members m ON m.account_id=a.id
                WHERE m.pool_id=? AND a.site=? AND a.state='healthy' AND a.login_status<>'expired'
                AND a.credential_kind='cookie' AND a.used_today<a.daily_limit
                AND NOT EXISTS(SELECT 1 FROM account_leases l WHERE l.account_id=a.id)
                ORDER BY """ + order + " LIMIT 1", (pool_id, site)).fetchone()
            if not row:
                raise NoAccountAvailable("该池没有可领取账号：请检查成员、登录状态、额度和占用情况")
            credential = self._vault.decrypt(row["credential"])
            c.execute("INSERT INTO account_leases VALUES (?,?,?,?)", (row["id"], owner, token, now + ttl))
            c.execute("UPDATE accounts SET used_today=used_today+1,last_used_at=? WHERE id=?", (now, row["id"]))
            account_id = row["id"]
        return self.get(account_id), credential, token

    def renew_lease(self, account_id, token, ttl=90):
        now = int(time.time())
        with self._conn() as c:
            return c.execute("UPDATE account_leases SET lease_until=? WHERE account_id=? AND token=? AND lease_until>?",
                             (now + ttl, account_id, token, now)).rowcount == 1

    def release_lease(self, account_id, token):
        with self._conn() as c:
            c.execute("DELETE FROM account_leases WHERE account_id=? AND token=?", (account_id, token))
