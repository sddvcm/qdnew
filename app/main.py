"""Flask 应用入口"""
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# ⚠️ 日志必须**最先**配置：放在 import Flask 之前，这样连"导入阶段就出错"
# 的情况（比如某个依赖缺失）也能把栈落到 app.log 里。
# 原先应用没有任何 logging 配置，启动失败时日志文件是空的，用户只看到
# 一句"启动失败"毫无线索 —— 用户实际反馈过。
from app import logsetup

logsetup.setup()
logsetup.install_excepthook()
logsetup.startup_banner()

import logging                                          # noqa: E402

from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from app.database import init_db
from app.plugin_loader import load_all_plugins
from app.scheduler import start, load_all_tasks, shutdown
import atexit
import updater                       # 读版本号（静态资源缓存破除用）

log = logging.getLogger("app")


def create_app():
    log.info("正在初始化应用…")
    app = Flask(__name__,
                template_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates"),
                static_folder=os.path.join(os.path.dirname(__file__), "static"))
    app.secret_key = os.environ.get("CHECKIN_SECRET_KEY", "checkin-default-key-change-me")

    # 模板改动即时生效：Flask 默认在非 debug 模式下**永久缓存模板**，
    # 自动更新换了 templates/*.html 后不重启就看不到新界面
    # （实测踩过：更新到 1.5.5 后设置页还是旧的样子，用户以为更新没成功）。
    # 打开 auto_reload 后每次渲染都会检查 mtime，改了就读新的。
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    # 静态文件同理：加长缓存会把旧 CSS/JS 一直发给浏览器。
    # 设一个较短值（1 小时）即可 —— 模板里的引用带版本参数时更容易失效，
    # 但这里没有加参数，所以别设太长。
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600

    init_db()
    # 可选组件（本地验证码识别包）—— 必须在插件加载前注入 sys.path，
    # 否则插件 import captcha 时找不到用户上传的 ddddocr 等库。
    from app import extras
    try:
        if extras.apply_all():
            log.info("已加载本地识别组件包")
    except Exception as e:            # noqa: BLE001 —— 组件坏了不该阻止启动
        log.warning("加载组件包失败（忽略）：%s", e)

    # 访问鉴权（密码保护）。必须在注册蓝图**之前**挂上 before_request，
    # 否则存在一个极短的窗口期：应用刚起、钩子还没装，此时访问无需密码。
    from app import auth
    # 安装向导里设的初始密码：读一次、落哈希、删明文（幂等，出错不阻断启动）
    try:
        if auth.apply_wizard_password():
            log.info("已应用安装向导中设置的访问密码")
    except Exception as e:            # noqa: BLE001
        log.warning("应用向导密码失败（忽略，可用默认密码登录）：%s", e)
    app.before_request(auth.check_auth)
    app.permanent_session_lifetime = timedelta(days=auth.SESSION_DAYS)

    try:
        load_all_plugins()
    except Exception:
        log.exception("加载插件失败")
        raise
    start()
    load_all_tasks()
    atexit.register(shutdown)

    from app.routes import (index, task_api, plugin_api, notify_api,
                            system_api, har_api, update_api, extras_api,
                            transfer_api, auth_api)
    app.register_blueprint(index.bp)
    app.register_blueprint(auth_api.bp)
    app.register_blueprint(task_api.bp, url_prefix="/api/tasks")
    app.register_blueprint(plugin_api.bp, url_prefix="/api/plugins")
    app.register_blueprint(notify_api.bp, url_prefix="/api/notify")
    app.register_blueprint(system_api.bp, url_prefix="/api/system")
    # har_api / update_api 的路径都写全了（/har、/api/har、/settings、/api/update），
    # 所以不加 url_prefix
    app.register_blueprint(har_api.bp)
    app.register_blueprint(update_api.bp)
    app.register_blueprint(extras_api.bp, url_prefix="/api/extras")
    app.register_blueprint(transfer_api.bp, url_prefix="/api/transfer")

    # 给所有模板注入版本号，用于静态资源加 ?v= 打破浏览器缓存。
    # ⚠️ 为什么必须有：之前 base.html 写死 `/static/css/app.css` 无版本参数，
    #    而 SEND_FILE_MAX_AGE_DEFAULT=3600（缓存 1 小时）—— 更新后浏览器
    #    仍用缓存里的旧 CSS，用户看到"样式改了却没生效"（实测踩过：
    #    scrollbar-gutter、checkbox 宽度回退都没生效，用户以为改动没做）。
    #    带上版本号后，每次发版 URL 变化，缓存自然失效。
    @app.context_processor
    def inject_app_version():
        try:
            v = updater.current_version().get("version", "0")
        except Exception:                       # noqa: BLE001
            v = "0"
        return {"app_version": v,
                # 全局提示用：鉴权开着且还是默认密码时，界面要常驻催改
                "auth_default_pw": auth.IS_DEFAULT_PASSWORD}

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "Not found"}), 404

    @app.errorhandler(500)
    def server_error(e):
        return jsonify({"error": "Internal server error"}), 500

    global _APP_INSTANCE
    _APP_INSTANCE = app
    log.info("应用初始化完成（端口 %s，日志 %s）",
             os.environ.get("PORT", 5800), logsetup.log_file())
    return app


# 后台线程（如自动更新的进度上报线程）里没有请求上下文，
# `current_app` 会直接抛异常。这里留一个应用实例引用给它们用。
# 本程序是单进程单应用，模块级变量足够；取不到就返回 None，调用方按"跳过"处理。
_APP_INSTANCE = None


def get_app_instance():
    """获取当前 Flask 应用实例（供无请求上下文的代码使用）"""
    return _APP_INSTANCE


if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get("PORT", 5800))
    app.run(host="0.0.0.0", port=port, debug=False)
