#!/usr/bin/env python3
"""
数据库初始化脚本
独立运行以确保数据库表正确创建
"""

import os
import sqlite3
from werkzeug.security import generate_password_hash

DATA_DIR = 'data'
DB_FILE = os.path.join(DATA_DIR, 'users.db')

def init_database():
    """初始化数据库表结构"""
    os.makedirs(DATA_DIR, exist_ok=True)
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    try:
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
        print("✓ 用户表创建成功")
        
        # 创建配置表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_config (
            id INTEGER PRIMARY KEY,
            setting_key TEXT UNIQUE NOT NULL,
            setting_value TEXT,
            description TEXT
        )
        ''')
        print("✓ 配置表创建成功")
        
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
        print("✓ 市场物品表创建成功")
        
        # 创建索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_buff_id ON market_items(buff_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_update_time ON market_items(update_time)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_kind ON market_items(kind)')
        print("✓ 索引创建成功")

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS steam_item_nameids (
            steam_market_id TEXT PRIMARY KEY,
            item_nameid TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        ''')
        print("✓ Steam item_nameid 缓存表创建成功")
        
        # 插入默认管理员
        cursor.execute("SELECT id FROM users WHERE username = ?", ('admin',))
        if not cursor.fetchone():
            password_hash = generate_password_hash('admin123')
            cursor.execute(
                "INSERT INTO users (username, password_hash, is_admin, is_active) VALUES (?, ?, 1, 1)",
                ('admin', password_hash)
            )
            print("✓ 创建默认管理员用户: admin / admin123")
        else:
            print("✓ 管理员用户已存在")
        
        conn.commit()
        print("\n数据库初始化完成！")
        print(f"数据库文件位置: {os.path.abspath(DB_FILE)}")
        
        # 验证表是否创建成功
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = cursor.fetchall()
        print(f"\n已创建的表: {[t[0] for t in tables]}")
        
    except Exception as e:
        print(f"❌ 数据库初始化失败: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()

if __name__ == '__main__':
    print("开始初始化数据库...")
    init_database()
