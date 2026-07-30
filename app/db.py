"""
图灵测试 · SQLite 数据库
存储对话历史、学到的语言习惯、口头禅（模型配置仍用 config.json）
使用 asyncio.Lock 保证写入安全，读取使用 WAL 模式
"""
import sqlite3
from typing import Optional
import os
import asyncio
import threading
from datetime import datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(HERE, "turing.db")

_local = threading.local()

# ---- 写入锁（防止并发写入导致 "database is locked"，惰性初始化避免事件循环冲突）----
_write_lock: asyncio.Lock | None = None


def _get_write_lock() -> asyncio.Lock:
    """惰性获取写入锁（在异步上下文中创建，避免模块加载时无事件循环）"""
    global _write_lock
    if _write_lock is None:
        try:
            _write_lock = asyncio.Lock()
        except RuntimeError:
            # 无事件循环时返回 None，使用同步写入
            return None
    return _write_lock


def _get_conn() -> sqlite3.Connection:
    """获取当前线程的数据库连接（自动建表）"""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        # WAL 模式：读写不互斥
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=5000")  # 等待 5s 而不是立即报错
        _local.conn.execute("PRAGMA synchronous=NORMAL")  # 减少磁盘写入频率
        _init_tables(_local.conn)
    return _local.conn


async def _exec_write(sql: str, params=()):
    """执行写入操作（带异步锁，防止并发写入冲突）"""
    lock = _get_write_lock()
    if lock:
        async with lock:
            loop = asyncio.get_running_loop()
            conn = _get_conn()
            await loop.run_in_executor(None, lambda: conn.execute(sql, params))
            await loop.run_in_executor(None, conn.commit)
    else:
        conn = _get_conn()
        conn.execute(sql, params)
        conn.commit()


async def _exec_write_many(sql: str, params_list: list):
    """批量写入"""
    if not params_list:
        return
    lock = _get_write_lock()
    if lock:
        async with lock:
            loop = asyncio.get_running_loop()
            conn = _get_conn()
            await loop.run_in_executor(None, lambda: conn.executemany(sql, params_list))
            await loop.run_in_executor(None, conn.commit)
    else:
        conn = _get_conn()
        conn.executemany(sql, params_list)
        conn.commit()


