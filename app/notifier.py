"""多渠道通知推送

⚠️ 设计要点：**每个渠道都要检查响应体**，不能只看"请求发出去了"。
这些服务的 HTTP 状态码普遍是 200，真正的结果在响应 JSON 的 code 字段里
（例如 PushPlus 的 903 = 未关注公众号、401 = Token 无效）。只看状态码会
把"没发出去"当成"已发送"，用户永远不知道自己收不到通知。
`send_notification` 会把每个渠道的结果收集进返回值，任务日志里能看到。
"""
import base64
import hashlib
import hmac
import json
import time

import requests

# ============================ PushPlus 结果码 ============================
# 官方文档 https://www.pushplus.plus/doc/ ；成功判定是 **code == 200**
PUSHPLUS_ERRORS = {
    401: "Token 无效，请检查推送 Token 是否填对",
    402: "Token 已过期",
    403: "Token 已禁用",
    500: "系统内部错误，稍后重试",
    888: "请求参数错误",
    900: "用户不存在",
    901: "用户未激活",
    903: "用户未关注「PushPlus 推送加」公众号 —— 请先微信扫码关注才能收到推送",
    904: "用户已达当日推送上限",
    905: "消息内容为空",
}


def send_notification(task_name: str, result: str, message: str, extra: dict,
                      task_id: int = None) -> list:
    """根据任务配置发送通知。

    返回 `[{channel, ok, detail}, ...]`，engine 会把它写进任务日志 ——
    之前这里返回 None，推送失败用户完全看不见。
    """
    outcomes: list = []
    if not task_id:
        return outcomes

    configs = NotifyModel.get_for_task(task_id)
    for cfg in configs:
        cfg_data = _loads(cfg["config"])
        should_send = (result == "success" and cfg["on_success"]) or \
                      (result == "failed" and cfg["on_failure"])
        if not should_send:
            continue

        title = f"[{'成功' if result == 'success' else '失败'}] {task_name}"
        body = message
        if extra:
            # 不要把超长日志塞进通知正文
            body += "\n" + "\n".join(
                f"{k}: {str(v)[:200]}" for k, v in extra.items() if k != "logs")

        notifier = cfg["notify_type"]
        try:
            ok, detail = _dispatch(notifier, cfg_data, title, body)
        except Exception as e:
            ok, detail = False, f"异常: {e}"
        outcomes.append({"channel": notifier, "ok": ok, "detail": detail})
        if not ok:
            print(f"[Notifier] {notifier} 推送失败: {detail}")

    return outcomes


def _dispatch(notifier, cfg_data, title, body):
    """分发到具体渠道，返回 (是否成功, 说明)"""
    if notifier == "pushplus":
        return _send_pushplus(cfg_data.get("token", ""), title, body)
    if notifier == "serverchan":
        return _send_serverchan(cfg_data.get("sendkey", ""), title, body)
    if notifier == "bark":
        return _send_bark(cfg_data.get("device_key", ""), title, body,
                          cfg_data.get("server_url", ""))
    if notifier == "webhook":
        return _send_webhook(cfg_data, title, body)
    if notifier == "dingtalk":
        return _send_dingtalk(cfg_data.get("webhook_url", ""), cfg_data.get("secret", ""),
                              title, body)
    if notifier == "wecom":
        return _send_wecom(cfg_data.get("webhook_url", ""), title, body)
    if notifier == "telegram":
        return _send_telegram(cfg_data.get("bot_token", ""), cfg_data.get("chat_id", ""),
                              title, body)
    return False, f"未知的通知类型: {notifier}"


