"""HAR 解析器 — 把浏览器/抓包工具导出的 .har 文件转成模板条目。

支持三种导入来源：
    1. **标准 HAR 文件**（Chrome / Edge / Firefox / Fiddler / Stream 导出）
       → 解析 `log.entries[]`，保留 method/url/headers/cookies/postData
    2. **cURL 命令**（`curl 'https://...' -H '...' --data '...'`）
       → 解析成单个条目，方便快速加一个请求
    3. **裸 JSON 条目列表**（本系统自己导出的模板格式）
       → 直接读

只取我们真正需要的东西，**响应数据一律丢弃**（签到模板只需要"要发什么请求"，
响应内容由运行时动态变化，存下来没有意义而且体积巨大 —— 一个 HAR 动辄几十 MB，
剥掉响应后通常只剩几 KB）。

⚠️ 关键取舍（重写者必读）
--------------------------
HAR 里的 `headers` 是个扁平列表，同一个 key 可能出现多次（比如多个 Set-Cookie、
多个 Cookie）。这里**只保留请求头**，重复 key 后者覆盖前者（除了 Cookie 会拼接）。
另外应当**剔除**这些会干扰的请求头：
    - 与传输层强绑定的：`content-length` / `host` / `connection` / `transfer-encoding`
      这些由 requests 自动生成，写死会导致请求异常
    - 浏览器指纹类：`sec-ch-ua*` / `sec-fetch-*` / `accept-encoding`
      这些留在模板里会让服务端按"现代浏览器"处理，但 requests 的实际行为对不上，
      反而容易触发风控；签到场景一律不发，必要时用户可在模板里手工加回。
"""
import json
import re
import urllib.parse
from typing import Any, Dict, List

# 导入时自动丢弃的请求头（小写）
DROP_HEADERS = {
    "content-length",
    "host",
    "connection",
    "transfer-encoding",
    "accept-encoding",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-dest",
    "sec-fetch-user",
    "upgrade-insecure-requests",
    "pragma",
    "cache-control",
    ":authority",
    ":method",
    ":path",
    ":scheme",
}

# 静态资源后缀 —— 导入时默认不勾选（签到模板基本用不上）
STATIC_EXT = re.compile(
    r"\.(js|css|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|eot|mp4|mp3|webm|map|pdf)(\?|$)",
    re.I,
)


class HarParseError(Exception):
    """HAR 文件无法解析"""


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_query(url: str) -> List[Dict[str, str]]:
    parsed = urllib.parse.urlsplit(url or "")
    return [
        {"name": k, "value": v}
        for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    ]


def _dedupe_headers(raw_headers: Any) -> List[Dict[str, str]]:
    """把 HAR 的 header 列表清洗成我们想要的形态"""
    out: List[Dict[str, str]] = []
    seen: Dict[str, int] = {}
    if not isinstance(raw_headers, list):
        return out

    for h in raw_headers:
        if not isinstance(h, dict):
            continue
        name = str(h.get("name") or "").strip()
        value = str(h.get("value") or "")
        if not name:
            continue
        low = name.lower()
        if low in DROP_HEADERS:
            continue
        # Cookie 单独走 cookies 字段，不重复塞进 headers
        if low == "cookie":
            continue
        if low in seen:
            # 同 key 多次：保留第一个（通常是主值），丢后续
            continue
        seen[low] = len(out)
        out.append({"name": name, "value": value, "checked": True})
    return out


def _parse_cookies_from_har(raw_cookies: Any, cookie_header: str = "") -> List[Dict[str, str]]:
    """HAR 里 cookie 可能在 `request.cookies`，也可能只在 Cookie 头里"""
    out: List[Dict[str, str]] = []
    seen = set()
    if isinstance(raw_cookies, list):
        for c in raw_cookies:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "value": str(c.get("value") or "")})
    if not out and cookie_header:
        for part in cookie_header.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k = k.strip()
            if k and k not in seen:
                seen.add(k)
                out.append({"name": k, "value": v.strip()})
    return out