async def run_db(func, *args, **kwargs):
    """将同步 DB 函数放到线程池中执行，避免阻塞事件循环"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


def _init_tables(conn: sqlite3.Connection):
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA busy_timeout=5000;
        PRAGMA synchronous=NORMAL;

        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id, id);

        CREATE TABLE IF NOT EXISTS learned_styles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            situation TEXT NOT NULL,
            style TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS learned_jargon (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            word TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS intro_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            source TEXT DEFAULT 'system',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            reporter TEXT NOT NULL,
            reason TEXT NOT NULL,
            trigger_msg TEXT NOT NULL,
            full_log TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS banned_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT DEFAULT '',
            user_id TEXT DEFAULT '',
            ip TEXT DEFAULT '',
            reason TEXT DEFAULT '',
            banned_by TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS global_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            freq INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS stickers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            category TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS registered_nicknames (
            nickname TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT '',
            ip TEXT DEFAULT '',
            first_seen TEXT DEFAULT (datetime('now','localtime')),
            last_seen TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS admin_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            created_by TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            is_active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS admin_login_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            user_id TEXT DEFAULT '',
            nickname TEXT DEFAULT '',
            ip TEXT DEFAULT '',
            fingerprint TEXT DEFAULT '',
            login_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS user_warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_user_id TEXT NOT NULL DEFAULT '',
            target_name TEXT DEFAULT '',
            target_ip TEXT DEFAULT '',
            message TEXT NOT NULL,
            issued_by TEXT DEFAULT '',
            issued_at TEXT DEFAULT (datetime('now','localtime')),
            read INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS recovery_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL UNIQUE,
            nickname TEXT NOT NULL DEFAULT '',
            ip TEXT DEFAULT '',
            recovery_code TEXT NOT NULL UNIQUE,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS admin_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id TEXT NOT NULL,
            admin_name TEXT NOT NULL,
            action_type TEXT NOT NULL,
            detail TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
    """)
    # 兼容旧表
    for col in ['status', 'offender', 'reporter_ip', 'offender_ip']:
        try:
            conn.execute(f"ALTER TABLE reports ADD COLUMN {col} TEXT DEFAULT ''")
        except Exception:
            pass
    for col in ['expires_at', 'fingerprint']:
        try:
            conn.execute(f"ALTER TABLE banned_users ADD COLUMN {col} TEXT DEFAULT ''")
        except Exception:
            pass
    # registered_nicknames 添加 last_change_at 列
    try:
        conn.execute("ALTER TABLE registered_nicknames ADD COLUMN last_change_at TEXT DEFAULT ''")
    except Exception:
        pass
    # 默认开场白
    count = conn.execute("SELECT count(*) FROM intro_templates").fetchone()[0]
    if count == 0:
        conn.executemany(
            "INSERT INTO intro_templates (content, source) VALUES (?, 'system')",
            [("你好",), ("嗨",), ("哈喽",), ("你好呀～",)],
        )
    # 聊天大厅消息表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat大厅_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id TEXT UNIQUE NOT NULL,
            user_id TEXT NOT NULL,
            nickname TEXT NOT NULL,
            content TEXT NOT NULL,
            msg_type TEXT NOT NULL DEFAULT 'text',
            reply_to TEXT DEFAULT '',
            recalled INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_msg_id ON chat大厅_messages(msg_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_created ON chat大厅_messages(created_at)")
    # 聊天大厅离线通知表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_lobby_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_user_id TEXT NOT NULL,
            from_nickname TEXT NOT NULL,
            noti_type TEXT NOT NULL DEFAULT 'at',
            msg_id TEXT NOT NULL,
            message_content TEXT DEFAULT '',
            delivered INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_noti_target ON chat_lobby_notifications(target_user_id, delivered)")
    conn.commit()


def _exec_read(sql: str, params=()):
    """同步执行只读查询"""
    return _get_conn().execute(sql, params).fetchall()


def _exec_read_one(sql: str, params=()):
    """同步执行只读查询，返回单行"""
    return _get_conn().execute(sql, params).fetchone()


# ---- 会话管理 ----
def get_conversation(session_id: str) -> list:
    rows = _exec_read(
        "SELECT role, content FROM conversations WHERE session_id=? ORDER BY id", (session_id,)
    )
    return [(r["role"], r["content"]) for r in rows]


def add_conversation(session_id: str, role: str, content: str):
    # 同步写入（被异步函数调用时需注意锁，但这里的调用者通常不在高并发路径）
    conn = _get_conn()
    conn.execute(
        "INSERT INTO conversations (session_id, role, content) VALUES (?,?,?)",
        (session_id, role, content),
    )
    conn.commit()


def clear_conversation(session_id: str):
    conn = _get_conn()
    conn.execute("DELETE FROM conversations WHERE session_id=?", (session_id,))
    conn.commit()


# ---- 学习存储 ----
def get_learned_styles(session_id: str) -> list:
    rows = _exec_read(
        "SELECT situation, style FROM learned_styles WHERE session_id=? ORDER BY id DESC LIMIT 6",
        (session_id,),
    )
    return [(r["situation"], r["style"]) for r in rows]


def replace_learned_styles(session_id: str, styles: list):
    conn = _get_conn()
    conn.execute("DELETE FROM learned_styles WHERE session_id=?", (session_id,))
    for s, t in styles:
        conn.execute(
            "INSERT INTO learned_styles (session_id, situation, style) VALUES (?,?,?)",
            (session_id, s, t),
        )
    conn.commit()


def get_learned_jargon(session_id: str) -> list:
    rows = _exec_read(
        "SELECT word FROM learned_jargon WHERE session_id=? ORDER BY id DESC LIMIT 8",
        (session_id,),
    )
    return [r["word"] for r in rows]


def replace_learned_jargon(session_id: str, words: list):
    conn = _get_conn()
    conn.execute("DELETE FROM learned_jargon WHERE session_id=?", (session_id,))
    for w in words:
        conn.execute(
            "INSERT INTO learned_jargon (session_id, word) VALUES (?,?)",
            (session_id, w),
        )
    conn.commit()


def get_all_learned_styles() -> list:
    rows = _exec_read(
        "SELECT situation, style, session_id FROM learned_styles ORDER BY id DESC LIMIT 30"
    )
    return [(r["situation"], r["style"], r["session_id"][:8]) for r in rows]


def get_all_learned_jargon() -> list:
    rows = _exec_read(
        "SELECT word, session_id FROM learned_jargon ORDER BY id DESC LIMIT 30"
    )
    return [(r["word"], r["session_id"][:8]) for r in rows]


def get_all_conversations(limit: int = 30) -> list:
    rows = _exec_read(
        "SELECT role, content, session_id FROM conversations ORDER BY id DESC LIMIT ?", (limit,)
    )
    rows.reverse()
    return [(r["role"], r["content"][:80], r["session_id"][:8]) for r in rows]


def search_memory(session_id: str, query: str, limit: int = 5) -> list:
    conn = _get_conn()
    results = []
    style_rows = conn.execute(
        "SELECT situation, style FROM learned_styles WHERE session_id=? ORDER BY id DESC", (session_id,)
    ).fetchall()
    for s, t in style_rows:
        if query.lower() in s.lower() or query.lower() in t.lower():
            results.append(f"[风格] 当「{s}」时 → 「{t}」")
    conv_rows = conn.execute(
        "SELECT role, content FROM conversations WHERE session_id=? AND content LIKE ? ORDER BY id DESC LIMIT ?",
        (session_id, f"%{query}%", limit),
    ).fetchall()
    for role, content in conv_rows:
        tag = "你说" if role == "user" else "AI说"
        results.append(f"[对话] {tag}：{content[:100]}")
    return results[:limit] if results else ["未找到相关内容"]


def get_memory_summary(session_id: str) -> str:
    conn = _get_conn()
    styles = conn.execute(
        "SELECT situation, style FROM learned_styles WHERE session_id=? ORDER BY id DESC LIMIT 5",
        (session_id,),
    ).fetchall()
    jargon = conn.execute(
        "SELECT word FROM learned_jargon WHERE session_id=? ORDER BY id DESC LIMIT 5",
        (session_id,),
    ).fetchall()
    history = conn.execute(
        "SELECT role, content FROM conversations WHERE session_id=? ORDER BY id DESC LIMIT 5",
        (session_id,),
    ).fetchall()
    parts = []
    if styles:
        parts.append("学到的风格：" + "；".join(f"当「{s}」→「{t}」" for s, t in styles))
    if jargon:
        parts.append("口头禅：" + "、".join(w for (w,) in jargon))
    if history:
        parts.append("最近对话：" + " | ".join(
            f"{'[U]' if r=='user' else '[AI]'}{c[:40]}" for r, c in reversed(history)
        ))
    return "\n".join(parts) if parts else "暂无记忆"


# ---- 开场白 ----
def get_intro_templates(limit: int = 10) -> list:
    rows = _exec_read(
        "SELECT content FROM intro_templates ORDER BY id DESC LIMIT ?", (limit,),
    )
    return [r["content"] for r in rows] if rows else ["你好"]


def add_intro_template(content: str, source: str = "stolen"):
    conn = _get_conn()
    existing = conn.execute(
        "SELECT id FROM intro_templates WHERE content=?", (content,)
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO intro_templates (content, source) VALUES (?,?)",
            (content, source),
        )
        conn.commit()


# ---- 举报 ----
def save_report(session_id: str, reporter: str, offender: str, reason: str, trigger_msg: str, full_log: str,
                reporter_ip: str = "", offender_ip: str = ""):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO reports (session_id, reporter, offender, reason, trigger_msg, full_log, reporter_ip, offender_ip) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (session_id, reporter, offender, reason, trigger_msg, full_log, reporter_ip, offender_ip),
    )
    conn.commit()


