"""系统设置 API"""
import json
from flask import Blueprint, request, jsonify
from app.database import get_db

bp = Blueprint("system_api", __name__)


@bp.route("/env", methods=["GET"])
def get_env():
    """运行环境信息 —— 排查「数据到底存哪了 / 共享目录通不通」。

    放在设置页的「环境信息」分页展示。所有探测都不抛异常，
    读不到就如实回报原因（含 open API 的失败详情）。
    """
    from app import fnos

    return jsonify({
        "data": fnos.data_health(),
        "platform": fnos.platform_config(),
        "openapi_ready": fnos.openapi_available(),
    })


@bp.route("/config", methods=["GET"])
def get_configs():
    db = get_db()
    rows = db.execute("SELECT * FROM system_config").fetchall()
    db.close()
    return jsonify({r["key"]: r["value"] for r in rows})


@bp.route("/config", methods=["PUT"])
def set_config():
    data = request.get_json()
    db = get_db()
    for k, v in data.items():
        db.execute(
            "INSERT INTO system_config (key, value, updated_at) VALUES (?,?,datetime('now','localtime')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now','localtime')",
            (k, str(v)))
    db.commit()
    db.close()
    return jsonify({"success": True})


@bp.route("/stats", methods=["GET"])
def get_stats():
    db = get_db()
    total_tasks = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    enabled_tasks = db.execute("SELECT COUNT(*) FROM tasks WHERE enabled=1").fetchone()[0]
    error_tasks = db.execute("SELECT COUNT(*) FROM tasks WHERE status='error'").fetchone()[0]
    today = db.execute(
        "SELECT COUNT(DISTINCT task_id) FROM checkin_logs WHERE checkin_date=date('now','localtime')"
    ).fetchone()[0]
    success_today = db.execute(
        "SELECT COUNT(DISTINCT task_id) FROM checkin_logs WHERE checkin_date=date('now','localtime') AND result='success'"
    ).fetchone()[0]
    db.close()
    return jsonify({
        "total_tasks": total_tasks,
        "enabled_tasks": enabled_tasks,
        "error_tasks": error_tasks,
        "today_checkins": today,
        "today_success": success_today,
    })
