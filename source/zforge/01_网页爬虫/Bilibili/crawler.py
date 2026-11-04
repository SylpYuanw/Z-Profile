# -*- coding: utf-8 -*-
"""
B站视频采集
===========


────────────────────────────────────────────────────────────────
三条数据路线的实测对比 (2026-10 本机)
────────────────────────────────────────────────────────────────
                    每视频耗时   拿到的字段                          抗改版
  原版做法            ~10s     全 (每次开一个新浏览器)                弱
  浏览器+XPath         3.1s    全, 但数字是 "2069.3万" 文本           弱
  浏览器+页面JSON      3.1s    全, 且是整数 20693775                 中
  ★ 公开 API          0.58s    全, 且是整数                        强

结论: 走 API。
  · 快 5 倍 (详情 35 个视频: 原来 350 秒 → 现在 20 秒)
  · 拿到的就是干净数字, 不用解析 "2069.3万" 这种文本
  · 页面改版不影响 (原版 XPath 依赖 //*[@id="viewbox_report"] 这类脆弱路径)
  · 10 个 BV 连续请求无限流, 加 0.35s 间隔足够礼貌

浏览器仍然要用 —— 但只用来开一次搜索列表页拿 BV 号和基本信息。
这样整个采集 = 1 次浏览器 + N 次轻量 API, 而不是 N+1 次浏览器。

────────────────────────────────────────────────────────────────
原版哪些东西保留了
────────────────────────────────────────────────────────────────
  · 列表页 XPath `//*[@class="video-list row"]/div` (实测仍有效, 作为兜底)
  · 拟人滚动 (三档高度 + 随机延迟)
  · 点赞率 / 三连率 的计算口径
  · 广告卡片过滤 (原版是硬编码下标数组, 这里改成按特征判断)
"""

from __future__ import annotations

import json
import random
import re
import time

from server.core.contract import CrawlContext

# ============================================================
# 参数声明
# ============================================================
META = {
    "id": "bilibili",
    "name": "B站视频采集",
    "site": "search.bilibili.com",
    "desc": "搜关键词拿视频列表，再逐个补全播放/点赞/投币/收藏/评论，计算点赞率与三连率",
    "accent": "#00a1d6",
    "collection": {
        "mode": "hybrid", "methods": ["selenium", "http"],
        "stages": [{"label": "搜索列表", "method": "selenium"},
                   {"label": "视频详情 API", "method": "http"}],
    },
    "authentication": {"requirement": "none", "credential_types": []},
    # B站列表和详情页都不需要登录态, 所以不进账号池。
    # 留这个入口是为了以后抓"需要登录"的数据 (个人收藏夹等) 时能直接打开。
    "needs_account": False,
    "exclusive": True,
    "est_seconds": 60,
    "params": [
        {
            "key": "keyword",
            "label": "搜索关键词",
            "type": "text",
            "required": True,
            "placeholder": "例如：python 教程",
            "help": "多个关键词用逗号分隔，会依次采集",
            "max_length": 60,
        },
        {
            "key": "pages",
            "label": "翻页数",
            "type": "number",
            "required": False,
            "default": 1,
            "min": 1,
            "max": 20,
            "help": "每页约 30~40 个视频",
        },
        {
            "key": "detail_limit",
            "label": "补详情数量上限",
            "type": "number",
            "required": False,
            "default": 30,
            "min": 0,
            "max": 200,
            "help": "对播放量最高的前 N 个补全点赞/投币/收藏。填 0 表示只抓列表页基础数据",
        },
        {
            "key": "min_views",
            "label": "最低播放量过滤",
            "type": "number",
            "required": False,
            "default": 0,
            "min": 0,
            "help": "低于这个播放量的不入库，用来滤掉广告和无关内容",
        },
    ],
}

# ============================================================
# 常量
# ============================================================
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")

