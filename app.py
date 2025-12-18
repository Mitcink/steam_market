from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import threading
import time
import requests
import json
import re
from datetime import datetime
import os
import random
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from database import db

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or 'your-secret-key-here-change-in-production'

DATA_DIR = 'data'
# 移除这些旧的JSON相关变量
# DATA_FILE = os.path.join(DATA_DIR, 'crawled_data.json')
# crawled_data = []
# db_lock = threading.Lock()

DB_FILE = os.path.join(DATA_DIR, 'users.db')
is_crawling = False
crawler_thread = None


# 初始化数据库和创建默认用户
def init_db():
    """Initialize SQLite database and create default users"""
    os.makedirs(DATA_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_FILE)
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

    # 创建配置表，用于存储系统配置
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS system_config (
        id INTEGER PRIMARY KEY,
        setting_key TEXT UNIQUE NOT NULL,
        setting_value TEXT,
        description TEXT
    )
    ''')

    # 插入默认管理员用户（密码：admin123）
    cursor.execute("SELECT * FROM users WHERE username = ?", ('admin',))
    if not cursor.fetchone():
        password_hash = generate_password_hash('admin123')
        cursor.execute(
            "INSERT INTO users (username, password_hash, is_admin, is_active) VALUES (?, ?, 1, 1)",
            ('admin', password_hash)
        )
        print("创建默认管理员用户: admin / admin123")

    # 插入默认普通用户（密码：user123）
    cursor.execute("SELECT * FROM users WHERE username = ?", ('user',))
    if not cursor.fetchone():
        password_hash = generate_password_hash('user123')
        cursor.execute(
            "INSERT INTO users (username, password_hash, is_admin, is_active) VALUES (?, ?, 0, 1)",
            ('user', password_hash)
        )
        print("创建默认普通用户: user / user123")

    # 插入系统配置
    cursor.execute(
        "INSERT OR IGNORE INTO system_config (setting_key, setting_value, description) VALUES ('allow_login', '1', '是否允许登录')")
    cursor.execute(
        "INSERT OR IGNORE INTO system_config (setting_key, setting_value, description) VALUES ('max_login_attempts', '5', '最大登录尝试次数')")

    conn.commit()
    conn.close()
    print("数据库初始化完成")


# 登录装饰器
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)

    return decorated_function


# 管理员权限装饰器
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT is_admin FROM users WHERE id = ?", (session['user_id'],))
        user = cursor.fetchone()
        conn.close()

        if not user or not user[0]:
            return jsonify({"error": "需要管理员权限"}), 403

        return f(*args, **kwargs)

    return decorated_function


# 替换原来的数据加载和保存函数
def load_data_from_file():
    """兼容性函数 - 现在从数据库加载数据"""
    items = db.load_items()
    print(f"从数据库加载了 {len(items)} 个物品")
    return items


def save_data_to_file():
    """兼容性函数 - 现在数据自动保存到数据库"""
    # 这个函数现在不需要做任何事情，因为数据实时保存到数据库
    pass


def update_crawled_data(new_items):
    """更新爬取的数据到数据库"""
    if new_items:
        # 转换数据格式以匹配数据库结构
        formatted_items = []
        for item in new_items:
            formatted_item = {
                'name': item.get('name'),
                'buff_price': item.get('buff_sell_price'),
                'steam_price': item.get('steam_sell_price'),
                'profit': (item.get('steam_sell_price', 0) * 0.85) - item.get('buff_sell_price', 0),
                'profit_rate': ((item.get('steam_sell_price', 0) * 0.85) - item.get('buff_sell_price', 0)) / item.get(
                    'buff_sell_price', 1) * 100 if item.get('buff_sell_price', 0) > 0 else 0,
                'steam_volume': item.get('steam_volume'),
                'buff_url': f"https://buff.163.com/goods/{item.get('buff_id')}",
                'steam_market_url': f"https://steamcommunity.com/market/listings/{item.get('steam_market_id')}",
                # 保留原始数据
                'buff_id': item.get('buff_id'),
                'steam_market_id': item.get('steam_market_id'),
                'buff_buy_price': item.get('buff_buy_price'),
                'steam_buy_price': item.get('steam_buy_price'),
                'steam_sell_num': item.get('steam_sell_num'),
                'buff_sell_num': item.get('buff_sell_num'),
                'update_time': item.get('update_time')
            }
            formatted_items.append(formatted_item)

        # 保存到数据库
        db.save_items(formatted_items)
        print(f"成功更新 {len(formatted_items)} 个物品到数据库")


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

        # 修复代理配置逻辑
        proxy_url = self.config.get("proxy_url")
        if proxy_url and proxy_url.strip():
            # 定义无效的代理URL列表
            invalid_proxy_urls = [
                "https://store.steampowered.com/",
                "http://store.steampowered.com/",
                "https://steamcommunity.com/",
                "http://steamcommunity.com/",
                "https://steamcommunity.com/market/",
                "http://steamcommunity.com/market/",
                "https://buff.163.com/",
                "http://buff.163.com/"
            ]

            # 检查是否是有效的代理URL
            if proxy_url not in invalid_proxy_urls:
                # 确保代理URL格式正确
                if not proxy_url.startswith(('http://', 'https://', 'socks5://')):
                    proxy_url = 'http://' + proxy_url

                self.session.proxies = {
                    "http": proxy_url,
                    "https": proxy_url
                }
                self.session.verify = False
                print(f"已配置代理: {proxy_url}")
            else:
                print(f"检测到无效代理URL: {proxy_url}，将不使用代理")
                self.session.proxies = {}
        else:
            print("未配置代理或代理URL为空")
            self.session.proxies = {}

    def get_buff_items(self, page_num):
        params = {
            "game": self.config["game_type"].lower(),
            "min_price": self.config["min_sell_price"],
            "max_price": self.config["max_sell_price"],
            "page_num": page_num,
        }
        cookies = {"session": self.config["buff_session"]}

        try:
            # 直接使用requests.get而不是session
            response = requests.get(
                "https://buff.163.com/api/market/goods",
                params=params,
                cookies=cookies,
                timeout=10,
                verify=False
            )
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
            # 使用requests.get而不是self.session.get
            response = requests.get(url, cookies=steam_cookies_dict, timeout=10, verify=False)
            response.raise_for_status()
            data = response.json()

            print(f"Raw Steam priceoverview data for {market_hash_name}: {data}")

            if data.get('success'):
                volume_str = data.get('volume', '0').replace(',', '')
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

            try:
                # 使用requests.get而不是self.session.get
                response = requests.get(market_url, cookies=steam_cookies_dict, timeout=10, verify=False)
                response.raise_for_status()
            except requests.exceptions.HTTPError as e:
                print(
                    f"获取 Steam 市场页面时发生 HTTP 错误 ({item['name']}): {e.response.status_code} - {e.response.reason}")
                return None
            except requests.exceptions.RequestException as e:
                print(f"获取 Steam 市场页面时发生网络错误 ({item['name']}): {e}")
                return None

            market_html = response.text
            match = re.search(r"Market_LoadOrderSpread$\s(\d+)\s$;", market_html)
            if not match:
                print(f"Market ID not found for {item['name']}. Skipping.")
                return None
            market_id = match.group(1)

            api_url = f"https://steamcommunity.com/market/itemordershistogram?country=CN&language=schinese&currency=23&item_nameid={market_id}"

            try:
                # 使用requests.get而不是self.session.get
                api_response = requests.get(api_url, cookies=steam_cookies_dict, timeout=10, verify=False)
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
    global is_crawling
    is_crawling = True

    print(f"Starting crawler with config: {config}")

    scraper = ScraperService(config)
    page_num = 1
    total_pages = 10
    new_items = []  # 收集新物品

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

                    if processed_item and processed_item['steam_volume'] > 0 and processed_item['steam_sell_price'] > 0:
                        sell_ratio = processed_item['buff_sell_price'] / (processed_item['steam_sell_price'] * 0.85)
                        if sell_ratio < 0.88:
                            new_items.append(processed_item)

                            # 每收集10个物品就批量保存一次
                            if len(new_items) >= 10:
                                update_crawled_data(new_items)
                                new_items = []

                sleep_time = random.uniform(2, 5)
                current_count = db.get_count()
                if current_count > 200:
                    sleep_time = random.uniform(5, 10)
                time.sleep(sleep_time)

            # 保存剩余的物品
            if new_items:
                update_crawled_data(new_items)
                new_items = []

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


# 新的API路由处理函数
@app.route('/api/test_connection', methods=['POST'])
def api_test_connection():
    try:
        # 获取前端发送的配置
        config = request.get_json()
        print(f"收到配置: {config}")

        # 从配置中提取需要的参数
        proxy_url = config.get('proxy_url')
        steam_cookies_str = config.get('steam_cookies')
        buff_session = config.get('buff_session')

        # 处理Steam cookies - 将字符串转换为字典
        steam_cookies = {}
        if steam_cookies_str:
            try:
                # 解析cookie字符串 "key1=value1; key2=value2"
                for cookie in steam_cookies_str.split(';'):
                    if '=' in cookie:
                        key, value = cookie.strip().split('=', 1)
                        steam_cookies[key] = value
            except Exception as e:
                print(f"解析Steam cookies失败: {e}")

        # 处理Buff cookies
        buff_cookies = {}
        if buff_session:
            buff_cookies['session'] = buff_session

        # 测试Steam连接
        steam_success, steam_message = test_connection(
            "https://steamcommunity.com/market/",
            cookies=steam_cookies,
            proxy=proxy_url if proxy_url != "https://store.steampowered.com/" else None
        )

        # 测试Buff连接
        buff_success, buff_message = test_connection(
            "https://buff.163.com",
            cookies=buff_cookies,
            proxy=proxy_url if proxy_url != "https://store.steampowered.com/" else None
        )

        # 返回JSON响应
        return jsonify({
            'buff': {
                'success': buff_success,
                'message': buff_message
            },
            'steam': {
                'success': steam_success,
                'message': steam_message
            }
        })

    except Exception as e:
        print(f"API错误: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ===================== 用户管理路由 =====================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT id, password_hash, is_active FROM users WHERE username = ?", (username,))
        user = cursor.fetchone()
        conn.close()

        if user and user[2] == 1 and check_password_hash(user[1], password):
            session['user_id'] = user[0]
            session['username'] = username

            # 检查是否为管理员
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT is_admin FROM users WHERE id = ?", (user[0],))
            is_admin = cursor.fetchone()[0]
            conn.close()

            session['is_admin'] = bool(is_admin)
            return redirect(url_for('index'))
        else:
            if user and user[2] == 0:
                return render_template('login.html', error='账户已被禁用')
            else:
                return render_template('login.html', error='用户名或密码错误')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/profile')
@login_required
def profile():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT username, created_at, is_admin FROM users WHERE id = ?", (session['user_id'],))
    user = cursor.fetchone()
    conn.close()

    return render_template('profile.html',
                           username=user[0],
                           created_at=user[1],
                           is_admin=user[2])


# ===================== 管理员用户管理路由 =====================
@app.route('/admin/users')
@login_required
@admin_required
def admin_users():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, created_at, is_admin, is_active FROM users ORDER BY created_at DESC")
    users = cursor.fetchall()
    conn.close()

    return render_template('admin_users.html', users=users)


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

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    try:
        password_hash = generate_password_hash(password)
        cursor.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
            (username, password_hash, 1 if is_admin else 0)
        )
        conn.commit()
        user_id = cursor.lastrowid

        cursor.execute("SELECT id, username, created_at, is_admin, is_active FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        conn.close()

        return jsonify({
            "message": "用户添加成功",
            "user": {
                "id": user[0],
                "username": user[1],
                "created_at": user[2],
                "is_admin": user[3],
                "is_active": user[4]
            }
        }), 201
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "用户名已存在"}), 409
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/api/admin/update_user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def admin_update_user(user_id):
    data = request.json

    # 不能修改自己的管理员状态
    if user_id == session['user_id'] and 'is_admin' in data:
        return jsonify({"error": "不能修改自己的管理员权限"}), 400

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    try:
        update_fields = []
        params = []

        if 'password' in data and data['password']:
            password_hash = generate_password_hash(data['password'])
            update_fields.append("password_hash = ?")
            params.append(password_hash)

        if 'is_admin' in data:
            update_fields.append("is_admin = ?")
            params.append(1 if data['is_admin'] else 0)

        if 'is_active' in data:
            update_fields.append("is_active = ?")
            params.append(1 if data['is_active'] else 0)

        if not update_fields:
            conn.close()
            return jsonify({"error": "没有可更新的字段"}), 400

        params.append(user_id)
        query = f"UPDATE users SET {', '.join(update_fields)} WHERE id = ?"
        cursor.execute(query, params)
        conn.commit()

        cursor.execute("SELECT id, username, created_at, is_admin, is_active FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        conn.close()

        return jsonify({
            "message": "用户更新成功",
            "user": {
                "id": user[0],
                "username": user[1],
                "created_at": user[2],
                "is_admin": user[3],
                "is_active": user[4]
            }
        })
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/api/admin/delete_user/<int:user_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_user(user_id):
    # 不能删除自己
    if user_id == session['user_id']:
        return jsonify({"error": "不能删除自己的账户"}), 400

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    try:
        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        conn.close()

        return jsonify({"message": "用户删除成功"})
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500


# ===================== 爬虫相关路由（需要登录） =====================
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
    crawler_thread = threading.Thread(target=run_crawler, args=(config,))
    crawler_thread.start()

    return jsonify({"message": "爬虫已启动。", "status": "running"})


@app.route('/api/stop_crawler', methods=['POST'])
@login_required
def stop_crawler():
    global is_crawling
    if not is_crawling:
        return jsonify({"message": "爬虫未在运行中。", "status": "stopped"}), 409

    is_crawling = False
    return jsonify({"message": "爬虫即将停止。", "status": "stopped"})


@app.route('/api/status')
@login_required
def get_status():
    global is_crawling
    return jsonify({"status": "running" if is_crawling else "stopped"})


# 修改数据API，从数据库获取数据
@app.route('/api/data')
@login_required
def get_data():
    try:
        # 直接从数据库获取最新数据
        items = db.load_items()
        return jsonify({
            "success": True,
            "data": items,
            "count": len(items)
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# 修改清空数据API
@app.route('/api/clear_data', methods=['POST'])
@admin_required
def clear_data():
    try:
        # 清空数据库中的物品数据
        conn = sqlite3.connect(db.db_path)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM items')
        conn.commit()
        conn.close()

        print("数据库中的物品数据已清空")
        return jsonify({"message": "数据已成功清空。"}), 200
    except Exception as e:
        print(f"清空数据失败: {e}")
        return jsonify({"message": f"清空数据失败: {e}"}), 500


if __name__ == '__main__':
    # 初始化数据库
    init_db()

    # 设置安全的密钥（生产环境请使用环境变量）
    app.secret_key = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-me'

    app.run(host='0.0.0.0', port=5000, debug=False)
