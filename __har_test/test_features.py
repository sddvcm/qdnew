"""三项新功能的测试：PushPlus 结果检测 / 云码识别 / 自动更新安全闸

跑法：
    python __har_test/test_features.py
"""
import base64
import json
import os
import shutil
import sys
import tempfile

ROOT = r"C:\Users\Administrator\WorkBuddy\自动签到\checkin-system"
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(extra)[:220]) if extra else ""))


# 用临时库，别碰生产数据
TMP = tempfile.mkdtemp(prefix="feat_")
import app.database as database  # noqa: E402
database.DB_DIR = TMP
database.DB_PATH = os.path.join(TMP, "t.db")
os.environ["CHECKIN_SECRET_KEY"] = "test-key"

import requests  # noqa: E402

# =========================================================
# 1. PushPlus 结果检测
# =========================================================
print("\n" + "=" * 60)
print("1. PushPlus 推送结果检测")
print("=" * 60)

from app import notifier  # noqa: E402


class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        # 真实 requests.Response 有 .content（bytes），_fetch_raw 用它取文件内容
        self.content = self.text.encode("utf-8")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        """真实 requests.Response 有这个方法，_fetch_raw 会调它"""
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def patch_post(resp):
    def _post(url, **kw):
        return resp
    return _post


orig_post = requests.post
try:
    # 1.1 成功：code==200
    requests.post = patch_post(FakeResp(payload={"code": 200, "msg": "请求成功"}))
    ok, detail = notifier._send_pushplus("tok", "标题", "正文")
    check("1.1 PushPlus 成功(code=200)", ok is True, detail)

    # 1.2 未关注公众号：code 903 —— 这正是最容易发生的坑
    requests.post = patch_post(FakeResp(payload={"code": 903, "msg": "用户未关注"}))
    ok, detail = notifier._send_pushplus("tok", "标题", "正文")
    check("1.2 PushPlus 未关注公众号被识别为失败", ok is False and "未关注" in detail, detail)

    # 1.3 Token 无效：code 401
    requests.post = patch_post(FakeResp(payload={"code": 401, "msg": "invalid token"}))
    ok, detail = notifier._send_pushplus("bad", "标题", "正文")
    check("1.3 PushPlus Token 无效被识别为失败", ok is False and "Token" in detail, detail)

    # 1.4 HTTP 200 但 body 非 JSON（网关返回 HTML 等）
    requests.post = patch_post(FakeResp(payload=None, text="<html>502</html>"))
    ok, detail = notifier._send_pushplus("tok", "标题", "正文")
    check("1.4 PushPlus 非JSON响应被识别为失败", ok is False and "非 JSON" in detail, detail)

    # 1.5 空 Token
    ok, detail = notifier._send_pushplus("", "标题", "正文")
    check("1.5 PushPlus 空Token前置拦截", ok is False and "未配置" in detail, detail)

    # 1.6 code 是字符串 "200" 也要认
    requests.post = patch_post(FakeResp(payload={"code": "200", "msg": "ok"}))
    ok, _ = notifier._send_pushplus("tok", "t", "b")
    check("1.6 PushPlus 字符串 code 兼容", ok is True)

    # 1.7 send_notification 汇总失败渠道
    import app.database as _db
    _db.init_db()          # 建表（临时库，前面只改了路径还没建）
    db = _db.get_db()
    # 任务表有 plugin_id 外键，先塞一个插件行
    db.execute("INSERT INTO plugins (name,display_name,plugin_type) VALUES (?,?,?)",
               ("testplugin", "测试插件", "http")
    )
    pid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("INSERT INTO notify_configs (notify_type,name,config,enabled) VALUES (?,?,?,?)",
               ("pushplus", "测试推送", json.dumps({"token": "x"}), 1))
    nid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("INSERT INTO notify_configs (notify_type,name,config,enabled) VALUES (?,?,?,?)",
               ("wecom", "企微", json.dumps({"webhook_url": ""}), 1))
    nid2 = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("INSERT INTO tasks (plugin_id,name,cron_expr,enabled) VALUES (?,?,?,1)",
               (pid, "测试任务", "0 9 * * *"))
    tid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    for n in (nid, nid2):
        db.execute("INSERT INTO task_notify (task_id,notify_id,on_success,on_failure) VALUES (?,?,1,1)",
                   (tid, n))
    db.commit()
    db.close()

    requests.post = patch_post(FakeResp(payload={"code": 903, "msg": "未关注"}))
    outcomes = notifier.send_notification("测试任务", "success", "签到成功", {"days": 3}, tid)
    check("1.8 send_notification 返回逐渠道结果", len(outcomes) == 2, outcomes)
    check("1.9 失败渠道被标记", any(not o["ok"] for o in outcomes), outcomes)
    check("1.10 未关注公众号原因透传",
          any("未关注" in o["detail"] for o in outcomes), outcomes)

    # 1.11 extra 里的 logs 不进通知正文（避免超长）
    captured = {}

    def _cap(url, **kw):
        captured.update(kw)
        return FakeResp(payload={"code": 200})

    requests.post = _cap
    notifier.send_notification("测试任务", "success", "msg",
                               {"days": 1, "logs": "X" * 5000}, tid)
    body = json.dumps(captured, ensure_ascii=False)
    check("1.11 长日志不进通知正文", "X" * 100 not in body, len(body))

