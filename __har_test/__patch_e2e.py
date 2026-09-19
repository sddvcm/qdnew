"""把 e2e 测试的 bare test_client() 换成已登录的 client。

一次性脚本：改完即可删。逐文件做精确文本替换，并顺带补上
`from test_auth_helper import login_client` 的导入与 __har_test 的 sys.path。
"""
import io
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))

# 文件 -> [(old, new), ...]
PLAN = {
    "integration.py": [("client = app.test_client()",
                        "client = login_client(app)")],
    "test_features.py": [("client = app.test_client()",
                          "client = login_client(app)")],
    "test_extras_e2e.py": [("c = app.test_client()", "c = login_client(app)")],
    "test_tpl_reload.py": [("c = app.test_client()", "c = login_client(app)"),
                           ("c2 = app.test_client()", "c2 = login_client(app)")],
    "test_transfer_e2e.py": [("c = app.test_client()", "c = login_client(app)"),
                             ("c2 = app2.test_client()", "c2 = login_client(app2)"),
                             ("c3 = app3.test_client()", "c3 = login_client(app3)")],
    "test_fnos.py": [("c = app.test_client()", "c = login_client(app)")],
    "test_update_progress.py": [("c = app.test_client()", "c = login_client(app)")],
    "test_logsetup.py": [("c = app.test_client()", "c = login_client(app)")],
}

IMPORT_LINE = "from test_auth_helper import login_client"

# 在每个文件里，把导入插到「最后一个 sys.path.insert 之后」
PATH_INS = re.compile(r"^sys\.path\.insert\(0, ROOT\)\s*$", re.M)
PATH_INS2 = re.compile(r'^sys\.path\.insert\(0, os\.path\.dirname\([^\)]*\)\)\s*$', re.M)


def ensure_here_in_path(text):
    """确保 __har_test 自身在 sys.path（这样能 import test_auth_helper）"""
    if "_HERE_FOR_HELPER" in text:
        return text
    anchor = "sys.path.insert(0, ROOT)"
    if anchor in text:
        return text.replace(
            anchor,
            "_HERE_FOR_HELPER = os.path.dirname(os.path.abspath(__file__))\n"
            "if _HERE_FOR_HELPER not in sys.path:\n"
            "    sys.path.insert(0, _HERE_FOR_HELPER)\n" + anchor,
            1)
    # 没有 ROOT 变量的，插到第一个 import 之前（靠 os 已导入）
    m = re.search(r"^import os\s*$", text, re.M)
    if m:
        return text[:m.end()] + (
            "\n\n_HERE_FOR_HELPER = os.path.dirname(os.path.abspath(__file__))\n"
            "if _HERE_FOR_HELPER not in sys.path:\n"
            "    sys.path.insert(0, _HERE_FOR_HELPER)") + text[m.end():]
    return text


def main():
    for fname, pairs in PLAN.items():
        path = os.path.join(HERE, fname)
        if not os.path.exists(path):
            print(f"skip  {fname} (不存在)")
            continue
        text = io.open(path, encoding="utf-8").read()
        orig = text

        for old, new in pairs:
            if old not in text:
                print(f"  ! {fname}: 找不到 {old!r}")
                continue
            text = text.replace(old, new)

        text = ensure_here_in_path(text)

        if IMPORT_LINE not in text:
            # 插到 ROOT 定义之后（那时 os/sys 都已导入）
            m = re.search(r"^ROOT\s*=.*$", text, re.M)
            if m:
                text = text[:m.end()] + "\n\n" + IMPORT_LINE + text[m.end():]
            else:
                m = re.search(r"^import sys\s*$", text, re.M)
                text = text[:m.end()] + "\n" + IMPORT_LINE + text[m.end():]

        if text != orig:
            io.open(path, "w", encoding="utf-8").write(text)
            print(f"patched {fname}")
        else:
            print(f"nochange {fname}")


if __name__ == "__main__":
    main()