SEARCH_URL = "https://search.bilibili.com/all?keyword={kw}&page={page}"
# 实测坑: 带 &page=1 的地址, 页面会把 page 参数吃掉并**不渲染视频列表** (卡片数 0)。
# 第 1 页必须用不带 page 参数的干净地址。第 2 页起才带。
SEARCH_URL_P1 = "https://search.bilibili.com/all?keyword={kw}"
VIEW_API = "https://api.bilibili.com/x/web-interface/view"

# 详情 API 的请求间隔 —— 礼貌值, 实测 0.35s 无限流
API_GAP = (0.30, 0.55)

# 判断"列表渲染出来了"用这一组选择器, 任意一个命中即算成功。
# 不要只依赖原版那个 XPath —— 它太脆弱, 一旦不中就会白等 12 秒。
CARD_SELECTORS = [
    ".video-list.row > div",
    ".bili-video-card",
    '[class*="bili-video-card"]',
    'a[href*="/video/"]',
]

SEL_CARD = ".video-list.row > div"          # 原版 XPath 的 CSS 等价形式
SEL_TITLE = ".bili-video-card__info--tit"

JS_EXTRACT = r"""
const out = [];
for (const card of document.querySelectorAll('.video-list.row > div')) {
    const link = card.querySelector('a[href*="/video/"]');
    if (!link) continue;
    const href = link.getAttribute('href') || '';
    const m = href.match(/(BV[0-9A-Za-z]{10})/);
    if (!m) continue;
    const stats = [...card.querySelectorAll('.bili-video-card__stats--item')]
                    .map(e => (e.innerText || '').trim()).filter(Boolean);
    const dur    = card.querySelector('.bili-video-card__stats__duration');
    const title  = card.querySelector('.bili-video-card__info--tit');
    const author = card.querySelector('.bili-video-card__info--author');
    const date   = card.querySelector('.bili-video-card__info--date');
    out.push({
        bv: m[1],
        url: 'https://www.bilibili.com/video/' + m[1] + '/',
        title: (title ? (title.getAttribute('title') || title.innerText) : '').trim(),
        views_raw: stats[0] || '',
        danmaku_raw: stats[1] || '',
        duration_text: dur ? dur.innerText.trim() : '',
        author: author ? author.innerText.trim() : '',
        date_text: date ? date.innerText.replace(/^[·\s]+/, '').trim() : '',
        card_text: (card.innerText || '').replace(/\n+/g, ' | ').slice(0, 400)
    });
}
return out;
"""

_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")


# ============================================================
# 解析工具
# ============================================================
def parse_count(raw) -> int:
    """'2069.3万' / '13.3万' / '6691' / '1.2亿' -> int"""
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip().replace(",", "").replace(" ", "")
    if s in ("--", "-", "", "点击", "播放"):
        return 0
    m = _NUM_RE.search(s)
    if not m:
        return 0
    try:
        num = float(m.group(1))
    except ValueError:
        return 0
    tail = s[m.end():]
    if tail.startswith("亿") or "亿" in s:
        num *= 100_000_000
    elif tail.startswith("万") or "万" in s:
        num *= 10_000
    return int(num)


def parse_duration(raw) -> int:
    """'39:58:14' -> 143894 ; 已是数字则原样返回"""
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    parts = str(raw).strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] if nums else 0


def looks_like_ad(title: str, card_text: str) -> tuple[bool, str]:
    """识别广告 / 卖课 / 直播卡片。原版用硬编码下标数组, 换个关键词就失效。"""
    t = f"{title or ''} {card_text or ''}"
    for kw, reason in (("正在直播", "直播卡"), ("直播", "直播卡"), ("加入购物车", "商品卡"),
                       ("相关推荐", "推荐位"), ("广告", "广告标记")):
        if kw in t:
            return True, reason
    return False, ""


