#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""让 cm36686 光感驱动在本机 DTB 上能初始化（DTS 查找失败时保留静态配置）。

=== 现象 ===
刷入带 cm36686 的内核后，dmesg：
    [Sensor dts] Device Tree: can not find alsps node!. Go to use old cust info
    <ALS/PS> cm36686_init 1903 : get_alsps_dts_func fail
    <ALS/PS> alsps_real_driver_init fail
    <ALS/PS> alsps_probe 1043 : alsps real driver init fail
→ 光感/距离传感器没注册，自动亮度用不了。

=== 根因 ===
cm36686.c 里本来就是这么写的：
    static struct alsps_hw *hw = &alsps_cust;      /* 有静态配置 */
    ...
    static int __init cm36686_init(void)
    {
        const char *name = "mediatek,CM36686";
        hw = get_alsps_dts_func(name, hw);         /* 查找失败返回 NULL，直接把 hw 冲掉 */
        if (!hw) {
            APS_ERR("get_alsps_dts_func fail\n");
            return 0;                              /* 于是一次都不尝试，直接放弃 */
        }
        alsps_driver_add(&cm36686_init_info);
    }

而本机 DTB 里 alsps 节点的 compatible 是 "mediatek,alsps"（不是
"mediatek,CM36686"），所以查找必然失败。

=== 对照成功的 LIS3DH ===
lis3dh.c 的写法就有兜底：
    hw = get_accel_dts_func(LIS3DH_DTS_COMPAT, &lis3dh_hw_builtin);
    if (!hw) {
        LIS3DH_ERR("no DTS node, using built-in defaults\n");
        hw = &lis3dh_hw_builtin;                   /* 保留静态配置 */
    }
    acc_driver_add(&lis3dh_init_info);
这也是为什么加速度计能正常工作、光感不能。

=== 修法 ===
照 LIS3DH 的模式改：DTS 查找失败时不要 return，而是把 hw 恢复成
静态配置 alsps_cust 再继续注册。这样即使 DTB 里没有匹配的
compatible，驱动仍会用编译进内核的默认参数（I2C 号/地址/阈值等）去 probe。

注意：alsps_cust 是 cust_alsps.h 里声明的全局静态配置，本文件
第 66 行的初值就是它，所以「恢复」只需要写回 &alsps_cust。

用法：
    python patch_cm36686_dts.py <内核树根> [--dry-run]
"""
import os
import sys

TARGET = os.path.join("drivers", "misc", "mediatek", "alsps",
                      "cm36686-new", "cm36686.c")

OLD = """\thw = get_alsps_dts_func(name, hw);
\tif (!hw) {
\t\tAPS_ERR("get_alsps_dts_func fail\\n");
\t\treturn 0;
\t}"""

NEW = """\t/* S3PRO: 本机 DTB 里 alsps 节点的 compatible 是 "mediatek,alsps"，
\t * 不是 "mediatek,CM36686"，所以这里必然查找失败。
\t * 原来失败就 return 0（一次都不尝试）。改成照 LIS3DH 的模式兜底：
\t * 用本文件里那份静态配置继续注册。
\t *
\t * 注意 alsps_cust 在本文件里只是 `struct alsps_hw alsps_cust;`
\t * 没有初始化（全零），直接用会因为 i2c_num=0 / i2c_addr=0 而 probe 失败，
\t * 所以在下面的补丁里同时把 I2C 总线和地址填成实测值。 */
\thw = get_alsps_dts_func(name, hw);
\tif (!hw) {
\t\tAPS_ERR("get_alsps_dts_func fail, fall back to alsps_cust\\n");
\t\thw = &alsps_cust;
\t}"""

# alsps_cust 的静态初始化（I2C-8 @ 0x1C，与 DTB 里 alsps@1c 一致）
# 标定表取自本机 DTB 的 /cust_alsps@0（compatible = "mediatek,AP3220"）真实值：
#   als_level = <0x00 0x0a 0x28 0x12c 0xb54 0x157c 0x3a98 0x4e20 0x6590 0xffff ...>
#   als_value = <0x00 0x08 0x0a 0xa0 0xe1 0x140 0x280 0x500 0xa28 0x2800 ...>
#   ps_threshold_high = 0x2a8, ps_threshold_low = 0x78
# 结构体里 als_level 只有 C_CUST_ALS_LEVEL-1 = 15 项、als_value 16 项，
# 所以按 DTB 顺序截断到对应长度。
CUST_OLD = "struct alsps_hw alsps_cust;"
CUST_NEW = """/* S3PRO: 原本这里只是一个未初始化的全局变量（全零），
 * DTS 查找失败后拿它去 probe 必然因为 i2c_num=0 而失败。
 *
 * 各字段来源：
 *   i2c_num/i2c_addr —— 裸 syscall 探针实测：i2c-4..9 中只有 bus 8 的 0x1c 有 ACK
 *   als_level/als_value/ps_threshold —— 取自本机 DTB 的 /cust_alsps@0 真实值
 *   polling_mode_* —— ALS 走定时器轮询（核心层 mod_timer 实现），PS 也走轮询，
 *                     这样即使 EINT 建不起来，两个传感器仍然能出数据
 */
