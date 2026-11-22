#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""移除 ddp_dsi_start() 末尾自加的 DSI_Start() 调用（原厂对比后纠错）。

背景（2026-11-21 23:35，见 docs/屏幕黑屏调查全记录.md §7.11）：
  本脚本原是"补上 DSI_Start()"的修复，依据是实机寄存器：
    开机正常 DSI_START=1 / 唤醒黑屏 DSI_START=0，其余寄存器一致。
  但定点反汇编原厂 3.18.35 的 ddp_dsi_start(0x47a6d0) 证明：
    - 原厂 ddp_dsi_start 单/双 DSI 分支【都不调用 DSI_Start】；
    - 原厂 DSI_Start 的 4 个真实调用者是
        DSI_set_cmdq_V2 / DSI_set_cmdq / ddp_dsi_trigger / ddp_dsi_switch_mode，
      其中 ddp_dsi_trigger 才是每帧视频触发时置 DSI_START 的位置（我们源码也有）。
  → 我们在 ddp_dsi_start 尾部自加 DSI_Start 属于无原厂依据的重复动作，移除它。

  注意：这不改变 DSI_Start 在 trigger 路径的调用，也不碰任何调试日志。

幂等：找不到标记块时安全跳过（视为已移除/从未加过）。
用法：
    python patch_dsi_start.py <内核树根> [--dry-run]
"""
import os
import sys

MARK = "S3PRO_DSI_START_FIX"

# 要移除的整块（注释 4 行 + 调用 + 其后空行）。锚定到 return 0 前，避免误删别处。
OLD = (
    "\t/* " + MARK + ": ddp_dsi_start 只把 DSI_START 清零，从未置 1，\n"
    "\t * 导致熄屏→唤醒后 DSI 不发送视频流（实测 DSI_START=0，屏幕全黑，\n"
    "\t * 而开机正常时该位为 1）。这里补上 DSI_Start()。 */\n"
    "\tDSI_Start(module, cmdq);\n\n"
    "\treturn 0;\n}\n\nint ddp_dsi_stop(DISP_MODULE_ENUM module, void *cmdq_handle)"
)
NEW = "\treturn 0;\n}\n\nint ddp_dsi_stop(DISP_MODULE_ENUM module, void *cmdq_handle)"


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
    if MARK not in t:
        print("  未找到 S3PRO_DSI_START_FIX 标记（已移除/从未加过），跳过")
        return 0

    cnt = t.count(OLD)
    if cnt != 1:
        print("!! 待移除块匹配 %d 次（应为 1），中止以免误删" % cnt)
        print("   （可能源码被手工改过；请人工核对 ddp_dsi_start 尾部后重试）")
        return 1

    t = t.replace(OLD, NEW, 1)
    print("  ddp_dsi_start: 已移除自加的 DSI_Start() 调用与 S3PRO_DSI_START_FIX 注释")

    if dry:
        print("  [dry-run] 未写文件")
        return 0

    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(path, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "标记已清除": MARK not in c,
        "start 尾部日志后直接 return": ("s3pro_dsi_regs(\"start\");\n\n\treturn 0;" in c),
        "trigger 路径 DSI_Start 仍在": c.count("DSI_Start(module, cmdq);") >= 1,
        "花括号配平": c.count("{") == c.count("}"),
    }
    print()
    bad = 0
    for k, v in checks.items():
        print("    %-32s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1
    if bad:
        return 1
    print("\n移除自加 DSI_Start 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
