"""为 fpk 准备内置的 Linux x86_64 Python 运行时 + 全部依赖 wheel。

为什么要这一步
--------------
飞牛 fpk 要求「安装时不需要在线下载」。本机是 Windows，无法直接产出 Linux
可执行环境，所以采用：
    1. 下载 python-build-standalone 的 **linux x86_64 可移植解释器**
    2. 用 pip 的 `--platform` 参数**下载 wheel 而不安装**（交叉下载）
    3. 把所有 wheel 解包进运行时目录的 site-packages
最终得到一个可以直接 tar 进 fpk、在飞牛上解压即用的自包含环境。

⚠️ 关键约束
-----------
- `pip download --platform manylinux2014_x86_64` 只能下 **wheel**，不能下 sdist
  （源码包需要在那台机器上编译）。所以任何没有 manylinux wheel 的依赖都会失败。
  已核对：本项目全部依赖都有 x86_64 wheel。
- `--only-binary=:all:` 强制只用 wheel，宁可失败也不要混进 sdist。
- 运行时选 3.12（与项目 Dockerfile 一致，Python 3.13 的部分 wheel 覆盖还不全）。
- 输出目录会很大（约 300~500MB），这是「内置环境」的必然代价。
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
FPK_DIR = os.path.dirname(HERE)                       # packaging/fnos
PROJECT_ROOT = os.path.dirname(FPK_DIR)               # 项目根
BUILD_DIR = os.path.join(FPK_DIR, "build")
RUNTIME_DIR = os.path.join(BUILD_DIR, "runtime")
WHEEL_DIR = os.path.join(BUILD_DIR, "wheels")

# 与项目 Dockerfile 保持一致，避免行为差异
PY_VERSION = "3.12"
PY_TAG = "cp312"
PLATFORM = "manylinux2014_x86_64"
PYTHON_STANDALONE_TAG = "20260901"                    # python-build-standalone release

RUNTIME_URL = (
    f"https://github.com/astral-sh/python-build-standalone/releases/download/"
    f"{PYTHON_STANDALONE_TAG}/"
    f"cpython-3.12.14+{PYTHON_STANDALONE_TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz"
)

# 直接读取项目 requirements.txt，避免两处清单不一致
def _requirements():
    path = os.path.join(PROJECT_ROOT, "requirements.txt")
    pkgs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            pkgs.append(line)
    return pkgs


def log(msg):
    print(f"[prepare] {msg}", flush=True)


def download(url, dest):
    """带重试的下载（本机代理偶有抖动）"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) > 1024:
        log(f"已存在，跳过：{os.path.basename(dest)} ({os.path.getsize(dest)//1024//1024}MB)")
        return dest
    import time
    for attempt in range(3):
        try:
            log(f"下载 {os.path.basename(dest)} …")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as out:
                shutil.copyfileobj(resp, out, 1024 * 256)
            log(f"  完成 {os.path.getsize(dest)//1024//1024}MB")
            return dest
        except Exception as e:
            if attempt == 2:
                raise
            log(f"  第 {attempt+1} 次失败：{e}，重试")
            time.sleep(3)


def step1_runtime():
    """下载并解压可移植 Python 运行时"""
    log("=" * 60)
    log("步骤 1/3：准备 Linux x86_64 Python 运行时")
    log("=" * 60)
    tar_path = os.path.join(BUILD_DIR, "python-runtime.tar.gz")
    download(RUNTIME_URL, tar_path)

    if os.path.isdir(RUNTIME_DIR) and os.path.exists(os.path.join(RUNTIME_DIR, "bin", "python3")):
        log("运行时已解压，跳过")
        return

    if os.path.isdir(RUNTIME_DIR):
        shutil.rmtree(RUNTIME_DIR, ignore_errors=True)
    os.makedirs(RUNTIME_DIR, exist_ok=True)

    log("解压运行时…")
    with tarfile.open(tar_path, "r:gz") as tf:
        tf.extractall(BUILD_DIR)

    # 压缩包解压后是 python/ 目录，重命名成 runtime/
    extracted = os.path.join(BUILD_DIR, "python")
    if os.path.isdir(extracted):
        if os.path.isdir(RUNTIME_DIR):
            shutil.rmtree(RUNTIME_DIR, ignore_errors=True)
        os.rename(extracted, RUNTIME_DIR)

    py = os.path.join(RUNTIME_DIR, "bin", "python3")
    log(f"运行时就绪：{py}")
    log(f"  （注意：这是 Linux 二进制，本机 Windows 无法直接执行，属正常）")


