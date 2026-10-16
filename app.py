from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_cors import CORS
import threading
import time
import requests
from collections import deque
import json
import re
from datetime import datetime, timedelta, timezone
import os
import sqlite3
import base64
import hashlib
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from contextlib import contextmanager
import urllib3
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
from crawler import MAX_SELL_RATIO, MIN_STEAM_VOLUME, MarketCrawler

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
CREDENTIAL_CONFIG_KEY = "crawler_credentials_v1"
NON_SECRET_CONFIG_KEY = "crawler_settings_v1"
BUFF_BUY_SETTINGS_KEY = "buff_buy_settings_v1"
CREDENTIAL_HEALTH_KEY = "crawler_credentials_health_v1"


def _derive_encryption_key():
    secret = os.environ.get("CREDENTIALS_SECRET") or app.secret_key
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


fernet = Fernet(_derive_encryption_key())

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
crawler_logs = deque(maxlen=300)
crawler_log_lock = threading.Lock()
crawler_log_seq = 0


def append_crawler_log(message):
    """同时打到标准输出和页面上的抓取日志。"""
    global crawler_log_seq
    text = str(message)
    print(text)
    stamped = f"{datetime.now().strftime('%H:%M:%S')} {text.strip()}"
    with crawler_log_lock:
        crawler_log_seq += 1
        crawler_logs.append({"id": crawler_log_seq, "message": stamped})


def snapshot_crawler_logs(after_id=0):
    with crawler_log_lock:
        lines = [entry for entry in crawler_logs if entry["id"] > after_id]
        latest_id = crawler_log_seq
    return lines, latest_id


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

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS purchase_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_platform TEXT NOT NULL,
            source_order_id TEXT,
            buff_goods_id INTEGER,
            item_name TEXT NOT NULL,
            game_type TEXT,
            quantity INTEGER DEFAULT 1,
            unit_price REAL,
            total_price REAL,
            purchased_at TEXT NOT NULL,
            release_at_utc TEXT NOT NULL,
            reminder_sent INTEGER DEFAULT 0,
            status TEXT DEFAULT 'purchased',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_purchase_release_at ON purchase_records(release_at_utc)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_purchase_status ON purchase_records(status)')

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
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS steam_item_nameids (
            steam_market_id TEXT PRIMARY KEY,
            item_nameid TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        ''')
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS steam_quotes (
            steam_market_id TEXT PRIMARY KEY,
            sell_price REAL,
            buy_price REAL,
            sell_num INTEGER,
            volume INTEGER,
            price_fetched_at TEXT,
            listings_fetched_at TEXT
        )
        ''')
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS crawler_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        ''')

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


def _encrypt_value(raw_value):
    if not raw_value:
        return ""
    return fernet.encrypt(raw_value.encode("utf-8")).decode("utf-8")


def _decrypt_value(encrypted_value):
    if not encrypted_value:
        return ""
    try:
        return fernet.decrypt(encrypted_value.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return ""


def get_system_config(setting_key, default=None):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT setting_value FROM system_config WHERE setting_key = ?", (setting_key,))
        row = cursor.fetchone()
    if not row or row[0] is None:
        return default
    return row[0]


def set_system_config(setting_key, setting_value, description=None):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''
            INSERT INTO system_config (setting_key, setting_value, description)
            VALUES (?, ?, ?)
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_value = excluded.setting_value,
                description = COALESCE(excluded.description, system_config.description)
            ''',
            (setting_key, setting_value, description),
        )
        conn.commit()


def delete_system_config(setting_key):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM system_config WHERE setting_key = ?", (setting_key,))
        conn.commit()


def get_saved_credentials():
    raw_value = get_system_config(CREDENTIAL_CONFIG_KEY, default="")
    if not raw_value:
        return {}

    try:
        encrypted_payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}

    return {
        "buff_auth": _decrypt_value(encrypted_payload.get("buff_auth", "")),
        "steam_cookies": _decrypt_value(encrypted_payload.get("steam_cookies", "")),
        "updated_at": encrypted_payload.get("updated_at"),
    }


