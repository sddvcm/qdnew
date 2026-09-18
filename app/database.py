"""SQLite 数据库初始化与迁移"""
import sqlite3
import os

DB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "checkin.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = get_db()
    cursor = conn.cursor()

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS plugins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            description TEXT DEFAULT '',
            version TEXT DEFAULT '1.0',
            plugin_type TEXT NOT NULL DEFAULT 'http',
            author TEXT DEFAULT '',
            builtin INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            form_schema TEXT NOT NULL DEFAULT '[]',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plugin_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            site_url TEXT DEFAULT '',
            username TEXT DEFAULT '',
            password TEXT DEFAULT '',
            cookie TEXT DEFAULT '',
            cron_expr TEXT DEFAULT '0 9 * * *',
            enabled INTEGER DEFAULT 1,
            status TEXT DEFAULT 'normal',
            status_message TEXT DEFAULT '',
            consecutive_fail INTEGER DEFAULT 0,
            max_fail_alert INTEGER DEFAULT 3,
            last_result TEXT DEFAULT '',
            last_message TEXT DEFAULT '',
            last_extra TEXT DEFAULT '{}',
            last_run_at TEXT,
            next_run_at TEXT,
            params TEXT NOT NULL DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (plugin_id) REFERENCES plugins(id)
        );

        CREATE TABLE IF NOT EXISTS checkin_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            checkin_date TEXT NOT NULL,
            checkin_time TEXT DEFAULT '',
            result TEXT NOT NULL,
            message TEXT DEFAULT '',
            extra_info TEXT DEFAULT '{}',
            duration_ms INTEGER DEFAULT 0,
            error_trace TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        );
        CREATE INDEX IF NOT EXISTS idx_logs_task_date ON checkin_logs(task_id, checkin_date);

        CREATE TABLE IF NOT EXISTS notify_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notify_type TEXT NOT NULL,
            name TEXT DEFAULT '',
            config TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS task_notify (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            notify_id INTEGER NOT NULL,
            on_success INTEGER DEFAULT 1,
            on_failure INTEGER DEFAULT 1,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (notify_id) REFERENCES notify_configs(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS system_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );

        -- ===== HAR 通用签到模板 =====
        -- entries 存整份模板（请求定义 + 断言 + 变量抽取规则），是 JSON 文本。
        -- 刻意**不把每个请求拆行存**：模板是一个整体，整体读整体写更简单，
        -- 且一个模板几十个请求也就几十 KB，SQLite 完全扛得住。
        CREATE TABLE IF NOT EXISTS har_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            host TEXT DEFAULT '',
            note TEXT DEFAULT '',
            entries TEXT NOT NULL DEFAULT '[]',
            variables TEXT NOT NULL DEFAULT '[]',
            builtin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );

        -- 任务与该模板自带变量的默认值绑定（同一模板可被多个任务用不同账号跑）
        CREATE INDEX IF NOT EXISTS idx_har_tpl_name ON har_templates(name);
    """)

    _migrate(conn)
    conn.commit()
    conn.close()


def _migrate(conn):
    """轻量迁移：老库缺表/缺列时补上，不重建数据"""
    cursor = conn.cursor()
    # tasks 表历史上没有 params 之外的结构化 HAR 字段，这里无需加列：
    # 插件配置一律走 params JSON（项目铁律）。仅确保新表存在即可。
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='har_templates'")
    if not cursor.fetchone():
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS har_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host TEXT DEFAULT '',
                note TEXT DEFAULT '',
                entries TEXT NOT NULL DEFAULT '[]',
                variables TEXT NOT NULL DEFAULT '[]',
                builtin INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
