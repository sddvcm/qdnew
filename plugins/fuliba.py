"""福利吧论坛 (wnflb2023.com) 签到插件

认证方式（二选一，皆可）：
  1. 直接填 Cookie  —— 最简单，Cookie 失效前一直可用
  2. 填用户名 + 密码 —— 插件自动登录拿到 Cookie，并回写存档，
     之后自动复用，过期时再自动重新登录（含验证码自动识别）

验证码：新 IP / 风控触发时自动拉取验证码图片并用 ddddocr 识别，
        无需人工干预（ddddocr 在需要时才懒加载，不影响 Cookie 模式性能）。

论坛基于 Discuz! X3.4，页面为 GBK 编码，已做兼容。
"""
import re
import time
import html
import urllib.parse
import requests
from plugins.base import BasePlugin, FormField, CheckinResult

BASE_URL = "https://www.wnflb2023.com"
FORUM_URL = BASE_URL + "/forum.php"
LOGIN_PAGE_URL = BASE_URL + "/member.php?mod=logging&action=login"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
TIMEOUT = 30
CAPTCHA_ATTEMPTS = 3  # 验证码识别最多换图重试次数


def _decode(resp):
    """优先按 GBK 解码（论坛是 GBK），失败再退回 apparent_encoding"""
    if resp.encoding and resp.encoding.lower() in ("gbk", "gb2312", "gb18030"):
        return resp.text
    try:
        return resp.content.decode("gbk")
    except (UnicodeDecodeError, LookupError):
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text


def _parse_cookies(raw):
    cookies = {}
    for item in raw.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def _cookie_to_str(session):
    return "; ".join(f"{c.name}={c.value}" for c in session.cookies)


def _check_logged_in(html):
    """页面 JS 里 discuz_uid 不为 '0' 即已登录（最可靠信号）"""
    m = re.search(r"discuz_uid\s*=\s*'(\d+)'", html)
    if m:
        return m.group(1) != "0"
    if 'class="logout"' in html or "mod=logging&action=logout" in html:
        return True
    if 'name="username"' in html and 'name="password"' in html:
        return False
    return False


def _extract_formhash_pair(html):
    """从首页 HTML 提取 fx_checkin:checkin&formhash=A&B 两个 hash"""
    m = re.search(r"fx_checkin:checkin&formhash=([a-f0-9]+)&([a-f0-9]+)", html)
    if m:
        return m.group(1), m.group(2)
    m2 = re.search(r"fx_checkin:checkin&formhash=([a-f0-9]+)", html)
    if m2:
        return m2.group(1), ""
    return None, None


# ========================= 验证码处理 =========================

def _detect_captcha(html):
    """
    识别登录页/挑战页是否需要验证码，并提取关键参数。
    返回 dict: {needed, idhash, update, seccodehash, auth}
    """
    res = {
        "needed": False,
        "idhash": "",
        "update": str(int(time.time() * 1000)),
        "seccodehash": "",
        "auth": "",
    }

    # auth 令牌（二次挑战，必须随登录一起提交）
    am = re.search(r'name="auth"\s+value="([A-Za-z0-9%_./=+]+)"', html)
    if am:
        res["auth"] = am.group(1)

    # idhash：优先 updateseccode('IDHASH', ...) 或 <span id="seccode_IDHASH">
    ih = re.search(r"updateseccode\(\s*['\"]([A-Za-z0-9]+)['\"]", html)
    if not ih:
        ih = re.search(r'id="seccode_([A-Za-z0-9]+)"', html)
    if not ih:
        sm = re.search(
            r"misc\.php\?mod=seccode&update=([^&\"']+)&idhash=([A-Za-z0-9]+)", html
        )
        if sm:
            res["idhash"] = sm.group(2)
            res["update"] = sm.group(1)
    if ih and not res["idhash"]:
        res["idhash"] = ih.group(1)

    # 输入框 id 形如 seccodeverify_<idhash>
    if not res["idhash"]:
        sid = re.search(r'id="seccodeverify_([A-Za-z0-9]+)"', html)
        if sid:
            res["idhash"] = sid.group(1)
    # 隐藏域 seccodehash
    if not res["idhash"]:
        sh = re.search(r'name="seccodehash"\s+value="([A-Za-z0-9]+)"', html)
        if sh:
            res["idhash"] = sh.group(1)

    if not res["idhash"] and re.search(r'name="seccodeverify"', html):
        res["idhash"] = "SkyV"  # 极端兜底

    if res["idhash"] or res["auth"]:
        res["needed"] = True

    if res["idhash"]:
        sh = re.search(r'name="seccodehash"\s+value="([A-Za-z0-9]+)"', html)
        res["seccodehash"] = sh.group(1) if sh else res["idhash"]

    return res


