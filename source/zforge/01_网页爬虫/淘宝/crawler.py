# -*- coding: utf-8 -*-
"""
淘宝商品采集
===========


────────────────────────────────────────────────────────────────
改了什么, 为什么
────────────────────────────────────────────────────────────────
1. 账号从账号池领, 不再读本地 Cookie 文件
   原版: `inject_cookies(d, "taobao_cookies.json")` —— 路径写死, 单账号, 明文
   现在: ctx.take_account() 从池子领 (加密存储, 多账号轮换, 失败自动冷却)

2. 新增登录态检测 —— 这是原版最缺的东西
   原版做法: 注入 Cookie 就直接开抓。Cookie 失效时会静默抓到 0 条,
   或者抓回一堆广告, 用户完全不知道发生了什么。
   现在: 先判落地页是不是被重定向回首页 / 登录页, 是就明确报错并
   把账号标记为失败 (触发冷却), 日志里直接告诉你该重新登录了。

3. 去掉 argparse 和 tkinter, 参数改由 META.params 声明

4. 所有 print 改成 ctx.log, 数据走 ctx.emit_item

5. 不指定 chromedriver 路径 (原版写死在 run_search_mode 里, 版本会对不上)

6. 选择器集中定义在文件顶部, 淘宝改版时只改这一处
"""

from __future__ import annotations

import random
import re
import time
from urllib.parse import urlsplit

from server.core.contract import CrawlContext

# ============================================================
# 参数声明
# ============================================================
META = {
    "id": "taobao",
    "name": "淘宝商品采集",
    "site": "s.taobao.com",
    "desc": "按关键词搜索商品，抓取价格、付款人数、链接。必须使用账号池中的登录态，否则会被重定向",
    "accent": "#ff5000",
    "collection": {
        "mode": "selenium", "methods": ["selenium"],
        "stages": [{"label": "商品搜索与翻页", "method": "selenium"}],
    },
    "authentication": {"requirement": "required", "credential_types": ["cookies"]},
    "needs_account": True,
    "account_site": "taobao",
    "exclusive": True,
    "est_seconds": 90,
    "params": [
        {
            "key": "keyword",
            "label": "搜索关键词",
            "type": "text",
            "required": True,
            "placeholder": "例如：显示器、手办",
            "help": "多个关键词用逗号分隔，会依次采集并分别入库",
            "max_length": 40,
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
        {
            "key": "min_pay",
            "label": "最低付款人数",
            "type": "number",
            "required": False,
            "default": 10,
            "min": 0,
            "help": "低于这个数的商品不入库，用来滤掉零销量的铺货",
        },
        {
            "key": "price_min",
            "label": "价格下限",
            "type": "number",
            "required": False,
            "default": 0,
            "min": 0,
        },
        {
            "key": "price_max",
            "label": "价格上限",
            "type": "number",
            "required": False,
            "default": 0,
            "min": 0,
            "help": "填 0 表示不限",
        },
    ],
}

# ============================================================
# 选择器 (淘宝改版时只改这里)
# ============================================================
SEL_CARD = '[id^="item_id_"]'                    # 原版选择器, 淘宝改版后可能失效
SEL_CARD_FALLBACKS = [
    '[class*="Card--"]',
    'a[href*="item.taobao.com"]',
    'a[href*="detail.tmall.com"]',
]
SEL_TITLE = '[class*="title--"], [class*="Title--"]'
SEL_PRICE = '[class*="priceInt"], [class*="Price--"]'
SEL_PAY = '[class*="realSales"], [class*="sale"], [class*="Sales"]'

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
]

LOGIN_HOSTS = ("login.taobao.com", "login.tmall.com", "passport.taobao.com", "passport.tmall.com")


