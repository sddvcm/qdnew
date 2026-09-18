"""HAR 模板渲染引擎 — 变量替换、内置函数过滤链、`{{_cookies}}` 花括号语法。

设计参照 QD(qd-today/qd) 的模板渲染，但**刻意做减法**，只保留签到场景真正需要的：

    "{{ username }}"                                   → 变量替换
    "{{ timestamp }}"                                  → 内置函数
    "{{ timestamp | md5 | substr:0:8 }}"               → 过滤链（左到右依次套用）
    "{{ cookie | get_cookie_value:'token' }}"          → 带参数的过滤器（位置参数）
    "{{ _cookies['token'] }}"                          → 字典取值
    "{{ timestamp | urlencode }}"                      → 编码

依赖：Flask 自带的 Jinja2（无需额外安装）。

⚠️ 与 QD 的关键差异（重写者必读）
--------------------------------
1. QD 的过滤链语法是 `{{ 值 | 过滤器 }}`，但 Jinja2 原生过滤器**不能带位置参数**
   之外的东西，且 `get_cookie_value` 这类函数在 QD 里同时是"全局函数"和"过滤器"。
   本实现通过 `_apply_filter` 手工解析 `name:arg1:arg2` 形式，**不经过 Jinja2 的
   过滤器机制**，因此参数用 `:` 分隔而非 `()`。
2. 变量缺失时返回空串而非报错（`Undefined` → `""`）。签到任务里少一个可选变量
   不该让整条链崩掉。
3. 变量名允许连字符（`{{ site-url }}`），Jinja2 原生标识符不允许 —— 因此
   **先做变量替换，再交给 Jinja2 渲染**，顺序不能反。
"""
import base64
import hashlib
import hmac
import json
import random
import re
import string
import time
import urllib.parse
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ============================ 内置函数 ============================


def _timestamp() -> str:
    """当前 UNIX 秒级时间戳（字符串）"""
    return str(int(time.time()))


def _timestamp_ms() -> str:
    """当前毫秒时间戳（字符串）"""
    return str(int(time.time() * 1000))


def _date_time(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return time.strftime(fmt, time.localtime())


def _uuid() -> str:
    return str(uuid.uuid4())


def _random_num(length: int = 6) -> str:
    return "".join(random.choice(string.digits) for _ in range(int(length)))


def _random_str(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(int(length)))


def _randint(a: int, b: int) -> str:
    return str(random.randint(int(a), int(b)))


def _md5(value: Any) -> str:
    return hashlib.md5(_to_bytes(value)).hexdigest()


def _sha1(value: Any) -> str:
    return hashlib.sha1(_to_bytes(value)).hexdigest()


def _sha256(value: Any) -> str:
    return hashlib.sha256(_to_bytes(value)).hexdigest()


def _hmac_sha256(value: Any, key: Any = "") -> str:
    return hmac.new(_to_bytes(key), _to_bytes(value), hashlib.sha256).hexdigest()


def _base64(value: Any) -> str:
    return base64.b64encode(_to_bytes(value)).decode()


def _base64_decode(value: Any) -> str:
    raw = _to_bytes(value)
    raw += b"=" * (-len(raw) % 4)
    try:
        return base64.b64decode(raw).decode("utf-8", "replace")
    except Exception:
        return ""


def _urlencode(value: Any) -> str:
    return urllib.parse.quote(str(value), safe="")


def _quote(value: Any) -> str:
    return urllib.parse.quote(str(value), safe="")


def _int(value: Any) -> str:
    try:
        return str(int(float(str(value).strip())))
    except (ValueError, TypeError):
        return "0"


def _to_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


BUILTIN_FUNCS: Dict[str, Any] = {
    # 时间
    "timestamp": _timestamp,
    "timestamp_ms": _timestamp_ms,
    "date_time": _date_time,
    "now": _date_time,
    # 随机
    "uuid": _uuid,
    "random_num": _random_num,
    "random_str": _random_str,
    "randint": _randint,
    # 摘要
    "md5": _md5,
    "sha1": _sha1,
    "sha256": _sha256,
    "hmac_sha256": _hmac_sha256,
    # 编码
    "base64": _base64,
    "base64encode": _base64,
    "base64_decode": _base64_decode,
    "base64decode": _base64_decode,
    "urlencode": _urlencode,
    "quote": _quote,
    # 类型
    "int": _int,
}


# ============================ 过滤器 ============================


def _filter_substr(value: Any, start: Any = 0, end: Any = None) -> str:
    s = str(value)
    start = int(start)
    return s[start:] if end is None else s[start:int(end)]


def _filter_replace(value: Any, old: Any = "", new: Any = "") -> str:
    return str(value).replace(str(old), str(new))


def _filter_regex_replace(value: Any, pattern: Any = "", repl: Any = "") -> str:
    try:
        return re.sub(str(pattern), str(repl), str(value))
    except re.error:
        return str(value)


def _filter_regex_search(value: Any, pattern: Any = "") -> str:
    """返回第一个捕获组（无捕获组则返回整个匹配），无匹配返回空串"""
    try:
        m = re.search(str(pattern), str(value))
    except re.error:
        return ""
    if not m:
        return ""
    return m.group(1) if m.groups() else m.group(0)


def _filter_regex_findall(value: Any, pattern: Any = "") -> Any:
    try:
        return re.findall(str(pattern), str(value))
    except re.error:
        return []


def _filter_get_cookie_value(value: Any, name: Any = "") -> str:
    """从 Cookie 字符串里取某个键的值

    传入的 value 可以是：
      - Cookie 字符串       "a=1; b=2"
      - dict（已解析的 cookie 环境变量）
    """
    if isinstance(value, dict):
        return str(value.get(str(name), ""))
    text = str(value or "")
    if not text:
        return ""
    for part in text.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() == str(name):
            return v.strip()
    return ""


def _filter_json(value: Any, path: Any = "") -> str:
    """按点分路径从 JSON 里取值：{{ resp | json:'data.token' }}"""
    try:
        data = json.loads(value) if isinstance(value, (str, bytes)) else value
    except (json.JSONDecodeError, TypeError):
        return ""
    cur = data
    for seg in str(path).split("."):
        if not seg:
            continue
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list) and seg.isdigit():
            idx = int(seg)
            cur = cur[idx] if 0 <= idx < len(cur) else None
        else:
            return ""
        if cur is None:
            return ""
    return cur if isinstance(cur, str) else json.dumps(cur, ensure_ascii=False)


