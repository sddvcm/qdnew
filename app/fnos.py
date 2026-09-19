"""飞牛 fnOS 平台适配层

集中处理「本应用跑在 fnOS 上还是普通环境」的差异，向上层提供干净的接口：

- 数据目录落点与来源（共享目录 / 私有目录）
- 共享目录（data-share）的可用性自检
- 通过官方开放 API 读取系统信息（语言、版本）

设计原则：**所有探测都不抛异常**。这个模块被诊断页调用，
它自己出错就没法诊断了 —— 一律降级为"未知/不可用"并附原因。

关于官方开放 API（https://developer.fnnas.com/api/calling/）：
    后端 API 走 Unix socket，不能走网络：
        POST http://localhost/api/v1/trimapp
        socket: /var/run/trim_open_gateway_apiscope.socket
        header: Authorization: Bearer <TRIM_API_TOKEN>
    token 由系统在调用应用脚本时注入到环境变量，**不持久化、不下发前端**。
    注意 socket 在容器/普通环境里不存在，调用前必须先判断。
"""
import json
import os
import socket
import time

# ---- 常量：官方约定的路径与标识 ----
APPS_DIR = "/var/apps"                      # 装好后的应用目录根
OPENAPI_SOCKET = "/var/run/trim_open_gateway_apiscope.socket"
OPENAPI_PATH = "/api/v1/trimapp"
APP_NAME_DEFAULT = "checkin-system"


def app_name() -> str:
    """当前应用名（fnOS 会通过 TRIM_APPNAME 注入；否则用默认值）"""
    return (os.environ.get("TRIM_APPNAME") or APP_NAME_DEFAULT).strip() or APP_NAME_DEFAULT


# ============================================================
# 数据目录
# ============================================================
def data_dir() -> str:
    """当前数据目录（数据库所在处）。

    优先用 CHECKIN_DATA_DIR（cmd/main 注入；Docker/本地开发也一样），
    退回 <项目根>/data。与 app/database.py 的判定保持一致。
    """
    d = os.environ.get("CHECKIN_DATA_DIR")
    if d:
        return d
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def data_dir_source() -> str:
    """数据目录的来源说明（cmd/main 注入 CHECKIN_DATA_DIR_SRC）"""
    return os.environ.get("CHECKIN_DATA_DIR_SRC", "").strip()


def is_fnpack() -> bool:
    """是否运行在飞牛 fpk 环境里。

    判据：数据目录或应用目录落在 fnOS 的目录规范下。
    普通 Docker/本地开发时这些都不存在。
    """
    markers = ("/@appdata/", "/@appshare/", "/@appcenter/", "/@appconf/", "/@apphome/")
    for p in (data_dir(), os.environ.get("TRIM_APPDEST", "")):
        if p and any(m in p for m in markers):
            return True
    return os.path.isdir(APPS_DIR)


def share_links() -> list:
    """官方文档给出的共享目录软链候选路径。

    文档原文：「也可以通过 /var/apps/myapp/share/ 下的软链访问对应目录」。
    实测两种拼写（share / shares）在不同的包结构下都出现过，所以都给出，
    由调用方判断哪些真实存在。

    ⚠️ 用字符串拼接而**不是 os.path.join**：/var/apps 是 fnOS 专有路径，
       永远用 POSIX 分隔符。os.path.join 在 Windows 上会拼出反斜杠，
       导致本地测试与展示都出现 `\\` 的怪路径（虽然线上是 Linux 不受影响）。
    """
    n = app_name()
    return [
        f"{APPS_DIR}/{n}/shares/{n}",
        f"{APPS_DIR}/{n}/share/{n}",
    ]


def share_root() -> str:
    """共享目录（data-share）的真实路径；不可用则返回空串。

    取径优先级与 cmd/common.sh 的 ensure_data_dir 一致：
        软链 → TRIM_DATA_SHARE_PATHS → 空（调用方回退私有目录）
    """
    for link in share_links():
        if os.path.isdir(link):
            real = os.path.realpath(link)
            if real:
                return real
    paths = os.environ.get("TRIM_DATA_SHARE_PATHS", "").strip()
    if paths:
        return paths.split(":")[0].strip()
    return ""


