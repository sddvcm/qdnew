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

    init_db()
    load_all_plugins()
    start()
    load_all_tasks()
    atexit.register(shutdown)

    from app.routes import (index, task_api, plugin_api, notify_api,
                            system_api, har_api, update_api)
    app.register_blueprint(index.bp)
    app.register_blueprint(task_api.bp, url_prefix="/api/tasks")
    app.register_blueprint(plugin_api.bp, url_prefix="/api/plugins")
    app.register_blueprint(notify_api.bp, url_prefix="/api/notify")
    app.register_blueprint(system_api.bp, url_prefix="/api/system")
    # har_api / update_api 的路径都写全了（/har、/api/har、/settings、/api/update），
    # 所以不加 url_prefix
    app.register_blueprint(har_api.bp)
    app.register_blueprint(update_api.bp)

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
