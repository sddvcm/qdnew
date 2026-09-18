"""HAR 通用签到插件 — 不写代码，只录抓包。

用法（Web 三步）：
    1. 浏览器 F12 → Network → 勾选「保留日志」→ 重现一次签到操作
       → 右键任意请求 → 「另存为带内容的 HAR」
    2. Web「模板管理」→ 上传刚保存的 HAR → 勾选签到相关的几个请求
       → 把用户名/密码/Cookie 等改成 `{{ username }}` / `{{ cookie }}` 变量
       → 填「成功断言」正则 → 保存
    3. Web「添加任务」→ 选本插件 → 模板选第 2 步那个 → 填变量 → 定时跑

本插件是**通用容器**：真正的逻辑在 `har/` 包里（解析 / 渲染 / 执行）。插件只负责
把任务配置翻译成引擎入参，并把结果翻译回 CheckinResult。

表单字段说明（`form_schema`）
------------------------------
    template_id : 选哪个 HAR 模板（下拉框运行时从数据库取，见 `form_schema()`）
    timeout     : 单请求超时秒数

⚠️ 为什么 `form_schema` 是方法而不是类属性
-------------------------------------------
模板列表是**运行时从数据库查的**，不是静态常量。而 `BasePlugin` 的加载器会
直接读 `instance.form_schema`，所以这里把它做成属性（property），每次访问都重新
查库。不要为了"省一次查询"改成类属性 —— 新建模板后表单会看不到新选项。
"""
from typing import Any, Dict, List, Optional

import requests

from plugins.base import BasePlugin, CheckinResult, FormField

from har import HarRunner, build_cookie_string, parse_cookie_string

# 变量名到任务通用字段的映射：模板里叫 username，任务表里也是 username
COMMON_FIELD_KEYS = ("username", "password", "cookie", "site_url")


class HarTemplatePlugin(BasePlugin):
    name = "har_template"
    display_name = "HAR 通用签到"
    description = (
        "把浏览器抓包(HAR)导入成签到模板，变量自动识别，无需写代码。"
        "适合处理普通 HTTP 请求式签到；需要复杂加密/协议逆向的站点请用专用插件。"
    )
    plugin_type = "http"
    author = "checkin-system"
    version = "1.0"

    # ---------------------------------------------------------------
    @property
    def form_schema(self) -> List[FormField]:
        """运行时生成的表单：模板下拉框从数据库实时读取"""
        return [
            FormField(
                key="template_id",
                label="HAR 模板",
                type="select",
                required=True,
                options=self._template_options(),
                help_text="在「模板管理」里导入 HAR 并配置好变量后，在这里选择",
            ),
            FormField(
                key="timeout",
                label="单请求超时(秒)",
                type="number",
                required=False,
                default=30,
            ),
        ]

    @form_schema.setter
    def form_schema(self, value):
        # 加载器可能会尝试赋值（`_discover_plugin_classes` 不会赋值，但保险起见）
        pass

    @staticmethod
    def _template_options() -> List[Dict[str, str]]:
        try:
            from app.models import HarTemplateModel

            return [
                {"value": str(t["id"]), "label": f"{t['name']}（{t['host'] or '未命名'}）"}
                for t in HarTemplateModel.get_all()
            ]
        except Exception:
            # 数据库还没建表 / 独立测试时，给出空列表而不是崩掉
            return []

    # ---------------------------------------------------------------
    def checkin(self, config: Dict[str, Any]) -> CheckinResult:
        return self._run(config, template_override=None)

    # 供任务详情页「测试模板」复用：临时指定一份模板，不存库
    def run_template(
        self,
        entries: List[Dict[str, Any]],
        variables: Dict[str, Any],
        cookies: Optional[Dict[str, str]] = None,
        timeout: int = 30,
    ) -> CheckinResult:
        return self._execute(entries, variables, cookies, timeout)

    # ---------------------------------------------------------------
    def _run(self, config: Dict[str, Any], template_override) -> CheckinResult:
        params = config.get("params", {}) or {}

        entries = template_override
        if entries is None:
            template_id = params.get("template_id")
            if not template_id:
                return CheckinResult(
                    False, "请先在表单里选择 HAR 模板（去「模板管理」导入抓包文件）",
                    {"error_type": "no_template"},
                )
            try:
                from app.models import HarTemplateModel

                tpl = HarTemplateModel.get_by_id(int(template_id))
            except Exception as exc:
                return CheckinResult(False, f"读取模板失败：{exc}", {"error_type": "db_error"})
            if not tpl:
                return CheckinResult(False, f"模板 #{template_id} 不存在（可能已被删除）",
                                     {"error_type": "no_template"})
            entries = tpl.get("entries") or []

        # 用户变量：任务 params.variables + 通用四字段（cookie 单独处理）
        variables: Dict[str, Any] = dict(params.get("variables") or {})
        for key in ("username", "password", "site_url"):
            value = config.get(key, "") or ""
            if value and not variables.get(key):
                variables[key] = value

        # 已存档 cookie：优先用任务 config.cookie（引擎加密存档的），
        # 其次是模板变量里用户手填的 cookie
        stored_cookie = config.get("cookie", "") or ""
        cookies = parse_cookie_string(stored_cookie) if stored_cookie else {}
        if not cookies:
            raw = variables.get("cookie") or params.get("cookie") or ""
            cookies = parse_cookie_string(raw) if isinstance(raw, str) else dict(raw or {})
        variables["cookie"] = build_cookie_string(cookies)

        timeout = params.get("timeout") or 30
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            timeout = 30

        return self._execute(entries, variables, cookies, timeout)

    # ---------------------------------------------------------------
    def _execute(
        self,
        entries: List[Dict[str, Any]],
        variables: Dict[str, Any],
        cookies: Dict[str, str],
        timeout: int,
    ) -> CheckinResult:
        if not entries:
            return CheckinResult(False, "模板里没有任何请求", {"error_type": "empty_template"})

        session = requests.Session()
        runner = HarRunner(session=session, timeout=timeout)

        # ⚠️ Cookie 优先级：**存档 cookie 覆盖模板里抓包时的旧 cookie**。
        # HAR 里抓下来的 Cookie（例如 `sid=抓包当时的会话`）是死数据，任务跑起来后
        # 必须用登录态/上次跑出来的新值。做法是把存档 cookie 预置进 runner 的初始
        # jar，让模板自带的 request.cookies 在其上叠加 —— 而不是反过来。
        preset = dict(cookies or {})
        result = runner.run(entries, variables, preset_cookies=preset)

        # 运行结束后把 jar 里的最终值回写（含本次新拿到的 Set-Cookie）
        final_cookies = dict(result.cookies or preset)

        extra = {
            "steps": len(result.steps),
            "logs": result.logs,
            "variables": {
                k: v for k, v in (result.variables or {}).items()
                if not str(k).startswith("_") and k != "__log__"
            },
        }

        out = CheckinResult(result.success, result.message, extra)
        if final_cookies:
            out.cookie = build_cookie_string(final_cookies)
        return out
