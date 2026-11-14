#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用原厂内核提取出的完整初始化表，替换我们面板驱动里的残缺表。

为什么必须换（实机证据链）：
  1) 开机画面能显示，但 dmesg 显示 primary_display_resume 因 state==DISP_ALIVE
     直接返回，lcm_resume 从未被调用（lcm_resume enter = 0 次）——
     也就是开机画面来自 bootloader 初始化好的面板状态。
  2) 熄屏时我们的 lcm_suspend 真的断电；唤醒时 lcm_resume 第一次真正
     负责初始化面板 → 黑屏。
  3) 加装诊断打印后确认：唤醒后 mipitx=2 dsiclk=2，DSI 不 busy，
     面板 GPIO(985/987/1016) 全部为高，SurfaceFlinger 也在正常出图
     —— 硬件通路全部正常，唯独面板不显示。
  4) 从原厂 7.0 内核（以及作者的 #113 内核，两份完全一致）抠出的初始化表
     共 109 项，含面板寄存器配置 + 25 条 gamma 曲线 + display-on(0x29)；
     我们驱动里的表只有 12 项且内容不同。

补充说明：原厂用 REGFLAG_DELAY=0xFFFC / END_OF_TABLE=0xFFFD，
           我们的驱动用 0xFE / 0xFF，数据文件里已按我们的宏名生成。

用法：
    python patch_lcm_table.py <内核树根> [--table <表文件>] [--dry-run]
"""
import os
import re
import sys

MARK = "S3PRO_LCM_TABLE_FULL"


def find_panel(tree):
    name = None
    cfg = os.path.join(tree, ".config")
    if os.path.exists(cfg):
        for line in open(cfg, encoding="utf-8", errors="replace"):
            if line.startswith("CONFIG_CUSTOM_KERNEL_LCM="):
                name = line.split("=", 1)[1].strip().strip('"')
                break
    base = os.path.join(tree, "drivers", "misc", "mediatek", "lcm")
    if name:
        d = os.path.join(base, name)
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.endswith(".c"):
                    p = os.path.join(d, f)
                    t = open(p, encoding="utf-8", errors="replace").read()
                    if "lcm_initialization_setting[]" in t:
                        return p
    for root, _d, files in os.walk(base):
        for f in files:
            if not f.endswith(".c"):
                continue
            p = os.path.join(root, f)
            t = open(p, encoding="utf-8", errors="replace").read()
            if "lcm_initialization_setting[]" in t and "lcm_get_params" in t:
                return p
    return None


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    table_file = os.path.join(root, "kernel-patches", "lcm-table", "nt35523_fhd_init.c")
    for i, a in enumerate(sys.argv):
        if a == "--table" and i + 1 < len(sys.argv):
            table_file = sys.argv[i + 1]

    if not os.path.exists(table_file):
        raise SystemExit("找不到表文件: %s" % table_file)
    table = open(table_file, encoding="utf-8").read().rstrip() + "\n"

    path = find_panel(tree)
    if not path or not os.path.exists(path):
        raise SystemExit("找不到面板驱动文件（可用 --file 指定）")
    print("  目标驱动: %s" % os.path.relpath(path, tree))

    t = open(path, encoding="utf-8", errors="surrogateescape").read()
    if MARK in t:
        print("  已替换过（找到 %s），跳过" % MARK)
        return 0

    # 定位旧表的完整定义
    m = re.search(r"(static\s+struct\s+LCM_setting_table\s+lcm_initialization_setting\[\]\s*=\s*\{.*?\n\};)",
                  t, re.S)
    if not m:
        raise SystemExit("找不到 lcm_initialization_setting[] 的定义")
    old_block = m.group(1)
    old_items = old_block.count("},")
    print("  旧表: %d 行 / 约 %d 项" % (old_block.count("\n") + 1, old_items))

    # 新表：标记注释放最前面（表文件可能以多行注释开头，不能拆行处理，
    # 否则会产生嵌套注释 /* ... /* ... */，C 语言不允许）
    new_table = ("/* %s: 由 build.sh 用 LK/原厂内核提取的完整面板初始化表替换 */\n%s"
                 % (MARK, table.rstrip()))

    new_t = t.replace(old_block, new_table.rstrip(), 1)
    new_items = new_table.count("},")
    print("  新表: %d 行 / 约 %d 项" % (new_table.count("\n") + 1, new_items))

    if dry:
        print("  [dry-run] 未写文件")
        return 0

    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(new_t)

    c = open(path, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "标记已写入": MARK in c,
        "包含 display-on(0x29)": re.search(r"\{0x29,", c) is not None,
        "包含 sleep-out(0x11)": re.search(r"\{0x11,", c) is not None,
        "包含 gamma 表(D1)": re.search(r"\{0xD1,", c) is not None,
        "表项数 >= 100": c.count("},") >= 100,
        "花括号配平": c.count("{") == c.count("}"),
    }
    print()
    bad = 0
    for k, v in checks.items():
        print("    %-28s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1
    if bad:
        print("\n!! 有 %d 项校验失败" % bad)
        return 1
    print("\n面板初始化表已替换为完整版（原厂提取）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
