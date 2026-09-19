#!/bin/bash
# 签到管理系统 — cmd/ 脚本共享函数
# 被 main / install_callback / upgrade_callback 通过 `source "$(dirname "$0")/common.sh"` 引用。
# ⚠️ 本文件依赖 TRIM_* 环境变量（由 fnOS 注入），不要单独执行。

APP_NAME="checkin-system"

APP_DIR="${TRIM_APPDEST}"
VAR_DIR="${TRIM_PKGVAR}"
ETC_DIR="${TRIM_PKGETC}"
ENV_FILE="${ETC_DIR}/app.env"
RUNTIME_DIR="${APP_DIR}/runtime"
RUNTIME_TAR="${APP_DIR}/runtime.tar"
PY_BIN="${RUNTIME_DIR}/bin/python3"
DEFAULT_PORT=5800

# ---- 用户可见的数据目录（官方 data-share 机制）----
# TRIM_PKGVAR 指向 @appdata（应用私有，文件管理器看不到）。
# 要让用户能在「文件管理」里直接找到数据库/备份/插件/导出文件，飞牛的
# **正规做法**是在 `config/resource` 里声明 data-share：
#
#     {"data-share": {"shares": [{"name": "checkin-system"}]}}
#
# 安装时 fnOS 会自动创建该共享目录（Windows ACL 权限模型，不是 POSIX ACL）、
# 并自动给应用运行用户授予访问权限。路径有**两个官方取径**：
#
#   ① 稳定软链（推荐，不受环境变量注入与否影响）
#        /var/apps/<appname>/share/<子目录>      ← /var/apps/<app>/shares/ 里的成员
#      ⚠️ 注意：/var/apps/<appname>/shares/ 是**声明的每个 share 各自的软链集合**；
#         文档原文「可以通过 /var/apps/myapp/share/ 下的软链访问对应目录」。
#         实测两种拼写都存在过，所以这里把两个候选都探一遍（见 ensure_data_dir）。
#   ② 环境变量
#        TRIM_DATA_SHARE_PATHS=/vol1/@appshare/<app>[：更多路径]
#      多路径用 ":" 分隔，取第一个（官方示例即 `${VAR%%:*}`）。
#
# ⚠️ **为什么必须两个都探**：TRIM_DATA_SHARE_PATHS 由系统注入到**生命周期脚本**
#    的环境里，而常驻进程（以及某些版本下的 start 分支）不一定继承到它。
#    只认环境变量 → 探测落空 → 退回私有目录（数据在文件管理器里又看不到了）。
#    软链是文件系统层的，任何时候都在，更可靠。
#
# ⚠️ 早期版本（1.5.7）我们自己拼 @appshare 路径并 mkdir —— 那是错的：
#    ①用户安装的应用默认没有建共享目录的权限，mkdir 必然失败；
#    ②即使建出来也没有 ACL 授权，应用写不进去；
#    ③结果 CHECKIN_DATA_DIR 指向不可写目录 → 建库失败 → 进程启动即崩。
#    现在：声明交给 fnOS 建、路径用官方取径、再加可写性探测兜底。
if [ -n "${TRIM_DATA_SHARE_PATHS:-}" ]; then
    SHARE_ROOT="${TRIM_DATA_SHARE_PATHS%%:*}"
elif [ -n "${wizard_share_path:-}" ]; then          # 安装向导传进来的（若有）
    SHARE_ROOT="${wizard_share_path}"
else
    SHARE_ROOT=""                                   # 交给 ensure_data_dir 探测
fi

# 文档明示的稳定软链入口。两种拼写都作为候选（哪个存在用哪个）。
APP_SHARES_DIR="/var/apps/${APP_NAME}"              # /var/apps/<app> 是安装后的应用目录
SHARE_LINK_CANDIDATES="${APP_SHARES_DIR}/shares/${APP_NAME} ${APP_SHARES_DIR}/share/${APP_NAME}"

# 兜底数据目录：@appdata（老行为）。共享目录不可用时回退到这里 ——
# **宁可数据放私有目录，也不能让应用起不来**。
FALLBACK_ROOT="${VAR_DIR}"

