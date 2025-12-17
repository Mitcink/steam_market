#!/usr/bin/env python3
"""
用户管理命令行工具
用于在服务器上添加、删除、修改用户
"""

import sys
import sqlite3
import argparse
from getpass import getpass
from werkzeug.security import generate_password_hash, check_password_hash

DB_FILE = 'data/users.db'


def init_db():
    """初始化数据库连接"""
    conn = sqlite3.connect(DB_FILE)
    return conn


def list_users():
    """列出所有用户"""
    conn = init_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, username, created_at, is_admin, is_active 
        FROM users 
        ORDER BY created_at DESC
    """)

    users = cursor.fetchall()

    print("\n用户列表:")
    print("-" * 80)
    print(f"{'ID':<5} {'用户名':<20} {'创建时间':<25} {'角色':<10} {'状态':<8}")
    print("-" * 80)

    for user in users:
        role = "管理员" if user[3] else "普通用户"
        status = "启用" if user[4] else "禁用"
        print(f"{user[0]:<5} {user[1]:<20} {user[2]:<25} {role:<10} {status:<8}")

    conn.close()


def add_user():
    """添加新用户"""
    print("\n添加新用户")
    print("-" * 40)

    username = input("用户名: ").strip()

    conn = init_db()
    cursor = conn.cursor()

    # 检查用户是否已存在
    cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
    if cursor.fetchone():
        print(f"错误: 用户 '{username}' 已存在!")
        conn.close()
        return

    password = getpass("密码: ")
    confirm_password = getpass("确认密码: ")

    if password != confirm_password:
        print("错误: 两次输入的密码不一致!")
        conn.close()
        return

    if len(password) < 6:
        print("错误: 密码长度至少6位!")
        conn.close()
        return

    is_admin_input = input("是否为管理员? (y/N): ").strip().lower()
    is_admin = 1 if is_admin_input == 'y' else 0

    password_hash = generate_password_hash(password)

    try:
        cursor.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
            (username, password_hash, is_admin)
        )
        conn.commit()
        print(f"成功: 用户 '{username}' 已添加!")
    except Exception as e:
        print(f"错误: 添加用户失败 - {e}")
    finally:
        conn.close()


def delete_user():
    """删除用户"""
    print("\n删除用户")
    print("-" * 40)

    username = input("要删除的用户名: ").strip()

    conn = init_db()
    cursor = conn.cursor()

    # 获取用户信息
    cursor.execute("SELECT id, username FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()

    if not user:
        print(f"错误: 用户 '{username}' 不存在!")
        conn.close()
        return

    # 确认删除
    confirm = input(f"确定要删除用户 '{username}' 吗? (y/N): ").strip().lower()

    if confirm != 'y':
        print("取消删除")
        conn.close()
        return

    try:
        cursor.execute("DELETE FROM users WHERE id = ?", (user[0],))
        conn.commit()
        print(f"成功: 用户 '{username}' 已删除!")
    except Exception as e:
        print(f"错误: 删除用户失败 - {e}")
    finally:
        conn.close()


def change_password():
    """修改用户密码"""
    print("\n修改用户密码")
    print("-" * 40)

    username = input("用户名: ").strip()

    conn = init_db()
    cursor = conn.cursor()

    # 获取用户信息
    cursor.execute("SELECT id, username FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()

    if not user:
        print(f"错误: 用户 '{username}' 不存在!")
        conn.close()
        return

    password = getpass("新密码: ")
    confirm_password = getpass("确认新密码: ")

    if password != confirm_password:
        print("错误: 两次输入的密码不一致!")
        conn.close()
        return

    if len(password) < 6:
        print("错误: 密码长度至少6位!")
        conn.close()
        return

    password_hash = generate_password_hash(password)

    try:
        cursor.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user[0])
        )
        conn.commit()
        print(f"成功: 用户 '{username}' 的密码已更新!")
    except Exception as e:
        print(f"错误: 更新密码失败 - {e}")
    finally:
        conn.close()


def toggle_user_status():
    """启用/禁用用户"""
    print("\n启用/禁用用户")
    print("-" * 40)

    username = input("用户名: ").strip()

    conn = init_db()
    cursor = conn.cursor()

    # 获取用户当前状态
    cursor.execute("SELECT id, username, is_active FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()

    if not user:
        print(f"错误: 用户 '{username}' 不存在!")
        conn.close()
        return

    current_status = "启用" if user[2] else "禁用"
    new_status = 0 if user[2] else 1
    new_status_text = "禁用" if user[2] else "启用"

    confirm = input(
        f"用户 '{username}' 当前状态: {current_status}\n确定要{new_status_text}此用户吗? (y/N): ").strip().lower()

    if confirm != 'y':
        print("操作取消")
        conn.close()
        return

    try:
        cursor.execute(
            "UPDATE users SET is_active = ? WHERE id = ?",
            (new_status, user[0])
        )
        conn.commit()
        print(f"成功: 用户 '{username}' 已{new_status_text}!")
    except Exception as e:
        print(f"错误: 更新用户状态失败 - {e}")
    finally:
        conn.close()


def toggle_admin_status():
    """设置/取消管理员权限"""
    print("\n设置/取消管理员权限")
    print("-" * 40)

    username = input("用户名: ").strip()

    conn = init_db()
    cursor = conn.cursor()

    # 获取用户当前权限
    cursor.execute("SELECT id, username, is_admin FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()

    if not user:
        print(f"错误: 用户 '{username}' 不存在!")
        conn.close()
        return

    current_role = "管理员" if user[2] else "普通用户"
    new_role = 0 if user[2] else 1
    new_role_text = "取消管理员权限" if user[2] else "设为管理员"

    confirm = input(f"用户 '{username}' 当前角色: {current_role}\n确定要{new_role_text}吗? (y/N): ").strip().lower()

    if confirm != 'y':
        print("操作取消")
        conn.close()
        return

    try:
        cursor.execute(
            "UPDATE users SET is_admin = ? WHERE id = ?",
            (new_role, user[0])
        )
        conn.commit()
        print(f"成功: 用户 '{username}' 权限已更新!")
    except Exception as e:
        print(f"错误: 更新用户权限失败 - {e}")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description='爬虫系统用户管理工具')
    subparsers = parser.add_subparsers(dest='command', help='可用的命令')

    # list 命令
    subparsers.add_parser('list', help='列出所有用户')

    # add 命令
    subparsers.add_parser('add', help='添加新用户')

    # delete 命令
    subparsers.add_parser('delete', help='删除用户')

    # password 命令
    subparsers.add_parser('password', help='修改用户密码')

    # status 命令
    subparsers.add_parser('status', help='启用/禁用用户')

    # admin 命令
    subparsers.add_parser('admin', help='设置/取消管理员权限')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    try:
        if args.command == 'list':
            list_users()
        elif args.command == 'add':
            add_user()
        elif args.command == 'delete':
            delete_user()
        elif args.command == 'password':
            change_password()
        elif args.command == 'status':
            toggle_user_status()
        elif args.command == 'admin':
            toggle_admin_status()
    except KeyboardInterrupt:
        print("\n操作已取消")
    except Exception as e:
        print(f"错误: {e}")


if __name__ == '__main__':
    main()