"""验证码识别器 —— 本地 ddddocr 与云码(jfbym)云端 OCR 的统一入口。

为什么要有这一层
----------------
插件里原本硬编码调用 ddddocr。引入云码后需要在两者间切换，还要支持
「本地识别不准时自动改用云端」。把选择逻辑集中在这里，插件只调 `solve()`，
以后再加别的 OCR 服务也不用改插件。

两种后端
--------
1. **local（ddddocr）**：本地跑，免费、不联网、无额度限制。缺点是识别率一般
   （尤其扭曲/粘连字符），且首次加载 onnxruntime 有几百 MB 内存开销。
   → **懒加载**，只有真要用到才 import，避免 Cookie 直连模式白吃内存。
2. **cloud（云码 / jfbym）**：调 `api.jfbym.com`，按次计费（约 3 积分/次），
   识别率明显高于本地模型，尤其适合 Discuz 那种扭曲字母数字。
   → 需要用户在 https://www.jfbym.com 注册并在用户中心拿 Token。

⚠️ 云码 API 规格（2026-09 核对官方文档 zhuce.jfbym.com/test/482.html）
-------------------------------------------------------------------
    POST http://api.jfbym.com/api/YmServer/customApi
    Content-Type: application/json
    body = {"token": "...", "type": "10110", "image": "<base64，不含 data:image/png;base64, 前缀>"}
    返回 = {"code": 10000, "msg": "...", "data": {"code": 0, "data": "识别结果", "time": "0.5"}}

    成功是 **外层 code == 10000**（整数，不是字符串），识别结果在 **data.data**。
    ⚠️ 外层 code 与 data.code 是两个不同的东西，别混用。常见错误码：
        10001 参数错误 / 10002 余额不足 / 10003 token 无效或未授权 /
        10004 验证类型不支持 / 10005 网络拥塞 / 10007 服务繁忙 / 10008 网络错误
    这些错误码要翻译成人话，否则用户只看到 "10003" 完全不知道是 Token 错了。

⚠️ `type` 是**打码类型 ID**，不同验证码形态对应不同值（10110=通用数英≤5位、
30102=数英计算、411115=双圈旋转 ……）。本项目场景是 Discuz 登录验证码，
默认 10110；把 type 做成可配置，将来换站点不用改代码。
"""
import base64
import time
from typing import Optional, Tuple

import requests

# ---------- 云码常量 ----------
JFBYM_API = "http://api.jfbym.com/api/YmServer/customApi"
JFBYM_DEFAULT_TYPE = "10110"          # 通用数英（≤5位）
JFBYM_TIMEOUT = 15

# 云码错误码 → 人话。**必须翻译**，否则用户看到纯数字无法自助排查。
JFBYM_ERRORS = {
    10001: "参数错误（可能是图片为空或 type 不对）",
    10002: "云码余额不足，请去 www.jfbym.com 充值",
    10003: "云码 Token 无效或未授权，请检查用户中心的 Token",
    10004: "该验证码类型(type)云码不支持，检查 type 配置",
    10005: "云码服务网络拥塞，稍后重试",
    10006: "数据包过载",
    10007: "云码识别失败（模型无法处理此图片），可换一张重试",
    10008: "云码网络错误，请稍后重试",
    10009: "云码结果准备中，请稍后重试",
    10010: "请求已结束",
}


class CaptchaError(Exception):
    """验证码识别失败（带人可读原因）"""


# ============================ 本地 ddddocr ============================

_ocr_instance = None


def _get_local_ocr():
    """懒加载 ddddocr 单例。

    ⚠️ 不要改成模块顶层 `import ddddocr`：它会拉起 onnxruntime，加载几百 MB
    模型，而绝大多数任务用的是 Cookie 直连、根本不需要验证码识别。
    这里做单例缓存，是为了避免每次识别都重新初始化模型（很慢）。
    """
    global _ocr_instance
    if _ocr_instance is None:
        try:
            import ddddocr
        except ImportError as exc:
            raise CaptchaError(
                "未安装 ddddocr，无法本地识别验证码。"
                "可在任务里改用云码（填云码 Token），或重建镜像安装 ddddocr"
            ) from exc
        _ocr_instance = ddddocr.DdddOcr(show_ad=False)
    return _ocr_instance


def solve_local(image_bytes: bytes) -> str:
    """用本地 ddddocr 识别"""
    if not image_bytes:
        raise CaptchaError("验证码图片为空")
    ocr = _get_local_ocr()
    try:
        code = (ocr.classification(image_bytes) or "").strip()
    except Exception as exc:
        raise CaptchaError(f"本地识别异常：{exc}") from exc
    if not code:
        raise CaptchaError("本地识别结果为空（图片可能不是验证码）")
    return code


# ============================ 云码 jfbym ============================

