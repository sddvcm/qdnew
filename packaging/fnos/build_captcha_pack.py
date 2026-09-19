"""构建「本地验证码识别」可选组件包（captcha-local-pack-<ver>.zip）。

背景
----
主安装包（fpk/docker）为了体积只装核心依赖。本地识别所需的
ddddocr / opencv / onnxruntime / numpy（解包约 390MB）单独打成这个包，
用户在「系统设置 → 本地验证码识别」上传即可开通。

为什么要单独构建而不是复用 make_runtime_tgz 的产物
--------------------------------------------------
runtime.tar 里同时混着解释器和全部依赖，还做过 ELF strip（那是对解释器做的）。
组件包只需要**纯 site-packages**，且必须能被 pip/import 正常工作 ——
所以这里直接解压原始 manylinux wheel，不做任何二进制改动。

产物可直接被 app/extras.py 的 install_from_zip() 安装。

用法：
    python packaging/fnos/build_captcha_pack.py [版本号]

前置：
    build/wheels/ 下已有验证码相关 wheel（由 prepare_runtime/fetch_wheels 拉取）。
    若缺，会明确提示先跑 fetch_wheels.py。
"""
import io
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
WHEEL_DIR = os.path.join(BUILD, "wheels")
DIST = os.path.join(HERE, "dist")
SP_IN_ZIP = "site-packages"
PACK_NAME = "captcha-local"

# 只打这些前缀的 wheel（本地识别所需的那几个大件）
#
# ⚠️ 必须包含 pillow：ddddocr 内部 `from PIL import Image`，虽然主运行时也内置了
# pillow，但组件包**要能独立工作** —— 不依赖"主包里恰好有哪个版本"。
# 版本不一致时 PIL 的 C 扩展（_imaging.so）与 Python 层混用会出诡异错误。
CAPTCHA_WHEELS = (
    "ddddocr-", "opencv_python-", "opencv-", "onnxruntime-",
    "numpy-", "protobuf-", "flatbuffers-", "google-",
    "pillow-",
)
# 运行时已内置、不需要重复打包的（避免包内出现两份不同版本）
SKIP_TOP = {"numpy.distutils"}


def log(msg):
    print(f"[captcha-pack] {msg}", flush=True)


def main():
    version = (sys.argv[1] if len(sys.argv) > 1 else "1.0").strip() or "1.0"
    if not os.path.isdir(WHEEL_DIR):
        raise SystemExit(f"缺少 {WHEEL_DIR}，请先跑 fetch_wheels.py / prepare_runtime.py")

    wheels = sorted(f for f in os.listdir(WHEEL_DIR)
                    if f.endswith(".whl")
                    and any(f.lower().startswith(p) for p in CAPTCHA_WHEELS))
    if not wheels:
        raise SystemExit(
            "build/wheels 下没有验证码相关 wheel。\n"
            "请先跑 packaging/fnos/fetch_wheels.py（当前 requirements-captcha.txt "
            "含 ddddocr）。"
        )

    log(f"版本 {version}，共 {len(wheels)} 个 wheel")
    os.makedirs(DIST, exist_ok=True)
    out_zip = os.path.join(DIST, f"{PACK_NAME}-pack-{version}.zip")

    provided = set()
    n_files = 0
    n_bytes = 0

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for w in wheels:
            wpath = os.path.join(WHEEL_DIR, w)
            log(f"  + {w}  {os.path.getsize(wpath)/1024/1024:.1f}MB")
            with zipfile.ZipFile(wpath) as zw:
                for info in zw.infolist():
                    if info.is_dir():
                        continue
                    rel = info.filename
                    # 丢掉 .data / dist-info / 编译缓存 —— 运行不需要
                    if rel.endswith(".data/") or "/.data/" in rel:
                        continue
                    top = rel.split("/")[0]
                    if top.endswith(".dist-info") or top.endswith(".data"):
                        continue
                    if top in SKIP_TOP:
                        continue
                    if rel.endswith((".pyc", ".pyo")):
                        continue
                    if "__pycache__" in rel:
                        continue
                    provided.add(top.split(".")[0])
                    data = zw.read(info)
                    out.writestr(f"{SP_IN_ZIP}/{rel}", data)
                    n_files += 1
                    n_bytes += len(data)

        manifest = {
            "name": PACK_NAME,
            "version": version,
            "python": "3.12",
            "platform": "linux_x86_64",
            "provides": sorted(provided),
            "files": n_files,
            "note": "本地验证码识别组件：ddddocr + OpenCV + ONNX Runtime + NumPy",
        }
        out.writestr("manifest.json",
                     json.dumps(manifest, ensure_ascii=False, indent=2))

    size_mb = os.path.getsize(out_zip) / 1024 / 1024
    log(f"文件数 {n_files}，解压后 {n_bytes/1024/1024:.0f}MB")
    log(f"✓ 输出 {out_zip}  {size_mb:.1f}MB")
    log(f"  provides = {sorted(provided)}")

    # 自校验：能否被 extras 正常安装
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
        os.environ.setdefault("CHECKIN_DATA_DIR",
                              os.path.join(BUILD, "_selftest_data"))
        from app import extras
        st = extras.install_from_zip(open(out_zip, "rb").read())
        log(f"✓ 自校验：可安装（version={st.get('version')}, "
            f"{st.get('size_mb')}MB, missing={st.get('missing_deps')}）")
        extras.uninstall()
    except Exception as e:      # noqa: BLE001
        log(f"⚠️ 自校验安装失败：{e}")

    print("CAPTCHA_PACK_OK")


if __name__ == "__main__":
    main()