def step2_wheels():
    """交叉下载全部依赖的 Linux wheel"""
    log("=" * 60)
    log("步骤 2/3：下载 Linux x86_64 依赖 wheel（不安装）")
    log("=" * 60)
    os.makedirs(WHEEL_DIR, exist_ok=True)

    pkgs = _requirements()
    log(f"依赖清单（来自 requirements.txt）：{', '.join(pkgs)}")

    cmd = [
        sys.executable, "-m", "pip", "download",
        "--dest", WHEEL_DIR,
        "--platform", PLATFORM,
        "--python-version", PY_VERSION,
        "--implementation", "cp",
        "--only-binary=:all:",          # 强制只用 wheel，禁止源码包
        "--no-deps" if False else "--no-cache-dir",
        "-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
        *pkgs,
    ]
    log("执行：" + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print(r.stdout[-4000:] if r.stdout else "")
    if r.returncode != 0:
        print(r.stderr[-4000:] if r.stderr else "")
        raise SystemExit(f"下载 wheel 失败（退出码 {r.returncode}）")

    whls = sorted(f for f in os.listdir(WHEEL_DIR) if f.endswith(".whl"))
    total = sum(os.path.getsize(os.path.join(WHEEL_DIR, f)) for f in whls)
    log(f"共 {len(whls)} 个 wheel，合计 {total//1024//1024}MB")
    for w in whls:
        log(f"  {os.path.getsize(os.path.join(WHEEL_DIR, w))//1024:>7}KB  {w}")


def step3_install():
    """把 wheel 解包进运行时的 site-packages

    不调用 pip install（那需要目标平台的 Python 来跑），而是直接把 wheel
    当 zip 解开 —— wheel 本质就是个 zip，纯 Python 包直接解压即可用；
    带 C 扩展的包，其 .so 也在 wheel 里预编译好了。
    """
    log("=" * 60)
    log("步骤 3/3：把 wheel 解包进运行时 site-packages")
    log("=" * 60)

    # 找 site-packages
    lib_dir = os.path.join(RUNTIME_DIR, "lib")
    sp = None
    for d in os.listdir(lib_dir):
        cand = os.path.join(lib_dir, d, "site-packages")
        if os.path.isdir(cand):
            sp = cand
            break
    if not sp:
        raise SystemExit(f"找不到 site-packages，运行时结构异常：{lib_dir}")
    log(f"目标：{sp}")

    whls = sorted(f for f in os.listdir(WHEEL_DIR) if f.endswith(".whl"))
    if not whls:
        raise SystemExit("wheel 目录为空，先跑步骤 2")

    for w in whls:
        path = os.path.join(WHEEL_DIR, w)
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                zf.extractall(sp)
                # .data 目录里的东西（脚本、二进制）需要挪到正确位置
                for n in names:
                    if ".data/" not in n:
                        continue
                    parts = n.split(".data/", 1)
                    if len(parts) != 2 or not parts[1]:
                        continue
                    wheel_name = parts[0]
                    rest = parts[1]
                    if "/" not in rest:
                        continue
                    kind, rel = rest.split("/", 1)
                    mapping = {"purelib": sp, "platlib": sp}
                    dest_base = mapping.get(kind)
                    if kind == "scripts":
                        dest_base = os.path.join(RUNTIME_DIR, "bin")
                    elif kind == "data":
                        dest_base = RUNTIME_DIR
                    elif kind == "headers":
                        dest_base = os.path.join(RUNTIME_DIR, "include")
                    if not dest_base:
                        continue
                    src = os.path.join(sp, n)
                    dst = os.path.join(dest_base, rel)
                    if os.path.isfile(src):
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        if not os.path.exists(dst):
                            shutil.copy2(src, dst)
        except Exception as e:
            log(f"  ⚠️ 解包 {w} 出错：{e}")
            continue
        log(f"  ✓ {w}")

    # 清掉 wheel 解压时留下的 .data 残留
    for d in list(os.listdir(sp)):
        if d.endswith(".data"):
            shutil.rmtree(os.path.join(sp, d), ignore_errors=True)

    log("完成。")
    return sp


def verify(sp):
    """静态校验：关键包文件确实就位（本机跑不了 Linux 二进制，只能查文件）"""
    log("=" * 60)
    log("校验：关键包是否就位")
    log("=" * 60)
    checks = [
        ("flask", "flask/app.py"),
        ("jinja2", "jinja2/__init__.py"),
        ("requests", "requests/__init__.py"),
        ("cryptography", "cryptography/__init__.py"),
        ("nacl (pynacl)", "nacl/__init__.py"),
        ("apscheduler", "apscheduler/__init__.py"),
        ("ddddocr", "ddddocr/__init__.py"),
        ("onnxruntime", "onnxruntime/__init__.py"),
        ("cv2", "cv2/__init__.py"),
        ("numpy", "numpy/__init__.py"),
        ("PIL", "PIL/__init__.py"),
        ("sqlite3", "sqlite3/__init__.py"),
    ]
    ok = True
    for name, rel in checks:
        p = os.path.join(sp, rel)
        exists = os.path.exists(p)
        # 二进制扩展单独查（.so 命名带平台后缀）
        if not exists and "." not in rel.split("/")[0]:
            pass
        log(f"  {'✓' if exists else '✗'} {name}  ({rel})")
        if not exists:
            ok = False

    # 查 C 扩展 .so 是否真的带过来了
    log("")
    log("C 扩展二进制：")
    for mod in ["cryptography", "nacl", "onnxruntime", "cv2", "numpy", "PIL"]:
        base = os.path.join(sp, mod)
        sos = []
        if os.path.isdir(base):
            for dirpath, _, files in os.walk(base):
                for f in files:
                    if f.endswith((".so", ".pyd")):
                        sos.append(os.path.relpath(os.path.join(dirpath, f), sp))
        log(f"  {mod}: {len(sos)} 个 .so" + (f"  例: {sos[0]}" if sos else "  ⚠️ 无"))

    return ok


if __name__ == "__main__":
    os.makedirs(BUILD_DIR, exist_ok=True)
    step1_runtime()
    step2_wheels()
    sp = step3_install()
    ok = verify(sp)

    # 统计体积
    total = 0
    for dirpath, _, files in os.walk(RUNTIME_DIR):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    log("")
    log(f"运行时总大小：{total//1024//1024}MB")
    log("PREPARE_OK" if ok else "PREPARE_PARTIAL")
