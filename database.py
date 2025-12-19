import sqlite3
import json
import os
from datetime import datetime
import threading


class ItemDatabase:
    def __init__(self, db_path="data/items.db"):
        self.db_path = db_path
        # 确保数据目录存在
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        # 初始化线程锁，用于同一个进程内的多线程互斥
        self.lock = threading.Lock()
        self.init_database()

    def _get_connection(self):
        """获取数据库连接"""
        # check_same_thread=False 允许在 Flask 的多线程环境中使用
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def init_database(self):
        """初始化表结构和开启 WAL 模式"""
        with self._get_connection() as conn:
            # 开启 WAL 模式（并发性能更好，且设置是持久的）
            conn.execute("PRAGMA journal_mode=WAL;")

            conn.execute('''
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    buff_price REAL,
                    steam_price REAL,
                    profit REAL,
                    profit_rate REAL,
                    steam_volume INTEGER,
                    buff_url TEXT,
                    steam_url TEXT,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    raw_data TEXT
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS system_config (
                    id INTEGER PRIMARY KEY,
                    setting_key TEXT UNIQUE NOT NULL,
                    setting_value TEXT
                )
            ''')
            conn.commit()

    def save_items(self, items):
        if not items: return
        # 使用线程锁防止同一进程内的写入冲突
        # SQLite 的文件锁会处理跨进程冲突
        with self.lock:
            try:
                with self._get_connection() as conn:
                    data_to_insert = [(
                        item.get('name'),
                        item.get('buff_price'),
                        item.get('steam_price'),
                        item.get('profit'),
                        item.get('profit_rate'),
                        item.get('steam_volume'),
                        item.get('buff_url'),
                        item.get('steam_market_url'),
                        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        json.dumps(item, ensure_ascii=False)
                    ) for item in items]

                    conn.executemany('''
                        INSERT OR REPLACE INTO items 
                        (name, buff_price, steam_price, profit, profit_rate, steam_volume, 
                         buff_url, steam_url, last_updated, raw_data)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', data_to_insert)
                    conn.commit()
            except Exception as e:
                print(f"Database Save Error: {e}")

    def load_items(self):
        try:
            with self._get_connection() as conn:
                # 仅获取最近更新的200条
                cursor = conn.execute('SELECT raw_data FROM items ORDER BY last_updated DESC LIMIT 200')
                return [json.loads(row['raw_data']) for row in cursor.fetchall()]
        except Exception as e:
            print(f"Database Load Error: {e}")
            return []

    def get_count(self):
        try:
            with self._get_connection() as conn:
                return conn.execute('SELECT COUNT(*) FROM items').fetchone()[0]
        except:
            return 0

    def clear_all(self):
        """清空所有物品数据"""
        with self.lock:
            with self._get_connection() as conn:
                conn.execute('DELETE FROM items')
                conn.commit()


# 单例模式
db = ItemDatabase()