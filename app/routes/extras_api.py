"""可选组件（extras）API + 本地验证码识别管理。

页面：系统设置页里的「本地验证码识别」区块调用这些接口。

API：
    GET    /api/extras/status           组件状态 + 全局识别设置
    POST   /api/extras/upload           上传组件包（multipart，字段 file）
    POST   /api/extras/toggle           启用/停用 {"enabled": true}
    DELETE /api/extras/captcha-local    卸载组件包
    POST   /api/extras/settings         保存全局识别方式/云码配置
    POST   /api/extras/test-local       用一张内置测试图验证本地识别是否真的能跑
"""
import base64

from flask import Blueprint, jsonify, request

from app import extras
from app.database import get_db

bp = Blueprint("extras_api", __name__)

# 全局设置键
K_BACKEND = "captcha_backend"
K_CLOUD_TOKEN = "captcha_cloud_token"
K_CLOUD_TYPE = "captcha_cloud_type"

BACKENDS = ("local", "cloud", "auto")


def _get_cfg(key: str, default: str = "") -> str:
    db = get_db()
    row = db.execute("SELECT value FROM system_config WHERE key=?", (key,)).fetchone()
    db.close()
    return (row["value"] if row else "") or default


def _set_cfg(key: str, value: str):
    db = get_db()
    db.execute(
        "INSERT INTO system_config (key, value, updated_at) "
        "VALUES (?,?,datetime('now','localtime')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=datetime('now','localtime')",
        (key, value or ""))
    db.commit()
    db.close()


def _cfg_all() -> dict:
    return {
        "backend": _get_cfg(K_BACKEND, "cloud") or "cloud",
        "cloud_token": _get_cfg(K_CLOUD_TOKEN, ""),
        "cloud_type": _get_cfg(K_CLOUD_TYPE, "") or "10110",
        "local_available": _local_ok(),
    }


def _local_ok() -> bool:
    """本地识别当前是否真的可用（装了组件且能 import）"""
    try:
        import captcha
        return bool(captcha.local_available())
    except Exception:      # noqa: BLE001
        return False


# ==================== 状态 ====================

@bp.route("/status", methods=["GET"])
def status():
    st = extras.status()
    st["config"] = _cfg_all()
    return jsonify({"success": True, "pack": st})


# ==================== 上传安装 ====================

@bp.route("/upload", methods=["POST"])
def upload():
    """上传组件包。

    接受两种方式（都进内存后交给 extras 校验，不落临时文件）：
      - multipart/form-data，字段名 file
      - application/json {"filename": "...", "content_base64": "..."}
    小包走 JSON 更方便脚本调用；页面用 multipart。
    """
    data = b""
    filename = ""

    f = request.files.get("file")
    if f is not None:
        filename = f.filename or "pack.zip"
        data = f.read()
    else:
        payload = request.get_json(silent=True) or {}
        b64 = payload.get("content_base64") or ""
        filename = payload.get("filename") or "pack.zip"
        if b64:
            try:
                data = base64.b64decode(b64, validate=False)
            except Exception as e:      # noqa: BLE001
                return jsonify({"success": False,
                                "message": f"base64 解码失败：{e}"}), 400

    if not data:
        return jsonify({"success": False,
                        "message": "没有收到文件内容"}), 400
    if not filename.lower().endswith(".zip"):
        return jsonify({"success": False,
                        "message": "只接受 .zip 组件包"}), 400

    try:
        result = extras.install_from_zip(data)
    except extras.ExtrasError as e:
        return jsonify({"success": False, "message": str(e)}), 400
    except Exception as e:              # noqa: BLE001
        return jsonify({"success": False,
                        "message": f"安装失败：{e}"}), 500

    result["config"] = _cfg_all()
    return jsonify({
        "success": True,
        "message": f"本地识别组件已安装（{result.get('extracted_mb', 0)}MB）并启用",
        "pack": result,
    })


# ==================== 启用 / 停用 ====================

