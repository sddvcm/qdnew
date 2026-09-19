"""黄瓜吧 (huangguaba.com) 签到插件

站点特征（2026-09 实测）：
- **自研 CMS**，不是 Discuz —— 不能复用 fuliba 插件
- 登录：POST /login.php，字段 username / password / captcha
- 验证码：**算术题文本**（如 "19 - 15 = ?"），由 GET /api/captcha.php
  以 JSON 下发（{"question":...,"hint":...}），**与 session 绑定**。
  程序可直接算出答案，无需打码/OCR。
- 签到：POST /api/user.php，body `action=sign`，未登录返回
  401 {"success":false,"message":"请先登录"}，响应为干净 JSON。
- CSRF：站内部分接口使用 CSRF_TOKEN（页面 JS 内联），签到接口若要求
  token，会从首页 HTML 提取后重试一次。

两种认证方式（与 fuliba 插件一致）：
- Cookie 直连：粘贴浏览器 Cookie，失效自动回退账号密码登录
- 账号密码：自动过算术验证码，登录成功后回写 Cookie 供下次复用
"""
import html
import re
import requests

from plugins.base import BasePlugin, CheckinResult

BASE_URL = "https://www.huangguaba.com"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 已知的登录失败关键词（响应 HTML 里出现即认为登录失败）
_LOGIN_FAIL_MARKERS = (
    "验证码错误", "验证码不正确", "计算结果", "用户名或密码错误",
    "密码错误", "账号不存在", "用户不存在", "已被封禁", "尝试次数",
)


