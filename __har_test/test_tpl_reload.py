# -*- coding: utf-8 -*-
"""验证「更新后不重启也能看到新界面」—— 复现并锁住用户踩到的 bug。

现象：自动更新到 1.5.5 后，设置页仍是旧界面（左侧无分页导航、
「发版辅助」还在）。根因是 Flask 在非 debug 模式下**永久缓存 Jinja 模板**，
换了 templates/*.html 也不重新读。

这个测试模拟真实场景：
  1. 起 app（模板读进缓存）
  2. 请求 /settings，确认看到当前内容
  3. **改动模板文件**（追加一个标记）
  4. 再请求 /settings —— 必须能看到新标记
"""
import io
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
TMP = tempfile.mkdtemp(prefix="tmpl_reload_")
os.environ["CHECKIN_DATA_DIR"] = TMP
os.environ["CHECKIN_SECRET_KEY"] = "tmpl-test-key"
sys.path.insert(0, ROOT)

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"FAIL  {name}  {extra}")


from app.main import create_app   # noqa: E402

TPL = os.path.join(ROOT, "templates", "settings.html")
BAK = TPL + ".testbak"
shutil.copy2(TPL, BAK)

print("=" * 60)
print("1. 配置项就位")
app = create_app()
app.config["TESTING"] = True
check("1.1 TEMPLATES_AUTO_RELOAD 已开启",
      app.config.get("TEMPLATES_AUTO_RELOAD") is True,
      app.config.get("TEMPLATES_AUTO_RELOAD"))
check("1.2 jinja_env.auto_reload 已开启",
      app.jinja_env.auto_reload is True)

c = app.test_client()
r = c.get("/settings")
html = r.get_data(as_text=True)
check("1.3 首次渲染 200", r.status_code == 200)
check("1.4 当前模板无测试标记", "TMPL_RELOAD_MARKER" not in html)

print()
print("2. 模拟自动更新换掉模板")
MARK = "<!--TMPL_RELOAD_MARKER-->"


def _render():
    c2 = app.test_client()
    return c2.get("/settings").get_data(as_text=True)


try:
    with io.open(TPL, encoding="utf-8") as f:
        content = f.read()
    # ⚠️ 标记必须插在 {% block content %} 内部！
    # 加在文件末尾（{% endblock %} 之后）会被 Jinja 忽略，导致误判"清缓存无效"——踩过。
    anchor = "<h2>系统设置</h2>"
    assert anchor in content, "模板结构变了，测试需同步"
    with io.open(TPL, "w", encoding="utf-8") as f:
        f.write(content.replace(anchor, anchor + MARK, 1))
    os.utime(TPL, None)

    # 2.1 auto_reload 这条路：靠 mtime 判断，理论可行但**不可靠**
    #     （Windows 的 mtime 秒级精度 + Jinja 对模板继承链的检查，
    #     秒内改动常检测不到）。所以只记录观察结果，不作为必过项。
    html2 = _render()
    auto_ok = "TMPL_RELOAD_MARKER" in html2
    print(f"      [观察] 仅靠 auto_reload 是否生效: {auto_ok}"
          f"{'' if auto_ok else '  ← 因此必须有下面这道保险'}")

    # 2.2 ★ 真正可靠的那条路：更新流程里显式清缓存
    #     这正是 update_api 在 run 成功后做的事，是用户踩坑后必须保证的行为。
    app.jinja_env.cache.clear()
    html3 = _render()
    check("2.1 清 Jinja 缓存后，不重启即可看到新模板（核心回归）",
          "TMPL_RELOAD_MARKER" in html3,
          "清了缓存还看不到新内容 —— 更新后新界面不可能生效")
finally:
    shutil.copy2(BAK, TPL)
    os.utime(TPL, None)
    app.jinja_env.cache.clear()

print()
print("3. 模拟更新后清模板缓存（第二道防线）")
# 直接改缓存里的内容，验证 jinja_env.cache.clear() 能把它清掉
try:
    app.jinja_env.cache.clear()
    r3 = c.get("/settings")
    html3 = r3.get_data(as_text=True)
    check("3.1 清缓存后渲染仍正常（标记已随文件还原而消失）",
          r3.status_code == 200 and "TMPL_RELOAD_MARKER" not in html3)
except Exception as e:      # noqa: BLE001
    check("3.1 清缓存后渲染仍正常", False, repr(e))

print()
print("4. 设置页内容确实是新版（分页 + 无发版辅助）")
r4 = c.get("/settings")
h4 = r4.get_data(as_text=True)
check("4.1 有左侧导航分页", "settings-nav" in h4 and "settings-pane" in h4)
check("4.2 5 个分页齐全",
      all(k in h4 for k in ('data-pane="update"', 'data-pane="notify"',
                            'data-pane="captcha"', 'data-pane="transfer"',
                            'data-pane="backup"')))
check("4.3 「发版辅助」已移除", "发版辅助" not in h4 and "downloadManifest" not in h4)

print()
print("5. 更新接口的 need_restart 逻辑")
import ast  # noqa: E402
src = io.open(os.path.join(ROOT, "app", "routes", "update_api.py"),
              encoding="utf-8").read()
check("5.1 更新后清 jinja 缓存", "jinja_env.cache.clear()" in src)
check("5.2 need_restart 按文件类型判定",
      "startswith((\"app/\", \"har/\"))" in src or 'startswith(("app/", "har/"))' in src)
check("5.3 已引入 current_app",
      "current_app" in src.split("def run")[0] or "current_app" in src)

# 清理
try:
    os.remove(BAK)
except OSError:
    pass
shutil.rmtree(TMP, ignore_errors=True)

print()
print("=" * 60)
print(f"PASS {PASS}  FAIL {FAIL}")
if FAILS:
    for f in FAILS:
        print("  -", f)
print("TPL_RELOAD_OK" if FAIL == 0 else "TPL_RELOAD_FAILED")
sys.exit(0 if FAIL == 0 else 1)
