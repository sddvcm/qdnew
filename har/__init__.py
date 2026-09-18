"""HAR 通用签到模板引擎（模块入口）。

本模块提供"不写代码，只录 HAR"的通用签到能力 —— 抓包 → 导入 → 变量化 → 定时跑。
与 `plugins/` 下的专用插件并存：专用插件处理需要复杂加密/协议逆向的场景
（例如 hyperdown 的 SealJSON 信封），本引擎处理"就是几个普通 HTTP 请求"的场景。

对外只暴露这几样：

    from har import parse_har, parse_curl, run_entries, find_variables, render

    entries = parse_har(har_json_text)          # 导入
    vars_   = find_variables(entries)            # 自动识别需要用户填的变量
    result  = run_entries(entries, {"username": "...", "password": "..."})
    print(result.success, result.message, result.logs)

数据流：
    任务配置(params.har_template) ──┐
    用户变量(params.variables)   ──┼→ HarRunner.run() → RunResult
    已存档 cookies(params.cookies)─┘

⚠️ 重写提醒：模板结构里 `rule` 三个字段名（success_asserts / failed_asserts /
extract_variables）是对齐 QD 的命名，改名前先看 `engine.py` 里的消费点。
"""
from .engine import HarError, HarRunner, RunResult, StepResult, run_entries
from .parser import (
    HarParseError,
    entries_to_har,
    parse_curl,
    parse_har,
)
from .render import (
    RESERVED_NAMES,
    build_cookie_string,
    find_variables,
    merge_set_cookie,
    parse_cookie_string,
    render_dict,
)
# ⚠️ 注意这里用的是 `_render_template` 别名而不是 `render`。
# 若写成 `from .render import render`，本包的 `har.render` 属性会被**函数**覆盖掉
# 那个**子模块**，导致 `from har.render import RESERVED_NAMES` 之类的写法拿到函数
# 并报 AttributeError。子模块名与同名函数冲突是 Python 包的经典坑，用别名绕开。
from .render import render as _render_template

render = _render_template  # 对外仍暴露同名函数，只是不再遮蔽子模块

__all__ = [
    "HarRunner",
    "HarError",
    "HarParseError",
    "RunResult",
    "StepResult",
    "run_entries",
    "parse_har",
    "parse_curl",
    "entries_to_har",
    "render",
    "render_dict",
    "find_variables",
    "RESERVED_NAMES",
    "parse_cookie_string",
    "build_cookie_string",
    "merge_set_cookie",
]
