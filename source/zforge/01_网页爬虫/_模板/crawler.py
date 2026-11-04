# -*- coding: utf-8 -*-
"""
新爬虫模板
==========

复制这个文件到 `01_网页爬虫/<你的站点>/crawler.py`，改完就能用。
**不需要改前端，也不需要改内核** —— 注册表会自动扫描到它，
前端会根据 META.params 自动生成表单。

────────────────────────────────────────────────────────────────
必须做的三件事
────────────────────────────────────────────────────────────────
  1. 填 META（id 必须唯一，且和目录名一致最省事）
  2. 写 run(ctx)
  3. 删掉这个文件顶部这段说明（或者留着，不影响运行）

────────────────────────────────────────────────────────────────
ctx 能干什么
────────────────────────────────────────────────────────────────
  ctx.log(level, msg)       写日志到页面。level: INFO/OK/WARN/ERR/SQL
  ctx.info/ok/warn/err(msg) 简写
  ctx.progress(0~1, 文字)   报进度
  ctx.progress_step(i, n)   按第几步/共几步报进度
  ctx.check_cancel()        检查是否被取消（长循环里必须定期调）
  ctx.sleep(秒)             可中断的等待 —— 代替 time.sleep
  ctx.emit_item({...})      交出一条数据（自动入库）
  ctx.take_account()        领账号，返回 (account, credential)
  ctx.report_account(ok)    回报账号使用结果
  ctx.get/get_int/get_str/get_list(key)   读参数

**不要用 print，不要直接写文件，不要直接连数据库。** 全走 ctx，
否则数据进不了库、日志传不到页面、取消不生效。
"""

from __future__ import annotations

import random
import time

from server.core.contract import CrawlContext

