"""通知配置 API"""
from flask import Blueprint, request, jsonify
from app.models import NotifyModel

bp = Blueprint("notify_api", __name__)


@bp.route("", methods=["GET"])
def list_notify():
    configs = NotifyModel.get_all()
    return jsonify(configs)


@bp.route("", methods=["POST"])
def create_notify():
    data = request.get_json()
    if not data or "notify_type" not in data:
        return jsonify({"error": "缺少 notify_type"}), 400
    nid = NotifyModel.create(data)
    return jsonify({"id": nid}), 201


@bp.route("/<int:nid>", methods=["PUT"])
def update_notify(nid):
    data = request.get_json()
    NotifyModel.update(nid, data)
    return jsonify({"success": True})


@bp.route("/<int:nid>", methods=["DELETE"])
def delete_notify(nid):
    NotifyModel.delete(nid)
    return jsonify({"success": True})


@bp.route("/task/<int:task_id>", methods=["GET"])
def task_notify(task_id):
    configs = NotifyModel.get_for_task(task_id)
    return jsonify(configs)


@bp.route("/task/<int:task_id>", methods=["PUT"])
def set_task_notify(task_id):
    data = request.get_json()
    notify_ids = data.get("notify_ids", [])
    on_success = data.get("on_success", True)
    on_failure = data.get("on_failure", True)
    NotifyModel.set_task_notify(task_id, notify_ids, on_success, on_failure)
    return jsonify({"success": True})
