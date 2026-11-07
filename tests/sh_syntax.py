#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简 bash 结构检查器（启发式，不是真正的 shell 解析器）。

为什么需要它：这台开发机没有 bash、没有 WSL、也没有网络装 shellcheck，
而 scripts/build.sh 是要在 CI 里跑的。用不了 shellcheck，就自己做一个够用的
结构检查，至少保证"语法不会一上来就把整个 job 打死"。

能查：
  - 未闭合的 '  "  ` 引号
  - 未闭合的 $(  ${  （并给出行号）
  - if/then/elif/else/fi、case/in/esac、for|while|until/do/done 的配对

查不到（别指望）：
  - 命令是否存在、变量是否已定义、引号内的转义细节、语义错误

用法：
    python tests/sh_syntax.py scripts/build.sh
"""

import re
import sys

WORD_RE = re.compile(r"[^\s;&|()<>]+")
KEYWORDS = {"if", "then", "elif", "else", "fi", "case", "in", "esac",
            "for", "while", "until", "do", "done", "select"}


def _blank(out, start, end):
    for k in range(start, end):
        if out[k] != "\n":
            out[k] = " "


def skeleton(text):
    """把引号内容、注释替换成空格（保留换行），并检查 $() / {} 配对。

    返回 (骨架文本, 错误列表)。
    """
    out = list(text)
    errors = []
    stack = []          # [(kind, line)]
    i, n = 0, len(text)

    def line_of(pos):
        return text.count("\n", 0, pos) + 1

    while i < n:
        c = text[i]

        if c == "\\":
            i += 2
            continue

        if c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                errors.append("第 %d 行：单引号未闭合" % line_of(i))
                return "".join(out), errors
            _blank(out, i + 1, j)
            i = j + 1
            continue

        if c == '"':
            j = i + 1
            closed = False
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    closed = True
                    break
                j += 1
            if not closed:
                errors.append("第 %d 行：双引号未闭合" % line_of(i))
                return "".join(out), errors
            _blank(out, i + 1, j)
            i = j + 1
            continue

        if c == "`":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "`":
                    break
                j += 1
            if j >= n:
                errors.append("第 %d 行：反引号未闭合" % line_of(i))
                return "".join(out), errors
            i = j + 1
            continue

        if c == "#" and (i == 0 or text[i - 1] in " \t\n;|&"):
            j = text.find("\n", i)
            j = n if j < 0 else j
            _blank(out, i, j)
            i = j
            continue

        if c == "$" and i + 1 < n and text[i + 1] in "({":
            kind = "$(" if text[i + 1] == "(" else "${"
            stack.append((kind, line_of(i)))
            i += 2
            continue

        if c == ")" and stack and stack[-1][0] == "$(":
            stack.pop()
            i += 1
            continue

        if c == "}" and stack and stack[-1][0] == "${":
            stack.pop()
            i += 1
            continue

        i += 1

    for kind, line in stack:
        errors.append("第 %d 行：%s 未闭合" % (line, kind))
    return "".join(out), errors


def check_blocks(skel):
    """在骨架上做关键词配对检查。返回错误列表。"""
    errors = []
    stack = []          # [{'kind','line','state'}]

    for m in WORD_RE.finditer(skel):
        w = m.group(0)
        if w not in KEYWORDS:
            continue
        line = skel.count("\n", 0, m.start()) + 1

        if w == "if":
            stack.append({"kind": "if", "line": line, "state": "header"})
        elif w == "case":
            stack.append({"kind": "case", "line": line, "state": "header"})
        elif w in ("for", "while", "until", "select"):
            stack.append({"kind": "loop", "line": line, "state": "header"})
        elif w == "then":
            if not stack or stack[-1]["kind"] != "if":
                errors.append("第 %d 行：多出来的 then" % line)
            elif stack[-1]["state"] == "body":
                errors.append("第 %d 行：重复的 then" % line)
            else:
                stack[-1]["state"] = "body"
        elif w == "elif":
            if not stack or stack[-1]["kind"] != "if":
                errors.append("第 %d 行：elif 不在 if 块里" % line)
            else:
                stack[-1]["state"] = "header"
        elif w == "else":
            if not stack or stack[-1]["kind"] != "if":
                errors.append("第 %d 行：else 不在 if 块里" % line)
            else:
                stack[-1]["state"] = "body"
        elif w == "fi":
            if not stack or stack[-1]["kind"] != "if":
                errors.append("第 %d 行：多出来的 fi" % line)
            else:
                stack.pop()
        elif w == "in":
            # 只有 case 的 in 才标记状态；for x in ... 里的 in 不管
            if stack and stack[-1]["kind"] == "case" and stack[-1]["state"] == "header":
                stack[-1]["state"] = "body"
        elif w == "esac":
            if not stack or stack[-1]["kind"] != "case":
                errors.append("第 %d 行：多出来的 esac" % line)
            else:
                stack.pop()
        elif w == "do":
            if not stack or stack[-1]["kind"] != "loop":
                errors.append("第 %d 行：do 不在循环里" % line)
            else:
                stack[-1]["state"] = "body"
        elif w == "done":
            if not stack or stack[-1]["kind"] != "loop":
                errors.append("第 %d 行：多出来的 done" % line)
            else:
                stack.pop()

    for frame in stack:
        errors.append("第 %d 行：%s 块没有闭合（缺 %s）"
                      % (frame["line"], frame["kind"],
                         {"if": "fi", "case": "esac", "loop": "done"}[frame["kind"]]))
    return errors


def check_text(text):
    skel, errors = skeleton(text)
    if errors:
        return errors
    return check_blocks(skel)


# --------------------------------------------------------------------------- #
# 变量检查：set -u 下引用未定义变量 = 运行到那一行直接退出
# --------------------------------------------------------------------------- #
VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# 这些是 shell 自己提供的，或者我们明确不管的
BUILTIN_VARS = {
    "BASH_SOURCE", "BASH_REMATCH", "BASH_VERSION", "PIPESTATUS", "RANDOM",
    "LINENO", "FUNCNAME", "SECONDS", "BASHPID", "UID", "EUID", "HOSTNAME",
    "HOME", "PWD", "OLDPWD", "IFS", "PATH", "SHELL", "REPLY", "OPTARG",
    "OPTIND", "PPID", "?", "#", "@", "*", "$", "!", "-", "0",
}

DECL_RE = re.compile(
    r"(?:^|[;&|(\s])(?:for|read|local|declare|export|readonly|typeset)\s+"
    r"(?:-\S+\s+)*"
    r"([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)


def _matching_brace(text, start):
    """start 指向 '{'，返回与之配对的 '}' 的位置；找不到返回 len(text)-1。"""
    depth = 0
    i, n = start, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n - 1


def analyze_vars(text):
    """返回 (used, defined)。

    used: {name: (line, 是否写了默认值)}
    defined: set(name)
    """
    used = {}
    defined = set()
    for m in DECL_RE.finditer(text):
        defined.add(m.group(1))

    i, n = 0, len(text)
    line = 1
    while i < n:
        c = text[i]

        if c == "\n":
            line += 1
            i += 1
            continue

        if c == "\\":
            i += 2
            continue

        # 单引号里的 $ 不展开，直接跳过
        if c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                break
            line += text.count("\n", i, j)
            i = j + 1
            continue

        # 注释
        if c == "#" and (i == 0 or text[i - 1] in " \t\n;"):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue

        if c == "$":
            j = i + 1
            if j < n and text[j] == "{":
                k = _matching_brace(text, j)
                inner = text[j + 1:k]
                m = VAR_RE.match(inner)
                if m:
                    name = m.group(0)
                    rest = inner[m.end():]
                    has_default = rest.startswith((":-", "-", ":=", ":+", "+"))
                    used.setdefault(name, (line, has_default))
                i = k + 1
                continue
            if j < n and text[j] == "(":
                i = j          # 进到 $( ... ) 里继续扫
                continue
            m = VAR_RE.match(text, j)
            if m:
                used.setdefault(m.group(0), (line, False))
                i = m.end()
                continue
            i = j + 1          # $1 $@ $$ 之类
            continue

        # 赋值：NAME= 出现在单词起始位置
        if (c.isalpha() or c == "_") and (i == 0 or text[i - 1] in " \t\n;|&("):
            m = VAR_RE.match(text, i)
            if m and m.end() < n and text[m.end()] == "=":
                defined.add(m.group(0))
                i = m.end() + 1
                continue

        i += 1

    return used, defined


def check_vars(text):
    used, defined = analyze_vars(text)
    errors = []
    for name, (line, has_default) in sorted(used.items()):
        if has_default or name in defined or name in BUILTIN_VARS:
            continue
        errors.append("第 %d 行：引用了未定义的变量 $%s（set -u 下会直接退出）"
                      % (line, name))
    return errors


def check_all(text):
    """结构检查 + 变量检查。"""
    problems = check_text(text)
    if problems:
        return problems
    return check_vars(text)


def check_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return check_all(f.read())
    except OSError as exc:
        return ["读不到文件（%s）：%s" % (path, exc)]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("用法: python tests/sh_syntax.py <script.sh ...>", file=sys.stderr)
        return 2
    rc = 0
    for path in argv:
        problems = check_file(path)
        if problems:
            rc = 1
            print("[FAIL] %s" % path)
            for p in problems:
                print("       %s" % p)
        else:
            print("[ok]   %s" % path)
    return rc


if __name__ == "__main__":
    sys.exit(main())