# ============================================================
# 浏览器 (只用来开一次列表页)
# ============================================================
def build_driver(ctx: CrawlContext):
    from selenium import webdriver
    from selenium.webdriver.edge.options import Options

    opts = Options()
    for a in ("--no-sandbox", "--disable-gpu", "--disable-blink-features=AutomationControlled",
              "--disable-features=IsolateOrigins,site-per-process",
              "--window-size=1500,950", "--accept-language=zh-CN,zh;q=0.9"):
        opts.add_argument(a)
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(f"--user-agent={UA}")
    opts.add_experimental_option("prefs", {
        "profile.default_content_setting_values.notifications": 2,
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    })

    t0 = time.time()
    from server.core.browser_runtime import create_edge
    driver = create_edge(opts)
    ctx.log("INFO", f"浏览器已启动 ({time.time()-t0:.1f}s)")

    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": """
        Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
        Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
        Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh','en']});
        window.chrome={runtime:{},loadTimes:function(){},csi:function(){},app:{}};
    """})
    driver.execute_cdp_cmd("Network.setUserAgentOverride", {
        "userAgent": UA, "acceptLanguage": "zh-CN,zh;q=0.9", "platform": "Win32"})
    return driver


def human_scroll(driver, ctx: CrawlContext, rounds: int = 3) -> None:
    for _ in range(rounds):
        ctx.check_cancel()
        driver.execute_script(f"window.scrollBy(0, {random.randint(700, 1100)})")
        ctx.sleep(random.uniform(0.5, 1.0))
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
    ctx.sleep(random.uniform(1.2, 1.8))


def wait_for_cards(driver, ctx: CrawlContext, timeout: float = 10.0) -> int:
    """
    等视频卡片渲染出来。返回首次命中的卡片数。

    不用 WebDriverWait + 单一 XPath —— 那样一旦选择器不中就白等满超时。
    改成自己轮询多个选择器, 任意一个命中就立刻返回。
    """
    from selenium.webdriver.common.by import By

    deadline = time.time() + timeout
    last = 0
    while time.time() < deadline:
        ctx.check_cancel()
        for sel in CARD_SELECTORS:
            try:
                n = len(driver.find_elements(By.CSS_SELECTOR, sel))
            except Exception:
                n = 0
            if n > 0:
                last = max(last, n)
                # "a[href*=/video/]" 命中不代表卡片真的渲染好了, 多等一轮
                if sel.startswith('a['):
                    time.sleep(0.8)
                return max(last, n)
        time.sleep(0.4)
    return 0


def crawl_list_page(driver, ctx: CrawlContext, keyword: str, page: int) -> list[dict]:
    """开一页搜索结果, 拿回卡片列表。第 1 页用不带 page 参数的 URL (见常量处说明)。"""
    url = (SEARCH_URL_P1.format(kw=keyword) if page == 1
           else SEARCH_URL.format(kw=keyword, page=page))

    driver.get(url)
    ctx.sleep(random.uniform(1.5, 2.2))

    n = wait_for_cards(driver, ctx, timeout=10)
    if n == 0:
        ctx.warn("首屏没等到卡片，重载一次再试")
        driver.get(url)
        ctx.sleep(2.0)
        n = wait_for_cards(driver, ctx, timeout=10)

    if n == 0:
        # 最后挣扎: 用真实用户的方式, 先回首页再点进来
        ctx.warn("仍未渲染，尝试先访问首页再回到搜索页")
        try:
            driver.get("https://www.bilibili.com/")
            ctx.sleep(1.5)
            driver.get(url)
            ctx.sleep(2.5)
            n = wait_for_cards(driver, ctx, timeout=8)
        except Exception as e:
            ctx.warn(f"兜底访问失败: {e}")

    human_scroll(driver, ctx)

    html = driver.page_source
    for sig in ("_____tmd_____", "punish"):
        if sig in html:
            ctx.warn(f"页面出现风控信号『{sig}』，本页数据可能不完整")

    # 主路: JS 提取 (一次往返拿全部卡片)
    cards = driver.execute_script(JS_EXTRACT) or []

    # 兜底: 原版 XPath
    if not cards:
        ctx.warn("JS 提取为空，改用原版 XPath 兜底")
        from lxml import etree
        tree = etree.HTML(html)
        for div in tree.xpath('//*[@class="video-list row"]/div'):
            a = div.xpath('.//a[contains(@href,"/video/")]')
            if not a:
                continue
            href = a[0].get("href", "")
            m = re.search(r"(BV[0-9A-Za-z]{10})", href)
            if not m:
                continue
            txt = " ".join((div.text_content() or "").split())
            cards.append({
                "bv": m.group(1),
                "url": f"https://www.bilibili.com/video/{m.group(1)}/",
                "title": (a[0].get("title") or txt)[:200],
                "views_raw": "", "danmaku_raw": "", "duration_text": "",
                "author": "", "date_text": "", "card_text": txt[:400],
            })

    ctx.log("INFO", f"第 {page} 页解析到 {len(cards)} 张卡片")
    return cards


# ============================================================
# 详情 API
# ============================================================
def fetch_detail(session, ctx: CrawlContext, bv: str) -> dict | None:
    """
    调公开 API 拿完整统计。失败返回 None (不抛异常 —— 一个视频失败不该中断整批)。
    """
    try:
        r = session.get(VIEW_API, params={"bvid": bv}, timeout=15)
        if r.status_code != 200:
            ctx.warn(f"{bv} HTTP {r.status_code}")
            return None
        j = r.json()
        if j.get("code") != 0:
            ctx.warn(f"{bv} 接口返回 code={j.get('code')} msg={j.get('message')}")
            return None
        d = j.get("data") or {}
        st = d.get("stat") or {}
        return {
            "title": d.get("title", ""),
            "views": st.get("view", 0),
            "danmaku": st.get("danmaku", 0),
            "like": st.get("like", 0),
            "coin": st.get("coin", 0),
            "favorite": st.get("favorite", 0),
            "reply": st.get("reply", 0),
            "share": st.get("share", 0),
            "duration_sec": d.get("duration", 0),
            "author": (d.get("owner") or {}).get("name", ""),
            "pubdate": d.get("pubdate", 0),
            "tname": d.get("tname", ""),
            "pic": d.get("pic", ""),
        }
    except Exception as e:
        ctx.warn(f"{bv} 请求异常: {type(e).__name__}: {e}")
        return None


# ============================================================
# 入口
# ============================================================
def run(ctx: CrawlContext) -> None:
    keywords = ctx.get_list("keyword")
    if not keywords:
        ctx.err("没有有效的关键词")
        return

    pages = max(1, ctx.get_int("pages", 1))
    detail_limit = max(0, ctx.get_int("detail_limit", 30))
    min_views = max(0, ctx.get_int("min_views", 0))

    ctx.log("INFO", f"关键词 {keywords} · 每个 {pages} 页 · 补详情上限 {detail_limit} · 最低播放 {min_views}")

    import requests
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })

    driver = None
    basics: dict[str, dict] = {}          # bv -> 列表页基本信息
    order: list[str] = []
    skipped = {"ad": 0, "dup": 0, "low": 0}

    try:
        # ---------- 阶段一: 列表页拿 BV ----------
        driver = build_driver(ctx)
        total_pages = len(keywords) * pages
        done = 0

        for keyword in keywords:
            for page in range(1, pages + 1):
                ctx.check_cancel()
                cards = crawl_list_page(driver, ctx, keyword, page)
                for card in cards:
                    ctx.check_cancel()
                    bv = card.get("bv")
                    if not bv or bv in basics:
                        skipped["dup"] += 1
                        continue
                    title = (card.get("title") or "").strip()
                    if len(title) < 4:
                        continue
                    is_ad, reason = looks_like_ad(title, card.get("card_text", ""))
                    if is_ad:
                        ctx.log("WARN", f"跳过疑似{reason}: {title[:28]}")
                        skipped["ad"] += 1
                        continue
                    views = parse_count(card.get("views_raw", ""))
                    if views < min_views:
                        skipped["low"] += 1
                        continue
                    basics[bv] = {
                        "bv": bv,
                        "url": card.get("url") or f"https://www.bilibili.com/video/{bv}/",
                        "title": title[:200],
                        "views": views,
                        "danmaku": parse_count(card.get("danmaku_raw", "")),
                        "duration_sec": parse_duration(card.get("duration_text", "")),
                        "author": (card.get("author") or "").strip()[:60],
                        "date_text": (card.get("date_text") or "").strip()[:30],
                        "keyword": keyword,
                    }
                    order.append(bv)
                done += 1
                ctx.progress_step(done, total_pages * 2, f"列表页 {done}/{total_pages} 完成")

        ctx.log("OK", f"列表阶段结束: {len(basics)} 个视频"
                      f"（过滤掉 广告{skipped['ad']} 播放量不足{skipped['low']}）")

        if not basics:
            ctx.err("列表页一个视频都没拿到 —— 可能被风控或关键词无结果")
            return

        # ---------- 阶段二: API 补详情 ----------
        # 优先给播放量高的补 —— 那才是用户关心的
        targets = sorted(basics.values(), key=lambda x: -x["views"])[:detail_limit]
        target_bvs = {t["bv"] for t in targets} if detail_limit else set()
        if detail_limit == 0:
            ctx.warn("detail_limit=0，只入库列表页基础数据（没有点赞/投币/收藏）")
        else:
            ctx.log("INFO", f"开始补详情: {len(targets)} 个视频 (每个约 0.4 秒)")

        enriched = 0
        for i, bv in enumerate([b for b in order if b in target_bvs], 1):
            ctx.check_cancel()
            item_base = basics[bv]
            detail = fetch_detail(session, ctx, bv)
            if detail:
                enriched += 1
            ctx.progress_step(total_pages + i, total_pages * 2 + len(targets),
                              f"补详情 {i}/{len(targets)} · {item_base['title'][:20]}")

            v = detail or {}
            views = v.get("views") or item_base["views"]
            danmaku = v.get("danmaku") or item_base["danmaku"]
            like = v.get("like")
            coin = v.get("coin")
            fav = v.get("favorite")
            reply = v.get("reply")
            share = v.get("share")
            duration = v.get("duration_sec") or item_base["duration_sec"]

            # 点赞率 / 三连率 —— 沿用原版口径, 但用整数算更准
            like_rate = round(like / views, 4) if (like is not None and views) else None
            triple = None
            if like is not None and coin is not None and fav is not None and views:
                triple = round((like + coin + fav) / views, 4)

            ctx.emit_item({
                "title": (v.get("title") or item_base["title"])[:200],
                "bv": bv,
                "url": item_base["url"],
                "keyword": item_base["keyword"],
                "views": views,
                "danmaku": danmaku,
                "like": like,
                "coin": coin,
                "favorite": fav,
                "reply": reply,
                "share": share,
                "duration_sec": duration,
                "author": v.get("author") or item_base["author"],
                "pubdate": v.get("pubdate"),
                "tname": v.get("tname", ""),
                "like_rate": like_rate,
                "triple_rate": triple,
                "danmaku_per_10k": round(danmaku / views * 10000, 2) if views else 0.0,
                "detail_ok": 1 if detail else 0,
            })

            if i < len(targets):
                ctx.sleep(random.uniform(*API_GAP))     # 礼貌间隔

        ctx.ok(f"补详情完成: {enriched}/{len(targets)} 成功")

        # ---------- 只抓了列表页的那些也入库, 但标记没有详情 ----------
        for bv in order:
            ctx.check_cancel()
            if bv in target_bvs:
                continue
            b = basics[bv]
            ctx.emit_item({
                "title": b["title"], "bv": bv, "url": b["url"], "keyword": b["keyword"],
                "views": b["views"], "danmaku": b["danmaku"],
                "like": None, "coin": None, "favorite": None, "reply": None, "share": None,
                "duration_sec": b["duration_sec"], "author": b["author"],
                "danmaku_per_10k": round(b["danmaku"] / b["views"] * 10000, 2) if b["views"] else 0.0,
                "detail_ok": 0,
            })

    finally:
        if driver is not None:
            try:
                driver.quit()
                ctx.log("INFO", "浏览器已关闭")
            except Exception:
                pass
        session.close()