@bp.route("/toggle", methods=["POST"])
def toggle():
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))
    try:
        state = extras.set_enabled(enabled)
    except extras.ExtrasError as e:
        return jsonify({"success": False, "message": str(e)}), 400
    return jsonify({
        "success": True,
        "message": "已启用本地识别" if state else "已停用本地识别",
        "pack": {**extras.status(), "config": _cfg_all()},
    })


# ==================== 卸载 ====================

@bp.route("/captcha-local", methods=["DELETE"])
def uninstall():
    res = extras.uninstall()
    return jsonify({**res, "pack": {**extras.status(), "config": _cfg_all()}})


# ==================== 全局识别设置 ====================

@bp.route("/settings", methods=["POST"])
def save_settings():
    data = request.get_json(silent=True) or {}
    backend = str(data.get("backend") or "").strip().lower()
    if backend and backend not in BACKENDS:
        return jsonify({"success": False,
                        "message": f"识别方式只能是 {BACKENDS}"}), 400

    if backend == "local" and not _local_ok():
        return jsonify({
            "success": False,
            "message": "本地识别组件尚未安装/不可用，无法设为「仅本地」。"
                       "请先上传组件包，或改选云码/自动。",
        }), 400

    if backend:
        _set_cfg(K_BACKEND, backend)
    if "cloud_token" in data:
        _set_cfg(K_CLOUD_TOKEN, str(data.get("cloud_token") or "").strip())
    if "cloud_type" in data:
        _set_cfg(K_CLOUD_TYPE, str(data.get("cloud_type") or "").strip())

    return jsonify({"success": True, "message": "已保存",
                    "config": _cfg_all()})


# ==================== 本地识别自检 ====================

# 一张 100x32 的白底黑块图，用于确认本地识别链路能跑通。
#
# ⚠️ 必须是**真实合法**的 PNG（含 IHDR/IDAT/IEND 三个 chunk 与正确 CRC）。
# 踩过的坑：早先这里手写了一段"看着像 base64 的字符串"，PNG 魔数对得上，
# 但后面的 chunk 结构不合法 —— PIL 直接抛 "cannot identify image file"，
# 于是「测试本地识别」永远失败，看起来像组件坏了，其实是测试图本身是坏的。
# 这个 base64 由 struct+zlib 真实生成并用 PIL 打开验证过。
#
# 不追求识别准确（图形不是 ddddocr 训练集风格），只验证：
# 图片能解码 → 模型能加载 → 前向推理能跑通。
_TEST_IMG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAGQAAAAgCAIAAABrSUp5AAAAfElEQVR42u3ZWQoAIAgFwO5/6bpC"
    "C2Xi+C88Biqx1j+rNl0B2WDBggULVjGsqLjnWPe4r2CdOFbECjkmibGivLJixVzDsEpgvffKjfXY"
    "Kz3WfLKio8NeOFiwtuLCWuuFtdbrNYzfWMCCBQsWrI+xUo4OPixgwYIFC1YZrAFKxEUpKCP7KwAA"
    "AABJRU5ErkJggg=="
)


@bp.route("/test-local", methods=["POST"])
def test_local():
    """跑一次本地识别，验证组件确实能用（而不是只"看起来装了"）。"""
    st = extras.status()
    if not st.get("installed"):
        return jsonify({"success": False,
                        "message": "尚未安装本地识别组件"}), 400
    if not st.get("enabled"):
        return jsonify({"success": False,
                        "message": "组件已安装但未启用，请先启用"}), 400

    try:
        img = base64.b64decode(_TEST_IMG_B64)
    except Exception as e:              # noqa: BLE001
        return jsonify({"success": False, "message": f"测试图解码失败：{e}"}), 500

    import captcha
    try:
        code, used = captcha.solve_local(img), "local"
    except captcha.CaptchaError as e:
        return jsonify({"success": False, "message": str(e)}), 400
    except Exception as e:              # noqa: BLE001
        return jsonify({"success": False,
                        "message": f"本地识别执行异常：{e}"}), 500

    return jsonify({
        "success": True,
        "message": f"本地识别可用（返回 {code!r}，测试图识别结果不要求准确）",
        "code": code,
        "backend": used,
    })
