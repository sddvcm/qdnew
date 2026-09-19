"""任务导出 / 导入 API（加密 .qdpack 格式）。

页面：系统设置页的「任务导出 / 导入」区块。

API：
    POST   /api/transfer/export     导出（返回文件下载）
    POST   /api/transfer/inspect    上传并解密预览（不写库）
    POST   /api/transfer/import     上传并导入（写库）

为什么 export 用 POST 而不是 GET：导出范围（勾选哪些任务、要不要带模板/通知）
是结构化参数，POST + JSON 更合适；响应依然是文件流。
"""
import io
import json

from flask import Blueprint, Response, jsonify, request

from app import transfer
from app.database import get_db
from app.plugin_loader import load_all_plugins

bp = Blueprint("transfer_api", __name__)


def _task_options():
    """给前端用的可导出任务列表（只给 id + 名字，不给敏感字段）"""
    db = get_db()
    rows = db.execute(
        "SELECT t.id, t.name, p.display_name AS plugin FROM tasks t "
        "LEFT JOIN plugins p ON t.plugin_id=p.id ORDER BY t.id"
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


@bp.route("/options", methods=["GET"])
def options():
    return jsonify({
        "tasks": _task_options(),
        "has_templates": _count("har_templates") > 0,
        "has_notifies": _count("notify_configs") > 0,
    })


def _count(table: str) -> int:
    db = get_db()
    try:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        db.close()


# ==================== 导出 ====================

@bp.route("/export", methods=["POST"])
def export():
    data = request.get_json(silent=True) or {}
    task_ids = data.get("task_ids")
    if task_ids is not None:
        if not isinstance(task_ids, list):
            return jsonify({"success": False, "message": "task_ids 必须是数组"}), 400
        try:
            task_ids = [int(x) for x in task_ids]
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "task_ids 含非法值"}), 400

    try:
        payload = transfer.collect(
            include_tasks=True,
            include_templates=bool(data.get("include_templates", True)),
            include_notifies=bool(data.get("include_notifies", True)),
            include_settings=bool(data.get("include_settings", False)),
            task_ids=task_ids,
        )
        blob = transfer.pack(payload)
    except transfer.TransferError as e:
        return jsonify({"success": False, "message": str(e)}), 400

    fname = transfer.export_filename()
    return Response(
        blob,
        mimetype="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{fname}",
            # 让前端知道导出了什么（自定义头，配合 XHR 读取）
            "X-Export-Summary": json.dumps({
                "tasks": len(payload.get(transfer.K_TASKS) or []),
                "templates": len(payload.get(transfer.K_TEMPLATES) or []),
                "notifies": len(payload.get(transfer.K_NOTIFIES) or []),
                "settings": len((payload.get(transfer.K_SETTINGS) or {})),
                "size": len(blob),
            }, ensure_ascii=False),
        },
    )


# ==================== 读取上传内容 ====================

def _read_upload():
    """从请求里取出二进制内容（multipart 或 JSON base64）"""
    f = request.files.get("file")
    if f is not None:
        return f.read()
    payload = request.get_json(silent=True) or {}
    b64 = payload.get("content_base64") or ""
    if b64:
        import base64 as _b64
        try:
            return _b64.b64decode(b64, validate=False)
        except Exception as e:      # noqa: BLE001
            raise transfer.TransferError(f"base64 解码失败：{e}") from e
    return b""


# ==================== 预览 ====================

@bp.route("/inspect", methods=["POST"])
def inspect_pack():
    """上传后先解密看一眼内容（不写库），让用户确认再导入。"""
    try:
        blob = _read_upload()
        info = transfer.summarize(blob)
    except transfer.TransferError as e:
        return jsonify({"success": False, "message": str(e)}), 400
    except Exception as e:          # noqa: BLE001
        return jsonify({"success": False, "message": f"读取失败：{e}"}), 500

    # 顺带标出哪些任务名在本机已存在（供用户判断冲突）
    db = get_db()
    local_tasks = {r["name"] for r in
                   db.execute("SELECT name FROM tasks").fetchall()}
    local_tpls = {r["name"] for r in
                  db.execute("SELECT name FROM har_templates").fetchall()}
    local_notifies = {r["name"] for r in
                      db.execute("SELECT name FROM notify_configs").fetchall()}
    db.close()

    info["conflicts"] = {
        "tasks": sorted(set(info["tasks"]) & local_tasks),
        "templates": sorted(set(info["templates"]) & local_tpls),
        "notifies": sorted(set(info["notifies"]) & local_notifies),
    }
    return jsonify({"success": True, "info": info})


# ==================== 导入 ====================

@bp.route("/import", methods=["POST"])
def import_pack():
    """执行导入。body 可带 on_conflict（skip/overwrite/rename）。"""
    on_conflict = "skip"
    blob = b""

    # 兼容 multipart（带 on_conflict 表单字段）与 JSON
    f = request.files.get("file")
    if f is not None:
        blob = f.read()
        on_conflict = (request.form.get("on_conflict") or "skip").strip()
    else:
        payload = request.get_json(silent=True) or {}
        on_conflict = str(payload.get("on_conflict") or "skip").strip()
        import base64 as _b64
        b64 = payload.get("content_base64") or ""
        if b64:
            try:
                blob = _b64.b64decode(b64, validate=False)
            except Exception as e:      # noqa: BLE001
                return jsonify({"success": False,
                                "message": f"base64 解码失败：{e}"}), 400

    if not blob:
        return jsonify({"success": False, "message": "没有收到文件内容"}), 400

    try:
        payload = transfer.unpack(blob)
        result = transfer.apply(payload, on_conflict=on_conflict)
    except transfer.TransferError as e:
        return jsonify({"success": False, "message": str(e)}), 400
    except Exception as e:      # noqa: BLE001
        return jsonify({"success": False, "message": f"导入失败：{e}"}), 500

    # 重新加载插件，让新导入的插件/任务能立即用
    reload_error = ""
    try:
        load_all_plugins()
    except Exception as e:      # noqa: BLE001
        reload_error = str(e)

    st = result["stat"]
    return jsonify({
        "success": True,
        "message": f"导入完成：任务 +{st['tasks']['added']}"
                   f"（跳过 {st['tasks']['skipped']}，覆盖 {st['tasks']['overwritten']}）"
                   f"，模板 +{st['templates']['added']}"
                   f"，通知渠道 +{st['notifies']['added']}",
        "stat": st,
        "details": result["details"],
        "reload_error": reload_error,
    })