# ============================================================
# ① META —— 声明我是谁、需要什么参数
# ============================================================
META = {
    "id": "mysite",                      # 唯一标识，前端路由用它
    "name": "我的站点采集",               # 显示名
    "site": "www.example.com",           # 目标站点
    "desc": "一句话说清这个爬虫干什么，会显示在卡片上",
    "accent": "#4f8cff",                 # 卡片强调色
    "collection": {
        "mode": "selenium", "methods": ["selenium"],
        "stages": [{"label": "页面采集", "method": "selenium"}],
    },
    "authentication": {"requirement": "none", "credential_types": []},

    # 需不需要从账号池取账号。不需要就填 False 并删掉 account_site
    "needs_account": False,
    # "account_site": "mysite",         # 账号所属站点（需要账号时必填，尚无命名池 ID）

    # 同一个爬虫是否只允许一个实例同时跑
    # True: 避免两个 Selenium 抢同一个账号（有账号池时建议 True）
    # False: 允许并发
    "exclusive": True,

    "est_seconds": 60,                   # 预估耗时，前端用来显示进度节奏

    # 参数声明 —— 前端据此自动生成表单
    "params": [
        {
            "key": "keyword",
            "label": "搜索关键词",
            "type": "text",              # text | number | select | switch
            "required": True,
            "placeholder": "例如：显示器",
            "help": "显示在输入框下方的小字提示",
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
        # select 的写法:
        # {
        #     "key": "sort", "label": "排序", "type": "select",
        #     "default": "sales",
        #     "options": [
        #         {"value": "sales", "label": "按销量"},
        #         {"value": "price", "label": "按价格"},
        #     ],
        # },
        # switch 的写法:
        # {"key": "headless", "label": "无头模式", "type": "switch", "default": False},
    ],
}

# ============================================================
# ② 常量 —— 选择器集中放这里，站点改版时只改这一处
# ============================================================
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")

SEL_CARD = ".item-card"
SEL_TITLE = ".item-card .title"
SEL_PRICE = ".item-card .price"


# ============================================================
# ③ 工具函数
# ============================================================
def build_driver(ctx: CrawlContext, user_agent: str = UA):
    """开一个尽量不像机器人的 Edge。"""
    from selenium import webdriver
    from selenium.webdriver.edge.options import Options

    opts = Options()
    for a in ("--no-sandbox", "--disable-gpu",
              "--disable-blink-features=AutomationControlled",
              "--window-size=1500,950", "--accept-language=zh-CN,zh;q=0.9"):
        opts.add_argument(a)
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(f"--user-agent={user_agent}")
    opts.add_experimental_option("prefs", {
        "profile.default_content_setting_values.notifications": 2,
        "credentials_enable_service": False,
    })

    t0 = time.time()
    driver = webdriver.Edge(options=opts)
    ctx.log("INFO", f"浏览器已启动 ({time.time()-t0:.1f}s)")

    # 抹掉自动化指纹
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": """
        Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
        Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
        Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh','en']});
        window.chrome={runtime:{},loadTimes:function(){},csi:function(){},app:{}};
    """})
    driver.execute_cdp_cmd("Network.setUserAgentOverride", {
        "userAgent": user_agent, "acceptLanguage": "zh-CN,zh;q=0.9", "platform": "Win32"})
    return driver


def human_scroll(driver, ctx: CrawlContext, rounds: int = 3) -> None:
    """拟人滚动，让懒加载内容渲染出来。"""
    for _ in range(rounds):
        ctx.check_cancel()
        driver.execute_script(f"window.scrollBy(0, {random.randint(700, 1100)})")
        ctx.sleep(random.uniform(0.5, 1.0))
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
    ctx.sleep(random.uniform(1.2, 1.8))


def parse_cards(driver, ctx: CrawlContext) -> list[dict]:
    """解析当前页。选择器不中时给出明确警告，不要静默返回空。"""
    from selenium.webdriver.common.by import By

    cards = driver.find_elements(By.CSS_SELECTOR, SEL_CARD)
    if not cards:
        ctx.err(f"选择器 {SEL_CARD} 没命中 —— 站点可能改版了")
        return []

    ctx.log("INFO", f"命中 {len(cards)} 张卡片")
    out = []
    for c in cards:
        ctx.check_cancel()
        try:
            title = ""
            for el in c.find_elements(By.CSS_SELECTOR, SEL_TITLE):
                title = (el.get_attribute("title") or el.text).strip()
                if title:
                    break
            if not title:
                continue
            price = ""
            for el in c.find_elements(By.CSS_SELECTOR, SEL_PRICE):
                price = el.text.strip()
                if price:
                    break
            out.append({"title": title[:200], "price_text": price})
        except Exception:
            continue
    return out


# ============================================================
# ④ 入口
# ============================================================
def run(ctx: CrawlContext) -> None:
    keywords = ctx.get_list("keyword")
    if not keywords:
        ctx.err("没有有效的关键词")
        return
    pages = max(1, ctx.get_int("pages", 1))

    ctx.log("INFO", f"关键词 {keywords} · 每个 {pages} 页")

    # ---- 需要账号时这样领 ----
    # account, credential = ctx.take_account()
    # cookies = credential.get("cookies") or []
    # ctx.log("OK", f"使用账号『{account.label}』")
    # 用完回报结果（成功会清零连败计数，失败会触发冷却）:
    # ctx.report_account(True)
    # ctx.report_account(False, reason="被风控")

    driver = None
    kept = 0
    total_pages = len(keywords) * pages
    done = 0
    seen = set()

    try:
        driver = build_driver(ctx)

        for kw in keywords:
            for page in range(1, pages + 1):
                ctx.check_cancel()
                url = f"https://www.example.com/search?q={kw}&page={page}"
                ctx.log("INFO", f"打开 {url}")
                driver.get(url)
                ctx.sleep(random.uniform(2.0, 3.0))

                human_scroll(driver, ctx)
                rows = parse_cards(driver, ctx)

                for r in rows:
                    ctx.check_cancel()
                    if r["title"] in seen:
                        continue
                    seen.add(r["title"])
                    ctx.emit_item({
                        "title": r["title"],
                        "price": r["price_text"],
                        "keyword": kw,
                        # 站点特有的字段随便加，会进 extra_json
                        "source_page": page,
                    })
                    kept += 1

                done += 1
                ctx.progress_step(done, total_pages, f"『{kw}』第 {page}/{pages} 页 · 已入库 {kept} 条")
                if page < pages:
                    ctx.sleep(random.uniform(1.5, 3.0))

    finally:
        if driver is not None:
            try:
                driver.quit()
                ctx.log("INFO", "浏览器已关闭")
            except Exception:
                pass

    if kept == 0:
        ctx.err("一条都没采到")
    else:
        ctx.ok(f"采集完成，共入库 {kept} 条")
