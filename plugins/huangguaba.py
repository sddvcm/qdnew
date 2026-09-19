"""黄瓜吧 (huangguaba.com) 签到插件

站点特征（2026-09 实测）：
- **自研 CMS**，不是 Discuz —— 不能复用 fuliba 插件
- 登录：POST /login.php，字段 username / password / captcha
- 验证码：**算术题文本**（如 "19 - 15 = ?"），由 GET /api/captcha.php
  以 JSON 下发（{"question":...,"hint":...}），**与 session 绑定**。
  程序可直接算出答案，无需打码/OCR。
- 签到：POST /api/user.php，body `action=sign`，未登录返回
  401 {"success":false,"message":"请先登录"}，响应为干净 JSON。
- CSRF：站内接口统一用 `csrf_token` **表单字段**（页面内联 `var CSRF_TOKEN`
  给出，随登录态刷新）。签到接口若要求 token，会从已登录页面提取后重试。

⚠️ **登录失败的排查史**（v1.0 的坑，务必别踩回去）：

  1. 原本从 `id="captchaQ"` 读算术题。但**每次刷新验证码，服务端都会把
     答案重新绑定到 session**，而登录页上那个 `<span id="captchaQ">` 里的
     题目和服务端存的答案**不是同一次生成的** → 我们算出来的答案对不上。
     症状：用户日志里出现 `class="form-input" placeholder="输入计算结果"`
     这段 HTML，说明**服务端把登录页原样返回了**（验证码校验失败时不重定向，
     直接把表单页当响应体吐回来）。
     → 现在**一律以 `GET /api/captcha.php` 的 `question` 为准**，不再读页面。
     → 页面里的 `captchaQ` 只作为最后兜底（且仅在没有接口数据时用）。

  2. 失败原因必须**从响应里挑出真正的人话**。把整段 HTML 当消息输出的话，
     用户看到的就是 `s="form-input" placeholder="输入计算结果"...` 这种
     天书（`<input` 被 HTML 转义成 `&lt;input`，截断后从 `s="` 开始）。
     → 现在优先匹配「登录失败 / 验证码错误 / 密码错误 / 尝试次数」等短语，
       截断长度也放大到能带足上下文。

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
#
# ⚠️ 顺序即优先级：越靠前越可能是「真正的原因」，取到第一个命中就返回。
#    原先把「计算结果」放在第二位 —— 但它同时出现在**正常的登录表单**
#    （placeholder="输入计算结果"）里，导致验证码错误时也会命中它，
#    消息里就会冒出一段 input 标签。现在删掉了，改用更明确的短语。
_LOGIN_FAIL_MARKERS = (
    "验证码错误", "验证码不正确", "验证码已失效", "验证码失效",
    "用户名或密码错误", "密码错误", "账号或密码", "账号不存在",
    "用户不存在", "已被封禁", "账号被锁定", "尝试次数", "登录失败",
    "请重新输入", "请稍后再试", "频繁",
)

# 从失败响应里提取人话时，允许的最大长度（够带上下文，又不会刷屏）
_FAIL_SNIPPET_MAX = 60


def _human_fail_reason(html_text: str):
    """从登录失败响应里挑出**人能看懂**的一句话。

    为什么单独抽出来：服务端验证码失败时返回的是**整页登录页 HTML**，
    直接把它当消息会让任务日志显示 `s="form-input" placeholder="输入计算结果"`
    这种截断后的标签残片（用户实际反馈过，完全没法定位）。

    策略：按关键词优先级找一条带上下文的中文短语并剥掉标签；
    一条都没命中就返回 None，让调用方去用别的兜底。
    """
    if not html_text:
        return None
    text = html_text
    # 去掉 <script>/<style> 块，避免从 JS 里抠出无意义的片段
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)

    def _clean(frag: str) -> str:
        """把片段洗成纯文本。

        为什么要这么绕（三条都踩过坑）：

        ⚠️ ① `re.sub(r"<[^>]+>", "", ...)` 洗不掉**残缺标签**。
              片段从标签中间截断时（如 `form> 验证码错误`），开头的 `form>`
              没有配对的 `<`，正则匹配不到，就残留下来了。
        ⚠️ ② 用 `$` 清理行尾时要配 `re.M`。片段里含换行，非 M 模式下 `$`
              只能匹配整串末尾，挂在第二行的半截标签永远清不掉。
        ⚠️ ③ 字符类里**不能写 `\\s`**。`<[^>\\s]*$` 会把引号、空格之前的
              内容也一并对齐，实测匹配不到 `...<a href="/registe` 这种尾巴；
              正确写法是 `<[^<>]*$`（只排除 `<` 和 `>`）。
        """
        frag = re.sub(r"<[^>]+>", "", frag)          # 先吃掉完整标签
        frag = re.sub(r"^\S{0,20}>", "", frag)       # 再清行首的标签尾巴
        frag = re.sub(r"<[^<>]*$", "", frag, flags=re.M)   # 再清行尾的半截标签
        frag = html.unescape(frag)
        frag = re.sub(r"\s+", " ", frag).strip()
        return frag.strip("<>/\"'` \t")

    for marker in _LOGIN_FAIL_MARKERS:
        for m in re.finditer(re.escape(marker), text):
            a = max(0, m.start() - 30)
            b = min(len(text), m.end() + 30)
            frag = _clean(text[a:b])
            if marker in frag:
                return frag[:_FAIL_SNIPPET_MAX]
    # 兜底：抓一个「错误/失败/不正确」类的整句
    m = re.search(r"[^<>\n]{0,25}(?:错误|失败|不正确|无效)[^<>\n]{0,25}", text)
    if m:
        frag = _clean(m.group(0))
        if frag:
            return frag[:_FAIL_SNIPPET_MAX]
    return None


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
    version = "1.1"
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
        """登录：取算术验证码 → 计算 → POST login.php → 校验登录态

        ⚠️ 取题目的顺序**必须是「接口优先」**：

            ① GET /api/captcha.php  → 服务端把新题目+答案一起绑到当前 session
            ② （仅当①失败）读页面 <span id="captchaQ"> 的题目

        反过来做会踩坑（v1.0 实测）：页面上的 captchaQ 和服务端存的答案是
        **两次生成**的，刷新验证码后两者不同步，算出来的答案永远错，
        服务端就把登录页原样返回（用户看到一串 input 标签当错误消息）。
        """
        login_url = f"{site_url}/login.php"

        # 1) 打开登录页 —— 主要是为了拿到 session cookie
        try:
            r = session.get(login_url, timeout=20)
        except requests.RequestException as e:
            # 连不上就说连不上。原先把「打开登录页失败」和「取不到验证码题目」
            # 混为一谈，用户会跑去查页面结构，方向全错。
            return False, (f"无法访问站点（{site_url}）："
                           f"{type(e).__name__}，请检查网址是否正确、"
                           f"服务器能否联网")

        # 2) 取验证码题目：接口优先（session 绑定最准），页面兜底
        #    ⚠️ 三类失败必须分开报，否则用户会往错误方向排查：
        #       · 连不上（ConnectionError/Timeout）→ 网址或网络问题
        #       · 返回了但不是 JSON（JSONDecodeError）→ 站点改版 / 被反代改写
        #       · 返回了 JSON 但没 question 字段 → 站点改版
        #       原先一律报「取不到题目（页面结构可能变化）」，把网络问题也
        #       归进去，用户跑去翻页面结构，方向全错。
        question = None
        api_error = None
        try:
            r2 = session.get(f"{site_url}/api/captcha.php", timeout=15)
            try:
                question = (r2.json() or {}).get("question")
            except ValueError:
                api_error = f"接口返回非 JSON（HTTP {r2.status_code}）"
        except requests.RequestException as e:
            api_error = f"{type(e).__name__}"

        if not question:
            m = re.search(r'id="captchaQ"[^>]*>\s*([^<]+?)\s*<', r.text)
            question = m.group(1) if m else None
        if not question:
            if api_error:
                return False, (f"无法访问站点（{site_url}）：{api_error}。"
                               f"请检查网址是否正确、服务器能否联网访问该站")
            return False, (f"取不到算术验证码（{site_url}/api/captcha.php "
                           f"没有 question 字段），站点可能已改版")

        # 3) 计算答案
        answer = solve_arithmetic(question)
        if answer is None:
            return False, f"无法解析算术验证码：{question!r}"

        # 4) 提交登录
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

        # 5) 校验登录态（以首页实际状态为准，不猜响应结构）
        logged, page = self._is_logged_in(session, site_url)
        if logged:
            return True, "登录成功"

        # 6) 失败：优先从响应里挑「人话」，挑不到再给一句保守结论。
        #    ⚠️ 绝不能把整段 HTML 当消息 —— 那会显示成一串 input 标签残片。
        reason = _human_fail_reason(r.text) or _human_fail_reason(page or "")
        if reason:
            return False, f"登录失败：{reason}"
        if r.status_code >= 500:
            return False, f"登录失败：站点返回 HTTP {r.status_code}，稍后再试"
        return False, (f"登录失败（HTTP {r.status_code}）："
                       f"请确认用户名/密码正确，"
                       f"或该账号是否要求先完成验证（如 App 端）")

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
