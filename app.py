from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_cors import CORS
import threading
import time
import requests
import json
import re
from datetime import datetime, timedelta
import os
import random
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from contextlib import contextmanager
import urllib3
from pathlib import Path
from urllib.parse import unquote, urlparse

# 假设 database.py 存在并处理 items 表
try:
    from database import db
except ImportError:
    # 简单的 mock，防止代码直接运行报错，实际使用请确保 database.py 存在
    class MockDB:
        def load_items(self): return []

        def save_items(self, items): pass

        def get_count(self): return 0

        @property
        def db_path(self): return os.path.join('data', 'items.db')


    db = MockDB()

# 禁用 SSL 警告（因为使用了 verify=False）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or 'your-secret-key-here-change-in-production'


def _static_asset_version(filename):
    try:
        asset_path = Path(app.static_folder) / filename
        return str(int(asset_path.stat().st_mtime))
    except OSError:
        return "0"


@app.context_processor
def inject_asset_helpers():
    def static_asset(filename):
        return url_for('static', filename=filename, v=_static_asset_version(filename))

    return {"static_asset": static_asset}

# 在模块加载时立即初始化数据库（确保无论如何启动都会执行）
# 注意：init_db() 函数在后面定义，所以这个调用需要延迟到所有函数定义之后
_db_initialized = False

def ensure_db_initialized():
    """确保数据库已初始化（线程安全）"""
    global _db_initialized
    if not _db_initialized:
        try:
            init_db()
            _db_initialized = True
        except Exception as e:
            print(f"数据库初始化警告: {e}")

# Session 配置
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)  # session 有效期 24 小时
app.config['SESSION_COOKIE_SECURE'] = False  # HTTP 环境设为 False，HTTPS 环境设为 True
app.config['SESSION_COOKIE_HTTPONLY'] = True  # 防止 XSS 攻击
app.config['SESSION_COOKIE_SAMESITE'] = 'Strict'  # 防止 CSRF 攻击，Strict 禁止所有跨站请求
app.config['SESSION_REFRESH_EACH_REQUEST'] = True  # 每次请求都刷新 session 过期时间

CORS(app, supports_credentials=True)

DATA_DIR = 'data'
DB_FILE = os.path.join(DATA_DIR, 'users.db')
STALE_DATA_WINDOW_DAYS = 15

MARKET_ITEM_QUERY = '''
    SELECT name, kind, buff_id, steam_market_id, buff_buy_price, buff_sell_price,
           steam_sell_price, steam_buy_price, steam_sell_num, buff_sell_num,
           steam_volume, update_time
    FROM market_items
    ORDER BY update_time DESC
'''

# 全局状态控制
crawled_data = []
is_crawling = False
crawler_thread = None
db_lock = threading.Lock()


# ===================== 数据库辅助函数 =====================

@contextmanager
def get_db_connection():
    """数据库连接上下文管理器，自动处理关闭"""
    conn = sqlite3.connect(DB_FILE)
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """Initialize SQLite database and create default users"""
    os.makedirs(DATA_DIR, exist_ok=True)

    with get_db_connection() as conn:
        cursor = conn.cursor()

        # 创建用户表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_admin BOOLEAN DEFAULT 0,
            is_active BOOLEAN DEFAULT 1
        )
        ''')

        # 创建配置表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_config (
            id INTEGER PRIMARY KEY,
            setting_key TEXT UNIQUE NOT NULL,
            setting_value TEXT,
            description TEXT
        )
        ''')

        # 插入默认管理员
        cursor.execute("SELECT id FROM users WHERE username = ?", ('admin',))
        if not cursor.fetchone():
            password_hash = generate_password_hash('admin123')
            cursor.execute(
                "INSERT INTO users (username, password_hash, is_admin, is_active) VALUES (?, ?, 1, 1)",
                ('admin', password_hash)
            )
            print("创建默认管理员用户: admin / admin123")

        # 创建市场物品数据表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS market_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            buff_id INTEGER UNIQUE NOT NULL,
            steam_market_id TEXT NOT NULL,
            buff_buy_price REAL,
            buff_sell_price REAL,
            steam_sell_price REAL,
            steam_buy_price REAL,
            steam_sell_num INTEGER,
            buff_sell_num INTEGER,
            steam_volume INTEGER,
            update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        # 创建索引以提高查询性能
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_buff_id ON market_items(buff_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_update_time ON market_items(update_time)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_kind ON market_items(kind)')

        conn.commit()
    
    print("数据库初始化完成")


def build_market_items(rows):
    items = []
    for row in rows:
        items.append({
            'name': row[0],
            'kind': row[1],
            'buff_id': row[2],
            'steam_market_id': row[3],
            'buff_buy_price': row[4],
            'buff_sell_price': row[5],
            'steam_sell_price': row[6],
            'steam_buy_price': row[7],
            'steam_sell_num': row[8],
            'buff_sell_num': row[9],
            'steam_volume': row[10],
            'update_time': row[11]
        })
    return items


def fetch_market_items(conn):
    cursor = conn.cursor()
    cursor.execute(MARKET_ITEM_QUERY)
    return build_market_items(cursor.fetchall())


# ===================== 装饰器 =====================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)

    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))

        # 优先使用 session 中的缓存状态，减少数据库查询
        if session.get('is_admin'):
            return f(*args, **kwargs)

        # 如果 session 中没有，再查库（双重保险）
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT is_admin FROM users WHERE id = ?", (session['user_id'],))
            user = cursor.fetchone()

        if not user or not user[0]:
            return jsonify({"error": "需要管理员权限"}), 403

        session['is_admin'] = True  # 更新缓存
        return f(*args, **kwargs)

    return decorated_function


