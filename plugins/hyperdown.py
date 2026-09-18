"""Hyperdown 网盘 (https://hyperdown.net) 每日签到插件

认证方式：账号(邮箱) + 密码登录，拿 access_token / refresh_token。
  - 登录 POST /auth/login（明文，不加密）
  - 查状态 GET /me/（返回 user.is_check_in）
  - 签到 POST /me/checkins（需 SealJSON 加密信封：X25519 ECDH + HKDF-SHA256
    + XChaCha20-Poly1305 + HMAC-SHA256，对端公钥已内嵌）

token 持久化：登录成功后把 access/refresh token 打包写进 result.cookie，
引擎会加密存档，下次运行作为 config["cookie"] 回传，直接复用，过期自动刷新/重登。

依赖：cryptography（镜像已含）+ pynacl（提供 XChaCha20-Poly1305 AEAD）。
  ⚠️ 需要重建 Docker 镜像使 pynacl 进入容器，否则插件 import 阶段即失败。
  算法对齐开源参考 jasper0507/hyperdown-checkin（SealJSON 2026-07-14 抓包）。
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Dict, Optional

import requests
from plugins.base import BasePlugin, FormField, CheckinResult

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

try:
    from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_encrypt
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Hyperdown 插件需要 PyNaCl：请重建镜像（pip install -r requirements.txt 会装 pynacl）"
    ) from exc

BASE_URL = "https://hyperdown.net"
DEFAULT_BASE = BASE_URL + "/api/v1"
USER_AGENT = "Go-http-client/1.1"
TIMEOUT = 30

# 客户端内嵌的 32 字节对端公钥（纯公钥，非私钥、非账号凭据）。
# 仅用于与服务器协商一次性会话密钥，每个请求用临时私钥，无法反推账号。
_PEER_PUB_HEX = "dd85f63f107a32ce3def4835fe56c27865a1557fedad19adbd72ff81ea2e1025"
PEER_PUBLIC_KEY = bytes.fromhex(os.environ.get("HYPERDOWN_SECURE_PEER_PUB") or _PEER_PUB_HEX)
SALT_PREFIX = b"hyperdown-secure-api:v1:"


# ===================== SealJSON 信封构造 =====================
# 算法对齐开源 secure_api.py（jasper0507/hyperdown-checkin, 抓包 2026-07-14）：
#   eph = X25519 临时密钥对
#   shared = eph.ECDH(PEER_PUBLIC_KEY)
#   key = HKDF-SHA256(shared, salt=SHA256(request_id:ts),
#                     info="hyperdown-secure-api:v1:"+METHOD+":"+path)
#   aad = METHOD\n+path\n+request_id\n+ts
#   sealed = XChaCha20-Poly1305.Seal(key, n24, body, aad)   # libsodium
#   sign = HMAC-SHA256(key, nul_join("v1",METHOD,path,request_id,ts,nonce,pub,ct,token))
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _normalize_path(path: str) -> str:
    path = (path or "").split("?", 1)[0]
    if not path:
        return "/"
    return path if path.startswith("/") else "/" + path


def _is_sensitive_path(method: str, path: str) -> bool:
    if method.upper() != "POST":
        return False
    return _normalize_path(path) in {
        "/api/v1/me/checkins",
        "/api/v1/redemptions/redeem",
        "/api/v1/shares/parse",
        "/api/v1/shares/downloads/resolve",
        "/api/v1/downloads/resolve",
        "/api/code/redeem",
    }


def _derive_key(ikm: bytes, method: str, path: str, ts: int, request_id: str) -> bytes:
    material = f"{request_id}:{ts}".encode()
    salt = hashlib.sha256(material).digest()
    info = SALT_PREFIX + f"{method.upper()}:{_normalize_path(path)}".encode()
    return HKDF(hashes.SHA256(), 32, salt, info).derive(ikm)


def _seal_json(method: str, path: str, body: bytes, access_token: str) -> dict:
    """构造 SealJSON 加密信封，返回可直接 json.dumps 的 dict。"""
    plaintext = body if body else b"{}"
    ts = int(time.time())
    request_id = secrets.token_hex(16)
    aead_nonce = secrets.token_bytes(24)

    eph = x25519.X25519PrivateKey.generate()
    eph_pub_hex = eph.public_key().public_bytes_raw().hex()

    # X25519 ECDH：shared = eph.private · PEER_PUBLIC_KEY
    peer_pub = x25519.X25519PublicKey.from_public_bytes(PEER_PUBLIC_KEY)
    shared = eph.exchange(peer_pub)

    key = _derive_key(shared, method, path, ts, request_id)
    aad = f"{method.upper()}\n{_normalize_path(path)}\n{request_id}\n{ts}".encode()
    # libsodium 签名：crypto_aead_xchacha20poly1305_ietf_encrypt(msg, aad, nonce, key)
    sealed = crypto_aead_xchacha20poly1305_ietf_encrypt(plaintext, aad, aead_nonce, key)

    nonce_field = _b64(aead_nonce)
    ct_field = _b64(sealed)

    sign_msg = b"\x00".join(
        p.encode()
        for p in [
            "v1",
            method.upper(),
            _normalize_path(path),
            request_id,
            str(ts),
            nonce_field,
            eph_pub_hex,
            ct_field,
            access_token or "",
        ]
    )
    sig = _b64(hmac.new(key, sign_msg, hashlib.sha256).digest())

    return {
        "v": "v1",
        "request_id": request_id,
        "ts": ts,
        "nonce": nonce_field,
        "pub": eph_pub_hex,
        "ciphertext": ct_field,
        "sign": sig,
    }


def _parse_stored_cookie(cookie: str):
    cookie = (cookie or "").strip()
    if not cookie:
        return "", ""
    if cookie.startswith("{"):
        try:
            d = json.loads(cookie)
            return d.get("access_token", ""), d.get("refresh_token", "")
        except (json.JSONDecodeError, TypeError):
            return "", ""
    return cookie, ""


def _pack_tokens(access_token: str, refresh_token: str) -> str:
    return json.dumps(
        {"access_token": access_token, "refresh_token": refresh_token},
        ensure_ascii=False, separators=(",", ":"),
    )


class APIError(Exception):
    def __init__(self, code: str, message: str, status: Optional[int] = None, raw=None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.raw = raw


class HyperdownPlugin(BasePlugin):
    name = "hyperdown"
    display_name = "Hyperdown 网盘签到"
    description = (
        "Hyperdown 网盘(hyperdown.net)每日签到领流量。认证用 账号(邮箱)+密码 登录；"
        "登录成功后 token 自动加密存档并复用，过期自动刷新/重登。"
        "注意：表单“用户名”填邮箱地址。"
    )
    plugin_type = "http"
    author = "checkin-system"
    version = "1.0"

    form_schema: list = []

    def __init__(self):
        self._base = DEFAULT_BASE

    # ============ 内部 HTTP 客户端 ============
    def _request(self, session, method, path, *, body=None, auth=True, secure=None, tokens=None):
        method = method.upper()
        url = self._base + path
        full_path = path if path.startswith("/api/") else "/api/v1" + path

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        access = (tokens or {}).get("access_token", "") if tokens else ""
        if auth and access:
            headers["Authorization"] = f"Bearer {access}"

        use_secure = _is_sensitive_path(method, full_path) if secure is None else secure

        if use_secure:
            envelope = _seal_json(
                method, full_path,
                json.dumps(body or {}, separators=(",", ":")).encode(), access,
            )
            headers["X-Hyperdown-Secure"] = "v1"
            raw_body = json.dumps(envelope, separators=(",", ":")).encode()
        elif body is not None:
            raw_body = json.dumps(body, separators=(",", ":")).encode()
        else:
            raw_body = None

        try:
            resp = session.request(method, url, data=raw_body, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise APIError("network_error", str(e), None)

        try:
            text = resp.text or ""
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            raise APIError("invalid_json", text[:200], resp.status_code)

        if isinstance(payload, dict) and payload.get("ok") is False:
            err = payload.get("error") or {}
            raise APIError(str(err.get("code") or "error"), str(err.get("message") or text), resp.status_code, payload)
        if resp.status_code >= 400:
            raise APIError("http_error", text[:200], resp.status_code, payload)

        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    # ============ 登录 / 刷新 ============
    def _login(self, session, email, password) -> dict:
        data = self._request(session, "POST", "/auth/login",
                             body={"email": email, "password": password}, auth=False, secure=False)
        return self._extract_tokens(data)

    def _refresh(self, session, refresh_token) -> dict:
        data = self._request(session, "POST", "/auth/refresh",
                             body={"refresh_token": refresh_token}, auth=False, secure=False)
        return self._extract_tokens(data)

    @staticmethod
    def _extract_tokens(data) -> dict:
        if not isinstance(data, dict):
            return {}
        for key in ("tokens", "token", "auth"):
            nested = data.get(key)
            if isinstance(nested, dict) and (nested.get("access_token") or nested.get("refresh_token")):
                return {
                    "access_token": str(nested.get("access_token") or ""),
                    "refresh_token": str(nested.get("refresh_token") or ""),
                }
        if data.get("access_token") or data.get("refresh_token"):
            return {
                "access_token": str(data.get("access_token") or ""),
                "refresh_token": str(data.get("refresh_token") or ""),
            }
        return {}

    # ============ 结果构造（统一设置 result.cookie 以便回写） ============
    def _ok(self, message: str, extra: dict, tokens: dict) -> CheckinResult:
        extra = dict(extra or {})
        extra["cookie"] = _pack_tokens(**tokens) if tokens else ""
        result = CheckinResult(True, message, extra)
        result.cookie = extra["cookie"]
        return result

    def _already(self, tokens: dict) -> CheckinResult:
        return self._ok("今日已签到（重复签到）", {"already_checkin": True}, tokens)

    # ============ 主入口 ============
    def checkin(self, config):
        params = config.get("params", {}) or {}
        cookie = config.get("cookie", "") or ""
        email = config.get("username", "") or params.get("email", "")
        password = config.get("password", "")
        site_url = (config.get("site_url", "") or params.get("base_url", "") or BASE_URL).rstrip("/")
        self._base = site_url
        if not self._base.endswith("/api/v1"):
            if "/api" in self._base:
                self._base = self._base.split("/api")[0] + "/api/v1"
            else:
                self._base = self._base.rstrip("/") + "/api/v1"

        session = requests.Session()

        access, refresh = _parse_stored_cookie(cookie)
        tokens = {"access_token": access, "refresh_token": refresh} if access else None

        if not tokens:
            if not (email and password):
                return CheckinResult(False, "请填写 账号(邮箱)+密码，或在 Cookie 字段提供有效 Token",
                                     {"error_type": "no_auth"})
            try:
                tokens = self._login(session, email, password)
            except APIError as e:
                return CheckinResult(False, f"登录失败: {e.message}", {"error_type": "login_failed"})
            if not tokens.get("access_token"):
                return CheckinResult(False, "登录成功但未返回 token", {"error_type": "login_failed"})

        def do_me():
            data = self._request(session, "GET", "/me/", auth=True, secure=False, tokens=tokens)
            # 服务端返回 {"user": {...}}，需解包一层；也兼容已解包的平铺结构
            if isinstance(data, dict) and isinstance(data.get("user"), dict):
                return data["user"]
            return data

        def ensure_tokens():
            nonlocal tokens
            if tokens.get("refresh_token"):
                try:
                    tokens = self._refresh(session, tokens["refresh_token"])
                    if tokens.get("access_token"):
                        return True
                except APIError:
                    pass
            if email and password:
                try:
                    tokens = self._login(session, email, password)
                    return bool(tokens.get("access_token"))
                except APIError:
                    return False
            return False

        try:
            user = do_me()
        except APIError as e:
            if e.status == 401 and ensure_tokens():
                try:
                    user = do_me()
                except APIError as e2:
                    return CheckinResult(False, f"查询状态失败: {e2.message}", {"error_type": "api_error"})
            else:
                return CheckinResult(False, f"查询状态失败: {e.message}", {"error_type": "api_error"})

        if isinstance(user, dict) and user.get("is_check_in"):
            return self._already(tokens)

        try:
            resp = self._request(session, "POST", "/me/checkins", body={}, auth=True, secure=True, tokens=tokens)
        except APIError as e:
            msg = e.message or ""
            if e.status == 401 and ensure_tokens():
                try:
                    resp = self._request(session, "POST", "/me/checkins", body={}, auth=True, secure=True, tokens=tokens)
                except APIError as e2:
                    return CheckinResult(False, f"签到失败(刷新后仍报错): {e2.message}", {"error_type": "api_error"})
            elif any(k in msg for k in ("已签到", "already", "repeat", "Already")) or e.code in ("already_check_in", "checked_in"):
                return self._already(tokens)
            else:
                return CheckinResult(False, f"签到失败: {e.message}", {"error_type": "api_error"})

        extra = {}
        if isinstance(resp, dict):
            for k in ("is_check_in", "today_traffic", "check_in_days", "total_traffic", "traffic"):
                if k in resp:
                    extra[k] = resp[k]
        msg = "签到成功"
        if isinstance(resp, dict):
            t = resp.get("today_traffic") or resp.get("traffic")
            if t:
                msg = f"签到成功，今日获得 {t}"
        return self._ok(msg, extra, tokens)