def solve_cloud(image_bytes: bytes, token: str, type_id: str = JFBYM_DEFAULT_TYPE,
                extra: str = "") -> str:
    """用云码识别。

    Args:
        image_bytes: 验证码图片原始字节
        token: 云码用户中心 Token
        type_id: 打码类型 ID，默认 10110（通用数英≤5位）
        extra: 部分类型需要的附加参数（如颜色提示）

    Raises:
        CaptchaError: 任何失败（含 token 无效、余额不足等，消息已翻译成人话）
    """
    if not token or not str(token).strip():
        raise CaptchaError("未配置云码 Token（去 www.jfbym.com 用户中心获取）")
    if not image_bytes:
        raise CaptchaError("验证码图片为空")

    # ⚠️ 必须剥掉 data URI 前缀（有些抓取路径会带上），否则云码当垃圾数据
    payload = {
        "token": str(token).strip(),
        "type": str(type_id or JFBYM_DEFAULT_TYPE).strip(),
        # 云码要**标准 base64**（不是 urlsafe），且不能带 data:image/... 前缀
        "image": base64.b64encode(image_bytes).decode("ascii"),
    }
    if extra:
        payload["extra"] = extra

    try:
        resp = requests.post(
            JFBYM_API, json=payload,
            headers={"Content-Type": "application/json"},
            timeout=JFBYM_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise CaptchaError(f"云码请求失败：{exc}") from exc

    try:
        result = resp.json()
    except ValueError as exc:
        raise CaptchaError(
            f"云码返回非 JSON（HTTP {resp.status_code}）：{(resp.text or '')[:120]}"
        ) from exc

    if not isinstance(result, dict):
        raise CaptchaError(f"云码返回结构异常：{str(result)[:120]}")

    # ⚠️ 外层 code 可能是 int 也可能是 str，统一转 int 再比较
    try:
        outer_code = int(result.get("code", -1))
    except (TypeError, ValueError):
        outer_code = -1

    if outer_code != 10000:
        reason = JFBYM_ERRORS.get(outer_code) or str(result.get("msg") or "未知错误")
        raise CaptchaError(f"云码识别失败[{outer_code}]：{reason}")

    data = result.get("data")
    # data 可能是 dict（新版）或 list（部分文档示例是 array），两种都兼容
    if isinstance(data, dict):
        code = str(data.get("data") or "").strip()
    elif isinstance(data, list) and data:
        first = data[0]
        code = str(first.get("data") if isinstance(first, dict) else first).strip()
    else:
        code = ""

    if not code:
        raise CaptchaError(f"云码返回成功但结果为空：{str(result)[:150]}")
    return code


# ============================ 统一入口 ============================

def solve(image_bytes: bytes, backend: str = "local", token: str = "",
          type_id: str = JFBYM_DEFAULT_TYPE, retries: int = 1) -> Tuple[str, str]:
    """识别验证码，返回 `(识别结果, 使用的后端)`。

    Args:
        backend: "local"（ddddocr）/ "cloud"（云码）/ "auto"（先本地，失败或
                 结果可疑时改用云端 —— 云端要 token 才生效）
        token: 云码 Token（backend 为 cloud/auto 时必填）
        type_id: 云码打码类型
        retries: 失败重试次数（含首次，所以 1 = 不重试）

    Returns:
        (code, used_backend)

    Raises:
        CaptchaError: 所有后端都失败
    """
    backend = (backend or "local").strip().lower()
    attempts = max(1, int(retries or 1))
    last_error: Optional[Exception] = None

    order = {
        "local": ["local"],
        "cloud": ["cloud"],
        # auto：先本地（免费），本地挂了或有 token 时补一次云端
        "auto": ["local", "cloud"] if token else ["local"],
    }.get(backend, ["local"])

    for attempt in range(attempts):
        for which in order:
            try:
                if which == "local":
                    return solve_local(image_bytes), "local"
                code = solve_cloud(image_bytes, token, type_id)
                return code, "cloud"
            except CaptchaError as exc:
                last_error = exc
                # 云端是付费接口，参数类错误（token 错/余额不足）重试没意义，直接抛
                msg = str(exc)
                if which == "cloud" and any(
                    k in msg for k in ("Token", "余额", "不支持", "未配置")
                ):
                    raise
                continue
        if attempt < attempts - 1:
            time.sleep(0.4)  # 换图重试前稍等，避免连打

    raise last_error or CaptchaError("验证码识别失败")


def describe_backend(backend: str, token: str = "") -> str:
    """给日志/通知用的一句话说明"""
    backend = (backend or "local").lower()
    if backend == "cloud":
        return "云码云端识别"
    if backend == "auto":
        return "本地优先(失败转云码)" if token else "本地识别(未配云码Token)"
    return "本地 ddddocr"