def _extract_login_fields(html):
    """从登录/挑战页提取 formhash、loginhash、auth（用于二次提交）。"""
    fh = re.search(r'name="formhash"\s+value="([a-f0-9]+)"', html)
    lh = re.search(r"loginhash=([A-Za-z0-9]+)", html)
    auth = re.search(r'name="auth"\s+value="([A-Za-z0-9%_]+)"', html)
    return (
        fh.group(1) if fh else None,
        lh.group(1) if lh else None,
        auth.group(1) if auth else None,
    )


def _solve_captcha(session, cap, ocr_conf=None):
    """
    拉取验证码图片并识别（本地 ddddocr 或云码，由 ocr_conf 决定）。
    多次重试（每次换一张新图），提高识别率。
    返回 (识别结果, 错误消息)。
    """
    import captcha as captcha_mod

    ocr_conf = ocr_conf or {}
    backend = ocr_conf.get("backend") or "local"
    token = ocr_conf.get("token") or ""
    type_id = ocr_conf.get("type") or captcha_mod.JFBYM_DEFAULT_TYPE

    headers = {"Referer": LOGIN_PAGE_URL}
    last_err = ""
    for attempt in range(1, CAPTCHA_ATTEMPTS + 1):
        try:
            update = str(int(time.time() * 1000))
            img_url = (
                f"{BASE_URL}/misc.php?mod=seccode"
                f"&update={update}&idhash={cap['idhash']}"
            )
            r = session.get(img_url, timeout=TIMEOUT, headers=headers)
            if r.status_code != 200 or len(r.content) < 100:
                last_err = f"验证码图片拉取失败(HTTP {r.status_code})"
                continue
            if r.content[:4] not in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1"):
                last_err = "拉到的不是图片（可能被风控拦截）"
                continue  # 非图片（可能被拦截），换新图重试

            try:
                code, used = captcha_mod.solve(
                    r.content, backend=backend, token=token, type_id=type_id)
            except captcha_mod.CaptchaError as e:
                # ⚠️ Token 无效 / 余额不足这类配置错误直接上抛，重试无意义
                msg = str(e)
                if any(k in msg for k in ("Token", "余额", "不支持", "未配置")):
                    return None, msg
                last_err = msg
                continue

            if code:
                return code, f"（{captcha_mod.describe_backend(backend, token)}）"
        except Exception as e:
            last_err = f"识别异常: {e}"
            continue
    return None, last_err or "验证码识别失败（多次重试均无结果）"


def _verify_captcha_code(session, cap, code):
    """调用 Discuz 验证码校验接口（action=check），在 cookie 写入 seccode 标记。"""
    url = (
        f"{BASE_URL}/misc.php?mod=seccode&action=check&inajax=1"
        f"&modid=member::logging&idhash={cap['idhash']}&secverify={code}"
    )
    try:
        r = session.get(
            url, timeout=TIMEOUT,
            headers={"Referer": LOGIN_PAGE_URL, "X-Requested-With": "XMLHttpRequest"},
        )
        txt = _decode(r)
        return "succeed" in txt
    except requests.RequestException:
        return False


