"""BUFF 列表扫描 + 少量 Steam 补全。

比例先用 BUFF 上的 Steam 参考价判断。Steam 的 priceoverview 每轮最多请求 20 次，
结果缓存 6 小时。额度用完或连续 429 时停在当前页，下一轮从这一页继续。
"""

import random
import re
import sqlite3
import time
from datetime import datetime
from urllib.parse import unquote, urlencode, urlparse

import requests

# 与页面默认筛选一致
MAX_SELL_RATIO = 0.8
# BUFF 参考价更新慢，预筛放宽，只丢掉明显高于入库线的饰品
REFERENCE_SKIP_RATIO = 0.85
MIN_STEAM_VOLUME = 20
STEAM_FEE_FACTOR = 0.85
EMPTY_PAGE_LIMIT = 3
BUFF_PAGE_SIZE = 80
BUFF_PAGE_INTERVAL = 6.0
BUFF_429_BASE_WAIT = 60
BUFF_429_MAX_WAIT = 180
ROUND_PAUSE_SECONDS = 180
# priceoverview 很紧。每轮只补一批，同一饰品 6 小时内不重复请求。
# 额度用完就停在当前页，不要把后面的列表空跑完。
STEAM_CALLS_PER_ROUND = 20
STEAM_CACHE_TTL_SECONDS = 6 * 60 * 60
STEAM_MIN_INTERVAL = 8.0
STEAM_429_RETRIES = 3

_INVALID_PROXY_URLS = {
    "https://store.steampowered.com/",
    "http://store.steampowered.com/",
    "https://steamcommunity.com/",
    "http://steamcommunity.com/",
    "https://steamcommunity.com/market/",
    "http://steamcommunity.com/market/",
    "https://buff.163.com/",
    "http://buff.163.com/",
}

def normalize_proxy_url(raw_value):
    """把代理地址整理成 requests 可用的 URL。站点地址和空值返回空字符串。"""
    proxy_url = (raw_value or "").strip()
    if not proxy_url or proxy_url in _INVALID_PROXY_URLS:
        return ""
    if proxy_url.lower().startswith(("http://", "https://", "socks5://", "socks5h://")):
        return proxy_url
    return "http://" + proxy_url


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class SteamPacer:
    """Steam 请求之间固定留出间隔，避免连续打满 priceoverview。"""

    def __init__(self):
        self.interval = STEAM_MIN_INTERVAL
        self._next_at = 0.0

    def wait(self, sleep):
        delay = self._next_at - time.time()
        if delay > 0 and not sleep(delay):
            return False
        return True

    def mark_sent(self):
        jitter = random.uniform(0.4, 1.2)
        self._next_at = time.time() + self.interval + jitter


def parse_steam_price_text(raw_value):
    """把 Steam 返回的价格字符串安全转成浮点数。"""
    if raw_value in (None, ""):
        return 0.0

    text = str(raw_value).strip()
    cleaned = re.sub(r"[^\d,.\-]", "", text)
    if not cleaned:
        return 0.0

    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")

    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_steam_count(raw_value):
    digits = re.sub(r"[^\d]", "", str(raw_value or ""))
    return int(digits) if digits else 0


