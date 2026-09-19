# -*- coding: utf-8 -*-
"""端到端模拟：用 Flask test_client 走完整的"上传组件 → 启用 → 切换识别方式 → 卸载" 流程。

用真实的 185MB 组件包太重，这里用一个"结构等价但极小"的假包验证接口链路；
文件级别的完整性已由 build_captcha_pack.py 的自校验 + 前面的检查覆盖。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
TMP = tempfile.mkdtemp(prefix="extras_e2e_")
os.environ["CHECKIN_DATA_DIR"] = TMP
os.environ["CHECKIN_SECRET_KEY"] = "test-secret-key-for-e2e"
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


def make_pack(version="1.0", with_ddddocr=True):
    """造一个结构合法的迷你组件包。

    为了模拟真实组件包（提供 numpy/cv2/onnxruntime/ddddocr 四件套），
    这里为每个依赖都塞一个**能 import 的假模块**，这样 status() 的
    missing_deps 才是空的 —— 否则测的是"包不完整"而不是"接口流程"。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({
            "name": "captcha-local", "version": version,
            "python": "3.12", "platform": "linux_x86_64",
            "provides": ["ddddocr", "numpy", "cv2", "onnxruntime"],
        }))
        if with_ddddocr:
            # 假的 numpy / cv2 / onnxruntime（只要能被 import 即可）
            for mod, pad in (("numpy", 600), ("cv2", 400), ("onnxruntime", 300)):
                zf.writestr(f"site-packages/{mod}/__init__.py",
                            f"VERSION = 'fake'\n" + "# pad\n" * pad)
            zf.writestr("site-packages/ddddocr/__init__.py",
                        "class DdddOcr:\n"
                        "    def __init__(self, show_ad=False): pass\n"
                        "    def classification(self, img): return 'AB12'\n")
    return buf.getvalue()


from app.main import create_app   # noqa: E402

app = create_app()
app.config["TESTING"] = True
c = app.test_client()

print("=" * 60)
print("1. 初始状态（未装组件）")
r = c.get("/api/extras/status")
d = r.get_json()
check("1.1 接口可用", r.status_code == 200 and d.get("success"))
pack = d["pack"]
check("1.2 未安装", pack["installed"] is False)
check("1.3 未启用", pack["enabled"] is False)
check("1.4 默认识别方式=cloud", pack["config"]["backend"] == "cloud",
      pack["config"]["backend"])
check("1.5 local_available=False", pack["config"]["local_available"] is False)
check("1.6 未装时 runtime_ok=False", pack["runtime_ok"] is False)

print()
print("2. 未装组件时不允许设成「仅本地」")
r = c.post("/api/extras/settings", json={"backend": "local"})
check("2.1 被拒并给出提示", r.status_code == 400
      and "本地识别组件尚未安装" in r.get_json().get("message", ""),
      r.get_json())
# 云码则允许
r = c.post("/api/extras/settings", json={"backend": "cloud",
                                        "cloud_token": "TOK123",
                                        "cloud_type": "10110"})
check("2.2 云码设置可保存", r.status_code == 200 and r.get_json()["success"])
check("2.3 token 已回读", r.get_json()["config"]["cloud_token"] == "TOK123")

print()
print("3. 上传组件包（multipart）")
pack_bytes = make_pack("1.0")
r = c.post("/api/extras/upload",
           data={"file": (io.BytesIO(pack_bytes), "captcha-local-pack-1.0.zip")},
           content_type="multipart/form-data")
d = r.get_json()
check("3.1 上传成功", r.status_code == 200 and d.get("success"), d)
check("3.2 返回 installed=True", d["pack"]["installed"] is True)
check("3.3 自动启用", d["pack"]["enabled"] is True)
check("3.4 版本正确", d["pack"]["version"] == "1.0")
check("3.5 大小已统计", d["pack"]["size_mb"] > 0, d["pack"]["size_mb"])

