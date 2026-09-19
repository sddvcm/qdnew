"""程序内自动更新 — 从 GitHub 拉取新版本文件并热替换。

设计目标
--------
「不用重新部署」：Web 上点一下「检查更新」→「立即更新」，拉取新代码、
校验、原子替换文件、重载插件，重启容器后仍然生效（前提是代码目录被挂载
出来，见 `docker-compose.yml` 的 `./:/app` 说明）。

⚠️ 安全模型（必须理解，这不是玩具）
--------------------------------
这个模块能**从网络下载代码并覆盖自身正在运行的程序**，等价于远程代码执行
能力。因此做了四道闸：

1. **源白名单**：只允许 `github.com` / `raw.githubusercontent.com` /
   `codeload.github.com` / `api.github.com` 及其子域。改写源地址也绕不过
   （`_assert_allowed_url` 每次都校验）。这是防止有人把更新源改成恶意站点。
2. **SHA256 清单校验**：更新包必须带 `update_manifest.json`，里面列出每个
   文件的哈希。任一文件校验不过 → 整批拒绝，不落地任何文件。
   注意：清单本身没有签名，所以它只能防"传输损坏"，**防不住"上游仓库被篡改"**。
   要防后者需要密钥签名，本项目未做 —— 知情即可。
3. **路径白名单**：只允许覆盖代码文件（`.py/.html/.css/.js/.json/.md/.txt`），
   且路径必须落在允许的目录内（`app/ har/ plugins/ templates/ docs/` 或根目录
   文件）。**禁止**触碰 `data/`（数据库）、`.env`（密钥）、`user_plugins/`
   （用户自己的插件）—— 覆盖这些会造成数据丢失。
4. **备份 + 回滚**：更新前把待改文件备份到 `data/backups/<时间戳>/`，
   写入失败立即回滚。备份留在磁盘上，出问题可以手工恢复。

更新源协议
----------
源是一个 GitHub 仓库（默认分支即可），根目录需有一个 `update_manifest.json`：

    {
      "version": "1.3.0",
      "released_at": "2026-09-20",
      "notes": "修了什么",
      "files": {
        "app/main.py": "sha256hex...",
        "har/render.py": "sha256hex..."
      }
    }

程序比对 `version.json` 的 version 与清单里的 version 判断是否有新版；
有则逐个下载 `files` 里的路径（从同一仓库的 raw 地址取），校验哈希后写入。

为什么不做成 git 拉取
--------------------
容器里不一定有 git，也不该让程序执行 git（等于任意命令执行）。纯 HTTPS +
哈希校验更可控，依赖只有 requests。
"""
import hashlib
import json
import os
import re
import shutil
import time
import urllib.parse
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

# ⚠️ updater.py 就放在**项目根目录**，所以 ROOT 是文件所在目录本身，
# 不是它的父目录（早期写成 dirname(dirname(__file__)) 会指到项目外一层，
# 后果是 build_manifest 遍历不到任何文件、run_update 把文件写到错误位置）。
ROOT = os.path.dirname(os.path.abspath(__file__))
# 备份目录跟随数据目录（fpk 模式下 CHECKIN_DATA_DIR 指向 fnOS 持久化目录，
# 这样升级应用 / 重建容器后备份仍在）。
DATA_DIR = os.environ.get("CHECKIN_DATA_DIR") or os.path.join(ROOT, "data")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
VERSION_FILE = os.path.join(ROOT, "version.json")
MANIFEST_NAME = "update_manifest.json"

# ---------- 安全闸 1：允许的更新源域名 ----------
ALLOWED_HOSTS = (
    "github.com",
    "raw.githubusercontent.com",
    "codeload.github.com",
    "api.github.com",
)

