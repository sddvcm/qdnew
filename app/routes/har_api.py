"""HAR 模板管理 API + 页面路由

页面：
    GET  /har                     模板列表
    GET  /har/new                 新建（上传 HAR 或粘 cURL）
    GET  /har/<id>/edit           模板编辑器（勾选请求 / 改成变量 / 填断言）

API：
    POST   /api/har/upload        上传 HAR 文本或 cURL，返回解析后的条目（**不落库**）
    POST   /api/har               新建模板
    GET    /api/har               模板列表
    GET    /api/har/<id>          模板详情
    PUT    /api/har/<id>          保存模板
    DELETE /api/har/<id>          删除模板
    POST   /api/har/<id>/test     用给定变量试跑一次（真发请求）
    GET    /api/har/<id>/export   导出为 HAR 文件
"""
import json
import platform
from datetime import datetime

from flask import Blueprint, Response, jsonify, render_template, request

from app.models import HarTemplateModel
from har import HarParseError, find_variables, parse_curl, parse_har
from har.render import RESERVED_NAMES

bp = Blueprint("har", __name__)


# ==================== 页面 ====================

@bp.route("/har")
def har_list():
    templates = HarTemplateModel.get_all()
    for t in templates:
        t["task_count"] = len(HarTemplateModel.count_tasks_using(t["id"]))
        t["request_count"] = len([e for e in (t.get("entries") or []) if e.get("checked", True)])
    return render_template("har_list.html", templates=templates)


@bp.route("/har/new")
def har_new():
    return render_template("har_new.html")


@bp.route("/har/<int:tid>/edit")
def har_edit(tid):
    """模板编辑器

    ⚠️ 模板里那段 JS 整块被 `{% raw %}` 包住（因为里面大量出现 `{{ }}` 字面示例），
    所以数据不能靠 Jinja 插值传进去，改用占位符 + 服务端替换。
    占位符见 `templates/har_editor.html` 顶部的 `__TPL_ID__` / `__ENTRIES__` / `__VARIABLES__`。
    """
    import json as _json

    tpl = HarTemplateModel.get_by_id(tid)
    if not tpl:
        return "模板不存在", 404

    html = render_template("har_editor.html", template=tpl)
    # 转义 `</` 防止 JSON 里的字符串提前闭合 <script>
    def _js(obj):
        return _json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")

    html = html.replace("__TPL_ID__", str(tpl["id"]))
    html = html.replace("__ENTRIES__", _js(tpl.get("entries") or []))
    html = html.replace("__VARIABLES__", _js(tpl.get("variables") or []))
    html = html.replace("__NAME__", tpl["name"] or "")
    html = html.replace("__NOTE__", tpl["note"] or "")
    html = html.replace("__HOST__", tpl["host"] or "")
    return html


# ==================== API ====================

@bp.route("/api/har/upload", methods=["POST"])
def upload():
    """解析上传内容，返回候选请求列表（不落库，让用户先确认要勾哪些）"""
    data = request.get_json(silent=True) or {}
    raw = data.get("content") or ""
    source = data.get("source") or "har"   # har | curl

    if not raw.strip():
        return jsonify({"success": False, "message": "内容为空"}), 400

    try:
        if source == "curl":
            entries = parse_curl(raw)
        else:
            entries = parse_har(raw)
    except HarParseError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"success": False, "message": f"解析失败：{exc}"}), 500

    # 自动推断站点主机名，作为模板名默认值
    host = ""
    for en in entries:
        url = (en.get("request") or {}).get("url") or ""
        if url.startswith("http"):
            host = url.split("/")[2]
            break

    return jsonify({
        "success": True,
        "entries": entries,
        "suggest_name": host or "未命名模板",
        "host": host,
        "total": len(entries),
        "checked": len([e for e in entries if e.get("checked", True)]),
    })


@bp.route("/api/har", methods=["GET"])
def list_templates():
    return jsonify(HarTemplateModel.get_all())


