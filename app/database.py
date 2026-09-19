"""SQLite 数据库初始化与迁移"""
import sqlite3
import os

# ⚠️ 数据目录可通过环境变量重定向。
# 默认 <项目根>/data（Docker / 本地开发行为不变）；
# 飞牛 fpk 模式下由 cmd/main 注入 CHECKIN_DATA_DIR=$TRIM_PKGVAR/data，
# 让数据库落在 fnOS 的持久化数据目录（@appdata），升级/重装应用不丢数据。
DB_DIR = os.environ.get("CHECKIN_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "checkin.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    # ⚠️ 建数据目录失败**不能让进程崩**：fpk 装机后 CHECKIN_DATA_DIR 指向
    # 共享目录，若该目录不可写（ACL 没授权、只读挂载等），makedirs 抛异常
    # 会让应用"启动即退出"，用户只看到一句"启动失败"毫无头绪（踩过）。
    # 这里显式捕获，打印清晰原因；目录真不可用时 get_db() 会给出更具体的错误。
    try:
        os.makedirs(DB_DIR, exist_ok=True)
    except OSError as e:
        print(f"[database] 无法创建数据目录 {DB_DIR}：{e}\n"
              f"[database] 请检查该目录是否可写（fpk 模式下应为 fnOS 分配的"
              f"共享目录 DATA_SHARE_PATH）。", flush=True)
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

        -- ===== 访问鉴权的会话 =====
        -- token 是一次性随机串（secrets.token_urlsafe），落库只为能校验与撤销。
        -- ⚠️ **不存明文密码** —— 密码只有 PBKDF2 哈希与盐，存 system_config 里。
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_exp ON auth_sessions(expires_at);
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
    # 鉴权会话表（v1.8.0 新增）—— 老库升级时要补，
    # 否则鉴权一开就直接 500（表不存在）。
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='auth_sessions'")
    if not cursor.fetchone():
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_auth_sessions_exp "
            "ON auth_sessions(expires_at)")
