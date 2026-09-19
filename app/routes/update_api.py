"""自动更新 API + 设置页

页面：
    GET  /settings              系统设置（更新源配置 + 检查/执行更新）

API：
    GET  /api/update/status     当前版本 + 安装包版本 + 已配置的更新源 + 备份列表
    POST /api/update/config     保存更新源
    POST /api/update/check      检查是否有新版本（只读，不下载）
    POST /api/update/run        执行更新（下载+校验+写入+重载插件）

注：原先还有个 /api/update/manifest（在网页上生成清单下载，即「发版辅助」），
   已于 v1.5.5 移除 —— 发版在开发机上用 `updater.build_manifest()` 脚本化完成，
   更可靠（网页版只能哈希"当前这台机器"的文件，且容易忘记先改 version.json）。
   """
import os
import re
import threading
from datetime import datetime

from flask import (Blueprint, current_app, jsonify, render_template, request,
                   has_app_context)

import updater
from app.database import get_db
from app.plugin_loader import load_all_plugins

bp = Blueprint("update_api", __name__)

SETTING_KEY = "update_source"
PROXY_KEY = "update_proxy"

# ---- 更新进度（内存态，供前端轮询）----
# 更新是"后台线程 + 轮询"，所以进度必须放在进程内可共享的地方。
# 只服务单个部署点，用模块级 dict + 锁足够，不必上 Redis/DB。
_PROGRESS: dict = {}
_PROGRESS_LOCK = threading.Lock()


def _get_app():
    """拿到 Flask app 对象（后台线程里没有请求上下文，current_app 会抛异常）。

    包内自带运行时且是单进程，直接 import 是安全的；import 失败就返回 None，
    调用方按"拿不到就跳过"处理 —— 清缓存失败不该影响更新成功与否。
    """
    if has_app_context():
        return current_app._get_current_object()
    try:
        from app.main import get_app_instance
        return get_app_instance()
    except Exception:                           # noqa: BLE001
        return None


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

def _pkg_version() -> str:
    """安装包（fpk）的版本 —— 应用中心显示的那个。

    fpk 装机后安装根目录有个 `manifest` 文本（version = x.y.z）。
    自动更新只改 version.json（运行版本），**改不了应用中心的记录** ——
    那是飞牛包管理器按安装时的 manifest 记的，只有重装/升级 fpk 才会变。
    把两个版本都摆出来，用户就不会困惑"明明更新了怎么应用中心还是旧号"。
    开发机/Docker 没有这个文件 → 返回空串，前端不显示这一行。
    """
    path = os.path.join(updater.ROOT, "manifest")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"\s*version\s*=\s*(\S+)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return ""