# 探测并落定最终数据目录。
# 判定标准很简单：**能不能真的写进去**。光看目录存在不够（可能是只读挂载
# 或 ACL 没授权），所以实际写一个探针文件。
# 结果写入全局 DATA_ROOT，并导出 CHECKIN_DATA_DIR 供 Python 侧使用。
DATA_ROOT=""
DATA_ROOT_SRC=""                                    # 记录来源，便于排查"数据到底在哪、为什么"
ensure_data_dir() {
    local cand probe link
    # 候选顺序：软链 → 环境变量 → 私有兜底
    # 软链优先是因为它由文件系统保证，不依赖环境变量注入。
    local candidates=""
    for link in $SHARE_LINK_CANDIDATES; do
        [ -d "$link" ] && candidates="$candidates $link"
    done
    [ -n "$SHARE_ROOT" ] && candidates="$candidates $SHARE_ROOT"
    candidates="$candidates $FALLBACK_ROOT"

    for cand in $candidates; do
        [ -n "$cand" ] || continue
        # ⚠️ 软链要取真实路径：后续 mv/cp 与 Python 侧记录都用它，
        #    避免同一份数据出现两种写法导致迁移判断出错。
        if [ -L "$cand" ]; then
            cand=$(readlink -f "$cand" 2>/dev/null) || continue
            [ -n "$cand" ] || continue
        fi
        if mkdir -p "$cand/data" 2>/dev/null; then
            probe="${cand}/data/.write_probe_$$"
            if (echo ok > "$probe") 2>/dev/null; then
                rm -f "$probe" 2>/dev/null
                DATA_ROOT="$cand"
                break
            fi
        fi
    done

    # 记录来源（供日志与 Python 侧诊断）
    if [ "$DATA_ROOT" = "$FALLBACK_ROOT" ]; then
        DATA_ROOT_SRC="私有兜底(@appdata)"
    elif [ -n "$SHARE_ROOT" ] && [ "$DATA_ROOT" = "$SHARE_ROOT" ]; then
        DATA_ROOT_SRC="共享目录(TRIM_DATA_SHARE_PATHS)"
    elif [ -n "$DATA_ROOT" ]; then
        DATA_ROOT_SRC="共享目录(软链)"
    else
        DATA_ROOT="${FALLBACK_ROOT}"                # 全都不行：退回私有，让后续报错更明确
        DATA_ROOT_SRC="私有兜底(强制)"
    fi
    export DATA_ROOT DATA_ROOT_SRC
}

ensure_data_dir
DATA_DIR="${DATA_ROOT}/data"
USER_PLUGINS_DIR="${DATA_ROOT}/user_plugins"
BACKUP_DIR="${DATA_DIR}/backups"          # 与 updater.py 的 DATA_DIR/backups 一致

# ---- 清理 Python 字节码缓存 ----
# ★ 血泪教训：用户从 1.6.0 在线更新到 1.6.1 后，**一重启就起不来**。
# 根因是更新用 os.replace() 原子替换 .py，但旧 .pyc 还留着；Python 判断
# 缓存有效性靠「源文件 mtime + size」，而 mtime 只有**秒级精度** ——
# 小改动在同一秒内完成时，新 .py 的 mtime/size 可能恰好与 .pyc 记录的一致，
# 解释器便认为缓存有效，**按旧字节码执行而磁盘上是新源码** →
# ImportError / TypeError（旧签名调新函数）→ 启动即崩，且报错与源码对不上。
#
# 启动前清一次是最稳的兜底：不管更新流程有没有清干净，重启后一定是新代码。
# 代价仅是首次启动多编译几十毫秒，完全可以接受。
purge_pycache() {
    [ -d "$APP_DIR" ] || return 0
    local n
    n=$(find "$APP_DIR" -type d -name __pycache__ 2>/dev/null | wc -l)
    [ "$n" -gt 0 ] || return 0
    find "$APP_DIR" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null
    log_msg "已清理 $n 个 __pycache__（避免旧字节码导致启动异常）"
}

# 日志与 PID（放在用户可见的共享目录里）
# ⚠️ 原先在 VAR_DIR(@appdata)，用户反馈"日志找不到、里面也没有内容"：
#   ①@appdata 在文件管理器里看不到，出问题时根本不知道去哪找；
#   ②更关键的是**日志文件是空的** —— 见 main 里的缓冲问题。
# 现在跟着数据一起落在共享目录，用户能直接打开看、方便反馈。
# 兜底：DATA_ROOT 不可写时退回私有目录，保证日志功能始终可用。
LOG_DIR="${DATA_ROOT}/logs"
LOG_FILE="${LOG_DIR}/app.log"
INSTALL_LOG="${LOG_DIR}/install.log"
PID_FILE="${DATA_ROOT}/app.pid"

