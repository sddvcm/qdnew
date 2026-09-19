# -*- coding: utf-8 -*-
"""黄瓜吧插件测试（plugins/huangguaba.py）

起一个**本地模拟服务器**完整模拟站点行为：
  - GET  /login.php      登录页（内嵌算术验证码题目，绑定 session）
  - GET  /api/captcha.php 验证码题目 JSON
  - POST /login.php      校验 用户名/密码/验证码，成功种登录态
  - GET  /               登录/未登录两种首页
  - POST /api/user.php   action=sign 签到（401 未登录 / 成功 / 已签到）
  - 可选：签到接口强制要求 csrf_token（验证插件的自动重试路径）

⚠️ mock 教训（之前踩过）：**mock 要真的校验**——本服务器真的验证算术答案、
真的检查登录态，这样才能测出插件逻辑错误。
"""
import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILS.append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {extra}")


# ---------------- 本地模拟服务器 ----------------
STATE = {
    "question": "19 - 15",          # 展示给插件的题目
    "answer": 4,                    # 服务端认可的正确答案
    "username": "testuser",
    "password": "testpass",
    "require_csrf": False,          # 签到接口是否强制要求 csrf_token
    "sessions": {},                 # sid -> {"logged":bool,"signed":bool}
    "csrf": "CSRF-TOKEN-XYZ",
}
LOCK = threading.Lock()

PAGE_LOGGED_OUT = """<html><head><title>黄瓜吧</title></head><body>
<nav><a href="/login.php" class="nav-link">登录</a>
<a href="/register.php" class="nav-link nav-btn">注册</a></nav>
<div class="content">首页内容</div></body></html>"""

