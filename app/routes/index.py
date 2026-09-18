"""首页路由"""
from flask import Blueprint, render_template
from app.models import TaskModel, PluginModel, CheckinLogModel, NotifyModel
from app.scheduler import get_next_run_time
from datetime import datetime

bp = Blueprint("index", __name__)


@bp.route("/")
def index():
    tasks = TaskModel.get_all()
    plugins = PluginModel.get_all()
    for task in tasks:
        task["next_run_str"] = get_next_run_time(task["id"]) or ""
    return render_template("index.html", tasks=tasks, plugins=plugins, now=datetime.now())


@bp.route("/task/<int:task_id>")
def task_detail(task_id):
    task = TaskModel.get_by_id(task_id)
    if not task:
        return "任务不存在", 404
    logs = CheckinLogModel.get_by_task(task_id, limit=365)
    calendar = CheckinLogModel.get_calendar(task_id)
    task["next_run_str"] = get_next_run_time(task_id) or ""
    return render_template("detail.html", task=task, logs=logs, calendar=calendar)


@bp.route("/task/add")
def task_add():
    from app.models import PluginModel as PM
    plugins = PM.get_all()
    return render_template("task_form.html", task=None, plugins=plugins, is_edit=False)


@bp.route("/task/<int:task_id>/edit")
def task_edit(task_id):
    task = TaskModel.get_by_id(task_id)
    if not task:
        return "任务不存在", 404
    from app.models import PluginModel as PM
    plugins = PM.get_all()
    return render_template("task_form.html", task=task, plugins=plugins, is_edit=True)


@bp.route("/plugin/import")
def plugin_import_page():
    return render_template("plugin_import.html")