log_msg() {
    mkdir -p "$LOG_DIR" 2>/dev/null
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$1]" >> "$INSTALL_LOG" 2>/dev/null
}

# 初始化目录结构（幂等，安装/升级/启动都可重复调用）
# 日志与 PID 现在也在 DATA_ROOT 下（共享目录），一起建。
ensure_dirs() {
    mkdir -p "$LOG_DIR" "$DATA_DIR" "$USER_PLUGINS_DIR" "$BACKUP_DIR" \
             "$ETC_DIR" 2>/dev/null
}

# 把历史版本落在 @appdata（VAR_DIR）下的用户数据搬到当前 DATA_ROOT。
# 仅当两者不同（即共享目录可用）时才做；幂等；目标已有同名项不覆盖。
migrate_legacy_data() {
    local item src dst base moved skipped
    [ "$DATA_ROOT" = "$VAR_DIR" ] && return 0      # 已在私有目录，无需迁移
    # logs 也一起搬：老版本日志写在 @appdata 里，用户看不到；
    # 新版本挪到共享目录，把历史日志带过去免得"日志凭空消失"。
    for item in data user_plugins backups logs; do
        src="${VAR_DIR}/${item}"
        dst="${DATA_ROOT}/${item}"
        [ -d "$src" ] || continue
        mkdir -p "$dst" 2>/dev/null

        if [ -z "$(ls -A "$dst" 2>/dev/null)" ]; then
            rmdir "$dst" 2>/dev/null      # 空壳先删，否则 mv 会把 src 塞进 dst 里
            if mv "$src" "$dst" 2>/dev/null; then
                log_msg "migrate: ${src} -> ${dst}"
            elif cp -ap "$src/." "$dst/" 2>/dev/null; then
                log_msg "migrate: 复制 ${src} -> ${dst}（跨设备回退）"
            else
                log_msg "migrate: 迁移 ${src} 失败，保留原位置继续"
            fi
        else
            moved=0; skipped=0
            for f in "$src"/.??* "$src"/*; do
                [ -e "$f" ] || continue
                base=$(basename "$f")
                if [ -e "$dst/$base" ]; then
                    skipped=$((skipped + 1)); continue
                fi
                mv "$f" "$dst/" 2>/dev/null && moved=$((moved + 1))
            done
            log_msg "migrate: ${item} 合并完成（移入 ${moved}，跳过已存在 ${skipped}）"
        fi
        [ -d "$src" ] && [ -z "$(ls -A "$src" 2>/dev/null)" ] && rmdir "$src" 2>/dev/null
    done
}

# 在 NAS（Linux）上解压内置运行时。
# ⚠️ 必须在目标机上解压而不是打包机上：tar 会还原 python3→python3.12 的软链
#    和 bin/python3.12 的执行位，这些在 Windows 打包机上无法正确生成。
# 幂等：runtime/bin/python3 已存在且可执行时跳过（升级覆盖后靠升级脚本重解压）。
extract_runtime() {
    if [ -x "$PY_BIN" ]; then
        return 0
    fi
    if [ ! -f "$RUNTIME_TAR" ]; then
        log_msg "ERROR: runtime.tar 不存在: $RUNTIME_TAR"
        echo "内置运行时包缺失（runtime.tar），安装包可能不完整。" > "${TRIM_TEMP_LOGFILE:-/dev/stderr}"
        return 1
    fi
    log_msg "解压内置运行时（约 120MB，需要 10~40 秒）…"
    # ⚠️ 必须 cd 进目标目录用**相对路径**解压：
    # GNU tar 会把 `C:/xxx` 这类含冒号的路径当成 `主机:路径` 远程语法
    # （报 "Cannot connect to C: resolve failed"）。fnOS 上是 POSIX 路径
    # 虽不会触发，但相对路径写法在所有 tar 实现上都安全，测试环境也能跑通。
    if ! (cd "$APP_DIR" && tar -xf runtime.tar) 2>>"$INSTALL_LOG"; then
        log_msg "ERROR: 运行时解压失败"
        echo "内置运行时解压失败，请检查磁盘空间（需要约 400MB 可用）。" > "${TRIM_TEMP_LOGFILE:-/dev/stderr}"
        return 1
    fi
    chmod +x "$RUNTIME_DIR/bin/"* 2>/dev/null
    if [ ! -x "$PY_BIN" ]; then
        log_msg "ERROR: 解压后 python3 不可执行"
        echo "运行时解压后不可执行，可能是文件系统不支持（挂载了 noexec？）。" > "${TRIM_TEMP_LOGFILE:-/dev/stderr}"
        return 1
    fi
    log_msg "运行时就绪"
    return 0
}

# 读取 etc/app.env（KEY=VALUE），导出为环境变量
load_env_file() {
    if [ -f "$ENV_FILE" ]; then
        set -a
        . "$ENV_FILE"
        set +a
    fi
}

# 生成随机密钥（64 位十六进制；仅依赖 coreutils，不依赖 openssl）
gen_random_hex() {
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
}

# 初始化 etc/app.env（已存在则不动 —— 密钥一旦生成就不能变，否则已存密码解不开）
init_env_file() {
    ensure_dirs
    if [ -f "$ENV_FILE" ]; then
        return 0
    fi
    local secret
    secret=$(gen_random_hex)
    cat > "$ENV_FILE" << EOF
# 签到管理系统配置（安装时自动生成）
# CHECKIN_SECRET_KEY 用于加密任务里的账号密码与 Cookie。
# ⚠️ 生成后不要修改 —— 修改会导致已保存的密码无法解密。
CHECKIN_SECRET_KEY=${secret}
APP_PORT=${DEFAULT_PORT}
EOF
    chmod 600 "$ENV_FILE" 2>/dev/null
    log_msg "已生成 app.env（随机密钥）"
}

# 修正归属：以 root 执行安装时，把应用目录交给包用户，
# 否则「程序内自动更新」（以包用户身份运行）将无法写代码文件。
#
# ⚠️ 三个必须防的坑：
# 1. `$SHARE_ROOT` 可能为空（未声明 data-share 或探测失败）——
#    直接传入空串会让 chown 把**当前目录**当参数，改动范围不可控。
#    所以只收集非空且存在的路径。
# 2. 共享目录可能是**软链**：`chown -R` 默认跟随命令行上的软链，
#    会把目标目录（@appshare 下的真实路径）整棵树改归属。
#    这里显式用 `-h` 对软链本身操作，避免波及目录外的东西。
# 3. 目录不存在时 chown 会报错刷屏，先判存在。
fix_ownership() {
    [ "$(id -u)" = "0" ] || return 0
    [ -n "$TRIM_USERNAME" ] || return 0

    local own="${TRIM_USERNAME}:${TRIM_GROUPNAME}"
    local p
    for p in "$APP_DIR" "$VAR_DIR" "$ETC_DIR" "$DATA_ROOT"; do
        [ -n "$p" ] && [ -e "$p" ] || continue
        # 软链只改链接本身，不跟随；实体目录正常递归
        if [ -L "$p" ]; then
            chown -h "$own" "$p" 2>/dev/null
        else
            chown -R "$own" "$p" 2>/dev/null
        fi
    done
    log_msg "已修正目录归属: $own（APP/VAR/ETC/DATA_ROOT）"
}

# 进程管理
is_running() {
    local pid
    [ -f "$PID_FILE" ] || return 1
    pid=$(head -n 1 "$PID_FILE" 2>/dev/null | tr -d '[:space:]')
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    # 防 PID 复用误判：Linux 上核对 /proc/<pid>/cmdline 是否真是本应用；
    # 拿不到 /proc（非 Linux 环境）时退化为仅存活检查。
    if [ -r "/proc/$pid/cmdline" ]; then
        grep -aq "app.main" "/proc/$pid/cmdline" 2>/dev/null || return 1
    fi
    return 0
}

find_running_pid() {
    # 优先扫 /proc（cmdline 精确、不受 ps 截断影响）；无 /proc 再退化为 ps 扫描
    if [ -d /proc ]; then
        for p in /proc/[0-9]*; do
            if grep -aq "app.main" "$p/cmdline" 2>/dev/null; then
                basename "$p" 2>/dev/null
                return 0
            fi
        done
        return 1
    fi
    ps aux 2>/dev/null | grep -v grep | grep "runtime/bin/python3 -m app.main" | awk '{print $2}' | head -n 1
}