@bp.route("/api/har", methods=["POST"])
def create_template():
    data = request.get_json(silent=True) or {}
    entries = data.get("entries") or []
    if not entries:
        return jsonify({"success": False, "message": "模板至少要有 1 个请求"}), 400
    tid = HarTemplateModel.create({
        "name": data.get("name") or "未命名模板",
        "host": data.get("host", ""),
        "note": data.get("note", ""),
        "entries": entries,
        "variables": data.get("variables") or find_variables(entries),
    })
    return jsonify({"success": True, "id": tid}), 201


@bp.route("/api/har/<int:tid>", methods=["GET"])
def get_template(tid):
    tpl = HarTemplateModel.get_by_id(tid)
    if not tpl:
        return jsonify({"error": "模板不存在"}), 404
    return jsonify(tpl)


@bp.route("/api/har/<int:tid>", methods=["PUT"])
def update_template(tid):
    tpl = HarTemplateModel.get_by_id(tid)
    if not tpl:
        return jsonify({"error": "模板不存在"}), 404
    data = request.get_json(silent=True) or {}
    if "entries" in data and not data["entries"]:
        return jsonify({"success": False, "message": "模板至少要有 1 个请求"}), 400
    # 变量列表若没传，按当前请求定义重新推断
    if "variables" not in data and "entries" in data:
        data["variables"] = find_variables(data["entries"])
    HarTemplateModel.update(tid, data)
    return jsonify({"success": True})


@bp.route("/api/har/<int:tid>", methods=["DELETE"])
def delete_template(tid):
    used = HarTemplateModel.count_tasks_using(tid)
    if used:
        names = "、".join(u["name"] for u in used[:3])
        return jsonify({
            "success": False,
            "message": f"还有 {len(used)} 个任务在用这个模板（{names}），请先改掉它们的模板选择",
            "used_by": used,
        }), 400
    HarTemplateModel.delete(tid)
    return jsonify({"success": True})


@bp.route("/api/har/<int:tid>/export", methods=["GET"])
def export_template(tid):
    from har import entries_to_har

    tpl = HarTemplateModel.get_by_id(tid)
    if not tpl:
        return jsonify({"error": "模板不存在"}), 404
    payload = json.dumps(entries_to_har(tpl["entries"], tpl["name"]), ensure_ascii=False, indent=2)
    filename = f"{tpl['name']}.har"
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@bp.route("/api/har/<int:tid>/test", methods=["POST"])
def test_template(tid):
    """真发请求试跑模板。

    ⚠️ 这是**唯一**会真实发起外部请求的接口，且是同步阻塞的 —— 浏览器点一次
    跑一次，不要在前端做自动重试/轮询，否则会反复打真实站点。
    """
    from plugins.har_template import HarTemplatePlugin

    tpl = HarTemplateModel.get_by_id(tid)
    if not tpl:
        return jsonify({"error": "模板不存在"}), 404

    data = request.get_json(silent=True) or {}
    variables = data.get("variables") or {}
    cookies = data.get("cookies") or {}
    if isinstance(cookies, str):
        from har import parse_cookie_string

        cookies = parse_cookie_string(cookies)

    plugin = HarTemplatePlugin()
    result = plugin.run_template(tpl["entries"], variables, cookies)
    return jsonify({
        "success": result.success,
        "message": result.message,
        "logs": result.extra.get("logs", ""),
        "variables": result.extra.get("variables", {}),
        "steps": result.extra.get("steps", 0),
        "cookie": result.cookie,
    })


@bp.route("/api/har/variables", methods=["POST"])
def detect_variables():
    """按当前请求定义重新推断变量列表（编辑器里加完变量后刷新表单用）"""
    data = request.get_json(silent=True) or {}
    entries = data.get("entries") or []
    found = find_variables(entries)
    return jsonify({
        "variables": found,
        "reserved": sorted(RESERVED_NAMES),
        "env": {
            "python": platform.python_version(),
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    })
