"""提交 v1.8.0（本地 git，带 PATH 补丁）"""
import io
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
GIT = r"C:\Users\Administrator\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd\git.exe"
MINGW = r"C:\Users\Administrator\.workbuddy\binaries\PortableGit\versions\1.2.0\mingw64\bin"
EXEC = r"C:\Users\Administrator\.workbuddy\binaries\PortableGit\versions\1.2.0\mingw64\libexec\git-core"

env = os.environ.copy()
# ⚠️ git-remote-https.exe 在 mingw64/bin，不在 cmd/ 旁；不补 PATH 会报
#    git: 'remote-https' is not a git command
env["PATH"] = MINGW + os.pathsep + EXEC + os.pathsep + env.get("PATH", "")
env["GIT_EXEC_PATH"] = EXEC
env["GIT_TERMINAL_PROMPT"] = "0"


def git(*args, check=True):
    r = subprocess.run([GIT] + list(args), cwd=ROOT, env=env,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    out = (r.stdout or "") + (r.stderr or "")
    print(f"$ git {' '.join(args)}\n{out.strip()[:600]}\n")
    if check and r.returncode != 0:
        raise SystemExit(f"git 失败: {out[:500]}")
    return out


def main():
    git("add", "-A")
    msg = ("v1.8.0 访问密码 + 安装向导 + 黄瓜吧登录修复\n\n"
           "- 新增访问密码保护（默认 123456，系统设置里可改）\n"
           "- FPK 安装向导支持自定义端口与访问密码\n"
           "- 修复黄瓜吧插件登录失败：验证码改为接口取题，"
           "失败消息不再输出 HTML 残片\n"
           "- 测试 700 项全通过")
    git("commit", "-m", msg)
    git("log", "--oneline", "-3")
    git("status", "--porcelain")


if __name__ == "__main__":
    main()