def _loads(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
    return value or {}


# ============================ PushPlus ============================

def _send_pushplus(token, title, body, template="txt"):
    """PushPlus 推送。

    ⚠️ **成功判定是响应 JSON 的 code == 200，不是 HTTP 状态码**。
    即便 Token 完全无效，HTTP 也返回 200，所以必须解析响应体，
    否则"推送失败"会被静默当成成功。
    """
    if not token or not str(token).strip():
        return False, "未配置推送 Token"

    resp = requests.post(
        "https://www.pushplus.plus/send",
        json={"token": str(token).strip(), "title": title,
              "content": body, "template": template},
        timeout=15,
    )
    try:
        data = resp.json()
    except ValueError:
        return False, f"响应非 JSON（HTTP {resp.status_code}）：{(resp.text or '')[:120]}"

    try:
        code = int(data.get("code", -1))
    except (TypeError, ValueError):
        code = -1

    if code == 200:
        return True, data.get("msg") or "发送成功"

    reason = PUSHPLUS_ERRORS.get(code) or str(data.get("msg") or "未知错误")
    return False, f"[{code}] {reason}"


# ============================ 其它渠道 ============================

def _send_serverchan(sendkey, title, body):
    if not sendkey:
        return False, "未配置 SendKey"
    resp = requests.get(f"https://sctapi.ftqq.com/{sendkey}.send",
                        params={"title": title, "desp": body}, timeout=15)
    try:
        data = resp.json()
        # Server酱：code==0 为成功
        if int(data.get("code", -1)) == 0:
            return True, "发送成功"
        return False, str(data.get("message") or data.get("error") or "发送失败")
    except (ValueError, TypeError):
        return False, f"响应异常（HTTP {resp.status_code}）"


def _send_bark(device_key, title, body, server_url=""):
    if not device_key:
        return False, "未配置设备 Key"
    url = (server_url or "https://api.day.app").rstrip("/")
    resp = requests.post(f"{url}/push", json={
        "device_key": device_key, "title": title, "body": body}, timeout=15)
    try:
        data = resp.json()
        if int(data.get("code", -1)) == 200:
            return True, "发送成功"
        return False, str(data.get("message") or "发送失败")
    except (ValueError, TypeError):
        return False, f"响应异常（HTTP {resp.status_code}）"


def _send_webhook(cfg, title, body):
    url = cfg.get("url", "")
    if not url:
        return False, "未配置 Webhook URL"
    headers = cfg.get("headers", {}) or {}
    template = cfg.get("body_template", "{{title}}\n{{body}}")
    payload = template.replace("{{title}}", title).replace("{{body}}", body)
    resp = requests.post(url, data=payload.encode(), headers=headers, timeout=15)
    if resp.status_code < 400:
        return True, f"HTTP {resp.status_code}"
    return False, f"HTTP {resp.status_code}：{(resp.text or '')[:120]}"


def _send_dingtalk(webhook_url, secret, title, body):
    if not webhook_url:
        return False, "未配置 Webhook 地址"
    timestamp = str(round(time.time() * 1000))
    url = webhook_url
    if secret:
        sign_str = f"{timestamp}\n{secret}"
        sign = base64.b64encode(
            hmac.new(secret.encode(), sign_str.encode(), hashlib.sha256).digest()).decode()
        url = f"{webhook_url}&timestamp={timestamp}&sign={sign}"
    resp = requests.post(url, json={
        "msgtype": "text", "text": {"content": f"{title}\n{body}"}}, timeout=15)
    try:
        data = resp.json()
        if int(data.get("errcode", -1)) == 0:
            return True, "发送成功"
        return False, str(data.get("errmsg") or "发送失败")
    except (ValueError, TypeError):
        return False, f"响应异常（HTTP {resp.status_code}）"


def _send_wecom(webhook_url, title, body):
    if not webhook_url:
        return False, "未配置 Webhook 地址"
    resp = requests.post(webhook_url, json={
        "msgtype": "text", "text": {"content": f"{title}\n{body}"}}, timeout=15)
    try:
        data = resp.json()
        if int(data.get("errcode", -1)) == 0:
            return True, "发送成功"
        return False, str(data.get("errmsg") or "发送失败")
    except (ValueError, TypeError):
        return False, f"响应异常（HTTP {resp.status_code}）"


def _send_telegram(bot_token, chat_id, title, body):
    if not bot_token or not chat_id:
        return False, "未配置 Bot Token 或 Chat ID"
    text = f"*{title}*\n{body}"
    resp = requests.get(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                        params={"chat_id": chat_id, "text": text,
                                "parse_mode": "Markdown"}, timeout=15)
    try:
        data = resp.json()
        if data.get("ok"):
            return True, "发送成功"
        return False, str(data.get("description") or "发送失败")
    except (ValueError, TypeError):
        return False, f"响应异常（HTTP {resp.status_code}）"


# 延迟导入，避免 models 反向依赖本模块时循环 import
from .models import NotifyModel  # noqa: E402