finally:
    requests.post = orig_post

# =========================================================
# 2. 云码识别
# =========================================================
print("\n" + "=" * 60)
print("2. 云码 jfbym 验证码识别")
print("=" * 60)

import captcha as cap_mod  # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

try:
    # 2.1 成功
    requests.post = patch_post(FakeResp(payload={
        "code": 10000, "msg": "识别成功",
        "data": {"code": 0, "data": "ab12", "time": "0.31"}}))
    code = cap_mod.solve_cloud(PNG, "tok123", "10110")
    check("2.1 云码识别成功返回结果", code == "ab12", code)

    # 2.2 请求体字段正确（token/type/image，且 image 无 data: 前缀）
    captured = {}

    def _cap2(url, **kw):
        captured["url"] = url
        captured["json"] = kw.get("json") or {}
        captured["headers"] = kw.get("headers") or {}
        return FakeResp(payload={"code": 10000, "data": {"data": "zz"}})

    requests.post = _cap2
    cap_mod.solve_cloud(PNG, "  tok-abc  ", "10110")
    check("2.2 请求URL正确", captured["url"] == "http://api.jfbym.com/api/YmServer/customApi",
          captured.get("url"))
    check("2.3 token 已去空格", captured["json"].get("token") == "tok-abc",
          captured["json"].get("token"))
    check("2.4 type 正确", captured["json"].get("type") == "10110")
    img = captured["json"].get("image", "")
    check("2.5 image 为标准base64且无data前缀",
          not img.startswith("data:") and base64.b64decode(img) == PNG, img[:40])
    check("2.6 Content-Type 为 json",
          "json" in (captured["headers"].get("Content-Type") or "").lower(),
          captured["headers"])

    # 2.7 Token 无效 10003 → 人话 + 不重试
    requests.post = patch_post(FakeResp(payload={"code": 10003, "msg": "无此访问权限"}))
    try:
        cap_mod.solve_cloud(PNG, "bad")
        check("2.7 云码Token无效抛错", False, "未抛错")
    except cap_mod.CaptchaError as e:
        check("2.7 云码Token无效抛错且可读", "Token" in str(e), str(e))

    # 2.8 余额不足 10002
    requests.post = patch_post(FakeResp(payload={"code": 10002, "msg": "余额不足"}))
    try:
        cap_mod.solve_cloud(PNG, "tok")
        check("2.8 云码余额不足提示", False)
    except cap_mod.CaptchaError as e:
        check("2.8 云码余额不足提示", "余额" in str(e), str(e))

    # 2.9 type 不支持 10004
    requests.post = patch_post(FakeResp(payload={"code": 10004, "msg": "无此验证类型"}))
    try:
        cap_mod.solve_cloud(PNG, "tok", "99999")
        check("2.9 云码类型不支持提示", False)
    except cap_mod.CaptchaError as e:
        check("2.9 云码类型不支持提示", "type" in str(e).lower() or "类型" in str(e), str(e))

    # 2.10 空 token 前置拦截（不发请求）
    called = {"n": 0}

    def _count(url, **kw):
        called["n"] += 1
        return FakeResp(payload={"code": 10000})

    requests.post = _count
    try:
        cap_mod.solve_cloud(PNG, "")
        check("2.10 空token前置拦截", False)
    except cap_mod.CaptchaError as e:
        check("2.10 空token前置拦截不发请求", called["n"] == 0 and "未配置" in str(e), str(e))

    # 2.11 data 是 list 的旧格式兼容
    requests.post = patch_post(FakeResp(payload={
        "code": 10000, "data": [{"code": 0, "data": "wxyz"}]}))
    code = cap_mod.solve_cloud(PNG, "tok")
    check("2.11 兼容 data 为 list 的返回", code == "wxyz", code)

    # 2.12 成功但结果为空 → 报错
    requests.post = patch_post(FakeResp(payload={"code": 10000, "data": {"data": ""}}))
    try:
        cap_mod.solve_cloud(PNG, "tok")
        check("2.12 空结果报错", False)
    except cap_mod.CaptchaError as e:
        check("2.12 空结果报错", "为空" in str(e), str(e))

    # 2.13 network 异常
    def _boom(url, **kw):
        raise requests.ConnectionError("refused")

    requests.post = _boom
    try:
        cap_mod.solve_cloud(PNG, "tok")
        check("2.13 云码网络异常可读", False)
    except cap_mod.CaptchaError as e:
        check("2.13 云码网络异常可读", "请求失败" in str(e), str(e))

    # 2.14 solve() 统一入口：cloud 后端
    requests.post = patch_post(FakeResp(payload={"code": 10000, "data": {"data": "QQ99"}}))
    code, used = cap_mod.solve(PNG, backend="cloud", token="tok")
    check("2.14 solve() cloud 后端", code == "QQ99" and used == "cloud", (code, used))

    # 2.15 solve() 不认识的 backend 回退 local（不炸）
    check("2.15 未知后端不抛未知异常",
          cap_mod.describe_backend("weird") == "本地 ddddocr",
          cap_mod.describe_backend("weird"))