def _extract_body(request: Dict[str, Any]) -> str:
    """取出请求体原文"""
    post = request.get("postData") or {}
    text = post.get("text")
    if text:
        return str(text)
    params = post.get("params")
    if isinstance(params, list) and params:
        return "&".join(
            f"{urllib.parse.quote(str(p.get('name', '')))}={urllib.parse.quote(str(p.get('value', '')))}"
            for p in params
        )
    return ""


def parse_har(hardata: Any) -> List[Dict[str, Any]]:
    """把 HAR（dict 或 JSON 字符串）解析成模板条目列表"""
    if isinstance(hardata, (str, bytes)):
        try:
            hardata = json.loads(hardata)
        except (json.JSONDecodeError, TypeError) as exc:
            raise HarParseError(f"HAR 不是有效 JSON：{exc}") from exc

    if not isinstance(hardata, dict):
        raise HarParseError("HAR 根节点必须是对象")

    # 兼容两种形态：标准 HAR(`log.entries`) / 我们自己的模板 (`entries`)
    entries = None
    if isinstance(hardata.get("log"), dict):
        entries = hardata["log"].get("entries")
    if entries is None and isinstance(hardata.get("entries"), list):
        entries = hardata["entries"]
    if entries is None:
        raise HarParseError("HAR 里找不到 log.entries，确认导出时选择了「另存为带内容的 HAR」")

    out: List[Dict[str, Any]] = []
    for en in entries:
        if not isinstance(en, dict):
            continue
        # 已经是我们自己的模板条目：原样保留
        if "request" in en and isinstance(en["request"], dict) and "rule" in en:
            out.append(_normalize_entry(en))
            continue

        request = en.get("request") or {}
        if not isinstance(request, dict):
            continue
        url = str(request.get("url") or "").strip()
        if not url or url.startswith(("data:", "blob:", "chrome-extension:")):
            continue
        method = str(request.get("method") or "GET").upper()

        headers_all = request.get("headers") or []
        cookie_header = ""
        for h in headers_all:
            if isinstance(h, dict) and str(h.get("name", "")).lower() == "cookie":
                cookie_header = str(h.get("value") or "")
                break

        response = en.get("response") or {}
        status = _safe_int(response.get("status"))
        mime = ""
        if isinstance(response.get("content"), dict):
            mime = str(response["content"].get("mimeType") or "")

        out.append({
            "checked": not bool(STATIC_EXT.search(url)),
            "comment": "",
            "startedDateTime": str(en.get("startedDateTime") or ""),
            "time": _safe_int(en.get("time")),
            "request": {
                "method": method,
                "url": url,
                "httpVersion": str(request.get("httpVersion") or "HTTP/1.1"),
                "headers": _dedupe_headers(headers_all),
                "queryString": request.get("queryString") or _parse_query(url),
                "cookies": _parse_cookies_from_har(request.get("cookies"), cookie_header),
                "headersSize": -1,
                "bodySize": _safe_int(request.get("bodySize"), -1),
                "data": _extract_body(request),
                "mimeType": (request.get("postData") or {}).get("mimeType") or "",
            },
            "response": {"status": status, "mimeType": mime},
            "rule": {
                "success_asserts": [],
                "failed_asserts": [],
                "extract_variables": [],
            },
        })

    if not out:
        raise HarParseError("HAR 里没有可用的请求（可能全是静态资源，或导出时未包含请求详情）")
    return out


def _normalize_entry(en: Dict[str, Any]) -> Dict[str, Any]:
    """补齐我们自己模板条目的缺失字段，保证运行时不炸"""
    request = en.setdefault("request", {})
    request.setdefault("method", "GET")
    request.setdefault("url", "")
    request.setdefault("headers", [])
    request.setdefault("cookies", [])
    request.setdefault("data", "")
    rule = en.setdefault("rule", {})
    rule.setdefault("success_asserts", [])
    rule.setdefault("failed_asserts", [])
    rule.setdefault("extract_variables", [])
    en.setdefault("checked", True)
    return en


