#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态自检：防「编译通过但补丁没生效」这一类静默失效。

背景（2026-11-24 踩的坑）：

  A. patch_vdo_eof_event.py 打了、CI 绿了、内核也编出来了，
     但运行时 trig 仍是 0 —— 因为 dpmgr_path_is_idle() 把点火挡住了。
     编译期完全看不出这类问题，只能靠 dmesg 判据。

  B. patch_no_stop_loop.py 用了「三行连续文本」当锚点，
     而 patch_disp_debug.py 会在这三行中间插一大段，
     结果锚点匹配 0 次 -> CI 直接挂。
     这是「好的失败」（fail fast），但本可以更早发现。

  C. 驱动目录的 Makefile 只编三个 .o：
         obj-y += nt35523_wqxga_dsi_vdo_sharp.o
         obj-y += tps65132.o
         obj-y += panel_disp_param.o
     往目录里多放一个 .c 完全不参与编译、也不报错 —— 静默被忽略。

本测试做三件不需要编译、不需要内核树的静态检查：

  1) 关键补丁必须带「进二进制」的字符串标记
     —— 要求源码里出现 "[S3PRO_XXX] 形式的字符串字面量
     —— 只有进了 .rodata，编译后才能用 `strings Image | grep S3PRO_XXX` 验证是否真的生效
     —— 只在注释里出现的标记无法验证（patch_dsi_config_start.py 原来就是这样）

  2) 驱动目录 obj-y 清单与 inputs/ 里的 .c 文件必须一一对应
     —— Makefile 列了但 .c 不存在 -> 编译会报（提前报更清楚）
     —— 目录里有 .c 但 Makefile 没列 -> 编译期【不会报】，属于静默忽略，必须抓出来

  3) build.sh 的补丁预检清单与 scripts/ 实际文件必须一致
     —— 预检列了但文件不存在 -> CI 步骤 0 挂（已知，会报）
     —— scripts/ 有新 patch_*.py 但预检漏列 -> 该补丁永不被检查，属于盲区

用法：
    python3 tests/test_patch_marks.py
退出码 0 = 全部通过，非 0 = 有失败项。
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
INPUTS = os.path.join(ROOT, "inputs")
BUILD_SH = os.path.join(SCRIPTS, "build.sh")

# ---------------------------------------------------------------------------
# 1) 关键补丁 -> 必须进二进制的标记
#    这些是「改了显示逻辑、必须能在运行时确认是否生效」的补丁。
#    纯诊断类补丁（lcm_trace / readid / vmcmd 等）不在此列。
# ---------------------------------------------------------------------------
REQUIRED_MARKS = {
    "patch_lcm_init_table.py": "S3PRO_LCM_INIT",
    "patch_stock_timing.py": "S3PRO_TMG",
    "patch_lk_replay.py": "S3PRO_LKREPLAY",
    "patch_no_stop_loop.py": "S3PRO_NO_STOP_LOOP",
    "patch_vdo_eof_event.py": "S3PRO_VDO_EOF",
    "patch_dsi_config_start.py": "S3PRO_DSI_CFG_START",
    "patch_resume_no_wait_token.py": "S3PRO_NO_WAIT_TOKEN",
    "patch_disp_debug.py": "S3PRO_TRIGGER",
}

# 驱动目录 Makefile（复制自内核树 drivers/misc/mediatek/lcm/<panel>/Makefile）
DRIVER_MAKEFILE = os.path.join(INPUTS, "Makefile.driver")

# ---------------------------------------------------------------------------
# 4) build.sh 引用的外部资源文件（不是 patch_*.py，所以预检和标记检查都覆盖不到）
#
#    REQUIRED：缺失会让 build.sh 走到错误的兜底分支，必须先查出来。
#      - inputs/boot.img 由 .gitignore 上方注释明确要求提交（Actions 要读它），
#        缺了 build.sh 的 `if [ -f "$STOCK_BOOT" ]` 为假，会跳过整个「打包回
#        boot.img」逻辑，产出一个没有 cmdline/ramdisk/DTB 的东西。
#      - stock_tables.c / nt35523_fhd_init.c 虽然 build.sh 里都有 `|| die` 兜底，
#        但报错发生在「编译已经跑了几十分钟之后」，提前检查省一次 CI。
#
#    CONDITIONAL：只在特定开关打开时才必需，缺失只 WARN。
# ---------------------------------------------------------------------------
REQUIRED_RESOURCES = {
    "inputs/boot.img": "打包底包（提 cmdline/ramdisk/DTB）；.gitignore 明确要求提交",
    "inputs/boot.dtb": "原厂 DTB",
    "kernel-patches/lcm-table/stock_tables.c": "原厂 BOE 表（patch_lcm_stock.py --tables）",
    "kernel-patches/lcm-table/nt35523_fhd_init.c": "面板初始化表（patch_lcm_table.py --table）",
}