# ============================================================
# 数值解析 (沿用原版逻辑, 补充健壮性)
# ============================================================
def normalize_price(raw) -> float:
    """'¥1,299.00' -> 1299.0 ; '1299-1599' -> 1299.0 ; '面议' -> 0.0"""
    if raw is None:
        return 0.0
    s = str(raw).strip().replace("¥", "").replace("￥", "").replace(",", "").replace(" ", "")
    if "-" in s:
        s = s.split("-")[0]
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return 0.0
    try:
        return float(m.group(1))
    except ValueError:
        return 0.0


def normalize_pay_count(raw) -> int:
    """'1.2万+人付款' -> 12000 ; '500人收货' -> 500 ; '100+' -> 100"""
    if raw is None:
        return 0
    s = str(raw).strip().replace(",", "")
    if not s:
        return 0
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return 0
    try:
        num = float(m.group(1))
    except ValueError:
        return 0
    if "万" in s[m.end():m.end() + 2] or "万" in s:
        num *= 10000
    return int(num)


# ============================================================
# 浏览器
# ============================================================
def build_driver(ctx: CrawlContext, user_agent: str):
    from selenium import webdriver
    from selenium.webdriver.edge.options import Options

    opts = Options()
    for a in (
        "--no-sandbox",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        "--window-size=1500,950",
        "--accept-language=zh-CN,zh;q=0.9",
        "--accept=text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    ):
        opts.add_argument(a)
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(f"--user-agent={user_agent}")
    opts.add_experimental_option("prefs", {
        "profile.default_content_setting_values.notifications": 2,
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        # 关掉"Chrome 正受到自动测试软件控制"横幅相关
        "excludeSwitches": ["enable-automation"],
    })

    t0 = time.time()
    from server.core.browser_runtime import create_edge
    driver = create_edge(opts)
    ctx.log("INFO", f"浏览器已启动 ({time.time()-t0:.1f}s)")

    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": """
        Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
        Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
        Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh','en']});
        Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>8});
        window.chrome={runtime:{},loadTimes:function(){},csi:function(){},app:{}};
    """})
    driver.execute_cdp_cmd("Network.setUserAgentOverride", {
        "userAgent": user_agent,
        "acceptLanguage": "zh-CN,zh;q=0.9,en;q=0.8",
        "platform": "Win32",
    })
    return driver


def inject_cookies(driver, ctx: CrawlContext, cookies: list[dict]) -> int:
    """把账号池领到的 Cookie 注入浏览器。返回成功条数。

    只注入域覆盖 .taobao.com 的 Cookie。判定是确定的：
    Selenium 要求 cookie 域与当前页面域匹配，否则必然抛异常。
    账号池在 login.taobao.com 上捕获的 Cookie 域为 login.taobao.com，
    在 www.taobao.com 上注入必定失败 —— 这类条目在注入前按域名剔除，
    而不是靠 try/except 把异常吞掉。
    """
    driver.get("https://www.taobao.com")
    time.sleep(random.uniform(1.0, 1.8))
    ok = 0
    for c in cookies:
        name, value = c.get("name"), c.get("value")
        if not name or value is None:
            continue
        domain = c.get("domain") or ".taobao.com"
        # 域必须能覆盖当前页面，否则注入必失败
        if not (domain.lstrip(".").endswith("taobao.com") and "login." not in domain):
            continue
        ck = {"name": name, "value": str(value), "domain": domain}
        if c.get("path"):
            ck["path"] = c["path"]
        if c.get("secure") is not None:
            ck["secure"] = bool(c["secure"])
        if c.get("httpOnly") is not None:
            ck["httpOnly"] = bool(c["httpOnly"])
        if c.get("expiry"):
            ck["expiry"] = int(c["expiry"])
        driver.add_cookie(ck)
        ok += 1
    return ok


def landed_on_login_page(driver) -> bool:
    """是否被重定向到了登录页。

    唯一判据：URL 主机落在登录域。这是确定事实，不需要猜测页面文案——
    J_SiteNavLogin 这类导航容器 ID 登录前后都在，用它判断必然误判。
    """
    host = (urlsplit(driver.current_url or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in LOGIN_HOSTS)


def captcha_blocking(driver) -> bool:
    """滑块验证是否正挡在页面上。

    唯一判据：验证容器存在于 DOM。淘宝弹滑块时会插入 .nc-container /
    #nc_1_wrapper 这类固定节点，这是确定的 DOM 事实。
    """
    return bool(driver.execute_script(
        "return !!document.querySelector('.nc-container, #nc_1_wrapper, .baxia-dialog');"))


def human_pause(ctx: CrawlContext, lo: float = 0.8, hi: float = 1.6) -> None:
    ctx.sleep(random.uniform(lo, hi))


def human_scroll(driver, ctx: CrawlContext) -> None:
    """原版的拟人滚动: 三档高度 + 随机延迟, 最后滚到底。"""
    for y in (800, 1600, 2400):
        ctx.check_cancel()
        driver.execute_script(f"window.scrollTo(0,{y})")
        ctx.sleep(random.uniform(0.8, 1.5))
    driver.execute_script("window.scrollTo(0,document.body.scrollHeight)")
    ctx.sleep(random.uniform(1.5, 3.0))


# ============================================================
# 解析
# ============================================================
def parse_cards(driver, ctx: CrawlContext) -> list[dict]:
    """
    解析当前页的商品卡片。
    先试原版选择器, 不中再逐个试兜底选择器 —— 淘宝改版时会走得下去。
    """
    from selenium.webdriver.common.by import By

    cards = driver.find_elements(By.CSS_SELECTOR, SEL_CARD)
    used = SEL_CARD
    if not cards:
        for sel in SEL_CARD_FALLBACKS:
            cards = driver.find_elements(By.CSS_SELECTOR, sel)
            if cards:
                used = sel
                ctx.warn(f"原选择器 {SEL_CARD} 未命中，改用兜底 {sel}（淘宝可能已改版）")
                break

    if not cards:
        ctx.err("一个商品卡片都没找到 —— 淘宝改版了，或者页面没真正加载出列表")
        return []

    ctx.log("INFO", f"命中 {len(cards)} 张卡片 (选择器 {used})")
    goods: list[dict] = []

    for c in cards:
        ctx.check_cancel()
        try:
            cid = c.get_attribute("id") or ""
            iid = cid.replace("item_id_", "") if cid.startswith("item_id_") else ""

            # 标题
            title = ""
            for el in c.find_elements(By.CSS_SELECTOR, SEL_TITLE):
                t = el.get_attribute("title") or el.text.strip()
                if t and len(t) > 5:
                    title = t
                    break
            if not title:
                for a in c.find_elements(By.CSS_SELECTOR, 'a[href*="item.taobao.com"], a[href*="tmall"]'):
                    t = a.get_attribute("title") or a.text.strip()
                    if t and len(t) > 5:
                        title = t
                        break

            # 价格
            price = ""
            for el in c.find_elements(By.CSS_SELECTOR, SEL_PRICE):
                price = el.text.strip()
                if price:
                    break

            # 付款人数
            pay = ""
            for el in c.find_elements(By.CSS_SELECTOR, SEL_PAY):
                t = el.text.strip()
                if "付款" in t or "收货" in t or "人买" in t:
                    pay = t
                    break
            if not pay:
                m = re.search(r"(\d+[+万]?\d*)\s*(?:人付款|人收货|人已买)", c.text)
                if m:
                    pay = m.group(1)

            # 链接
            url = ""
            if iid:
                url = f"https://item.taobao.com/item.htm?id={iid}"
            else:
                for a in c.find_elements(By.CSS_SELECTOR, 'a[href*="id="]'):
                    href = a.get_attribute("href") or ""
                    m = re.search(r"id=(\d+)", href)
                    if m:
                        iid = m.group(1)
                        url = f"https://item.taobao.com/item.htm?id={iid}"
                        break

            if title:
                goods.append({
                    "title": title[:200],
                    "price": price,
                    "pay_count": pay,
                    "item_id": iid,
                    "url": url,
                })
        except Exception:
            continue

    return goods


def click_next_page(driver, page_no: int, ctx: CrawlContext) -> bool:
    """
    翻页。原版做法: 记下当前标题集合, 点完后对比, 新增超过一半才算翻页成功。
    保留这个思路, 它很实用 —— 能识别"点了但没翻过去"。
    """
    from selenium.webdriver.common.by import By

    before = set()
    for c in driver.find_elements(By.CSS_SELECTOR, SEL_TITLE):
        t = c.get_attribute("title") or c.text.strip()
        if t:
            before.add(t)

    driver.execute_script("window.scrollTo(0,document.body.scrollHeight)")
    ctx.sleep(random.uniform(0.8, 1.5))

    target = None
    for b in driver.find_elements(By.CSS_SELECTOR, ".next-pagination-item"):
        if b.text.strip() == str(page_no):
            target = b
            break
    if target is None:
        ctx.warn(f"找不到第 {page_no} 页的翻页按钮")
        return False

    driver.execute_script('arguments[0].scrollIntoView({block:"center"})', target)
    ctx.sleep(0.5)
    try:
        target.click()
    except Exception as e:
        ctx.warn(f"点击翻页失败: {e}")
        return False
    ctx.sleep(random.uniform(3, 5))

    after = set()
    for c in driver.find_elements(By.CSS_SELECTOR, SEL_TITLE):
        t = c.get_attribute("title") or c.text.strip()
        if t:
            after.add(t)

    if not after:
        return False
    changed = len(after - before) / len(after)
    if changed < 0.5:
        ctx.warn(f"翻页后内容变化只有 {changed:.0%}，可能没翻成功")
        return False
    return True


# ============================================================
# 入口
# ============================================================
def run(ctx: CrawlContext) -> None:
    keywords = ctx.get_list("keyword")
    if not keywords:
        ctx.err("没有有效的关键词")
        return

    pages = max(1, ctx.get_int("pages", 1))
    min_pay = max(0, ctx.get_int("min_pay", 0))
    price_min = ctx.get_int("price_min", 0)
    price_max = ctx.get_int("price_max", 0)

    # ---------- 领账号 ----------
    ctx.log("INFO", "从账号池领取淘宝账号…")
    account, credential = ctx.take_account()
    cookies = credential.get("cookies") or []
    ctx.log("OK", f"使用账号『{account.label}』({len(cookies)} 条 Cookie)")

    driver = None
    kept = 0
    skipped = {"ad": 0, "low_pay": 0, "price": 0, "dup": 0}
    seen = set()
    total_pages = len(keywords) * pages
    done_pages = 0

    try:
        user_agent = credential.get("user_agent") or random.choice(UA_POOL)
        driver = build_driver(ctx, user_agent)

        # ---------- 注入 Cookie ----------
        n = inject_cookies(driver, ctx, cookies)
        ctx.log("INFO", f"注入 Cookie {n}/{len(cookies)} 条")

        # ---------- 逐关键词逐页 ----------
        captcha_keywords = []      # 被滑块拦下的关键词，采集结束后统一报告
        for keyword in keywords:
            url = f"https://s.taobao.com/search?q={keyword}"
            ctx.log("INFO", f"打开搜索页: {url}")
            driver.get(url)
            ctx.sleep(random.uniform(2.5, 4.0))

            # 判据一：URL 落到登录域 —— 确定的登录失效
            if landed_on_login_page(driver):
                ctx.err("被重定向到登录页，账号登录态已失效")
                ctx.err("可在账号池页面重新登录该账号")
                ctx.report_account(False, reason="被重定向到登录页", login_status="expired")
                return

            # 判据二：滑块容器出现在 DOM —— 确定的风控拦截
            if captcha_blocking(driver):
                ctx.err(f"『{keyword}』触发滑块验证，跳过该关键词")
                ctx.err("滑块需人工拖动，程序不尝试绕过；账号登录态保持不变")
                captcha_keywords.append(keyword)
                continue

            for page in range(1, pages + 1):
                ctx.check_cancel()
                if page > 1:
                    if not click_next_page(driver, page, ctx):
                        ctx.warn(f"第 {page} 页翻页失败，跳出这个关键词")
                        break

                human_scroll(driver, ctx)
                raw = parse_cards(driver, ctx)
                ctx.log("INFO", f"『{keyword}』第 {page} 页 → {len(raw)} 个商品")

                # 判据三：首页一张卡都解析不出 —— 不确定原因，但确定没数据
                if not raw and page == 1:
                    ctx.err(f"『{keyword}』一张商品卡都没解析出来，跳过该关键词")
                    ctx.err("常见原因：登录态失效、滑块拦截或淘宝改版")
                    captcha_keywords.append(keyword)
                    break

                for g in raw:
                    ctx.check_cancel()
                    title = g["title"]
                    if title in seen:
                        skipped["dup"] += 1
                        continue
                    seen.add(title)

                    price = normalize_price(g["price"])
                    pay = normalize_pay_count(g["pay_count"])

                    if price <= 0:
                        skipped["ad"] += 1        # 价格为 0 基本是广告位
                        continue
                    if pay < min_pay:
                        skipped["low_pay"] += 1
                        continue
                    if price_min and price < price_min:
                        skipped["price"] += 1
                        continue
                    if price_max and price > price_max:
                        skipped["price"] += 1
                        continue

                    ctx.emit_item({
                        "title": title,
                        "price": price,
                        "pay_count": pay,
                        "price_text": g["price"],
                        "pay_text": g["pay_count"],
                        "item_id": g["item_id"],
                        "url": g["url"],
                        "keyword": keyword,
                        # 综合分: 销量占大头, 价格越低加分越多
                        "hot_score": round(pay / (price ** 0.5), 2) if price > 0 else 0.0,
                    })
                    kept += 1

                done_pages += 1
                ctx.progress_step(done_pages, total_pages,
                                  f"『{keyword}』第 {page}/{pages} 页 · 已入库 {kept} 条")
                if page < pages:
                    ctx.sleep(random.uniform(2.0, 4.0))

    except Exception as e:
        ctx.err(f"采集过程中出错: {type(e).__name__}: {e}")
        raise
    else:
        # 账号回报只看一个确定事实：本次是否真的采到了数据。
        # 采到 -> 登录态确认为有效；一条没采到 -> 只记日志，不写 login_status。
        # 滑块/改版/网络问题一律不改变账号登录状态，避免一次偶发失败把账号踢出队列。
        if kept > 0:
            ctx.report_account(True, login_status="valid")
        else:
            ctx.log("WARN", "未采到符合条件的商品；不据此判断账号失效")
            if captcha_keywords:
                ctx.warn(f"以下关键词未采集：{', '.join(captcha_keywords)}（滑块或解析失败，非账号问题）")
    finally:
        if driver is not None:
            try:
                driver.quit()
                ctx.log("INFO", "浏览器已关闭")
            except Exception:
                pass

    ctx.log("INFO", f"过滤明细: 广告/无价 {skipped['ad']} · 销量不足 {skipped['low_pay']} · "
                    f"价格不符 {skipped['price']} · 重复 {skipped['dup']}")
    if kept == 0:
        if captcha_keywords:
            ctx.err(f"一条都没采到：{len(captcha_keywords)} 个关键词被滑块或解析失败拦下")
        else:
            ctx.err("一条都没采到")
    else:
        ctx.ok(f"采集完成，共入库 {kept} 个商品")
        if captcha_keywords:
            ctx.warn(f"另有 {len(captcha_keywords)} 个关键词未采集：{', '.join(captcha_keywords)}")