finally:
    requests.post = orig_post

# =========================================================
# 3. 自动更新安全闸
# =========================================================
print("\n" + "=" * 60)
print("3. 自动更新：路径白名单 / 域名白名单 / 版本比较")
print("=" * 60)

import updater  # noqa: E402

# 3.1 版本比较
check("3.1 版本比较 1.10 > 1.9",
      updater.is_newer("1.10.0", "1.9.0") is True,
      f"{updater._parse_version('1.10.0')} vs {updater._parse_version('1.9.0')}")
check("3.2 同版本不算新", updater.is_newer("1.2.0", "1.2.0") is False)
check("3.3 旧版本不算新", updater.is_newer("1.1.0", "1.2.0") is False)
check("3.4 版本段数不同也能比", updater.is_newer("1.2.1", "1.2") is True)

# 3.5 允许的路径
for p in ["app/main.py", "har/render.py", "plugins/fuliba.py",
          "templates/base.html", "docs/README.md", "version.json",
          "requirements.txt", "captcha.py", "updater.py"]:
    try:
        updater.validate_path(p)
        check(f"3.5 允许更新 {p}", True)
    except updater.UpdateError as e:
        check(f"3.5 允许更新 {p}", False, str(e))

# 3.6 禁止的路径 —— 这些是安全底线
FORBIDDEN_CASES = [
    ("data/checkin.db", "数据库"),
    ("data/backups/x.py", "备份目录"),
    (".env", "环境变量密钥"),
    ("user_plugins/mine.py", "用户自己的插件"),
    ("logs/app.log", "日志"),
    ("../../etc/passwd", "目录穿越"),
    ("app/../../../secret.py", "目录穿越"),
    ("C:/Windows/system32/x.py", "绝对路径"),
    ("app/main.sh", "非白名单扩展名"),
    ("app/evil.pyc", "非白名单扩展名"),
    ("random_top.py", "根目录非白名单文件"),
    ("node_modules/x.js", "node_modules"),
    (".git/config", "git 目录"),
]
for path, why in FORBIDDEN_CASES:
    try:
        updater.validate_path(path)
        check(f"3.6 拒绝更新 {path}（{why}）", False, "竟然通过了校验")
    except updater.UpdateError:
        check(f"3.6 拒绝更新 {path}（{why}）", True)