def save_credentials(buff_auth=None, steam_cookies=None):
    current = get_saved_credentials()
    next_payload = {
        "buff_auth": _encrypt_value(buff_auth if buff_auth is not None else current.get("buff_auth", "")),
        "steam_cookies": _encrypt_value(steam_cookies if steam_cookies is not None else current.get("steam_cookies", "")),
        "updated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    set_system_config(
        CREDENTIAL_CONFIG_KEY,
        json.dumps(next_payload, ensure_ascii=False),
        description="Crawler credentials stored encrypted",
    )
    return next_payload["updated_at"]


def clear_credentials(fields=None):
    current = get_saved_credentials()
    if not current:
        return

    fields = set(fields or ["buff_auth", "steam_cookies"])
    next_buff_auth = "" if "buff_auth" in fields else current.get("buff_auth", "")
    next_steam = "" if "steam_cookies" in fields else current.get("steam_cookies", "")

    if not next_buff_auth and not next_steam:
        delete_system_config(CREDENTIAL_CONFIG_KEY)
        return

    save_credentials(buff_auth=next_buff_auth, steam_cookies=next_steam)


def get_saved_settings():
    raw_value = get_system_config(NON_SECRET_CONFIG_KEY, default="")
    if not raw_value:
        return {}
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return {}


