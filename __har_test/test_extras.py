# -*- coding: utf-8 -*-
"""extras 组件机制测试：安装/校验/路径穿越防护/启用停用/卸载/状态。

用临时数据目录 + 人造组件包，不碰真实数据。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="extras_test_")
os.environ["CHECKIN_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)
os.chdir(ROOT)

HAS_REQUESTS = True
try:
    import requests  # noqa: F401
except ImportError:
    HAS_REQUESTS = False

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


def make_pack(files=None, name="captcha-local", manifest_extra=None,
              sp_path="site-packages", fake_mod=None):
    """构造一个组件包 zip 字节"""
    files = files or {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        man = {"name": name, "version": "9.9", "python": "3.12",
               "platform": "linux_x86_64",
               "provides": ["ddddocr"]}
        if manifest_extra:
            man.update(manifest_extra)
        zf.writestr("manifest.json", json.dumps(man))
        for rel, content in files.items():
            zf.writestr(f"{sp_path}/{rel}", content)
        if fake_mod:
            zf.writestr(f"{sp_path}/{fake_mod}/__init__.py", "VALUE = 42\n")
    return buf.getvalue()


import app.extras as extras  # noqa: E402

print("=" * 60)
print("1. 初始状态")
st = extras.status()
check("1.1 初始未安装", st["installed"] is False)
check("1.2 初始未启用", st["enabled"] is False)
check("1.3 site-packages 路径含 extras",
      "extras" in extras.site_packages_dir().replace("\\", "/"))

print()
print("2. 安装正常包")
pack = make_pack(files={"ddddocr/__init__.py": "X=1\n" + "# pad\n" * 4000,
                        "cv2/__init__.py": "Y=2\n",
                        "onnxruntime/__init__.py": "Z=3\n"},
                 fake_mod="mymod")
res = extras.install_from_zip(pack)
check("2.1 安装返回 installed=True", res.get("installed") is True)
check("2.2 安装后自动启用", res.get("enabled") is True)
check("2.3 版本从 manifest 读出", res.get("version") == "9.9", res.get("version"))
check("2.4 目录真的建了", os.path.isdir(extras.site_packages_dir()))
check("2.5 文件真的落地",
      os.path.isfile(os.path.join(extras.site_packages_dir(), "ddddocr",
                                  "__init__.py")))
check("2.6 大小已统计", res.get("size_mb", 0) > 0)
check("2.7 启用标记存在", os.path.isfile(
    os.path.join(extras.pack_dir(), ".enabled")))

print()
print("3. sys.path 注入")
check("3.1 路径已进 sys.path", extras.site_packages_dir() in sys.path)
sys.path.insert(0, extras.site_packages_dir())  # 模拟真实场景
try:
    import importlib
    m = importlib.import_module("mymod")
    check("3.2 能 import 包内模块", getattr(m, "VALUE", None) == 42)
except Exception as e:
    check("3.2 能 import 包内模块", False, repr(e))

print()
print("4. 安全防护")
# 4a 非 zip
try:
    extras.install_from_zip(b"this is not a zip")
    check("4.1 非 zip 被拒", False, "竟然成功了")
except extras.ExtrasError as e:
    check("4.1 非 zip 被拒", "zip" in str(e).lower() or "有效" in str(e), str(e))
# 4b 缺 manifest
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as zf:
    zf.writestr("site-packages/x/__init__.py", "1")
try:
    extras.install_from_zip(buf.getvalue())
    check("4.2 缺 manifest 被拒", False, "竟然成功了")
except extras.ExtrasError as e:
    check("4.2 缺 manifest 被拒", "manifest" in str(e), str(e))
# 4c name 不对
try:
    extras.install_from_zip(make_pack(name="evil-pack",
                                      files={"a/__init__.py": "1"}))
    check("4.3 name 不符被拒", False, "竟然成功了")
except extras.ExtrasError as e:
    check("4.3 name 不符被拒", "captcha-local" in str(e), str(e))
# 4d 路径穿越
try:
    bad = make_pack(files={"../../../evil.py": "pwned"})
    extras.install_from_zip(bad)
    # 可能被"没有 site-packages 内容"拦下，那也算拒了
    escaped = os.path.isfile(os.path.join(TMP, "..", "evil.py"))
    check("4.4 路径穿越被拒", not escaped, "文件逃逸了!")
except extras.ExtrasError as e:
    check("4.4 路径穿越被拒", True, "")
# 4e 没有 site-packages 内容
try:
    extras.install_from_zip(make_pack(files={}, sp_path="other"))
    check("4.5 空内容被拒", False, "竟然成功了")
except extras.ExtrasError as e:
    check("4.5 空内容被拒", "site-packages" in str(e), str(e))

print()
print("5. 启停切换")
extras.set_enabled(False)
check("5.1 停用后 enabled=False", extras.is_enabled() is False)
check("5.2 停用后移出 sys.path",
      extras.site_packages_dir() not in sys.path)
extras.set_enabled(True)
check("5.3 重新启用回到 sys.path",
      extras.site_packages_dir() in sys.path)

print()
print("6. 重复安装（覆盖）")
res2 = extras.install_from_zip(make_pack(
    files={"ddddocr/__init__.py": "NEW=1\n"}, manifest_extra={"version": "10.0"}))
check("6.1 版本更新为 10.0", res2.get("version") == "10.0", res2.get("version"))
check("6.2 旧文件被清掉（不残留 cv2）",
      not os.path.isdir(os.path.join(extras.site_packages_dir(), "cv2")))
check("6.3 新文件在位",
      os.path.isfile(os.path.join(extras.site_packages_dir(), "ddddocr",
                                  "__init__.py")))

print()
print("7. 卸载")
r = extras.uninstall()
check("7.1 卸载返回成功", r.get("success") is True)
check("7.2 目录已删除", not os.path.isdir(extras.pack_dir()))
check("7.3 状态回到未安装", extras.status()["installed"] is False)
check("7.4 已移出 sys.path",
      extras.site_packages_dir() not in sys.path)

print()
print("8. 未安装时启用应报错")
try:
    extras.set_enabled(True)
    check("8.1 未安装启用报错", False, "竟然成功了")
except extras.ExtrasError as e:
    check("8.1 未安装启用报错", "尚未安装" in str(e), str(e))

print()
print("9. 构建器 build_pack_from_site_packages")
sp = os.path.join(TMP, "fake_sp")
os.makedirs(os.path.join(sp, "ddddocr"), exist_ok=True)
os.makedirs(os.path.join(sp, "unrelated_pkg"), exist_ok=True)
os.makedirs(os.path.join(sp, "cv2"), exist_ok=True)
with open(os.path.join(sp, "ddddocr", "__init__.py"), "w") as f:
    f.write("A=1\n")
with open(os.path.join(sp, "cv2", "cv2.abi3.so"), "wb") as f:
    f.write(b"\x7fELFfake")
with open(os.path.join(sp, "unrelated_pkg", "__init__.py"), "w") as f:
    f.write("nope\n")
outz = os.path.join(TMP, "built.zip")
extras.build_pack_from_site_packages(sp, outz, version="1.2")
with zipfile.ZipFile(outz) as zf:
    zn = zf.namelist()
check("9.1 产物含 manifest", "manifest.json" in zn)
check("9.2 白名单内 ddddocr 被打入",
      any("ddddocr/__init__.py" in n for n in zn))
check("9.3 cv2 被打入", any("cv2/cv2.abi3.so" in n for n in zn))
check("9.4 无关包被排除",
      not any("unrelated_pkg" in n for n in zn), str(zn))
with zipfile.ZipFile(outz) as zf:
    man = json.loads(zf.read("manifest.json"))
check("9.5 manifest name 正确", man.get("name") == "captcha-local")
check("9.6 version 正确", man.get("version") == "1.2")
# 用构建器产物反过来装一遍
res3 = extras.install_from_zip(open(outz, "rb").read())
check("9.7 构建产物可安装", res3.get("installed") is True)

print()
print("10. captcha 集成")
import captcha  # noqa: E402
check("10.1 captcha 能 import", True)
check("10.2 local_available 可调用", isinstance(captcha.local_available(), bool))
# 未装真组件时，local 应给人话错误
try:
    captcha.solve_local(b"\x89PNG_fake")
    check("10.3 无真组件时 local 报人话", False, "竟然成功了")
except captcha.CaptchaError as e:
    msg = str(e)
    check("10.3 无真组件时 local 报人话",
          ("本地识别组件" in msg or "ddddocr" in msg), msg)
except Exception as e:
    check("10.3 无真组件时 local 报人话", False,
          f"抛了非 CaptchaError（会崩任务）：{type(e).__name__}: {e}")

print()
print("=" * 60)
print(f"PASS {PASS}  FAIL {FAIL}")
if FAILS:
    print("失败项：")
    for f in FAILS:
        print("  -", f)
print("EXTRAS_TEST_OK" if FAIL == 0 else "EXTRAS_TEST_FAILED")

# 清理
try:
    shutil.rmtree(TMP, ignore_errors=True)
except Exception:
    pass
sys.exit(0 if FAIL == 0 else 1)