struct alsps_hw alsps_cust = {
\t.i2c_num = 8,
\t.power_id = -1,
\t.power_vol = 0,
\t.polling_mode = 1,
\t.polling_mode_ps = 1,
\t.polling_mode_als = 1,
\t.i2c_addr = {0x1C, 0x48, 0x78, 0x00},
\t.als_level = {0, 10, 40, 300, 2900, 5500, 15000, 20000,
\t\t      26000, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF},
\t.als_value = {0, 8, 10, 160, 225, 320, 640, 1280,
\t\t      2600, 10240, 10240, 10240, 10240, 10240, 10240, 10240},
\t.ps_threshold = 0x2A8,
\t.ps_threshold_high = 0x2A8,
\t.ps_threshold_low = 0x78,
\t.als_threshold_high = 0xFFFF,
\t.als_threshold_low = 0x0,
\t.als_window_loss = 0,
\t.is_batch_supported_ps = false,
\t.is_batch_supported_als = false,
};"""


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

    if "S3PRO: 原本这里只是一个未初始化的全局变量" in src:
        print("[ok]  cm36686 DTS 兜底补丁已打过，跳过")
        return

    changes = []

    # ---- 1) 给 alsps_cust 填真实配置 ----
    if CUST_OLD in src:
        src = src.replace(CUST_OLD, CUST_NEW, 1)
        changes.append("alsps_cust 静态初始化")
    else:
        raise SystemExit("找不到 `struct alsps_hw alsps_cust;`，结构不符，中止")

    # ---- 2) DTS 查找失败时兜底 ----
    if OLD in src:
        src = src.replace(OLD, NEW, 1)
        changes.append("DTS 失败兜底")
    else:
        import re
        pat = re.compile(
            r'hw = get_alsps_dts_func\(name, hw\);\s*\n'
            r'\s*if \(!hw\) \{\s*\n'
            r'\s*APS_ERR\("get_alsps_dts_func fail\\n"\);\s*\n'
            r'\s*return 0;\s*\n'
            r'\s*\}')
        if not pat.search(src):
            raise SystemExit(
                "cm36686_init() 的结构和预期不符，中止（避免改错）")
        src = pat.sub(lambda m: NEW, src, count=1)
        changes.append("DTS 失败兜底")

    if len(changes) != 2:
        raise SystemExit("只完成 %d/2 项：%s" % (len(changes), ", ".join(changes)))

    if dry:
        print("[dry] 会做：%s" % ", ".join(changes))
        return

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print("[ok]  cm36686 DTS 补丁完成：%s" % ", ".join(changes))


if __name__ == "__main__":
    main()