print()
print("4. 状态反映真实可用性")
r = c.get("/api/extras/status")
pack = r.get_json()["pack"]
check("4.1 local_available 变 True", pack["config"]["local_available"] is True)
check("4.2 runtime_ok True（假 ddddocr 能 import）", pack["runtime_ok"] is True,
      pack["missing_deps"])

print()
print("5. 现在可以设成「仅本地」了")
r = c.post("/api/extras/settings", json={"backend": "local"})
check("5.1 设置成功", r.status_code == 200 and r.get_json()["success"],
      r.get_json())
check("5.2 已回读 local",
      r.get_json()["config"]["backend"] == "local")

print()
print("6. 测试本地识别（会真的构造 DdddOcr）")
r = c.post("/api/extras/test-local")
d = r.get_json()
check("6.1 测试通过并返回识别结果",
      r.status_code == 200 and d.get("success") and d.get("code") == "AB12", d)

print()
print("6.5 内置测试图必须是**真实可解码**的图片（回归：踩过手写假 PNG 的坑）")
import base64 as _b64  # noqa: E402
from app.routes.extras_api import _TEST_IMG_B64  # noqa: E402
_img = _b64.b64decode(_TEST_IMG_B64)
check("6.5.1 是 PNG 魔数", _img[:8] == b"\x89PNG\r\n\x1a\n", _img[:8])
check("6.5.2 含 IHDR/IDAT/IEND 三个 chunk",
      b"IHDR" in _img[:32] and b"IDAT" in _img and _img.rstrip().endswith(b"IEND\xaeB`\x82"))
# 关键：能被真实解码（早先手写的假 PNG 魔数对但结构坏，PIL 抛
# "cannot identify image file"，导致"测试本地识别"永远失败）
try:
    from PIL import Image
    _im = Image.open(io.BytesIO(_img))
    _im.load()                    # load() 才会真正解码全部数据
    check("6.5.3 PIL 能完整解码", True)
    check("6.5.4 尺寸合理（宽度>=50）", _im.size[0] >= 50, _im.size)
except ImportError:
    check("6.5.3 PIL 不可用（跳过）", True)
except Exception as e:
    check("6.5.3 PIL 能完整解码", False, f"{type(e).__name__}: {e}")

# 逐 chunk 校验 CRC —— 手写假图最容易在这里出错
import struct as _st, zlib as _zl  # noqa: E402
_pos = 8
_bad = []
while _pos < len(_img):
    _ln, = _st.unpack_from(">I", _img, _pos)
    _typ = _img[_pos + 4:_pos + 8]
    _data = _img[_pos + 8:_pos + 8 + _ln]
    _crc, = _st.unpack_from(">I", _img, _pos + 8 + _ln)
    if _zl.crc32(_typ + _data) & 0xFFFFFFFF != _crc:
        _bad.append(_typ.decode("latin-1"))
    _pos += 12 + _ln
    if _typ == b"IEND":
        break
check("6.5.5 所有 chunk CRC 正确", not _bad, _bad)

