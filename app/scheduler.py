"""APScheduler 定时调度管理"""
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from .models import TaskModel
from .engine import execute_checkin

_scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
_job_ids: dict = {}


def _make_job_id(task_id):
    return f"checkin_task_{task_id}"


def load_all_tasks():
    """启动时从数据库加载所有启用的任务到调度器"""
    tasks = TaskModel.get_all()
    for task in tasks:
        if task["enabled"]:
            add_task(task)


def add_task(task: dict):
    """添加任务到调度器"""
    job_id = _make_job_id(task["id"])
    if job_id in _job_ids:
        _scheduler.remove_job(job_id)

    cron_expr = task.get("cron_expr", "0 9 * * *")
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        parts = ["0", "9", "*", "*", "*"]

    try:
        trigger = CronTrigger(
            minute=parts[0], hour=parts[1], day=parts[2],
            month=parts[3], day_of_week=parts[4],
            timezone="Asia/Shanghai"
        )
        job = _scheduler.add_job(
            execute_checkin, trigger,
            args=[task["id"]], id=job_id,
            replace_existing=True
        )
        _job_ids[job_id] = job
        if job.next_run_time:
            TaskModel.update(task["id"], {"status": "normal"})
    except Exception as e:
        print(f"[Scheduler] Failed to add task {task['id']}: {e}")


def remove_task(task_id: int):
    """从调度器移除任务"""
    job_id = _make_job_id(task_id)
    if job_id in _job_ids:
        _scheduler.remove_job(job_id)
        del _job_ids[job_id]


def update_task(task: dict):
    """更新调度任务"""
    remove_task(task["id"])
    if task["enabled"]:
        add_task(task)


def get_next_run_time(task_id: int):
    """获取下次运行时间"""
    job_id = _make_job_id(task_id)
    job = _job_ids.get(job_id)
    if job and job.next_run_time:
        return job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
    return None


def start():
    """启动调度器"""
    if not _scheduler.running:
        _scheduler.start()


def shutdown():
    """关闭调度器"""
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