def writable(path: str) -> bool:
    """真的能写吗？——光看目录存在不够（可能只读挂载或 ACL 未授权）。

    与 cmd 侧探测同一套判据：实际写一个探针文件。
    """
    if not path or not os.path.isdir(path):
        return False
    probe = os.path.join(path, f".write_probe_{os.getpid()}")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        return True
    except OSError:
        return False
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


def data_health() -> dict:
    """数据目录体检 —— 给诊断页用。

    返回：实际落点、来源、是否共享目录、可写性、空间占用等。
    """
    d = data_dir()
    root = share_root()
    used_share = bool(root) and os.path.realpath(d).startswith(os.path.realpath(root))
    info = {
        "data_dir": d,
        "source": data_dir_source() or ("共享目录" if used_share else "私有目录"),
        "on_share": used_share,
        "writable": writable(d),
        "is_fnpack": is_fnpack(),
        "share_root": root,
        "share_links": [{"path": p, "exists": os.path.isdir(p)} for p in share_links()],
    }
    try:
        st = os.statvfs(d)
        info["free_mb"] = round(st.f_bavail * st.f_frsize / 1024 / 1024, 1)
        info["total_mb"] = round(st.f_blocks * st.f_frsize / 1024 / 1024, 1)
    except (OSError, AttributeError):
        pass
    return info


# ============================================================
# 官方开放 API（Unix Socket）
# ============================================================
def openapi_available() -> bool:
    """官方开放 API 是否可用（socket 存在 + 有 token）"""
    return os.path.exists(OPENAPI_SOCKET) and bool(os.environ.get("TRIM_API_TOKEN"))


def _openapi_call(req: str, data: dict = None, timeout: float = 5.0) -> dict:
    """调用官方后端 API。

    ⚠️ 必须走 Unix socket —— 官方明确「只能由应用服务端通过 Unix Socket 调用，
    不要在前端浏览器中直接调用，也不要把 token 暴露给前端」。
    token 每次都从环境变量现读，**不缓存、不落盘**。
    """
    import http.client

    token = os.environ.get("TRIM_API_TOKEN", "")
    if not token:
        raise RuntimeError("缺少 TRIM_API_TOKEN（非 fnOS 环境或未由系统启动）")

    class _UnixHTTPConnection(http.client.HTTPConnection):
        def __init__(self, sock_path, **kw):
            super().__init__("localhost", **kw)
            self._sock_path = sock_path

        def connect(self):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(self._sock_path)
            self.sock = sock

    body = json.dumps({
        "reqId": f"{os.getpid()}-{int(time.time() * 1000) % 1000000}",
        "req": req,
        "appName": app_name(),
        "data": data or {},
    }, ensure_ascii=False)

    conn = _UnixHTTPConnection(OPENAPI_SOCKET, timeout=timeout)
    try:
        conn.request("POST", OPENAPI_PATH, body=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        })
        resp = conn.getresponse()
        payload = resp.read().decode("utf-8", "replace")
    finally:
        conn.close()

    obj = json.loads(payload)
    if obj.get("code") != 0:
        raise RuntimeError(obj.get("msg") or f"开放 API 返回 code={obj.get('code')}")
    return obj.get("data") or {}


def platform_config() -> dict:
    """读取 fnOS 系统语言与版本（官方 trim.system.getPlatformConfig）。

    失败不抛异常，返回 {"available": False, "error": "..."}，
    让诊断页如实展示"读不到 + 为什么"。
    """
    if not openapi_available():
        return {
            "available": False,
            "error": ("不在飞牛环境中（socket 不存在）"
                      if not os.path.exists(OPENAPI_SOCKET)
                      else "缺少 TRIM_API_TOKEN"),
        }
    try:
        d = _openapi_call("trim.system.getPlatformConfig", {})
        return {
            "available": True,
            "system_language": d.get("systemLanguage", ""),
            "system_version": d.get("systemVersion", ""),
        }
    except Exception as e:                      # noqa: BLE001
        return {"available": False, "error": f"{type(e).__name__}: {e}"}
