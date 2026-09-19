"""端到端集成测试：Flask 应用 + HAR 模板 CRUD + 插件注册 + 真实任务执行(mock HTTP)

跑法（用测试库，不碰真 data/checkin.db）：
    python __har_test/integration.py
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = r"C:\Users\Administrator\WorkBuddy\自动签到\checkin-system"

from test_auth_helper import login_client
_HERE_FOR_HELPER = os.path.dirname(os.path.abspath(__file__))
if _HERE_FOR_HELPER not in sys.path:
    sys.path.insert(0, _HERE_FOR_HELPER)
sys.path.insert(0, ROOT)

# ⚠️ 必须在 import app.* 之前把 DB 目录指到临时目录，否则会写进生产库
TMP = tempfile.mkdtemp(prefix="checkin_test_")
import app.database as database
database.DB_DIR = TMP
database.DB_PATH = os.path.join(TMP, "test.db")

os.environ["CHECKIN_SECRET_KEY"] = "test-secret-key"

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(extra)[:260]) if extra else ""))


SAMPLE_HAR = {
    "log": {"version": "1.2", "creator": {"name": "test", "version": "1"}, "pages": [], "entries": [
        {"startedDateTime": "", "time": 100,
         "request": {"method": "GET", "url": "https://127.0.0.1:18080/home",
                     "httpVersion": "HTTP/1.1",
                     "headers": [{"name": "Host", "value": "127.0.0.1:18080"},
                                 {"name": "Cookie", "value": "sid=AA11"}],
                     "queryString": [], "cookies": []},
         "response": {"status": 200, "content": {"mimeType": "text/html"}}},
        {"startedDateTime": "", "time": 80,
         "request": {"method": "POST", "url": "https://127.0.0.1:18080/api/sign",
                     "httpVersion": "HTTP/1.1",
                     "headers": [{"name": "Content-Type", "value": "application/x-www-form-urlencoded"}],
                     "queryString": [], "cookies": [],
                     "postData": {"mimeType": "application/x-www-form-urlencoded",
                                  "text": "uid=1001&hash=zz99"}},
         "response": {"status": 200, "content": {"mimeType": "application/json"}}},
    ]}
}

try:
    from app.main import create_app

    app = create_app()
    client = login_client(app)
    check("4.1 应用启动成功", True)

    # ---- 数据库建表 ----
    db = database.get_db()
    tables = [r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    db.close()
    check("4.2 har_templates 表已建", "har_templates" in tables, tables)

    # ---- 插件被自动发现并入库 ----
    from app.plugin_loader import get_all_plugins
    plugins = get_all_plugins()
    check("4.3 har_template 插件已加载", "har_template" in plugins, list(plugins.keys()))
    p = plugins.get("har_template")
    if p:
        schema = p.form_schema
        check("4.4 插件表单有 template_id 下拉", any(f.key == "template_id" for f in schema),
              [f.key for f in schema])
        check("4.5 下拉类型为 select", any(f.key == "template_id" and f.type == "select" for f in schema))

    from app.models import PluginModel
    row = PluginModel.get_by_name("har_template")
    check("4.6 插件已写入 plugins 表", bool(row), row.get("display_name") if row else None)

    # ---- 上传解析 ----
    r = client.post("/api/har/upload", json={"content": json.dumps(SAMPLE_HAR), "source": "har"})
    d = r.get_json()
    check("4.7 /api/har/upload 解析成功", r.status_code == 200 and d.get("success"), d.get("message"))
    check("4.8 解析出 2 个请求", len(d.get("entries") or []) == 2, len(d.get("entries") or []))
    check("4.9 推断出站点名", d.get("host") == "127.0.0.1:18080", d.get("host"))

    # ---- cURL 上传 ----
    r = client.post("/api/har/upload", json={
        "content": "curl 'https://127.0.0.1:18080/api/sign' -X POST -H 'Cookie: a=1' --data 'x=1'",
        "source": "curl"})
    d2 = r.get_json()
    check("4.10 cURL 上传解析成功", d2.get("success") and len(d2["entries"]) == 1, d2.get("message"))
    check("4.11 cURL 站点名正确", d2.get("host") == "127.0.0.1:18080", d2.get("host"))

    # ---- 非法内容 ----
    r = client.post("/api/har/upload", json={"content": "{bad json", "source": "har"})
    check("4.12 非法 HAR 返回 400", r.status_code == 400, r.status_code)
    r = client.post("/api/har/upload", json={"content": "", "source": "har"})
    check("4.13 空内容返回 400", r.status_code == 400, r.status_code)

    # ---- 创建模板（把固定值改成变量） ----
    entries = d["entries"]
    entries[1]["request"]["data"] = "uid={{ username }}&hash={{ token }}"
    entries[1]["request"]["url"] = "{{ site_url }}/api/sign"
    entries[1]["rule"] = {
        "success_asserts": [{"re": "签到成功", "from": "content"}],
        "failed_asserts": [],
        "extract_variables": [],
    }
    r = client.post("/api/har", json={"name": "集成测试模板", "host": "127.0.0.1:18080", "entries": entries})
    cr = r.get_json()
    check("4.14 创建模板成功", r.status_code == 201 and cr.get("success"), cr)
    tpl_id = cr.get("id")

    # ---- 读回模板 ----
    r = client.get(f"/api/har/{tpl_id}")
    t = r.get_json()
    check("4.15 读回模板成功", r.status_code == 200 and t["name"] == "集成测试模板", t.get("name"))
    check("4.16 entries 出库即已解析为 list", isinstance(t["entries"], list), type(t["entries"]))
    check("4.17 变量自动识别（含 site_url/token/username）",
          set(t["variables"]) >= {"site_url", "token", "username"}, t["variables"])

    # ---- 列表 ----
    r = client.get("/api/har")
    check("4.18 模板列表接口", r.status_code == 200 and len(r.get_json()) >= 1)

    # ---- 更新 ----
    r = client.put(f"/api/har/{tpl_id}", json={"note": "改过的备注", "entries": entries})
    check("4.19 更新模板成功", r.get_json().get("success"), r.get_json())
    r = client.get(f"/api/har/{tpl_id}")
    check("4.20 备注已更新", r.get_json()["note"] == "改过的备注", r.get_json()["note"])

    # ---- 拒绝空 entries ----
    r = client.put(f"/api/har/{tpl_id}", json={"entries": []})
    check("4.21 拒绝清空请求列表", r.status_code == 400, r.status_code)
    r = client.post("/api/har", json={"name": "x", "entries": []})
    check("4.22 创建时拒绝空 entries", r.status_code == 400, r.status_code)

    # ---- 导出 ----
    r = client.get(f"/api/har/{tpl_id}/export")
    check("4.23 导出 HAR", r.status_code == 200 and b"entries" in r.data, r.status_code)

    # ---- 变量推断接口 ----
    r = client.post("/api/har/variables", json={"entries": entries})
    vd = r.get_json()
    check("4.24 变量推断接口", set(vd.get("variables", [])) >= {"username", "token", "site_url"},
          vd.get("variables"))

    # ---- 页面可渲染 ----
    for path, name in [("/har", "4.25 模板列表页"), (f"/har/{tpl_id}/edit", "4.26 模板编辑页"),
                       ("/har/new", "4.27 导入页")]:
        r = client.get(path)
        check(name, r.status_code == 200, r.status_code)

    # ---- 建任务：插件 + 模板 ----
    plugin = PluginModel.get_by_name("har_template")
    r = client.post("/api/tasks", json={
        "plugin_id": plugin["id"], "name": "集成测试任务",
        "username": "user001", "password": "",
        # ⚠️ enabled 必须是 1：execute_checkin 开头就检查 `task["enabled"]`，
        # 用 0 建任务再调 execute_checkin 会静默返回、一个请求都不发。
        "cron_expr": "0 9 * * *", "enabled": 1,
        "params": {"template_id": tpl_id, "variables": {"token": "TK9", "site_url": "https://127.0.0.1:18080"}},
    })
    td = r.get_json()
    check("4.28 创建任务成功", r.status_code == 201, td)
    task_id = td.get("id")

    # ---- 删除保护：模板被任务占用时应拒绝删除 ----
    r = client.delete(f"/api/har/{tpl_id}")
    check("4.29 模板被占用时拒绝删除", r.status_code == 400, r.get_json().get("message"))

    # ---- 执行任务（mock HTTP） ----
    import requests
    from app.engine import execute_checkin
    from app.models import TaskModel

    calls = []

    class _FakeRawHeaders:
        """模拟 urllib3 的 HTTPHeaderDict：只需支持 getlist('Set-Cookie')"""

        def __init__(self, set_cookies):
            self._items = list(set_cookies or [])

        def getlist(self, name):
            return list(self._items) if name.lower() == "set-cookie" else []

    class FakeResp:
        def __init__(self, status, text, set_cookie=None):
            self.status_code = status
            self.content = text.encode()
            self.text = text
            self.encoding = "utf-8"
            self.headers = {}
            # ⚠️ 这里必须是**实例**，之前误写成再调用一次（`_FH(...)()`），
            # 结果是 class 对象挂到 .raw.headers 上，取 getlist 时炸成
            # "'_FH' object is not callable"。
            self.raw = type("Raw", (), {"headers": _FakeRawHeaders(set_cookie)})()

    def fake_request(self, method, url, headers=None, data=None, timeout=None, allow_redirects=None):
        calls.append({"method": method, "url": url, "data": data.decode() if isinstance(data, bytes) else data,
                      "cookie": (headers or {}).get("Cookie", "")})
        if "/home" in url:
            return FakeResp(200, "<html>welcome</html>", ["sid=SID99"])
        if "/api/sign" in url:
            return FakeResp(200, '{"msg":"签到成功 +5 积分"}')
        return FakeResp(404, "nope")

    orig = requests.Session.request
    requests.Session.request = fake_request
    try:
        execute_checkin(task_id)
    finally:
        requests.Session.request = orig

    task = TaskModel.get_by_id(task_id)
    check("4.30 任务执行成功", task["last_result"] == "success", task["last_message"])
    check("4.31 共发了 2 个请求", len(calls) == 2, [c["url"] for c in calls])
    check("4.32 变量已渲染进请求体", "user001" in (calls[1]["data"] or ""), calls[1]["data"])
    check("4.33 抽取值渲染正确", "TK9" in (calls[1]["data"] or ""), calls[1]["data"])
    check("4.34 第一步的 Set-Cookie 带到了第二步",
          "SID99" in calls[1]["cookie"], calls[1]["cookie"])
    check("4.35 日志写入 last_extra",
          "POST" in json.dumps(task["last_extra"], ensure_ascii=False),
          str(task["last_extra"])[:150])

    # ---- cookie 回写复用：第二次执行应带着归档 cookie ----
    calls.clear()
    requests.Session.request = fake_request
    try:
        execute_checkin(task_id)
    finally:
        requests.Session.request = orig
    check("4.36 第二次执行复用归档 Cookie",
          "SID99" in calls[0]["cookie"], calls[0]["cookie"])

    # ---- 失败路径：断言不匹配 ----
    entries_bad = json.loads(json.dumps(entries))
    entries_bad[1]["rule"]["success_asserts"] = [{"re": "绝对不会出现的字样XYZ", "from": "content"}]
    client.put(f"/api/har/{tpl_id}", json={"entries": entries_bad})
    requests.Session.request = fake_request
    try:
        execute_checkin(task_id)
    finally:
        requests.Session.request = orig
    task = TaskModel.get_by_id(task_id)
    check("4.37 断言不匹配→任务失败", task["last_result"] == "failed", task["last_message"])
    check("4.38 失败信息可读", "成功断言" in task["last_message"], task["last_message"][:160])

    # ---- 删除任务后模板可删 ----
    client.delete(f"/api/tasks/{task_id}")
    r = client.delete(f"/api/har/{tpl_id}")
    check("4.39 任务删除后模板可删", r.get_json().get("success"), r.get_json())

    # ---- 不存在的模板 ----
    r = client.get("/api/har/99999")
    check("4.40 不存在模板返回 404", r.status_code == 404, r.status_code)
    r = client.post("/api/har/99999/test", json={"variables": {}})
    check("4.41 试跑不存在模板返回 404", r.status_code == 404, r.status_code)

except Exception as exc:
    import traceback
    traceback.print_exc()
    check("!! 未捕获异常", False, str(exc))
finally:
    try:
        shutil.rmtree(TMP, ignore_errors=True)
    except Exception:
        pass

print("\n" + "=" * 60)
print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print("INTEGRATION_OK" if not FAIL else "INTEGRATION_FAILED")
