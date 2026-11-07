#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sh_syntax.py 的自测：既要"干净脚本能过"，也要"坏脚本一定被抓"。

关键在后者——只证明 build.sh 通过是不够的，还得证明这个检查器真的会报错，
否则它可能只是永远返回"没问题"的空壳。所以这里用**变异测试**：
拿真正的 build.sh 逐项破坏（删 fi / 删 done / 删 esac / 加未闭合引号），
每一项都必须被检查出来。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sh_syntax  # noqa: E402

BUILD_SH = os.path.join(HERE, "..", "scripts", "build.sh")
FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILED.append(name)


def read_build():
    with open(BUILD_SH, "r", encoding="utf-8") as f:
        return f.read()


def drop_line(text, stripped_target, only_nth=1):
    """删掉第 n 个摘要等于 target 的行。"""
    lines = text.split("\n")
    seen = 0
    keep = []
    dropped = False
    for line in lines:
        if not dropped and line.strip() == stripped_target:
            seen += 1
            if seen == only_nth:
                dropped = True
                continue
        keep.append(line)
    if not dropped:
        raise AssertionError("变异失败：找不到要删的行 %r" % stripped_target)
    return "\n".join(keep)


def test_clean():
    print("[1] 真实 build.sh 应当通过")
    problems = sh_syntax.check_file(BUILD_SH)
    check("build.sh 无结构问题", not problems, problems)
    text = read_build()
    check("文件非空且确实包含 if/then", "then" in text and "fi" in text)


def test_mutations():
    print("[2] 变异测试：破坏后必须被抓出来")
    text = read_build()
    mutations = [
        ("删掉一个 fi", drop_line(text, "fi"), "fi"),
        ("删掉一个 done", drop_line(text, "done"), "done"),
        ("删掉 esac", drop_line(text, "esac"), "esac"),
        ("双引号未闭合", text + '\necho "没闭合\n', "双引号"),
        ("单引号未闭合", text + "\necho '没闭合\n", "单引号"),
        ("$() 未闭合", text + "\necho $(abc\n", "$("),
        ("${} 未闭合", text + "\necho ${abc\n", "${"),
    ]
    for label, mutated, expect_kw in mutations:
        problems = sh_syntax.check_text(mutated)
        hit = any(expect_kw in p for p in problems) or bool(problems)
        check("%s → 报错" % label, hit, problems)
        if problems:
            check("%s → 带行号" % label, any("第" in p for p in problems), problems)


def test_snippets():
    print("[3] 常见写法的正例/反例")
    good = [
        "if true; then echo 1; fi",
        "if a; then b; elif c; then d; else e; fi",
        "for x in a b c; do echo $x; done",
        "while read -r l; do echo \"$l\"; done < file",
        "case \"$1\" in\n  a) echo A ;;\n  *) echo other ;;\nesac",
        "echo '# 不是注释也不是 if'\n# 这一整行是注释 if fi done esac",
        "f() { echo hi; }\nf",
        "X=\"$(echo 'if then fi')\"",
        "echo \"${VAR:-默认}\"",
    ]
    for s in good:
        problems = sh_syntax.check_text(s)
        check("正例：%r" % s.split("\n")[0][:38], not problems, problems)

    bad = [
        ("缺 fi", "if true; then echo 1"),
        ("多 fi", "echo 1\nfi"),
        ("缺 done", "for x in a b; do echo $x"),
        ("then 不在 if 里", "then echo"),
        ("注释里的 fi 不该被数", "# fi\nif true; then echo; fi"),
    ]
    for label, s in bad:
        problems = sh_syntax.check_text(s)
        expected_clean = label.startswith("注释")
        if expected_clean:
            check("%s → 通过" % label, not problems, problems)
        else:
            check("%s → 报错" % label, bool(problems))


def test_cli():
    print("[4] CLI 行为")
    rc = sh_syntax.main([BUILD_SH])
    check("CLI 返回 0", rc == 0, rc)
    rc = sh_syntax.main([os.path.join(HERE, "_不存在的文件.sh")])
    check("文件不存在时不应崩溃", rc in (0, 1, 2), rc)


def test_vars():
    print("[5] set -u 变量检查")
    text = read_build()

    # 真实 build.sh 不应有未定义变量
    problems = sh_syntax.check_vars(text)
    check("build.sh 无未定义变量", not problems, problems)

    # 变量检查必须真的能抓到——改名一个 $VAR 就要报
    mutated = text.replace("config_source=$CONFIG_SOURCE", "config_source=$CONFIG_SOURC")
    problems = sh_syntax.check_vars(mutated)
    check("改名 $CONFIG_SOURCE → 报错", bool(problems), problems)
    check("报错里点名了它", any("CONFIG_SOURC" in p for p in problems), problems)

    mutated = text.replace('"$KERNEL_TARGET"', '"$KERNEL_TARGTE"')
    problems = sh_syntax.check_vars(mutated)
    check("改名 $KERNEL_TARGET → 报错", bool(problems), problems)

    cases = [
        ("未定义直接引用", "echo $FOO", True),
        ("双引号里也要查", 'echo "$FOO"', True),
        ("${} 形式", "echo ${FOO}", True),
        ("带默认值算安全", "echo ${FOO:-bar}", False),
        ("带 - 默认也算安全", "echo ${FOO-bar}", False),
        ("赋值后使用", "FOO=1\necho $FOO", False),
        ("for 循环变量", "for x in a; do echo $x; done", False),
        ("read 声明", "read -r line\necho $line", False),
        ("单引号里不展开", "echo '$FOO'", False),
        ("位置参数", 'echo "$1" "$@" "$?"', False),
        ("内置变量", 'echo "$PATH" "$PIPESTATUS" "$BASH_SOURCE"', False),
        ("注释里的不算", "# $FOO\necho hi", False),
    ]
    for label, snippet, should_flag in cases:
        problems = sh_syntax.check_vars(snippet)
        if should_flag:
            check("%s → 报错" % label, bool(problems), problems)
        else:
            check("%s → 通过" % label, not problems, problems)


def main():
    test_clean()
    test_mutations()
    test_snippets()
    test_cli()
    test_vars()
    print()
    if FAILED:
        print("失败 %d 项：%s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
