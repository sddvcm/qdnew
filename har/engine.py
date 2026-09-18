"""HAR 模板执行引擎 — 把一串 HTTP 请求定义跑成一条签到流程。

职责：
    1. 顺序执行 entries 里的每个请求（支持 `{% if %}` / `{% for %}` 控制语句）
    2. 请求前渲染 method / url / headers / cookies / body（替换 `{{ }}` 变量）
    3. 响应后按规则断言成败、抽取变量存入环境
    4. 维护 cookie jar：Set-Cookie 自动合并，后续请求自动带上
    5. 全程产出人可读日志（写进任务日志/通知）

依赖：requests（项目已有）

⚠️ 与 QD 的差异（重写者必读）
-----------------------------
1. QD 用 Tornado 异步 + pycurl；这里用 **requests 同步** —— 签到任务本质是串行
   几步 HTTP，同步实现更简单、依赖更少，且能直接复用 Flask 线程池调度。
2. QD 的 `env.variables` 与 `env.session` 分开存；这里合并为一个
   `EnvContext{variables, cookies}`，cookie 同时以 `_cookies` 字典暴露给模板。
3. QD 支持 `api://xxx` 站内接口；这里**不支持**（签到场景用不上，砍掉减依赖）。
"""
import json
import re
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from .render import build_cookie_string, merge_set_cookie, render

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# 单次任务最多发几个请求（防写错 for 循环把站点打爆）
DEFAULT_REQUEST_LIMIT = 60
DEFAULT_TIMEOUT = 30


class HarError(Exception):
    """模板执行失败（带人可读原因，会被写进签到日志）"""


@dataclass
class EnvContext:
    """运行上下文：变量表 + cookie jar"""

    variables: Dict[str, Any] = field(default_factory=dict)
    cookies: Dict[str, str] = field(default_factory=dict)

    def template_vars(self) -> Dict[str, Any]:
        merged = dict(self.variables)
        merged["_cookies"] = self.cookies
        return merged


@dataclass
class StepResult:
    """单个请求的执行结果"""

    index: int
    method: str
    url: str
    status: int = 0
    body: str = ""
    success: bool = False
    message: str = ""


@dataclass
class RunResult:
    """整条模板的执行结果"""

    success: bool
    message: str
    logs: str = ""
    variables: Dict[str, Any] = field(default_factory=dict)
    cookies: Dict[str, str] = field(default_factory=dict)
    steps: List[StepResult] = field(default_factory=list)


# ============================ 断言与抽取 ============================


def _assert_matches(rule: Dict[str, Any], status: int, headers: Dict[str, str], body: str) -> bool:
    pattern = rule.get("re") or ""
    if not pattern:
        return False
    source = rule.get("from", "content")
    if source == "status":
        target = str(status)
    elif source == "header":
        target = "\n".join(f"{k}: {v}" for k, v in headers.items())
    elif str(source).startswith("header-"):
        target = headers.get(str(source)[7:].lower(), "")
    else:
        target = body or ""
    try:
        return re.search(pattern, target, re.M | re.S) is not None
    except re.error:
        return False


def _extract_variables(
    rules: List[Dict[str, Any]],
    status: int,
    headers: Dict[str, str],
    body: str,
    env: EnvContext,
) -> Dict[str, Any]:
    """按规则从响应里抽变量，写回 env.variables"""
    extracted: Dict[str, Any] = {}
    for rule in rules or []:
        name = rule.get("name")
        pattern = rule.get("re")
        if not name or not pattern:
            continue

        # 支持 /正则/flags 写法（QD 兼容）
        flags = re.M | re.S
        m = re.match(r"^/(.*)/([gimsu]*)$", pattern, re.S)
        find_all = False
        if m:
            pattern = m.group(1)
            mods = m.group(2)
            if "g" in mods:
                find_all = True
            if "i" in mods:
                flags |= re.I
            if "m" in mods:
                flags |= re.M
            if "s" in mods:
                flags |= re.S

        source = rule.get("from", "content")
        if source == "status":
            target = str(status)
        elif source == "header":
            target = "\n".join(f"{k}: {v}" for k, v in headers.items())
        elif str(source).startswith("header-"):
            target = headers.get(str(source)[7:].lower(), "")
        else:
            target = body or ""

        try:
            if find_all:
                value = re.findall(pattern, target, flags)
            else:
                hit = re.search(pattern, target, flags)
                if not hit:
                    continue
                value = hit.group(1) if hit.groups() else hit.group(0)
        except re.error as exc:
            value = f"正则错误: {exc}"

        env.variables[str(name)] = value
        extracted[str(name)] = value
    return extracted


