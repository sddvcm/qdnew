"""任务 CRUD API"""
from flask import Blueprint, request, jsonify
from app.models import TaskModel, PluginModel, CheckinLogModel
from app.scheduler import add_task as sched_add, remove_task as sched_remove, update_task as sched_update
from app.engine import execute_checkin

bp = Blueprint("task_api", __name__)


@bp.route("", methods=["GET"])
def list_tasks():
    tasks = TaskModel.get_all()
    return jsonify(tasks)


@bp.route("/<int:task_id>", methods=["GET"])
def get_task(task_id):
    task = TaskModel.get_by_id(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(task)


@bp.route("", methods=["POST"])
def create_task():
    data = request.get_json()
    if not data or "plugin_id" not in data or "name" not in data:
        return jsonify({"error": "缺少必填字段: plugin_id, name"}), 400

    plugin = PluginModel.get_by_id(data["plugin_id"])
    if not plugin:
        return jsonify({"error": "插件不存在"}), 400

    tid = TaskModel.create(data)
    task = TaskModel.get_by_id(tid)
    if task["enabled"]:
        sched_add(task)
    return jsonify(task), 201


@bp.route("/<int:task_id>", methods=["PUT"])
def update_task(task_id):
    task = TaskModel.get_by_id(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    data = request.get_json()
    TaskModel.update(task_id, data)
    task = TaskModel.get_by_id(task_id)
    sched_update(task)
    return jsonify(task)


@bp.route("/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    task = TaskModel.get_by_id(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    TaskModel.delete(task_id)
    sched_remove(task_id)
    return jsonify({"success": True})


@bp.route("/<int:task_id>/run", methods=["POST"])
def run_task(task_id):
    task = TaskModel.get_by_id(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    execute_checkin(task_id)
    task = TaskModel.get_by_id(task_id)
    return jsonify({
        "success": task["last_result"] == "success",
        "message": task["last_message"],
        "extra": task["last_extra"],
    })


@bp.route("/<int:task_id>/logs", methods=["GET"])
def task_logs(task_id):
    limit = request.args.get("limit", 365, type=int)
    logs = CheckinLogModel.get_by_task(task_id, limit)
    return jsonify(logs)


@bp.route("/<int:task_id>/calendar", methods=["GET"])
def task_calendar(task_id):
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    calendar = CheckinLogModel.get_calendar(task_id, year, month)
    return jsonify(calendar)
