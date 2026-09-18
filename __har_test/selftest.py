"""har 包自测：渲染引擎 + HAR 解析 + 执行引擎（mock HTTP）"""
import json
import sys
import os

ROOT = r"C:\Users\Administrator\WorkBuddy\自动签到\checkin-system"
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(extra)[:200]) if extra else ""))


# ============ 1. 渲染引擎 ============
from har.render import render, find_variables, parse_cookie_string, build_cookie_string

v = {
    "username": "alice@example.com",
    "password": "p@ss:word",          # 带冒号，测参数切分
    "token": "abc123",
    "site_url": "https://demo.test",
    "_cookies": {"sid": "S1", "jsid": "J2"},
}

check("1.1 纯变量替换", render("{{ username }}", v) == "alice@example.com", render("{{ username }}", v))
check("1.2 变量缺失→空串", render("a{{ notexist }}b", v) == "ab", render("a{{ notexist }}b", v))
check("1.3 无花括号原样", render("plain text", v) == "plain text")
check("1.4 内置函数 timestamp", render("{{ timestamp }}", v).isdigit(), render("{{ timestamp }}", v))
check("1.5 md5 过滤链", render("{{ timestamp | md5 }}", v) and len(render("{{ timestamp | md5 }}", v)) == 32)
check("1.6 substr 链", render("{{ username | substr:0:5 }}", v) == "alice", render("{{ username | substr:0:5 }}", v))
check("1.7 多级过滤链", render("{{ username | substr:0:5 | upper }}", v) == "ALICE",
      render("{{ username | substr:0:5 | upper }}", v))
check("1.8 md5 双层链", len(render("{{ username | md5 | substr:0:8 }}", v)) == 8,
      render("{{ username | md5 | substr:0:8 }}", v))
check("1.9 _cookies 下标", render("{{ _cookies['sid'] }}", v) == "S1", render("{{ _cookies['sid'] }}", v))
check("1.10 属性式取值", render("{{ _cookies.sid }}", v) == "S1", render("{{ _cookies.sid }}", v))
check("1.11 get_cookie_value", render("{{ cookie | get_cookie_value:'b' }}", {"cookie": "a=1; b=2"}) == "2",
      render("{{ cookie | get_cookie_value:'b' }}", {"cookie": "a=1; b=2"}))
check("1.12 参数含冒号不被切错", render("{{ password | replace:'@':'#' }}", v) == "p#ss:word",
      render("{{ password | replace:'@':'#' }}", v))
check("1.13 urlencode", render("{{ username | urlencode }}", v) == "alice%40example.com",
      render("{{ username | urlencode }}", v))
check("1.14 base64", render("{{ username | base64 }}", v) == "YWxpY2VAZXhhbXBsZS5jb20=",
      render("{{ username | base64 }}", v))
check("1.15 json 路径", render("""{{ resp | json:'data.token' }}""", {"resp": '{"data":{"token":"T9"}}'}) == "T9",
      render("""{{ resp | json:'data.token' }}""", {"resp": '{"data":{"token":"T9"}}'}))
check("1.16 random_num 参数化调用", len(render("{{ random_num:8 }}", v)) == 8, render("{{ random_num:8 }}", v))
check("1.17 同一段多处替换", render("{{ username }}:{{ token }}", v) == "alice@example.com:abc123",
      render("{{ username }}:{{ token }}", v))
check("1.18 default 过滤器", render("{{ missing | default:'fb' }}", v) == "fb",
      render("{{ missing | default:'fb' }}", v))
check("1.19 sha256", len(render("{{ token | sha256 }}", v)) == 64)
check("1.20 非字符串输入不炸", render(None, v) == "" and render(123, v) == "123")

# 变量识别
found = find_variables([
    {"request": {"method": "POST", "url": "{{ site_url }}/api/checkin?u={{ username }}",
                 "headers": [{"name": "X-Token", "value": "{{ token }}"}],
                 "cookies": [{"name": "sid", "value": "{{ _cookies['sid'] }}"}],
                 "data": "u={{ username }}&t={{ timestamp }}&c={{ cookie }}"},
     "rule": {}}
])
check("1.21 变量识别(排除内置)", sorted(found) == ["cookie", "site_url", "token", "username"],
      sorted(found) if isinstance(found, list) else found)