def load_data_from_db():
    """Load crawled data from SQLite database."""
    global crawled_data
    try:
        with get_db_connection() as conn:
            crawled_data = fetch_market_items(conn)
        print(f"从数据库加载了 {len(crawled_data)} 条数据")
    except Exception as e:
        print(f"从数据库加载数据时出错: {e}")
        crawled_data = []


def save_item_to_db(item):
    """Save or update a single item to SQLite database."""
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO market_items 
                (name, kind, buff_id, steam_market_id, buff_buy_price, buff_sell_price,
                 steam_sell_price, steam_buy_price, steam_sell_num, buff_sell_num,
                 steam_volume, update_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                item['name'],
                item['kind'],
                item['buff_id'],
                item['steam_market_id'],
                item['buff_buy_price'],
                item['buff_sell_price'],
                item['steam_sell_price'],
                item['steam_buy_price'],
                item['steam_sell_num'],
                item['buff_sell_num'],
                item['steam_volume'],
                item['update_time']
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"保存数据到数据库时出错: {e}")


def prune_stale_market_data(max_age_days=STALE_DATA_WINDOW_DAYS):
    """删除距离最新抓取日期过旧的残留数据，避免停爬后旧结果长期污染列表。"""
    global crawled_data

    deleted_count = 0
    latest_date_str = None
    cutoff_date_str = None

    with db_lock:
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT MAX(date(update_time)) FROM market_items")
                row = cursor.fetchone()
                latest_date_str = row[0] if row else None

                if not latest_date_str:
                    return {"deleted": 0, "latest_date": None, "cutoff_date": None}

                latest_date = datetime.strptime(latest_date_str, "%Y-%m-%d").date()
                cutoff_date = latest_date - timedelta(days=max_age_days)
                cutoff_date_str = cutoff_date.isoformat()

                cursor.execute(
                    "DELETE FROM market_items WHERE date(update_time) < ?",
                    (cutoff_date_str,),
                )
                deleted_count = max(cursor.rowcount or 0, 0)
                conn.commit()

                crawled_data = fetch_market_items(conn)
        except Exception as e:
            print(f"清理过期市场数据时出错: {e}")
            return {"deleted": 0, "latest_date": latest_date_str, "cutoff_date": cutoff_date_str}

    if deleted_count:
        print(f"已清理 {deleted_count} 条 {cutoff_date_str} 之前的旧数据")

    return {"deleted": deleted_count, "latest_date": latest_date_str, "cutoff_date": cutoff_date_str}


def parse_cookies_string(cookie_string):
    """解析 cookie 字符串为字典"""
    if not cookie_string:
        return {}
    cookies = {}
    for pair in re.finditer(r'([^=;]+)=([^;]*)', cookie_string):
        key = pair.group(1).strip()
        value = pair.group(2).strip()
        cookies[key] = value
    return cookies


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


# ===================== 爬虫服务类 =====================

