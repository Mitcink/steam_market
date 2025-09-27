from flask import Flask, render_template, request, jsonify
import threading
import time
import requests
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import os
import random

app = Flask(__name__)

DATA_FILE = 'crawled_data.json'
crawled_data = []
is_crawling = False
crawler_thread = None
db_lock = threading.Lock()


def load_data_from_file():
    """Load crawled data from a JSON file."""
    global crawled_data
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            try:
                crawled_data = json.load(f)
                print(f"Loaded {len(crawled_data)} items from {DATA_FILE}")
            except (json.JSONDecodeError, FileNotFoundError):
                print(f"Error loading {DATA_FILE}, starting with empty data.")
                crawled_data = []
    else:
        print(f"No {DATA_FILE} found, starting with empty data.")


def save_data_to_file():
    """Save crawled data to a JSON file."""
    with db_lock:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(crawled_data, f, ensure_ascii=False, indent=4)


def parse_cookies_string(cookie_string):
    """
    Parses a semicolon-separated cookie string into a dictionary.
    """
    if not cookie_string:
        return {}
    cookies = {}
    for pair in re.finditer(r'([^=;]+)=([^;]*)', cookie_string):
        key = pair.group(1).strip()
        value = pair.group(2).strip()
        cookies[key] = value
    return cookies