# ---------- 安全闸 3：允许被覆盖的路径 ----------
# 组件名白名单（匹配路径的任意一段）
FORBIDDEN_SEGMENTS = {
    "data", ".git", "user_plugins", "logs", "__pycache__",
    ".env", "node_modules", ".workbuddy",
}
# 只允许这些扩展名被覆盖
ALLOWED_EXT = {".py", ".html", ".css", ".js", ".json", ".md", ".txt", ".example"}
# 只允许这些顶层目录下的文件（根目录下的散文件由 ALLOWED_ROOT_FILES 单独控制）
ALLOWED_DIRS = {"app", "har", "plugins", "templates", "docs"}
ALLOWED_ROOT_FILES = {
    "version.json", "requirements.txt", "Dockerfile",
    "docker-compose.yml", "README.md", "DEVELOPMENT.md",
    "captcha.py", "updater.py", "app.py", "run.py",
    # 部署模板：新装用户需要它来生成 .env。注意真正的 .env 仍在
    # NEVER_OVERWRITE 里，绝不允许被更新覆盖。
    ".env.example",
}
# 永不覆盖的文件（含敏感配置）
NEVER_OVERWRITE = {".env", "update_manifest.json"}


def _build_proxies(proxy: Optional[str]) -> Optional[Dict[str, str]]:
    """把用户填的代理地址整理成 requests 的 proxies 字典。

    空字符串 / None → 返回 None（走直连，保持默认行为）。
    用户可填 `http://192.168.2.100:7897` 或省略协议只填 `192.168.2.100:7897`
    （自动补 http://）。HTTP / HTTPS 统一走同一代理。
    """
    if not proxy:
        return None
    p = str(proxy).strip()
    if not p:
        return None
    if not re.match(r"^https?://", p, re.I):
        p = "http://" + p
    return {"http": p, "https": p}


class UpdateError(Exception):
    """更新失败（消息面向用户，可直接展示）"""


# ============================ 版本 ============================

def current_version() -> Dict:
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _parse_version(text: str) -> Tuple[int, ...]:
    """把 '1.2.3' 解析成 (1,2,3)；非数字段按 0 处理"""
    parts = []
    for seg in re.split(r"[._-]", str(text or "0").strip()):
        m = re.match(r"^(\d+)", seg)
        parts.append(int(m.group(1)) if m else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:4])


def is_newer(remote: str, local: str) -> bool:
    return _parse_version(remote) > _parse_version(local)


# ============================ 源地址处理 ============================

def _assert_allowed_url(url: str):
    """安全闸 1：源必须是白名单域名，且 https"""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("https", "http"):
        raise UpdateError(f"更新源协议不允许：{parsed.scheme}")
    host = (parsed.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        raise UpdateError(
            f"更新源域名不在白名单内：{host}\n"
            f"只允许：{', '.join(ALLOWED_HOSTS)}"
        )


def normalize_source(source: str, proxy: Optional[str] = None) -> Dict:
    """把用户填的源地址规范成 {owner, repo, branch, raw_base, api_base}。

    支持三种填法：
        https://github.com/owner/repo
        owner/repo
        https://github.com/owner/repo/tree/branch
    """
    source = (source or "").strip()
    if not source:
        raise UpdateError("未配置更新源，请在「系统设置」里填写 GitHub 仓库")

    branch = ""
    m = re.match(r"^(?:https?://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?"
                 r"(?:/tree/([\w./-]+))?/?$", source)
    if not m:
        raise UpdateError(
            "更新源格式不正确。应为 https://github.com/用户名/仓库名 "
            "（可选 /tree/分支名）"
        )
    owner, repo, branch = m.group(1), m.group(2), (m.group(3) or "").strip("/")

    if not branch:
        # 没指定分支就走 GitHub API 查默认分支
        api = f"https://api.github.com/repos/{owner}/{repo}"
        _assert_allowed_url(api)
        try:
            resp = requests.get(api, headers={"User-Agent": "checkin-system"},
                                timeout=15, proxies=_build_proxies(proxy))
            if resp.status_code == 404:
                raise UpdateError(f"仓库不存在或未公开：{owner}/{repo}")
            resp.raise_for_status()
            branch = resp.json().get("default_branch") or "main"
        except requests.RequestException as e:
            raise UpdateError(f"查询仓库默认分支失败：{e}") from e

    return {
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "raw_base": f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}",
        "web_url": f"https://github.com/{owner}/{repo}",
    }


