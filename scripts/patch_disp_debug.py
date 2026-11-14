#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""显示诊断插桩：定位"熄屏唤醒后黑屏"。

已确认的事实（实机 dmesg）：
  开机时 primary_display_resume 因 state==DISP_ALIVE 直接返回，
  面板驱动的 lcm_resume 从未被调用（lcm_resume enter = 0 次），
  开机画面依赖 bootloader 初始化好的面板状态。
  熄屏时我们的 lcm_suspend 会真的断电，唤醒时 lcm_resume 第一次真正
  负责初始化面板 —— 然后黑屏。

本补丁只在关键点加 DISPMSG 打印，不改任何逻辑：
  ddp_dsi_start / ddp_dsi_stop 结束时：MIPI TX PLL、DSI 时钟、DSI_INTSTA
  primary_display suspend/resume：状态机迁移与 lcm 回调时机

用法：
    python patch_disp_debug.py <内核树根> [--dry-run]
"""
import os
import sys

MARK = "S3PRO_DBG"

DSI_TAIL = "	return 0;\n}\n\nint ddp_dsi_stop(DISP_MODULE_ENUM module, void *cmdq_handle)"
DSI_TAIL_NEW = (
    '	DISPMSG("S3PRO_DBG dsi_start_end: mipitx=%d dsiclk=%d intsta0=0x%08x intsta1=0x%08x\\n",\n'
    '		MIPITX_IsEnabled(module, NULL), DSI_ClkIsEnabled(module, NULL),\n'
    '		INREG32(&DSI_REG[0]->DSI_INTSTA), INREG32(&DSI_REG[1]->DSI_INTSTA));\n'
    "\n" + DSI_TAIL
)

STOP_TAIL = "	return 0;\n}\n\nint ddp_dsi_switch_lcm_mode"
STOP_TAIL_NEW = (
    '	DISPMSG("S3PRO_DBG dsi_stop_end: mipitx=%d dsiclk=%d intsta0=0x%08x intsta1=0x%08x\\n",\n'
    '		MIPITX_IsEnabled(module, NULL), DSI_ClkIsEnabled(module, NULL),\n'
    '		INREG32(&DSI_REG[0]->DSI_INTSTA), INREG32(&DSI_REG[1]->DSI_INTSTA));\n'
    "\n" + STOP_TAIL
)

LCM_RESUME_BLOCK = (
    '	DISPCHECK("[POWER]lcm resume[begin]\\n");\n'
    '	disp_lcm_resume(pgc->plcm);\n'
    '	DISPCHECK("[POWER]lcm resume[end]\\n");'
)
LCM_RESUME_BLOCK_NEW = (
    '	DISPCHECK("[POWER]lcm resume[begin]\\n");\n'
    '	DISPMSG("S3PRO_DBG resume: call lcm_resume, state=%d\\n", pgc->state);\n'
    '	disp_lcm_resume(pgc->plcm);\n'
    '	DISPMSG("S3PRO_DBG resume: lcm_resume returned\\n");\n'
    '	DISPCHECK("[POWER]lcm resume[end]\\n");'
)

PD_PATCHES = [
    ('DISPCHECK("[POWER]lcm suspend[begin]\\n");\n\tdisp_lcm_suspend(pgc->plcm);',
     'DISPCHECK("[POWER]lcm suspend[begin]\\n");\n'
     '\tDISPMSG("S3PRO_DBG suspend: call lcm_suspend, state=%d\\n", pgc->state);\n'
     '\tdisp_lcm_suspend(pgc->plcm);\n'
     '\tDISPMSG("S3PRO_DBG suspend: lcm_suspend returned\\n");'),
    ('\tpgc->state = DISP_SLEPT;',
     '\tDISPMSG("S3PRO_DBG suspend: done -> SLEPT\\n");\n\tpgc->state = DISP_SLEPT;'),
    (LCM_RESUME_BLOCK, LCM_RESUME_BLOCK_NEW),
    ('DISPCHECK("[POWER]dpmgr path start[end]\\n");',
     'DISPCHECK("[POWER]dpmgr path start[end]\\n");\n'
     '\tDISPMSG("S3PRO_DBG resume: path started, state=%d\\n", pgc->state);'),
]


def patch_file(path, patches, dry, label):
    if not os.path.exists(path):
        return ["找不到文件: %s" % path]
    t = open(path, encoding="utf-8", errors="surrogateescape").read()
    if MARK in t:
        print("  %s: 已打过补丁，跳过" % label)
        return []
    problems = []
    for old, new in patches:
        cnt = t.count(old)
        if cnt != 1:
            problems.append("%s: 锚点匹配 %d 次（应为 1）: %r" % (label, cnt, old[:70]))
            continue
        t = t.replace(old, new, 1)
    if problems:
        return problems
    print("  %s: 已插入 %d 处打印" % (label, len(patches)))
    if not dry:
        open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)
    return []


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    dsi = os.path.join(tree, "drivers", "misc", "mediatek", "video", "mt8173",
                       "dispsys", "ddp_dsi.c")
    pd = os.path.join(tree, "drivers", "misc", "mediatek", "video", "mt8173",
                      "videox", "primary_display.c")

    problems = patch_file(dsi, [(DSI_TAIL, DSI_TAIL_NEW), (STOP_TAIL, STOP_TAIL_NEW)],
                          dry, "ddp_dsi.c")
    problems += patch_file(pd, PD_PATCHES, dry, "primary_display.c")

    if problems:
        print("\n!! 问题：")
        for p in problems:
            print("   - " + p)
        return 1
    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    checks = {}
    for label, path, key in (
        ("ddp_dsi.c start", dsi, "S3PRO_DBG dsi_start_end"),
        ("ddp_dsi.c stop", dsi, "S3PRO_DBG dsi_stop_end"),
        ("primary_display.c suspend", pd, "S3PRO_DBG suspend"),
        ("primary_display.c resume", pd, "S3PRO_DBG resume"),
    ):
        c = open(path, encoding="utf-8", errors="surrogateescape").read()
        checks[label] = key in c
    print()
    bad = 0
    for k, v in checks.items():
        print("    %-32s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1
    for f in (dsi, pd):
        c = open(f, encoding="utf-8", errors="surrogateescape").read()
        ok = c.count("{") == c.count("}")
        print("    %-32s %s" % (os.path.basename(f) + " 花括号", "OK" if ok else "失败!"))
        if not ok:
            bad += 1
    if bad:
        return 1
    print("\n显示诊断插桩完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