ck = parse_cookie_string("a=1; b=2; c=3")
check("1.22 cookie 解析", ck == {"a": "1", "b": "2", "c": "3"}, ck)
check("1.23 cookie 拼装", build_cookie_string({"a": "1", "b": "2"}) == "a=1; b=2",
      build_cookie_string({"a": "1", "b": "2"}))

# ============ 2. HAR 解析 ============
from har.parser import parse_har, parse_curl, HarParseError

SAMPLE_HAR = {
    "log": {
        "version": "1.2",
        "creator": {"name": "Chrome", "version": "120"},
        "entries": [
            {
                "startedDateTime": "2026-09-18T10:00:00.000Z",
                "time": 120,
                "request": {
                    "method": "GET",
                    "url": "https://demo.test/index.html",
                    "httpVersion": "HTTP/1.1",
                    "headers": [
                        {"name": "Host", "value": "demo.test"},
                        {"name": "Cookie", "value": "sid=ABC; jsid=DEF"},
                        {"name": "User-Agent", "value": "Mozilla/5.0 test"},
                        {"name": "Accept-Encoding", "value": "gzip, deflate"},
                        {"name": "Content-Length", "value": "0"},
                    ],
                    "queryString": [],
                    "cookies": [],
                },
                "response": {"status": 200, "content": {"mimeType": "text/html"}},
            },
            {
                "startedDateTime": "2026-09-18T10:00:01.000Z",
                "time": 90,
                "request": {
                    "method": "POST",
                    "url": "https://demo.test/api/sign",
                    "httpVersion": "HTTP/1.1",
                    "headers": [
                        {"name": "Cookie", "value": "sid=ABC; jsid=DEF"},
                        {"name": "Content-Type", "value": "application/x-www-form-urlencoded"},
                        {"name": "X-Requested-With", "value": "XMLHttpRequest"},
                    ],
                    "queryString": [],
                    "cookies": [{"name": "sid", "value": "ABC"}, {"name": "jsid", "value": "DEF"}],
                    "postData": {
                        "mimeType": "application/x-www-form-urlencoded",
                        "text": "uid=12345&formhash=abcd1234",
                    },
                },
                "response": {"status": 200, "content": {"mimeType": "application/json"}},
            },
            {
                "startedDateTime": "2026-09-18T10:00:02.000Z",
                "time": 30,
                "request": {
                    "method": "GET",
                    "url": "https://demo.test/static/app.js",
                    "httpVersion": "HTTP/1.1",
                    "headers": [],
                    "queryString": [],
                    "cookies": [],
                },
                "response": {"status": 200, "content": {"mimeType": "application/javascript"}},
            },
        ],
        "pages": [],
    }
}

entries = parse_har(SAMPLE_HAR)
check("2.1 HAR 请求数", len(entries) == 3, len(entries))
check("2.2 静态资源不勾选", entries[2]["checked"] is False, entries[2]["checked"])
check("2.3 接口请求默认勾选", entries[1]["checked"] is True)
check("2.4 method/url 解析", entries[1]["request"]["method"] == "POST" and
      entries[1]["request"]["url"] == "https://demo.test/api/sign")
hdr_names = [h["name"].lower() for h in entries[0]["request"]["headers"]]
check("2.5 剔除 Host/Content-Length/Accept-Encoding",
      "host" not in hdr_names and "content-length" not in hdr_names and "accept-encoding" not in hdr_names,
      hdr_names)
check("2.6 Cookie 头不进 headers", "cookie" not in hdr_names, hdr_names)
check("2.7 postData 解析", entries[1]["request"]["data"] == "uid=12345&formhash=abcd1234",
      entries[1]["request"]["data"])
check("2.8 rule 三字段存在",
      set(entries[1]["rule"]) == {"success_asserts", "failed_asserts", "extract_variables"},
      list(entries[1]["rule"]))
