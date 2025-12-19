from flask import Flask, render_template, request, jsonify, session, redirect, url_for, g
from typing import Optional, Dict, Any
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
from database import db
# 忽略 SSL 警告
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or 'dev-secret-key-9527'

DATA_DIR = 'data'
USER_DB = os.path.join(DATA_DIR, 'users.db')

# 爬虫全局状态
crawler_state: Dict[str, Any] = {
    "is_crawling": False,
    "thread": None,
}


# ===================== 用户权限管理 =====================

def init_user_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(USER_DB)
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        is_admin BOOLEAN DEFAULT 0
    )''')
    # 默认账号: admin / admin123
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
            # 关键：如果是接口请求，返回 JSON 而不是跳转页面
            if request.path.startswith('/api/'):
                return jsonify({"status": "error", "message": "Unauthorized: 登录已失效"}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


# ===================== 爬虫逻辑封装 =====================

class ScraperService:
    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        self.session.verify = False  # 保留关闭 SSL 验证
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

        # 处理代理
        proxy = self.config.get("proxy_url")
        if proxy and proxy.strip():
            if not proxy.startswith(('http', 'socks')): proxy = 'http://' + proxy
            self.session.proxies = {"http": proxy, "https": proxy}

    def fetch_steam_price(self, hash_name):
        """适配前端 game_type 的 Steam 价格获取"""
        # CSGO AppID: 730, Dota2 AppID: 570
        appid = "730" if self.config.get("game_type") == "CSGO" else "570"
        url = f"https://steamcommunity.com/market/priceoverview/?appid={appid}&currency=23&market_hash_name={hash_name}"

        # 解析 Cookie 字符串
        cookies = {}
        c_str = self.config.get("steam_cookies", "")
        for pair in re.finditer(r'([^=;]+)=([^;]*)', c_str):
            cookies[pair.group(1).strip()] = pair.group(2).strip()

        try:
            resp = self.session.get(url, cookies=cookies, timeout=10)
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
    """主爬虫线程"""
    scraper = ScraperService(config)
    page = 1

    try:
        while crawler_state["is_crawling"]:
            buff_params = {
                "game": config.get("game_type", "csgo").lower(),
                "page_num": page,
                "min_price": config.get("min_sell_price"),
                "max_price": config.get("max_sell_price"),
                "use_sort": "1"
            }
            buff_cookies = {"session": config.get("buff_session")}

            try:
                resp = scraper.session.get("https://buff.163.com/api/market/goods",
                                           params=buff_params, cookies=buff_cookies, timeout=10)
                res_data = resp.json()
                items = res_data.get("data", {}).get("items", [])
                total_page = res_data.get("data", {}).get("total_page", 1)
            except:
                time.sleep(10)
                continue

            batch = []
            for item in items:
                if not crawler_state["is_crawling"]: break

                # 过滤：在售数量
                if item["sell_num"] >= int(config.get("min_sell_count", 0)):
                    s_price, s_vol = scraper.fetch_steam_price(item["market_hash_name"])

                    if s_price and s_vol > 10:
                        b_price = float(item["sell_min_price"])
                        profit = (s_price * 0.85) - b_price

                        # 构造存入数据库的格式 (适配 database.py 的 save_items)
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
                # 随机延迟，对应前端的“多线程”模拟（实际上 Steam 必须单线程，否则必封）
                time.sleep(random.uniform(2, 5))

            if batch:
                db.save_items(batch)

            page = page + 1 if page < total_page else 1
            time.sleep(5)
    finally:
        crawler_state["is_crawling"] = False


# ===================== 路由接口 =====================

@app.route('/')
@login_required
def index():
    return render_template('index.html', user=session.get('username'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username, password = request.form.get('username'), request.form.get('password')
        conn = sqlite3.connect(USER_DB)
        conn.row_factory = sqlite3.Row
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            session.update({"user_id": user['id'], "username": user['username'], "is_admin": user['is_admin']})
            return redirect(url_for('index'))
        return "登录失败", 401
    return render_template('login.html')

@app.route('/api/status')
@login_required
def get_status():
    """补全被删除的状态接口，供前端轮询查询"""
    return jsonify({
        "status": "running" if crawler_state["is_crawling"] else "stopped",
        "is_crawling": crawler_state["is_crawling"]
    })


@app.route('/api/start_crawler', methods=['POST'])
@login_required
def start_crawler():
    if crawler_state["is_crawling"]:
        return jsonify({"status": "error", "message": "爬虫已在运行中"}), 400

    # 接收前端配置
    config = request.json
    if not config:
        return jsonify({"status": "error", "message": "无效的配置参数"}), 400

    crawler_state["is_crawling"] = True
    # 启动异步线程
    crawler_state["thread"] = threading.Thread(
        target=crawler_worker,
        args=(config,),
        daemon=True
    )
    crawler_state["thread"].start()

    return jsonify({"status": "success", "message": "爬虫指令发送成功"})


@app.route('/api/stop_crawler', methods=['POST'])
@login_required
def stop():
    crawler_state["is_crawling"] = False
    return jsonify({"message": "Stopped"})

@app.route('/api/clear_data', methods=['POST'])
@login_required
def clear_data():
    if not session.get('is_admin'):
        return jsonify({"error": "需要管理员权限"}), 403
    try:
        # 使用 database.py 实例清空数据（假设你在 ItemDatabase 里实现了清空）
        # 如果没有实现，可以手动操作：
        conn = sqlite3.connect(db.db_path)
        conn.execute("DELETE FROM items")
        conn.commit()
        conn.close()
        return jsonify({"message": "数据已成功清空"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/data')
@login_required
def get_data():
    return jsonify({"data": db.load_items()})


if __name__ == '__main__':
    init_user_db()
    app.run(host='0.0.0.0', port=5000)