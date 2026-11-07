#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TWRP 模块打包器的自测。

重点不是"能打个 zip"，而是那几个**上机才会暴露**的细节：
CRLF、可执行位、必要条目 —— 任何一个不对，recovery 里只会看到一句
"not found" 或干脆闪退，很难排查。
"""

import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import make_twrp_zip as m  # noqa: E402

OUT = os.path.join(HERE, "_twrp_test.zip")
FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILED.append(name)


def test_build():
    print("[1] 打包")
    path, names = m.build(OUT)
    check("生成了 zip", os.path.isfile(path))
    check("包含两个必要条目",
          set(m.REQUIRED).issubset(set(names)), names)
    problems = m.verify(path)
    check("自检无问题", not problems, problems)


def test_details():
    print("[2] 上机才会暴露的细节")
    with zipfile.ZipFile(OUT) as zf:
        data = zf.read("META-INF/com/google/android/update-binary")
        info = zf.getinfo("META-INF/com/google/android/update-binary")
    check("LF 换行（无 CRLF）", b"\r\n" not in data)
    check("shebang 正确", data.startswith(b"#!/sbin/sh"))
    mode = (info.external_attr >> 16) & 0o777
    check("有可执行位", bool(mode & 0o111), oct(mode))
    check("脚本非空", len(data) > 500, len(data))


def test_catches_broken():
    print("[3] 坏包必须被 verify 抓出来")
    bad = os.path.join(HERE, "_twrp_bad.zip")
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("META-INF/com/google/android/updater-script", "# only")
    problems = m.verify(bad)
    check("缺 update-binary → 报错", any("update-binary" in p for p in problems),
          problems)
    os.remove(bad)

    # CRLF + 无可执行位
    bad2 = os.path.join(HERE, "_twrp_bad2.zip")
    with zipfile.ZipFile(bad2, "w") as zf:
        info = zipfile.ZipInfo("META-INF/com/google/android/update-binary")
        info.external_attr = (0o100644 << 16)
        zf.writestr(info, "#!/sbin/sh\r\necho hi\r\n")
        zf.writestr("META-INF/com/google/android/updater-script", "# ok")
    problems = m.verify(bad2)
    check("CRLF → 报错", any("CRLF" in p for p in problems), problems)
    check("没有可执行位 → 报错",
          any("可执行位" in p for p in problems), problems)
    os.remove(bad2)


def test_script_lint():
    print("[4] update-binary 的 shell 结构")
    sys.path.insert(0, HERE)
    import sh_syntax
    src = os.path.join(ROOT, "twrp-dump", "META-INF", "com", "google", "android",
                       "update-binary")
    problems = sh_syntax.check_file(src)
    check("结构 + 变量检查通过", not problems, problems)


def main():
    try:
        test_build()
        test_details()
        test_catches_broken()
        test_script_lint()
    finally:
        if os.path.exists(OUT):
            os.remove(OUT)
    print()
    if FAILED:
        print("失败 %d 项：%s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
