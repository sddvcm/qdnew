#!/bin/bash
# 签到管理系统 — cmd/ 脚本共享函数
# 被 main / install_callback / upgrade_callback 通过 `source "$(dirname "$0")/common.sh"` 引用。
# ⚠️ 本文件依赖 TRIM_* 环境变量（由 fnOS 注入），不要单独执行。

APP_NAME="checkin-system"

APP_DIR="${TRIM_APPDEST}"
VAR_DIR="${TRIM_PKGVAR}"
ETC_DIR="${TRIM_PKGETC}"
ENV_FILE="${ETC_DIR}/app.env"
LOG_DIR="${VAR_DIR}/logs"
LOG_FILE="${LOG_DIR}/app.log"
PID_FILE="${VAR_DIR}/app.pid"
RUNTIME_DIR="${APP_DIR}/runtime"
RUNTIME_TAR="${APP_DIR}/runtime.tar"
PY_BIN="${RUNTIME_DIR}/bin/python3"
DEFAULT_PORT=5800

# ---- 用户可见的数据目录（@appshare）----
# TRIM_PKGVAR 指向 @appdata（应用私有数据，文件管理器里看不到）。
# 用户要求把数据库/备份/插件/导出文件放到同卷的 @appshare/<应用名> 下，
# 这样在飞牛文件管理里能直接找到、随时备份。
# ⚠️ 卷名从 TRIM_PKGVAR 推导，**绝不写死 /vol1** —— 应用装在哪个卷就落在哪个卷
#    （含 root 安装到系统盘 /usr/local/apps 的情况）。
case "${TRIM_PKGVAR}" in
    */@appdata/*)
        SHARE_VOL="${TRIM_PKGVAR%%/@appdata*}"          # 如 /vol1
        SHARE_ROOT="${SHARE_VOL}/@appshare/${APP_NAME}"
        ;;
    /usr/local/apps/@appdata/*)
        SHARE_ROOT="/usr/local/apps/@appshare/${APP_NAME}"
        ;;
    *)
        # 未知布局：不猜，回退到 var（老行为），保证能跑
        SHARE_ROOT="${VAR_DIR}"
        ;;
esac

DATA_DIR="${SHARE_ROOT}/data"
USER_PLUGINS_DIR="${SHARE_ROOT}/user_plugins"
BACKUP_DIR="${DATA_DIR}/backups"          # 与 updater.py 的 DATA_DIR/backups 一致

log_msg() {
    mkdir -p "$LOG_DIR" 2>/dev/null
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$1]" >> "$LOG_DIR/install.log" 2>/dev/null
}

# 初始化目录结构（幂等，安装/升级/启动都可重复调用）
ensure_dirs() {
    mkdir -p "$LOG_DIR" "$DATA_DIR" "$USER_PLUGINS_DIR" "$BACKUP_DIR" \
             "$ETC_DIR" "$SHARE_ROOT" 2>/dev/null
}

# 一次性迁移：把历史版本落在 @appdata（TRIM_PKGVAR）下的用户数据搬到 @appshare。
# 幂等；目标已有同名项时**不覆盖**（保新数据），只搬缺的。
# 在 start / install_callback / upgrade_callback 里都会被调用 ——
# 对已装 1.5.x 的机器，升级后第一次启动即完成搬家，无需手工操作。
#
# ⚠️ 两个坑（实测踩过）：
# 1. `mv src dst` 当 dst 存在时会把 src **塞进 dst 里面**（变成 dst/src），
#    不是合并。所以 dst 为空时要先 rmdir 再 mv。
# 2. ensure_dirs 会先建好空的 $DATA_DIR/backups —— 若只判断"dst 非空就跳过"，
#    这个脚手架空目录会让迁移永远被跳过。空目标必须当"不存在"处理。
migrate_legacy_data() {
    local item src dst base moved skipped
    for item in data user_plugins backups; do
        src="${VAR_DIR}/${item}"
        dst="${SHARE_ROOT}/${item}"
        [ -d "$src" ] || continue
        mkdir -p "$dst" 2>/dev/null

        if [ ! -n "$(ls -A "$dst" 2>/dev/null)" ]; then
            # 目标为空（可能只是脚手架）：删掉空壳后整体搬
            rmdir "$dst" 2>/dev/null
            if mv "$src" "$dst" 2>/dev/null; then
                log_msg "migrate: ${src} -> ${dst}"
            else
                # mv 失败（跨设备等）：退回复制
                if cp -ap "$src/." "$dst/" 2>/dev/null; then
                    log_msg "migrate: 复制 ${src} -> ${dst}（跨设备回退）"
                else
                    log_msg "migrate: 迁移 ${src} 失败，保留原位置继续"
                fi
            fi
        else
            # 目标非空：逐项合并，已存在的同名项跳过（不覆盖新数据）
            moved=0
            skipped=0
            for f in "$src"/.??* "$src"/*; do
                [ -e "$f" ] || continue
                base=$(basename "$f")
                if [ -e "$dst/$base" ]; then
                    skipped=$((skipped + 1))
                    continue
                fi
                if mv "$f" "$dst/" 2>/dev/null; then
                    moved=$((moved + 1))
                fi
            done
            log_msg "migrate: ${item} 合并完成（移入 ${moved} 项，跳过已存在 ${skipped} 项）"
        fi

        # 源目录搬空了就清理掉
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
    if ! (cd "$APP_DIR" && tar -xf runtime.tar) 2>>"$LOG_DIR/install.log"; then
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
fix_ownership() {
    if [ "$(id -u)" = "0" ] && [ -n "$TRIM_USERNAME" ]; then
        chown -R "$TRIM_USERNAME:$TRIM_GROUPNAME" \
            "$APP_DIR" "$VAR_DIR" "$ETC_DIR" "$SHARE_ROOT" 2>/dev/null
        log_msg "已修正目录归属: $TRIM_USERNAME:$TRIM_GROUPNAME"
    fi
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