def _submit_login(session, formhash, loginhash, username="", password="",
                  seccode="", auth="", seccodehash="", challenge=False):
    """
    执行一次登录 POST。返回 (ok, msg, resp_html)。
    challenge=True 时按 Discuz 二次验证码挑战提交（凭据已由 auth 关联，
    无需重复发送账号密码）。
    """
    if challenge:
        data = {
            "formhash": formhash,
            "referer": BASE_URL + "/",
            "auth": auth,
            "questionid": "0",
            "answer": "",
            "seccodehash": seccodehash or "",
            "seccodemodid": "member::logging",
            "seccodeverify": seccode,
        }
    else:
        data = {
            "formhash": formhash,
            "referer": BASE_URL + "/",
            "loginfield": "username",
            "username": username,
            "password": password,
            "questionid": "0",
            "answer": "",
            "cookietime": "2592000",
        }
        if seccode:
            data["seccodeverify"] = seccode
            if seccodehash:
                data["seccodehash"] = seccodehash

    login_url = (
        f"{BASE_URL}/member.php?mod=logging&action=login"
        f"&loginsubmit=yes&loginhash={loginhash}"
    )
    if challenge:
        login_url += "&inajax=1"

    try:
        r = session.post(
            login_url, data=data, timeout=TIMEOUT, allow_redirects=True,
            headers={"Referer": LOGIN_PAGE_URL},
        )
    except requests.RequestException as e:
        return False, f"登录请求异常: {e}", ""

    txt = _decode(r)
    msg = _extract_message(txt)
    if not msg:
        cdata = re.search(r"<!\[CDATA\[(.*?)\]\]>", txt, re.DOTALL)
        if cdata:
            msg = re.sub(r"<[^>]+>", "", cdata.group(1)).strip()

    # 仍被要求验证码（验证码错误等）
    if "请输入验证码" in txt and "auth=" in txt:
        return False, (msg or "验证码不正确，请重试"), txt

    # 权威校验：访问首页看 discuz_uid 是否为真实 UID
    logged, _ = _verify_login(session)
    if logged:
        return True, "登录成功", txt
    if msg and ("密码" in msg or "用户名" in msg):
        return False, f"登录失败: {msg}", txt
    return False, (msg or "登录失败（未进入登录态）"), txt


def _verify_login(session):
    """访问论坛首页，判断是否已登录。返回 (bool, html)"""
    try:
        resp = session.get(FORUM_URL, timeout=TIMEOUT, headers=HEADERS)
    except requests.RequestException:
        return False, ""
    html = _decode(resp)
    return _check_logged_in(html), html


