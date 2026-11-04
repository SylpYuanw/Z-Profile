# -*- coding: utf-8 -*-
"""用户在独立浏览器完成登录，服务端接收 Cookie 并加密录入。"""
from __future__ import annotations

import threading
import time
import uuid
from urllib.parse import urlsplit

from .accounts import AccountError


SITES = {
    "taobao": {"url": "https://login.taobao.com/member/login.jhtml", "domain": "taobao.com", "keys": ("unb", "cookie2")},
    "bilibili": {"url": "https://passport.bilibili.com/login", "domain": "bilibili.com", "keys": ("DedeUserID", "SESSDATA")},
}
ACTIVE = ("opening", "waiting", "saving")


def open_login_browser():
    from selenium.webdriver.edge.options import Options
    from .browser_runtime import create_edge
    options = Options()
    # Selenium 为每次会话建立临时用户目录，不复用用户日常浏览器的登录态。
    options.add_argument("--window-size=1120,850")
    options.add_experimental_option("prefs", {"credentials_enable_service": False, "profile.password_manager_enabled": False})
    return create_edge(options)


def login_cookies(site, url, cookies):
    """只接收所选站点的登录 Cookie；完整登录有效性仍由采集器检查。"""
    spec = SITES[site]
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == spec["domain"] or host.endswith("." + spec["domain"])):
        return None
    allowed = ("taobao.com", "tmall.com") if site == "taobao" else ("bilibili.com",)
    clean = [c for c in cookies if any((c.get("domain", "").lstrip(".") == d or c.get("domain", "").endswith("." + d)) for d in allowed)]
    values = {c.get("name"): c.get("value") for c in clean}
    if not all(values.get(key) for key in spec["keys"]):
        return None
    now = time.time()
    if any(c.get("name") in spec["keys"] and 0 < c.get("expiry", 0) <= now for c in clean):
        return None
    return clean


class BrowserLogins:
    def __init__(self, pool, *, browser_factory=None, timeout=600):
        self.pool = pool
        self.browser_factory = browser_factory or open_login_browser
        self.timeout = timeout
        self._jobs = {}
        self._lock = threading.RLock()
        self._closed = False

    def start(self, pool_id, phone, label="", update_account_id=None):
        group = self.pool.require_pool(pool_id)
        if group["site"] not in SITES:
            raise AccountError("该站点暂未接入网页登录录入")
        phone = self.pool.validate_phone(phone)
        if not phone:
            raise AccountError("请先填写手机号；网站通常不会返回完整手机号")
        if not isinstance(label, str) or len(label.strip()) > 60:
            raise AccountError("自定义名称最多 60 字")
        label = label.strip() or phone
        if update_account_id is not None:
            account = self.pool.get(update_account_id)
            if account is None or account.site != group["site"]:
                raise AccountError("待更新账号不存在或站点不符")
            if self.pool.account_details(account)["busy"]:
                raise AccountError("账号正在使用，请结束采集后重新登录")
            # 已有账号的改名在账号资料中处理，重新登录只更新同身份凭据和管理手机号。
            label = account.label
        with self._lock:
            if self._closed:
                raise AccountError("服务正在停止")
            if any(j["state"] in ACTIVE for j in self._jobs.values()):
                raise AccountError("已有登录窗口正在处理，请先完成或取消后再添加下一个账号")
            job_id = "login-" + uuid.uuid4().hex[:12]
            job = {"id": job_id, "pool_id": pool_id, "site": group["site"], "phone": phone,
                   "label": label, "update_account_id": update_account_id, "state": "opening",
                   "message": "正在打开独立浏览器，请稍候", "created_at": int(time.time()),
                   "expires_at": int(time.time()) + self.timeout, "account": None,
                   "_cancel": threading.Event(), "_thread": None}
            self._jobs[job_id] = job
            thread = threading.Thread(target=self._run, args=(job,), daemon=True, name=job_id)
            job["_thread"] = thread
            thread.start()
            return self._public(job)

    def _public(self, job):
        return {key: value for key, value in job.items() if not key.startswith("_")}

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            return self._public(job) if job else None

    def list(self):
        with self._lock:
            return [self._public(j) for j in list(self._jobs.values())[-20:]][::-1]

    def cancel(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise AccountError("登录任务不存在；服务重启后请重新打开登录页")
            if job["state"] == "saving":
                raise AccountError("正在加密保存，请稍候")
            if job["state"] in ACTIVE:
                job["_cancel"].set()
                job["message"] = "正在关闭登录窗口"
            return self._public(job)

    def _set(self, job, state, message, **extra):
        with self._lock:
            job.update(state=state, message=message, **extra)

    def _run(self, job):
        driver = None
        result = None
        try:
            driver = self.browser_factory()
            if job["_cancel"].is_set():
                return
            if hasattr(driver, "set_page_load_timeout"):
                driver.set_page_load_timeout(25)
                driver.set_script_timeout(10)
            driver.get(SITES[job["site"]]["url"])
            self._set(job, "waiting", "请在弹出的官方页面扫码或手机登录；检测到登录 Cookie 后会自动保存。无需导出 Cookie。")
            stable_identity = None
            while not job["_cancel"].is_set() and time.time() < job["expires_at"]:
                current = login_cookies(job["site"], driver.current_url, driver.get_cookies())
                identity = self.pool.cookie_values({"cookies": current}).get(SITES[job["site"]]["keys"][0]) if current else None
                if identity and identity == stable_identity:
                    # 再次检查取消标记；保存开始后不再接受取消，避免状态与实际写入不一致。
                    with self._lock:
                        if job["_cancel"].is_set():
                            return
                        self._set(job, "saving", "正在加密保存到所选账号池")
                    result = self.pool.import_cookies(job["pool_id"], job["label"], current,
                                                      job["update_account_id"], phone=job["phone"])
                    break
                stable_identity = identity
                job["_cancel"].wait(1)
            if not result and not job["_cancel"].is_set():
                self._set(job, "expired", "等待登录超时，未保存账号；可以重新打开登录页")
        except AccountError as error:
            self._set(job, "failed", str(error))
        except Exception:
            # 浏览器错误可能包含页面或凭据上下文，对接口仅返回操作说明。
            self._set(job, "failed", "登录窗口已关闭或 Edge 启动失败。请确认 Edge 已安装；首次准备驱动需要联网，可重新打开登录页重试。")
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
            if result:
                self._set(job, "saved", "已保存登录态。网站是否仍接受凭据将在采集时校验。", account=result["account"])
            elif job["_cancel"].is_set():
                self._set(job, "canceled", "已取消，未保存登录态")

    def shutdown(self):
        with self._lock:
            self._closed = True
            jobs = list(self._jobs.values())
            for job in jobs:
                if job["state"] in ("opening", "waiting"):
                    job["_cancel"].set()
        for job in jobs:
            if job["_thread"] and job["_thread"].is_alive():
                job["_thread"].join(timeout=40)
