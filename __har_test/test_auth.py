"""访问鉴权（密码保护）全流程测试 —— 走真实 Flask test_client。

⚠️ 这层鉴权是**安全边界**，测试必须覆盖「能被绕过的路径」：
   - 未登录时，所有业务 API 必须 401 且**不含任何数据**
   - 未登录时，页面必须跳登录页（不能把内容先吐出来再跳）
   - 静态资源必须放行（否则登录页自己都没样式，等于把人锁死）
   - 改密码必须校验旧密码（否则会话被劫持就能一键夺权）
   - 关开关必须校验密码（关掉 = 整站敞开，是高危操作）
   - 改密码后旧会话必须失效
   - `?next=` 不能变成开放重定向（`//evil.com` 要挡掉）

测试要点：用**临时库**，绝不碰真实 data/checkin.db。
"""
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# ⚠️ 必须在 import app 之前设好数据目录：database.py 在模块加载时就
#    把 DB_DIR 定死了，之后再改环境变量没用。
TMP = tempfile.mkdtemp(prefix="auth_test_")
os.environ["CHECKIN_DATA_DIR"] = TMP
os.environ["CHECKIN_SECRET_KEY"] = "test-secret-key-for-auth"

PASS, FAIL = 0, 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILS.append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  | {exc(extra)}" if extra else ""))


def exc(v):
    return str(v)[:150]


def build_app():
    """造一个最小应用：只装鉴权 + 一个假业务路由。

    不加载真实 create_app() —— 那会拉起 APScheduler、扫插件、连飞牛 API，
    测试跑得慢还容易被环境干扰。这里只拼装被鉴权保护所必需的部分。
    """
    from flask import Flask, jsonify

    from app import auth
    import app.routes.auth_api as auth_api
    from app.database import init_db

    init_db()

    a = Flask(__name__,
              template_folder=os.path.join(ROOT, "templates"),
              static_folder=os.path.join(ROOT, "app", "static"))
    a.secret_key = "test-session-key"
    a.config["TESTING"] = True
    a.config["TEMPLATES_AUTO_RELOAD"] = True

    from datetime import timedelta
    a.permanent_session_lifetime = timedelta(days=auth.SESSION_DAYS)

    a.before_request(auth.check_auth)
    a.register_blueprint(auth_api.bp)

    # 假业务路由 —— 用来验证「未登录时拿不到任何东西」
    @a.route("/api/tasks")
    def fake_tasks():
        return jsonify({"secret": "任务列表内容"})

    @a.route("/")
    def fake_index():
        return "<h1>任务列表页面</h1>"

    return a


def fresh_client(app):
    """新客户端（清空 cookie）—— 模拟「另一个浏览器/攻击者」"""
    return app.test_client()


def login(c, pw):
    return c.post("/api/auth/login", json={"password": pw})


