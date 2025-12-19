import sqlite3
import json
import os
from datetime import datetime
import threading

class ItemDatabase:
    def __init__(self, db_path="data/items.db"):
        self.db_path = db_path
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.init_database()

    def _get_connection(self):
        """获取开启了 WAL 模式的连接"""
        conn = sqlite3.connect(self.db_path, timeout=20) # 增加超时等待
        conn.execute("PRAGMA journal_mode=WAL;") # 开启并发性能更好的WAL模式
        conn.row_factory = sqlite3.Row
        return conn

    def init_database(self):
        with self._get_connection() as conn:
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
            conn.commit()

    def save_items(self, items):
        if not items: return
        with self.lock: # 确保同一时间只有一个线程写入
            with self._get_connection() as conn:
                try:
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
        with self._get_connection() as conn:
            try:
                # 仅获取最近更新的200条，避免数据过多导致前端卡顿
                cursor = conn.execute('SELECT raw_data FROM items ORDER BY last_updated DESC LIMIT 200')
                return [json.loads(row['raw_data']) for row in cursor.fetchall()]
            except Exception as e:
                print(f"Database Load Error: {e}")
                return []

    def get_count(self):
        with self._get_connection() as conn:
            return conn.execute('SELECT COUNT(*) FROM items').fetchone()[0]

db = ItemDatabase()