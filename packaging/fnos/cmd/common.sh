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
DATA_DIR="${VAR_DIR}/data"
USER_PLUGINS_DIR="${VAR_DIR}/user_plugins"
BACKUP_DIR="${VAR_DIR}/backups"
RUNTIME_DIR="${APP_DIR}/runtime"
RUNTIME_TAR="${APP_DIR}/runtime.tar"
PY_BIN="${RUNTIME_DIR}/bin/python3"
DEFAULT_PORT=5800

log_msg() {
    mkdir -p "$LOG_DIR" 2>/dev/null
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$1]" >> "$LOG_DIR/install.log" 2>/dev/null
}

# 初始化目录结构（幂等，安装/升级/启动都可重复调用）
ensure_dirs() {
    mkdir -p "$LOG_DIR" "$DATA_DIR" "$USER_PLUGINS_DIR" "$BACKUP_DIR" "$ETC_DIR" 2>/dev/null
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
    log_msg "解压内置运行时（约 400MB，需要 1~3 分钟）…"
    # ⚠️ 必须 cd 进目标目录用**相对路径**解压：
    # GNU tar 会把 `C:/xxx` 这类含冒号的路径当成 `主机:路径` 远程语法
    # （报 "Cannot connect to C: resolve failed"）。fnOS 上是 POSIX 路径
    # 虽不会触发，但相对路径写法在所有 tar 实现上都安全，测试环境也能跑通。
    if ! (cd "$APP_DIR" && tar -xf runtime.tar) 2>>"$LOG_DIR/install.log"; then
        log_msg "ERROR: 运行时解压失败"
        echo "内置运行时解压失败，请检查磁盘空间（需要约 1GB 可用）。" > "${TRIM_TEMP_LOGFILE:-/dev/stderr}"
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
            "$APP_DIR" "$VAR_DIR" "$ETC_DIR" 2>/dev/null
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
