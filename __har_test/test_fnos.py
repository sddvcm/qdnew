# -*- coding: utf-8 -*-
"""飞牛平台适配层测试（app/fnos.py + /api/system/env）

重点验证**降级路径**：这个模块被诊断页调用，它自己出错就没法诊断了，
所以任何探测都必须不抛异常、如实回报"读不到 + 为什么"。

同时锁住一个关键行为：数据目录的**取径优先级**
    软链（/var/apps/<app>/shares/<app>） → TRIM_DATA_SHARE_PATHS → 私有目录
"""
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

from test_auth_helper import login_client
TMP = tempfile.mkdtemp(prefix="fnos_test_")
os.environ["CHECKIN_DATA_DIR"] = TMP
os.environ["CHECKIN_SECRET_KEY"] = "test-key"
_HERE_FOR_HELPER = os.path.dirname(os.path.abspath(__file__))
if _HERE_FOR_HELPER not in sys.path:
    sys.path.insert(0, _HERE_FOR_HELPER)
sys.path.insert(0, ROOT)

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILS.append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {extra}")


from app import fnos  # noqa: E402

print("=" * 62)
print("1. 数据目录与来源")
check("1.1 data_dir 用 CHECKIN_DATA_DIR", fnos.data_dir() == TMP, fnos.data_dir())
os.environ["CHECKIN_DATA_DIR_SRC"] = "共享目录(软链)"
check("1.2 来源可读取", fnos.data_dir_source() == "共享目录(软链)",
      fnos.data_dir_source())
os.environ.pop("CHECKIN_DATA_DIR_SRC", None)
check("1.3 无来源时返回空串（不炸）", fnos.data_dir_source() == "")

print()
print("2. 可写性探测")
check("2.1 可写目录判定为 True", fnos.writable(TMP) is True)
check("2.2 不存在的目录判定为 False",
      fnos.writable(os.path.join(TMP, "nope", "nope")) is False)
check("2.3 空路径判定为 False", fnos.writable("") is False)
# 探针文件不能残留
left = [f for f in os.listdir(TMP) if f.startswith(".write_probe")]
check("2.4 探针文件已清理", not left, left)

print()
print("3. 共享目录取径（软链优先）")
n = fnos.app_name()
check("3.1 app_name 有默认值", n == "checkin-system", n)
links = fnos.share_links()
check("3.2 给出两种官方拼写的候选", len(links) == 2, links)
check("3.3 候选含 shares 与 share 拼写",
      any("/shares/" in p for p in links) and any("/share/" in p for p in links),
      links)
check("3.4 本机无软链时 share_root 为空",
      fnos.share_root() == "", repr(fnos.share_root()))
# 环境变量作为第二取径
os.environ["TRIM_DATA_SHARE_PATHS"] = "/vol1/@appshare/" + n + ":/vol2/@appshare/" + n
check("3.5 无软链时回退环境变量", fnos.share_root() == "/vol1/@appshare/" + n,
      fnos.share_root())
os.environ["TRIM_DATA_SHARE_PATHS"] = ""
check("3.6 变量为空串也不炸", fnos.share_root() == "", repr(fnos.share_root()))
os.environ.pop("TRIM_DATA_SHARE_PATHS", None)

print()
print("4. fnpack 环境识别")
os.environ["CHECKIN_DATA_DIR"] = "/vol1/@appdata/checkin-system/data"
check("4.1 @appdata 路径识别为 fnpack", fnos.is_fnpack() is True)
os.environ["CHECKIN_DATA_DIR"] = "/vol1/@appshare/checkin-system/data"
check("4.2 @appshare 路径识别为 fnpack", fnos.is_fnpack() is True)
os.environ["CHECKIN_DATA_DIR"] = TMP
check("4.3 普通路径识别为非 fnpack", fnos.is_fnpack() is False, TMP)

print()
print("5. 开放 API 降级（关键：绝不抛异常）")
check("5.1 无 socket 时 openapi_available=False",
      fnos.openapi_available() is False)
pf = fnos.platform_config()
check("5.2 platform_config 返回字典而非抛异常", isinstance(pf, dict), type(pf))
check("5.3 明确告知不可用原因", pf.get("available") is False and pf.get("error"),
      pf)
check("5.4 原因可读（提到飞牛或 token）",
      ("飞牛" in pf.get("error", "")) or ("token" in pf.get("error", "").lower()),
      pf.get("error"))

print()
print("6. data_health 结构")
os.environ["CHECKIN_DATA_DIR"] = TMP
h = fnos.data_health()
for k in ("data_dir", "source", "on_share", "writable", "is_fnpack",
          "share_root", "share_links"):
    check(f"6.x 含字段 {k}", k in h, list(h.keys()) if k not in h else "")
check("6.1 writable=True", h["writable"] is True)
check("6.2 on_share=False（本机无共享目录）", h["on_share"] is False)
check("6.3 share_links 是列表且带 exists",
      isinstance(h["share_links"], list)
      and all("exists" in x for x in h["share_links"]), h["share_links"])

print()
print("7. 环境信息接口")
from app.main import create_app  # noqa: E402
app = create_app()
app.config["TESTING"] = True
c = login_client(app)
r = c.get("/api/system/env")
check("7.1 /api/system/env 200", r.status_code == 200, r.status_code)
if r.status_code == 200:
    d = r.get_json()
    check("7.2 含 data 段", "data" in d and "data_dir" in d["data"])
    check("7.3 含 platform 段", "platform" in d)
    check("7.4 含 openapi_ready", "openapi_ready" in d)
    check("7.5 data_dir 与运行环境一致", d["data"]["data_dir"] == TMP,
          d["data"]["data_dir"])

print()
print("=" * 62)
print(f"PASS {PASS}  FAIL {FAIL}")
if FAILS:
    for f in FAILS:
        print("  -", f)
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
