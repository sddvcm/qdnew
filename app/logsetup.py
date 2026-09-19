"""日志配置

⚠️ 为什么必须有这个模块：应用原先**完全没有 logging 配置**，只有散落的
`print()`。后果是启动期的报错很难留痕：

1. Python 重定向到文件时 stdout 是**块缓冲**，进程崩在半路时缓冲区
   内容不会落盘 → 日志文件 0 字节（用户实际反馈"里面也没有日志生成"）。
   cmd/main 已经加了 `-u` / `PYTHONUNBUFFERED=1`，但应用自身也该把日志
   接进 logging 体系，而不是靠 print。
2. Werkzeug 的启动信息与异常栈走 logging（默认 stderr），不受 print 控制。
3. 出问题时用户需要的是**一个有内容、能找到的文件**，不是空文件。

日志落点：`<CHECKIN_LOG_DIR>/app.log`（= 共享目录下的 logs/，文件管理可见）。
未设置该环境变量时退回 <项目根>/logs（本地开发）。
"""
import logging
import logging.handlers
import os
import sys

# ⚠️ 显式绑定：某些精简/裁剪过的 Python 运行时里 `logging.handlers`
# 并不会随 `import logging` 自动加载子模块，直接 `logging.handlers.X`
# 会抛 AttributeError。这里明确引用一次，缺失时尽早暴露。
from logging.handlers import RotatingFileHandler

_configured = False


def log_dir() -> str:
    """日志目录（与 cmd/main 注入的 CHECKIN_LOG_DIR 保持一致）"""
    d = os.environ.get("CHECKIN_LOG_DIR")
    if d:
        return d
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "logs")


def log_file() -> str:
    return os.path.join(log_dir(), "app.log")


def setup(level=logging.INFO):
    """配置根日志。幂等 —— 重复调用不会叠加 handler。

    原则：
    - 文件 handler 固定 1MB × 3 轮转，避免长期运行把磁盘写满
    - 控制台 handler 也保留：cmd/main 会把 stdout 重定向进同一个 app.log，
      本地直接 `python -m app.main` 时也能在终端看到
    - 写文件失败时**不能让应用起不来**（只读挂载等），降级为仅控制台
    """
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    try:
        d = log_dir()
        os.makedirs(d, exist_ok=True)
        fh = RotatingFileHandler(
            os.path.join(d, "app.log"), maxBytes=1024 * 1024,
            backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as e:                      # noqa: BLE001
        # 日志写不了不该影响服务运行 —— 打印到 stderr（会被 cmd 重定向接住）
        print(f"[logging] 无法创建日志文件（降级为仅控制台）：{e}", flush=True)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # Werkzeug 的每次请求日志很吵，降到 WARNING
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    # APScheduler 同理
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    _configured = True


def install_excepthook():
    """未捕获异常写进日志 —— 没有它，进程崩溃时日志里什么都看不到。

    这是排查"启动即退出"最关键的一环：哪怕崩在 import 阶段，
    栈也会落到 app.log 里。

    另外把 `threading.excepthook` 也接上：后台线程（如自动更新的进度线程、
    APScheduler 的任务线程）里的异常默认只打到 stderr 且格式很潦草，
    接进日志才查得到。
    """
    def _hook(exc_type, exc_value, exc_tb):
        logging.getLogger("app").critical(
            "未捕获异常，进程即将退出", exc_info=(exc_type, exc_value, exc_tb))
        # 原始 hook 会打印到 stderr（被 cmd 重定向接住），保留
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook

    try:
        import threading

        def _thread_hook(args):
            logging.getLogger("app").error(
                "线程 %s 未捕获异常：%s", getattr(args.thread, "name", "?"),
                args.exc_value, exc_info=(args.exc_type, args.exc_value,
                                          args.exc_traceback))

        threading.excepthook = _thread_hook
    except Exception:                           # noqa: BLE001
        pass


def startup_banner():
    """把启动环境写进日志 —— 排查问题时这些信息最关键。

    尤其对"更新后启动失败"：需要立刻知道跑的是哪个版本、哪个 Python、
    数据/日志落在哪，而不是反复问用户。
    """
    log = logging.getLogger("app")
    try:
        import platform
        log.info("=" * 56)
        log.info("签到管理系统启动")
        log.info("Python: %s (%s)", platform.python_version(), sys.executable)
        log.info("工作目录: %s", os.getcwd())
        try:
            ver_file = os.path.join(os.getcwd(), "version.json")
            if os.path.isfile(ver_file):
                import json
                with open(ver_file, encoding="utf-8") as f:
                    v = json.load(f)
                log.info("程序版本: %s", v.get("version", "?"))
            else:
                log.warning("找不到 version.json（可能更新不完整）")
        except Exception as e:                  # noqa: BLE001
            log.warning("读取版本失败：%s", e)
        log.info("数据目录: %s", os.environ.get("CHECKIN_DATA_DIR", "(默认)"))
        log.info("日志文件: %s", log_file())
        log.info("=" * 56)
    except Exception:                           # noqa: BLE001
        pass