# ============================ 控制语句 ============================

_IF_RE = re.compile(r"^{%\s*if\s+(.+?)\s*%}$", re.S)
_ELSE_RE = re.compile(r"^{%\s*else\s*%}$")
_ENDIF_RE = re.compile(r"^{%\s*endif\s*%}$")
_FOR_RE = re.compile(r"^{%\s*for\s+(\w+)\s+in\s+([\w\-]+)\s*%}$")


def _truthy(expr: str, env: EnvContext) -> bool:
    """求值 `{% if %}` 条件。`a == 'x'` / `a != 'x'` / 变量名 三种形式"""
    expr = expr.strip()
    for op in ("==", "!="):
        if op in expr:
            left, right = expr.split(op, 1)
            lv = _resolve(left.strip(), env)
            rv = _resolve(right.strip(), env)
            return (lv == rv) if op == "==" else (lv != rv)
    value = _resolve(expr, env)
    return bool(value) and value not in ("False", "false", "0", 0)


def _resolve(token: str, env: EnvContext) -> Any:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    if token in env.variables:
        return env.variables[token]
    return render("{{ " + token + " }}", env.template_vars())


def _expand_control_flow(entries: List[Dict[str, Any]]) -> List[Any]:
    """把带 `{% if %}` / `{% for %}` 的伪条目编译成嵌套结构

    QD 的做法是把控制语句写在 url 字段里（`url: "{% if x %}"`），沿用。
    返回的列表里元素是：
      - {"type": "request", "entry": {...}}
      - {"type": "if", "cond": str, "true": [...], "false": [...]}
      - {"type": "for", "var": str, "list": str, "body": [...]}
    """
    root: List[Any] = []
    stack: List[Dict[str, Any]] = []

    def append(item: Any):
        if stack:
            top = stack[-1]
            if top["type"] == "for":
                top["body"].append(item)
            else:
                top["true" if top["branch"] == "true" else "false"].append(item)
        else:
            root.append(item)

    for entry in entries or []:
        url = str((entry.get("request") or {}).get("url") or "")

        m_for = _FOR_RE.match(url)
        m_if = _IF_RE.match(url)
        if m_for:
            node = {"type": "for", "var": m_for.group(1), "list": m_for.group(2), "body": []}
            stack.append(node)
        elif m_if:
            node = {"type": "if", "cond": m_if.group(1), "branch": "true", "true": [], "false": []}
            stack.append(node)
        elif _ELSE_RE.match(url):
            if stack and stack[-1]["type"] == "if":
                stack[-1]["branch"] = "false"
        elif _ENDIF_RE.match(url):
            if stack:
                node = stack.pop()
                append(node)
        else:
            append({"type": "request", "entry": entry})

    # 未闭合的块也要吐出来，避免静默丢请求
    while stack:
        node = stack.pop()
        append(node)
    return root


# ============================ 执行器 ============================


