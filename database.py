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

    def init_database(self):
        """初始化数据库"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
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
        conn.close()
        print("Database initialized")

    def save_items(self, items):
        """批量保存物品（替换原来的save_data_to_file）"""
        if not items:
            return

        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            try:
                # 准备数据
                data_to_insert = []
                for item in items:
                    data_to_insert.append((
                        item.get('name'),
                        item.get('buff_price'),
                        item.get('steam_price'),
                        item.get('profit'),
                        item.get('profit_rate'),
                        item.get('steam_volume'),
                        item.get('buff_url'),
                        item.get('steam_market_url'),
                        datetime.now().isoformat(),
                        json.dumps(item, ensure_ascii=False)
                    ))

                # 使用INSERT OR REPLACE实现upsert
                cursor.executemany('''
                    INSERT OR REPLACE INTO items 
                    (name, buff_price, steam_price, profit, profit_rate, steam_volume, 
                     buff_url, steam_url, last_updated, raw_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', data_to_insert)

                conn.commit()
                print(f"Saved {len(items)} items to database")

            except Exception as e:
                print(f"Error saving items: {e}")
            finally:
                conn.close()

    def load_items(self):
        """加载所有物品（替换原来的load_data_from_file）"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute('SELECT raw_data FROM items ORDER BY last_updated DESC')
            rows = cursor.fetchall()

            items = []
            for row in rows:
                try:
                    item_data = json.loads(row[0])
                    items.append(item_data)
                except json.JSONDecodeError:
                    continue

            print(f"Loaded {len(items)} items from database")
            return items

        except Exception as e:
            print(f"Error loading items: {e}")
            return []
        finally:
            conn.close()

    def get_count(self):
        """获取物品总数"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute('SELECT COUNT(*) FROM items')
            count = cursor.fetchone()[0]
            return count
        except Exception as e:
            print(f"Error getting count: {e}")
            return 0
        finally:
            conn.close()


# 全局数据库实例
db = ItemDatabase()