class ScraperService:
    def __init__(self, config):
        self.config = config
        self.session = requests.Session()

        # 优化：添加更真实的 Headers
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "X-Requested-With": "XMLHttpRequest"
        })

        self._configure_proxy()

    def _configure_proxy(self):
        """配置代理"""
        proxy_url = self.config.get("proxy_url")
        if not proxy_url or not proxy_url.strip():
            print("未配置代理或代理URL为空")
            self.session.proxies = {}
            return

        # 无效代理列表检查 (保留原有逻辑)
        invalid_proxy_urls = [
            "https://store.steampowered.com/", "http://store.steampowered.com/",
            "https://steamcommunity.com/", "http://steamcommunity.com/",
            "https://steamcommunity.com/market/", "http://steamcommunity.com/market/",
            "https://buff.163.com/", "http://buff.163.com/"
        ]

        if proxy_url in invalid_proxy_urls:
            print(f"检测到无效代理URL: {proxy_url}，将不使用代理")
            self.session.proxies = {}
            return

        # 格式化代理 URL
        if not proxy_url.startswith(('http://', 'https://', 'socks5://')):
            proxy_url = 'http://' + proxy_url

        self.session.proxies = {
            "http": proxy_url,
            "https": proxy_url
        }
        self.session.verify = False  # 禁用 SSL 验证以支持某些代理
        print(f"已配置代理: {proxy_url}")

    def get_buff_items(self, page_num):
        params = {
            "game": self.config["game_type"].lower(),
            "min_price": self.config["min_sell_price"],
            "max_price": self.config["max_sell_price"],
            "page_num": page_num,
            "sort_by": "price.asc",  # 通常需要排序
            "_": int(time.time() * 1000)  # 防止缓存
        }
        # 获取 buff_session，注意要检查是否为空字符串
        buff_session = self.config.get("buff_session", "").strip()
        cookies = {"session": buff_session} if buff_session else {}
        
        if not buff_session:
            print("警告: buff_session 未配置或为空，可能导致 Buff API 返回'请先登录'错误")

        try:
            # FIX: 使用 self.session.get 而不是 requests.get，确保代理生效
            response = self.session.get(
                "https://buff.163.com/api/market/goods",
                params=params,
                cookies=cookies,
                timeout=15
            )
            response.raise_for_status()
            data = response.json()

            if data.get("code") == "OK" and "data" in data:
                items = data["data"].get("items", [])
                total_page = data["data"].get("total_page", 0)
                return items, total_page
            else:
                print(f"Buff API 错误: {data.get('error') or '未知错误'}")
                return [], 0

        except Exception as e:
            print(f"获取 Buff 数据时发生错误: {e}")
            return [], 0

    def get_steam_price_overview(self, appid, market_hash_name):
        url = f"https://steamcommunity.com/market/priceoverview/?appid={appid}&market_hash_name={market_hash_name}&currency=23"
        steam_cookies = parse_cookies_string(self.config["steam_cookies"])

        try:
            response = self.session.get(url, cookies=steam_cookies, timeout=15)

            if response.status_code == 429:
                print("Steam API 请求过多 (429)，暂停 60 秒...")
                time.sleep(60)
                return 0

            response.raise_for_status()
            data = response.json()

            if data.get('success'):
                volume_str = re.sub(r"[^\d]", "", str(data.get('volume', '0')))
                return {
                    "success": True,
                    "volume": int(volume_str) if volume_str else 0,
                    "lowest_price": parse_steam_price_text(data.get("lowest_price")),
                    "median_price": parse_steam_price_text(data.get("median_price")),
                }
        except Exception as e:
            print(f"获取 Steam 销量失败 ({market_hash_name}): {e}")
        return {
            "success": False,
            "volume": 0,
            "lowest_price": 0.0,
            "median_price": 0.0,
        }

    def get_steam_data(self, item):
        market_url = item.get("steam_market_url")
        if not market_url:
            return None

        steam_cookies = parse_cookies_string(self.config["steam_cookies"])
        market_ref = extract_steam_market_ref(market_url)
        if not market_ref:
            print(f"无法解析 Steam 市场链接: {item['name']} -> {market_url}")
            return None

        try:
            time.sleep(random.uniform(2, 4))
            overview = self.get_steam_price_overview(market_ref["appid"], market_ref["market_hash_name"])
            steam_sell_price = overview["lowest_price"] or overview["median_price"]
            steam_volume = overview["volume"]
            steam_sell_num = 0
            steam_buy_price = 0.0

            time.sleep(random.uniform(2, 4))
            response = self.session.get(market_url, cookies=steam_cookies, timeout=15)
            response.raise_for_status()
            market_html = response.text

            sell_num_patterns = [
                r'"sell_order_count"\s*:\s*"?(\\?[\d,]+)"?',
                r'"sell_listing_count"\s*:\s*"?(\\?[\d,]+)"?',
                r'"quantity"\s*:\s*"?(\\?[\d,]+)"?',
            ]
            for pattern in sell_num_patterns:
                sell_num_match = re.search(pattern, market_html)
                if sell_num_match:
                    raw_sell_num = re.sub(r"[^\d]", "", sell_num_match.group(1))
                    if raw_sell_num:
                        steam_sell_num = int(raw_sell_num)
                        break

            buy_price_patterns = [
                r'"highest_buy_order"\s*:\s*"?(\\?[\d,.]+)"?',
                r'"buy_order_price"\s*:\s*"?(\\?[\d,.]+)"?',
            ]
            for pattern in buy_price_patterns:
                buy_price_match = re.search(pattern, market_html)
                if buy_price_match:
                    steam_buy_price = parse_steam_price_text(buy_price_match.group(1))
                    if steam_buy_price > 0:
                        break

            if steam_sell_price <= 0:
                print(f"Steam 价格为空，跳过: {item['name']}")
                return None

            return {
                "name": item["name"],
                "kind": item["game"],
                "buff_id": item["id"],
                "steam_market_id": market_ref["steam_market_id"],
                "buff_buy_price": float(item["buy_max_price"]),
                "buff_sell_price": float(item["sell_min_price"]),
                "steam_sell_price": steam_sell_price,
                "steam_buy_price": steam_buy_price,
                "steam_sell_num": steam_sell_num,
                "buff_sell_num": item["sell_num"],
                "steam_volume": steam_volume,
                "update_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

        except Exception as e:
            print(f"处理 Steam 数据出错 ({item['name']}): {e}")
            return None


def run_crawler(config):
    global is_crawling
    is_crawling = True

    print(f"使用配置启动爬虫: {config}")

    scraper = ScraperService(config)
    page_num = 1
    total_pages = 10
    new_items = []

    try:
        while is_crawling:
            items, total_pages_buff = scraper.get_buff_items(page_num)

            if total_pages_buff > 0:
                total_pages = total_pages_buff

            if not items:
                print(f"页面 {page_num} 无物品，稍后重试...")
                time.sleep(random.uniform(3, 5))
                # 如果连续失败，可以考虑重置到第1页或停止
                if page_num > 1:
                    page_num = 1
                continue
            
            print(f"页面 {page_num} 获取到 {len(items)} 个物品")
            for item in items:
                if not is_crawling:
                    break

                item_name = item.get('name', '未知')
                sell_num = item.get("sell_num", 0)
                
                if sell_num >= int(config["min_sell_count"]):
                    print(f"  处理: {item_name} (销量: {sell_num})")
                    # 调用 get_steam_data 获取 Steam 市场数据
                    processed_item = scraper.get_steam_data(item)

                    if processed_item:
                        if processed_item['steam_volume'] > 0 and processed_item['steam_sell_price'] > 0:
                            sell_ratio = processed_item['buff_sell_price'] / (processed_item['steam_sell_price'] * 0.85)
                            print(f"    比率: {sell_ratio:.2f} (Buff:¥{processed_item['buff_sell_price']} Steam:¥{processed_item['steam_sell_price']})")
                            if sell_ratio < 0.88:
                                print(f"    ✓ 保存到数据库")
                                save_item_to_db(processed_item)
                            with db_lock:
                                found = False
                                for i, existing_item in enumerate(crawled_data):
                                    if existing_item['buff_id'] == processed_item['buff_id']:
                                        crawled_data[i] = processed_item
                                        found = True
                                        break
                                if not found:
                                    crawled_data.append(processed_item)
                                    if len(crawled_data) > 1000:
                                        crawled_data.pop(0)

                sleep_time = random.uniform(2, 5)
                if len(crawled_data) > 200:
                    sleep_time = random.uniform(5, 10)
                time.sleep(sleep_time)

            print(f"页面 {page_num} 处理完成，当前数据库共 {len(crawled_data)} 条数据")
            page_num += 1
            if page_num > total_pages:
                print("已遍历所有页面，重新开始...")
                page_num = 1
                time.sleep(10)

            if is_crawling:
                print(f"准备抓取第 {page_num} 页...")
                time.sleep(random.uniform(5, 10))

    except Exception as e:
        print(f"爬虫线程发生未捕获异常: {e}")
        import traceback
        traceback.print_exc()
    finally:
        is_crawling = False
        prune_result = prune_stale_market_data()
        if prune_result["deleted"]:
            print(
                f"停止抓取后已清理 {prune_result['deleted']} 条旧数据，"
                f"保留 {prune_result['cutoff_date']} 及之后的记录。"
            )
        print("爬虫线程已结束。")


def test_connection(url, cookies=None, proxy=None):
    """测试连接工具函数"""
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.0.0 Safari/537.36"
        }
        response = requests.get(
            url,
            timeout=10,
            cookies=cookies,
            proxies=proxies,
            headers=headers,
            verify=False
        )
        response.raise_for_status()
        return True, f"连接成功 (HTTP {response.status_code})"
    except Exception as e:
        return False, str(e)


