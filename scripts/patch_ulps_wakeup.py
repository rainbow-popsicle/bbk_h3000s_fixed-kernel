#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 DSI_SleepOut 的 ULPS_WAKEUP 从 0x22E09 改成健康态的 0x23000（验证性修复）。

=== 证据链（2026-11-27 实测 + 源码逆向）===

【逐寄存器 diff：健康态(能亮) vs 唤醒黑屏态】
    tim4 (DSI_PHY_TIMECON4)  健康=0x00023000   黑屏=0x00022e09
    其余差异(com/mode/sta/trig/hs_tx...)全是"无帧"的果，唯独 tim4 是独立硬差异。

【0x22E09 的来源：ddp_dsi.c:581 DSI_SleepOut】
    DSI_OUTREGBIT(cmdq, DSI_PHY_TIMCON4_REG,
                  DSI_REG[i]->DSI_PHY_TIMECON4, ULPS_WAKEUP, 0x22E09);
    - 全内核仅此一处写 0x22E09（grep 确认）
    - 注释 "cycle to 1ms for 520MHz" —— 但本机 PLL_CLOCK=500MHz（LOS/原厂逆向确认）
      → 按 520MHz 算出的值用在 500MHz 上，ULPS 唤醒等待时序不对
      → D-PHY 卡在 LP(1/1) 出不来 → 无帧 → event53 不来 → ESD/CMDQ 自锁 → 黑屏

【为什么健康态是 0x23000】
    开机由 LK 初始化面板（内核不开 ddp_dsi_power_on），tim4 保持 LK 的 0x23000。
    唤醒走内核 ddp_dsi_power_on → DSI_SleepOut → 改成 0x22E09 → 出问题。
    LOS 14.1 内核镜像里搜 0x00022e09 = 0 处（LOS 不写这个值）。

【0x23000 vs 0x22E09】
    0x23000 = 143360 cycles，0x22E09 = 142857 cycles（健康值更大=等更久，
    对 500MHz 更宽容，不会因等待不足出问题）。

【风险与定位】
    这是验证性补丁：若刷机后仍黑，说明 tim4 也只是"果"，可关闭开关回退。
    只改一个常量、影响面小，且可一键回退。

用法：
    python patch_ulps_wakeup.py <内核树根> [--dry-run]
开关：ULPS_WAKEUP_FIX（build.sh 默认 1）
"""

import os
import sys

MARK = "S3PRO_ULPS_FIX"

OLD = ("\t\t/* cycle to 1ms for 520MHz */\n"
       "\t\tDSI_OUTREGBIT(cmdq, DSI_PHY_TIMCON4_REG, DSI_REG[i]->DSI_PHY_TIMECON4, ULPS_WAKEUP,\n"
       "\t\t\t      0x22E09);")

NEW = ("\t\t/* S3PRO_ULPS_FIX: 原值是 MTK 按 520MHz 算的 ULPS 唤醒等待；\n"
       "\t\t * 本机 PLL_CLOCK=500MHz。实测健康态(能亮)此寄存器=0x23000，\n"
       "\t\t * 唤醒黑屏态是旧值（本函数写入）。改回健康值验证 D-PHY LP 问题。 */\n"
       "\t\tDSI_OUTREGBIT(cmdq, DSI_PHY_TIMCON4_REG, DSI_REG[i]->DSI_PHY_TIMECON4, ULPS_WAKEUP,\n"
       "\t\t\t      0x23000);")


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

    cnt = t.count(OLD)
    if cnt != 1:
        print("!! 锚点匹配 %d 次（应为 1），中止" % cnt)
        print("   （DSI_SleepOut 里 0x22E09 那段结构可能变了）")
        return 1

    t = t.replace(OLD, NEW, 1)
    print("  已把 DSI_SleepOut 的 ULPS_WAKEUP 0x22E09 -> 0x23000")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(path, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "标记存在": MARK in c,
        "0x22E09 已移除": "0x22E09" not in c,
        "0x23000 已写入": "ULPS_WAKEUP,\n\t\t\t      0x23000);" in c,
        "在 DSI_SleepOut 函数体内": ("DSI_SleepOut" in c and
                                c.find(MARK) > c.find("DSI_STATUS DSI_SleepOut") and
                                c.find(MARK) < c.find("DSI_STATUS DSI_Wakeup")),
        "花括号配平": brace_balance(c) == 0,
    }
    print()
    ok = True
    for k, v in checks.items():
        print("  %-32s %s" % (k, "OK" if v else "FAIL"))
        if not v:
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