def _do_login(session, username, password, ocr_conf=None):
    """
    账号密码登录（含新 IP 二次验证码挑战）。
    成功返回 (True, 消息)，失败返回 (False, 消息)。
    """
    try:
        resp = session.get(LOGIN_PAGE_URL, timeout=TIMEOUT, headers=HEADERS)
        page = _decode(resp)
    except requests.RequestException as e:
        return False, f"获取登录页失败: {e}"

    fh_m = re.search(r'name="formhash"\s+value="([a-f0-9]+)"', page)
    lh_m = re.search(r"loginhash=([A-Za-z0-9]+)", page)
    if not fh_m or not lh_m:
        return False, "无法解析登录页(formhash/loginhash 缺失)"
    formhash, loginhash = fh_m.group(1), lh_m.group(1)

    # 首次尝试：无验证码直接提交（老 IP / 已信任环境通常直接成功）
    ok, msg, resp_html = _submit_login(
        session, formhash, loginhash, username, password, "", "", ""
    )
    if ok:
        return True, "登录成功"

    # 被验证码挑战：从第一次提交的响应里直接解析挑战页（auth 由本次响应给出）。
    # auth 在 HTML 里可能以 %2F 形式存在（含 /），需先 unquote 还原。
    chtml = resp_html or ""
    c_fh, c_lh, auth = _extract_login_fields(chtml)
    auth = urllib.parse.unquote(auth) if auth else None
    if not auth:
        am = re.search(r"auth=([A-Za-z0-9%_./=+]+)", chtml)
        auth = urllib.parse.unquote(am.group(1)) if am else None
    if not auth:
        return False, msg  # 真失败（密码错等），msg 已是原因

    cap = _detect_captcha(chtml)
    if not (c_fh and c_lh and cap["needed"] and cap["idhash"]):
        try:
            r = session.get(
                f"{BASE_URL}/member.php",
                params={"mod": "logging", "action": "login", "auth": auth},
                timeout=TIMEOUT, headers={"Referer": LOGIN_PAGE_URL},
            )
            chtml = _decode(r)
            c_fh, c_lh, c_auth = _extract_login_fields(chtml)
            auth = urllib.parse.unquote(c_auth) if c_auth else auth
            cap = _detect_captcha(chtml)
        except requests.RequestException as e:
            return False, f"获取挑战页异常: {e}"

    if not (c_fh and c_lh):
        return False, "验证码挑战页未解析出 formhash/loginhash"
    if not cap["needed"] or not cap["idhash"]:
        return False, f"验证码挑战页未解析出验证码(idhash 缺失): {msg}"

    # 二次挑战提交（不带账号密码，凭据由 auth 关联）。最多 3 次换图重试。
    last_msg = "验证码识别失败"
    for attempt in range(1, CAPTCHA_ATTEMPTS + 1):
        code, err = _solve_captcha(session, cap, ocr_conf)
        if not code:
            return False, err or "验证码识别失败"

        # 调用验证码校验接口（设置 seccode cookie，确认识别是否正确）
        if not _verify_captcha_code(session, cap, code):
            continue  # 校验未通过，换新图重试

        ok2, last_msg, _ = _submit_login(
            session, c_fh, c_lh, username, password, code, auth,
            cap["seccodehash"], challenge=True,
        )
        if ok2:
            return True, "登录成功(已通过验证码)"

        if "验证码" in last_msg and ("不正确" in last_msg or "错误" in last_msg):
            continue  # 验证码不正确，换新图重试
        if "密码" in last_msg or "用户名" in last_msg:
            # 凭据缺失（auth 失效）：重拉挑战页获取新 auth 重试一次
            try:
                r = session.get(
                    f"{BASE_URL}/member.php",
                    params={"mod": "logging", "action": "login", "auth": auth},
                    timeout=TIMEOUT, headers={"Referer": LOGIN_PAGE_URL},
                )
                chtml = _decode(r)
                c_fh, c_lh, c_auth = _extract_login_fields(chtml)
                auth = urllib.parse.unquote(c_auth) if c_auth else auth
                cap = _detect_captcha(chtml)
            except requests.RequestException:
                pass
            continue
        return False, last_msg
    return False, last_msg


