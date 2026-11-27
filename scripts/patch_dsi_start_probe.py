#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断补丁：在 ddp_dsi_start / ddp_dsi_stop 入口打点 module 参数。

=== 目的（2026-11-27，规格书 8-lane 双 MIPI 线索）===

实测逐寄存器 diff：
    健康(亮)  dsi1 com=0x12 = DSI_EN + DSI_DUAL_EN, start=1
    黑屏      dsi1 com=0x02 = 只有 DSI_EN,         start=0

DUAL_EN 的所有写入点都严格绑定 module == DISP_MODULE_DSIDUAL：
    置1: ddp_dsi_start@3035/3036, build_cmdq START_VDO_MODE@3831/3833
    清0: ddp_dsi_stop @3086/3088, build_cmdq STOP_VDO_MODE @3809/3811

DUAL_EN 丢失 = stop 清了但 start 没置回。区分两个候选：
    A: start 收到的 module 不是 DSIDUAL（走 else 分支，不置 DUAL_EN）
    B: ESD 的 STOP/START cmdq 序列不完整

打点后刷机，熄屏→亮屏复现黑屏，抓：
    adb shell dmesg | grep S3PRO_START_PROBE

判据：
    若 start 的 is_dual=0 而 stop 的 is_dual=1 → 候选 A 实锤（module 不对称）
    若 start/stop 都是 is_dual=1 → 候选 B 或更深处（DUAL_EN 被 cmdq 路径清掉）

模块枚举参考（ddp_hal.h）：DSI0=18, DSI1=19, DSIDUAL=32。
日志 tag: [S3PRO_START_PROBE]，CHECK_MARKS 检查 S3PRO_START_PROBE。

用法：
    python patch_dsi_start_probe.py <内核树根> [--dry-run]
开关：DSI_START_PROBE（build.sh 默认 1）
"""

import os
import sys

MARK = "S3PRO_START_PROBE"

OLD_START = ("int ddp_dsi_start(DISP_MODULE_ENUM module, void *cmdq)\n"
             "{\n"
             "\tint i = 0;\n"
             "\n"
             "\tDISPFUNC();\n")

NEW_START = ("int ddp_dsi_start(DISP_MODULE_ENUM module, void *cmdq)\n"
             "{\n"
             "\tint i = 0;\n"
             "\n"
             "\tDISPFUNC();\n"
             "\tpr_alert(\"[S3PRO_START_PROBE] start module=%d is_dual=%d cmdq=%d\\n\",\n"
             "\t\t (int)module, (module == DISP_MODULE_DSIDUAL) ? 1 : 0, cmdq ? 1 : 0);\n")

OLD_STOP = ("int ddp_dsi_stop(DISP_MODULE_ENUM module, void *cmdq_handle)\n"
            "{\n"
            "\tint i = 0;\n"
            "\tunsigned int tmp = 0;\n"
            "\n"
            "\tDISPFUNC();\n")

NEW_STOP = ("int ddp_dsi_stop(DISP_MODULE_ENUM module, void *cmdq_handle)\n"
            "{\n"
            "\tint i = 0;\n"
            "\tunsigned int tmp = 0;\n"
            "\n"
            "\tDISPFUNC();\n"
            "\tpr_alert(\"[S3PRO_START_PROBE] stop  module=%d is_dual=%d cmdq=%d\\n\",\n"
            "\t\t (int)module, (module == DISP_MODULE_DSIDUAL) ? 1 : 0, cmdq_handle ? 1 : 0);\n")


def brace_balance(s):
    depth = 0
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
    return depth


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    path = os.path.join(tree, "drivers", "misc", "mediatek", "video", "mt8173",
                        "dispsys", "ddp_dsi.c")
    if not os.path.exists(path):
        raise SystemExit("找不到 ddp_dsi.c: %s" % path)
    print("  目标: %s" % os.path.relpath(path, tree))

    t = open(path, encoding="utf-8", errors="surrogateescape").read()

    if MARK in t:
        print("  已打过 %s，跳过" % MARK)
        return 0

    cs = t.count(OLD_START)
    ct = t.count(OLD_STOP)
    if cs != 1 or ct != 1:
        print("!! 锚点匹配 start=%d stop=%d（应各为 1），中止" % (cs, ct))
        return 1

    t = t.replace(OLD_START, NEW_START, 1)
    t = t.replace(OLD_STOP, NEW_STOP, 1)
    print("  已在 ddp_dsi_start / ddp_dsi_stop 入口插入 module 打点")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(path, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "标记存在": MARK in c,
        "start 打点在 ddp_dsi_start 体内": (c.find(MARK) > c.find("int ddp_dsi_start(") and
                                        c.find(MARK) < c.find("int ddp_dsi_stop(")),
        "stop 打点在 ddp_dsi_stop 体内": c.rfind(MARK) > c.find("int ddp_dsi_stop("),
        "pr_alert 两条都在": c.count("pr_alert(\"[S3PRO_START_PROBE]") == 2,
        "花括号配平": brace_balance(c) == 0,
    }
    print()
    ok = True
    for k, v in checks.items():
        print("  %-34s %s" % (k, "OK" if v else "FAIL"))
        if not v:
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
