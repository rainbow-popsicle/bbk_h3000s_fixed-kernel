#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""让 cm36686 光感驱动能在这棵内核树上编译（补 I2C 扩展宏的兜底定义）。

=== 编译错误 ===
    drivers/misc/mediatek/alsps/cm36686-new/cm36686.c:201:19:
        error: 'I2C_MASK_FLAG' undeclared
    ...同处还有 I2C_WR_FLAG、I2C_RS_FLAG

=== 原因 ===
这三个宏在 include/linux/i2c.h 里，但整段被
    #ifdef CONFIG_MTK_I2C_EXTENSION
    #define I2C_MASK_FLAG (0x00ff)
    #define I2C_WR_FLAG   (0x1000)
    #define I2C_RS_FLAG   (0x0800)
    ...
    #endif
包着。而 drivers/misc/mediatek/i2c/Kconfig 里：
    config MTK_I2C_EXTENSION
        def_bool y if (ARCH_MT6735 || ARCH_MT6735M || ARCH_MT6753 ||
                       ARCH_MT6580 || ARCH_MT6755 || ARCH_MT6570)
MT8173 不在这个列表里 → 该选项默认关闭 → 宏不存在。

=== 为什么不直接打开 CONFIG_MTK_I2C_EXTENSION ===
那会改变整个 MTK I2C 驱动的消息处理路径（它给 client->addr 塞
DMA/方向等标志位，属于平台定制行为）。本机触摸屏 gt9xx、
各种传感器都挂在同一条 I2C 上，贸然打开风险很大。

    === 采取的做法 ===
只在 cm36686.c 里给出与 i2c.h 完全一致的兜底定义（#ifndef 保护，
若将来该选项被打开，用的仍是内核里的原定义，不会重复定义）。
数值直接照抄 i2c.h：
    I2C_MASK_FLAG = 0x00ff
    I2C_WR_FLAG   = 0x1000
    I2C_RS_FLAG   = 0x0800

用到的位置全部集中在 CM36686_i2c_master_operate() 一个函数里，
共 6 处（4 次 &= MASK、1 次 |= WR、1 次 |= RS），语义保持不变。

用法：
    python patch_cm36686_i2c.py <内核树根> [--dry-run]
"""
import os
import re
import sys

TARGET = os.path.join("drivers", "misc", "mediatek", "alsps",
                      "cm36686-new", "cm36686.c")

# 兜底定义：与 include/linux/i2c.h 里的值完全一致
FALLBACK = """
/* S3PRO: 本平台未启用 CONFIG_MTK_I2C_EXTENSION（MT8173 不在它的
 * def_bool 列表里），导致 include/linux/i2c.h 里的
 * I2C_MASK_FLAG / I2C_WR_FLAG / I2C_RS_FLAG 不存在，
 * 而本文件 CM36686_i2c_master_operate() 要用到它们。
 * 这里按 i2c.h 的原值补一份兜底定义；若将来该选项被打开，
 * 内核里的定义先生效，这里不会重复定义。
 * 选择补定义而不是打开 CONFIG_MTK_I2C_EXTENSION，是因为后者会改变
 * 整条 I2C 驱动的消息处理路径，而触摸屏等设备共用同一条总线。 */
#ifndef I2C_MASK_FLAG
#define I2C_MASK_FLAG	(0x00ff)
#endif
#ifndef I2C_WR_FLAG
#define I2C_WR_FLAG	(0x1000)
#endif
#ifndef I2C_RS_FLAG
#define I2C_RS_FLAG	(0x0800)
#endif
"""

# 插在最后一个 #include 之后（保证 i2c.h 已先包含，兜底才安全）
ANCHOR_RE = re.compile(r'^#include\s+[<"].*[>"]\s*$', re.M)
MARK = "S3PRO: 本平台未启用 CONFIG_MTK_I2C_EXTENSION"


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    path = os.path.join(tree, TARGET)
    if not os.path.isfile(path):
        raise SystemExit("找不到 %s" % path)

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        src = f.read()

    if MARK in src:
        print("[ok]  cm36686 I2C 宏补丁已打过，跳过")
        return

    if not re.search(r'\bI2C_MASK_FLAG\b', src):
        print("[ok]  本文件没有用到 I2C_MASK_FLAG，无需补丁")
        return

    # 找最后一个 #include 的位置
    last = None
    for m in ANCHOR_RE.finditer(src):
        last = m
    if last is None:
        raise SystemExit("cm36686.c 里找不到 #include，结构不符，中止")

    insert_at = last.end()
    new_src = src[:insert_at] + "\n" + FALLBACK + src[insert_at:]

    if dry:
        print("[dry] 会在第 %d 个字符处插入 3 个宏的兜底定义"
              % insert_at)
        return

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_src)
    print("[ok]  cm36686 I2C 宏补丁完成（插入 I2C_MASK_FLAG/WR_FLAG/RS_FLAG 兜底定义）")


if __name__ == "__main__":
    main()
