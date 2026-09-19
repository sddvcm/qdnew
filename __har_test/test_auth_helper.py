"""让 e2e 测试自动登录 —— 一行接入。

背景：v1.8.0 加了访问密码保护，默认**开启**。已有的 e2e 测试都是
`app.test_client()` 直接打接口，于是全部撞上 401「请先登录」，
41 项挂掉。

两种做法，我选了后者：

  ✗ 测试里关掉鉴权（`auth_enabled=0`）
    问题：这等于让「鉴权默认开启」这条产品行为**永远没被 e2e 覆盖过**，
    而且哪天鉴权逻辑坏了，测试照样全绿。
  ✓ **测试里正常登录一次**（本模块的做法）
    走的是和真实用户完全一样的路径，鉴权一旦坏掉测试立刻红。

接入方式（两行）：

    from test_auth_helper import login_client
    ...
    app = create_app()
    client = login_client(app)          # ← 替代 app.test_client()

⚠️ 默认密码 123456 是**产品约定**，改产品默认值时这里也要跟着改；
   helper 里做了断言，改错了会立刻报错而不是静默失败。
"""
import os
import sys

DEFAULT_PASSWORD = "123456"


def login_client(app, password=None):
    """返回一个**已登录**的 test_client。

    password 传 None 时用出厂默认密码。
    """
    password = password or DEFAULT_PASSWORD
    c = app.test_client()

    r = c.get("/api/auth/status")
    status = r.get_json() or {}
    if not status.get("enabled"):
        # 鉴权被显式关掉了（比如某个测试自己改了配置）—— 直接用即可
        return c

    resp = c.post("/api/auth/login", json={"password": password})
    if resp.status_code != 200:
        raise AssertionError(
            f"e2e helper 登录失败（HTTP {resp.status_code}）："
            f"{resp.get_data(as_text=True)[:200]}\n"
            f"默认密码应为 {DEFAULT_PASSWORD} —— 若产品改了这个约定，"
            f"请同步更新 __har_test/test_auth_helper.py")

    # 自检：确认真的登进去了，别让后续断言以「其实是 401」的方式失败
    probe = c.get("/api/auth/status")
    if not (probe.get_json() or {}).get("authed"):
        raise AssertionError("登录返回成功但会话没建立 —— 检查 auth.check_auth")
    return c