# ===================== API 路由 =====================

@app.route('/api/test_connection', methods=['POST'])
def api_test_connection():
    try:
        config = request.get_json()
        proxy_url = config.get('proxy_url')
        steam_cookies_str = config.get('steam_cookies')
        buff_session = config.get('buff_session')

        steam_cookies = parse_cookies_string(steam_cookies_str)
        buff_cookies = {'session': buff_session} if buff_session else {}

        # 过滤无效代理逻辑
        invalid_list = ["https://store.steampowered.com/", "http://store.steampowered.com/"]  # 简化的列表
        effective_proxy = proxy_url if proxy_url and proxy_url not in invalid_list else None

        # 修正：确保代理带有 scheme
        if effective_proxy and not effective_proxy.startswith(('http', 'socks')):
            effective_proxy = f"http://{effective_proxy}"

        steam_success, steam_msg = test_connection(
            "https://steamcommunity.com/market/",
            cookies=steam_cookies,
            proxy=effective_proxy
        )

        buff_success, buff_msg = test_connection(
            "https://buff.163.com",
            cookies=buff_cookies,
            proxy=effective_proxy
        )

        return jsonify({
            'buff': {'success': buff_success, 'message': buff_msg},
            'steam': {'success': steam_success, 'message': steam_msg}
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ===================== 用户管理路由 =====================

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, password_hash, is_active, is_admin FROM users WHERE username = ?", (username,))
            user = cursor.fetchone()

        if user and user[2] == 1 and check_password_hash(user[1], password):
            session.permanent = True  # 启用永久 session，使用配置的超时时间
            session['user_id'] = user[0]
            session['username'] = username
            session['is_admin'] = bool(user[3])
            return redirect(url_for('index'))
        else:
            error_msg = '账户已被禁用' if (user and user[2] == 0) else '用户名或密码错误'
            return render_template('login.html', error=error_msg)

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/profile')
@login_required
def profile():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT username, created_at, is_admin FROM users WHERE id = ?", (session['user_id'],))
        user = cursor.fetchone()

    return render_template('profile.html',
                           username=user[0],
                           created_at=user[1],
                           is_admin=bool(user[2]))


# ===================== 管理员路由 =====================

@app.route('/admin/users')
@login_required
@admin_required
def admin_users():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, username, created_at, is_admin, is_active FROM users ORDER BY created_at DESC")
        users = cursor.fetchall()

    users_json = [
        {
            "id": user[0],
            "username": user[1],
            "created_at": user[2],
            "is_admin": bool(user[3]),
            "is_active": bool(user[4]),
        }
        for user in users
    ]

    return render_template(
        'admin_users.html',
        users_json=users_json,
        current_user_id=session['user_id']
    )


@app.route('/api/admin/add_user', methods=['POST'])
@login_required
@admin_required
def admin_add_user():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    is_admin = data.get('is_admin', False)

    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            password_hash = generate_password_hash(password)
            cursor.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                (username, password_hash, 1 if is_admin else 0)
            )
            conn.commit()
            user_id = cursor.lastrowid

            # 获取新用户信息用于返回
            cursor.execute("SELECT id, username, created_at, is_admin, is_active FROM users WHERE id = ?", (user_id,))
            user = cursor.fetchone()

        return jsonify({
            "message": "用户添加成功",
            "user": {
                "id": user[0], "username": user[1],
                "created_at": user[2], "is_admin": user[3], "is_active": user[4]
            }
        }), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "用户名已存在"}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/admin/update_user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def admin_update_user(user_id):
    data = request.json
    if user_id == session['user_id'] and 'is_admin' in data:
        return jsonify({"error": "不能修改自己的管理员权限"}), 400

    try:
        update_fields = []
        params = []

        if 'password' in data and data['password']:
            update_fields.append("password_hash = ?")
            params.append(generate_password_hash(data['password']))

        if 'is_admin' in data:
            update_fields.append("is_admin = ?")
            params.append(1 if data['is_admin'] else 0)

        if 'is_active' in data:
            update_fields.append("is_active = ?")
            params.append(1 if data['is_active'] else 0)

        if not update_fields:
            return jsonify({"error": "没有可更新的字段"}), 400

        params.append(user_id)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            query = f"UPDATE users SET {', '.join(update_fields)} WHERE id = ?"
            cursor.execute(query, params)
            conn.commit()

            cursor.execute("SELECT id, username, created_at, is_admin, is_active FROM users WHERE id = ?", (user_id,))
            user = cursor.fetchone()

        return jsonify({
            "message": "用户更新成功",
            "user": {
                "id": user[0], "username": user[1],
                "created_at": user[2], "is_admin": user[3], "is_active": user[4]
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/admin/delete_user/<int:user_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_user(user_id):
    if user_id == session['user_id']:
        return jsonify({"error": "不能删除自己的账户"}), 400

    try:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
        return jsonify({"message": "用户删除成功"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===================== 核心页面路由 =====================

@app.route('/')
@login_required
def index():
    return render_template('index.html',
                           username=session.get('username'),
                           is_admin=session.get('is_admin', False))


@app.route('/api/start_crawler', methods=['POST'])
@login_required
def start_crawler():
    global crawler_thread, is_crawling
    if is_crawling:
        return jsonify({"message": "爬虫已在运行中。", "status": "running"}), 409

    config = request.json
    # 重置状态
    is_crawling = True
    crawler_thread = threading.Thread(target=run_crawler, args=(config,))
    crawler_thread.daemon = True  # 设置为守护线程，主程序退出时自动结束
    crawler_thread.start()

    return jsonify({"message": "爬虫已启动。", "status": "running"})


@app.route('/api/stop_crawler', methods=['POST'])
@login_required
def stop_crawler():
    global is_crawling
    if not is_crawling:
        return jsonify({"message": "爬虫未在运行中。", "status": "stopped"}), 409

    is_crawling = False
    return jsonify({"message": "爬虫停止信号已发送。", "status": "stopped"})


@app.route('/api/status')
@login_required
def get_status():
    return jsonify({"status": "running" if is_crawling else "stopped"})


@app.route('/api/data')
@login_required
def get_data():
    try:
        with db_lock:
            with get_db_connection() as conn:
                items = fetch_market_items(conn)
                cursor = conn.cursor()
                cursor.execute("SELECT MAX(date(update_time)) FROM market_items")
                latest_row = cursor.fetchone()
                latest_date = latest_row[0] if latest_row else None
            
        return jsonify({
            "success": True,
            "data": items,
            "count": len(items),
            "meta": {
                "latest_date": latest_date,
                "stale_window_days": STALE_DATA_WINDOW_DAYS
            }
        })
    except Exception as e:
        print(f"获取数据时出错: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/clear_data', methods=['POST'])
@admin_required
def clear_data():
    global crawled_data
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM market_items')
            conn.commit()
            conn.close()
            crawled_data = []
            print("成功清空数据库中的所有市场物品数据")
            return jsonify({"message": "数据已成功清空。"}), 200
        except Exception as e:
            print(f"清空数据库时出错: {e}")
            return jsonify({"message": f"清空数据失败: {e}"}), 500


# 在模块加载时立即初始化数据库（无论如何启动都会执行）
ensure_db_initialized()

if __name__ == '__main__':
    # 加载爬虫数据
    os.makedirs('data', exist_ok=True)
    load_data_from_db()

    # 设置安全的密钥（生产环境请使用环境变量）
    app.secret_key = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-me'

    app.run(host='0.0.0.0', port=5000, debug=False)