check("2.9 响应体未被保留(瘦身)", "content" not in entries[1].get("response", {}) or
      "text" not in entries[1]["response"].get("content", {}))
check("2.10 cookies 字段解析", entries[1]["request"]["cookies"] ==
      [{"name": "sid", "value": "ABC"}, {"name": "jsid", "value": "DEF"}],
      entries[1]["request"]["cookies"])

try:
    parse_har("not json")
    check("2.11 非法 JSON 抛错", False)
except HarParseError:
    check("2.11 非法 JSON 抛错", True)

# cURL
curl_cmd = ("curl 'https://demo.test/api/sign' -X POST "
            "-H 'Content-Type: application/x-www-form-urlencoded' "
            "-H 'Cookie: sid=ABC; jsid=DEF' "
            "-H 'User-Agent: Mozilla/5.0 test' "
            "--data 'uid=12345&formhash=abcd1234'")
cent = parse_curl(curl_cmd)[0]
check("2.12 cURL method", cent["request"]["method"] == "POST", cent["request"]["method"])
check("2.13 cURL url", cent["request"]["url"] == "https://demo.test/api/sign", cent["request"]["url"])
check("2.14 cURL body", cent["request"]["data"] == "uid=12345&formhash=abcd1234", cent["request"]["data"])
check("2.15 cURL cookie 解析", {c["name"]: c["value"] for c in cent["request"]["cookies"]} ==
      {"sid": "ABC", "jsid": "DEF"}, cent["request"]["cookies"])

# ============ 3. 执行引擎（mock HTTP） ============
import requests
from har.engine import HarRunner, HarError

CALLS = []


class FakeResp:
    def __init__(self, status, text, headers=None, set_cookie=None):
        self.status_code = status
        self.content = text.encode("utf-8")
        self.text = text
        self.encoding = "utf-8"
        self.headers = dict(headers or {})
        if set_cookie:
            self.headers["Set-Cookie"] = set_cookie
        self.raw = type("R", (), {"headers": _FakeRawHeaders(set_cookie or [])})()


class _FakeRawHeaders:
    def __init__(self, items):
        self._items = items

    def getlist(self, name):
        return list(self._items) if name.lower() == "set-cookie" else []


def make_session(routes):
    """routes: [(matcher(method,url)->bool, FakeResp)]"""
    def _request(self, method, url, headers=None, data=None, timeout=None, allow_redirects=None):
        CALLS.append({"method": method, "url": url, "headers": dict(headers or {}),
                      "data": data.decode() if isinstance(data, bytes) else data})
        for matcher, resp in routes:
            if matcher(method, url):
                return resp
        return FakeResp(404, "not found")

    return _request


orig = requests.Session.request