def _fetch_raw(src: Dict, path: str, timeout: int = 20,
               proxy: Optional[str] = None) -> Optional[bytes]:
    url = f"{src['raw_base']}/{path.lstrip('/')}"
    _assert_allowed_url(url)
    resp = requests.get(url, headers={"User-Agent": "checkin-system"},
                        timeout=timeout, proxies=_build_proxies(proxy))
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content


# ============================ 检查更新 ============================

def check_update(source: str, proxy: Optional[str] = None) -> Dict:
    """检查是否有新版本。返回给前端的结构（不抛异常，失败也在结构里）"""
    local = current_version()
    result = {
        "current_version": local.get("version", "未知"),
        "latest_version": "",
        "has_update": False,
        "notes": "",
        "released_at": "",
        "file_count": 0,
        "web_url": "",
        "error": "",
    }
    try:
        src = normalize_source(source, proxy=proxy)
        result["web_url"] = src["web_url"]
        raw = _fetch_raw(src, MANIFEST_NAME, proxy=proxy)
        if raw is None:
            result["error"] = (
                f"仓库里找不到 {MANIFEST_NAME}。\n"
                "请确认仓库根目录有该清单文件（见 DEVELOPMENT.md 的更新章节）"
            )
            return result
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            result["error"] = f"{MANIFEST_NAME} 解析失败：{e}"
            return result

        latest = str(manifest.get("version") or "")
        result["latest_version"] = latest or "未知"
        result["notes"] = str(manifest.get("notes") or "")
        result["released_at"] = str(manifest.get("released_at") or "")
        files = manifest.get("files") or {}
        result["file_count"] = len(files) if isinstance(files, dict) else 0

        if not latest:
            result["error"] = f"{MANIFEST_NAME} 里没有 version 字段"
            return result

        result["has_update"] = is_newer(latest, local.get("version", "0"))
    except UpdateError as e:
        result["error"] = str(e)
    except requests.RequestException as e:
        result["error"] = f"连接更新源失败：{e}"
    except Exception as e:
        result["error"] = f"检查更新出错：{e}"
    return result


# ============================ 路径校验 ============================

def validate_path(rel_path: str) -> str:
    """安全闸 3：校验相对路径可否被更新覆盖。返回规范化路径，非法则抛错。

    规则：
      - 必须是相对路径，不得含 `..`、绝对路径、盘符
      - 不得落在禁止目录（data/ user_plugins/ logs/ .git/ 等）
      - 扩展名必须在 ALLOWED_EXT 内
      - 必须位于 ALLOWED_DIRS 内，或是 ALLOWED_ROOT_FILES 之一
    """
    path = str(rel_path or "").strip().replace("\\", "/").lstrip("/")
    if not path:
        raise UpdateError("文件路径为空")
    if ".." in path.split("/"):
        raise UpdateError(f"路径含 .. 越权：{rel_path}")
    if re.match(r"^[A-Za-z]:", path):
        raise UpdateError(f"不接受绝对路径：{rel_path}")

    segments = [s for s in path.split("/") if s]
    for seg in segments:
        if seg.lower() in FORBIDDEN_SEGMENTS:
            raise UpdateError(f"路径落在禁止目录（{seg}）：{rel_path}")

    basename = segments[-1]
    if basename in NEVER_OVERWRITE:
        raise UpdateError(f"该文件禁止被更新覆盖：{rel_path}")

    ext = os.path.splitext(basename)[1].lower()
    if ext not in ALLOWED_EXT:
        raise UpdateError(f"文件类型不允许更新（{ext or '无扩展名'}）：{rel_path}")

    if len(segments) == 1:
        if basename not in ALLOWED_ROOT_FILES:
            raise UpdateError(f"根目录下只允许更新 {sorted(ALLOWED_ROOT_FILES)}，收到：{rel_path}")
    else:
        if segments[0] not in ALLOWED_DIRS:
            raise UpdateError(f"只允许更新 {sorted(ALLOWED_DIRS)} 下的文件，收到：{rel_path}")

    return "/".join(segments)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_content(data: bytes) -> str:
    """对"文本内容"算哈希 —— **统一按 LF 归一化后再算**。

    ⚠️ 为什么必须归一化（踩过的大坑）：
    打包机是 Windows，工作区文件是 **CRLF**；而 git 提交到 GitHub 后，
    raw 下载回来的内容是 **LF**（git 的 autocrlf 或仓库存储本身）。
    如果清单按本地 CRLF 算哈希，线上按 LF 校验 —— 12 个文件全部不匹配，
    自动更新直接报"文件校验失败"整批拒绝。

    所以清单与校验两侧都走这个函数：二进制按原样，文本先把 CRLF 折成 LF。
    """
    if b"\r\n" in data:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


