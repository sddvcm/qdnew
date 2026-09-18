"""新插件模板 — 复制此文件创建新签到插件"""
from plugins.base import BasePlugin, FormField, CheckinResult


class TemplatePlugin(BasePlugin):
    name = "template"
    display_name = "新签到插件模板"
    description = "请修改此模板创建你的签到插件"
    plugin_type = "http"
    author = "you"

    form_schema = [
        FormField(key="site_url", label="站点地址", type="url", required=True,
                  placeholder="https://example.com"),
        FormField(key="username", label="用户名", type="text", required=False),
        FormField(key="password", label="密码", type="password", required=False, sensitive=True),
        FormField(key="cookie", label="Cookie", type="textarea", required=False,
                  placeholder="从浏览器复制", sensitive=True),
        FormField(key="retry", label="重试次数", type="number", required=False, default=3),
    ]

    def checkin(self, config):
        params = config.get("params", {})
        site_url = params.get("site_url", "")
        username = config.get("username", "") or params.get("username", "")
        password = config.get("password", "") or params.get("password", "")
        cookie = config.get("cookie", "") or params.get("cookie", "")

        # TODO: 实现签到逻辑
        # 1. 如果需要登录，先登录获取Cookie
        # 2. 执行签到请求
        # 3. 解析签到结果

        return CheckinResult(
            success=False,
            message="插件模板尚未实现签到逻辑，请修改 plugins/template.py",
            extra={"status": "not_implemented"}
        )