CONDITIONAL_RESOURCES = {
    "kernel-patches/lcm-table/sharp_tables.c": "sharp 表（仅 SHARP_TABLES=1 时用）",
    "kernel-patches/binder-3.18": "binder 现代补丁数据目录（patch_binder_modern.py）",
    "inputs/config.txt": "外部 config（可选，缺了走默认）",
    "inputs/defconfig": "外部 defconfig（可选，缺了走默认）",
    "scripts/analyze_dump.py": "workflow「体检 inputs/」步骤用",
}


def read(path):
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def check_marks(fail):
    """关键补丁必须带 "[S3PRO_XXX] 形式的字符串字面量。"""
    print("--- 1) 关键补丁的二进制标记 ---")
    for script, mark in sorted(REQUIRED_MARKS.items()):
        path = os.path.join(SCRIPTS, script)
        if not os.path.exists(path):
            print("  [FAIL] %-30s 补丁文件不存在" % script)
            fail.append("%s 不存在" % script)
            continue
        src = read(path)
        # 字符串字面量形式： "[S3PRO_XXX]  或  "S3PRO_XXX
        lit_bracket = '"[' + mark + ']'
        lit_plain = '"' + mark
        ok = (lit_bracket in src) or (lit_plain in src)
        # 额外提示：如果标记只出现在注释里，说明不可验证
        in_comment_only = False
        if ok:
            # 粗判：标记是否至少有一次出现在 pr_alert/printk/DISPMSG 参数里
            # 补丁脚本里为了生成 C 代码的 " 会写成 \" 或 \\"，
            # 所以正则要允许引号前有 0~2 个反斜杠
            has_print = re.search(
                r'(pr_alert|pr_info|pr_err|printk|DISPMSG|DISPERR|DISPCHECK)\(\s*\\{0,2}"\[?'
                + re.escape(mark),
                src,
            ) is not None
            if not has_print:
                in_comment_only = True
        if not ok:
            print("  [FAIL] %-30s 找不到字符串字面量 \"[%s]" % (script, mark))
            fail.append("%s 缺字符串标记 %s" % (script, mark))
        elif in_comment_only:
            print("  [WARN] %-30s 有标记字面量但不在 print 里，可能不进 .rodata" % script)
        else:
            print("  [ OK ] %-30s \"[%s]…\"" % (script, mark))


def parse_obj_y(text):
    """从 Makefile 文本解析 obj-y/obj-m 列的 .o 名（去 .o -> .c）。"""
    names = []
    for line in text.split("\n"):
        line = line.split("#", 1)[0]
        m = re.search(r"obj-[ym]\s*\+?=\s*(.+)", line)
        if not m:
            continue
        for tok in m.group(1).split():
            tok = tok.strip()
            if tok.endswith(".o"):
                names.append(tok[:-2] + ".c")
    # 去重保序
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def check_driver_files(fail):
    """驱动目录 Makefile 的 obj-y 与 inputs/ 实际 .c 一一对应。"""
    print("\n--- 2) 驱动目录文件清单（Makefile vs inputs/）---")
    if not os.path.exists(DRIVER_MAKEFILE):
        print("  [FAIL] 缺少 %s（应从内核树拷入）" % os.path.relpath(DRIVER_MAKEFILE, ROOT))
        fail.append("缺 Makefile.driver")
        return

    wanted = parse_obj_y(read(DRIVER_MAKEFILE))
    if not wanted:
        print("  [FAIL] Makefile.driver 里没解析到 obj-y")
        fail.append("Makefile.driver 无 obj-y")
        return
    print("  Makefile 要求编译: %s" % ", ".join(wanted))

    # 2a) Makefile 列了但文件不在
    missing = [n for n in wanted if not os.path.exists(os.path.join(INPUTS, n))]
    if missing:
        print("  [FAIL] Makefile 要编但 inputs/ 里没有: %s" % ", ".join(missing))
        fail.append("缺驱动文件: %s" % ", ".join(missing))
    else:
        print("  [ OK ] Makefile 要编的 .c 全部存在")

    # 2b) inputs/ 里有 .c 但 Makefile 没列（编译期静默忽略）
    actual = sorted(
        f for f in os.listdir(INPUTS) if f.endswith(".c")
    )
    extra = [f for f in actual if f not in wanted]
    if extra:
        print("  [FAIL] inputs/ 里有多余 .c（Makefile 没列，会被静默忽略）: %s"
              % ", ".join(extra))
        fail.append("多余驱动文件: %s" % ", ".join(extra))
    else:
        print("  [ OK ] inputs/ 里没有 Makefile 未列的 .c")