@bp.route("/api/update/status", methods=["GET"])
def status():
    return jsonify({
        "version": updater.current_version(),
        "pkg_version": _pkg_version(),
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
    """执行更新（**后台线程**跑，立即返回；前端轮询 /api/update/progress）。

    为什么要后台化：整个更新要下载几十个文件（走代理可能几十秒到几分钟），
    同步等待的话前端只能转一个"更新中"，用户无法区分"在跑"和"卡住了"。
    现在立刻返回并用轮询查进度，界面能显示百分比和当前文件。

    ⚠️ 两点注意：
    1. 这是**危险的写操作**（会覆盖程序文件），接口层做了源必填校验，
       但真正的防护在 updater 里（域名白名单 + 路径白名单 + 哈希校验 + 备份回滚）。
    2. 更新后**重载插件 + 清模板缓存**让改动尽量即时生效；但 Flask 的模块
       一旦 import 就不会重新执行，涉及 app/ 的改动仍需重启才完全生效。
       页面会如实告知，不要谎称"无需重启"。
    """
    source = _get_source()
    if not source:
        return jsonify({"success": False, "message": "请先配置更新源"}), 400

    with _PROGRESS_LOCK:
        if _PROGRESS.get("running"):
            return jsonify({"success": False,
                            "message": "已有更新任务正在执行，请等待完成"}), 409
        _PROGRESS.clear()
        _PROGRESS.update({"running": True, "phase": "starting", "done": 0,
                          "total": 0, "message": "正在准备…", "percent": 0,
                          "success": None, "error": "",
                          "started_at": datetime.now().isoformat(timespec="seconds")})

    data = request.get_json(silent=True) or {}
    allow_downgrade = bool(data.get("allow_downgrade"))
    proxy = _get_proxy()

    t = threading.Thread(target=_run_update_worker,
                         args=(source, allow_downgrade, proxy or None),
                         daemon=True)
    t.start()
    return jsonify({"success": True, "message": "更新已开始", "progress": dict(_PROGRESS)})


@bp.route("/api/update/progress", methods=["GET"])
def progress():
    """查询更新进度（前端每秒轮询）。"""
    with _PROGRESS_LOCK:
        return jsonify(dict(_PROGRESS))


def _run_update_worker(source, allow_downgrade, proxy):
    """后台执行更新并把进度写进 _PROGRESS。"""
    def on_progress(phase, done, total, msg):
        # 四个阶段给不同权重：下载最耗时给 0~85%，备份 85~92%，写入 92~100%。
        # 这样进度条不会在下载阶段"卡"很久不动，用户能看出确实在推进。
        if phase == "fetching":
            pct = int(done / total * 85) if total else 0
        elif phase == "backing_up":
            pct = 85 + (int(done / total * 7) if total else 0)
        elif phase == "writing":
            pct = 92 + (int(done / total * 8) if total else 0)
        else:                                   # done
            pct = 100
        with _PROGRESS_LOCK:
            _PROGRESS.update({"phase": phase, "done": done, "total": total,
                              "message": msg or "", "percent": min(pct, 100)})

    try:
        result = updater.run_update(source, allow_downgrade=allow_downgrade,
                                    proxy=proxy, progress=on_progress)
    except Exception as e:                      # noqa: BLE001
        result = {"updated": False, "error": f"{type(e).__name__}: {e}"}

    if not result.get("updated"):
        with _PROGRESS_LOCK:
            _PROGRESS.update({"running": False, "success": False,
                              "error": result.get("error") or "没有需要更新的内容",
                              "message": result.get("error") or "没有需要更新的内容"})
        return

    # 重载插件，让新插件代码立即生效
    reload_error = ""
    try:
        load_all_plugins()
    except Exception as e:                      # noqa: BLE001
        reload_error = str(e)

    # 清掉模板缓存，让新的 templates/*.html 立即生效。
    # 虽然 create_app() 里开了 TEMPLATES_AUTO_RELOAD，但这是"更新后"的关键路径，
    # 显式清一次更稳妥（auto_reload 靠 mtime 判断，某些文件系统精度下可能仍
    # 认为文件没变）。实测踩过：更新到 1.5.5 后设置页还是旧界面，用户以为
    # 更新失败 —— 其实就是模板被 Jinja 缓存住了。
    #
    # ⚠️ 这里没有请求上下文，current_app 会抛异常 —— 要拿到应用对象再清。
    try:
        app = _get_app()
        if app is not None:
            app.jinja_env.cache.clear()
    except Exception:                           # noqa: BLE001 —— 清不掉不影响更新
        pass

    with _PROGRESS_LOCK:
        _PROGRESS.update({
            "running": False, "success": True, "phase": "done",
            "percent": 100, "error": "",
            "message": f"已更新到 {result['version']}，共写入 {len(result['files'])} 个文件",
            "result": {
                "version": result.get("version", ""),
                "files": result.get("files", []),
                "file_count": len(result.get("files", [])),
                "backup_dir": result.get("backup_dir", ""),
                "reload_error": reload_error,
                # 涉及 app/ 核心模块（路由、蓝图）的改动仍需重启才完全生效，
                # 模板/静态文件/插件现在已即时生效，不必再让用户无脑重启。
                "need_restart": any(
                    str(f).startswith(("app/", "har/"))
                    for f in result.get("files", [])
                ),
            },
        })