def _filter_length(value: Any) -> str:
    return str(len(value))


def _filter_default(value: Any, fallback: Any = "") -> str:
    return str(value) if value not in (None, "") else str(fallback)


def _filter_upper(value: Any) -> str:
    return str(value).upper()


def _filter_lower(value: Any) -> str:
    return str(value).lower()


def _filter_strip(value: Any) -> str:
    return str(value).strip()


FILTERS: Dict[str, Any] = {
    "substr": _filter_substr,
    "slice": _filter_substr,
    "replace": _filter_replace,
    "regex_replace": _filter_regex_replace,
    "regex_search": _filter_regex_search,
    "regex_findall": _filter_regex_findall,
    "get_cookie_value": _filter_get_cookie_value,
    "json": _filter_json,
    "length": _filter_length,
    "default": _filter_default,
    "upper": _filter_upper,
    "lower": _filter_lower,
    "strip": _filter_strip,
}

# 过滤器 / 内置函数共用一个命名空间（`md5` 既是函数也是过滤器）
ALL_CALLABLES: Dict[str, Any] = dict(BUILTIN_FUNCS)
ALL_CALLABLES.update(FILTERS)


# ============================ 表达式求值 ============================

# 管道链最左边那一节：变量名或函数名，可带 `:` 参数
_HEAD_RE = re.compile(r"^\s*([A-Za-z_][\w\-]*)\s*(?::(.*))?$", re.S)