def extract_steam_market_ref(market_url):
    """从 Steam 市场链接中提取 appid、market_hash_name 和 listings 路径。"""
    if not market_url:
        return None

    parsed = urlparse(market_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[0] != "market" or parts[1] != "listings":
        return None

    appid = parts[2]
    market_hash_name = unquote("/".join(parts[3:]))
    return {
        "appid": appid,
        "market_hash_name": market_hash_name,
        "steam_market_id": f"{appid}/{market_hash_name}",
    }


def extract_steam_item_nameid(market_html):
    patterns = [
        r"Market_LoadOrderSpread\s*\(\s*(\d+)\s*\)",
        r"Market_LoadOrderSpread\$\s+(\d+)",
        r'name="item_nameid"\s+value="(\d+)"',
        r'"item_nameid"\s*:\s*"?(\d+)"?',
    ]
    for pattern in patterns:
        match = re.search(pattern, market_html or "")
        if match:
            return match.group(1)
    return None


def parse_histogram_sell_count(api_data):
    count = parse_steam_count(api_data.get("sell_order_count"))
    if count:
        return count
    summary = api_data.get("sell_order_summary") or ""
    tagged = re.search(r">(\d[\d,]*)<", summary)
    if tagged:
        return parse_steam_count(tagged.group(1))
    plain = re.search(r"(\d[\d,]*)", summary)
    return parse_steam_count(plain.group(1)) if plain else 0


def parse_histogram_buy_price(api_data):
    table = api_data.get("buy_order_table") or ""
    match = re.search(r"[¥￥]\s*([\d,.]+)", table)
    if match:
        price = parse_steam_price_text(match.group(1))
        if price > 0:
            return price
    # currency=23 时 highest_buy_order 的单位是分
    minor = parse_steam_count(api_data.get("highest_buy_order"))
    if minor:
        return minor / 100.0
    return 0.0


def buff_steam_reference_price(item):
    """BUFF 列表里自带的 Steam 人民币参考价。和在售价几乎相同的值不当成 Steam 价。"""
    sell_min = parse_steam_price_text(item.get("sell_min_price"))
    candidates = []
    goods_info = item.get("goods_info") or {}
    if isinstance(goods_info, dict):
        candidates.append(goods_info.get("steam_price_cny"))
    candidates.append(item.get("sell_reference_price"))

    for raw_value in candidates:
        price = parse_steam_price_text(raw_value)
        if price <= 0:
            continue
        if sell_min > 0 and abs(price - sell_min) / sell_min <= 0.05:
            continue
        return price
    return 0.0


def sell_ratio(buff_sell_price, steam_sell_price):
    if steam_sell_price <= 0:
        return None
    return buff_sell_price / (steam_sell_price * STEAM_FEE_FACTOR)


def ensure_crawler_schema(db_file):
    with sqlite3.connect(db_file, timeout=10) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS steam_item_nameids (
                steam_market_id TEXT PRIMARY KEY,
                item_nameid TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS steam_quotes (
                steam_market_id TEXT PRIMARY KEY,
                sell_price REAL,
                buy_price REAL,
                sell_num INTEGER,
                volume INTEGER,
                price_fetched_at TEXT,
                listings_fetched_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS crawler_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.commit()


def quote_age_seconds(timestamp):
    if not timestamp:
        return None
    try:
        fetched = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (datetime.now() - fetched).total_seconds()


class MarketCrawler:
    def __init__(
        self,
        config,
        *,
        db_file,
        db_lock,
        log,
        save_item,
        remember_item,
        count_items,
        buff_cookies,
        steam_cookies,
        should_stop,
    ):
        self.config = config
        self.db_file = db_file
        self.db_lock = db_lock
        self.log = log
        self.save_item = save_item
        self.remember_item = remember_item
        self.count_items = count_items
        self.buff_cookies = buff_cookies
        self.steam_cookies = steam_cookies
        self.should_stop = should_stop
        self.pacer = SteamPacer()
        self.nameids = {}
        self.quotes = {}
        self.steam_calls = 0
        self.steam_blocked = False
        self.catalog_paused = False
        self.buff_session = requests.Session()
        self.steam_session = requests.Session()
        self._configure_proxies()

    def run(self):
        ensure_crawler_schema(self.db_file)
        self.nameids = self._load_nameids()
        self.quotes = self._load_quotes()
        min_sell_count = int(self.config["min_sell_count"])
        self.log(
            "入库条件与页面默认筛选一致: "
            f"出售价比例 ≤ {MAX_SELL_RATIO}，Steam 24小时成交量 ≥ {MIN_STEAM_VOLUME}，"
            f"BUFF 在售数 ≥ {min_sell_count}"
        )
        self.log(
            f"BUFF 列表每页间隔约 {BUFF_PAGE_INTERVAL:.0f} 秒，遇到 429 会等待后重试同一页。"
            f"参考价比例高于 {REFERENCE_SKIP_RATIO} 才跳过 Steam。"
            "Steam 价格和成交量优先用 6 小时内的缓存。"
            f"每轮最多新请求 {STEAM_CALLS_PER_ROUND} 次，间隔约 {STEAM_MIN_INTERVAL:.0f} 秒。"
            "额度用完或连续 429 就停在当前页，下一轮从这一页继续。"
        )

        while not self.should_stop():
            self.steam_calls = 0
            self.steam_blocked = False
            self.catalog_paused = False
            if not self._scan_catalog(min_sell_count):
                break
            if self.should_stop():
                break
            self.log(f"本轮结束，{ROUND_PAUSE_SECONDS} 秒后开始下一轮。")
            if not self._sleep(ROUND_PAUSE_SECONDS):
                break

    def _scan_catalog(self, min_sell_count):
        page_num = self._load_resume_page()
        total_pages = max(page_num, 1)
        empty_streak = 0
        rate_streak = 0
        if page_num > 1:
            self.log(f"从第 {page_num} 页继续。")

        while not self.should_stop():
            items, reported_pages, status = self._fetch_buff_page(page_num)
            if reported_pages:
                total_pages = reported_pages
            if reported_pages and page_num > total_pages:
                self._save_resume_page(1)
                self.log("已扫完最后一页，下一轮从第 1 页开始。")
                return True
            if status == "rate_limited":
                rate_streak += 1
                wait = min(BUFF_429_BASE_WAIT * (2 ** (rate_streak - 1)), BUFF_429_MAX_WAIT)
                self.log(f"BUFF 429，{wait:.0f} 秒后重试第 {page_num} 页")
                if not self._sleep(wait):
                    self._save_resume_page(page_num)
                    return False
                continue
            if status != "ok" or not items:
                rate_streak = 0
                empty_streak += 1
                self.log(f"页面 {page_num} 无物品（连续 {empty_streak} 次）")
                if empty_streak >= EMPTY_PAGE_LIMIT:
                    self._save_resume_page(page_num)
                    self.log("连续 3 次没有拿到 BUFF 列表，已停止。请检查 BUFF 登录凭证、价格区间或代理。")
                    return False
                if not self._sleep(random.uniform(5, 8)):
                    self._save_resume_page(page_num)
                    return False
                continue

            empty_streak = 0
            rate_streak = 0
            stats = self._process_page(items, min_sell_count)
            self.log(
                f"页面 {page_num}/{total_pages}：{len(items)} 个，"
                f"在售达标 {stats['qualified']}，参考价跳过 {stats['reference_skipped']}，"
                f"用缓存 {stats['cache_hits']}，请求 Steam {stats['steam_checked']}，"
                f"未达标 {stats['below_threshold']}，入库 {stats['saved']}"
            )
            if self.catalog_paused:
                self._save_resume_page(page_num)
                if self.steam_blocked:
                    self.log(f"Steam 连续 429，停在第 {page_num} 页，下一轮从这里继续。")
                else:
                    self.log(
                        f"本轮 Steam 请求已达 {STEAM_CALLS_PER_ROUND} 次，"
                        f"停在第 {page_num} 页，下一轮从这里继续。"
                    )
                return True

            page_num += 1
            if page_num > total_pages:
                self._save_resume_page(1)
                self.log("已扫完最后一页，下一轮从第 1 页开始。")
                return True
            if not self._sleep(BUFF_PAGE_INTERVAL + random.uniform(0.5, 2.0)):
                self._save_resume_page(page_num)
                return False
        self._save_resume_page(page_num)
        return False

    def _process_page(self, items, min_sell_count):
        stats = {
            "qualified": 0,
            "reference_skipped": 0,
            "cache_hits": 0,
            "steam_checked": 0,
            "below_threshold": 0,
            "saved": 0,
        }
        for item in items:
            if self.should_stop():
                break
            try:
                buff_sell_num = int(item.get("sell_num") or 0)
            except (TypeError, ValueError):
                buff_sell_num = 0
            if buff_sell_num < min_sell_count:
                continue

            stats["qualified"] += 1
            item_name = item.get("name", "未知")
            try:
                buff_sell_price = float(item.get("sell_min_price") or 0)
            except (TypeError, ValueError):
                buff_sell_price = 0.0

            reference = buff_steam_reference_price(item)
            reference_ratio = sell_ratio(buff_sell_price, reference) if reference else None
            if reference_ratio is not None and reference_ratio > REFERENCE_SKIP_RATIO:
                stats["reference_skipped"] += 1
                continue

            market_ref = extract_steam_market_ref(item.get("steam_market_url"))
            if not market_ref:
                self.log(f"    无法解析 Steam 市场链接: {item_name}")
                continue

            quote = self.quotes.get(market_ref["steam_market_id"]) or {}
            price_fresh = self._is_fresh(quote.get("price_fetched_at")) and (quote.get("sell_price") or 0) > 0
            if price_fresh:
                stats["cache_hits"] += 1
            elif not self._can_call_steam():
                self.catalog_paused = True
                break
            else:
                stats["steam_checked"] += 1
                self.log(f"  处理: {item_name} (BUFF在售数: {buff_sell_num})")

            record = self._build_record(item, buff_sell_price, buff_sell_num, market_ref, reference)
            if self.steam_blocked:
                self.catalog_paused = True
                if record:
                    self.save_item(record)
                    self.remember_item(record)
                    stats["saved"] += 1
                break
            if not record:
                stats["below_threshold"] += 1
                continue
            self.save_item(record)
            self.remember_item(record)
            stats["saved"] += 1
            if self.steam_calls >= STEAM_CALLS_PER_ROUND:
                self.catalog_paused = True
                break
        return stats

    def _build_record(self, item, buff_sell_price, buff_sell_num, market_ref, reference):
        steam_market_id = market_ref["steam_market_id"]
        quote = dict(self.quotes.get(steam_market_id) or {})
        steam_sell_price = float(quote.get("sell_price") or 0)
        steam_volume = int(quote.get("volume") or 0)
        steam_sell_num = int(quote.get("sell_num") or 0)
        steam_buy_price = float(quote.get("buy_price") or 0)
        item_name = item.get("name", "未知")

        if not self._is_fresh(quote.get("price_fetched_at")) or steam_sell_price <= 0:
            overview = self._price_overview(market_ref["appid"], market_ref["market_hash_name"])
            if overview and overview["lowest_price"] > 0:
                steam_sell_price = overview["lowest_price"]
                steam_volume = overview["volume"]
                self._store_quote(
                    steam_market_id,
                    sell_price=steam_sell_price,
                    volume=steam_volume,
                    price_fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
            elif steam_sell_price <= 0 and reference > 0:
                steam_sell_price = reference

        if (
            steam_sell_num <= 0
            and not self._is_fresh(quote.get("listings_fetched_at"))
            and self._is_fresh(quote.get("price_fetched_at"))
            and self._can_call_steam()
            and steam_sell_price > 0
        ):
            listings = self._fetch_listings(item, steam_market_id)
            if listings:
                steam_sell_num = listings["sell_num"]
                steam_buy_price = listings["buy_price"]
                self._store_quote(
                    steam_market_id,
                    sell_num=steam_sell_num,
                    buy_price=steam_buy_price,
                    listings_fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )

        if steam_sell_price <= 0:
            return None

        ratio = sell_ratio(buff_sell_price, steam_sell_price)
        reasons = []
        if ratio is None or ratio > MAX_SELL_RATIO:
            reasons.append(f"比例 {ratio:.2f} > {MAX_SELL_RATIO}" if ratio is not None else "没有 Steam 出售价")
        if steam_volume < MIN_STEAM_VOLUME:
            reasons.append(f"Steam成交量 {steam_volume} < {MIN_STEAM_VOLUME}")
        if reasons:
            return None

        try:
            buff_buy_price = float(item.get("buy_max_price") or 0)
        except (TypeError, ValueError):
            buff_buy_price = 0.0

        return {
            "name": item.get("name") or item_name,
            "kind": item.get("game"),
            "buff_id": item.get("id"),
            "steam_market_id": steam_market_id,
            "buff_buy_price": buff_buy_price,
            "buff_sell_price": buff_sell_price,
            "steam_sell_price": steam_sell_price,
            "steam_buy_price": steam_buy_price,
            "steam_sell_num": steam_sell_num,
            "buff_sell_num": buff_sell_num,
            "steam_volume": steam_volume,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _can_call_steam(self):
        return not self.steam_blocked and self.steam_calls < STEAM_CALLS_PER_ROUND

    def _is_fresh(self, timestamp):
        age = quote_age_seconds(timestamp)
        return age is not None and age < STEAM_CACHE_TTL_SECONDS

    def _fetch_listings(self, item, steam_market_id):
        item_name = item.get("name", "未知")
        item_nameid = self.nameids.get(steam_market_id)
        if not item_nameid:
            item_nameid = self._fetch_item_nameid(item.get("steam_market_url"), steam_market_id, item_name)
        if not item_nameid or not self._can_call_steam():
            return None
        histogram = self._fetch_histogram(item_nameid, item_name)
        if not histogram:
            return None
        sell_num = parse_histogram_sell_count(histogram)
        buy_price = parse_histogram_buy_price(histogram)
        self.log(f"    Steam在售数: {sell_num} 求购价:¥{buy_price:.2f}")
        return {"sell_num": sell_num, "buy_price": buy_price}

    def _price_overview(self, appid, market_hash_name):
        if not self._can_call_steam():
            return None
        query = urlencode({
            "appid": appid,
            "market_hash_name": market_hash_name,
            "currency": 23,
        })
        url = f"https://steamcommunity.com/market/priceoverview/?{query}"
        response = self._steam_get(url, item_name=market_hash_name)
        if response is None:
            return None
        try:
            data = response.json()
        except ValueError:
            self.log(f"    Steam 价格不是 JSON: {market_hash_name}")
            return None
        if not data.get("success"):
            self.log(f"    Steam 价格接口失败: {market_hash_name}")
            return None
        return {
            "volume": parse_steam_count(data.get("volume")),
            "lowest_price": parse_steam_price_text(data.get("lowest_price")),
        }

    def _fetch_item_nameid(self, market_url, steam_market_id, item_name):
        response = self._steam_get(market_url, item_name=item_name)
        if response is None:
            return None
        item_nameid = extract_steam_item_nameid(response.text)
        if item_nameid:
            self._store_nameid(steam_market_id, item_nameid)
        return item_nameid

    def _fetch_histogram(self, item_nameid, item_name):
        query = urlencode({
            "country": "CN",
            "language": "schinese",
            "currency": 23,
            "item_nameid": item_nameid,
        })
        url = f"https://steamcommunity.com/market/itemordershistogram?{query}"
        response = self._steam_get(url, item_name=item_name)
        if response is None:
            return None
        if response.text.strip().startswith("<"):
            self.log("Steam 直方图返回了 HTML，Cookie 可能失效")
            return None
        try:
            data = response.json()
        except ValueError:
            self.log(f"    Steam 直方图不是 JSON: {item_name}")
            return None
        if not data.get("success"):
            return None
        return data

    def _steam_get(self, url, item_name=""):
        """发一次 Steam 请求。429 时等待后重试，连续失败才暂停本轮。"""
        for attempt in range(STEAM_429_RETRIES):
            if self.steam_blocked or self.should_stop() or not self.pacer.wait(self._sleep):
                return None
            self.steam_calls += 1
            try:
                response = self.steam_session.get(
                    url,
                    cookies=self.steam_cookies,
                    headers={
                        "User-Agent": _BROWSER_UA,
                        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    },
                    timeout=15,
                )
            except requests.RequestException as exc:
                self.log(f"    Steam 请求失败 ({item_name}): {exc}")
                return None

            if response.status_code != 429:
                try:
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    self.log(f"    Steam HTTP 错误 ({item_name}): {exc}")
                    return None
                self.pacer.mark_sent()
                return response

            if attempt + 1 >= STEAM_429_RETRIES:
                self.steam_blocked = True
                self.log("Steam 连续 429，本轮暂停，稍后从当前页继续。")
                return None
            wait = min(BUFF_429_BASE_WAIT * (2 ** attempt), BUFF_429_MAX_WAIT)
            self.log(f"Steam 429，{wait:.0f} 秒后重试当前饰品。")
            if not self._sleep(wait):
                return None
        return None

    def _fetch_buff_page(self, page_num):
        params = {
            "game": str(self.config.get("game_type") or "CSGO").lower(),
            "min_price": self.config["min_sell_price"],
            "max_price": self.config["max_sell_price"],
            "page_num": page_num,
            "page_size": BUFF_PAGE_SIZE,
            "sort_by": "price.asc",
            "_": int(time.time() * 1000),
        }
        try:
            response = self.buff_session.get(
                "https://buff.163.com/api/market/goods",
                params=params,
                cookies=self.buff_cookies,
                headers={
                    "User-Agent": _BROWSER_UA,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": "https://buff.163.com/",
                },
                timeout=15,
            )
        except requests.RequestException as exc:
            self.log(f"获取 Buff 数据时发生错误: {exc}")
            return [], 0, "error"

        if response.status_code == 429:
            return [], 0, "rate_limited"

        try:
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            self.log(f"获取 Buff 数据时发生错误: {exc}")
            return [], 0, "error"

        if data.get("code") == "OK" and "data" in data:
            payload = data["data"] or {}
            return payload.get("items") or [], int(payload.get("total_page") or 0), "ok"

        self.log(f"Buff API 错误: {data.get('error') or data.get('code') or '未知错误'}")
        return [], 0, "error"

    def _configure_proxies(self):
        self._apply_proxy(self.buff_session, self.config.get("buff_proxy_url"), "BUFF")
        legacy_steam_proxy = self.config.get("steam_proxy_url") or self.config.get("proxy_url")
        self._apply_proxy(self.steam_session, legacy_steam_proxy, "Steam")

    def _apply_proxy(self, session, raw_value, label):
        raw_value = (raw_value or "").strip()
        proxy_url = normalize_proxy_url(raw_value)
        if not proxy_url:
            session.proxies = {}
            if raw_value:
                self.log(f"{label} 代理地址无效，改为直接连接")
            else:
                self.log(f"{label} 未配置代理，直接连接")
            return
        session.proxies = {"http": proxy_url, "https": proxy_url}
        session.verify = False
        self.log(f"{label} 已配置代理: {proxy_url}")

    def _load_nameids(self):
        with self.db_lock:
            with sqlite3.connect(self.db_file, timeout=10) as conn:
                rows = conn.execute(
                    "SELECT steam_market_id, item_nameid FROM steam_item_nameids"
                ).fetchall()
        return {steam_market_id: item_nameid for steam_market_id, item_nameid in rows}

    def _load_quotes(self):
        with self.db_lock:
            with sqlite3.connect(self.db_file, timeout=10) as conn:
                rows = conn.execute(
                    """
                    SELECT steam_market_id, sell_price, buy_price, sell_num, volume,
                           price_fetched_at, listings_fetched_at
                    FROM steam_quotes
                    """
                ).fetchall()
        quotes = {}
        for row in rows:
            quotes[row[0]] = {
                "sell_price": row[1] or 0,
                "buy_price": row[2] or 0,
                "sell_num": row[3] or 0,
                "volume": row[4] or 0,
                "price_fetched_at": row[5],
                "listings_fetched_at": row[6],
            }
        return quotes

    def _catalog_signature(self):
        game = str(self.config.get("game_type") or "CSGO").lower()
        return "|".join([
            game,
            str(self.config.get("min_sell_price")),
            str(self.config.get("max_sell_price")),
            "price.asc",
        ])

    def _state_get(self, key):
        with self.db_lock:
            with sqlite3.connect(self.db_file, timeout=10) as conn:
                row = conn.execute(
                    "SELECT value FROM crawler_state WHERE key = ?",
                    (key,),
                ).fetchone()
        return row[0] if row else None

    def _state_set(self, key, value):
        with self.db_lock:
            with sqlite3.connect(self.db_file, timeout=10) as conn:
                conn.execute(
                    """
                    INSERT INTO crawler_state (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )
                conn.commit()

    def _load_resume_page(self):
        if self._state_get("catalog_signature") != self._catalog_signature():
            return 1
        try:
            page = int(self._state_get("catalog_page") or 1)
        except (TypeError, ValueError):
            return 1
        return page if page >= 1 else 1

    def _save_resume_page(self, page_num):
        self._state_set("catalog_signature", self._catalog_signature())
        self._state_set("catalog_page", str(max(int(page_num), 1)))

    def _store_quote(self, steam_market_id, **fields):
        current = dict(self.quotes.get(steam_market_id) or {})
        current.update(fields)
        self.quotes[steam_market_id] = current
        with self.db_lock:
            with sqlite3.connect(self.db_file, timeout=10) as conn:
                conn.execute(
                    """
                    INSERT INTO steam_quotes (
                        steam_market_id, sell_price, buy_price, sell_num, volume,
                        price_fetched_at, listings_fetched_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(steam_market_id) DO UPDATE SET
                        sell_price = excluded.sell_price,
                        buy_price = excluded.buy_price,
                        sell_num = excluded.sell_num,
                        volume = excluded.volume,
                        price_fetched_at = excluded.price_fetched_at,
                        listings_fetched_at = excluded.listings_fetched_at
                    """,
                    (
                        steam_market_id,
                        current.get("sell_price") or 0,
                        current.get("buy_price") or 0,
                        current.get("sell_num") or 0,
                        current.get("volume") or 0,
                        current.get("price_fetched_at"),
                        current.get("listings_fetched_at"),
                    ),
                )
                conn.commit()

    def _store_nameid(self, steam_market_id, item_nameid):
        self.nameids[steam_market_id] = item_nameid
        updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.db_lock:
            with sqlite3.connect(self.db_file, timeout=10) as conn:
                conn.execute(
                    """
                    INSERT INTO steam_item_nameids (steam_market_id, item_nameid, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(steam_market_id) DO UPDATE SET
                        item_nameid = excluded.item_nameid,
                        updated_at = excluded.updated_at
                    """,
                    (steam_market_id, item_nameid, updated_at),
                )
                conn.commit()

    def _sleep(self, seconds):
        deadline = time.time() + max(seconds, 0)
        while time.time() < deadline:
            if self.should_stop():
                return False
            time.sleep(min(0.5, deadline - time.time()))
        return not self.should_stop()