def save_settings(config):
    allowed_keys = {
        "proxy_url",
        "game_type",
        "min_sell_price",
        "max_sell_price",
        "min_sell_count",
    }
    filtered = {key: config.get(key) for key in allowed_keys if key in config}
    filtered["updated_at"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    set_system_config(
        NON_SECRET_CONFIG_KEY,
        json.dumps(filtered, ensure_ascii=False),
        description="Crawler non-secret settings",
    )
    return filtered


def merge_runtime_config(request_config):
    request_config = request_config or {}
    saved_settings = get_saved_settings()
    saved_credentials = get_saved_credentials()

    merged = {
        "proxy_url": str(request_config.get("proxy_url") or saved_settings.get("proxy_url") or "").strip(),
        "game_type": request_config.get("game_type") or saved_settings.get("game_type") or "CSGO",
        "min_sell_price": str(request_config.get("min_sell_price") or saved_settings.get("min_sell_price") or "25"),
        "max_sell_price": str(request_config.get("max_sell_price") or saved_settings.get("max_sell_price") or "300"),
        "min_sell_count": str(request_config.get("min_sell_count") or saved_settings.get("min_sell_count") or "20"),
        "buff_auth": str(request_config.get("buff_auth") or saved_credentials.get("buff_auth") or "").strip(),
        "steam_cookies": str(request_config.get("steam_cookies") or saved_credentials.get("steam_cookies") or "").strip(),
    }

    if merged["buff_auth"] or merged["steam_cookies"]:
        save_credentials(
            buff_auth=merged["buff_auth"] if merged["buff_auth"] else None,
            steam_cookies=merged["steam_cookies"] if merged["steam_cookies"] else None,
        )

    save_settings(merged)
    return merged


def build_credentials_status():
    saved_settings = get_saved_settings()
    saved_credentials = get_saved_credentials()
    credential_health = get_credential_health()
    has_buff = bool(saved_credentials.get("buff_auth"))
    has_steam = bool(saved_credentials.get("steam_cookies"))

    return {
        "buff": {
            "configured": has_buff,
            "masked": "已保存" if has_buff else "未保存",
            "health": credential_health.get("buff", {}),
        },
        "steam": {
            "configured": has_steam,
            "masked": "已保存" if has_steam else "未保存",
            "health": credential_health.get("steam", {}),
        },
        "settings": {
            "proxy_url": saved_settings.get("proxy_url", ""),
            "game_type": saved_settings.get("game_type", "CSGO"),
            "min_sell_price": saved_settings.get("min_sell_price", "25"),
            "max_sell_price": saved_settings.get("max_sell_price", "300"),
            "min_sell_count": saved_settings.get("min_sell_count", "20"),
            "updated_at": saved_settings.get("updated_at"),
        },
        "credentials_updated_at": saved_credentials.get("updated_at"),
    }


def sanitize_config_for_log(config):
    safe = dict(config)
    if safe.get("buff_auth"):
        safe["buff_auth"] = "[REDACTED]"
    if safe.get("steam_cookies"):
        safe["steam_cookies"] = "[REDACTED]"
    return safe


def compute_release_at_utc(purchased_at=None):
    base_time = purchased_at or datetime.now(timezone.utc)
    if base_time.tzinfo is None:
        base_time = base_time.replace(tzinfo=timezone.utc)
    release_day = (base_time + timedelta(days=7)).date()
    release_at = datetime.combine(release_day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=7)
    return release_at


def normalize_buff_game(game_value):
    text = str(game_value or "").strip().lower()
    if text in {"csgo", "cs2", "cs2 / csgo"}:
        return "csgo"
    if text in {"dota2", "dota 2"}:
        return "dota2"
    return text or "csgo"


def get_buff_cookie_jar(config):
    raw_auth = str(config.get("buff_auth") or "").strip()
    if not raw_auth:
        return {}

    cookies = parse_cookies_string(raw_auth)
    if cookies:
        return cookies

    return {"session": raw_auth}


def get_buff_session_value(config):
    cookies = get_buff_cookie_jar(config)
    return str(cookies.get("session") or "").strip()


def get_buff_buy_settings():
    raw_value = get_system_config(BUFF_BUY_SETTINGS_KEY, default="")
    if not raw_value:
        return {}
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return {}


def save_buff_buy_settings(settings):
    allowed_keys = {"steamid", "pay_method", "allow_tradable_cooldown"}
    filtered = {key: settings.get(key) for key in allowed_keys if key in settings}
    filtered["updated_at"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    set_system_config(
        BUFF_BUY_SETTINGS_KEY,
        json.dumps(filtered, ensure_ascii=False),
        description="BUFF batch buy settings",
    )
    return filtered


def get_credential_health():
    raw_value = get_system_config(CREDENTIAL_HEALTH_KEY, default="")
    if not raw_value:
        return {}
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return {}


def save_credential_health(health):
    set_system_config(
        CREDENTIAL_HEALTH_KEY,
        json.dumps(health, ensure_ascii=False),
        description="Credential health state",
    )
    return health


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
            append_crawler_log(f"保存数据到数据库时出错: {e}")


def count_market_items():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM market_items")
        row = cursor.fetchone()
        return row[0] if row else 0


def market_data_meta(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), MAX(update_time), MAX(date(update_time)) FROM market_items")
    count, latest_update_time, latest_date = cursor.fetchone()
    return {
        "count": count or 0,
        "latest_update_time": latest_update_time,
        "latest_date": latest_date,
        "stale_window_days": STALE_DATA_WINDOW_DAYS,
        "max_sell_ratio": MAX_SELL_RATIO,
        "min_steam_volume": MIN_STEAM_VOLUME,
    }


def remember_crawled_item(item):
    with db_lock:
        for index, existing_item in enumerate(crawled_data):
            if existing_item["buff_id"] == item["buff_id"]:
                crawled_data[index] = item
                return
        crawled_data.append(item)
        if len(crawled_data) > 1000:
            crawled_data.pop(0)


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


def run_crawler(config):
    global is_crawling
    is_crawling = True
    append_crawler_log(f"使用配置启动爬虫: {sanitize_config_for_log(config)}")
    if not get_buff_session_value(config):
        append_crawler_log("警告: BUFF 凭证未配置或缺少 session，列表接口可能返回请先登录。")

    crawler = MarketCrawler(
        config,
        db_file=DB_FILE,
        db_lock=db_lock,
        log=append_crawler_log,
        save_item=save_item_to_db,
        remember_item=remember_crawled_item,
        count_items=count_market_items,
        buff_cookies=get_buff_cookie_jar(config),
        steam_cookies=parse_cookies_string(config.get("steam_cookies")),
        should_stop=lambda: not is_crawling,
    )
    try:
        crawler.run()
    except Exception as exc:
        append_crawler_log(f"爬虫线程发生未捕获异常: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        is_crawling = False
        prune_result = prune_stale_market_data()
        if prune_result["deleted"]:
            append_crawler_log(
                f"停止抓取后已清理 {prune_result['deleted']} 条旧数据，"
                f"保留 {prune_result['cutoff_date']} 及之后的记录。"
            )
        append_crawler_log("爬虫线程已结束。")


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


@app.route('/api/config', methods=['GET'])
@login_required
def api_get_config():
    return jsonify({"success": True, "config": build_credentials_status()})


@app.route('/api/config', methods=['POST'])
@login_required
@admin_required
def api_save_config():
    data = request.get_json() or {}
    merged = merge_runtime_config(data)
    return jsonify({
        "success": True,
        "message": "配置已保存到服务器。",
        "config": build_credentials_status(),
        "effective": sanitize_config_for_log(merged),
    })


@app.route('/api/config/credentials', methods=['DELETE'])
@login_required
@admin_required
def api_clear_credentials():
    data = request.get_json(silent=True) or {}
    fields = data.get("fields") or ["buff_auth", "steam_cookies"]
    clear_credentials(fields=fields)
    return jsonify({
        "success": True,
        "message": "凭证已清除。",
        "config": build_credentials_status(),
    })


@app.route('/api/buff_buy_settings', methods=['GET'])
@login_required
@admin_required
def api_get_buff_buy_settings():
    return jsonify({"success": True, "settings": get_buff_buy_settings()})


@app.route('/api/buff_buy_settings', methods=['POST'])
@login_required
@admin_required
def api_save_buff_buy_settings():
    data = request.get_json() or {}
    settings = save_buff_buy_settings(data)
    return jsonify({"success": True, "message": "BUFF 购买设置已保存。", "settings": settings})


def _build_purchase_record_payload(item_name, game_type, preview_item, order_payload, total_price):
    purchased_at = datetime.now(timezone.utc)
    release_at = compute_release_at_utc(purchased_at)
    return {
        "source_platform": "BUFF",
        "source_order_id": str(order_payload.get("data", {}).get("order_id") or order_payload.get("data", {}).get("bill_order_id") or ""),
        "buff_goods_id": preview_item.get("goods_id"),
        "item_name": item_name,
        "game_type": game_type,
        "quantity": 1,
        "unit_price": float(total_price),
        "total_price": float(total_price),
        "purchased_at": purchased_at.astimezone(timezone.utc).isoformat(),
        "release_at_utc": release_at.astimezone(timezone.utc).isoformat(),
        "status": "purchased",
    }


@app.route('/api/buff/buy_preview_batch', methods=['POST'])
@login_required
@admin_required
def api_buff_buy_preview_batch():
    data = request.get_json() or {}
    items = data.get("items") or []
    config = merge_runtime_config(data.get("config") or {})
    buy_settings = save_buff_buy_settings(data.get("buy_settings") or {})
    steamid = str((data.get("buy_settings") or {}).get("steamid") or buy_settings.get("steamid") or "").strip()
    pay_method = str((data.get("buy_settings") or {}).get("pay_method") or buy_settings.get("pay_method") or "").strip()
    allow_cooldown = int((data.get("buy_settings") or {}).get("allow_tradable_cooldown") or buy_settings.get("allow_tradable_cooldown") or 0)

    if not steamid:
        return jsonify({"success": False, "error": "缺少 steamid"}), 400

    service = BuffTradeService(config)
    results = []

    for item in items:
        game_type = str(item.get("game_type") or config.get("game_type") or "CSGO").lower()
        goods_id = item.get("goods_id") or item.get("buff_id")
        item_name = str(item.get("name") or "")
        target_price = float(item.get("target_price") or item.get("buff_sell_price") or 0)
        if not goods_id or target_price <= 0:
            results.append({"success": False, "name": item_name, "goods_id": goods_id, "error": "缺少 goods_id 或目标价格"})
            continue

        try:
            order_book = service.get_sell_order_book(game_type, goods_id)
            sell_orders = order_book.get("items") or order_book.get("sell_orders") or []
            matched_order = None
            for order in sell_orders:
                order_price = float(order.get("price") or order.get("sell_min_price") or 0)
                if order_price > 0 and order_price <= target_price:
                    matched_order = order
                    break

            if not matched_order:
                results.append({
                    "success": False,
                    "name": item_name,
                    "goods_id": goods_id,
                    "error": "未找到满足价格条件的卖单",
                })
                continue

            sell_order_id = matched_order.get("id") or matched_order.get("sell_order_id")
            price = matched_order.get("price") or target_price
            preview_payload = service.preview_buy(
                game=game_type,
                sell_order_id=sell_order_id,
                goods_id=goods_id,
                price=price,
                steamid=steamid,
                allow_tradable_cooldown=allow_cooldown,
            )

            pay_description = None
            if pay_method:
                pay_desc_payload = service.get_pay_method_description(pay_method=pay_method, amount=price)
                pay_description = (pay_desc_payload.get("data") or {}).get("description")

            results.append({
                "success": True,
                "name": item_name,
                "goods_id": goods_id,
                "sell_order_id": sell_order_id,
                "price": float(price),
                "preview": preview_payload.get("data") or {},
                "pay_method": pay_method or None,
                "pay_method_description": pay_description,
            })
        except Exception as e:
            results.append({
                "success": False,
                "name": item_name,
                "goods_id": goods_id,
                "error": str(e),
            })

    return jsonify({"success": True, "data": results, "count": len(results)})


@app.route('/api/buff/buy_batch', methods=['POST'])
@login_required
@admin_required
def api_buff_buy_batch():
    data = request.get_json() or {}
    items = data.get("items") or []
    config = merge_runtime_config(data.get("config") or {})
    buy_settings = save_buff_buy_settings(data.get("buy_settings") or {})
    steamid = str((data.get("buy_settings") or {}).get("steamid") or buy_settings.get("steamid") or "").strip()
    pay_method = str((data.get("buy_settings") or {}).get("pay_method") or buy_settings.get("pay_method") or "").strip()
    allow_cooldown = int((data.get("buy_settings") or {}).get("allow_tradable_cooldown") or buy_settings.get("allow_tradable_cooldown") or 0)

    if not steamid or not pay_method:
        return jsonify({"success": False, "error": "缺少 steamid 或 pay_method"}), 400

    service = BuffTradeService(config)
    results = []

    for item in items:
        item_name = str(item.get("name") or "")
        game_type = str(item.get("game_type") or config.get("game_type") or "CSGO").lower()
        goods_id = item.get("goods_id") or item.get("buff_id")
        sell_order_id = item.get("sell_order_id")
        price = float(item.get("price") or 0)

        if not goods_id or not sell_order_id or price <= 0:
            results.append({"success": False, "name": item_name, "goods_id": goods_id, "error": "缺少 sell_order_id 或价格"})
            continue

        try:
            order_payload = service.buy_order(
                game=game_type,
                sell_order_id=sell_order_id,
                goods_id=goods_id,
                price=price,
                steamid=steamid,
                pay_method=pay_method,
                allow_tradable_cooldown=allow_cooldown,
            )

            record_payload = _build_purchase_record_payload(item_name, game_type.upper(), item, order_payload, price)
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    '''
                    INSERT INTO purchase_records (
                        source_platform, source_order_id, buff_goods_id, item_name, game_type,
                        quantity, unit_price, total_price, purchased_at, release_at_utc, reminder_sent, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    ''',
                    (
                        record_payload["source_platform"],
                        record_payload["source_order_id"],
                        record_payload["buff_goods_id"],
                        record_payload["item_name"],
                        record_payload["game_type"],
                        record_payload["quantity"],
                        record_payload["unit_price"],
                        record_payload["total_price"],
                        record_payload["purchased_at"],
                        record_payload["release_at_utc"],
                        record_payload["status"],
                    ),
                )
                conn.commit()

            results.append({
                "success": True,
                "name": item_name,
                "goods_id": goods_id,
                "sell_order_id": sell_order_id,
                "price": price,
                "order": order_payload.get("data") or {},
                "release_at_utc": record_payload["release_at_utc"],
            })
        except Exception as e:
            results.append({
                "success": False,
                "name": item_name,
                "goods_id": goods_id,
                "sell_order_id": sell_order_id,
                "error": str(e),
            })

    return jsonify({"success": True, "data": results, "count": len(results)})


@app.route('/api/purchase_records', methods=['GET'])
@login_required
def api_get_purchase_records():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT id, source_platform, source_order_id, buff_goods_id, item_name, game_type,
                   quantity, unit_price, total_price, purchased_at, release_at_utc, reminder_sent, status
            FROM purchase_records
            ORDER BY release_at_utc ASC, created_at DESC
            '''
        )
        rows = cursor.fetchall()

    records = [
        {
            "id": row[0],
            "source_platform": row[1],
            "source_order_id": row[2],
            "buff_goods_id": row[3],
            "item_name": row[4],
            "game_type": row[5],
            "quantity": row[6],
            "unit_price": row[7],
            "total_price": row[8],
            "purchased_at": row[9],
            "release_at_utc": row[10],
            "reminder_sent": bool(row[11]),
            "status": row[12],
        }
        for row in rows
    ]
    return jsonify({"success": True, "data": records, "count": len(records)})


@app.route('/api/purchase_records', methods=['POST'])
@login_required
@admin_required
def api_create_purchase_record():
    data = request.get_json() or {}

    item_name = str(data.get("item_name") or "").strip()
    if not item_name:
        return jsonify({"success": False, "error": "item_name 不能为空"}), 400

    quantity = int(data.get("quantity") or 1)
    unit_price = float(data.get("unit_price") or 0)
    total_price = float(data.get("total_price") or unit_price * quantity)

    purchased_at_raw = data.get("purchased_at")
    if purchased_at_raw:
        try:
            purchased_at = datetime.fromisoformat(purchased_at_raw.replace("Z", "+00:00"))
        except ValueError:
            return jsonify({"success": False, "error": "purchased_at 格式无效"}), 400
    else:
        purchased_at = datetime.now(timezone.utc)

    release_at = compute_release_at_utc(purchased_at)

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''
            INSERT INTO purchase_records (
                source_platform, source_order_id, buff_goods_id, item_name, game_type,
                quantity, unit_price, total_price, purchased_at, release_at_utc, reminder_sent, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            ''',
            (
                str(data.get("source_platform") or "BUFF"),
                str(data.get("source_order_id") or ""),
                data.get("buff_goods_id"),
                item_name,
                str(data.get("game_type") or ""),
                quantity,
                unit_price,
                total_price,
                purchased_at.astimezone(timezone.utc).isoformat(),
                release_at.astimezone(timezone.utc).isoformat(),
                str(data.get("status") or "purchased"),
            ),
        )
        record_id = cursor.lastrowid
        conn.commit()

    return jsonify({
        "success": True,
        "message": "购买记录已保存。",
        "record": {
            "id": record_id,
            "item_name": item_name,
            "release_at_utc": release_at.astimezone(timezone.utc).isoformat(),
        }
    }), 201

@app.route('/api/test_connection', methods=['POST'])
@login_required
@admin_required
def api_test_connection():
    try:
        config = merge_runtime_config(request.get_json() or {})
        proxy_url = config.get('proxy_url')
        steam_cookies_str = config.get('steam_cookies')
        buff_session = get_buff_session_value(config)

        steam_cookies = parse_cookies_string(steam_cookies_str)
        buff_cookies = get_buff_cookie_jar(config)

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

        checked_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        save_credential_health({
            "buff": {
                "status": "healthy" if buff_success else "expired_or_invalid",
                "checked_at": checked_at,
                "message": buff_msg,
            },
            "steam": {
                "status": "healthy" if steam_success else "expired_or_invalid",
                "checked_at": checked_at,
                "message": steam_msg,
            },
        })

        return jsonify({
            'buff': {'success': buff_success, 'message': buff_msg},
            'steam': {'success': steam_success, 'message': steam_msg},
            'config': build_credentials_status(),
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
@admin_required
def start_crawler():
    global crawler_thread, is_crawling
    if is_crawling:
        return jsonify({"message": "爬虫已在运行中。", "status": "running"}), 409

    config = merge_runtime_config(request.get_json() or {})
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
                meta = market_data_meta(conn)

        return jsonify({
            "success": True,
            "data": items,
            "count": meta["count"],
            "meta": meta,
        })
    except Exception as e:
        print(f"获取数据时出错: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/data/version')
@login_required
def get_data_version():
    try:
        with get_db_connection() as conn:
            meta = market_data_meta(conn)
        return jsonify({"success": True, "count": meta["count"], "meta": meta})
    except Exception as e:
        print(f"获取数据版本时出错: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/crawler_log')
@login_required
def api_crawler_log():
    try:
        after_id = int(request.args.get("after") or 0)
    except (TypeError, ValueError):
        after_id = 0
    lines, latest_id = snapshot_crawler_logs(after_id)
    return jsonify({"success": True, "logs": lines, "latest_id": latest_id})


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
