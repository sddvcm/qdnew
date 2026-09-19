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

# 内置测试图：云码官网（www.jfbym.com）公开的示例验证码，与真实登录验证码
# 同风格（fuliba 用的 10110=通用数英1~4位 就是这类）。含 PNG 与 JPEG 两种
# 格式、含噪点背景，比自绘图更能代表真实识别场景。
#
# ⚠️ 这些 base64 由 packaging/fnos/fetch_test_captchas.py **程序化生成**，
# 且经过三重验证（PIL 完整解码 / PNG chunk CRC / base64 往返）。
# **绝不要手写或手工修改 base64** —— 上版本手写的假 PNG（魔数对、结构坏）
# 曾导致「测试本地识别」必报 cannot identify image file。
_TEST_IMAGES = [
    # 1.png —— 84×32 PNG，3136 bytes（来源：云码官网示例图）
    {"name": "1.png", "desc": "数英4位·白底（PNG）", "b64": (
    "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"
    "rkJggg=="
    )},
    # 2.png —— 84×32 PNG，3774 bytes（来源：云码官网示例图）
    {"name": "2.png", "desc": "数英4位·红字（PNG）", "b64": (
    "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"
    )},
    # 3.jpg —— 120×30 JPEG，3828 bytes（来源：云码官网示例图）
    {"name": "3.jpg", "desc": "数英4位·红字（JPEG）", "b64": (
    "/9j/4QAwRXhpZgAATU0AKgAAAAgAAQExAAIAAAAOAAAAGgAAAAB3d3cubWVpdHUuY29tAP/b"
    "AEMAAgEBAQEBAgEBAQICAgICBAMCAgICBQQEAwQGBQYGBgUGBgYHCQgGBwkHBgYICwgJCgoK"
    "CgoGCAsMCwoMCQoKCv/bAEMBAgICAgICBQMDBQoHBgcKCgoKCgoKCgoKCgoKCgoKCgoKCgoK"
    "CgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCv/AABEIAB4AeAMBEQACEQEDEQH/xAAfAAAB"
    "BQEBAQEBAQAAAAAAAAAAAQIDBAUGBwgJCgv/xAC1EAACAQMDAgQDBQUEBAAAAX0BAgMABBEF"
    "EiExQQYTUWEHInEUMoGRoQgjQrHBFVLR8CQzYnKCCQoWFxgZGiUmJygpKjQ1Njc4OTpDREVG"
    "R0hJSlNUVVZXWFlaY2RlZmdoaWpzdHV2d3h5eoOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmq"
    "srO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4eLj5OXm5+jp6vHy8/T19vf4+fr/xAAfAQAD"
    "AQEBAQEBAQEBAAAAAAAAAQIDBAUGBwgJCgv/xAC1EQACAQIEBAMEBwUEBAABAncAAQIDEQQF"
    "ITEGEkFRB2FxEyIygQgUQpGhscEJIzNS8BVictEKFiQ04SXxFxgZGiYnKCkqNTY3ODk6Q0RF"
    "RkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqCg4SFhoeIiYqSk5SVlpeYmZqio6Slpqeo"
    "qaqys7S1tre4ubrCw8TFxsfIycrS09TV1tfY2dri4+Tl5ufo6ery8/T19vf4+fr/2gAMAwEA"
    "AhEDEQA/APv/AP4J6ftAfG/4kfs06ZYfFy81/T/FHhiY6Prh1K7k33clvz5v7ySWTzcYil83"
    "96bqK5i8qPyq+UyytmEcLy1H+8j0Z+x+K3C+R5fxPfBPmwlWUeSUVpZ2vblSXXTfSzu7m7+y"
    "B8ePjH8cPhE/xu8a+LNWt7fxXq93d+GNKuLGSyfStIM3lWcLnzJfOklji+0+b/08/uv3flV6"
    "2H+tYiLzDrHp5PTY8DxDyvJeG8THKsNTXsoxjzVIu6cmryS0v7rfL8tbO56v/wAJn4v6f8Jf"
    "qP8A4MZa7KVRtXZ8RXo4KlCChLnSPMv2wfHn7TOk/s4+K9R/Zz8e6hZeMrWw+06LhBcyy+XL"
    "5kscUflSebLJFFJFFH5X+skrxszpyqRk79GfdeGsOG8dxbS/1hjfDXSttvpfdWs7NvXRPS58"
    "x/8ABP3w3+zT4r+Bui/txfH/AMetr/jPT5Tbar4s+JXjQanJ4fmtrnEUaPc/u7I7xFcxk/vY"
    "/tv+sk8yvGyieS4vL/rlWXvx01d7Wf4a6r1P2fxRnxHl3EVbhTKqChgl70FCCUXzRvdpXcm4"
    "2i29+XRKx9waL8SdW8R6JZeIPDXxCnvdOvYYrmyurPV/MiuYpf8AVSxSf8tYq+toYzDY7L+a"
    "ex/OGKyythueOKp8sk/et17aLYs/8Jp4x/6HDUf/AAZS1qrW0PL5Yh/wmnjH/ocNR/8ABlLT"
    "16Byrsc98Sf2gbP4PeEtQ+IPxI+KF5pOj6XaS3F5qN1qEvlxRfuv/Iv72Pyov+WtZyp1qDlU"
    "nWV10sj18oyPHcUY2nleW4dyryej7Jatv5HyJ+xR+3f+238bP27PFPwu+OGtX/h3SoPCDapp"
    "/gRliEulCSSxNrvkx5vm/Zrgeb5v/LWXPlRf6qP4XKc5zrNOIZ0Z1bRW2np/mf0h4geG3h5l"
    "Hhdgc4wq9pXbkpVNY3a5k7LykrLutbvr9uf8Jr4x/wChw1H/AMGUtffarc/lhqN9ERzeM/Gw"
    "ixb+L9R8z0l1GWi1w5Uzyf8AaW/bD/4VV4afQbX4/wCiaH4nulXZpviTxhFYXUdschrhY5pw"
    "Aw4xk4784xXq8O18ioZsoZtU5aUU36tapOybs+tl5aXO3G8I8a5vlUq2T4RzlFpXXVPdrpde"
    "bM7wvcfHqx/Z2u7T4afErVNZvtW0tL3StRTxV5kV1NMV2PBcGRSbc5PKsD8rMCGYV6fEGdZR"
    "xNVeKwajFR0SjHl93W3MrWvp+NtkfF5Zk/EuVY6WDxsZQpwvzc+r5tNU272ld2XaN3ZuxgQ+"
    "Ev24vEcmlWNh451sNEt7bSWt54tuUmllNzE9uVdLkhmZQy4dgIzvXftKmvmI2PopKyPmX/gq"
    "h8MfFX7LX7X+ofFHwJ4ql8O/D/4z30umfEZbaxlnsY8Sf6UJLaOKPPmW3mS/u5ftMspvvLlj"
    "r84zXD53l2YLME/cm7P8tv6e5/bHg7mGXcccD1srxNFVsVgIuVH3lGTvq1dt3s7bpxV4pp9f"
    "XvCFl/wUl/Zr8O+HfCfhf4ceFfiRoFnYRQSeHtP1eCyfS4vs3lxWtleyyxGWOP7PFL/pNt5n"
    "+kxRebc/vZY/WwkM4w6v9mX9f1pc/Oq1Xwr4hrYmnmVZ4OrD7TXOptu+sfiTadtHy6X93r9Q"
    "eEZtY8SeC9L1Xx74Oh0nVbrTYrjUtEkvIrn7BdeV+9i83/VS+V/qvNr6aEmfguJy3JsJxDUj"
    "lk+fDrrqr+dn3F/syw/tiO3hEkcdrD5nkxTfuvN/z5tbOhGrBpnm0Ma6mMdXBK6g0+2zPzf8"
    "J/st/C34e/8ABVW6/Z2+N3hEav4O1573xH4A0lf3WnWdxII7oD7PFL5flRR2dxbeX/y1+zR/"
    "uvKNfmtLKqH9tPDR+DV26X3enyP7ox3Gub5x4MUcfl0UsVHlp1pNKU3Fe7Fc7V9XJO99LuzT"
    "bZ9ieFv25f2R38b6d+z/APBzX38R3dqY7CCw8DaBLeWOmWwi/dS+ZaxfZorX/j2tf3X+qlli"
    "MvlfvfK+2w+e5NPFLKaa1Xa/r00P5hzLw546wOXvO8auTnvy8zV3Z2a5W+bu721WqbVj2i8v"
    "TDLHBB5f/PSbzv8AllFXsWS0R+au6bvuSTQedF5H2iSL/rjRdrUVm9FufK/7dPgv9or4pfFf"
    "wh4a0n9kfTviV4K8O3drrs1pL4ptdN+1X8UV/F5UvmyjzI/3tlL/AKv/AJZSxS+b5tfJ5lhM"
    "8x+JdanS91eZ++eGOYcKZLk1d5hmv1LEyVm+RzbTts1a2l1o+t+h4Z+z58R/j3d/8FdPFviX"
    "Uv2afsOuazodnY+KPDv/AAl1tL/wjthjTPNvvtH+rucRRxy+XH+8/e8V81kmPxlPPZUlQ16u"
    "/pqftPG2Q8LVPAXC8+YurQjKbhL2cl7Rtz0/u2fV6aeaP0fr9QV2tT+G5KMW1HboFO9hxpyq"
    "yUI7vQ+Kfid8S/2fv2l/2jvGfwi+H37FzfFXWNHs7608Z+J9dvUtY7K5itfKtrGxuLn95aiS"
    "5iki/d/ZvLl825j8397LXwuLx0q+cV6beiXVLt5/0tz+nMgyHiDgng/C4zMcy+rKpKLopJyc"
    "1zLmbUdGkmrX5lK/K0i3/wAENdY1XUf2Rdb0/VNWubm2sPHN7b6dDLL5kVrH9ltpPKi/55Re"
    "bJJL/wBtZa6uEsVKGS1ZS3v+pn9JLCUqfFtKk7SVSEXNpKOvLo9O9kfb/gn/AJHDSP8AsJWv"
    "/o2vr6bukz+YaiSukefftk/s2fDz9qvwt4g+EPxLjuIrG51v7TBe6eIjc20scn+tjklik8qX"
    "/WRf9cpZa5MXhvr2j1R9zwVxdiOCs0ePw8rO3y2tqtLm14I8IeHPh74O0rwH4Vs/I0zRtNjs"
    "9OtTLLJ5dtHF5ccX73/plRh8PLCPU+bzLNZ8VcVyzGrondvS127t6LTc067NGcD0k7FPR/30"
    "Ul7j/j6mlk/9pf8AtKghRSVlpc85/aL/AGOfgN+1df8Ahy9+N3hW61FfDMty9rFBqMttFMJI"
    "vLkhl8r/AJZfu45f+2cX/TWKXxsXk0Me9D9J4M8Q+JuB8HLD5TO3Mknez2ba+K/d/f6W1PBv"
    "ws+F3wL8PWfw9+EfgXTfDek23lSTR6faZN1L5XlRf9fVz+6i/ey/88q7csyejl2stz5biHif"
    "i7PMY8RnFVtPz/DTp5bHVaPo8/lfb9cnlluP9Z5Pnf6qu17ngbmpUvYA8mCf/tlWuGb+pSiF"
    "agq1NRjrY8S8EfscDwf+2/4v/bLPxE+0jxV4ej0xfD/9k+X9lMcNjmX7T5v73/jy/wCeX/LW"
    "vkMJlco4+VTufrOc+JVTFeHmD4ejH+E5P5ybvdW83bU9tr6tKyPyvmvqFJq6sXGq6UlNbrX7"
    "j5q1r9gWfTPiV4w8W/s6/HvXPh/H461WO88ci20yK+uLn/W+ZFZXMuPsEkn2mSXzcy+VLLH5"
    "XleV5VfMYzh+E6rn337n7Pl/izHOcpw+EzjB/WFQT9jeVlDVPVK3MtErXV1dN63Oi/Yd/Yvu"
    "P2LNH8T+EtO+LU/iDw/rWqLf6XpdxosdrJpuMxSmSSIn7VLLHHb/APPP/Vf6r97XdlmV08NH"
    "l6HmeJfihHxRxtGvQwaw86EVGo07qdlZWTb5Utereurdj6H8E/8AI4aR/wBhK1/9G17UND8f"
    "qdTsPEfwU8VatrF/q8N1p2y5unljWW4fK5kzz+79K0h+6loPDyjCPs56opx/APxhKm9dR03H"
    "vcSf/G6Kl6r1Kr1KdOPLSVhf+FAeMf8AoJab/wCBEn/xus9iedDIv2ffGUEX2e3u9JSP0E8n"
    "/wAaoewe0QTfAfxdDjfqOnc+k8n/AMbpYeE4PRmFWj9bqc/M16EGnfs/+NvMluX1HSi8k3J8"
    "6T/V+n+r60q7qTerOiq24KEm2Wv+FAeMf+glpv8A4ESf/G6oXOgH7P8A4xJx/aWm/wDgRJ/8"
    "bo3E5qwh+APjAMVOo6bkf9PEn/xunb2eiCjWdGHN1Gj4D+Liyr/aOnfPnH7+T/43Wvs1Ti5I"
    "0p1KMcRWqNX2sPP7P/jEHH9pab/4ESf/ABusTP2lw/4UB4x/6CWm/wDgRJ/8boDnK2m/s/eN"
    "rOy8yXUtKZ5cSykTycyHv/q+lRTUqz3DGYtRpWS9fMnPwF8Xq4Q6jp2Scf8AHxJ/8bpVKEoK"
    "6Zniq8Y1aTwy5L7l7w78E/FWja7YarPdadttbuOWRYrh8tiTPH7v0rSHQqUro//Z"
    )},
    # 4.jpg —— 135×43 JPEG，4810 bytes（来源：云码官网示例图）
    {"name": "4.jpg", "desc": "数英4位·噪点背景（JPEG）", "b64": (
    "/9j/4QAwRXhpZgAATU0AKgAAAAgAAQExAAIAAAAOAAAAGgAAAAB3d3cubWVpdHUuY29tAP/b"
    "AEMAAgEBAQEBAgEBAQICAgICBAMCAgICBQQEAwQGBQYGBgUGBgYHCQgGBwkHBgYICwgJCgoK"
    "CgoGCAsMCwoMCQoKCv/bAEMBAgICAgICBQMDBQoHBgcKCgoKCgoKCgoKCgoKCgoKCgoKCgoK"
    "CgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCv/AABEIACsAhwMBEQACEQEDEQH/xAAfAAAB"
    "BQEBAQEBAQAAAAAAAAAAAQIDBAUGBwgJCgv/xAC1EAACAQMDAgQDBQUEBAAAAX0BAgMABBEF"
    "EiExQQYTUWEHInEUMoGRoQgjQrHBFVLR8CQzYnKCCQoWFxgZGiUmJygpKjQ1Njc4OTpDREVG"
    "R0hJSlNUVVZXWFlaY2RlZmdoaWpzdHV2d3h5eoOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmq"
    "srO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4eLj5OXm5+jp6vHy8/T19vf4+fr/xAAfAQAD"
    "AQEBAQEBAQEBAAAAAAAAAQIDBAUGBwgJCgv/xAC1EQACAQIEBAMEBwUEBAABAncAAQIDEQQF"
    "ITEGEkFRB2FxEyIygQgUQpGhscEJIzNS8BVictEKFiQ04SXxFxgZGiYnKCkqNTY3ODk6Q0RF"
    "RkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqCg4SFhoeIiYqSk5SVlpeYmZqio6Slpqeo"
    "qaqys7S1tre4ubrCw8TFxsfIycrS09TV1tfY2dri4+Tl5ufo6ery8/T19vf4+fr/2gAMAwEA"
    "AhEDEQA/AP04r9EVatbYyp6M8f8AG3xJ/aM1jXtU0P4c/DK2sdPsJpY/7c1b/lr5X/LWL/Mt"
    "efiq1ZdD0qVRIp/sc/E7x/8AE7/hJL7xz4jkvfsv2Xyf3MUflf63/nlSy/E2kGOjeJ7ZXr1c"
    "SmjzqETz/wAefH7w54V8LXHirw5pUmt2dr/zEIpvKsf/AAJ/5a/88v3Xm/vf9b5VcmLxdXCx"
    "0NcPRtI8fs/En7UPx+upL7w59psrOKGKSGaG8l02Lyv+mXlS/vf9V/y1ll/7ZebXjOdfGu9j"
    "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"
    "CgD/2Q=="
    )},
]