def _parse_cookies(text: str) -> dict:
    """把浏览器复制的 Cookie 字符串解析成 dict"""
    jar = {}
    for part in (text or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        jar[k.strip()] = v.strip()
    return jar


def _cookie_to_str(session) -> str:
    """把 session 里的 Cookie 拼回字符串（回写给引擎存档）"""
    return "; ".join(f"{k}={v}" for k, v in session.cookies.get_dict().items())


def solve_arithmetic(question: str):
    """解算术验证码。

    支持 "A + B"、"A - B"、"A × B"、"A ÷ B"（含 x/*/÷/ 变体），
    格式如 "19 - 15 = ?"。返回字符串答案；解析失败返回 None。
    """
    q = html.unescape(question or "")
    q = q.replace("？", "?").replace("=", "").strip()
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*([+\-x×*÷/])\s*(-?\d+(?:\.\d+)?)", q)
    if not m:
        return None
    a = float(m.group(1))
    op = m.group(2)
    b = float(m.group(3))
    try:
        if op == "+":
            v = a + b
        elif op == "-":
            v = a - b
        elif op in ("×", "x", "*"):
            v = a * b
        else:                                   # ÷ 或 /
            v = a / b
    except (ZeroDivisionError, ValueError):
        return None
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return str(round(v, 2))


def _extract_csrf(page_html: str):
    """从页面 JS 里提取 CSRF_TOKEN（站内部分接口要求）"""
    m = (re.search(r"CSRF_TOKEN\s*=\s*['\"]([^'\"]+)['\"]", page_html)
         or re.search(r"csrf[_-]?token['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]",
                      page_html, re.I))
    return m.group(1) if m else None


class HuangGuabaPlugin(BasePlugin):
    name = "huangguaba"
    display_name = "黄瓜吧"
    description = ("黄瓜吧 (huangguaba.com) 每日签到。支持 Cookie 直连或账号密码"
                   "（自动过算术验证码，登录后回写 Cookie 复用）。")
    version = "1.0"
    plugin_type = "http"
    author = "checkin-system"

    # 认证用通用字段 site_url/username/password/cookie 即可，无需专属表单
    form_schema = []

    # ------------------------------------------------------------------
    def checkin(self, config):
        params = config.get("params", {}) or {}
        site_url = (config.get("site_url", "") or params.get("site_url")
                    or BASE_URL).rstrip("/")
        cookie = config.get("cookie", "") or params.get("cookie", "")
        username = config.get("username", "")
        password = config.get("password", "")

        session = requests.Session()
        session.headers.update(HEADERS)

        # 方式一：Cookie 直连
        if cookie:
            session.cookies.update(_parse_cookies(cookie))
            result = self._do_sign(session, site_url)
            if result.success or result.extra.get("error_type") != "cookie_expired" \
                    or not (username and password):
                if result.success:
                    result.cookie = _cookie_to_str(session)
                return result
            # Cookie 失效且有账号密码 → 回退登录

        # 方式二：账号密码登录（自动过算术验证码）
        if username and password:
            ok, msg = self._do_login(session, site_url, username, password)
            if not ok:
                return CheckinResult(False, msg, {"error_type": "login_failed"})
            result = self._do_sign(session, site_url)
            if result.success:
                result.cookie = _cookie_to_str(session)   # 回写复用
            return result

        return CheckinResult(False, "请填写 Cookie 或 用户名+密码 任意一种认证方式",
                             {"error_type": "no_auth"})

    # ------------------------------------------------------------------
    def _is_logged_in(self, session, site_url):
        """访问首页判断登录态：未登录时有明显的 登录/注册 导航链接"""
        try:
            r = session.get(f"{site_url}/", timeout=20)
        except requests.RequestException as e:
            return False, f"访问首页失败：{e}"
        if r.status_code != 200:
            return False, f"首页返回 {r.status_code}"
        h = r.text
        # 未登录特征：导航栏有「登录」「注册」链接
        logged_out = re.search(r'href="[^"]*/login\.php"[^>]*>\s*登录\s*<', h) \
            or re.search(r'href="[^"]*/register\.php"[^>]*>\s*注册\s*<', h)
        logged_in = ("每日签到" in h) or ("退出" in h) or ("logout" in h.lower())
        return (logged_in and not logged_out), h

    # ------------------------------------------------------------------
    def _do_login(self, session, site_url, username, password):
        """登录：取算术验证码 → 计算 → POST login.php → 校验登录态"""
        login_url = f"{site_url}/login.php"

        # 1) 打开登录页：拿到 session 绑定的验证码题目
        try:
            r = session.get(login_url, timeout=20)
        except requests.RequestException as e:
            return False, f"打开登录页失败：{e}"
        m = re.search(r'id="captchaQ"[^>]*>\s*([^<]+?)\s*<', r.text)
        question = m.group(1) if m else None

        # 兜底：页面上没有题目时走接口（同样绑定 session）
        if not question:
            try:
                r2 = session.get(f"{site_url}/api/captcha.php", timeout=15)
                question = (r2.json() or {}).get("question")
            except Exception:                       # noqa: BLE001
                question = None
        if not question:
            return False, "取不到算术验证码题目（页面结构可能变化）"

        # 2) 计算答案
        answer = solve_arithmetic(question)
        if answer is None:
            return False, f"无法解析算术验证码：{question!r}"

        # 3) 提交登录
        try:
            r = session.post(
                login_url,
                data={"username": username, "password": password,
                      "captcha": answer},
                headers={"Referer": login_url,
                         "Origin": site_url},
                timeout=20, allow_redirects=True)
        except requests.RequestException as e:
            return False, f"登录请求失败：{e}"

        # 4) 校验登录态（以首页实际状态为准，不猜响应结构）
        logged, page = self._is_logged_in(session, site_url)
        if logged:
            return True, "登录成功"

        # 从登录响应或首页提取可读的失败原因
        for src in (r.text, page or ""):
            for marker in _LOGIN_FAIL_MARKERS:
                if marker in src:
                    # 尽量带上前后文，方便定位（如"验证码错误"）
                    m2 = re.search(r"[^><]{0,30}" + re.escape(marker) +
                                   r"[^><]{0,30}", src)
                    return False, f"登录失败：{m2.group(0).strip() if m2 else marker}"
        return False, f"登录失败（HTTP {r.status_code}），请检查用户名/密码"

    # ------------------------------------------------------------------
    def _do_sign(self, session, site_url):
        """签到：POST /api/user.php action=sign"""
        sign_url = f"{site_url}/api/user.php"
        headers = {"Referer": f"{site_url}/",
                   "X-Requested-With": "XMLHttpRequest"}

        def _post(extra=None):
            data = {"action": "sign"}
            if extra:
                data.update(extra)
            return session.post(sign_url, data=data, headers=headers, timeout=20)

        try:
            r = _post()
        except requests.RequestException as e:
            return CheckinResult(False, f"签到请求失败：{e}",
                                 {"error_type": "network"})

        # 401 = 未登录（Cookie 失效）
        if r.status_code == 401:
            return CheckinResult(False, "登录态已失效，请重新登录",
                                 {"error_type": "cookie_expired"})

        # 接口要求 CSRF token 时：从首页提取后重试一次
        if ("csrf" in r.text.lower() or "token" in r.text.lower()) \
                and r.status_code in (400, 403):
            try:
                _, page = self._is_logged_in(session, site_url)
                token = _extract_csrf(page or "")
            except Exception:                       # noqa: BLE001
                token = None
            if token:
                r = _post({"csrf_token": token})

        # 解析 JSON 响应
        try:
            data = r.json()
        except ValueError:
            return CheckinResult(
                False, f"签到接口返回非 JSON（HTTP {r.status_code}），站点可能改版",
                {"error_type": "unexpected"})

        msg = (data.get("message") or data.get("msg") or "").strip()
        if data.get("success"):
            return CheckinResult(True, msg or "签到成功", {"raw": data})

        # 「今日已签到」视为成功（重复执行定时任务的正常情况）
        if any(w in msg for w in ("已签到", "已签", "重复签到", "already")):
            return CheckinResult(True, msg or "今日已签到", {"already": True,
                                                            "raw": data})

        # 登录态类错误归类，供上层回退账号密码
        if r.status_code == 401 or "登录" in msg:
            return CheckinResult(False, msg or "请先登录",
                                 {"error_type": "cookie_expired"})

        return CheckinResult(False, msg or f"签到失败（HTTP {r.status_code}）",
                             {"raw": data})