def main():
    app = build_app()

    # ============================================================
    print("\n--- 1. 默认状态：鉴权开启，默认密码 123456 ---")
    c = fresh_client(app)
    r = c.get("/api/auth/status")
    d = r.get_json()
    check("1.1 默认开启鉴权", d.get("enabled") is True, d)
    check("1.2 标记为默认密码", d.get("is_default_password") is True, d)
    check("1.3 未登录时 authed=False", d.get("authed") is False, d)

    print("\n--- 2. 未登录：业务接口必须被挡住 ---")
    c = fresh_client(app)
    r = c.get("/api/tasks")
    check("2.1 业务 API 返回 401", r.status_code == 401, r.status_code)
    body = r.get_data(as_text=True)
    check("2.2 401 响应体不含业务数据", "任务列表内容" not in body, body[:80])
    check("2.3 401 响应是 JSON（前端好处理）",
          (r.get_json() or {}).get("error") == "unauthorized", body[:100])

    print("\n--- 3. 未登录：页面必须跳登录页 ---")
    c = fresh_client(app)
    r = c.get("/", follow_redirects=False)
    check("3.1 页面 302 跳转", r.status_code == 302, r.status_code)
    check("3.2 跳到 /login", "/login" in (r.headers.get("Location") or ""),
          r.headers.get("Location"))
    check("3.3 响应体不含页面内容", "任务列表页面" not in r.get_data(as_text=True))
    # 带上原地址，登录后能跳回来
    r2 = c.get("/?a=1", follow_redirects=False)
    check("3.4 next 带上了原地址",
          "next=" in (r2.headers.get("Location") or ""),
          r2.headers.get("Location"))

    print("\n--- 4. 静态资源必须放行（否则登录页自己没样式） ---")
    c = fresh_client(app)
    for p in ("/static/css/app.css", "/static/js/app.js"):
        r = c.get(p, follow_redirects=False)
        check(f"4.x {p} 不跳转", r.status_code != 302, r.status_code)
    r = c.get("/login")
    check("4.5 登录页本身可访问", r.status_code == 200, r.status_code)
    check("4.6 登录页提示默认密码", "123456" in r.get_data(as_text=True))

    print("\n--- 5. 登录 ---")
    c = fresh_client(app)
    r = login(c, "wrong-password")
    check("5.1 错密码 401", r.status_code == 401, r.status_code)
    check("5.2 错密码不放行后续请求",
          c.get("/api/tasks").status_code == 401)
    r = login(c, "123456")
    check("5.3 默认密码登录成功", r.status_code == 200 and r.get_json()["success"],
          r.get_data(as_text=True)[:80])
    r = c.get("/api/tasks")
    check("5.4 登录后能访问业务 API", r.status_code == 200, r.status_code)
    check("5.5 拿得到真实数据",
          (r.get_json() or {}).get("secret") == "任务列表内容")
    r = c.get("/")
    check("5.6 登录后能打开页面", r.status_code == 200 and
          "任务列表页面" in r.get_data(as_text=True), r.status_code)

    print("\n--- 6. 会话隔离：另一个客户端拿不到 ---")
    a2 = fresh_client(app)
    check("6.1 未登录客户端仍被挡", a2.get("/api/tasks").status_code == 401)

    print("\n--- 7. 改密码 ---")
    c = fresh_client(app)
    login(c, "123456")
    r = c.post("/api/auth/password", json={
        "old_password": "wrong", "new_password": "newpass123",
        "confirm_password": "newpass123"})
    check("7.1 旧密码错误 → 401", r.status_code == 401, r.status_code)
    check("7.2 错误提示明确",
          "当前密码错误" in (r.get_json() or {}).get("message", ""),
          r.get_data(as_text=True)[:80])

    r = c.post("/api/auth/password", json={
        "old_password": "123456", "new_password": "abc",
        "confirm_password": "abc"})
    check("7.3 新密码过短被拒", r.status_code == 400, r.status_code)

    r = c.post("/api/auth/password", json={
        "old_password": "123456", "new_password": "newpass123",
        "confirm_password": "different"})
    check("7.4 两次不一致被拒", r.status_code == 400, r.status_code)

    r = c.post("/api/auth/password", json={
        "old_password": "123456", "new_password": "123456",
        "confirm_password": "123456"})
    check("7.5 不许改回默认密码", r.status_code == 400, r.status_code)

    r = c.post("/api/auth/password", json={
        "old_password": "123456", "new_password": "newpass123",
        "confirm_password": "newpass123"})
    check("7.6 改密码成功", r.status_code == 200 and r.get_json()["success"],
          r.get_data(as_text=True)[:80])
    check("7.7 改完自己没被踢下线",
          c.get("/api/tasks").status_code == 200)

    print("\n--- 8. 改密码后：旧密码失效、新密码可用 ---")
    c3 = fresh_client(app)
    check("8.1 旧密码不再能登录",
          login(c3, "123456").status_code == 401)
    c4 = fresh_client(app)
    check("8.2 新密码能登录",
          login(c4, "newpass123").status_code == 200)
    r = c4.get("/api/auth/status")
    check("8.3 状态显示已非默认密码",
          r.get_json().get("is_default_password") is False, r.get_json())

    print("\n--- 9. 改密码让**其他**会话失效 ---")
    # 造一个旧会话
    old = fresh_client(app)
    login(old, "newpass123")
    check("9.1 旧会话此时有效", old.get("/api/tasks").status_code == 200)
    # 用另一个客户端改密码
    other = fresh_client(app)
    login(other, "newpass123")
    other.post("/api/auth/password", json={
        "old_password": "newpass123", "new_password": "thirdpass9",
        "confirm_password": "thirdpass9"})
    check("9.2 改密码后旧会话失效（401）",
          old.get("/api/tasks").status_code == 401,
          old.get("/api/tasks").status_code)

    print("\n--- 10. 开关密码保护 ---")
    c = fresh_client(app)
    login(c, "thirdpass9")
    r = c.post("/api/auth/toggle", json={"enabled": False, "password": "bad"})
    check("10.1 关开关要验密码（错的被拒）", r.status_code == 401, r.status_code)
    r = c.post("/api/auth/toggle", json={"enabled": True, "password": "thirdpass9"})
    check("10.2 开关本身可保持开启", r.status_code == 200, r.get_data(as_text=True)[:70])

    r = c.post("/api/auth/toggle", json={"enabled": False, "password": "thirdpass9"})
    check("10.3 正确密码可关闭", r.status_code == 200 and
          r.get_json().get("enabled") is False, r.get_data(as_text=True)[:70])
    anon = fresh_client(app)
    check("10.4 关闭后匿名可访问业务 API",
          anon.get("/api/tasks").status_code == 200)
    check("10.5 关闭后匿名可访问页面",
          anon.get("/").status_code == 200)

    print("\n--- 11. 重新开启 ---")
    # 关掉后 session 被清，需重新登录
    c = fresh_client(app)
    login(c, "thirdpass9")
    r = c.post("/api/auth/toggle", json={"enabled": True, "password": "thirdpass9"})
    check("11.1 重新开启成功", r.status_code == 200 and
          r.get_json().get("enabled") is True, r.get_data(as_text=True)[:70])
    anon2 = fresh_client(app)
    check("11.2 开启后匿名又被挡住",
          anon2.get("/api/tasks").status_code == 401)

    print("\n--- 12. 安全：开放重定向 ---")
    c = fresh_client(app)
    r = c.get("/login?next=//evil.com", follow_redirects=False)
    # 未登录时 /login 直接渲染页面（不是跳转），换一个已登录的场景验证
    c2 = fresh_client(app)
    login(c2, "thirdpass9")
    r = c2.get("/login?next=//evil.com", follow_redirects=False)
    loc = r.headers.get("Location") or ""
    check("12.1 挡掉 //evil.com（跳回站内）",
          r.status_code == 302 and "evil.com" not in loc, f"{r.status_code} {loc}")
    r = c2.get("/login?next=/settings", follow_redirects=False)
    check("12.2 合法站内地址可跳转",
          (r.headers.get("Location") or "").endswith("/settings"),
          r.headers.get("Location"))

    print("\n--- 13. 登出 ---")
    c = fresh_client(app)
    login(c, "thirdpass9")
    check("13.1 登出前可访问", c.get("/api/tasks").status_code == 200)
    c.post("/api/auth/logout")
    check("13.2 登出后被挡", c.get("/api/tasks").status_code == 401)

    print("\n--- 14. 密码存储：不落明文 ---")
    from app.database import get_db
    db = get_db()
    rows = db.execute("SELECT key, value FROM system_config").fetchall()
    db.close()
    cfg = {r["key"]: r["value"] for r in rows}
    check("14.1 没有明文密码字段",
          not any("thirdpass9" in v for v in cfg.values()),
          [k for k, v in cfg.items() if "thirdpass9" in v])
    check("14.2 存的是 PBKDF2 哈希（64 位 hex）",
          len(cfg.get("auth_password_hash", "")) == 64
          and all(ch in "0123456789abcdef"
                  for ch in cfg.get("auth_password_hash", "")),
          cfg.get("auth_password_hash", "")[:24] + "...")
    check("14.3 有独立随机盐", len(cfg.get("auth_password_salt", "")) == 32,
          cfg.get("auth_password_salt", "")[:16] + "...")
    check("14.4 哈希不等于任何常见密码的直接摘要",
          cfg.get("auth_password_hash") != __import__("hashlib")
          .sha256(b"thirdpass9").hexdigest())

    print("\n--- 15. 会话 token 落库并可撤销 ---")
    db = get_db()
    n = db.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0]
    db.close()
    check("15.1 auth_sessions 表有记录", n >= 1, f"count={n}")

    # 清理
    shutil.rmtree(TMP, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"PASS {PASS}  FAIL {FAIL}")
    if FAILS:
        for f in FAILS:
            print("  -", f)
    print("AUTH_OK" if not FAIL else "AUTH_FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
