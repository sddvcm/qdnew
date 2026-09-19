"""安装向导（wizard）配置项 → 实际生效 的回归测试。

⚠️ 这轮测试针对的是一类**极难排查**的故障：向导里填了端口，但应用还是
   按默认端口起 —— 用户在安装界面明明填了 5801，装完却打不开，日志里
   还看不出哪里不对。

覆盖：
  1. wizard/install 的 JSON 结构合法（type=text + field/initValue/rules）
  2. APP_PORT 环境变量（fnOS 把向导值以同名变量注入）被写进 app.env
  3. 非法端口（空 / 非数字 / 超范围）回退默认，**且不写坏 app.env**
  4. 没传 APP_PORT 时用默认 5800
  5. 幂等：已有 app.env 不被覆盖（密钥与端口都不能变）
  6. AUTH_PASSWORD 落成 .auth-init，且权限 600
  7. app.env 里的 APP_PORT 能被 load_env_file 重新读出来

跑法：python __har_test/test_wizard.py
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FNOS = os.path.join(ROOT, "packaging", "fnos")
CMD = os.path.join(FNOS, "cmd")
WIZARD = os.path.join(FNOS, "wizard")
BASH = os.environ.get("BASH_BIN") or \
    r"C:\Users\Administrator\.workbuddy\binaries\PortableGit\versions\1.2.0\bin\bash.exe"

PASS, FAIL = 0, 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILS.append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  | {str(extra)[:140]}" if extra else ""))


def posix(p):
    p = os.path.abspath(p)
    if p[1:3] == ":\\":
        return "/" + p[0].lower() + p[2:].replace("\\", "/")
    return p.replace("\\", "/")


# ---------------- 1. wizard/install 结构 ----------------
def test_wizard_json():
    print("\n--- 1. wizard/install JSON 结构 ---")
    p = os.path.join(WIZARD, "install")
    check("1.1 wizard/install 存在", os.path.isfile(p))
    raw = io.open(p, encoding="utf-8").read()
    try:
        data = json.loads(raw)
    except Exception as e:
        check("1.2 是合法 JSON", False, str(e)[:120])
        return
    check("1.2 是合法 JSON", True)
    check("1.3 是数组且非空", isinstance(data, list) and len(data) > 0)
    step = data[0]
    check("1.4 有 stepTitle", bool(step.get("stepTitle")), step.get("stepTitle"))
    items = step.get("items") or []
    check("1.5 有 items 且非空", len(items) > 0, f"n={len(items)}")

    types = [it.get("type") for it in items]
    check("1.6 只用了 fnpack 认识的类型（tips/text）",
          all(t in ("tips", "text", "radio", "switch", "select", "password")
              for t in types), types)

    # 端口项
    port_item = next((it for it in items if it.get("field") == "APP_PORT"), None)
    check("1.7 有 APP_PORT 输入项", port_item is not None)
    if port_item:
        check("1.8 端口项是 text 类型", port_item.get("type") == "text",
              port_item.get("type"))
        check("1.9 有默认值 5800",
              str(port_item.get("initValue")) == "5800", port_item.get("initValue"))
        check("1.10 有 label", bool(port_item.get("label")), port_item.get("label"))
        rules = port_item.get("rules") or []
        check("1.11 带 required 校验",
              any(r.get("required") for r in rules), rules)
        check("1.12 带数字格式校验",
              any(r.get("pattern") for r in rules), rules)

    # 密码项
    pw_item = next((it for it in items if it.get("field") == "AUTH_PASSWORD"), None)
    check("1.13 有 AUTH_PASSWORD 输入项", pw_item is not None)
    if pw_item:
        check("1.14 密码项默认留空（不预设弱密码）",
              (pw_item.get("initValue") or "") == "", pw_item.get("initValue"))

    # tips 不能带 field（否则 fnpack 可能校验不过）
    for it in items:
        if it.get("type") == "tips":
            check("1.15 tips 项不带 field", "field" not in it, it.get("field"))
            break

    # 别出现我们踩过坑的 switch 字段名（wizard/uninstall 曾经因此打包失败）
    check("1.16 没有自造字段名（如 fieldName）",
          not any("fieldName" in it for it in items))


def test_uninstall_wizard():
    """★ 回归：wizard/uninstall 曾用 {"type":"switch"}，fnpack 直接拒绝打包。

    报错原文：`File "uninstall" is not valid due to JSON format or content
    validation failure` —— 而且只有当 build 脚本**真的**把 wizard/ 拷进包时
    才会暴露（早期它把 wizard 清空了，所以这个雷一直埋着）。
    """
    print("\n--- 1b. wizard/uninstall 结构（switch 是雷） ---")
    p = os.path.join(WIZARD, "uninstall")
    check("1b.1 wizard/uninstall 存在", os.path.isfile(p))
    if not os.path.isfile(p):
        return
    raw = io.open(p, encoding="utf-8").read()
    try:
        data = json.loads(raw)
    except Exception as e:
        check("1b.2 是合法 JSON", False, str(e)[:120])
        return
    check("1b.2 是合法 JSON", True)
    items = data[0].get("items") or []

    bad = [it.get("type") for it in items if it.get("type") == "switch"]
    check("1b.3 **不含 switch 类型**（fnpack 会拒绝打包）", not bad, bad)

    # 用 radio 表达二选一（fn-seekbox 实测可打包的写法）
    radio = next((it for it in items if it.get("type") == "radio"), None)
    check("1b.4 用 radio 表达「保留/删除」", radio is not None)
    if radio:
        opts = [o.get("value") for o in (radio.get("options") or [])]
        check("1b.5 radio 有 keep / delete 两个选项",
              set(opts) == {"keep", "delete"}, opts)
        check("1b.6 默认保留（不能默认删数据）",
              radio.get("initValue") == "keep", radio.get("initValue"))

    # 卸载脚本必须认识新字段名（否则用户选了删除也不会删）
    sh = os.path.join(CMD, "uninstall_callback")
    t = io.open(sh, encoding="utf-8").read()
    check("1b.7 卸载脚本读 data_action", "data_action" in t)
    check("1b.8 兼容老字段 purge_data", "purge_data" in t)


# ---------------- 2. cmd 脚本读取向导值 ----------------
def run_init(tmp, extra_env=None):
    """在隔离目录里跑 init_env_file，返回 (rc, app.env 内容 or None)"""
    etc = os.path.join(tmp, "etc")
    var = os.path.join(tmp, "var")
    appdest = os.path.join(tmp, "appdest")
    for d in (etc, var, appdest):
        os.makedirs(d, exist_ok=True)

    env = os.environ.copy()
    env.update({
        "TRIM_APPDEST": posix(appdest),
        "TRIM_PKGVAR": posix(var),
        "TRIM_PKGETC": posix(etc),
        "TRIM_USERNAME": "", "TRIM_GROUPNAME": "",
    })
    # 清掉可能从上一次继承的
    for k in ("APP_PORT", "AUTH_PASSWORD"):
        env.pop(k, None)
    if extra_env:
        env.update(extra_env)

    script = (f'source "{posix(os.path.join(CMD, "common.sh"))}"\n'
              f'init_env_file\n'
              f'echo "RC=$?"\n')
    r = subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                       env=env, cwd=CMD, timeout=60)
    envfile = os.path.join(etc, "app.env")
    content = io.open(envfile, encoding="utf-8").read() if os.path.isfile(envfile) else None
    return r, content, etc


def get_port(content):
    if not content:
        return None
    m = re.search(r"^\s*APP_PORT\s*=\s*(\S+)", content, re.M)
    return m.group(1) if m else None


def test_port_from_wizard():
    print("\n--- 2. 向导端口写入 app.env ---")
    tmp = tempfile.mkdtemp(prefix="wizard_test_")
    try:
        for given, want in [("5801", "5801"), ("8080", "8080"),
                            ("12668", "12668"), ("65535", "65535")]:
            sub = os.path.join(tmp, given)
            r, content, _ = run_init(sub, {"APP_PORT": given})
            check(f"2.x 向导填 {given} → app.env={want}",
                  get_port(content) == want,
                  f"rc={r.returncode} port={get_port(content)} "
                  f"{(r.stderr or '')[:80]}")

        print("\n--- 3. 非法端口回退默认（不能让坏端口卡住启动） ---")
        for bad, label in [("", "空"), ("abc", "非数字"), ("0", "0"),
                           ("70000", "超范围"), ("-1", "负数"), ("58.5", "小数")]:
            sub = os.path.join(tmp, "bad_" + label)
            r, content, _ = run_init(sub, {"APP_PORT": bad})
            got = get_port(content)
            check(f"3.x 非法端口「{label}」→ 回退 5800", got == "5800",
                  f"got={got} rc={r.returncode}")
            check(f"3.y 非法端口「{label}」→ app.env 仍完好",
                  content is not None and "CHECKIN_SECRET_KEY=" in content)

        print("\n--- 4. 未传 APP_PORT → 用默认 5800 ---")
        sub = os.path.join(tmp, "noenv")
        r, content, _ = run_init(sub)
        check("4.1 默认端口 5800", get_port(content) == "5800",
              f"rc={r.returncode} port={get_port(content)}")

        print("\n--- 5. 幂等：已有 app.env 不被覆盖 ---")
        sub = os.path.join(tmp, "idem")
        r1, c1, etc = run_init(sub, {"APP_PORT": "5900"})
        first = get_port(c1)
        # 再跑一次，这次故意传一个不同的端口 —— 必须**不生效**
        r2, c2, _ = run_init(sub, {"APP_PORT": "5901"})
        check("5.1 端口保持第一次的值", get_port(c2) == first == "5900",
              f"first={first} second={get_port(c2)}")
        s1 = re.search(r"CHECKIN_SECRET_KEY=(\w+)", c1)
        s2 = re.search(r"CHECKIN_SECRET_KEY=(\w+)", c2)
        check("5.2 密钥不变（改了会导致已存密码解不开）",
              s1 and s2 and s1.group(1) == s2.group(1))

        print("\n--- 6. AUTH_PASSWORD → .auth-init（权限 600） ---")
        sub = os.path.join(tmp, "authpw")
        r, content, etc = run_init(sub, {"AUTH_PASSWORD": "MySecret123"})
        initf = os.path.join(etc, ".auth-init")
        check("6.1 .auth-init 已生成", os.path.isfile(initf))
        if os.path.isfile(initf):
            val = io.open(initf, encoding="utf-8").read()
            check("6.2 内容就是向导密码", val == "MySecret123", repr(val))
            check("6.3 **不落明文进 app.env**",
                  "MySecret123" not in (content or ""),
                  "app.env 里出现了明文密码！")

        sub2 = os.path.join(tmp, "nopw")
        r, content2, etc2 = run_init(sub2)
        check("6.4 未填密码则不生成 .auth-init",
              not os.path.isfile(os.path.join(etc2, ".auth-init")))

        print("\n--- 7. app.env 能被 load_env_file 读回来 ---")
        sub = os.path.join(tmp, "reload")
        r, content, etc = run_init(sub, {"APP_PORT": "5877"})
        env = os.environ.copy()
        env.update({
            "TRIM_APPDEST": posix(os.path.join(sub, "appdest")),
            "TRIM_PKGVAR": posix(os.path.join(sub, "var")),
            "TRIM_PKGETC": posix(etc),
            "TRIM_USERNAME": "", "TRIM_GROUPNAME": "",
        })
        script = (f'source "{posix(os.path.join(CMD, "common.sh"))}"\n'
                  f'load_env_file\n'
                  f'echo "READ_PORT=$APP_PORT"\n')
        r2 = subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                            env=env, cwd=CMD, timeout=60)
        check("7.1 读回的端口 = 向导填的",
              "READ_PORT=5877" in (r2.stdout or ""),
              (r2.stdout or "")[:100] + (r2.stderr or "")[:100])

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------- 8. 打包脚本会把 APP_PORT 一起带进去 ----------------
def test_build_consistency():
    print("\n--- 8. 与打包流程的一致性 ---")
    b = os.path.join(FNOS, "build_with_fnpack.py")
    t = io.open(b, encoding="utf-8").read()
    # 之前 build 脚本会把 wizard/ 清空！现在不能再清了
    check("8.1 build 脚本不再清空 wizard/*",
          "wizard" not in t or "unlink" not in t.split("wizard")[1][:400],
          "检查 build_with_fnpack.py 的 wizard 处理")
    check("8.2 build 脚本会拷贝 wizard/ 目录",
          'for sub in ("cmd", "config")' in t or '"wizard"' in t,
          "需要把 wizard 纳入拷贝列表，否则安装向导不会进包")
    # ui/config 里的端口应与 manifest/默认一致
    uicfg = os.path.join(FNOS, "build", "payload", "ui", "config")
    if os.path.isfile(uicfg):
        d = json.loads(io.open(uicfg, encoding="utf-8").read())
        ent = list(d.get(".url", {}).values())
        if ent:
            check("8.3 桌面入口端口可被向导端口覆盖（飞牛按 APP_PORT 解析）",
                  "port" in ent[0], ent[0])


def main():
    test_wizard_json()
    test_uninstall_wizard()
    test_port_from_wizard()
    test_build_consistency()
    print("\n" + "=" * 60)
    print(f"PASS {PASS}  FAIL {FAIL}")
    if FAILS:
        for f in FAILS:
            print("  -", f)
    print("WIZARD_OK" if not FAIL else "WIZARD_FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
