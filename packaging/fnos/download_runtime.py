"""多源下载 python-build-standalone 运行时。

背景：本机走 Clash 代理时，GitHub Release 资产的重定向目标
(objects.githubusercontent.com) 会卡死 —— urllib 15 分钟拿不到响应头。
所以这里准备多条路，先 8 秒快速探测谁活着，再用赢家全量下载：

    1. requests + 系统代理（Clash）
    2. requests 直连（绕过代理环境变量）
    3. ghfast.top 加速镜像
    4. gh-proxy.com 加速镜像

支持断点续传（Range），中断重跑不从头来。
"""
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
DEST = os.path.join(BUILD, "python-runtime.tar.gz")

TAG = "20260901"
FNAME = f"cpython-3.12.14+{TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz"
GH_PATH = f"astral-sh/python-build-standalone/releases/download/{TAG}/{FNAME}"

SOURCES = [
    ("proxy", f"https://github.com/{GH_PATH}", True),
    ("direct", f"https://github.com/{GH_PATH}", False),
    ("ghfast", f"https://ghfast.top/https://github.com/{GH_PATH}", True),
    ("ghproxy", f"https://gh-proxy.com/https://github.com/{GH_PATH}", True),
]

EXPECT_MIN = 20 * 1024 * 1024   # 运行时至少 20MB，防止下到错误页


def log(msg):
    print(f"[dl] {msg}", flush=True)


def make_session(use_proxy: bool) -> requests.Session:
    s = requests.Session()
    if not use_proxy:
        s.trust_env = False   # 忽略 HTTP_PROXY 等环境变量
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s


def probe(name: str, url: str, use_proxy: bool) -> bool:
    """8 秒内能拿到响应头（且允许 Range）即认为可用"""
    try:
        s = make_session(use_proxy)
        t0 = time.time()
        r = s.get(url, stream=True, timeout=(8, 8),
                  allow_redirects=True)
        ok = r.status_code == 200 and int(r.headers.get("content-length", 0)) > EXPECT_MIN
        r.close()
        dt = time.time() - t0
        log(f"  探测 {name}: {'✓' if ok else '✗'} status={r.status_code} "
            f"len={int(r.headers.get('content-length', 0))//1024//1024}MB 用时{dt:.1f}s")
        return ok
    except Exception as e:
        log(f"  探测 {name}: ✗ {type(e).__name__}: {str(e)[:90]}")
        return False


def download(name: str, url: str, use_proxy: bool) -> bool:
    s = make_session(use_proxy)
    part = DEST + ".part"
    done = os.path.exists(DEST) and os.path.getsize(DEST) > EXPECT_MIN
    if done:
        log("目标文件已完整存在，跳过")
        return True

    headers = {}
    if os.path.exists(part):
        headers["Range"] = f"bytes={os.path.getsize(part)}-"
        log(f"断点续传：已有 {os.path.getsize(part)//1024//1024}MB")
    else:
        os.makedirs(BUILD, exist_ok=True)

    try:
        with s.get(url, stream=True, timeout=(15, 60), headers=headers,
                   allow_redirects=True) as r:
            if r.status_code not in (200, 206):
                log(f"  {name}: HTTP {r.status_code}")
                return False
            total = int(r.headers.get("content-length", 0))
            mode = "ab" if r.status_code == 206 else "wb"
            got = os.path.getsize(part) if r.status_code == 206 else 0
            t0 = time.time()
            with open(part, mode) as f:
                for chunk in r.iter_content(1024 * 256):
                    f.write(chunk)
                    got += len(chunk)
                    if time.time() - t0 > 20:
                        dt = time.time() - t0
                        log(f"  {name}: {got/1024//1024}MB"
                            f"{f'/{total/1024//1024}MB' if total else ''}"
                            f"  {got/dt/1024:.0f}KB/s")
                        t0 = time.time()
        os.replace(part, DEST)
        log(f"  {name}: 完成 {os.path.getsize(DEST)//1024//1024}MB")
        return True
    except Exception as e:
        log(f"  {name}: 失败 {type(e).__name__}: {str(e)[:120]}")
        return False


def main():
    os.makedirs(BUILD, exist_ok=True)
    if os.path.exists(DEST) and os.path.getsize(DEST) > EXPECT_MIN:
        log("已存在完整文件，跳过下载")
        return 0

    log("探测可用下载源 …")
    for name, url, use_proxy in SOURCES:
        if probe(name, url, use_proxy):
            log(f"使用源：{name}")
            if download(name, url, use_proxy):
                print("RUNTIME_DOWNLOAD_OK")
                return 0
    print("RUNTIME_DOWNLOAD_FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