def check_preflight(fail):
    """build.sh 的补丁预检清单 vs scripts/ 实际文件。"""
    print("\n--- 3) build.sh 预检清单 vs scripts/ ---")
    if not os.path.exists(BUILD_SH):
        print("  [FAIL] 找不到 build.sh")
        fail.append("缺 build.sh")
        return

    text = read(BUILD_SH)
    m = re.search(r"for s in \\\r?\n([\s\S]*?)\r?\ndo", text)
    if not m:
        print("  [FAIL] build.sh 里没找到预检 for 循环")
        fail.append("build.sh 无预检循环")
        return

    listed = re.findall(r"([A-Za-z0-9_]+\.py)", m.group(1))
    listed_set = set(listed)
    print("  预检列出 %d 个脚本" % len(listed_set))

    # 3a) 列了但不存在
    missing = sorted(n for n in listed_set if not os.path.exists(os.path.join(SCRIPTS, n)))
    if missing:
        print("  [FAIL] 预检列了但 scripts/ 里缺失: %s" % ", ".join(missing))
        fail.append("预检缺文件: %s" % ", ".join(missing))
    else:
        print("  [ OK ] 预检列出的脚本全部存在")

    # 3b) scripts/ 里有 patch_*.py 但预检漏了。
    #     这里要区分两类，性质完全不同：
    #       - 被 build.sh 调用但没进预检 -> 真盲区（缺文件时会静默跳过，必须修）
    #       - 从未被 build.sh 调用     -> 死代码（不进预检是有意的，只提示）
    on_disk = sorted(
        f for f in os.listdir(SCRIPTS)
        if f.startswith("patch_") and f.endswith(".py")
    )
    unlisted = [f for f in on_disk if f not in listed_set]
    real_gap = []
    dead = []
    for name in unlisted:
        # 在 build.sh 里是否被引用（排除预检循环本身）
        if name in text:
            real_gap.append(name)
        else:
            dead.append(name)

    if real_gap:
        print("  [FAIL] 被 build.sh 调用但预检漏列（缺文件会静默跳过）: %s"
              % ", ".join(real_gap))
        fail.append("预检漏列(被调用): %s" % ", ".join(real_gap))
    else:
        print("  [ OK ] 没有「被调用但未预检」的补丁")

    if dead:
        print("  [WARN] 未被 build.sh 调用的历史遗留补丁（不进预检是有意的）: %s"
              % ", ".join(dead))
    else:
        print("  [ OK ] 没有死补丁")