@bp.route("/test-local", methods=["POST"])
def test_local():
    """用内置的真实示例验证码跑本地识别，验证组件确实能用。

    测试图是云码官网的公开示例（4 张，PNG/JPEG 都有），与 fuliba 实际遇到的
    登录验证码同类型（通用数英1~4位）。逐张识别并返回结果 —— 用户可以对照
    图片自己判断识别准不准（1.png=5289 / 2.png=1858 / 3.jpg=KE4a / 4.jpg=1256）。
    """
    st = extras.status()
    if not st.get("installed"):
        return jsonify({"success": False,
                        "message": "尚未安装本地识别组件"}), 400
    if not st.get("enabled"):
        return jsonify({"success": False,
                        "message": "组件已安装但未启用，请先启用"}), 400

    import captcha
    results = []
    ok_cnt = 0
    first_err = ""
    for it in _TEST_IMAGES:
        name, desc = it["name"], it["desc"]
        try:
            img = base64.b64decode(it["b64"])
        except Exception as e:      # noqa: BLE001
            results.append({"name": name, "desc": desc, "ok": False,
                            "error": f"内置图解码失败：{e}"})
            continue
        try:
            code = captcha.solve_local(img)
            results.append({"name": name, "desc": desc, "ok": True,
                            "code": code})
            ok_cnt += 1
        except captcha.CaptchaError as e:
            results.append({"name": name, "desc": desc, "ok": False,
                            "error": str(e)})
            if not first_err:
                first_err = str(e)
        except Exception as e:      # noqa: BLE001
            results.append({"name": name, "desc": desc, "ok": False,
                            "error": f"执行异常：{e}"})
            if not first_err:
                first_err = f"执行异常：{e}"

    total = len(_TEST_IMAGES)
    if ok_cnt == 0:
        return jsonify({
            "success": False,
            "message": first_err or "本地识别全部失败",
            "results": results,
        }), 400

    return jsonify({
        "success": True,
        "message": f"本地识别可用：{ok_cnt}/{total} 张示例图识别成功，"
                   f"结果见 results（可对照图片判断准确率）",
        "results": results,
    })