def _split_args(raw: str) -> List[str]:
    """切分 `:` 分隔的参数（引号内的 `:` 不切）"""
    if raw is None:
        return []
    args: List[str] = []
    buf: List[str] = []
    quote: Optional[str] = None
    i = 0
    while i < len(raw):
        ch = raw[i]
        if quote:
            if ch == "\\" and i + 1 < len(raw):
                buf.append(raw[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
        elif ch in "'\"":
            quote = ch
        elif ch == ":":
            args.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    args.append("".join(buf).strip())
    return args


def _coerce_arg(token: str, variables: Dict[str, Any]) -> Any:
    """把参数文本转成值：变量引用 > 引号字面量 > 数字/布尔 > 裸字符串"""
    token = token.strip()
    if not token:
        return ""
    if token in variables:
        return variables[token]
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    if token in ("true", "True"):
        return True
    if token in ("false", "False"):
        return False
    if token in ("null", "None"):
        return ""
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    return token


def _eval_expr(expr: str, variables: Dict[str, Any], cookies: Optional[Dict[str, str]] = None) -> Any:
    """求值一个 `{{ }}` 里的表达式。

    支持三种形态，**判定顺序不能变**：
        1. 管道链：`a | f1 | f2:arg`  —— 先按 `|` 切开，逐段套用
        2. 函数调用：`timestamp`、`random_num:6`
        3. 变量 / 属性取值：`username`、`_cookies['sid']`、`resp.data.token`
    最后的兜底才交给 Jinja2（处理算术、字符串拼接）。
    """
    expr = (expr or "").strip()
    if not expr:
        return ""

    merged: Dict[str, Any] = dict(BUILTIN_FUNCS)
    merged.update(variables)
    merged["_cookies"] = cookies or {}
    merged["_variables"] = variables

    head_text, pipe_segments = _split_head_and_pipes(expr)

    # ---- 求值链头 ----
    value = _eval_head(head_text, variables, merged)

    # ---- 逐段套用过滤器 ----
    for seg in pipe_segments:
        value = _apply_one_filter(value, seg, merged)

    return value


def _split_head_and_pipes(expr: str) -> Tuple[str, List[str]]:
    """按顶层 `|` 切分，返回 (链头表达式, [过滤器段...])"""
    parts: List[str] = []
    buf: List[str] = []
    quote: Optional[str] = None
    depth = 0
    i = 0
    while i < len(expr):
        ch = expr[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(expr):
                buf.append(expr[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch in "([":
            depth += 1
            buf.append(ch)
        elif ch in ")]":
            depth -= 1
            buf.append(ch)
        elif ch == "|" and depth == 0:
            # 注意：`||` 是逻辑或，不当管道
            if i + 1 < len(expr) and expr[i + 1] == "|":
                buf.append("||")
                i += 2
                continue
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts[0], [p for p in parts[1:]]


def _eval_head(head: str, variables: Dict[str, Any], merged: Dict[str, Any]) -> Any:
    """求值管道链最左边那一节"""
    head = head.strip()
    if not head:
        return ""

    # 纯属性/下标取值：`_cookies['sid']` / `resp.data.token`
    if _looks_like_lookup(head):
        found = _safe_lookup(head, merged)
        if found is not _MISSING:
            return found
        return ""

    m = _HEAD_RE.match(head)
    if not m:
        return _jinja_eval(head, merged)

    name, args_raw = m.group(1), m.group(2)

    # ⚠️ 判定顺序铁律：**用户变量 > 内置函数**。
    # `{{ token }}` 若 token 恰好撞名某个内置函数，用户填的值必须优先，
    # 否则会拿到函数对象（曾把 `{{ md5 }}` 渲染成 "<function ...>" 就是这个原因）。
    if name in variables:
        return variables[name]

    if name in ALL_CALLABLES:
        args = [_coerce_arg(a, merged) for a in _split_args(args_raw)] if args_raw else []
        try:
            return ALL_CALLABLES[name](*args)
        except Exception:
            return ""

    if name in merged:
        return merged[name]

    return _jinja_eval(head, merged)


def _apply_one_filter(value: Any, segment: str, merged: Dict[str, Any]) -> Any:
    """套用一个过滤器段，形如 `md5` 或 `substr:0:8`"""
    segment = segment.strip()
    if not segment:
        return value
    if ":" in segment:
        fname, raw_args = segment.split(":", 1)
        args = [_coerce_arg(a, merged) for a in _split_args(raw_args)]
    else:
        fname, args = segment, []
    func = ALL_CALLABLES.get(fname.strip())
    if not func:
        return value
    try:
        return func(value, *args)
    except Exception:
        # 单个过滤器失败不应中断整条链，保留当前值继续
        return value


def _looks_like_lookup(expr: str) -> bool:
    """判断是否像 `a.b.c` / `a['b']` / `a[0]` 这类纯取值"""
    return bool(re.match(r"^[A-Za-z_][\w\-]*(\s*(\.[\w\-]+|\[[^\]]+\]))+$", expr))


_MISSING = object()


def _safe_lookup(expr: str, ctx: Dict[str, Any]) -> Any:
    """安全解析 `a.b.c` / `a['b']` / `a[0]`，任一层缺失返回 _MISSING"""
    tokens = re.findall(r"[A-Za-z_][\w\-]*|\[[^\]]+\]", expr)
    if not tokens:
        return _MISSING
    head = tokens[0]
    if head not in ctx:
        return _MISSING
    cur = ctx[head]
    for tok in tokens[1:]:
        try:
            if tok.startswith("["):
                inner = tok[1:-1].strip()
                if inner and inner[0] in "'\"" and inner[-1] == inner[0]:
                    key: Any = inner[1:-1]
                elif inner.isdigit():
                    key = int(inner)
                else:
                    key = _safe_lookup(inner, ctx)
                    if key is _MISSING:
                        return _MISSING
            else:
                key = tok
            if isinstance(cur, dict):
                cur = cur.get(key, _MISSING)
            elif isinstance(cur, (list, tuple)) and isinstance(key, int):
                cur = cur[key] if 0 <= key < len(cur) else _MISSING
            else:
                cur = getattr(cur, key, _MISSING)
            if cur is _MISSING:
                return _MISSING
        except Exception:
            return _MISSING
    return cur


_JINJA_ENV = None


def _jinja_eval(expr: str, ctx: Dict[str, Any]) -> str:
    """最后兜底：用 Jinja2 求值（仅当表达式里有算术/字符串运算时才会走到这）"""
    global _JINJA_ENV
    if _JINJA_ENV is None:
        from jinja2.sandbox import SandboxedEnvironment

        env = SandboxedEnvironment()
        env.filters.update(FILTERS)
        env.globals.update(BUILTIN_FUNCS)
        _JINJA_ENV = env
    try:
        out = _JINJA_ENV.from_string("{{ " + expr + " }}").render(**ctx)
        return out
    except Exception:
        return ""


# ============================ 对外主入口 ============================

_TOKEN_RE = re.compile(r"\{\{(.*?)\}\}", re.S)


def render(text: Any, variables: Optional[Dict[str, Any]] = None) -> str:
    """渲染一段文本，把 `{{ ... }}` 全部替换为求值结果。

    Args:
        text: 待渲染文本（None / 非字符串原样返回空串）
        variables: 变量表，包含用户填写的变量 + `_cookies` 等内置键

    Returns:
        渲染后的字符串。变量缺失 → 空串（不抛异常）。
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        return str(text)
    if "{{" not in text:
        return text

    variables = dict(variables or {})
    cookies = variables.get("_cookies") or {}
    if isinstance(cookies, str):
        cookies = parse_cookie_string(cookies)
    variables["_cookies"] = cookies

    def _sub(match: "re.Match") -> str:
        value = _eval_expr(match.group(1), variables, cookies)
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    result = _TOKEN_RE.sub(_sub, text)

    # 渲染完再跑一次 Jinja2：处理 `{% if %}` 之类的语句块（可选能力）
    if "{%" in result:
        result = _render_statements(result, variables)
    return result


def _render_statements(text: str, variables: Dict[str, Any]) -> str:
    global _JINJA_ENV
    if _JINJA_ENV is None:
        _jinja_eval("1", {})
    try:
        return _JINJA_ENV.from_string(text).render(**variables)
    except Exception:
        return text


def render_dict(obj: Any, variables: Dict[str, Any]) -> Any:
    """递归渲染 dict / list / str 里的所有 `{{ }}`"""
    if isinstance(obj, str):
        return render(obj, variables)
    if isinstance(obj, dict):
        return {k: render_dict(v, variables) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render_dict(v, variables) for v in obj]
    return obj


# ============================ 变量发现（自动识别） ============================

# 渲染时会被替换掉的"内置名"，不算用户变量
RESERVED_NAMES = set(BUILTIN_FUNCS) | set(FILTERS) | {"_cookies", "_variables", "loop"}


def find_variables(text: Any, extra_reserved: Optional[Iterable[str]] = None) -> List[str]:
    """从请求定义里自动找出所有用户变量名（用于生成任务表单）

    只认「出现在管道最左边」的名字，`{{ timestamp }}` 这类内置函数会被排除。
    """
    reserved = set(RESERVED_NAMES)
    if extra_reserved:
        reserved.update(extra_reserved)
    found: List[str] = []

    def _scan(value: Any):
        if isinstance(value, str):
            for raw in _TOKEN_RE.findall(value):
                expr = raw.strip()
                # 只取管道链最左边那一节 —— 右边都是过滤器名，不是用户变量
                head_text, _pipes = _split_head_and_pipes(expr)
                m = re.match(r"^([A-Za-z_][\w\-]*)", head_text.strip())
                if not m:
                    continue
                name = m.group(1)
                if name in reserved or name in found:
                    continue
                # `_cookies[...]` / `_variables[...]` 不是用户变量
                if head_text.strip().startswith(("_cookies", "_variables")):
                    continue
                found.append(name)
        elif isinstance(value, dict):
            for v in value.values():
                _scan(v)
        elif isinstance(value, list):
            for v in value:
                _scan(v)

    _scan(text)
    return found


# ============================ Cookie 工具 ============================


def parse_cookie_string(raw: str) -> Dict[str, str]:
    """把 `a=1; b=2` 解析成 dict"""
    out: Dict[str, str] = {}
    for part in str(raw or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def build_cookie_string(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in (cookies or {}).items() if v is not None)


def merge_set_cookie(cookies: Dict[str, str], set_cookie_headers: Iterable[str]) -> Dict[str, str]:
    """把响应里的 Set-Cookie 合并进 cookie 表（后续请求自动带上）"""
    out = dict(cookies or {})
    for header in set_cookie_headers or []:
        first = header.split(";", 1)[0].strip()
        if "=" not in first:
            continue
        k, v = first.split("=", 1)
        k, v = k.strip(), v.strip()
        if v in ("", "deleted"):
            out.pop(k, None)
        else:
            out[k] = v
    return out
