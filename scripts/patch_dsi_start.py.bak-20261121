#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补上 ddp_dsi_start() 缺失的 DSI_Start() 调用。

实机寄存器证据（本次抓取，同一台设备同一内核）：

  开机（能正常显示）                       唤醒后（黑屏）
  -------------------------------------    -------------------------------------
  S3PRO_REG config dsi0 A                  S3PRO_REG start dsi0 A
    start=00000001   ← DSI 在发送          start=00000000   ← DSI 没在发送
    intsta=80000000                        intsta=00000000
    com=00000012  mode=00000003            com=00000012  mode=00000003
    phylc=00000001                         phylc=00000001

  其它寄存器（COM_CTRL / MODE_CTRL / PHY_LCCON / 所有 timing）
  两边完全一致 —— 唯一差别就是 DSI_START 位。

代码原因：
  ddp_dsi_start() 是模块的 .start 回调（ddp_dsi.c 里 .start = ddp_dsi_start），
  但它只把 DSI_START 清零，从不置 1：
      DSI_OUTREGBIT(..., DSI_START, 0);      /* 只清零 */
      DSI_OUTREGBIT(..., DSI_DUAL_EN, 1);
      DSI_SetMode(...);
      DSI_clk_HS_mode(..., true);
      return 0;                               /* 没有 DSI_Start() */
  而同文件里 ddp_dsi_switch_lcm_mode() 是调用了 DSI_Start(module, cmdq_handle) 的。

开机时之所以没事：DSI_START 是 bootloader(LK) 留下来的 1，
我们内核开机路径根本不调用 ddp_dsi_start（实测开机 lcm_resume=0 次、
dsi_start=0 次）。只有熄屏再唤醒时，内核才第一次真正执行
stop → start，于是 DSI_START 被打成 0 且再也没被置回 1。

用法：
    python patch_dsi_start.py <内核树根> [--dry-run]
"""
import os
import sys

MARK = "S3PRO_DSI_START_FIX"

ANCHOR = "	return 0;\n}\n\nint ddp_dsi_stop(DISP_MODULE_ENUM module, void *cmdq_handle)"

NEW = (
    "\t/* " + MARK + ": ddp_dsi_start 只把 DSI_START 清零，从未置 1，\n"
    "\t * 导致熄屏→唤醒后 DSI 不发送视频流（实测 DSI_START=0，屏幕全黑，\n"
    "\t * 而开机正常时该位为 1）。这里补上 DSI_Start()。 */\n"
    "\tDSI_Start(module, cmdq);\n\n"
    + ANCHOR
)


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    path = os.path.join(tree, "drivers", "misc", "mediatek", "video", "mt8173",
                        "dispsys", "ddp_dsi.c")
    if not os.path.exists(path):
        raise SystemExit("找不到 ddp_dsi.c: %s" % path)

    t = open(path, encoding="utf-8", errors="surrogateescape").read()
    if MARK in t:
        print("  已打过 DSI_Start 修复，跳过")
        return 0

    cnt = t.count(ANCHOR)
    if cnt != 1:
        print("!! 锚点匹配 %d 次（应为 1），中止" % cnt)
        return 1

    t = t.replace(ANCHOR, NEW, 1)
    print("  ddp_dsi_start: 已补上 DSI_Start(module, cmdq)")

    if dry:
        print("  [dry-run] 未写文件")
        return 0

    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(path, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "标记存在": MARK in c,
        "DSI_Start 调用存在": "DSI_Start(module, cmdq);" in c,
        "花括号配平": c.count("{") == c.count("}"),
    }
    print()
    bad = 0
    for k, v in checks.items():
        print("    %-24s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1
    if bad:
        return 1
    print("\nDSI_Start 修复完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