def get_reports(limit: int = 20) -> list:
    rows = _exec_read("SELECT * FROM reports ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def update_report_status(report_id: int, status: str):
    conn = _get_conn()
    conn.execute("UPDATE reports SET status=? WHERE id=?", (status, report_id))
    conn.commit()


def clear_all_reports():
    conn = _get_conn()
    conn.execute("DELETE FROM reports")
    conn.commit()


# ---- 封禁 ----
def _ban_active(ban: dict) -> bool:
    expires = ban.get("expires_at", "")
    if not expires:
        return True
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return expires > now
    except Exception:
        return True


def is_banned(name: str = "", ip: str = "", user_id: str = "", fingerprint: str = "") -> bool:
    conn = _get_conn()
    conditions = []
    params = []
    if name:
        conditions.append("name=?")
        params.append(name)
    if ip:
        conditions.append("ip=?")
        params.append(ip)
    if user_id:
        conditions.append("user_id=?")
        params.append(user_id)
    if fingerprint:
        conditions.append("fingerprint=?")
        params.append(fingerprint)
    if not conditions:
        return False
    sql = "SELECT * FROM banned_users WHERE " + " OR ".join(conditions)
    rows = conn.execute(sql, params).fetchall()
    for row in rows:
        if _ban_active(dict(row)):
            return True
    return False


def get_ban_info(name: str = "", ip: str = "", user_id: str = "", fingerprint: str = "") -> Optional[dict]:
    conn = _get_conn()
    conditions = []
    params = []
    if name:
        conditions.append("name=?")
        params.append(name)
    if ip:
        conditions.append("ip=?")
        params.append(ip)
    if user_id:
        conditions.append("user_id=?")
        params.append(user_id)
    if fingerprint:
        conditions.append("fingerprint=?")
        params.append(fingerprint)
    if not conditions:
        return None
    sql = "SELECT * FROM banned_users WHERE " + " OR ".join(conditions)
    rows = conn.execute(sql, params).fetchall()
    for row in rows:
        ban = dict(row)
        if _ban_active(ban):
            return ban
    return None


def add_banned_user(name: str, reason: str = "", ip: str = "", user_id: str = "", banned_by: str = "", expires_at: str = "", fingerprint: str = ""):
    conn = _get_conn()
    # 同时封禁 IP、user_id、name、fingerprint 四个维度
    targets = []
    if name:
        targets.append(("name", name))
    if ip:
        targets.append(("ip", ip))
    if user_id:
        targets.append(("user_id", user_id))
    if fingerprint:
        targets.append(("fingerprint", fingerprint))
    if not targets:
        targets = [("name", name)]
    for field, value in targets:
        # 检查是否已有未过期的同值封禁
        existing = conn.execute(
            f"SELECT id, expires_at FROM banned_users WHERE {field}=?", (value,)
        ).fetchone()
        if existing and not existing["expires_at"]:
            continue  # 已永久封禁，跳过
        if existing and existing["expires_at"]:
            try:
                if existing["expires_at"] > datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
                    continue  # 未过期，跳过
            except Exception:
                pass
        conn.execute(
            "INSERT INTO banned_users (name, ip, user_id, fingerprint, reason, banned_by, expires_at) VALUES (?,?,?,?,?,?,?)",
            (name if field == "name" else "", ip if field == "ip" else "", user_id if field == "user_id" else "",
             fingerprint if field == "fingerprint" else "", reason, banned_by, expires_at),
        )
    conn.commit()


def remove_banned_user(ban_id: int):
    conn = _get_conn()
    conn.execute("DELETE FROM banned_users WHERE id=?", (ban_id,))
    conn.commit()


def get_banned_users() -> list:
    rows = _exec_read("SELECT * FROM banned_users ORDER BY id DESC")
    return [dict(r) for r in rows]


# ---- 贴纸 ----
def add_sticker(filename: str, description: str = "", category: str = ""):
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO stickers (filename, description, category) VALUES (?,?,?)",
        (filename, description, category),
    )
    conn.commit()