class FulibaPlugin(BasePlugin):
    name = "fuliba"
    display_name = "福利吧论坛签到"
    description = "Discuz! 论坛 fx_checkin 插件自动签到，支持账号密码登录(含验证码自动识别)或Cookie直连"
    plugin_type = "http"
    author = "checkin-system"

    # 通用字段(username/password/cookie/site_url)已在任务表单顶层，这里只放插件专属参数
    form_schema = [
        FormField(key="retry", label="重试次数", type="number", required=False,
                  default=3, help_text="登录或签到失败时最多重试几次"),
        FormField(key="ocr_backend", label="验证码识别方式", type="select", required=False,
                  default="local",
                  options=[
                      {"value": "local", "label": "本地 ddddocr（免费，无需配置）"},
                      {"value": "cloud", "label": "云码 jfbym（更准，按次计费）"},
                      {"value": "auto", "label": "本地优先，失败转云码"},
                  ],
                  help_text="只有账号密码登录且触发验证码时才会用到；Cookie 直连模式不涉及"),
        FormField(key="jfbym_token", label="云码 Token", type="password", required=False,
                  sensitive=True,
                  placeholder="选云码时必填",
                  help_text="去 www.jfbym.com 注册，用户中心复制 Token"),
        FormField(key="jfbym_type", label="云码识别类型", type="text", required=False,
                  default="10110",
                  placeholder="10110",
                  help_text="默认 10110=通用数英(≤5位)，Discuz 登录验证码用这个即可"),
    ]

    # ---------- 签到 ----------
    def _do_checkin(self, session, site_url, retry):
        for attempt in range(max(1, int(retry))):
            try:
                home = _decode(session.get(
                    FORUM_URL if site_url == BASE_URL else f"{site_url}/forum.php",
                    timeout=TIMEOUT, headers=HEADERS))
            except requests.RequestException as e:
                if attempt == max(1, int(retry)) - 1:
                    return CheckinResult(False, f"访问首页失败: {e}", {"error_type": "network"})
                continue

            if not _check_logged_in(home):
                return CheckinResult(False, "登录态失效，请重新登录或更新Cookie",
                                     {"error_type": "cookie_expired"})

            formhash, fx = _extract_formhash_pair(home)
            if not formhash:
                if attempt == max(1, int(retry)) - 1:
                    return CheckinResult(False, "无法获取签到formhash，页面结构可能已变化",
                                         {"error_type": "no_formhash"})
                continue

            url = f"{site_url}/plugin.php?id=fx_checkin:checkin&formhash={formhash}"
            if fx:
                url += f"&{fx}"
            url += "&inajax=1"
            try:
                sign = session.get(url, timeout=TIMEOUT, headers={
                    "Referer": FORUM_URL, "X-Requested-With": "XMLHttpRequest"})
            except requests.RequestException as e:
                if attempt == max(1, int(retry)) - 1:
                    return CheckinResult(False, f"签到请求失败: {e}", {"error_type": "network"})
                continue

            message = _extract_message(sign.text)
            if "签到成功" in message:
                rank = re.search(r"第\s*(\d+)\s*个", message)
                days = re.search(r"累计签到[：:]\s*<i>(\d+)</i>", sign.text)
                consecutive = re.search(r"已连续签到[：:]\s*<i>(\d+)</i>", sign.text)
                return CheckinResult(
                    True,
                    f"签到成功！今日第 {rank.group(1)} 个签到" if rank else "签到成功！",
                    {"days": int(days.group(1)) if days else 0,
                     "consecutive_days": int(consecutive.group(1)) if consecutive else 0},
                )
            if "已经签到" in message or "已签到" in message or "无需重复签到" in message:
                return CheckinResult(True, "今日已签到（重复签到）", {"already_checkin": True})
            if "请登录" in message or "先登录" in message:
                return CheckinResult(False, "Cookie已过期，请重新登录或更新Cookie",
                                     {"error_type": "cookie_expired"})
            return CheckinResult(False, message or "未知签到响应")

        return CheckinResult(False, "重试次数已用完，签到失败", {"error_type": "max_retry"})

    # ---------- 主入口 ----------
    def checkin(self, config):
        params = config.get("params", {})
        site_url = (config.get("site_url", "") or params.get("site_url", BASE_URL)).rstrip("/")
        cookie = config.get("cookie", "") or params.get("cookie", "")
        username = config.get("username", "")
        password = config.get("password", "")
        retry = int(params.get("retry", 3))
        # 验证码识别配置：backend=local(ddddocr) / cloud(云码) / auto(本地优先)
        ocr_conf = {
            "backend": params.get("ocr_backend", "local"),
            "token": params.get("jfbym_token", "") or "",
            "type": params.get("jfbym_type", "") or "",
        }

        session = requests.Session()
        session.headers.update(HEADERS)

        # 方式一：直接用 Cookie
        if cookie:
            session.cookies.update(_parse_cookies(cookie))
            result = self._do_checkin(session, site_url, retry)
            if result.success or result.extra.get("error_type") != "cookie_expired" or not (username and password):
                if result.success:
                    result.cookie = _cookie_to_str(session)
                return result
            # Cookie 失效且有账号密码 → 回退登录（含验证码自动识别）

        # 方式二：账号密码登录（含验证码挑战）
        if username and password:
            ok, msg = _do_login(session, username, password, ocr_conf)
            if not ok:
                return CheckinResult(False, msg, {"error_type": "login_failed"})
            result = self._do_checkin(session, site_url, retry)
            if result.success:
                result.cookie = _cookie_to_str(session)  # 回写，下次复用
            return result

        return CheckinResult(False, "请填写 Cookie 或 用户名+密码 任意一种认证方式",
                             {"error_type": "no_auth"})

def _extract_message(text):
    """从 XML/CDATA/HTML 中提取纯文本签到消息（模块级，供登录/签到共用）"""
    cdata = re.search(r'<!\[CDATA\[(.*?)\]\]>', text, re.DOTALL)
    if cdata:
        text = cdata.group(1)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    for w in ['提示信息', '关闭', '确定', '取消', '知道了']:
        text = text.replace(w, '')
    text = re.sub(r'\s+', ' ', text).strip()
    return text
