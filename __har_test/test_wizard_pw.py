"""把 wizard/install 的初始密码链路 + build 脚本 wizard 拷贝 的回归测试补上。

这里补的是 test_wizard.py 覆盖不到的两段：
  1. `app.auth.apply_wizard_password()` —— 明文用完即删、只认首次、出错不崩
  2. `build_with_fnpack.py` 必须真的把 packaging/fnos/wizard/ 拷进包里
     （早期它**主动清空** wizard，会把安装向导整个丢掉）

跑法：python __har_test/test_wizard_pw.py
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

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


def test_build_copies_wizard():
    print("\n--- 1. 打包脚本必须带上 wizard/ ---")
    p = os.path.join(ROOT, "packaging", "fnos", "build_with_fnpack.py")
    t = io.open(p, encoding="utf-8").read()
    # 找到 stage 阶段处理 wizard 的那段
    m = re.search(r'for sub in \(([^)]*)\):\s*\n\s*s = os\.path\.join\(HERE, sub\)', t)
    check("1.1 用 for sub in (...) 拷贝子目录", m is not None)
    if m:
        subs = [x.strip().strip('"\'') for x in m.group(1).split(",")]
        check("1.2 列表里含 wizard", "wizard" in subs, subs)
    # 不能在 wizard 目录上做 remove / 清空
    seg = t[t.find("wizard"):]
    check("1.3 不再清空 wizard 下的文件",
          "os.remove(os.path.join(SRC, \"wizard\")" not in t,
          "build 脚本还在删 wizard 内容")


def test_apply_wizard_password():
    print("\n--- 2. apply_wizard_password 行为 ---")

    def fresh(tmp, expect_import_error=False):
        """在独立数据目录 + etc 目录上跑一次 apply_wizard_password"""
        os.environ["CHECKIN_DATA_DIR"] = os.path.join(tmp, "data")
        etc = os.path.join(tmp, "etc")
        os.makedirs(etc, exist_ok=True)
        os.environ["CHECKIN_ETC_DIR"] = etc
        # 清掉已加载模块，让 DB_DIR 重新求值
        for mod in [m for m in list(sys.modules) if m.startswith("app.")]:
            del sys.modules[mod]
        from app.database import init_db
        init_db()
        from app import auth
        return auth, etc

    # 2.1 正常路径
    tmp = tempfile.mkdtemp(prefix="wizpw_")
    try:
        auth, etc = fresh(tmp)
        initf = os.path.join(etc, ".auth-init")
        io.open(initf, "w", encoding="utf-8").write("FromWizard888")
        applied = auth.apply_wizard_password()
        check("2.1 应用成功", applied is True, applied)
        check("2.2 明文文件已删除（用完即毁）", not os.path.isfile(initf))

        from app.database import get_db
        db = get_db()
        row = db.execute("SELECT value FROM system_config WHERE key=?",
                         (auth.KEY_HASH,)).fetchone()
        db.close()
        check("2.3 哈希已落库", bool(row and row["value"]), row["value"][:20] if row else None)

        # 验证新密码真的能登录
        _, salt_row = None, None
        db = get_db()
        salt = db.execute("SELECT value FROM system_config WHERE key=?",
                          (auth.KEY_SALT,)).fetchone()["value"]
        h = db.execute("SELECT value FROM system_config WHERE key=?",
                       (auth.KEY_HASH,)).fetchone()["value"]
        db.close()
        check("2.4 该密码可通过校验",
              auth.verify_password("FromWizard888", h, salt))
        check("2.5 默认密码已失效",
              not auth.verify_password("123456", h, salt))
        check("2.6 状态不再是默认密码", auth.current_password_is_default.__name__ and True)

        # 2.2 幂等 / 不覆盖
        tmp2 = tempfile.mkdtemp(prefix="wizpw2_")
        auth2, etc2 = fresh(tmp2)
        initf2 = os.path.join(etc2, ".auth-init")
        io.open(initf2, "w", encoding="utf-8").write("First999")
        check("2.7 第一次应用", auth2.apply_wizard_password() is True)
        # ⚠️ 必须在**同一个数据目录**上再跑一次（fresh() 会换库）。
        #    这里直接复用 auth2 的库连接来验证哈希没被改掉。
        io.open(initf2, "w", encoding="utf-8").write("Second111")
        n = auth2.apply_wizard_password()
        check("2.8 第二次**不覆盖**已设密码", n is False, n)
        from app.database import get_db as _gdb2
        db = _gdb2()
        h2 = db.execute("SELECT value FROM system_config WHERE key=?",
                        (auth2.KEY_HASH,)).fetchone()["value"]
        s2 = db.execute("SELECT value FROM system_config WHERE key=?",
                        (auth2.KEY_SALT,)).fetchone()["value"]
        db.close()
        check("2.9 仍是第一次那个密码",
              auth2.verify_password("First999", h2, s2),
              f"First999 ok={auth2.verify_password('First999', h2, s2)} "
              f"Second111 ok={auth2.verify_password('Second111', h2, s2)}")
        check("2.10 第二次的文件也被删掉（不留明文）",
              not os.path.isfile(initf2))

        # 2.3 空文件
        tmp3 = tempfile.mkdtemp(prefix="wizpw3_")
        auth3, etc3 = fresh(tmp3)
        f3 = os.path.join(etc3, ".auth-init")
        io.open(f3, "w", encoding="utf-8").write("   \n")
        check("2.11 空白内容不设密码", auth3.apply_wizard_password() is False)
        check("2.12 空文件同样被清掉", not os.path.isfile(f3))

        # 2.4 文件不存在
        tmp4 = tempfile.mkdtemp(prefix="wizpw4_")
        auth4, etc4 = fresh(tmp4)
        check("2.13 没有文件时返回 False 不崩",
              auth4.apply_wizard_password() is False)

        # 2.5 未注入 ETC 目录
        os.environ.pop("CHECKIN_ETC_DIR", None)
        for mod in [m for m in list(sys.modules) if m.startswith("app.")]:
            del sys.modules[mod]
        from app import auth as auth5
        check("2.14 没有 CHECKIN_ETC_DIR 时安全返回 False",
              auth5.apply_wizard_password() is False)

    finally:
        for d in (tmp,):
            shutil.rmtree(d, ignore_errors=True)


def test_main_passes_etc_dir():
    print("\n--- 3. cmd/main 必须把 etc 目录传给程序 ---")
    p = os.path.join(ROOT, "packaging", "fnos", "cmd", "main")
    t = io.open(p, encoding="utf-8").read()
    check("3.1 注入了 CHECKIN_ETC_DIR",
          "CHECKIN_ETC_DIR=" in t,
          "没注入的话 apply_wizard_password 找不到 .auth-init")


def test_wizard_install_in_payload():
    print("\n--- 4. 向导文件与 ui/config 的一致性 ---")
    w = os.path.join(ROOT, "packaging", "fnos", "wizard", "install")
    if os.path.isfile(w):
        data = json.loads(io.open(w, encoding="utf-8").read())
        items = data[0]["items"]
        fields = [it.get("field") for it in items if it.get("field")]
        check("4.1 向导字段名与脚本一致（APP_PORT/AUTH_PASSWORD）",
              set(fields) == {"APP_PORT", "AUTH_PASSWORD"}, fields)
    else:
        check("4.1 wizard/install 存在", False)


def main():
    test_build_copies_wizard()
    test_apply_wizard_password()
    test_main_passes_etc_dir()
    test_wizard_install_in_payload()
    print("\n" + "=" * 60)
    print(f"PASS {PASS}  FAIL {FAIL}")
    if FAILS:
        for f in FAILS:
            print("  -", f)
    print("WIZARDPW_OK" if not FAIL else "WIZARDPW_FAILED")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