PAGE_LOGGED_IN = """<html><head><title>黄瓜吧</title></head><body>
<nav><a href="/logout.php">退出</a></nav>
<div class="card"><h3>每日签到</h3><div id="calendar"></div>
<a href="/sign.php">去签到 →</a></div>
<script>const CSRF_TOKEN = 'CSRF-TOKEN-XYZ';</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):              # 静默访问日志
        pass

    def _sid(self):
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "jr_session":
                return v
        sid = "sid-%d" % len(STATE["sessions"])
        with LOCK:
            STATE["sessions"][sid] = {"logged": False, "signed": False}
        return sid

    def _send(self, code, body, ctype="text/html; charset=utf-8",
              set_cookie=None, location=None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if set_cookie:
            self.send_header("Set-Cookie", "jr_session=%s; Path=/" % set_cookie)
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj, set_cookie=None):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8", set_cookie)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        ct = self.headers.get("Content-Type", "")
        if "json" in ct:
            return json.loads(raw or "{}")
        return parse_qs(raw)

    def do_GET(self):
        sid = self._sid()
        with LOCK:
            st = STATE["sessions"].setdefault(sid, {"logged": False,
                                                    "signed": False})
        if self.path.startswith("/login.php"):
            self._send(200,
                       '<span class="captcha-question" id="captchaQ">'
                       + STATE["question"] + " = ?</span>"
                       + PAGE_LOGGED_OUT, set_cookie=sid)
        elif self.path.startswith("/api/captcha.php"):
            self._json(200, {"question": STATE["question"] + " = ?",
                             "hint": "请输入计算结果"}, set_cookie=sid)
        elif self.path == "/" or self.path.startswith("/?"):
            self._send(200, PAGE_LOGGED_IN if st["logged"] else PAGE_LOGGED_OUT,
                       set_cookie=sid)
        else:
            self._send(404, "not found")

    def do_POST(self):
        sid = self._sid()
        with LOCK:
            st = STATE["sessions"].setdefault(sid, {"logged": False,
                                                    "signed": False})
        body = self._body()
        if isinstance(body, dict) and all(
                isinstance(v, list) for v in body.values()):   # urlencoded
            body = {k: v[0] for k, v in body.items()}

        if self.path.startswith("/login.php"):
            with LOCK:
                ok_cap = (str(body.get("captcha", "")).strip()
                          == str(STATE["answer"]))
                ok_user = (body.get("username") == STATE["username"]
                           and body.get("password") == STATE["password"])
            if not ok_cap:
                self._send(200, "<html>提示信息：验证码错误，请重试</html>",
                           set_cookie=sid)
            elif not ok_user:
                self._send(200, "<html>提示信息：用户名或密码错误</html>",
                           set_cookie=sid)
            else:
                with LOCK:
                    st["logged"] = True
                self._send(302, "", location="/", set_cookie=sid)
            return

        if self.path.startswith("/api/user.php"):
            if not st["logged"]:
                self._json(401, {"success": False, "message": "请先登录"})
                return
            if body.get("action") != "sign":
                self._json(400, {"success": False, "message": "未知操作"})
                return
            if STATE["require_csrf"] and \
                    body.get("csrf_token") != STATE["csrf"]:
                self._json(403, {"success": False,
                                 "message": "csrf token 校验失败"})
                return
            if st["signed"]:
                self._json(200, {"success": False, "message": "今日已签到"})
            else:
                st["signed"] = True
                self._json(200, {"success": True,
                                 "message": "签到成功，获得 5 个瓜子"})
            return

        self._send(404, "not found")


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = server.server_address[1]
SITE = f"http://127.0.0.1:{PORT}"
threading.Thread(target=server.serve_forever, daemon=True).start()

# ---------------- 插件导入 ----------------
import importlib  # noqa: E402
plugin_mod = importlib.import_module("plugins.huangguaba")
plugin = plugin_mod.HuangGuabaPlugin()

print("=" * 62)
print("1. 算术验证码求解")
cases = [("19 - 15 = ?", "4"), ("60 - 8 = ?", "52"), ("3 + 9 = ?", "12"),
         ("7 × 6 = ?", "42"), ("8 ÷ 2 = ?", "4"), ("12 * 3 = ?", "36"),
         ("5 + ?", None), ("", None)]
for q, want in cases:
    got = plugin_mod.solve_arithmetic(q)
    check(f"1.x {q!r} -> {want}", got == want, got)

print()
print("2. Cookie 直连签到（成功路径）")
with LOCK:
    STATE["sessions"].clear()
    STATE["require_csrf"] = False
# 先走一遍登录拿一个已登录的 sid 当"用户粘贴的 Cookie"
s0 = STATE["sessions"]
cookie_val = None
r = plugin.checkin({"site_url": SITE, "username": STATE["username"],
                    "password": STATE["password"]})
check("2.0 预备：账号密码签到成功", r.success, r.message)
# 从插件回写的 cookie 里取会话
cookie_val = r.cookie
check("2.0b 成功后回写 Cookie", bool(r.cookie), r.cookie[:60])

r = plugin.checkin({"site_url": SITE, "cookie": cookie_val})
check("2.1 Cookie 直连成功（已签到也算成功）", r.success, r.message)
check("2.2 消息为「今日已签到」", "已签到" in r.message, r.message)

print()
print("3. 账号密码自动登录签到")
with LOCK:
    STATE["sessions"].clear()          # 全新状态：未登录、未签到
r = plugin.checkin({"site_url": SITE, "username": STATE["username"],
                    "password": STATE["password"]})
check("3.1 登录+签到成功", r.success, r.message)
check("3.2 消息含奖励", "签到成功" in r.message, r.message)
check("3.3 回写 Cookie 供下次复用", bool(r.cookie))

print()
print("4. 验证码错误 → 登录失败并给出可读原因")
with LOCK:
    STATE["sessions"].clear()
    STATE["answer"] = 999              # 服务端认可的答案与题目不符
r = plugin.checkin({"site_url": SITE, "username": STATE["username"],
                    "password": STATE["password"]})
check("4.1 登录失败", not r.success, r.message)
check("4.2 报「验证码错误」", "验证码错误" in r.message, r.message)
with LOCK:
    STATE["answer"] = 4

print()
print("5. 密码错误 → 可读失败原因")
r = plugin.checkin({"site_url": SITE, "username": STATE["username"],
                    "password": "wrong-pass"})
check("5.1 登录失败", not r.success, r.message)
check("5.2 报「用户名或密码错误」", "用户名或密码错误" in r.message, r.message)

print()
print("6. Cookie 失效 → 自动回退账号密码重新登录")
with LOCK:
    STATE["sessions"].clear()
r = plugin.checkin({"site_url": SITE, "cookie": "jr_session=stale-invalid",
                    "username": STATE["username"],
                    "password": STATE["password"]})
check("6.1 回退登录后签到成功", r.success, r.message)

print()
print("7. CSRF 强制校验 → 插件提取 token 重试")
with LOCK:
    STATE["sessions"].clear()
    STATE["require_csrf"] = True
r = plugin.checkin({"site_url": SITE, "username": STATE["username"],
                    "password": STATE["password"]})
check("7.1 需要 csrf 时仍签到成功", r.success, r.message)
with LOCK:
    STATE["require_csrf"] = False

print()
print("8. 无任何认证方式 → 可读报错")
r = plugin.checkin({"site_url": SITE})
check("8.1 提示填写认证方式", not r.success and "Cookie" in r.message,
      r.message)

print()
print("9. 参数校验")
ok, msg = plugin.validate_params({})
check("9.1 无必填专属参数，校验通过", ok, msg)

print()
print("10. 插件元信息")
check("10.1 name=huangguaba", plugin.name == "huangguaba")
check("10.2 display_name=黄瓜吧", plugin.display_name == "黄瓜吧")
check("10.3 继承 BasePlugin",
      isinstance(plugin, __import__("plugins.base", fromlist=["BasePlugin"])
                 .BasePlugin))

print()
print("=" * 62)
print(f"PASS {PASS}  FAIL {FAIL}")
if FAILS:
    for f in FAILS:
        print("  -", f)
server.shutdown()
sys.exit(0 if FAIL == 0 else 1)