# ============================ cURL 解析 ============================

_CURL_TOKEN_RE = re.compile(r"""'([^']*)'|"([^"]*)"|(\S+)""")


def _tokenize_curl(cmd: str) -> List[str]:
    """按 shell 规则切分（只处理单/双引号，不处理变量展开）"""
    # 去掉续行符
    cmd = cmd.replace("\\\r\n", " ").replace("\\\n", " ")
    cmd = re.sub(r"^\s*curl\s+", "", cmd.strip(), flags=re.I)
    tokens: List[str] = []
    for m in _CURL_TOKEN_RE.finditer(cmd):
        tokens.append(m.group(1) if m.group(1) is not None
                      else m.group(2) if m.group(2) is not None
                      else m.group(3))
    return tokens


def parse_curl(cmd: str) -> List[Dict[str, Any]]:
    """把一条 cURL 命令解析成模板条目"""
    tokens = _tokenize_curl(cmd or "")
    if not tokens:
        raise HarParseError("cURL 命令为空")

    url = ""
    method = ""
    headers: List[Dict[str, str]] = []
    cookies: List[Dict[str, str]] = []
    body = ""

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        low = tok.lower()

        def _next() -> str:
            nonlocal i
            i += 1
            return tokens[i] if i < len(tokens) else ""

        if tok in ("-H", "--header"):
            raw = _next()
            if ":" in raw:
                name, value = raw.split(":", 1)
                name, value = name.strip(), value.strip()
                if name.lower() == "cookie":
                    cookies = _parse_cookies_from_har(None, value)
                elif name.lower() not in DROP_HEADERS:
                    headers.append({"name": name, "value": value, "checked": True})
        elif tok in ("-b", "--cookie"):
            cookies = _parse_cookies_from_har(None, _next())
        elif tok in ("-d", "--data", "--data-raw", "--data-binary", "--data-urlencode"):
            body = _next()
            if not method:
                method = "POST"
        elif tok in ("-X", "--request"):
            method = _next().upper()
        elif tok in ("-A", "--user-agent"):
            headers.append({"name": "User-Agent", "value": _next(), "checked": True})
        elif tok in ("-e", "--referer"):
            headers.append({"name": "Referer", "value": _next(), "checked": True})
        elif tok in ("--compressed", "-k", "--insecure", "-s", "--silent", "-L", "--location", "-v", "--verbose"):
            pass
        elif tok in ("-o", "--output", "-w", "--write-out", "-x", "--proxy", "--max-time", "--connect-timeout"):
            _next()
        elif low.startswith("http://") or low.startswith("https://"):
            if not url:
                url = tok
        i += 1

    if not url:
        raise HarParseError("cURL 里找不到 URL")

    return [{
        "checked": True,
        "comment": "",
        "startedDateTime": "",
        "time": 0,
        "request": {
            "method": method or "GET",
            "url": url,
            "httpVersion": "HTTP/1.1",
            "headers": headers,
            "queryString": _parse_query(url),
            "cookies": cookies,
            "headersSize": -1,
            "bodySize": len(body),
            "data": body,
            "mimeType": "application/x-www-form-urlencoded" if body else "",
        },
        "response": {"status": 0, "mimeType": ""},
        "rule": {"success_asserts": [], "failed_asserts": [], "extract_variables": []},
    }]


def entries_to_har(entries: List[Dict[str, Any]], title: str = "") -> Dict[str, Any]:
    """把模板条目导出成标准 HAR 结构（供「下载模板」用）"""
    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "checkin-system", "version": "1.0"},
            "pages": [],
            "entries": [
                {
                    "checked": en.get("checked", True),
                    "comment": en.get("comment", ""),
                    "startedDateTime": en.get("startedDateTime", ""),
                    "time": en.get("time", 0),
                    "request": en.get("request", {}),
                    "response": en.get("response", {}),
                    "rule": en.get("rule", {}),
                }
                for en in (entries or [])
            ],
        }
    }
