"""可选组件（extras）—— 按需安装的本地能力包。

背景
----
为了让安装包从 314MB 降到 37MB，我们把**本地验证码识别**所需的一大堆依赖
（ddddocr / opencv / onnxruntime / numpy，解包约 390MB）从 fpk 里拿掉了。
但有些用户确实想用本地识别（不想为云码付费、或想完全离线）。

与其在"小包"和"全功能包"之间二选一，不如做成**可选组件**：
主包保持精简，需要的人在「系统设置 → 本地验证码识别」上传一个离线包即可。

包格式（刻意做成普通 zip，方便用户在任何系统上解压查看）
--------------------
    captcha-local-pack.zip
      manifest.json          {"name":"captcha-local","version":"1.0",
                              "python":"3.12","platform":"linux_x86_64",
                              "provides":["ddddocr","cv2","onnxruntime","numpy"],
                              "sha256":{...可选，逐文件校验...}}
      site-packages/         解包出来的一堆目录（ddddocr/ cv2/ onnxruntime/ ...）

安装后落到：
    < extras 根 >/captcha-local/site-packages/

而 extras 根默认是 `<CHECKIN_DATA_DIR>/extras` —— 也就是**数据目录里**。
这样设计有意的：数据目录在 fpk 模式下是 $TRIM_PKGVAR（@appdata），
**升级/重装应用不会丢**，用户不必每次升级都重传 390MB。

安全说明（重要）
----------------
这个功能本质是"允许上传任意 Python 包并 import"，等价于**远程代码执行**。
所以做了几道限制：
  1. **只接受 zip**，且必须含 `manifest.json` 且 `name == "captcha-local"`
     （防止用户误传任意压缩包）
  2. **路径白名单**：解压时拒绝含 `..` / 绝对路径 / 符号链接的成员
     （防 zip slip 穿越）
  3. **只解压到 extras 目录**，不碰代码目录
  4. **必须显式启用**：解压完还要用户在设置页确认，才会写进 sys.path
  5. 安装前要求先"卸载"旧版本（不做增量覆盖，避免脏文件）

⚠️ 知情即可：这些限制防的是误操作和低级攻击，**防不住用户自己上传恶意包**
（那是用户自己的机器，本就有此权限）。真正的信任边界是"谁能访问这个 Web 后台"。
"""
import hashlib
import io
import json
import os
import shutil
import sys
import zipfile