class ScraperService:
    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36"
        })
        if self.config["proxy_url"]:
            self.session.proxies = {"http": self.config["proxy_url"], "https": self.config["proxy_url"]}
            # 禁用 SSL 验证以解决与代理相关的 SSL 错误
            self.session.verify = False

    def get_buff_items(self, page_num):
        params = {
            "game": self.config["game_type"].lower(),
            "min_price": self.config["min_sell_price"],
            "max_price": self.config["max_sell_price"],
            "page_num": page_num,
        }
        cookies = {"session": self.config["buff_session"]}
        try:
            response = self.session.get("https://buff.163.com/api/market/goods", params=params, cookies=cookies)
            response.raise_for_status()
            data = response.json()
            if "data" in data and "items" in data["data"] and "total_page" in data["data"]:
                return data["data"]["items"], data["data"]["total_page"]
            else:
                print("Buff API 返回了非预期的 JSON 格式。")
                print("完整的响应内容如下：", json.dumps(data, indent=2))
                return [], 0
        except requests.exceptions.RequestException as e:
            print(f"获取 Buff 数据时发生错误: {e}")
            return [], 0
        except json.JSONDecodeError:
            print("Buff API 返回的内容无法解析为 JSON。")
            return [], 0

    def get_steam_price_overview(self, appid, market_hash_name):
        """Fetches price overview including volume for a Steam item."""
        url = f"https://steamcommunity.com/market/priceoverview/?appid={appid}&market_hash_name={market_hash_name}&currency=23"
        steam_cookies_dict = parse_cookies_string(self.config["steam_cookies"])
        try:
            response = self.session.get(url, cookies=steam_cookies_dict, timeout=10)
            response.raise_for_status()
            data = response.json()

            # Print the raw data to debug the volume issue
            print(f"Raw Steam priceoverview data for {market_hash_name}: {data}")

            if data.get('success'):
                volume_str = data.get('volume', '0').replace(',', '')
                # Handle cases where volume might be an empty string
                return int(volume_str) if volume_str.isdigit() else 0
        except requests.exceptions.RequestException as e:
            print(f"获取 Steam 价格概览时发生网络错误: {e}")
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            print(f"解析 Steam 价格概览数据时出错: {e}")

        return 0

    def get_steam_data(self, item):
        try:
            market_url = item["steam_market_url"]
            steam_cookies_dict = parse_cookies_string(self.config["steam_cookies"])

            # Request for the market page to get itemordershistogram id
            try:
                response = self.session.get(market_url, cookies=steam_cookies_dict, timeout=10)
                response.raise_for_status()
            except requests.exceptions.HTTPError as e:
                print(
                    f"获取 Steam 市场页面时发生 HTTP 错误 ({item['name']}): {e.response.status_code} - {e.response.reason}")
                return None
            except requests.exceptions.RequestException as e:
                print(f"获取 Steam 市场页面时发生网络错误 ({item['name']}): {e}")
                return None

            market_html = response.text
            match = re.search(r"Market_LoadOrderSpread\(\s(\d+)\s\);", market_html)
            if not match:
                print(f"Market ID not found for {item['name']}. Skipping.")
                return None
            market_id = match.group(1)

            # Request for item orders
            api_url = f"https://steamcommunity.com/market/itemordershistogram?country=CN&language=schinese&currency=23&item_nameid={market_id}"

            try:
                api_response = self.session.get(api_url, cookies=steam_cookies_dict, timeout=10)
                api_response.raise_for_status()
                api_data = api_response.json()
            except requests.exceptions.HTTPError as e:
                print(
                    f"获取 Steam API 数据时发生 HTTP 错误 ({item['name']}): {e.response.status_code} - {e.response.reason}")
                return None
            except requests.exceptions.RequestException as e:
                print(f"获取 Steam API 数据时发生网络错误 ({item['name']}): {e}")
                return None
            except json.JSONDecodeError as e:
                print(f"解析 Steam API JSON 时发生错误 ({item['name']}): {e}")
                print(f"非 JSON 响应内容为: {api_response.text}")
                return None

            sell_table = api_data.get("sell_order_table", "")
            buy_table = api_data.get("buy_order_table", "")

            sell_price_match = re.search(r"¥ (\d+\.\d{2})", sell_table)
            buy_price_match = re.search(r"¥ (\d+\.\d{2})", buy_table)
            sell_num_match = re.search(r">(\d+)</span>\s个出售中", api_data.get("sell_order_summary", ""))

            # Fix: Extract AppID from the URL path, not a query parameter.
            appid_match = re.search(r'listings/(\d+)/', market_url)
            market_hash_name_match = re.search(r'listings/\d+/(.*)', market_url)

            appid = appid_match.group(1) if appid_match else None
            market_hash_name = market_hash_name_match.group(1) if market_hash_name_match else None

            steam_volume = 0
            if appid and market_hash_name:
                steam_volume = self.get_steam_price_overview(appid, market_hash_name)

            steam_sell_price = float(sell_price_match.group(1)) if sell_price_match else 0
            steam_buy_price = float(buy_price_match.group(1)) if buy_price_match else 0
            steam_sell_num = int(sell_num_match.group(1)) if sell_num_match else 0

            processed_item = {
                "name": item["name"],
                "kind": item["game"],
                "buff_id": item["id"],
                "steam_market_id": market_url.replace("https://steamcommunity.com/market/listings/", ""),
                "buff_buy_price": float(item["buy_max_price"]),
                "buff_sell_price": float(item["sell_min_price"]),
                "steam_sell_price": steam_sell_price,
                "steam_buy_price": steam_buy_price,
                "steam_sell_num": steam_sell_num,
                "buff_sell_num": item["sell_num"],
                "steam_volume": steam_volume,
                "update_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            return processed_item

        except requests.exceptions.RequestException as e:
            print(f"获取 Steam 市场页面时发生错误 ({item['name']}): {e}")
            return None
        except (KeyError, IndexError) as e:
            print(f"解析 Steam 市场数据时缺少键值 ({item['name']}): {e}")
            return None


def run_crawler(config):
    global is_crawling, crawled_data
    is_crawling = True

    print(f"Starting crawler with config: {config}")

    scraper = ScraperService(config)
    page_num = 1
    total_pages = 10

    while is_crawling:
        try:
            items, total_pages_from_buff = scraper.get_buff_items(page_num)
            if total_pages_from_buff > 0:
                total_pages = total_pages_from_buff

            if not items:
                print("本页没有找到物品。正在从第一页重新开始。")
                page_num = 1
                time.sleep(5)
                continue

            for item in items:
                if not is_crawling:
                    break

                if item["sell_num"] >= int(config["min_sell_count"]):
                    processed_item = scraper.get_steam_data(item)

                    # New filtering logic: Only save data if Steam volume is > 0 AND sell ratio < 0.85
                    if processed_item and processed_item['steam_volume'] > 0 and processed_item['steam_sell_price'] > 0:
                        sell_ratio = processed_item['buff_sell_price'] / (processed_item['steam_sell_price'] * 0.85)
                        if sell_ratio < 0.88:
                            with db_lock:
                                found = False
                                for i, existing_item in enumerate(crawled_data):
                                    if existing_item['buff_id'] == processed_item['buff_id']:
                                        crawled_data[i] = processed_item
                                        found = True
                                        break
                                if not found:
                                    crawled_data.append(processed_item)
                                    # 扩大数据存储上限
                                    if len(crawled_data) > 1000:
                                        crawled_data.pop(0)

                            save_data_to_file()

                # Introduce a random delay to avoid rate limits
                # 根据数据条数动态调整反爬策略
                sleep_time = random.uniform(2, 5)
                if len(crawled_data) > 200:
                    sleep_time = random.uniform(5, 10)
                time.sleep(sleep_time)

            page_num += 1
            if page_num > total_pages:
                print("已达到页面上限，正在从第一页重新开始。")
                page_num = 1

            if is_crawling:
                print(f"Processing next page from Buff after 10-second break...")
                time.sleep(10)

        except Exception as e:
            print(f"发生一个未预料的错误: {e}")
            time.sleep(10)

    is_crawling = False
    print("爬虫已停止。")


def test_connection(url, cookies=None, proxy=None):
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        # 使用 verify=False 禁用 SSL 验证
        response = requests.get(url, timeout=10, cookies=cookies, proxies=proxies, verify=False)
        response.raise_for_status()
        return True, "连接成功"
    except requests.exceptions.RequestException as e:
        return False, str(e)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/start_crawler', methods=['POST'])
def start_crawler():
    global crawler_thread, is_crawling
    if is_crawling:
        return jsonify({"message": "爬虫已在运行中。", "status": "running"}), 409

    config = request.json
    crawler_thread = threading.Thread(target=run_crawler, args=(config,))
    crawler_thread.start()

    return jsonify({"message": "爬虫已启动。", "status": "running"})


@app.route('/api/stop_crawler', methods=['POST'])
def stop_crawler():
    global is_crawling
    if not is_crawling:
        return jsonify({"message": "爬虫未在运行中。", "status": "stopped"}), 409

    is_crawling = False
    return jsonify({"message": "爬虫即将停止。", "status": "stopped"})


@app.route('/api/status')
def get_status():
    global is_crawling
    return jsonify({"status": "running" if is_crawling else "stopped"})


@app.route('/api/data')
def get_data():
    global crawled_data
    with db_lock:
        return jsonify({"data": crawled_data})


@app.route('/api/clear_data', methods=['POST'])
def clear_data():
    global crawled_data
    with db_lock:
        try:
            if os.path.exists(DATA_FILE):
                os.remove(DATA_FILE)
                print(f"Successfully removed {DATA_FILE}")
            crawled_data = []
            return jsonify({"message": "数据已成功清空。"}), 200
        except OSError as e:
            print(f"Error removing file {DATA_FILE}: {e}")
            return jsonify({"message": f"清空数据失败: {e}"}), 500


if __name__ == '__main__':
    load_data_from_file()
    app.run(debug=True)