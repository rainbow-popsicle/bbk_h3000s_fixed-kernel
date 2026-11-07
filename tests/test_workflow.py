#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Actions 工作流的静态检查（不需要 PyYAML，也不联网）。

写这个的原因很实在：这台机器没装 PyYAML、也连不上网，而我在编辑
build-kernel.yml 时用批量替换把某个 input 的 description/default 两行
整个吃掉了，剩下的 YAML 依然"语法合法"——真正的 CI 要到第一次运行才发现。
所以这里针对这个文件做结构化校验。

检查项：
  1. 没有 TAB、没有 CRLF、没有行尾空格（YAML 三大杀手）
  2. workflow_dispatch.inputs 下每个输入都有 description，且类型与默认值自洽
  3. 每个 `run: |` 后面都跟了更深缩进的块
  4. workflow 里引用的 inputs.* 都在 inputs 里声明过
  5. job 步骤里出现的 run 命令引用的脚本文件确实存在
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "build-kernel.yml")

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILED.append(name)


def load():
    with open(WORKFLOW, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8")
    return raw, text


def test_basics(raw, text):
    print("[1] 基础格式")
    check("无 TAB", "\t" not in text)
    check("无 CRLF", "\r\n" not in text)
    check("无行尾空格", all(l == l.rstrip() for l in text.split("\n")))
    check("无 BOM", not raw.startswith(b"\xef\xbb\xbf"))


def parse_inputs(lines):
    """返回 {input_name: {'indent': n, 'keys': {key: value}}}"""
    inputs = {}
    in_inputs = False
    cur = None
    for i, line in enumerate(lines):
        if re.match(r"^\s{4}inputs:\s*$", line):
            in_inputs = True
            continue
        if in_inputs:
            if line.strip() and not line.startswith("      "):
                break
            m = re.match(r"^ {6}([A-Za-z_][A-Za-z0-9_]*):\s*$", line)
            if m:
                cur = m.group(1)
                inputs[cur] = {"line": i + 1, "keys": {}}
                continue
            m = re.match(r"^ {8}([A-Za-z_]+):\s*(.*)$", line)
            if m and cur:
                inputs[cur]["keys"][m.group(1)] = m.group(2).strip()
    return inputs


def test_inputs(lines, text):
    print("[2] workflow_dispatch.inputs 结构")
    inputs = parse_inputs(lines)
    check("解析到输入项", len(inputs) >= 4, sorted(inputs))

    for name, info in inputs.items():
        keys = info["keys"]
        check("%s 有 description" % name, "description" in keys,
              "第 %d 行的输入缺 description" % info["line"])
        # choice 类型必须有 options，boolean 必须有默认值
        typ = keys.get("type", "")
        if typ.startswith("choice"):
            check("%s 声明了 options" % name, "options" in keys)
        if typ.startswith("boolean"):
            check("%s 的 default 是 true/false" % name,
                  keys.get("default") in ("true", "false"), keys.get("default"))
        # 非 boolean/choice 的输入应当有 default 或 required: true
        if not typ and "default" not in keys and "required" not in keys:
            check("%s 缺 default/required" % name, False)

    used = set(re.findall(r"inputs\.([A-Za-z_][A-Za-z0-9_]*)", text))
    unknown = used - set(inputs)
    check("引用的 inputs 都已声明", not unknown, unknown)


def test_run_blocks(lines):
    print("[3] run 块缩进")
    bad = []
    for i, line in enumerate(lines):
        if line.strip().endswith("run: |"):
            base = len(line) - len(line.lstrip())
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if not nxt.strip() or (len(nxt) - len(nxt.lstrip())) <= base:
                bad.append(i + 1)
    check("每个 run: | 都有缩进块", not bad, bad)


def test_scripts_exist(text):
    print("[4] 引用的脚本存在")
    for rel in ("scripts/build.sh", "scripts/analyze_dump.py",
                "tests/test_bootimg.py", "tests/test_sh_syntax.py"):
        if rel in text:
            check("%s 存在" % rel, os.path.isfile(os.path.join(ROOT, rel)))


def main():
    raw, text = load()
    lines = text.split("\n")
    print("工作流: %s（%d 行）" % (os.path.relpath(WORKFLOW, ROOT), len(lines)))
    test_basics(raw, text)
    test_inputs(lines, text)
    test_run_blocks(lines)
    test_scripts_exist(text)
    print()
    if FAILED:
        print("失败 %d 项：%s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