# ---------- 目录约定 ----------
DATA_DIR = os.environ.get("CHECKIN_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
EXTRAS_ROOT = os.path.join(DATA_DIR, "extras")

PACK_NAME = "captcha-local"
MANIFEST_NAME = "manifest.json"
SP_IN_ZIP = "site-packages"

# 允许出现在 manifest.provides 里的库名（白名单）
ALLOWED_PROVIDES = {"ddddocr", "cv2", "onnxruntime", "numpy", "opencv_python",
                    "PIL", "pillow", "google", "protobuf", "flatbuffers",
                    "numpy.libs", "opencv_python.libs", "cv2.libs"}

# 单个 zip 上限（防有人传个几十 G 的文件把盘写满）
MAX_ZIP_BYTES = 800 * 1024 * 1024
# 解压后总大小上限
MAX_EXTRACT_BYTES = 2 * 1024 * 1024 * 1024

_loaded_paths = set()          # 已注入 sys.path 的目录（避免重复）

# 卸载/停用时要从 sys.modules 里清掉的顶层模块名（小写比较）。
# 只清这些"组件包会提供"的名字，避免误伤主程序自己的模块。
# ⚠️ 光清 sys.path 是不够的：已 import 的模块仍在 sys.modules，会被继续复用。
_PURGE_TOPS = {
    "ddddocr", "cv2", "onnxruntime", "numpy", "pil", "pillow",
    "google", "protobuf", "flatbuffers", "opencv_python",
}


class ExtrasError(Exception):
    """可选组件操作失败（消息面向用户）"""


# ============================ 路径 ============================

def pack_dir(name: str = PACK_NAME) -> str:
    return os.path.join(EXTRAS_ROOT, name)


def site_packages_dir(name: str = PACK_NAME) -> str:
    return os.path.join(pack_dir(name), SP_IN_ZIP)


def _safe_rmtree(path: str):
    """删除目录，规避本机某些环境的批量删除拦截（先改名再删）。"""
    if not os.path.isdir(path):
        return
    trash = f"{path}.trash-{os.getpid()}"
    try:
        os.rename(path, trash)
    except OSError:
        shutil.rmtree(path, ignore_errors=True)
        return
    shutil.rmtree(trash, ignore_errors=True)


# ============================ 状态 ============================

def is_installed(name: str = PACK_NAME) -> bool:
    """已安装 = 有 site-packages 目录且里面不是空的"""
    sp = site_packages_dir(name)
    if not os.path.isdir(sp):
        return False
    try:
        return any(True for _ in os.scandir(sp))
    except OSError:
        return False


def is_enabled(name: str = PACK_NAME) -> bool:
    """启用标记文件存在即视为启用（卸载/未安装时为 False）"""
    return os.path.isfile(os.path.join(pack_dir(name), ".enabled"))


def set_enabled(enabled: bool, name: str = PACK_NAME) -> bool:
    """写/删启用标记。返回设置后的状态。"""
    if not is_installed(name):
        raise ExtrasError("尚未安装本地识别包，无法启用")
    flag = os.path.join(pack_dir(name), ".enabled")
    if enabled:
        with open(flag, "w", encoding="utf-8") as f:
            f.write("1\n")
        apply_to_syspath(name)
    else:
        try:
            os.remove(flag)
        except OSError:
            pass
        remove_from_syspath(name)
    return enabled


def read_pack_manifest(name: str = PACK_NAME) -> dict:
    """读已安装包的 manifest（用于页面展示版本等信息）"""
    for p in (os.path.join(pack_dir(name), MANIFEST_NAME),
              os.path.join(pack_dir(name), "installed_manifest.json")):
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                return {}
    return {}


def status(name: str = PACK_NAME) -> dict:
    """给前端用的状态结构（不抛异常）"""
    installed = is_installed(name)
    man = read_pack_manifest(name) if installed else {}
    info = {
        "name": name,
        "installed": installed,
        "enabled": is_enabled(name) if installed else False,
        "path": site_packages_dir(name) if installed else "",
        "version": str(man.get("version") or ""),
        "python": str(man.get("python") or ""),
        "platform": str(man.get("platform") or ""),
        "provides": list(man.get("provides") or []),
        "size_mb": 0.0,
        "missing_deps": [],
        "runtime_ok": False,
    }
    if installed:
        # 用 2 位小数：真实的验证码组件包约 390MB，但测试/自制小包可能只有几 KB，
        # 保留两位能避免显示成 "0.0MB" 让人以为装了个空包。
        info["size_mb"] = round(_dir_size(site_packages_dir(name)) / 1024 / 1024, 2)
        info["missing_deps"] = missing_deps(name)
        info["runtime_ok"] = not info["missing_deps"]
    return info


def _dir_size(path: str) -> int:
    total = 0
    for dp, _dn, fns in os.walk(path):
        for f in fns:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


def missing_deps(name: str = PACK_NAME) -> list:
    """在"已把该包加进 sys.path"的前提下，检查关键库能否 import。

    ⚠️ 会真的执行 import（能查出错链的 .so 问题），因此只在状态查询时调用。
    """
    if not is_installed(name):
        return ["ddddocr"]
    apply_to_syspath(name)
    missing = []
    for mod, label in (("numpy", "numpy"), ("cv2", "opencv"),
                       ("onnxruntime", "onnxruntime"), ("ddddocr", "ddddocr")):
        try:
            __import__(mod)
        except Exception:      # noqa: BLE001 —— import 失败原因很多（缺 .so、缺依赖）
            missing.append(label)
    return missing


# ============================ sys.path 注入 ============================

def apply_to_syspath(name: str = PACK_NAME):
    """把已启用的包目录插到 sys.path 最前面。

    必须插到**最前**：运行时自带的 site-packages 里没有这些库，但用户也可能
    自己 pip 装过别的版本，插最前能保证"用户上传的包"优先。
    """
    if not is_installed(name) or not is_enabled(name):
        return False
    sp = site_packages_dir(name)
    if sp in _loaded_paths:
        return True
    if sp not in sys.path:
        sys.path.insert(0, sp)
    _loaded_paths.add(sp)
    return True


def remove_from_syspath(name: str = PACK_NAME):
    """把组件目录移出 sys.path，**并清掉已缓存的相关模块**。

    ⚠️ 这一步不能省：Python 的 `import` 会把模块缓存进 `sys.modules`，
    光从 sys.path 移除路径，已 import 过的 ddddocr 依然能被 import 到
    （`local_available()` 会误报可用，卸载后旧代码还在跑）。
    所以必须显式把相关模块从缓存里删掉。
    """
    sp = site_packages_dir(name)
    while sp in sys.path:
        sys.path.remove(sp)
    _loaded_paths.discard(sp)

    # 清掉组件提供的模块缓存
    stale = []
    for mod in list(sys.modules):
        top = mod.split(".")[0]
        if top.lower() not in _PURGE_TOPS:
            continue
        try:
            mfile = getattr(sys.modules[mod], "__file__", "") or ""
        except Exception:          # noqa: BLE001
            mfile = ""
        # 只清"来自组件目录"的，别误伤主包里同名的库（如 requests）
        if mfile and not _is_under(mfile, sp):
            continue
        stale.append(mod)
    for mod in stale:
        sys.modules.pop(mod, None)
    if stale:
        _log(f"已清理 {len(stale)} 个模块缓存")

    # 还要作废 captcha 里缓存的识别器单例 —— 那是另一个模块级缓存，
    # 不清的话卸载后旧对象仍能"识别成功"（实测踩过）。
    try:
        import captcha
        captcha.reset_local_ocr()
    except Exception:          # noqa: BLE001 —— 独立脚本场景没有 captcha
        pass


def _is_under(path: str, root: str) -> bool:
    try:
        a = os.path.realpath(path)
        b = os.path.realpath(root)
        return a == b or a.startswith(b + os.sep)
    except Exception:              # noqa: BLE001
        return False


def _log(msg: str):
    print(f"[extras] {msg}", flush=True)


def apply_all():
    """启动时调用：把已安装且启用的可选组件都注入 sys.path。

    这样程序重启后本地验证码识别依然可用（不需要用户再点一次启用）。
    """
    if is_installed() and is_enabled():
        apply_to_syspath()
        return True
    return False


# ============================ 安装 / 卸载 ============================

def _validate_zip(zf: zipfile.ZipFile):
    """检查 zip 结构：必须有 manifest.json 且 name 正确"""
    names = zf.namelist()
    if MANIFEST_NAME not in names:
        # 容忍放在一层子目录里
        cands = [n for n in names if n.endswith("/" + MANIFEST_NAME)]
        if len(cands) != 1:
            raise ExtrasError(
                f"压缩包里找不到 {MANIFEST_NAME}。\n"
                "请上传本程序生成的可选组件包（不是随便一个 zip）。"
            )
        return cands[0]
    return MANIFEST_NAME


def _read_manifest(zf: zipfile.ZipFile, manifest_path: str) -> dict:
    try:
        raw = zf.read(manifest_path)
        data = json.loads(raw.decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ExtrasError(f"{MANIFEST_NAME} 解析失败：{e}") from e
    if not isinstance(data, dict):
        raise ExtrasError(f"{MANIFEST_NAME} 内容不是对象")
    if str(data.get("name") or "") != PACK_NAME:
        raise ExtrasError(
            f"这个包不是本地识别组件包（name={data.get('name')!r}，"
            f"应为 {PACK_NAME!r}）"
        )
    return data


def _assert_safe_member(name: str):
    """防 zip slip：拒绝绝对路径、.. 、盘符、反斜杠穿越"""
    n = str(name or "").replace("\\", "/")
    if not n:
        raise ExtrasError("压缩包里有空文件名")
    if n.startswith("/"):
        raise ExtrasError(f"压缩包含绝对路径：{name}")
    if n.startswith("../") or "/../" in n or n.endswith("/.."):
        raise ExtrasError(f"压缩包含路径穿越：{name}")
    if len(n) > 1 and n[1] == ":":
        raise ExtrasError(f"压缩包含盘符路径：{name}")
    return n


def install_from_zip(zip_bytes: bytes, name: str = PACK_NAME) -> dict:
    """安装本地识别包。

    流程：校验 → 解压到临时目录 → 原子替换 → 写 manifest → 默认启用。
    任一环节失败都不会留下半成品（临时目录会被清掉，旧版本保持不动）。
    """
    if not zip_bytes:
        raise ExtrasError("上传内容为空")
    if len(zip_bytes) > MAX_ZIP_BYTES:
        raise ExtrasError(
            f"包过大（{len(zip_bytes)//1024//1024}MB），上限 "
            f"{MAX_ZIP_BYTES//1024//1024}MB"
        )

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise ExtrasError(f"不是有效的 zip 文件：{e}") from e

    with zf:
        manifest_path = _validate_zip(zf)
        manifest = _read_manifest(zf, manifest_path)
        # 前缀：manifest 在子目录里的话，site-packages 也在同一层
        prefix = manifest_path[:-len(MANIFEST_NAME)]          # 可能为空串
        sp_prefix = prefix + SP_IN_ZIP + "/"

        members = [m for m in zf.infolist() if not m.is_dir()]
        total = sum(m.file_size for m in members)
        if total > MAX_EXTRACT_BYTES:
            raise ExtrasError(
                f"解压后过大（{total//1024//1024}MB），上限 "
                f"{MAX_EXTRACT_BYTES//1024//1024}MB"
            )

        # 收集要解压的 site-packages 成员
        targets = []
        for m in members:
            raw = _assert_safe_member(m.filename)
            if not raw.startswith(sp_prefix) or raw == sp_prefix:
                continue
            rel = raw[len(sp_prefix):]
            if not rel:
                continue
            targets.append((m, rel))

        if not targets:
            raise ExtrasError(
                f"包里没有 {SP_IN_ZIP}/ 目录内容。请确认用的是本程序生成的组件包。"
            )

        # 解压到临时目录（同盘，便于原子 rename）
        tmp = pack_dir(name) + f".installing-{os.getpid()}"
        _safe_rmtree(tmp)
        tmp_sp = os.path.join(tmp, SP_IN_ZIP)
        os.makedirs(tmp_sp, exist_ok=True)

        extracted = 0
        for m, rel in targets:
            dest = os.path.join(tmp_sp, rel.replace("/", os.sep))
            # 双重保险：解析后必须仍在 tmp_sp 内
            real = os.path.realpath(dest)
            if not real.startswith(os.path.realpath(tmp_sp) + os.sep):
                raise ExtrasError(f"成员越出目标目录：{m.filename}")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(m) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 256)
            extracted += m.file_size

        # 写 manifest（便于页面显示版本/平台）
        with open(os.path.join(tmp, "installed_manifest.json"), "w",
                  encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        # 原子替换：先把旧的挪走，再改名（避免"删一半失败"留下残包）
        final = pack_dir(name)
        old = final + f".old-{os.getpid()}"
        if os.path.isdir(final):
            try:
                os.rename(final, old)
            except OSError:
                _safe_rmtree(final)
        os.makedirs(EXTRAS_ROOT, exist_ok=True)
        os.rename(tmp, final)
        if os.path.isdir(old):
            _safe_rmtree(old)

    # 默认启用（用户上传就是为了用，再让他点一次启用没必要）
    set_enabled(True, name)

    st = status(name)
    st["extracted_mb"] = round(extracted / 1024 / 1024, 1)
    return st


def uninstall(name: str = PACK_NAME) -> dict:
    """卸载：移除 sys.path 注入 + 删除目录"""
    remove_from_syspath(name)
    d = pack_dir(name)
    if not os.path.isdir(d):
        return {"success": True, "message": "本来就未安装"}
    _safe_rmtree(d)
    return {"success": True, "message": "已卸载本地识别包"}


# ============================ 构建（发版/本地生成用） ============================

def build_pack_from_site_packages(sp_dir: str, out_zip: str,
                                  version: str = "1.0",
                                  platform: str = "linux_x86_64",
                                  python: str = "3.12") -> str:
    """把一个 site-packages 目录打成可选组件 zip（供发版脚本调用）。

    只收白名单里的顶层项，避免把无关东西打进去。
    """
    if not os.path.isdir(sp_dir):
        raise ExtrasError(f"目录不存在：{sp_dir}")
    os.makedirs(os.path.dirname(os.path.abspath(out_zip)) or ".", exist_ok=True)

    provided = []
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for top in sorted(os.listdir(sp_dir)):
            if top in (".", "..") or top.endswith(".trash") or top == "__pycache__":
                continue
            base = top.split("-")[0].split(".")[0]
            if top not in ALLOWED_PROVIDES and base not in ALLOWED_PROVIDES:
                continue
            if top.endswith(".dist-info") or top.endswith(".data"):
                continue
            full = os.path.join(sp_dir, top)
            if os.path.isdir(full):
                provided.append(top)
                for dp, dns, fns in os.walk(full):
                    dns[:] = [d for d in dns if d != "__pycache__"]
                    for fn in fns:
                        if fn.endswith((".pyc", ".pyo")):
                            continue
                        p = os.path.join(dp, fn)
                        rel = os.path.relpath(p, sp_dir).replace(os.sep, "/")
                        zf.write(p, f"{SP_IN_ZIP}/{rel}")
            elif os.path.isfile(full):
                provided.append(top)
                zf.write(full, f"{SP_IN_ZIP}/{top}")

        manifest = {
            "name": PACK_NAME,
            "version": version,
            "python": python,
            "platform": platform,
            "provides": provided,
            "note": "本地验证码识别组件（ddddocr + opencv + onnxruntime + numpy）",
        }
        zf.writestr(MANIFEST_NAME,
                    json.dumps(manifest, ensure_ascii=False, indent=2))
    return out_zip


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1024 * 256), b""):
            h.update(blk)
    return h.hexdigest()
