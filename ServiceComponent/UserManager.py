import re
import os
import bcrypt
import logging
import sqlite3
import hashlib
import threading
from typing import Tuple


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)


class UserManager:
    INVALID_USER_ID = -1
    USER_NAME_LENGTH = (3, 50)
    USER_NAME_PATTERN_RE = r'^[a-zA-Z0-9_\-]+$'
    USER_NAME_PATTERN_SQL = '[a-zA-Z0-9_-]'
    # 密码策略：最少 8 字符，且必须同时包含字母和数字
    PASSWORD_MIN_LENGTH = 8

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.write_lock = threading.Lock()
        # P0-2：打开默认认证库路径前执行旧库迁移（幂等）。
        # - 仅当路径等于 GlobalConfig.DEFAULT_USER_DB_PATH 时迁移，
        #   测试/自定义路径不受影响；
        # - 迁移失败抛错（fail-fast），禁止静默新建空库导致账号丢失。
        try:
            from GlobalConfig import DEFAULT_USER_DB_PATH, migrate_legacy_user_db
            if os.path.abspath(db_path) == os.path.abspath(DEFAULT_USER_DB_PATH):
                migrate_legacy_user_db(db_path)
        except RuntimeError:
            raise
        except Exception:
            # GlobalConfig 不可用或非默认路径：跳过迁移，正常初始化
            pass
        self._init_db()

    def _init_db(self):
        conn = None
        try:
            conn = self._get_conn()
            cursor = conn.cursor()

            cursor.execute(f'''
                CREATE TABLE IF NOT EXISTS user_account (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL COLLATE NOCASE
                        CHECK(LENGTH(username) BETWEEN {self.USER_NAME_LENGTH[0]} AND {self.USER_NAME_LENGTH[1]})
                        CHECK(username GLOB '{self.USER_NAME_PATTERN_SQL}*'),
                    password_hash TEXT NOT NULL,
                    is_active INTEGER DEFAULT 1 CHECK(is_active IN (0, 1)),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS role (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role_name TEXT UNIQUE NOT NULL
                );''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_role (
                    user_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    PRIMARY KEY (user_id, role_id),
                    FOREIGN KEY (user_id) REFERENCES user_account(id) ON DELETE CASCADE,
                    FOREIGN KEY (role_id) REFERENCES role(id) ON DELETE CASCADE
                );''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS permission (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    perm_name TEXT UNIQUE NOT NULL
                );''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS role_permission (
                    role_id INTEGER NOT NULL,
                    perm_id INTEGER NOT NULL,
                    PRIMARY KEY (role_id, perm_id),
                    FOREIGN KEY (role_id) REFERENCES role(id) ON DELETE CASCADE,
                    FOREIGN KEY (perm_id) REFERENCES permission(id) ON DELETE CASCADE
                );''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS login_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    username TEXT NOT NULL,
                    client_ip TEXT NOT NULL,
                    attempted_password_hash TEXT,
                    result TEXT CHECK(result IN ('SUCCESS', 'FAILURE')) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES user_account(id) ON DELETE SET NULL
                );
            ''')

            cursor.execute('''
                CREATE TRIGGER IF NOT EXISTS update_user_account_timestamp
                AFTER UPDATE ON user_account
                FOR EACH ROW
                BEGIN
                    UPDATE user_account SET updated_at = CURRENT_TIMESTAMP WHERE id = OLD.id;
                END;
            ''')

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_username ON user_account(username)")
            conn.commit()

        except sqlite3.Error as e:
            # P0-2 修复：初始化失败（如目录只读无法建库/建表）必须抛错 fail-fast，
            # 不得吞掉异常后以“看似正常”的状态运行——那会导致登录时才暴露问题，
            # 且无法区分“配置问题”与“凭证错误”。启动失败信息中给出迁移指引。
            raise RuntimeError(
                f"认证数据库初始化失败（路径：{self.db_path}）：{e}。"
                f"常见原因：所在目录只读（Docker 中 _config 默认只读挂载）。"
                f"请将 Authentication.db 放到可写目录（默认 _data/），"
                f"或设置 AUTH_DB_PATH 环境变量。") from e
        except Exception as e:
            raise RuntimeError(
                f"认证数据库初始化异常（路径：{self.db_path}）：{e}") from e
        finally:
            if conn:
                conn.close()

    # ------------------------------------------ Authentication and Permission -----------------------------------------

    def authenticate(self, username, password, client_ip):
        with self.write_lock:
            conn = None
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, password_hash, is_active FROM user_account WHERE username = ?",
                    (username,)
                )
                user = cursor.fetchone()

                if not user:
                    self._log_login_attempt(
                        conn, None, username, client_ip,
                        None,
                        'FAILURE'
                    )
                    return False, "Invalid credentials"

                user_id, password_hash, is_active = user

                if not is_active:
                    self._log_login_attempt(
                        conn, user_id, username, client_ip,
                        None,
                        'FAILURE'
                    )
                    return False, "Account disabled"

                if bcrypt.checkpw(password.encode(), password_hash.encode('utf-8')):
                    self._log_login_attempt(
                        conn, user_id, username, client_ip,
                        None,
                        'SUCCESS'
                    )
                    return True, "Login successful"
                else:
                    self._log_login_attempt(
                        conn, user_id, username, client_ip,
                        None,
                        'FAILURE'
                    )
                    return False, "Invalid credentials"
            except Exception as e:
                logger.error(f'authenticate() - Exception: {str(e)}')
                return False, "Authentication error"
            finally:
                if conn:
                    conn.close()

    def check_permission(self, user_id, permission):
        conn = None
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM user_role ur "
                "JOIN role_permission rp ON ur.role_id = rp.role_id "
                "JOIN permission p ON rp.perm_id = p.id "
                "WHERE ur.user_id = ? AND p.perm_name = ?",
                (user_id, permission)
            )
            return cursor.fetchone() is not None
        except Exception:
            return False
        finally:
            if conn:
                conn.close()

    # ----------------------------------------------- Management - User ------------------------------------------------

    def get_all_users(self):
        """获取所有用户信息（包括角色）"""
        with self.write_lock:
            conn = None
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                # 查询用户基础信息
                cursor.execute('''
                    SELECT u.id, u.username, u.is_active, u.created_at, u.updated_at
                    FROM user_account u
                ''')
                users = []
                for row in cursor.fetchall():
                    user = {
                        'id': row[0],
                        'username': row[1],
                        'is_active': bool(row[2]),
                        'created_at': row[3],
                        'updated_at': row[4],
                        'roles': []
                    }
                    # 查询对应用户的角色
                    cursor.execute('''
                        SELECT r.role_name 
                        FROM user_role ur
                        JOIN role r ON ur.role_id = r.id
                        WHERE ur.user_id = ?
                    ''', (user['id'],))
                    user['roles'] = [role[0] for role in cursor.fetchall()]
                    users.append(user)
                return users
            except Exception as e:
                logger.error(f"get_all_users failed: {str(e)}")
                return []
            finally:
                if conn:
                    conn.close()

    def create_user(self, username: str, password: str, roles: list) -> Tuple[int, str]:
        result, reason = self._check_user_name(username)
        if not result:
            return UserManager.INVALID_USER_ID, reason

        # 密码强度策略：弱口令直接拒绝创建
        result, reason = self._check_password(password)
        if not result:
            return UserManager.INVALID_USER_ID, reason

        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode('utf-8')

        with self.write_lock:
            conn = None
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO user_account (username, password_hash) VALUES (?, ?)",
                    (username, password_hash)
                )
                user_id = cursor.lastrowid

                # 验证所有角色存在
                valid_roles = []
                for role_name in roles:
                    role_id = self._get_role_id(conn, role_name)
                    if role_id is None:
                        logger.warning(f"Role '{role_name}' not found, skipping")
                        continue
                    valid_roles.append((user_id, role_id))

                if valid_roles:
                    cursor.executemany(
                        "INSERT INTO user_role (user_id, role_id) VALUES (?, ?)",
                        valid_roles
                    )

                conn.commit()
                return user_id, f"User {username} created with {len(valid_roles)} roles"
            except sqlite3.IntegrityError as e:
                logger.error(f'User creation failed (duplicate): {username}')
                return UserManager.INVALID_USER_ID, "Username already exists"
            except Exception as e:
                logger.error(f'create_user() - exception: {str(e)}')
                return UserManager.INVALID_USER_ID, f"Error creating user: {str(e)}"
            finally:
                if conn:
                    conn.close()

    def update_user(self, user_id: int, new_username=None, new_password=None, is_active=None):
        with self.write_lock:
            conn = None
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                updates = []
                params = []

                if new_username:
                    updates.append("username = ?")
                    params.append(new_username)

                if new_password:
                    pwd_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode('utf-8')
                    updates.append("password_hash = ?")
                    params.append(pwd_hash)

                if is_active is not None:
                    updates.append("is_active = ?")
                    params.append(int(is_active))

                if not updates:
                    return False, "No updates provided"

                update_stmt = ", ".join(updates)
                params.append(user_id)
                cursor.execute(
                    f"UPDATE user_account SET {update_stmt} WHERE id = ?",
                    params
                )

                conn.commit()
                return True, "User updated successfully"
            except sqlite3.IntegrityError:
                return False, "Username already exists"
            except Exception as e:
                logger.error(f"update_user failed: {str(e)}")
                return False, f"Update error: {str(e)}"
            finally:
                if conn:
                    conn.close()

    def delete_user(self, user_id: int):
        """删除用户（级联删除关联角色）"""
        with self.write_lock:
            conn = None
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM user_account WHERE id = ?", (user_id,))
                conn.commit()
                return True, "User deleted"
            except Exception as e:
                logger.error(f"delete_user failed: {str(e)}")
                return False, f"Deletion error: {str(e)}"
            finally:
                if conn:
                    conn.close()

    # ----------------------------------------------- Management - Roles -----------------------------------------------

    def get_all_roles(self):
        """获取所有角色信息（包括权限）"""
        with self.write_lock:
            conn = None
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                # 查询角色基础信息
                cursor.execute('SELECT id, role_name FROM role')
                roles = []
                for row in cursor.fetchall():
                    role = {
                        'id': row[0],
                        'name': row[1],
                        'permissions': []
                    }
                    # 查询角色对应的权限
                    cursor.execute('''
                        SELECT p.perm_name
                        FROM role_permission rp
                        JOIN permission p ON rp.perm_id = p.id
                        WHERE rp.role_id = ?
                    ''', (role['id'],))
                    role['permissions'] = [perm[0] for perm in cursor.fetchall()]
                    roles.append(role)
                return roles
            except Exception as e:
                logger.error(f"get_all_roles failed: {str(e)}")
                return []
            finally:
                if conn:
                    conn.close()

    def assign_roles(self, user_id: int, roles: list):
        """为用户分配角色（覆盖原有角色）"""
        with self.write_lock:
            conn = None
            try:
                conn = self._get_conn()
                cursor = conn.cursor()

                # 删除现有角色
                cursor.execute("DELETE FROM user_role WHERE user_id = ?", (user_id,))

                # 添加新角色
                valid_roles = []
                for role_name in roles:
                    role_id = self._get_role_id(conn, role_name)
                    if role_id:
                        valid_roles.append((user_id, role_id))

                if valid_roles:
                    cursor.executemany(
                        "INSERT INTO user_role (user_id, role_id) VALUES (?, ?)",
                        valid_roles
                    )

                conn.commit()
                return True, f"Assigned {len(valid_roles)} roles to user"
            except Exception as e:
                logger.error(f"assign_roles failed: {str(e)}")
                return False, f"Role assignment error: {str(e)}"
            finally:
                if conn:
                    conn.close()

    def add_role(self, role_name, permissions):
        with self.write_lock:
            conn = None
            try:
                conn = self._get_conn()
                cursor = conn.cursor()

                # 检查角色是否已存在
                if self._get_role_id(conn, role_name):
                    return False, "Role already exists"

                cursor.execute("INSERT INTO role (role_name) VALUES (?)", (role_name,))
                role_id = cursor.lastrowid

                # 添加权限
                valid_perms = []
                for perm in permissions:
                    perm_id = self._get_perm_id(conn, perm)
                    if perm_id is None:
                        # 自动创建缺失权限
                        cursor.execute("INSERT INTO permission (perm_name) VALUES (?)", (perm,))
                        perm_id = cursor.lastrowid
                    valid_perms.append((role_id, perm_id))

                if valid_perms:
                    cursor.executemany(
                        "INSERT INTO role_permission (role_id, perm_id) VALUES (?, ?)",
                        valid_perms
                    )

                conn.commit()
                return True, f"Role '{role_name}' created with {len(valid_perms)} permissions"
            except sqlite3.IntegrityError:
                return False, "Role name already exists"
            except Exception as e:
                logger.error(f'add_role() - exception: {str(e)}')
                return False, f"Error creating role: {str(e)}"
            finally:
                if conn:
                    conn.close()

    def delete_role(self, role_name: str) -> Tuple[bool, str]:
        """删除角色及其关联的权限分配（级联删除user_role和role_permission）"""
        with self.write_lock:
            conn = None
            try:
                conn = self._get_conn()
                cursor = conn.cursor()

                # 获取角色ID
                cursor.execute("SELECT id FROM role WHERE role_name = ?", (role_name,))
                role = cursor.fetchone()

                if not role:
                    return False, "Role does not exist"

                role_id = role[0]

                # 执行删除（级联删除由外键约束自动处理）
                cursor.execute("DELETE FROM role WHERE id = ?", (role_id,))
                conn.commit()

                return True, f"Role '{role_name}' deleted"
            except sqlite3.IntegrityError as e:
                # 这个错误实际上不太可能发生，因为外键约束应该处理级联删除
                logger.error(f"delete_role integrity error: {str(e)}")
                return False, "Database integrity constraint violation"
            except Exception as e:
                logger.error(f"delete_role failed: {str(e)}")
                return False, f"Error deleting role: {str(e)}"
            finally:
                if conn:
                    conn.close()

    # -------------------------------------------- Management - Permissions --------------------------------------------

    def get_all_permissions(self):
        """获取所有权限信息"""
        with self.write_lock:
            conn = None
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute('SELECT id, perm_name FROM permission')
                return [{'id': row[0], 'name': row[1]} for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"get_all_permissions failed: {str(e)}")
                return []
            finally:
                if conn:
                    conn.close()

    def create_permission(self, perm_name: str):
        """创建新权限项"""
        with self.write_lock:
            conn = None
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute("INSERT INTO permission (perm_name) VALUES (?)", (perm_name,))
                conn.commit()
                return True, "Permission created"
            except sqlite3.IntegrityError:
                return False, "Permission already exists"
            except Exception as e:
                return False, f"Error creating permission: {str(e)}"
            finally:
                if conn:
                    conn.close()

    def delete_permission(self, perm_name: str):
        """删除权限（级联删除关联）"""
        with self.write_lock:
            conn = None
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM permission WHERE perm_name = ?", (perm_name,))
                conn.commit()
                return cursor.rowcount > 0, "Permission deleted"
            except Exception as e:
                return False, f"Error deleting permission: {str(e)}"
            finally:
                if conn:
                    conn.close()

    # ------------------------------------------------------ Logs ------------------------------------------------------

    def get_login_logs(self, username=None, result=None, start_time=None, end_time=None,
                       client_ip=None, page=1, per_page=20):
        """
        获取登录日志（支持分页和条件过滤）
        :param username: 按用户名过滤
        :param result: 按登录结果过滤（'SUCCESS'/'FAILURE'）
        :param start_time: 开始时间（格式：YYYY-MM-DD HH:MM:SS）
        :param end_time: 结束时间（格式：YYYY-MM-DD HH:MM:SS）
        :param client_ip: 按客户端IP过滤
        :param page: 页码（从1开始）
        :param per_page: 每页记录数
        :return: 登录日志列表
        """
        # 参数校验与安全限制
        page = max(1, page)
        per_page = max(1, min(100, per_page))  # 限制每页最多100条

        conn = None
        try:
            conn = self._get_conn()
            cursor = conn.cursor()

            # 构建基础查询
            base_query = """
                SELECT id, user_id, username, client_ip, 
                       attempted_password_hash, result, created_at
                FROM login_log
                WHERE 1=1
            """
            conditions = []
            params = []

            # 添加过滤条件
            if username:
                conditions.append("username = ?")
                params.append(username)
            if result in ('SUCCESS', 'FAILURE'):
                conditions.append("result = ?")
                params.append(result)
            if client_ip:
                conditions.append("client_ip = ?")
                params.append(client_ip)
            if start_time:
                conditions.append("created_at >= ?")
                params.append(start_time)
            if end_time:
                conditions.append("created_at <= ?")
                params.append(end_time)

            # 组合查询条件
            if conditions:
                base_query += " AND " + " AND ".join(conditions)

            # 添加排序和分页
            base_query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.append(per_page)
            params.append((page - 1) * per_page)

            # 执行查询
            cursor.execute(base_query, params)
            logs = []
            for row in cursor.fetchall():
                logs.append({
                    'id': row[0],
                    'user_id': row[1],
                    'username': row[2],
                    'client_ip': row[3],
                    'attempted_password_hash': row[4],
                    'result': row[5],
                    'timestamp': row[6]
                })
            return logs
        except Exception as e:
            logger.error(f"get_login_logs failed: {str(e)}")
            return []
        finally:
            if conn:
                conn.close()

    # ---------------------------------------------------- Helpers -----------------------------------------------------

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _log_login_attempt(self, conn, user_id, username, client_ip, pwd_hash, result):
        # 安全加固：绝不落库口令的任何哈希（含裸 SHA-256），防止弱口令离线爆破。
        pwd_hash = None
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO login_log (user_id, username, client_ip, attempted_password_hash, result) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, client_ip, pwd_hash, result)
        )
        conn.commit()

    def _get_role_id(self, conn, role_name) -> int:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM role WHERE role_name = ?", (role_name,))
        result = cursor.fetchone()
        return result[0] if result else None

    def _get_perm_id(self, conn, perm_name) -> int:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM permission WHERE perm_name = ?", (perm_name,))
        result = cursor.fetchone()
        return result[0] if result else None

    def _check_user_name(self, user_name: str) -> Tuple[bool, str]:
        if isinstance(user_name, str) and user_name:
            if len(user_name) < self.USER_NAME_LENGTH[0] or len(user_name) > self.USER_NAME_LENGTH[1]:
                return False, "invalid length"

            if not re.match(self.USER_NAME_PATTERN_RE, user_name):
                return False, "invalid char"

            return True, ''
        return False, 'invalid type'

    def _check_password(self, password: str) -> Tuple[bool, str]:
        """密码策略校验：最少 8 字符，且必须同时包含字母和数字。"""
        if not isinstance(password, str) or not password:
            return False, 'invalid password type'

        if len(password) < self.PASSWORD_MIN_LENGTH:
            return False, f'password too short (min {self.PASSWORD_MIN_LENGTH} chars)'

        if not re.search(r'[a-zA-Z]', password) or not re.search(r'\d', password):
            return False, 'password must contain both letters and digits'

        return True, ''
