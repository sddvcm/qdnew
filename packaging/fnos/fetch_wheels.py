"""快速获取全部依赖 wheel：pip 解析 + 多源高速直拉。

为什么不用 pip download 直下
----------------------------
pip 走 files.pythonhosted.org 经本机代理只有 ~26KB/s，200MB 依赖要 2 小时。
而 tuna 等国内镜像的 /packages/<hash-path>/ 目录结构与 pypi 完全镜像，
直拉速度 5~20MB/s。所以：

    1. `pip install --dry-run --report` 让 pip 解析出**精确的 wheel 清单与 URL**
       （只拉元数据，几秒完成；平台标签与正式下载完全一致）
    2. 对每个 wheel 依次尝试：本地已有 → tuna/packages/<path> → 原始 URL
       （requests 直连 + 代理两套，谁通用谁）
    3. 全部落盘后用 `pip download --no-index --find-links` 做最终校验
       （确保离线目录完整可解析，也就是 fpk 内置依赖的最终形态）
"""
import json
import os
import subprocess
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
WHEELS = os.path.join(BUILD, "wheels")
PROJECT = os.path.dirname(os.path.dirname(HERE))     # 项目根
REPORT = os.path.join(BUILD, "resolve_report.json")

PLATFORM_FLAGS = [
    "--platform", "manylinux2014_x86_64",
    "--platform", "manylinux_2_17_x86_64",
    "--platform", "manylinux_2_27_x86_64",
    "--platform", "manylinux_2_28_x86_64",
]
# 每个源 = (名称, URL变换函数, 是否走系统代理)。
# 实测（2026-09-18，本机 Clash 7897）：pythonhosted **直连 2.3MB/s**，走代理仅 60KB/s
# —— Clash 规则把它分到慢节点了。所以"直连"排第二，仅次于镜像命中。
MIRRORS = [
    ("tuna",
     lambda url: url.replace("https://files.pythonhosted.org/",
                             "https://pypi.tuna.tsinghua.edu.cn/packages/"),
     True),
    ("tencent",
     lambda url: url.replace("https://files.pythonhosted.org/",
                             "https://mirrors.cloud.tencent.com/pypi/packages/"),
     True),
    ("direct",
     lambda url: url,
     False),          # 原始 URL 但绕过代理环境变量
    ("proxy",
     lambda url: url,
     True),           # 原始 URL 走代理（兜底）
]


def log(msg):
    print(f"[wheels] {msg}", flush=True)


def resolve_urls() -> list:
    """让 pip 解析出精确的 wheel 清单（dry-run，只下元数据）"""
    os.makedirs(BUILD, exist_ok=True)
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--dry-run", "--report", REPORT,
        "--ignore-installed",
        *PLATFORM_FLAGS,
        "--python-version", "3.12",
        "--implementation", "cp",
        "--only-binary=:all:",
        "--no-cache-dir",
        "-r", os.path.join(PROJECT, "requirements.txt"),
    ]
    log("pip 解析依赖（只拉元数据）…")
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not os.path.exists(REPORT):
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        raise SystemExit("pip 解析失败")
    report = json.load(open(REPORT, encoding="utf-8"))
    items = []
    for pkg in report.get("install", []):
        url = (pkg.get("download_info") or {}).get("url", "")
        if url.endswith(".whl"):
            items.append((os.path.basename(url), url))
    return items


def make_session(use_proxy: bool) -> requests.Session:
    s = requests.Session()
    if not use_proxy:
        s.trust_env = False   # 绕过 HTTP_PROXY 等环境变量
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s


def fetch(url: str, dest: str) -> bool:
    """多源下载单个 wheel，带进度与断点续传"""
    part = dest + ".part"
    for name, make, use_proxy in MIRRORS:
        src = make(url)
        shown = src.split("/")[-1][:60]
        headers = {}
        if os.path.exists(part):
            headers["Range"] = f"bytes={os.path.getsize(part)}-"
        try:
            s = make_session(use_proxy)
            t0 = time.time()
            with s.get(src, stream=True, timeout=(15, 60),
                       headers=headers) as r:
                if r.status_code not in (200, 206):
                    log(f"    [{name}] HTTP {r.status_code}，换源")
                    continue
                total = int(r.headers.get("content-length", 0))
                mode = "ab" if r.status_code == 206 else "wb"
                got = os.path.getsize(part) if r.status_code == 206 else 0
                last = t0
                with open(part, mode) as f:
                    for chunk in r.iter_content(1024 * 256):
                        f.write(chunk)
                        got += len(chunk)
                        now = time.time()
                        if now - last > 5:
                            log(f"    {got/1024//1024}MB"
                                f"{f'/{total/1024//1024}MB' if total else ''}"
                                f"  {got/(now-t0)/1024:.0f}KB/s")
                            last = now
            os.replace(part, dest)
            log(f"    ✓ [{name}] {os.path.basename(dest)} "
                f"{os.path.getsize(dest)/1024/1024:.1f}MB "
                f"({time.time()-t0:.0f}s)")
            return True
        except Exception as e:
            log(f"    [{name}] {type(e).__name__}: {str(e)[:80]}，换源")
            continue
    return False


def main():
    os.makedirs(WHEELS, exist_ok=True)
    items = resolve_urls()
    log(f"共 {len(items)} 个 wheel")
    missing = []
    for fname, url in items:
        dest = os.path.join(WHEELS, fname)
        if os.path.exists(dest) and os.path.getsize(dest) > 1024:
            log(f"  已有  {fname}")
            continue
        missing.append((fname, url, dest))
    log(f"待下载 {len(missing)} 个")

    failed = []
    for i, (fname, url, dest) in enumerate(missing, 1):
        log(f"({i}/{len(missing)}) {fname[:70]}")
        if not fetch(url, dest):
            failed.append(fname)

    if failed:
        log(f"失败 {len(failed)} 个：{failed}")
        print("WHEELS_FAILED")
        return 1

    # 最终校验：离线目录必须能被 pip 完整解析（这就是 fpk 内置依赖的最终形态）
    log("校验：--no-index --find-links 离线解析 …")
    cmd = [
        sys.executable, "-m", "pip", "download",
        "--dest", os.path.join(BUILD, "verify"),
        "--no-index", "--find-links", WHEELS,
        *PLATFORM_FLAGS,
        "--python-version", "3.12",
        "--implementation", "cp",
        "--only-binary=:all:",
        "-r", os.path.join(PROJECT, "requirements.txt"),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-2000:])
        raise SystemExit("离线解析校验失败")
    log("离线解析通过")
    print("WHEELS_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