def get_stickers() -> list:
    rows = _exec_read("SELECT * FROM stickers ORDER BY id DESC")
    return [dict(r) for r in rows]


def get_sticker_by_filename(filename: str):
    row = _exec_read_one("SELECT * FROM stickers WHERE filename=?", (filename,))
    return dict(row) if row else None


def delete_sticker(sticker_id: int):
    conn = _get_conn()
    conn.execute("DELETE FROM stickers WHERE id=?", (sticker_id,))
    conn.commit()


# ---- 昵称注册 ----
def register_nickname(nickname: str, user_id: str, ip: str = ""):
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO registered_nicknames (nickname, user_id, ip, last_seen) VALUES (?,?,?, datetime('now','localtime'))",
        (nickname, user_id, ip),
    )
    conn.commit()


def is_nickname_taken(nickname: str, exclude_user_id: str = "") -> bool:
    if exclude_user_id:
        row = _exec_read_one(
            "SELECT 1 FROM registered_nicknames WHERE nickname=? AND user_id!=?",
            (nickname, exclude_user_id),
        )
    else:
        row = _exec_read_one(
            "SELECT 1 FROM registered_nicknames WHERE nickname=?", (nickname,)
        )
    return row is not None


def get_user_by_nickname(nickname: str):
    row = _exec_read_one("SELECT * FROM registered_nicknames WHERE nickname=?", (nickname,))
    return dict(row) if row else None