class HarRunner:
    """执行一个 HAR 模板

    Args:
        session: 可复用的 requests.Session（多个任务间不要共用，除非刻意共享登录态）
        timeout: 单请求超时秒数
        request_limit: 本次最多发多少请求
    """

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        timeout: int = DEFAULT_TIMEOUT,
        request_limit: int = DEFAULT_REQUEST_LIMIT,
    ):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.request_limit = request_limit
        self.log_lines: List[str] = []
        self.steps: List[StepResult] = []

    # ---------- 日志 ----------
    def log(self, msg: str):
        self.log_lines.append(msg)

    @property
    def logs(self) -> str:
        return "\n".join(self.log_lines)

    # ---------- 单请求 ----------
    def _do_request(self, entry: Dict[str, Any], env: EnvContext, index: int) -> StepResult:
        request = entry.get("request") or {}
        rule = entry.get("rule") or {}
        variables = env.template_vars()

        method = (render(request.get("method") or "GET", variables) or "GET").upper()
        url = render(request.get("url") or "", variables).strip()
        if not url:
            raise HarError(f"第 {index} 个请求的 URL 为空")

        headers: Dict[str, str] = dict(DEFAULT_HEADERS)
        raw_headers = request.get("headers") or []
        if isinstance(raw_headers, dict):
            raw_headers = [{"name": k, "value": v} for k, v in raw_headers.items()]
        for h in raw_headers:
            name = render((h or {}).get("name") or "", variables).strip()
            value = render((h or {}).get("value") or "", variables)
            if name:
                headers[name] = value

        # cookie：环境 jar + 条目自带的 cookie 字段
        # ⚠️ 顺序很关键：env.cookies 是**现存会话**（登录态/上次跑出来的），
        # 应当最后由它覆盖条目里抓包时写死的旧值；否则回写复用永远不生效。
        cookie_jar: Dict[str, str] = {}
        raw_cookies = request.get("cookies") or []
        if isinstance(raw_cookies, dict):
            raw_cookies = [{"name": k, "value": v} for k, v in raw_cookies.items()]
        for c in raw_cookies:
            name = render((c or {}).get("name") or "", variables).strip()
            value = render((c or {}).get("value") or "", variables)
            if name:
                cookie_jar[name] = value
        cookie_jar.update(env.cookies)
        if cookie_jar:
            headers["Cookie"] = build_cookie_string(cookie_jar)

        body = request.get("data")
        if method in ("GET", "HEAD", "OPTIONS"):
            payload = None
        else:
            payload = render(body, variables) if body else None
            if payload is not None and not isinstance(payload, (str, bytes)):
                payload = json.dumps(payload, ensure_ascii=False)
            if payload is not None:
                payload = payload.encode("utf-8") if isinstance(payload, str) else payload

        self.log(f"[{index}] {method} {url}")
        try:
            resp = self.session.request(
                method, url, headers=headers, data=payload,
                timeout=self.timeout, allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise HarError(f"第 {index} 个请求发送失败：{exc}") from exc

        # 解码响应体
        encoding = resp.encoding or resp.apparent_encoding or "utf-8"
        try:
            text = resp.content.decode(encoding, "replace")
        except (LookupError, UnicodeDecodeError):
            text = resp.text

        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        status = resp.status_code
        sub = str(status)
        self.log(f"    → HTTP {status}  {len(resp.content)} bytes")

        # 合并 Set-Cookie
        raw_set_cookie = resp.raw.headers.getlist("Set-Cookie") if hasattr(resp.raw, "headers") else []
        if not raw_set_cookie and resp.headers.get("Set-Cookie"):
            raw_set_cookie = [resp.headers["Set-Cookie"]]
        if raw_set_cookie:
            before = dict(env.cookies)
            env.cookies = merge_set_cookie(env.cookies, raw_set_cookie)
            changed = [k for k in env.cookies if before.get(k) != env.cookies[k]]
            if changed:
                self.log(f"    → 更新 Cookie: {', '.join(changed)}")

        # 断言
        success = True
        message = ""
        success_asserts = rule.get("success_asserts") or []
        failed_asserts = rule.get("failed_asserts") or []

        # ⚠️ `__log__` 是**哨兵**不是正则：命中它表示"把整个响应体存进 __log__ 变量"，
        # 本身不参与成败判定。必须先从断言列表里摘掉，否则正则 `__log__` 永远匹配不上
        # 正常响应体，会把每个请求都判成失败。
        success_asserts = [a for a in success_asserts if str(a.get("re") or "") != "__log__"]
        capture_log = any(str(a.get("re") or "") == "__log__"
                          for a in (rule.get("success_asserts") or []) + (rule.get("failed_asserts") or []))
        failed_asserts = [a for a in failed_asserts if str(a.get("re") or "") != "__log__"]

        if success_asserts:
            hit = False
            for a in success_asserts:
                a_rendered = {
                    "re": render(str(a.get("re") or ""), variables),
                    "from": a.get("from", "content"),
                }
                if _assert_matches(a_rendered, status, resp_headers, text):
                    hit = True
                    break
            if not hit:
                success = False
                message = "未匹配「成功断言」"

        for a in failed_asserts:
            a_rendered = {
                "re": render(str(a.get("re") or ""), variables),
                "from": a.get("from", "content"),
            }
            if _assert_matches(a_rendered, status, resp_headers, text):
                success = False
                message = "命中「失败断言」"
                break

        if capture_log:
            env.variables["__log__"] = text

        if not success:
            snippet = (text or "").strip().replace("\n", " ")[:160]
            self.log(f"    ✗ {message}｜响应片段: {snippet}")
            raise HarError(
                f"第 {index} 个请求判定失败（{message}，HTTP {status}）｜ "
                f"{url} ｜ 响应片段: {snippet}"
            )

        _extract_variables(rule.get("extract_variables") or [], status, resp_headers, text, env)

        # 变量抽取后立刻可见（含 __log__），便于模板里接着用
        return StepResult(index=index, method=method, url=url, status=status, body=text, success=True)

    # ---------- 遍历 ----------
    def _walk(self, nodes: List[Any], env: EnvContext, counter: List[int]):
        for node in nodes:
            if self.request_limit <= 0:
                raise HarError(f"请求数超过上限（{DEFAULT_REQUEST_LIMIT}），请检查模板是否写错")

            if node["type"] == "request":
                counter[0] += 1
                step = self._do_request(node["entry"], env, counter[0])
                self.steps.append(step)

            elif node["type"] == "if":
                branch = node["true"] if _truthy(node["cond"], env) else node["false"]
                self.log(f"[控制] if {node['cond']} → {'true' if branch is node['true'] else 'false'}")
                self._walk(branch, env, counter)

            elif node["type"] == "for":
                raw = env.variables.get(node["list"])
                if raw is None:
                    self.log(f"[控制] for {node['var']} in {node['list']} → 变量不存在，跳过")
                    continue
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        raw = [raw]
                if not isinstance(raw, (list, tuple)):
                    raw = [raw]
                for item in raw:
                    env.variables[node["var"]] = item
                    self._walk(node["body"], env, counter)

    # ---------- 主入口 ----------
    def run(
        self,
        entries: List[Dict[str, Any]],
        variables: Optional[Dict[str, Any]] = None,
        preset_cookies: Optional[Dict[str, str]] = None,
    ) -> RunResult:
        """跑一遍模板。

        Args:
            entries: 请求定义列表
            variables: 用户变量表
            preset_cookies: 预置 cookie（登录态），会在每个请求上生效，
                并与模板自带的 cookie 合并 —— **预置值优先**。
        """
        env = EnvContext(variables=dict(variables or {}), cookies=dict(preset_cookies or {}))
        self.log_lines = []
        self.steps = []
        counter = [0]

        try:
            nodes = _expand_control_flow(entries or [])
        except Exception as exc:
            return RunResult(False, f"模板结构有误：{exc}", self.logs)

        if not nodes:
            return RunResult(False, "模板里没有任何请求，请在模板编辑页勾选需要执行的请求", self.logs)

        try:
            self._walk(nodes, env, counter)
        except HarError as exc:
            return RunResult(False, str(exc), self.logs, env.variables, env.cookies, self.steps)
        except Exception as exc:
            detail = traceback.format_exc()
            self.log(f"[异常] {exc}")
            return RunResult(False, f"执行异常：{exc}\n{detail}", self.logs, env.variables, env.cookies, self.steps)

        log_text = str(env.variables.get("__log__") or "").strip()
        message = log_text.split("\n")[0] if log_text else f"执行成功，共 {counter[0]} 个请求"
        return RunResult(True, message, self.logs, env.variables, env.cookies, self.steps)


def run_entries(
    entries: List[Dict[str, Any]],
    variables: Optional[Dict[str, Any]] = None,
    cookies: Optional[Dict[str, str]] = None,
    session: Optional[requests.Session] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> RunResult:
    """便捷函数：跑一遍模板，返回结果"""
    runner = HarRunner(session=session, timeout=timeout)
    return runner.run(entries, variables, preset_cookies=cookies)