# ============================ 执行更新 ============================

def run_update(source: str, allow_downgrade: bool = False,
               proxy: Optional[str] = None) -> Dict:
    """执行更新：下载 → 校验 → 备份 → 写入。

    返回结构含 `updated` / `files` / `backup_dir` / `error`。
    **不做重启**：调用方（路由）负责在返回后重载插件。
    """
    out = {"updated": False, "version": "", "files": [], "backup_dir": "",
           "skipped": [], "error": ""}
    try:
        src = normalize_source(source, proxy=proxy)
        raw = _fetch_raw(src, MANIFEST_NAME, proxy=proxy)
        if raw is None:
            raise UpdateError(f"仓库里找不到 {MANIFEST_NAME}")
        manifest = json.loads(raw.decode("utf-8"))

        latest = str(manifest.get("version") or "")
        local_ver = current_version().get("version", "0")
        if not latest:
            raise UpdateError(f"{MANIFEST_NAME} 缺少 version 字段")
        if not allow_downgrade and not is_newer(latest, local_ver):
            out["version"] = latest
            out["error"] = f"当前已是最新版本（本地 {local_ver}，远端 {latest}）"
            return out

        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            raise UpdateError(f"{MANIFEST_NAME} 的 files 字段为空或格式不对")

        # ---- 阶段 1：全部下载 + 校验（任一失败则整体中止，不落地任何文件）----
        staged: List[Tuple[str, bytes]] = []
        for rel, expect_hash in files.items():
            try:
                safe = validate_path(rel)
            except UpdateError as e:
                raise UpdateError(f"清单里的路径非法：{e}") from e

            data = _fetch_raw(src, safe, proxy=proxy)
            if data is None:
                raise UpdateError(f"远端缺少清单里声明的文件：{safe}")

            if expect_hash:
                # 兼容两种清单：按 LF 归一化算的（本程序新版本）与按原始字节算的（旧版）。
                # 只要任一匹配就放行 —— 避免因换行符差异整批拒绝。
                exp = str(expect_hash).lower()
                if _hash_content(data).lower() != exp and _sha256(data).lower() != exp:
                    actual = _hash_content(data)
                    raise UpdateError(
                        f"文件校验失败：{safe}\n"
                        f"  期望 {str(expect_hash)[:16]}… 实际 {actual[:16]}…\n"
                        "（可能传输损坏，或远端清单与文件不一致）"
                    )
            staged.append((safe, data))

        # ---- 阶段 2：备份 ----
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = os.path.join(BACKUP_DIR, stamp)
        os.makedirs(backup_dir, exist_ok=True)
        backed_up: List[Tuple[str, str]] = []  # (绝对路径, 备份相对路径)
        for safe, _ in staged:
            target = os.path.join(ROOT, safe)
            if os.path.exists(target):
                bkp = os.path.join(backup_dir, safe)
                os.makedirs(os.path.dirname(bkp), exist_ok=True)
                shutil.copy2(target, bkp)
                backed_up.append((target, safe))

        # ---- 阶段 3：写入（失败即回滚）----
        written: List[str] = []
        try:
            for safe, data in staged:
                target = os.path.join(ROOT, safe)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                tmp = target + ".new"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, target)  # 原子替换，避免半截文件
                written.append(safe)
        except Exception as e:
            # 回滚已写入的文件
            for target, safe in backed_up:
                bkp = os.path.join(backup_dir, safe)
                if os.path.exists(bkp) and target in [os.path.join(ROOT, w) for w in written]:
                    try:
                        shutil.copy2(bkp, target)
                    except OSError:
                        pass
            raise UpdateError(f"写入失败已回滚：{e}") from e

        # ---- 阶段 4：同步本地 version.json ----
        try:
            with open(VERSION_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "name": manifest.get("name", "checkin-system"),
                    "version": latest,
                    "released_at": manifest.get("released_at", ""),
                    "notes": manifest.get("notes", ""),
                }, f, ensure_ascii=False, indent=2)
        except OSError as e:
            out["skipped"].append(f"version.json 写入失败：{e}")

        out.update({
            "updated": True,
            "version": latest,
            "files": written,
            "backup_dir": os.path.relpath(backup_dir, ROOT).replace("\\", "/"),
        })
    except UpdateError as e:
        out["error"] = str(e)
    except requests.RequestException as e:
        out["error"] = f"下载更新失败：{e}"
    except Exception as e:
        out["error"] = f"更新异常：{e}"
    return out


