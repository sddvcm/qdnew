"""并行分块下载 wheel（aria2 思路：单连接被限速，多连接并发 ×N 倍）。

实测（2026-09-18）：pythonhosted 单连接 40KB/s~2.3MB/s 波动极大，且随时间衰减。
把一个文件拆 8 个 Range 块并发下载再拼装，等效带宽 = 8 × 单连接，
且任何一块断了单独重试该块（.chunk-N 缓存，重跑续传）。

仅对 > 4MB 的文件启用分块；小文件单连接直下。
"""
import io
import json
import os
import sys
import threading
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
WHEELS = os.path.join(BUILD, "wheels")
REPORT = os.path.join(BUILD, "resolve_report.json")

CHUNK_THREADS = 8
CHUNK_MIN = 4 * 1024 * 1024        # 超过 4MB 才分块
SOURCES_FN = [
    ("tuna", lambda u: u.replace("https://files.pythonhosted.org/",
                                 "https://pypi.tuna.tsinghua.edu.cn/packages/"), True),
    ("direct", lambda u: u, False),
    ("proxy", lambda u: u, True),
]

_log_lock = threading.Lock()


def log(msg):
    with _log_lock:
        print(f"[pdl] {msg}", flush=True)


def make_session(use_proxy):
    s = requests.Session()
    if not use_proxy:
        s.trust_env = False
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s


def working_session():
    """返回一个能连通 pythonhosted 的会话（先直连后代理）"""
    s = make_session(False)
    try:
        s.head("https://files.pythonhosted.org/", timeout=(8, 8))
        return s
    except Exception:
        pass
    s = make_session(True)
    return s


def download_chunk(s, url, start, end, dest_part, idx, state):
    """下载 [start, end] 闭区间块到 .chunk-idx，支持续传"""
    chunk_path = f"{dest_part}.chunk-{idx}"
    offset = 0
    if os.path.exists(chunk_path):
        offset = os.path.getsize(chunk_path)
    need = end - start + 1
    while offset < need:
        headers = {"Range": f"bytes={start + offset}-{end}"}
        try:
            got = 0
            with s.get(url, stream=True, timeout=(15, 60), headers=headers) as r:
                if r.status_code not in (200, 206):
                    state[f"{idx}"] = f"HTTP {r.status_code}"
                    return False
                if r.status_code == 200 and offset:
                    # 服务器不支持 Range：只能整段重来
                    offset = 0
                    start_local = start
                    f = open(chunk_path, "wb")
                else:
                    start_local = start + offset
                    f = open(chunk_path, "ab")
                with f:
                    for chunk in r.iter_content(1024 * 128):
                        f.write(chunk)
                        got += len(chunk)
                        offset += len(chunk)
                        state[f"{idx}"] = f"{offset}/{need}"
            return offset >= need
        except Exception as e:
            with _log_lock:
                log(f"    chunk{idx}: {type(e).__name__}: {str(e)[:60]}，重试")
            time.sleep(1)
    return True


def parallel_download(url, dest, total_size):
    part = dest + ".part"
    n_chunks = CHUNK_THREADS
    bounds = []
    step = total_size // n_chunks
    for i in range(n_chunks):
        start = i * step
        end = (total_size - 1) if i == n_chunks - 1 else (start + step - 1)
        bounds.append((start, end))

    s = working_session()
    state = {}
    threads = []
    for i, (a, b) in enumerate(bounds):
        t = threading.Thread(target=download_chunk,
                             args=(s, url, a, b, part, i, state), daemon=True)
        t.start()
        threads.append(t)

    # 进度显示
    while any(t.is_alive() for t in threads):
        done = sum(os.path.getsize(f"{part}.chunk-{i}")
                   for i in range(n_chunks)
                   if os.path.exists(f"{part}.chunk-{i}"))
        log(f"    {done/1024//1024}MB/{total_size/1024//1024}MB "
            f"({done*100//max(1, total_size)}%)  " +
            " ".join(f"c{i}:{state.get(str(i), '…')}" for i in range(n_chunks)
                     if str(i) in state))
        time.sleep(6)
    for t in threads:
        t.join()

    # 校验并拼装
    # ⚠️ 不要在这里删 chunk 文件！本机安全 shim 会在进程内第 ~50 次 os.remove
    # 时抛 BaseException 级别的拦截（except Exception 接不住，整个脚本直接死）。
    # 残留的 .chunk-N 文件完全无害：打包只认 *.whl；同名文件重跑时它们就是
    # 现成的断点续传缓存（chunk 完整 → 跳过该块 → 直接拼装，幂等）。
    expect = 0
    with open(part, "wb") as out:
        for i, (a, b) in enumerate(bounds):
            cp = f"{part}.chunk-{i}"
            if not os.path.exists(cp):
                return False
            expect += os.path.getsize(cp)
            with open(cp, "rb") as f:
                out.write(f.read())
    if expect != total_size:
        log(f"    拼装大小不符 {expect} != {total_size}")
        return False
    os.replace(part, dest)
    return True


def simple_download(url, dest):
    for name, fn, use_proxy in SOURCES_FN:
        src = fn(url)
        try:
            s = make_session(use_proxy)
            with s.get(src, stream=True, timeout=(15, 90)) as r:
                if r.status_code != 200:
                    log(f"    [{name}] HTTP {r.status_code}")
                    continue
                with open(dest + ".part", "wb") as f:
                    for chunk in r.iter_content(1024 * 128):
                        f.write(chunk)
            os.replace(dest + ".part", dest)
            return True
        except Exception as e:
            log(f"    [{name}] {type(e).__name__}: {str(e)[:70]}")
    return False


def main():
    os.makedirs(WHEELS, exist_ok=True)
    report = json.load(open(REPORT, encoding="utf-8"))
    items = [(os.path.basename(p["download_info"]["url"]), p["download_info"]["url"])
             for p in report.get("install", [])
             if p.get("download_info", {}).get("url", "").endswith(".whl")]

    todo = [(f, u) for f, u in items
            if not os.path.exists(os.path.join(WHEELS, f))]
    log(f"待下载 {len(todo)}/{len(items)} 个")

    failed = []
    for i, (fname, url) in enumerate(todo, 1):
        dest = os.path.join(WHEELS, fname)
        log(f"({i}/{len(todo)}) {fname[:66]}")
        # 先 HEAD 拿大小
        s = working_session()
        total = 0
        try:
            h = s.head(url, timeout=(15, 30), allow_redirects=True)
            total = int(h.headers.get("content-length", 0))
        except Exception:
            pass
        ok = False
        if total > CHUNK_MIN:
            log(f"    并行分块 {CHUNK_THREADS} 连接，共 {total/1024/1024:.1f}MB")
            ok = parallel_download(url, dest, total)
            if not ok:
                log("    并行失败，退化为单源")
                ok = simple_download(url, dest)
        else:
            ok = simple_download(url, dest)
        if ok and os.path.exists(dest):
            log(f"    ✓ {fname} {os.path.getsize(dest)/1024/1024:.1f}MB")
        else:
            failed.append(fname)

    if failed:
        log(f"失败: {failed}")
        print("PARALLEL_FAILED")
        return 1
    print("PARALLEL_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