try:
    # 场景 A：完整三步流程 + 变量抽取 + cookie 继承 + 成功断言
    CALLS.clear()
    routes_a = [
        (lambda m, u: u.endswith("/login"), FakeResp(
            200, '{"ok":true,"data":{"token":"TK1"}}',
            {"Content-Type": "application/json"}, ["sid=SID1; Path=/", "jsid=JID1; Path=/"])),
        (lambda m, u: u.endswith("/sign"), FakeResp(
            200, '<html><body>签到成功 获得 5 积分</body></html>', {"Content-Type": "text/html"})),
    ]
    requests.Session.request = make_session(routes_a)
    tpl = [
        {"request": {"method": "POST", "url": "{{ site_url }}/login",
                     "headers": [{"name": "Content-Type", "value": "application/x-www-form-urlencoded"}],
                     "cookies": [], "data": "u={{ username }}&p={{ password }}"},
         "rule": {"success_asserts": [{"re": '"ok":true', "from": "content"}],
                  "failed_asserts": [],
                  "extract_variables": [{"name": "token", "re": '"token":"(\\w+)"', "from": "content"}]}},
        {"request": {"method": "POST", "url": "{{ site_url }}/sign",
                     "headers": [], "cookies": [],
                     "data": "t={{ token }}&c={{ _cookies['sid'] }}&h={{ timestamp }}"},
         "rule": {"success_asserts": [{"re": "签到成功", "from": "content"}],
                  "failed_asserts": [{"re": "已签到", "from": "content"}],
                  "extract_variables": [{"name": "pts", "re": "(\\d+) 积分", "from": "content"}]}},
    ]
    r = HarRunner().run(tpl, {"site_url": "https://demo.test", "username": "alice", "password": "pw"})
    check("3.1 多步流程成功", r.success, r.message)
    check("3.2 请求数=2", len(r.steps) == 2, len(r.steps))
    check("3.3 变量抽取", r.variables.get("token") == "TK1", r.variables.get("token"))
    check("3.4 二次抽取", r.variables.get("pts") == "5", r.variables.get("pts"))
    check("3.5 Set-Cookie 继承", r.cookies.get("sid") == "SID1", r.cookies)
    check("3.6 第二步带上了抽取变量+cookie",
          "TK1" in (CALLS[1]["data"] or "") and "SID1" in CALLS[1]["headers"].get("Cookie", ""),
          CALLS[1])
    check("3.7 日志非空", "POST" in r.logs and len(r.logs) > 20)

    # 场景 B：失败断言命中 → 应失败
    CALLS.clear()
    routes_b = [(lambda m, u: True, FakeResp(200, "<html>您今天已经签到过了</html>", {}))]
    requests.Session.request = make_session(routes_b)
    r2 = HarRunner().run(tpl[:1] + [{
        "request": {"method": "POST", "url": "https://demo.test/sign", "headers": [], "cookies": [], "data": ""},
        "rule": {"success_asserts": [], "failed_asserts": [{"re": "已经签到", "from": "content"}],
                 "extract_variables": []}}], {"site_url": "https://demo.test"})
    check("3.8 失败断言命中→失败", not r2.success, r2.message)
    check("3.9 失败信息含URL", "https://demo.test/" in r2.message, r2.message[:120])

    # 场景 C：成功断言不匹配 → 应失败
    CALLS.clear()
    requests.Session.request = make_session([(lambda m, u: True, FakeResp(200, "welcome", {}))])
    r3 = HarRunner().run([{
        "request": {"method": "GET", "url": "https://demo.test/api", "headers": [], "cookies": [], "data": ""},
        "rule": {"success_asserts": [{"re": "签到成功", "from": "content"}],
                 "failed_asserts": [], "extract_variables": []}}], {})
    check("3.10 成功断言不匹配→失败", not r3.success, r3.message)

    # 场景 D：`{% if %}` 分支
    CALLS.clear()
    requests.Session.request = make_session([(lambda m, u: True, FakeResp(200, "ok", {}))])
    tpl_if = [
        {"request": {"method": "POST", "url": "https://demo.test/login", "headers": [], "cookies": [],
                     "data": "u={{ username }}"}, "rule": {}},
        {"request": {"method": "GET", "url": "{% if has_captcha %}", "headers": [], "cookies": [], "data": ""},
         "rule": {}},
        {"request": {"method": "GET", "url": "https://demo.test/api/captcha", "headers": [], "cookies": [],
                     "data": ""}, "rule": {}},
        {"request": {"method": "GET", "url": "{% else %}", "headers": [], "cookies": [], "data": ""}, "rule": {}},
        {"request": {"method": "GET", "url": "https://demo.test/api/sign", "headers": [], "cookies": [],
                     "data": ""}, "rule": {}},
        {"request": {"method": "GET", "url": "{% endif %}", "headers": [], "cookies": [], "data": ""}, "rule": {}},
    ]
    r4 = HarRunner().run(tpl_if, {"username": "a", "has_captcha": ""})
    urls4 = [c["url"] for c in CALLS]
    check("3.11 if 假分支只走 sign", len(CALLS) == 2 and any("sign" in u for u in urls4)
          and not any("captcha" in u for u in urls4), urls4)
    CALLS.clear()
    r5 = HarRunner().run(tpl_if, {"username": "a", "has_captcha": "True"})
    urls5 = [c["url"] for c in CALLS]
    check("3.12 if 真分支走 captcha", any("captcha" in u for u in urls5), urls5)

    # 场景 E：`{% for %}` 循环
    CALLS.clear()
    requests.Session.request = make_session([(lambda m, u: True, FakeResp(200, "ok", {}))])
    tpl_for = [
        {"request": {"method": "GET", "url": "{% for item in mylist %}", "headers": [], "cookies": [], "data": ""},
         "rule": {}},
        {"request": {"method": "GET", "url": "https://demo.test/sign?id={{ item }}", "headers": [], "cookies": [],
                     "data": ""}, "rule": {}},
        {"request": {"method": "GET", "url": "{% endif %}", "headers": [], "cookies": [], "data": ""}, "rule": {}},
    ]
    r6 = HarRunner().run(tpl_for, {"mylist": ["11", "22", "33"]})
    check("3.13 for 循环 3 次", len(CALLS) == 3, [c["url"] for c in CALLS])
    check("3.14 for 变量逐个替换",
          CALLS[0]["url"].endswith("11") and CALLS[2]["url"].endswith("33"),
          [c["url"] for c in CALLS])

    # 场景 F：网络异常 → 失败但不崩
    def _boom(self, method, url, headers=None, data=None, timeout=None, allow_redirects=None):
        raise requests.ConnectionError("connection refused")
    requests.Session.request = _boom
    r7 = HarRunner().run([{
        "request": {"method": "GET", "url": "https://demo.test/x", "headers": [], "cookies": [], "data": ""},
        "rule": {}}], {})
    check("3.15 网络异常→失败不崩", not r7.success and "发送失败" in r7.message, r7.message[:120])

    # 场景 G：空模板
    r8 = HarRunner().run([], {})
    check("3.16 空模板→明确报错", not r8.success and "没有任何请求" in r8.message, r8.message)

    # 场景 H：Cookie 头自动注入 + 二次请求继承
    CALLS.clear()
    requests.Session.request = make_session([
        (lambda m, u: "step1" in u, FakeResp(200, "ok", {}, ["sid=NEW1"])),
        (lambda m, u: "step2" in u, FakeResp(200, "ok", {})),
    ])
    r9 = HarRunner().run([
        {"request": {"method": "GET", "url": "https://demo.test/step1", "headers": [], "cookies": [],
                     "data": ""}, "rule": {}},
        {"request": {"method": "GET", "url": "https://demo.test/step2", "headers": [], "cookies": [],
                     "data": ""}, "rule": {}},
    ], {})
    check("3.17 第二次请求带上新 cookie",
          CALLS[1]["headers"].get("Cookie", "").find("sid=NEW1") >= 0, CALLS[1]["headers"])

    # 场景 H2：预置 cookie 必须**覆盖**模板里抓包时写死的旧 cookie
    CALLS.clear()
    requests.Session.request = make_session([(lambda m, u: True, FakeResp(200, "ok", {}))])
    HarRunner().run(
        [{"request": {"method": "GET", "url": "https://demo.test/x", "headers": [],
                      "cookies": [{"name": "sid", "value": "OLD_FROM_HAR"}], "data": ""}, "rule": {}}],
        {},
        preset_cookies={"sid": "NEW_FROM_STORE"},
    )
    check("3.17b 预置 cookie 优先于模板旧值",
          "NEW_FROM_STORE" in CALLS[0]["headers"].get("Cookie", ""), CALLS[0]["headers"])

    # 场景 I：__log__ 全量日志提取
    CALLS.clear()
    requests.Session.request = make_session([(lambda m, u: True, FakeResp(200, "full body here", {}))])
    r10 = HarRunner().run([{
        "request": {"method": "GET", "url": "https://demo.test/x", "headers": [], "cookies": [], "data": ""},
        "rule": {"success_asserts": [{"re": "__log__", "from": "content"}],
                 "failed_asserts": [], "extract_variables": []}}], {})
    check("3.18 __log__ 提取", r10.variables.get("__log__") == "full body here",
          repr(r10.variables.get("__log__")))

finally:
    requests.Session.request = orig

print("\n" + "=" * 60)
print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print("SELFTEST_OK" if not FAIL else "SELFTEST_FAILED")
