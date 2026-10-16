"""BUFF 列表扫描 + Steam 行情补全。

先用 BUFF 自己的 Steam 参考价去掉明显没差价的饰品，只对剩下的请求 Steam。
item_nameid 写入本地表，下一轮不再下载商品页 HTML。
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
MIN_STEAM_VOLUME = 20
STEAM_FEE_FACTOR = 0.85
# 参考价算出的比例已经高于这个值时，即使 Steam 现价再高一截也过不了 0.8
REFERENCE_SKIP_RATIO = 1.05
EMPTY_PAGE_LIMIT = 3
BUFF_PAGE_SIZE = 80
ROUND_PAUSE_SECONDS = 180
STEAM_MIN_INTERVAL = 2.5
STEAM_MAX_INTERVAL = 90

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

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class SteamPacer:
    """所有 Steam 请求共用一个间隔。429 时把间隔拉长，成功后再慢慢收回来。"""

    def __init__(self):
        self.interval = 3.0
        self._next_at = 0.0

    def wait(self, sleep):
        delay = self._next_at - time.time()
        if delay > 0 and not sleep(delay):
            return False
        return True

    def mark_sent(self):
        jitter = random.uniform(0.2, 0.8)
        self._next_at = time.time() + self.interval + jitter

    def penalize(self):
        self.interval = min(max(self.interval * 2, 60), STEAM_MAX_INTERVAL)
        self._next_at = time.time() + self.interval

    def reward(self):
        self.interval = max(STEAM_MIN_INTERVAL, self.interval * 0.85)


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


def ensure_nameid_schema(db_file):
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
        conn.commit()


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
        self.session = requests.Session()
        self._configure_proxy()

    def run(self):
        ensure_nameid_schema(self.db_file)
        self.nameids = self._load_nameids()
        min_sell_count = int(self.config["min_sell_count"])
        self.log(
            "入库条件与页面默认筛选一致: "
            f"出售价比例 ≤ {MAX_SELL_RATIO}，Steam 24小时成交量 ≥ {MIN_STEAM_VOLUME}，"
            f"BUFF 在售数 ≥ {min_sell_count}"
        )
        self.log(
            f"BUFF 每页 {BUFF_PAGE_SIZE} 条。参考价比例 > {REFERENCE_SKIP_RATIO} 的不请求 Steam。"
            f"已缓存 {len(self.nameids)} 个 Steam item_nameid。"
            f"每轮结束后暂停 {ROUND_PAUSE_SECONDS} 秒。"
        )

        while not self.should_stop():
            if not self._scan_catalog(min_sell_count):
                break
            if self.should_stop():
                break
            self.log(f"本轮结束，{ROUND_PAUSE_SECONDS} 秒后开始下一轮。")
            if not self._sleep(ROUND_PAUSE_SECONDS):
                break

    def _scan_catalog(self, min_sell_count):
        page_num = 1
        total_pages = 1
        empty_streak = 0

        while not self.should_stop():
            items, reported_pages, ok = self._fetch_buff_page(page_num)
            if reported_pages:
                total_pages = reported_pages
            if not ok or not items:
                empty_streak += 1
                self.log(f"页面 {page_num} 无物品（连续 {empty_streak} 次）")
                if empty_streak >= EMPTY_PAGE_LIMIT:
                    self.log("连续 3 次没有拿到 BUFF 列表，已停止。请检查 BUFF 登录凭证、价格区间或代理。")
                    return False
                if not self._sleep(random.uniform(5, 8)):
                    return False
                continue

            empty_streak = 0
            stats = self._process_page(items, min_sell_count)
            self.log(
                f"页面 {page_num}/{total_pages}：{len(items)} 个，"
                f"在售达标 {stats['qualified']}，参考价跳过 {stats['reference_skipped']}，"
                f"查询 Steam {stats['steam_checked']}，入库 {stats['saved']}"
            )

            page_num += 1
            if page_num > total_pages:
                return True
            if not self._sleep(1):
                return False
        return False

    def _process_page(self, items, min_sell_count):
        stats = {"qualified": 0, "reference_skipped": 0, "steam_checked": 0, "saved": 0}
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

            self.log(f"  处理: {item_name} (BUFF在售数: {buff_sell_num})")
            stats["steam_checked"] += 1
            record = self._fetch_steam_record(item, buff_sell_price, buff_sell_num)
            if not record:
                continue
            self.save_item(record)
            self.remember_item(record)
            stats["saved"] += 1
            self.log(f"    ✓ 已保存，数据库共 {self.count_items()} 条")
        return stats

    def _fetch_steam_record(self, item, buff_sell_price, buff_sell_num):
        market_url = item.get("steam_market_url")
        market_ref = extract_steam_market_ref(market_url)
        if not market_ref:
            self.log(f"    无法解析 Steam 市场链接: {item.get('name')} -> {market_url}")
            return None

        overview = self._price_overview(market_ref["appid"], market_ref["market_hash_name"])
        if not overview:
            return None

        steam_sell_price = overview["lowest_price"]
        steam_volume = overview["volume"]
        if steam_sell_price <= 0:
            self.log(f"    Steam 没有最低出售价，跳过: {item.get('name')}")
            return None

        ratio = sell_ratio(buff_sell_price, steam_sell_price)
        self.log(
            f"    比例: {ratio:.2f} "
            f"(BUFF出售价:¥{buff_sell_price} Steam出售价:¥{steam_sell_price} "
            f"Steam成交量:{steam_volume})"
        )
        reasons = []
        if ratio > MAX_SELL_RATIO:
            reasons.append(f"比例 {ratio:.2f} > {MAX_SELL_RATIO}")
        if steam_volume < MIN_STEAM_VOLUME:
            reasons.append(f"Steam成交量 {steam_volume} < {MIN_STEAM_VOLUME}")
        if reasons:
            self.log(f"    跳过，未达入库条件: {'; '.join(reasons)}")
            return None

        steam_market_id = market_ref["steam_market_id"]
        item_nameid = self.nameids.get(steam_market_id)
        if not item_nameid:
            item_nameid = self._fetch_item_nameid(market_url, steam_market_id, item.get("name"))

        steam_sell_num = 0
        steam_buy_price = 0.0
        if item_nameid:
            histogram = self._fetch_histogram(item_nameid, item.get("name"))
            if histogram:
                steam_sell_num = parse_histogram_sell_count(histogram)
                steam_buy_price = parse_histogram_buy_price(histogram)
                self.log(f"    Steam在售数: {steam_sell_num} 求购价:¥{steam_buy_price:.2f}")
            else:
                self.log(f"    Steam 直方图获取失败，在售数记为 0: {item.get('name')}")
        else:
            self.log(f"    未找到 Steam item_nameid，在售数记为 0: {item.get('name')}")

        try:
            buff_buy_price = float(item.get("buy_max_price") or 0)
        except (TypeError, ValueError):
            buff_buy_price = 0.0

        return {
            "name": item.get("name"),
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

    def _price_overview(self, appid, market_hash_name):
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
        """发一次 Steam 请求。429 时拉长间隔并再试一次。停止信号返回 None。"""
        for attempt in range(2):
            if self.should_stop() or not self.pacer.wait(self._sleep):
                return None
            try:
                response = self.session.get(
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

            if response.status_code == 429:
                self.pacer.penalize()
                self.log(f"Steam 429，下次请求间隔 {self.pacer.interval:.0f} 秒")
                continue

            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                self.log(f"    Steam HTTP 错误 ({item_name}): {exc}")
                return None
            self.pacer.reward()
            self.pacer.mark_sent()
            return response
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
            response = self.session.get(
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
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            self.log(f"获取 Buff 数据时发生错误: {exc}")
            return [], 0, False

        if data.get("code") == "OK" and "data" in data:
            payload = data["data"] or {}
            return payload.get("items") or [], int(payload.get("total_page") or 0), True

        self.log(f"Buff API 错误: {data.get('error') or data.get('code') or '未知错误'}")
        return [], 0, False

    def _configure_proxy(self):
        proxy_url = (self.config.get("proxy_url") or "").strip()
        if not proxy_url:
            self.log("未配置代理")
            self.session.proxies = {}
            return
        if proxy_url in _INVALID_PROXY_URLS:
            self.log(f"检测到无效代理 URL: {proxy_url}，将不使用代理")
            self.session.proxies = {}
            return
        if not proxy_url.startswith(("http://", "https://", "socks5://")):
            proxy_url = "http://" + proxy_url
        self.session.proxies = {"http": proxy_url, "https": proxy_url}
        self.session.verify = False
        self.log(f"已配置代理: {proxy_url}")

    def _load_nameids(self):
        with self.db_lock:
            with sqlite3.connect(self.db_file, timeout=10) as conn:
                rows = conn.execute(
                    "SELECT steam_market_id, item_nameid FROM steam_item_nameids"
                ).fetchall()
        return {steam_market_id: item_nameid for steam_market_id, item_nameid in rows}

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