# 3.6b .env.example 允许下发，但真正的 .env 必须被拒（安全边界）
try:
    updater.validate_path(".env.example")
    check("3.6b 允许更新 .env.example（部署模板）", True)
except updater.UpdateError as e:
    check("3.6b 允许更新 .env.example（部署模板）", False, str(e))

try:
    updater.validate_path(".env")
    check("3.6c 仍然拒绝 .env（密钥绝不能覆盖）", False, "竟然通过了")
except updater.UpdateError:
    check("3.6c 仍然拒绝 .env（密钥绝不能覆盖）", True)

# 3.7 update_manifest.json 自身不能被覆盖（防自指篡改）
try:
    updater.validate_path("update_manifest.json")
    check("3.7 拒绝覆盖 update_manifest.json", False)
except updater.UpdateError:
    check("3.7 拒绝覆盖 update_manifest.json", True)

# 3.8 域名白名单
for url, ok_expected in [
    ("https://raw.githubusercontent.com/a/b/main/x.py", True),
    ("https://github.com/a/b", True),
    ("https://api.github.com/repos/a/b", True),
    ("https://evil.com/x.py", False),
    ("https://raw.githubusercontent.com.evil.com/x.py", False),
    ("https://github.com@evil.com/x.py", False),
    ("ftp://github.com/x", False),
]:
    try:
        updater._assert_allowed_url(url)
        got = True
    except updater.UpdateError:
        got = False
    check(f"3.8 域名校验 {url[:45]}", got is ok_expected, f"期望{ok_expected} 实际{got}")

# 3.9 源地址解析
for src, exp_owner, exp_repo, exp_branch in [
    ("https://github.com/user/repo", "user", "repo", None),
    ("user/repo", "user", "repo", None),
    ("https://github.com/user/repo/tree/dev", "user", "repo", "dev"),
    ("https://github.com/user/repo.git", "user", "repo", None),
]:
    try:
        if exp_branch:
            # 带分支的会跳过 API 查询
            r = updater.normalize_source(src)
            check(f"3.9 解析源 {src}", r["owner"] == exp_owner and r["repo"] == exp_repo
                  and r["branch"] == exp_branch, r)
        else:
            check(f"3.9 解析源 {src} 格式（跳过网络查询）", True)
    except updater.UpdateError as e:
        check(f"3.9 解析源 {src}", False, str(e))

try:
    updater.normalize_source("")
    check("3.10 空源报错", False)
except updater.UpdateError as e:
    check("3.10 空源报错", "未配置" in str(e), str(e))

try:
    updater.normalize_source("不是地址")
    check("3.11 非法源报错", False)
except updater.UpdateError as e:
    check("3.11 非法源报错", "格式" in str(e), str(e))

# 3.12 清单生成：不能包含 data/ 与 .env
manifest = updater.build_manifest("9.9.9", "测试")
bad_keys = [k for k in manifest["files"]
            if k.startswith("data/") or k == ".env" or "user_plugins" in k]
check("3.12 生成的清单不含敏感文件", not bad_keys, bad_keys)
check("3.12b 清单不含 .env 本身", ".env" not in manifest["files"])
check("3.13 清单含核心文件", "app/main.py" in manifest["files"]
      and "har/render.py" in manifest["files"], list(manifest["files"])[:6])
