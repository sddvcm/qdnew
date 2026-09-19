"""Flask 应用入口"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from app.database import init_db
from app.plugin_loader import load_all_plugins
from app.scheduler import start, load_all_tasks, shutdown
import atexit


def create_app():
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
            print("[extras] 已加载本地识别组件包", flush=True)
    except Exception as e:            # noqa: BLE001 —— 组件坏了不该阻止启动
        print(f"[extras] 加载组件包失败（忽略）：{e}", flush=True)

    load_all_plugins()
    start()
    load_all_tasks()
    atexit.register(shutdown)

    from app.routes import (index, task_api, plugin_api, notify_api,
                            system_api, har_api, update_api, extras_api,
                            transfer_api)
    app.register_blueprint(index.bp)
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

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "Not found"}), 404

    @app.errorhandler(500)
    def server_error(e):
        return jsonify({"error": "Internal server error"}), 500

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get("PORT", 5800))
    app.run(host="0.0.0.0", port=port, debug=False)
