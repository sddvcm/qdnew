"""访问鉴权路由 —— 登录页 + 密码管理 API"""
from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)

from app.database import get_db
from app import auth

bp = Blueprint("auth", __name__)


# ==================== 页面 ====================

@bp.route("/login", methods=["GET"])
def login_page():
    """登录页。已登录就直接放行，不要让人再登一次。"""
    db = get_db()
    try:
        enabled = auth.is_enabled(db)
    finally:
        db.close()
    if not enabled:
        return redirect("/")
    if session.get("auth_ok") and auth.valid_session(session.get("auth_token")):
        return redirect(_safe_next(request.args.get("next")) or "/")
    return render_template("login.html",
                           next_url=_safe_next(request.args.get("next")) or "",
                           is_default=auth.IS_DEFAULT_PASSWORD)


def _safe_next(nxt):
    """只允许站内相对跳转，挡掉 `//evil.com` 这类开放重定向。"""
    if not nxt or not nxt.startswith("/") or nxt.startswith("//"):
        return ""
    return nxt


# ==================== API ====================

@bp.route("/api/auth/status", methods=["GET"])
def status():
    out = {"enabled": False, "authed": True, "is_default_password": False}
    db = get_db()
    try:
        out["enabled"] = auth.is_enabled(db)
        if not out["enabled"]:
            return jsonify(out)
        out["is_default_password"] = auth.current_password_is_default(db)
        out["authed"] = bool(session.get("auth_ok")
                             and auth.valid_session(session.get("auth_token")))
    finally:
        db.close()
    return jsonify(out)


@bp.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    password = data.get("password") or ""

    db = get_db()
    try:
        if not auth.is_enabled(db):
            return jsonify({"success": True, "message": "未开启密码保护"})
        stored = None
        row = db.execute("SELECT value FROM system_config WHERE key=?",
                         (auth.KEY_HASH,)).fetchone()
        stored = row["value"] if row else ""
        row = db.execute("SELECT value FROM system_config WHERE key=?",
                         (auth.KEY_SALT,)).fetchone()
        salt = row["value"] if row else ""

        # 没设过密码 → 用出厂默认值登录（首次开箱即用的前提）
        if not stored or not salt:
            ok = (password == auth.DEFAULT_PASSWORD)
        else:
            ok = auth.verify_password(password, stored, salt)

        if not ok:
            return jsonify({"success": False, "message": "密码错误"}), 401

        token = auth.create_session(db)
        db.commit()
        session["auth_ok"] = True
        session["auth_token"] = token
        session.permanent = True
        return jsonify({"success": True, "is_default_password":
                        auth.current_password_is_default(db)})
    finally:
        db.close()


@bp.route("/api/auth/logout", methods=["POST"])
def logout():
    auth.drop_session(session.get("auth_token"))
    session.clear()
    return jsonify({"success": True})


@bp.route("/api/auth/password", methods=["POST"])
def change_password():
    """修改访问密码。

    ⚠️ 必须校验**旧密码**：否则会话被劫持后攻击者可以一键改密码把主人踢出去。
    """
    data = request.get_json(silent=True) or {}
    old = data.get("old_password") or ""
    new = data.get("new_password") or ""
    confirm = data.get("confirm_password")

    if len(new) < 4:
        return jsonify({"success": False, "message": "新密码至少 4 位"}), 400
    if confirm is not None and confirm != new:
        return jsonify({"success": False, "message": "两次输入的新密码不一致"}), 400
    if new == auth.DEFAULT_PASSWORD:
        return jsonify({"success": False,
                        "message": "新密码不能与默认密码相同"}), 400

    db = get_db()
    try:
        row = db.execute("SELECT value FROM system_config WHERE key=?",
                         (auth.KEY_HASH,)).fetchone()
        stored = row["value"] if row else ""
        row = db.execute("SELECT value FROM system_config WHERE key=?",
                         (auth.KEY_SALT,)).fetchone()
        salt = row["value"] if row else ""

        if not stored or not salt:
            ok = (old == auth.DEFAULT_PASSWORD)
        else:
            ok = auth.verify_password(old, stored, salt)
        if not ok:
            return jsonify({"success": False, "message": "当前密码错误"}), 401

        auth.set_password(db, new)
        # set_password 清空了所有会话 —— 给自己重新发一个，别把用户踢下线
        token = auth.create_session(db)
        db.commit()
        session["auth_ok"] = True
        session["auth_token"] = token
        return jsonify({"success": True, "message": "密码已修改"})
    finally:
        db.close()


@bp.route("/api/auth/toggle", methods=["POST"])
def toggle():
    """开关密码保护。

    ⚠️ 关闭前必须验密码：这是把整站敞开的高危操作，不能让一个
       被劫持的会话直接关掉防护。
    """
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))
    password = data.get("password") or ""

    db = get_db()
    try:
        row = db.execute("SELECT value FROM system_config WHERE key=?",
                         (auth.KEY_HASH,)).fetchone()
        stored = row["value"] if row else ""
        row = db.execute("SELECT value FROM system_config WHERE key=?",
                         (auth.KEY_SALT,)).fetchone()
        salt = row["value"] if row else ""
        ok = ((password == auth.DEFAULT_PASSWORD) if not stored or not salt
              else auth.verify_password(password, stored, salt))
        if not ok:
            return jsonify({"success": False, "message": "密码错误"}), 401

        auth.set_enabled(db, enabled)
        db.commit()
        if not enabled:
            session.clear()
        return jsonify({"success": True,
                        "enabled": enabled,
                        "message": "已关闭密码保护（任何人可访问，请确认内网环境）"
                                   if not enabled else "已开启密码保护"})
    finally:
        db.close()