def _strip_c_comments(text):
    """去掉 C 的块注释和行注释（块注释要跨行处理）。"""
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            # 字符串原样保留
            out.append(c)
            i += 1
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    out.append(text[i:i + 2])
                    i += 2
                    continue
                out.append(text[i])
                if text[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            # 用换行占位，保持行数
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def check_c_strings(fail):
    """补丁生成的 C 代码里，字符串字面量必须单行闭合。

    背景（2026-11-24 编译挂）：patch_dsi_config_start.py 里的 pr_alert 字符串
    写成 "…timing\\n") 少了一个反斜杠，Python 解析后 \n 变成真换行，
    生成的 C 代码成了跨行字符串：
        pr_alert("[S3PRO_DSI_CFG_START] …timing
        ");
    编译直接 error: missing terminating " character。
    test_panel_patch_chain.py 只查花括号配平，查不出这种，所以在这里补一道。
    """
    import importlib.util

    print("\n--- 4) 补丁生成的 C 代码：字符串必须单行闭合 ---")
    targets = [
        "patch_lcm_init_table.py", "patch_stock_timing.py", "patch_lk_replay.py",
        "patch_no_stop_loop.py", "patch_vdo_eof_event.py", "patch_dsi_config_start.py",
        "patch_resume_no_wait_token.py",
    ]
    bad = []
    for name in targets:
        path = os.path.join(SCRIPTS, name)
        if not os.path.exists(path):
            continue
        modname = "pm_" + name[:-3]
        try:
            spec = importlib.util.spec_from_file_location(modname, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as exc:                       # noqa: BLE001
            print("  [FAIL] %-30s import 失败: %s" % (name, exc))
            fail.append("%s import 失败" % name)
            continue

        for attr in ("NEW", "REPLACE", "IMPL"):
            val = getattr(mod, attr, None)
            if not isinstance(val, str):
                continue
            stripped = _strip_c_comments(val)
            for ln_no, ln in enumerate(stripped.split("\n"), 1):
                # 数未转义的双引号，奇数 = 没闭合
                cnt = 0
                k = 0
                while k < len(ln):
                    if ln[k] == "\\" and k + 1 < len(ln):
                        k += 2
                        continue
                    if ln[k] == '"':
                        cnt += 1
                    k += 1
                if cnt % 2 != 0:
                    bad.append((name, attr, ln_no, ln.strip()[:70]))

    if bad:
        for name, attr, ln_no, txt in bad:
            print("  [FAIL] %-30s %s 第%d行引号未闭合: %s" % (name, attr, ln_no, txt))
            fail.append("%s.%s 第%d行字符串跨行" % (name, attr, ln_no))
    else:
        print("  [ OK ] 6 个关键补丁的 C 代码字符串全部单行闭合")


def check_resources(fail):
    """build.sh 引用的外部资源文件（预检和标记检查都覆盖不到的那一类）。"""
    print("\n--- 5) build.sh 引用的外部资源 ---")

    # 4a) 必需资源
    for rel, desc in sorted(REQUIRED_RESOURCES.items()):
        p = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.exists(p):
            print("  [FAIL] %-46s 缺失（%s）" % (rel, desc))
            fail.append("缺资源: %s" % rel)
        else:
            size = os.path.getsize(p) if os.path.isfile(p) else 0
            if size == 0:
                print("  [FAIL] %-46s 存在但为空（%s）" % (rel, desc))
                fail.append("资源为空: %s" % rel)
            else:
                print("  [ OK ] %-46s %s 字节" % (rel, size))

    # 4b) 条件资源（只提示）
    for rel, desc in sorted(CONDITIONAL_RESOURCES.items()):
        p = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.exists(p):
            print("  [WARN] %-46s 不存在（%s）" % (rel, desc))
        elif os.path.isdir(p):
            n = len([x for x in os.listdir(p) if not x.startswith(".")])
            if n == 0:
                print("  [WARN] %-46s 是空目录（%s）" % (rel, desc))
            else:
                print("  [ OK ] %-46s 目录，%d 项" % (rel, n))
        else:
            print("  [ OK ] %-46s %s 字节" % (rel, os.path.getsize(p)))


def main():
    print("=" * 60)
    print("  静态自检：防「编译通过但补丁没生效」的静默失效")
    print("=" * 60)
    fail = []
    check_marks(fail)
    check_driver_files(fail)
    check_preflight(fail)
    check_c_strings(fail)
    check_resources(fail)

    print("\n" + "=" * 60)
    if fail:
        print("  结果：FAIL —— %d 项问题" % len(fail))
        for f in fail:
            print("    - %s" % f)
        return 1
    print("  结果：PASS —— 标记可验证 / 驱动清单一致 / 预检无遗漏 / 字符串闭合 / 资源齐全")
    return 0


if __name__ == "__main__":
    sys.exit(main())
