# -*- coding: utf-8 -*-
import io, subprocess, os
repo = r"C:\Users\Administrator\WorkBuddy\自动签到\checkin-system"
git = r"C:\Users\Administrator\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd\git.exe"
TOKEN_FILE = r"C:\Users\Administrator\WorkBuddy\自动签到\GitHub API.txt"
out = []

def run(args, env=None, mask=True):
    r = subprocess.run([git] + args, cwd=repo, capture_output=True, text=True,
                       env=env)
    cmd = " ".join(args)
    if mask:
        cmd = cmd.replace(TOKEN, "***") if 'TOKEN' in dir() else cmd
    out.append("$ git " + cmd)
    o = (r.stdout or "").strip()
    e = (r.stderr or "").strip()
    if mask and 'TOKEN' in dir():
        o = o.replace(TOKEN, "***"); e = e.replace(TOKEN, "***")
    if o: out.append(o[:1500])
    if e: out.append("[err] " + e[:600])
    out.append("")
    return r

TOKEN = io.open(TOKEN_FILE, encoding="utf-8").read().strip()

# 1) 提交工作区剩余改动
run(["add", "-A"])
run(["commit", "-m",
     "chore: Dockerfile 支持 WITH_CAPTCHA 可选装本地识别；更新部署文档；\n"
     "移除已废弃的 packaging/fnos/payload 快照（改用 build/payload）"])

# 2) 推送（token 只放在临时 remote URL 里，不写进 .git/config）
url = f"https://x-access-token:{TOKEN}@github.com/sddvcm/qdnew.git"
run(["push", url, "main:main"])

run(["log", "--oneline", "-3"])
out.append("--- 远端 main ---")
run(["ls-remote", "--heads", "origin"])

with io.open(os.path.join(repo, "__push.out"), "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("done")