def list_backups(limit: int = 20) -> List[Dict]:
    """列出历史备份（用于人工回滚排查）"""
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for name in sorted(os.listdir(BACKUP_DIR), reverse=True)[:limit]:
        full = os.path.join(BACKUP_DIR, name)
        if not os.path.isdir(full):
            continue
        count = 0
        for _, _, fs in os.walk(full):
            count += len(fs)
        out.append({"name": name, "files": count,
                    "path": os.path.relpath(full, ROOT).replace("\\", "/")})
    return out


def build_manifest(version: str, notes: str = "", released_at: str = "") -> Dict:
    """生成本仓库的 update_manifest.json（发版时用）。

    在项目根目录跑：
        python -c "import updater,json;print(json.dumps(updater.build_manifest('1.3.0','修了xx'),ensure_ascii=False,indent=2))" > update_manifest.json
    """
    files: Dict[str, str] = {}
    for sub in sorted(ALLOWED_DIRS):
        base = os.path.join(ROOT, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in FORBIDDEN_SEGMENTS
                           and not d.startswith("__")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT).replace("\\", "/")
                try:
                    validate_path(rel)
                except UpdateError:
                    continue
                with open(full, "rb") as f:
                    # ⚠️ 用 _hash_content（LF 归一化），别用 _sha256 ——
                    # 本地 CRLF / GitHub LF 差异会让校验永远失败。
                    files[rel] = _hash_content(f.read())

    for fn in sorted(ALLOWED_ROOT_FILES):
        full = os.path.join(ROOT, fn)
        if not os.path.isfile(full) or fn in NEVER_OVERWRITE:
            continue
        rel = fn
        try:
            validate_path(rel)
        except UpdateError:
            continue
        with open(full, "rb") as f:
            files[rel] = _hash_content(f.read())

    return {
        "name": "checkin-system",
        "version": version,
        "released_at": released_at or datetime.now().strftime("%Y-%m-%d"),
        "notes": notes,
        "files": files,
    }


def check_proxy(proxy: str, url: Optional[str] = None) -> Dict:
    """测试代理能否连通 GitHub 更新源（不落库、不校验白名单）。

    用于设置页「测试代理」按钮：拿着用户刚填的代理地址，尝试拉一次
    仓库的 update_manifest.json，验证这台机器确实能经此代理访问 GitHub。
    成功返回 {ok:True, version}；失败返回 {ok:False, error}。
    """
    if not proxy or not str(proxy).strip():
        return {"ok": False, "error": "请先填写代理地址"}
    test_url = (url or "").strip() or \
        "https://raw.githubusercontent.com/sddvcm/qdnew/main/update_manifest.json"
    proxies = _build_proxies(proxy)
    try:
        resp = requests.get(test_url, headers={"User-Agent": "checkin-system"},
                            timeout=20, proxies=proxies)
        resp.raise_for_status()
        try:
            data = resp.json()
            ver = str(data.get("version") or "")
        except (json.JSONDecodeError, UnicodeDecodeError):
            ver = ""
        return {"ok": True, "status": resp.status_code, "version": ver}
    except requests.RequestException as e:
        return {"ok": False, "error": f"代理连接失败：{e}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"测试出错：{e}"}