# 6.6 端到端：把"会校验图片"的假 ddddocr 装上，再打 test-local 接口。
# 早先的假 ddddocr 直接 return 'AB12'，**根本不看图片** → 图片坏了也测不出来。
# 这个版本模仿真 ddddocr：先解码图片，失败就报同样的错。
_pack2 = io.BytesIO()
with zipfile.ZipFile(_pack2, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("manifest.json", json.dumps({
        "name": "captcha-local", "version": "3.0",
        "python": "3.12", "platform": "linux_x86_64",
        "provides": ["ddddocr", "numpy", "cv2", "onnxruntime"],
    }))
    for _m, _p in (("numpy", 100), ("cv2", 80), ("onnxruntime", 60)):
        zf.writestr("site-packages/%s/__init__.py" % _m, "V=1\n" + "# pad\n" * _p)
    zf.writestr("site-packages/ddddocr/__init__.py", (
        "import io as _io\n"
        "class DdddOcr:\n"
        "    def __init__(self, show_ad=False): pass\n"
        "    def classification(self, img):\n"
        "        from PIL import Image\n"
        "        im = Image.open(_io.BytesIO(img)); im.load()\n"
        "        return 'AB12'\n"
    ))
r = c.post("/api/extras/upload",
           data={"file": (io.BytesIO(_pack2.getvalue()), "p3.zip")},
           content_type="multipart/form-data")
check("6.6.1 装上会校验图片的假 ddddocr", r.get_json()["pack"]["installed"] is True)
r = c.post("/api/extras/test-local")
d = r.get_json()
check("6.6.2 test-local 通过（说明喂进去的是合法图片）",
      r.status_code == 200 and d.get("success"), d)

print()
print("7. 停用 / 重新启用")
r = c.post("/api/extras/toggle", json={"enabled": False})
check("7.1 停用成功", r.status_code == 200 and r.get_json()["pack"]["enabled"] is False)
check("7.2 停用后 local_available=False",
      r.get_json()["pack"]["config"]["local_available"] is False)
r = c.post("/api/extras/toggle", json={"enabled": True})
check("7.3 重新启用", r.get_json()["pack"]["enabled"] is True)
check("7.4 启用后 local_available=True",
      r.get_json()["pack"]["config"]["local_available"] is True)

print()
print("8. 升级组件包（上传新版本）")
r = c.post("/api/extras/upload",
           data={"file": (io.BytesIO(make_pack("2.0")),
                          "captcha-local-pack-2.0.zip")},
           content_type="multipart/form-data")
check("8.1 版本更新为 2.0", r.get_json()["pack"]["version"] == "2.0",
      r.get_json()["pack"]["version"])

print()
print("9. 拒绝畸形输入")
r = c.post("/api/extras/upload",
           data={"file": (io.BytesIO(b"not a zip"), "x.zip")},
           content_type="multipart/form-data")
check("9.1 非 zip 被拒", r.status_code == 400, r.get_json())
r = c.post("/api/extras/upload",
           data={"file": (io.BytesIO(pack_bytes), "pack.txt")},
           content_type="multipart/form-data")
check("9.2 非 .zip 扩展名被拒", r.status_code == 400, r.get_json())
r = c.post("/api/extras/upload", json={})
check("9.3 空请求被拒", r.status_code == 400, r.get_json())

print()
print("10. 卸载（必须清掉 sys.modules，否则会误报可用）")
# 先确认此刻 ddddocr 确实已被 import 过（第 6 步构造过实例）
check("10.0 卸载前 ddddocr 已在 sys.modules",
      "ddddocr" in sys.modules)
r = c.delete("/api/extras/captcha-local")
d = r.get_json()
check("10.1 卸载成功", d.get("success") is True, d)
check("10.2 状态回到未安装", d["pack"]["installed"] is False)
check("10.3 local_available 变回 False",
      d["pack"]["config"]["local_available"] is False)
check("10.4 ddddocr 已从 sys.modules 清除",
      "ddddocr" not in sys.modules, "缓存未清理，会误报可用")
check("10.5 extras 路径已移出 sys.path",
      all("extras" not in p for p in sys.path if "captcha-local" in p))

print()
print("11. 卸载后 local 报人话（不崩）")
import captcha  # noqa: E402
try:
    captcha.solve_local(b"\x89PNG")
    check("11.1 报 CaptchaError", False, "竟然成功了")
except captcha.CaptchaError as e:
    check("11.1 报 CaptchaError 且提示装组件",
          "本地识别组件" in str(e), str(e))
except Exception as e:
    check("11.1 报 CaptchaError", False, f"{type(e).__name__}: {e}")

print()
print("12. 页面能渲染（含新 UI）")
r = c.get("/settings")
html = r.get_data(as_text=True)
check("12.1 /settings 200", r.status_code == 200)
for k in ("本地验证码识别", "uploadExtras", "captchaBackend", "testLocalCaptcha"):
    check(f"12.2 页面含 {k}", k in html)

print()
print("=" * 60)
print(f"PASS {PASS}  FAIL {FAIL}")
if FAILS:
    for f in FAILS:
        print("  -", f)
print("E2E_OK" if FAIL == 0 else "E2E_FAILED")

shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
