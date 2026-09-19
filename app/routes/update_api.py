"""自动更新 API + 设置页

页面：
    GET  /settings              系统设置（更新源配置 + 检查/执行更新）

API：
    GET  /api/update/status     当前版本 + 已配置的更新源 + 备份列表
    POST /api/update/config     保存更新源
    POST /api/update/check      检查是否有新版本（只读，不下载）
    POST /api/update/run        执行更新（下载+校验+写入+重载插件）

注：原先还有个 /api/update/manifest（在网页上生成清单下载，即「发版辅助」），
已于 v1.5.5 移除 —— 发版在开发机上用 `updater.build_manifest()` 脚本化完成，
更可靠（网页版只能哈希"当前这台机器"的文件，且容易忘记先改 version.json）。
"""
from flask import Blueprint, current_app, jsonify, render_template, request

import updater
from app.database import get_db
from app.plugin_loader import load_all_plugins

bp = Blueprint("update_api", __name__)

SETTING_KEY = "update_source"
PROXY_KEY = "update_proxy"


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


def _get_proxy() -> str:
    db = get_db()
    row = db.execute("SELECT value FROM system_config WHERE key=?", (PROXY_KEY,)).fetchone()
    db.close()
    return (row["value"] if row else "") or ""


def _set_proxy(value: str):
    db = get_db()
    db.execute(
        "INSERT INTO system_config (key, value, updated_at) "
        "VALUES (?,?,datetime('now','localtime')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=datetime('now','localtime')",
        (PROXY_KEY, (value or "").strip()))
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
        "proxy": _get_proxy(),
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


@bp.route("/api/update/proxy", methods=["POST"])
def set_proxy():
    """保存/清空更新用的代理地址。"""
    data = request.get_json(silent=True) or {}
    proxy = (data.get("proxy") or "").strip()
    _set_proxy(proxy)
    return jsonify({"success": True, "proxy": proxy})


@bp.route("/api/update/proxy/test", methods=["POST"])
def test_proxy():
    """测试某代理能否连通 GitHub 更新源（不落库）。

    请求体可带 {proxy} 测刚填的地址；也可不带 proxy，则测已保存的代理。
    """
    data = request.get_json(silent=True) or {}
    proxy = (data.get("proxy") or "").strip() or _get_proxy()
    if not proxy:
        return jsonify({"success": False,
                        "message": "请先填写代理地址，或在请求中带上 proxy"}), 400
    res = updater.check_proxy(proxy)
    return jsonify({"success": res.get("ok"), "message": res.get("error") or "",
                    "result": res})


@bp.route("/api/update/check", methods=["POST"])
def check():
    source = _get_source()
    if not source:
        return jsonify({"success": False, "message": "请先在上方填写更新源仓库地址",
                        "result": None}), 400
    proxy = _get_proxy()
    result = updater.check_update(source, proxy=proxy or None)
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
    proxy = _get_proxy()

    result = updater.run_update(source, allow_downgrade=allow_downgrade,
                                 proxy=proxy or None)

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

    # 清掉模板缓存，让新的 templates/*.html 立即生效。
    # 虽然 create_app() 里开了 TEMPLATES_AUTO_RELOAD，但这是"更新后"的关键路径，
    # 显式清一次更稳妥（auto_reload 靠 mtime 判断，某些文件系统精度/时区差异下
    # 可能仍认为文件没变）。实测踩过：更新到 1.5.5 后设置页还是旧界面，
    # 用户以为更新失败 —— 其实就是模板被 Jinja 缓存住了。
    try:
        current_app.jinja_env.cache.clear()
    except Exception:           # noqa: BLE001 —— 清不掉也不该让更新报错
        pass

    return jsonify({
        "success": True,
        "message": f"已更新到 {result['version']}，共写入 {len(result['files'])} 个文件",
        "result": result,
        "reload_error": reload_error,
        # 涉及 app/ 核心模块（路由、蓝图）的改动仍需重启才完全生效，
        # 但模板/静态文件/插件现在已即时生效，不必再让用户无脑重启。
        "need_restart": any(
            str(f).startswith(("app/", "har/")) for f in result.get("files", [])
        ),
    })
