from flask import Flask, render_template, request, jsonify, session, redirect, url_for, g
import threading
import time
import requests
import json
import re
import os
import random
import sqlite3
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

# 导入 database.py 中的 db 实例
try:
    from database import db
except ImportError:
    db = None

# 忽略 SSL 警告 (保留关闭验证的逻辑)
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or 'dev-secret-key-9527'

DATA_DIR = 'data'
USER_DB = os.path.join(DATA_DIR, 'users.db')


# 全局状态管理
class CrawlerState:
    def __init__(self):
        self.is_crawling = False
        self.thread = None


state = CrawlerState()


# ===================== 权限校验与初始化 =====================

def init_user_db():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    conn = sqlite3.connect(USER_DB)
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        is_admin BOOLEAN DEFAULT 0
    )''')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username='admin'")
    if not cursor.fetchone():
        cursor.execute("INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 1)",
                       ('admin', generate_password_hash('admin123')))
    conn.commit()
    conn.close()


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            if request.path.startswith('/api/'):
                return jsonify({"status": "error", "message": "登录过期"}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)

    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_admin'):
            return "权限不足", 403
        return f(*args, **kwargs)

    return decorated_function


# ===================== 核心页面路由 =====================

@app.route('/')
@login_required
def index():
    return render_template('index.html', user=session.get('username'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        conn = sqlite3.connect(USER_DB)
        conn.row_factory = sqlite3.Row
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            session.update({"user_id": user['id'], "username": user['username'], "is_admin": user['is_admin']})
            return redirect(url_for('index'))
        return "用户名或密码错误", 401
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/profile')
@login_required
def profile():
    return render_template('profile.html', user=session.get('username'))


@app.route('/admin/users')
@login_required
@admin_required
def admin_users():
    conn = sqlite3.connect(USER_DB)
    conn.row_factory = sqlite3.Row
    users = conn.execute("SELECT id, username, is_admin FROM users").fetchall()
    conn.close()
    return render_template('admin_users.html', users=users)


# ===================== 爬虫逻辑封装 =====================

class ScraperService:
    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        self.session.verify = False
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://buff.163.com/market/goods?game=csgo",  # 必须加这个
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest"
        }

        # # 代理处理
        # proxy = self.config.get("proxy_url")
        # if proxy and proxy.strip():
        #     if not proxy.startswith(('http', 'socks')): proxy = 'http://' + proxy
        #     self.session.proxies = {"http": proxy, "https": proxy}

    def fetch_steam_price(self, hash_name):
        appid = "730" if self.config.get("game_type") == "CSGO" else "570"
        url = f"https://steamcommunity.com/market/priceoverview/?appid={appid}&currency=23&market_hash_name={hash_name}"

        cookies = {}
        c_str = self.config.get("steam_cookies", "")
        for pair in re.finditer(r'([^=;]+)=([^;]*)', c_str):
            cookies[pair.group(1).strip()] = pair.group(2).strip()

        try:
            resp = self.session.get(url, cookies=cookies, headers=self.headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    price = float(re.sub(r'[^\d.]', '', data.get("lowest_price", "0")))
                    volume = int(data.get("volume", "0").replace(",", ""))
                    return price, volume
        except:
            pass
        return None, 0


def crawler_worker(config):
    scraper = ScraperService(config)
    page = 1
    print(
        f"[{datetime.now()}] >>> 爬虫线程启动配置: Game={config.get('game_type')}, MinPrice={config.get('min_sell_price')}",
        flush=True)

    try:
        while state.is_crawling:
            buff_params = {
                "game": config.get("game_type", "csgo").lower(),
                "page_num": page,
                "min_price": config.get("min_sell_price"),
                "max_price": config.get("max_sell_price"),
                "use_sort": "1",
                "trigger": "search"  # 补充缺失的参数
            }
            raw_cookie = config.get("buff_session", "")
            buff_cookies = {}
            if ";" in raw_cookie:
                # 如果用户粘贴了完整的 Cookie 字符串，自动解析
                for item in raw_cookie.split(';'):
                    if '=' in item:
                        k, v = item.strip().split('=', 1)
                        buff_cookies[k] = v
            else:
                # 兼容只填入 sessionid 的情况
                buff_cookies = {"session": raw_cookie}
            try:
                print(f"[{datetime.now()}] 正在请求 Buff API 第 {page} 页...", flush=True)
                resp = scraper.session.get("https://buff.163.com/api/market/goods",
                                           params=buff_params, cookies=buff_cookies, timeout=15)

                if resp.status_code != 200:
                    print(f"[{datetime.now()}] Buff 接口返回状态码异常: {resp.status_code}", flush=True)
                    time.sleep(10)
                    page += 1  # 失败也尝试下一页，避免死循环
                    continue

                res_data = resp.json()
                if res_data.get("code") != "OK":
                    print(f"[{datetime.now()}] Buff 业务报错: {res_data.get('error', '未知错误')}", flush=True)
                    # 如果是登录失效，直接停止爬虫
                    if "Login" in res_data.get('error', ''):
                        state.is_crawling = False
                        break
                    time.sleep(10)
                    page += 1
                    continue

                items = res_data.get("data", {}).get("items", [])
                total_page = res_data.get("data", {}).get("total_page", 1)
                print(f"[{datetime.now()}] 第 {page} 页获取成功，包含 {len(items)} 个物品", flush=True)

            except Exception as e:
                print(f"[{datetime.now()}] 请求过程发生网络异常: {str(e)}", flush=True)
                time.sleep(10)
                page += 1
                continue

            batch = []
            for item in items:
                if not state.is_crawling: break

                # 筛选逻辑：上架数量必须大于设定值
                min_count = int(config.get("min_sell_count") or 0)
                if item.get("sell_num", 0) >= min_count:
                    print(f"   - 正在查询 Steam 价格: {item['name']}", flush=True)
                    s_price, s_vol = scraper.fetch_steam_price(item["market_hash_name"])

                    if s_price and s_vol > 0:
                        b_price = float(item["sell_min_price"])
                        # 利润 = Steam到手价(15%手续费) - Buff买入价
                        profit = (s_price * 0.85) - b_price

                        batch.append({
                            "name": item["name"],
                            "buff_price": b_price,
                            "steam_price": s_price,
                            "profit": round(profit, 2),
                            "profit_rate": round((profit / b_price) * 100, 2) if b_price > 0 else 0,
                            "steam_volume": s_vol,
                            "buff_url": f"https://buff.163.com/goods/{item['id']}",
                            "steam_market_url": f"https://steamcommunity.com/market/listings/730/{item['market_hash_name']}"
                        })
                    # 避免频繁请求 Steam 被封 IP
                    time.sleep(random.uniform(3, 6))

            if batch and db:
                db.save_items(batch)
                print(f"[{datetime.now()}] 已保存 {len(batch)} 个套利项到数据库", flush=True)

            page = page + 1 if page < total_page else 1
            print(f"[{datetime.now()}] 准备进入下一页，休息中...", flush=True)
            time.sleep(5)

    except Exception as e:
        print(f"[{datetime.now()}] 线程崩溃: {str(e)}", flush=True)
    finally:
        state.is_crawling = False
        print(f"[{datetime.now()}] <<< 爬虫线程已安全退出", flush=True)

# ===================== API 接口 =====================

@app.route('/api/status')
@login_required
def get_status():
    return jsonify({
        "status": "running" if state.is_crawling else "stopped",
        "is_crawling": state.is_crawling
    })


@app.route('/api/start_crawler', methods=['POST'])
@login_required
def start_crawler():
    if state.is_crawling: return jsonify({"status": "running"})
    config = request.json
    state.is_crawling = True
    state.thread = threading.Thread(target=crawler_worker, args=(config,), daemon=True)
    state.thread.start()
    return jsonify({"status": "success", "message": "爬虫已启动"})


@app.route('/api/stop_crawler', methods=['POST'])
@login_required
def stop_crawler():
    state.is_crawling = False
    return jsonify({"status": "success"})


@app.route('/api/data')
@login_required
def get_data():
    if db:
        items = db.load_items()
        return jsonify({"success": True, "data": items})
    return jsonify({"success": False, "data": []})


@app.route('/api/clear_data', methods=['POST'])
@login_required
@admin_required
def clear_data():
    try:
        conn = sqlite3.connect(db.db_path)
        conn.execute("DELETE FROM items")
        conn.commit()
        conn.close()
        return jsonify({"message": "数据已清空"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    init_user_db()
    app.run(host='0.0.0.0', port=5000)