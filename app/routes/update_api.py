"""自动更新 API + 设置页

页面：
    GET  /settings              系统设置（更新源配置 + 检查/执行更新）

API：
    GET  /api/update/status     当前版本 + 已配置的更新源 + 备份列表
    POST /api/update/config     保存更新源
    POST /api/update/check      检查是否有新版本（只读，不下载）
    POST /api/update/run        执行更新（下载+校验+写入+重载插件）
    GET  /api/update/manifest   生成并下载本仓库的 update_manifest.json（发版用）
"""
import json

from flask import Blueprint, Response, jsonify, render_template, request

import updater
from app.database import get_db
from app.plugin_loader import load_all_plugins

bp = Blueprint("update_api", __name__)

SETTING_KEY = "update_source"


def _get_source() -> str:
    db = get_db()
    row = db.execute("SELECT value FROM system_config WHERE key=?", (SETTING_KEY,)).fetchone()
    db.close()
    return (row["value"] if row else "") or ""


def _set_source(value: str):
    db = get_db()
    db.execute(
        "INSERT INTO system_config (key, value, updated_at) "
        "VALUES (?,?,datetime('now','localtime')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=datetime('now','localtime')",
        (SETTING_KEY, value or ""))
    db.commit()
    db.close()


# ==================== 页面 ====================

@bp.route("/settings")
def settings_page():
    return render_template("settings.html", source=_get_source())


# ==================== API ====================

@bp.route("/api/update/status", methods=["GET"])
def status():
    return jsonify({
        "version": updater.current_version(),
        "source": _get_source(),
        "backups": updater.list_backups(),
        "allowed_hosts": list(updater.ALLOWED_HOSTS),
    })


@bp.route("/api/update/config", methods=["POST"])
def set_config():
    data = request.get_json(silent=True) or {}
    source = (data.get("source") or "").strip()

    # 保存前先校验格式，避免存进去一个之后才发现不能用
    if source:
        try:
            normalized = updater.normalize_source(source)
        except updater.UpdateError as e:
            return jsonify({"success": False, "message": str(e)}), 400
        # 存规范化后的 web 地址（去掉可能带的分支尾巴，分支信息保留）
        branch = normalized["branch"]
        default_like = branch in ("main", "master")
        source = normalized["web_url"] + ("" if default_like else f"/tree/{branch}")

    _set_source(source)
    return jsonify({"success": True, "source": source})


@bp.route("/api/update/check", methods=["POST"])
def check():
    source = _get_source()
    if not source:
        return jsonify({"success": False, "message": "请先在上方填写更新源仓库地址",
                        "result": None}), 400
    result = updater.check_update(source)
    return jsonify({
        "success": not result.get("error"),
        "message": result.get("error") or "",
        "result": result,
    })


@bp.route("/api/update/run", methods=["POST"])
def run():
    """执行更新。

    ⚠️ 两点注意：
    1. 这是**危险的写操作**（会覆盖程序文件），接口层做了源必填校验，
       但真正的防护在 updater 里（域名白名单 + 路径白名单 + 哈希校验 + 备份回滚）。
    2. 更新后**重载插件**让新代码生效；但 Flask 的模块一旦 import 就不会重新
       执行，所以「完全生效」仍建议重启容器。页面会明确告知用户这一点，
       不要谎称"无需重启"。
    """
    source = _get_source()
    if not source:
        return jsonify({"success": False, "message": "请先配置更新源"}), 400

    data = request.get_json(silent=True) or {}
    allow_downgrade = bool(data.get("allow_downgrade"))

    result = updater.run_update(source, allow_downgrade=allow_downgrade)

    if not result.get("updated"):
        return jsonify({
            "success": False,
            "message": result.get("error") or "没有需要更新的内容",
            "result": result,
        })

    # 重载插件，让新插件代码立即生效
    reload_error = ""
    try:
        load_all_plugins()
    except Exception as e:
        reload_error = str(e)

    return jsonify({
        "success": True,
        "message": f"已更新到 {result['version']}，共写入 {len(result['files'])} 个文件",
        "result": result,
        "reload_error": reload_error,
        "need_restart": True,
    })


@bp.route("/api/update/manifest", methods=["GET"])
def manifest():
    """生成当前代码的清单（发版时下载下来提交到仓库根目录）"""
    version = request.args.get("version", "").strip()
    notes = request.args.get("notes", "").strip()
    if not version:
        version = updater.current_version().get("version", "0.0.0")
    data = updater.build_manifest(version, notes)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''update_manifest.json"},
    )