# ---- 管理员账号 ----
def get_admin_account(username: str):
    row = _exec_read_one(
        "SELECT * FROM admin_accounts WHERE username=? AND is_active=1", (username,)
    )
    return dict(row) if row else None


def create_admin_account(username: str, password_hash: str, role: str = "admin", created_by: str = ""):
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO admin_accounts (username, password_hash, role, created_by) VALUES (?,?,?,?)",
        (username, password_hash, role, created_by),
    )
    conn.commit()


def delete_admin_account(username: str):
    conn = _get_conn()
    conn.execute("DELETE FROM admin_accounts WHERE username=?", (username,))
    conn.commit()


def list_admin_accounts() -> list:
    rows = _exec_read(
        "SELECT id, username, password_hash, role, created_by, created_at, is_active FROM admin_accounts ORDER BY id"
    )
    return [dict(r) for r in rows]


# ---- 管理员登录日志 ----
def log_admin_login(username: str, user_id: str = "", nickname: str = "", ip: str = "", fingerprint: str = ""):
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO admin_login_logs (username, user_id, nickname, ip, fingerprint) VALUES (?,?,?,?,?)",
            (username, user_id, nickname, ip, fingerprint),
        )
        conn.commit()
    except Exception as e:
        print(f"  [DB] log_admin_login 失败: {e}")


def get_admin_login_logs(limit: int = 50) -> list:
    rows = _exec_read(
        "SELECT * FROM admin_login_logs ORDER BY id DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in rows]


# ---- 管理员操作日志 ----
def log_admin_action(admin_id: str, admin_name: str, action_type: str, detail: str = ""):
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO admin_actions (admin_id, admin_name, action_type, detail) VALUES (?,?,?,?)",
            (admin_id, admin_name, action_type, detail),
        )
        conn.commit()
    except Exception as e:
        print(f"  [DB] log_admin_action 失败: {e}")


# ---- 用户警告 ----
def add_warning(target_user_id: str, message: str, issued_by: str = "", target_name: str = "", target_ip: str = ""):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO user_warnings (target_user_id, message, issued_by, target_name, target_ip) VALUES (?,?,?,?,?)",
        (target_user_id, message, issued_by, target_name, target_ip),
    )
    conn.commit()


def get_pending_warnings(user_id: str) -> list:
    rows = _exec_read(
        "SELECT * FROM user_warnings WHERE target_user_id=? AND read=0 ORDER BY id DESC",
        (user_id,),
    )
    return [dict(r) for r in rows]


def mark_warnings_read(user_id: str):
    conn = _get_conn()
    conn.execute("UPDATE user_warnings SET read=1 WHERE target_user_id=?", (user_id,))
    conn.commit()


