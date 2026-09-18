"""插件管理 API"""
import json
from flask import Blueprint, request, jsonify
from app.models import PluginModel
from app.plugin_loader import get_all_plugins, load_user_plugin, delete_user_plugin

bp = Blueprint("plugin_api", __name__)


@bp.route("", methods=["GET"])
def list_plugins():
    plugins = PluginModel.get_all()
    return jsonify(plugins)


@bp.route("/<int:plugin_id>/schema", methods=["GET"])
def plugin_schema(plugin_id):
    plugin = PluginModel.get_by_id(plugin_id)
    if not plugin:
        return jsonify({"error": "插件不存在"}), 404
    schema = json.loads(plugin["form_schema"])
    return jsonify(schema)


@bp.route("/import", methods=["POST"])
def import_plugin():
    data = request.get_json()
    if not data:
        return jsonify({"error": "无效请求"}), 400

    source_code = data.get("code", "")
    filename = data.get("filename", "custom_plugin.py")
    if not source_code:
        return jsonify({"error": "插件代码不能为空"}), 400

    success, message, info = load_user_plugin(source_code, filename)
    return jsonify({"success": success, "message": message, "plugin": info})


@bp.route("/<string:plugin_name>", methods=["DELETE"])
def delete_plugin(plugin_name):
    success, message = delete_user_plugin(plugin_name)
    return jsonify({"success": success, "message": message})