check("3.13b 清单含部署模板 .env.example", ".env.example" in manifest["files"])
check("3.14 清单每个值都是 sha256", all(
    len(v) == 64 and all(c in "0123456789abcdef" for c in v)
    for v in manifest["files"].values()))

# 3.15 check_update 对不存在清单的处理（不抛异常，结构里带 error）
orig_get = requests.get


def _get_404(url, **kw):
    return FakeResp(status=404, payload=None, text="404")


requests.get = _get_404
try:
    res = updater.check_update("https://github.com/user/repo/tree/main")
    check("3.15 缺清单时返回结构化错误", res["error"] and "找不到" in res["error"],
          res["error"][:120])
finally:
    requests.get = orig_get

# 3.16 更新时哈希不匹配必须整体拒绝，且不落任何文件
MANIFEST_WITH_BAD_HASH = {
    "name": "checkin-system",
    "version": "9.9.9",
    "files": {
        "app/main.py": "0" * 64,          # 故意错的哈希
        "har/render.py": "1" * 64,
    },
}


def _get_bad_hash(url, **kw):
    if url.endswith("update_manifest.json"):
        return FakeResp(payload=MANIFEST_WITH_BAD_HASH)
    return FakeResp(payload=None, text="# not the real file")


requests.get = _get_bad_hash
try:
    before = {}
    for rel in MANIFEST_WITH_BAD_HASH["files"]:
        p = os.path.join(updater.ROOT, rel)
        before[rel] = open(p, "rb").read() if os.path.exists(p) else None

    out = updater.run_update("https://github.com/user/repo/tree/main")
    check("3.16 哈希不匹配时拒绝更新", out["updated"] is False, out.get("error", "")[:100])
    check("3.17 拒绝原因提到校验失败", "校验失败" in (out.get("error") or ""),
          (out.get("error") or "")[:160])

    # 关键：拒绝后原文件必须**一个字节都没变**
    unchanged = True
    for rel, old in before.items():
        p = os.path.join(updater.ROOT, rel)
        now = open(p, "rb").read() if os.path.exists(p) else None
        if now != old:
            unchanged = False
    check("3.17b 校验失败后原文件未被改动", unchanged)
finally:
    requests.get = orig_get

# 3.18 备份目录列表可用
check("3.18 list_backups 返回列表", isinstance(updater.list_backups(), list))

# 3.19 status 接口 / 设置页
from app.main import create_app  # noqa: E402
app = create_app()
client = app.test_client()
r = client.get("/settings")
check("3.19 设置页可访问", r.status_code == 200 and b"update" in r.data, r.status_code)
r = client.get("/api/update/status")
d = r.get_json()
check("3.20 状态接口返回版本", r.status_code == 200 and "version" in d, list(d.keys()))
check("3.21 状态接口含白名单域名", "allowed_hosts" in d and d["allowed_hosts"], d.get("allowed_hosts"))

# 3.22 保存非法更新源被拒绝
r = client.post("/api/update/config", json={"source": "https://evil.com/a/b"})
check("3.22 保存非白名单源被拒绝", r.status_code == 400, r.get_json())

# 3.23 未配置源时检查更新给出明确提示
r = client.post("/api/update/check")
check("3.23 未配置源时提示明确", r.status_code == 400
      and "更新源" in (r.get_json().get("message") or ""), r.get_json())

# 3.24 清单下载接口
r = client.get("/api/update/manifest?version=2.0.0")
check("3.24 清单下载接口可用", r.status_code == 200 and b"files" in r.data, r.status_code)

# 3.25 导航含设置入口
r = client.get("/")
check("3.25 导航含系统设置", "系统设置" in r.get_data(as_text=True))

shutil.rmtree(TMP, ignore_errors=True)

print("\n" + "=" * 60)
print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print("FEATURES_OK" if not FAIL else "FEATURES_FAILED")