def get_all_warnings(limit: int = 50) -> list:
    rows = _exec_read("SELECT * FROM user_warnings ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def delete_warning(warn_id: int):
    conn = _get_conn()
    conn.execute("DELETE FROM user_warnings WHERE id=?", (warn_id,))
    conn.commit()


# ---- 用户搜索 ----
def get_all_users(limit: int = 500) -> list:
    rows = _exec_read(
        "SELECT * FROM registered_nicknames ORDER BY last_seen DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in rows]


def search_users(query: str) -> list:
    like = f"%{query}%"
    rows = _exec_read(
        "SELECT DISTINCT * FROM registered_nicknames WHERE nickname LIKE ? OR user_id LIKE ? OR ip LIKE ? ORDER BY last_seen DESC LIMIT 50",
        (like, like, like),
    )
    return [dict(r) for r in rows]


# ---- 恢复码系统 ----
def generate_recovery_code(user_id: str, nickname: str, ip: str = "") -> str:
    """为用户生成恢复码。如果已有则返回旧的。"""
    import secrets
    conn = _get_conn()
    # 检查是否已有恢复码
    existing = conn.execute(
        "SELECT recovery_code FROM recovery_codes WHERE user_id=?", (user_id,)
    ).fetchone()
    if existing:
        return existing["recovery_code"]
    # 生成唯一恢复码（8位大写字母数字）
    while True:
        code = secrets.token_hex(4).upper()
        dup = conn.execute(
            "SELECT 1 FROM recovery_codes WHERE recovery_code=?", (code,)
        ).fetchone()
        if not dup:
            break
    conn.execute(
        "INSERT INTO recovery_codes (user_id, nickname, ip, recovery_code) VALUES (?,?,?,?)",
        (user_id, nickname, ip, code),
    )
    conn.commit()
    return code


def verify_recovery_code(code: str):
    """验证恢复码，返回绑定的用户信息 dict 或 None"""
    row = _exec_read_one(
        "SELECT user_id, nickname, ip FROM recovery_codes WHERE recovery_code=?",
        (code.strip().upper(),),
    )
    return dict(row) if row else None


def get_recovery_code_by_user_id(user_id: str) -> str:
    """根据 user_id 获取恢复码"""
    row = _exec_read_one(
        "SELECT recovery_code FROM recovery_codes WHERE user_id=?", (user_id,)
    )
    return row["recovery_code"] if row else ""


def check_ip_registered(ip: str) -> str:
    """检查 IP 是否已注册过账号，返回已注册的 user_id 或空字符串"""
    if not ip:
        return ""
    row = _exec_read_one(
        "SELECT user_id FROM recovery_codes WHERE ip=? ORDER BY id DESC LIMIT 1", (ip,)
    )
    return row["user_id"] if row else ""


def get_all_recovery_codes(limit: int = 100) -> list:
    """获取所有恢复码列表（管理员用）"""
    rows = _exec_read(
        "SELECT * FROM recovery_codes ORDER BY id DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in rows]


def change_nickname(user_id: str, old_nickname: str, new_nickname: str, ip: str = "") -> tuple:
    """修改昵称，返回 (success: bool, message: str)
    规则：30天内只能修改一次
    """
    from datetime import datetime, timedelta
    conn = _get_conn()
    # 检查新昵称是否被占用
    existing = conn.execute(
        "SELECT user_id FROM registered_nicknames WHERE nickname=?", (new_nickname,)
    ).fetchone()
    if existing and existing["user_id"] != user_id:
        return False, "该昵称已被其他用户使用"
    # 检查冷却时间
    row = conn.execute(
        "SELECT last_change_at FROM registered_nicknames WHERE user_id=?", (user_id,)
    ).fetchone()
    if row and row["last_change_at"]:
        try:
            last_change = datetime.strptime(row["last_change_at"], "%Y-%m-%d %H:%M:%S")
            cooldown_end = last_change + timedelta(days=30)
            now = datetime.now()
            if now < cooldown_end:
                remaining = (cooldown_end - now).days
                return False, f"昵称修改冷却中，还需等待 {remaining} 天"
        except Exception:
            pass
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 更新 registered_nicknames 表
    conn.execute(
        "INSERT OR REPLACE INTO registered_nicknames (nickname, user_id, ip, last_change_at, last_seen) VALUES (?,?,?,?,?)",
        (new_nickname, user_id, ip, now_str, now_str),
    )
    # 删除旧昵称记录（如果不是新昵称）
    if old_nickname and old_nickname != new_nickname:
        conn.execute(
            "DELETE FROM registered_nicknames WHERE nickname=? AND user_id=?",
            (old_nickname, user_id),
        )
    conn.commit()
    return True, "昵称修改成功"


def get_all_active_players() -> list:
    from app.game.manager import game_manager
    results = []
    for pid, p in game_manager.players.items():
        results.append({
            "id": p.id,
            "name": p.name,
            "ip": p.ip,
            "user_id": p.user_id,
            "is_admin": getattr(p, "is_admin", False),
            "in_game": bool(p.room_id),
        })
    return results


# ---- 聊天大厅 ----
def chat_save_message(msg_id: str, user_id: str, nickname: str, content: str,
                      msg_type: str = "text", reply_to: str = "") -> bool:
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO chat大厅_messages (msg_id, user_id, nickname, content, msg_type, reply_to) VALUES (?,?,?,?,?,?)",
            (msg_id, user_id, nickname, content, msg_type, reply_to),
        )
        conn.commit()
        return True
    except Exception:
        return False


def chat_get_history(limit: int = 100, before_id: str = "") -> list:
    conn = _get_conn()
    if before_id:
        rows = conn.execute(
            "SELECT * FROM chat大厅_messages WHERE recalled=0 AND id < (SELECT id FROM chat大厅_messages WHERE msg_id=?) ORDER BY id DESC LIMIT ?",
            (before_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM chat大厅_messages WHERE recalled=0 ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result = [dict(r) for r in rows]
    result.reverse()
    return result


def chat_recall_message(msg_id: str, user_id: str, force_admin: bool = False) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT user_id, nickname, content FROM chat大厅_messages WHERE msg_id=?", (msg_id,)).fetchone()
    if not row:
        return False
    if row["user_id"] != user_id and not force_admin:
        return False
    conn.execute("UPDATE chat大厅_messages SET recalled=1 WHERE msg_id=?", (msg_id,))
    conn.commit()
    return True


def chat_get_message_by_id(msg_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM chat大厅_messages WHERE msg_id=?", (msg_id,)).fetchone()
    return dict(row) if row else None


def chat_save_notification(target_user_id: str, from_nickname: str, noti_type: str,
                           msg_id: str, message_content: str = "") -> bool:
    """保存离线通知（@提及/回复）"""
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO chat_lobby_notifications (target_user_id, from_nickname, noti_type, msg_id, message_content) VALUES (?,?,?,?,?)",
            (target_user_id, from_nickname, noti_type, msg_id, message_content),
        )
        conn.commit()
        return True
    except Exception:
        return False


def chat_get_pending_notifications(user_id: str) -> list:
    """获取未送达的离线通知"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM chat_lobby_notifications WHERE target_user_id=? AND delivered=0 ORDER BY id",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def chat_mark_notifications_delivered(user_id: str):
    """标记通知为已送达"""
    conn = _get_conn()
    conn.execute(
        "UPDATE chat_lobby_notifications SET delivered=1 WHERE target_user_id=? AND delivered=0",
        (user_id,),
    )
    conn.commit()


def chat_get_total_count() -> int:
    """获取聊天大厅消息总数"""
    conn = _get_conn()
    row = conn.execute("SELECT count(*) FROM chat大厅_messages").fetchone()
    return row[0] if row else 0


def chat_clear_all_messages() -> bool:
    """清空聊天大厅所有消息和通知"""
    try:
        conn = _get_conn()
        conn.execute("DELETE FROM chat大厅_messages")
        conn.execute("DELETE FROM chat_lobby_notifications")
        conn.commit()
        return True
    except Exception:
        return False
