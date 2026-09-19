"""访问鉴权 —— 网页端密码保护

背景：原先整个管理界面**没有任何鉴权**，只要知道地址谁都能进来改任务、
看账号配置。局域网内无妨，但一旦经反代暴露到外网就是灾难 ——
所以加了这层。

设计取舍：
- **固定默认密码 123456**（用户明确要求），首次打开就能进，不至于把自己锁在
  门外；但一旦检测到仍是默认密码，界面会**常驻醒目提示**，并在侧边导航上
  打红点，催用户去改。
- 密码用 `PBKDF2-HMAC-SHA256`（20 万轮 + 随机盐）存哈希，**不存明文**，
  与 `crypto.py` 的 AES 密钥体系无关（那个是给任务里的账号密码用的）。
- 会话是一次性随机 token 存 SQLite，客户端只拿 Cookie 名，
  **Cookie 值里不含任何可推导密码的信息**。
- 开关 `auth_enabled`：关掉即恢复「无鉴权」旧行为（给纯内网用户留退路）。

⚠️ **白名单必须精确**（`_EXEMPT_EXACT` / `_EXEMPT_PREFIX`）：漏掉一条会
   把用户挡在登录页外（比如忘了放行 `/api/auth/*`，登录请求本身就被拦），
   多放一条等于鉴权形同虚设。改动这里务必同步跑 test_auth.py。
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
import time

from flask import jsonify, redirect, request, session, url_for

# ---------------- 配置键（存 system_config 表） ----------------
KEY_ENABLED = "auth_enabled"          # "1" / "0"
KEY_HASH = "auth_password_hash"       # PBKDF2 串
KEY_SALT = "auth_password_salt"

DEFAULT_PASSWORD = "123456"
PBKDF2_ROUNDS = 200_000
SESSION_DAYS = 30

# 当前密码是不是「出厂默认」——由 check_auth() 每请求刷新，供界面提示用
IS_DEFAULT_PASSWORD = True

# ---------------- 免登录路径 ----------------
# 精确匹配
_EXEMPT_EXACT = {
    "/login",
    "/api/auth/status",
    "/api/auth/login",
    "/favicon.ico",
    "/robots.txt",
}
# 前缀匹配（静态资源必须放行，否则登录页自己都没样式）
_EXEMPT_PREFIX = (
    "/static/",
)


def is_exempt(path: str) -> bool:
    if path in _EXEMPT_EXACT:
        return True
    return any(path.startswith(p) for p in _EXEMPT_PREFIX)


# ---------------- 密码哈希 ----------------
def hash_password(password: str, salt: str = None) -> tuple:
    """返回 (hash_hex, salt_hex)。salt 传 None 时随机生成。"""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), PBKDF2_ROUNDS)
    return dk.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    if not stored_hash or not salt:
        return False
    try:
        calc, _ = hash_password(password, salt)
    except ValueError:              # salt 不是合法 hex
        return False
    # 定长比较，避免时序侧信道
    return hmac.compare_digest(calc, stored_hash)


# ---------------- system_config 读写 ----------------
def _cfg_get(db, key, default=""):
    row = db.execute("SELECT value FROM system_config WHERE key=?",
                     (key,)).fetchone()
    return (row["value"] if row else default)


def _cfg_set(db, key, value):
    db.execute(
        "INSERT INTO system_config (key, value, updated_at) "
        "VALUES (?,?,datetime('now','localtime')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=datetime('now','localtime')",
        (key, str(value)))


def is_enabled(db) -> bool:
    """鉴权开关。**没配置过时默认开启**（新装用户开箱即保护）。"""
    return _cfg_get(db, KEY_ENABLED, "1") != "0"


def current_password_is_default(db) -> bool:
    """当前密码是否等于出厂默认值 —— 用于界面上催改。"""
    stored = _cfg_get(db, KEY_HASH, "")
    salt = _cfg_get(db, KEY_SALT, "")
    if not stored or not salt:
        return True                      # 没设过 = 就是默认
    return verify_password(DEFAULT_PASSWORD, stored, salt)


def set_password(db, password: str):
    h, s = hash_password(password)
    _cfg_set(db, KEY_HASH, h)
    _cfg_set(db, KEY_SALT, s)
    # 改密码 = 让所有已登录会话失效（防止旧 Cookie 继续可用）
    db.execute("DELETE FROM auth_sessions")


def set_enabled(db, enabled: bool):
    _cfg_set(db, KEY_ENABLED, "1" if enabled else "0")


# ---------------- 安装向导设置的初始密码 ----------------
# 装机时用户在向导里填了密码，cmd/common.sh 会把它写进 $ETC/.auth-init
# （**不写进 app.env**，避免明文长期留在配置里）。
# 这里在首次启动时读一次、转成 PBKDF2 哈希落库，然后**立刻删文件**。
BOOTSTRAP_FILE = ".auth-init"


def _bootstrap_path():
    """定位 .auth-init：优先 $CHECKIN_ETC_DIR，其次从数据目录反推。

    fpk 下 etc 目录是 TRIM_PKGETC（@appconf），与数据目录（@appshare）不同，
    所以必须由 cmd/main 显式注入路径 —— 猜不出来。
    """
    p = os.environ.get("CHECKIN_ETC_DIR", "").strip()
    if p and os.path.isdir(p):
        return os.path.join(p, BOOTSTRAP_FILE)
    return ""


def apply_wizard_password():
    """首次启动时把向导里设的密码落成哈希。返回是否应用了。

    ⚠️ 三条约束：
      1. **只认首次**：库里已经有哈希就不动（用户后来自己改过密码，
         不能被一个残留的 .auth-init 覆盖回去）。
      2. **无论成败都删文件** —— 明文密码留在磁盘上是纯负债。
      3. 出错不能阻止应用启动（密码没设成顶多用默认值，还能进得去）。
    """
    path = _bootstrap_path()
    if not path or not os.path.isfile(path):
        return False
    try:
        raw = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return False
    finally:
        try:
            os.remove(path)          # ★ 明文用完即毁，别留在盘上
        except OSError:
            pass

    password = raw.strip()
    if not password:
        return False

    from app.database import get_db
    db = get_db()
    try:
        if _cfg_get(db, KEY_HASH, ""):
            return False             # 已有密码，不覆盖
        set_password(db, password)
        set_enabled(db, True)
        db.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        db.close()


# ---------------- 会话 ----------------
def create_session(db) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    db.execute(
        "INSERT INTO auth_sessions (token, created_at, expires_at) VALUES (?,?,?)",
        (token, now, now + SESSION_DAYS * 86400))
    # 顺手清过期会话，避免表无限增长
    db.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (now,))
    return token


def valid_session(token: str) -> bool:
    if not token:
        return False
    from app.database import get_db
    db = get_db()
    try:
        row = db.execute(
            "SELECT expires_at FROM auth_sessions WHERE token=?", (token,)
        ).fetchone()
    finally:
        db.close()
    return bool(row and row["expires_at"] > int(time.time()))


def drop_session(token: str):
    if not token:
        return
    from app.database import get_db
    db = get_db()
    try:
        db.execute("DELETE FROM auth_sessions WHERE token=?", (token,))
        db.commit()
    finally:
        db.close()


# ---------------- 请求钩子 ----------------
def _wants_json() -> bool:
    """这个请求该回 JSON 还是回登录页跳转？

    前后端分离的 `fetch('/api/...')` 如果被 302 到登录页，JS 那边会
    `r.json()` 解析失败、报一堆莫名其妙的错 —— 所以 API 一律回 401 JSON，
    由前端的全局拦截器统一跳登录页。
    """
    if request.path.startswith("/api/"):
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def check_auth():
    """注册为 `app.before_request`。返回非 None 即短路该请求。"""
    global IS_DEFAULT_PASSWORD

    from app.database import get_db
    db = get_db()
    try:
        enabled = is_enabled(db)
        IS_DEFAULT_PASSWORD = current_password_is_default(db)
        if not enabled or is_exempt(request.path):
            return None
        if session.get("auth_ok") and valid_session(session.get("auth_token")):
            return None
    finally:
        db.close()

    if _wants_json():
        return jsonify({"success": False, "error": "unauthorized",
                        "message": "请先登录"}), 401
    # 带上原地址，登录后跳回来
    nxt = request.full_path if request.query_string else request.path
    return redirect(url_for("auth.login_page", next=nxt))
