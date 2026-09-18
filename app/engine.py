"""签到执行引擎"""
import time
import traceback
from .plugin_loader import get_plugin
from .models import TaskModel
from .notifier import send_notification


def execute_checkin(task_id: int):
    """执行单个签到任务"""
    task = TaskModel.get_by_id(task_id)
    if not task or not task["enabled"]:
        return

    # ⚠️ 插件查找顺序：必须是「插件 name(唯一键)」优先。
    # 老代码先传 `plugin_display_name`（中文显示名，如 "HAR 通用签到"）去查，
    # 这个值**永远不在** _loaded_plugins 的键里，每次都靠后面 plugin_id 兜底才成功。
    # 现在改成先按 plugin_id 查 name，再用 display_name 兜底，两条路都能走通。
    plugin = None
    pid = task.get("plugin_id")
    if pid:
        from .models import PluginModel
        p = PluginModel.get_by_id(pid)
        if p:
            plugin = get_plugin(p["name"])
    if not plugin and task.get("plugin_display_name"):
        plugin = get_plugin(task["plugin_display_name"])
    if not plugin:
        TaskModel.update_run_result(task_id, "failed", "插件未找到", {}, error_trace="Plugin not loaded")
        return

    config = {
        "username": task.get("username", ""),
        "password": task.get("password", ""),
        "cookie": task.get("cookie", ""),
        "site_url": task.get("site_url", ""),
        "params": task.get("params", {}),
    }

    start_time = time.time()

    try:
        pre_result = plugin.pre_checkin(config)
        if pre_result:
            result = pre_result
        else:
            result = plugin.checkin(config)
            result = plugin.post_checkin(config, result)

        # 登录类插件回写最新 Cookie（加密存档），下次自动复用
        if getattr(result, "cookie", ""):
            try:
                TaskModel.update_cookie(task_id, result.cookie)
            except Exception:
                pass

        duration_ms = int((time.time() - start_time) * 1000)
        result_str = "success" if result.success else "failed"

        # 通知结果要落到任务日志里：推送失败（Token 无效 / 未关注公众号）之前
        # 是完全静默的，用户以为收到了其实没发出去
        notify_outcomes = send_notification(
            task["name"], result_str, result.message, result.extra, task_id)
        failed_pushes = [o for o in (notify_outcomes or []) if not o.get("ok")]
        if failed_pushes:
            detail = "；".join(f"{o['channel']}: {o['detail']}" for o in failed_pushes)
            result.extra["notify_error"] = detail
            result.message = f"{result.message}（通知推送失败：{detail}）"

        TaskModel.update_run_result(
            task_id, result_str, result.message, result.extra,
            duration_ms=duration_ms,
            error_trace="" if result.success else result.message
        )

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        error_msg = f"{str(e)}\n{traceback.format_exc()}"
        TaskModel.update_run_result(
            task_id, "failed", f"执行异常: {str(e)}", {},
            duration_ms=duration_ms, error_trace=error_msg
        )
        send_notification(task["name"], "failed", f"执行异常: {str(e)}", {}, task_id)


def execute_all_checkins():
    """执行所有启用的签到任务"""
    tasks = TaskModel.get_all()
    for task in tasks:
        if task["enabled"]:
            try:
                execute_checkin(task["id"])
            except Exception as e:
                print(f"[Engine] Task {task['id']} failed: {e}")
