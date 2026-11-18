#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""让 cm36686 驱动在本机能真正 probe 成功（光感/距离传感器）。

=== 实机证据（2026-11-17，全部实测，非推测）===

1) 硬件位置：自写裸 syscall 探针扫描 i2c-4..9 × 地址 {0x1c,0x60,0x49,0x48}
       bus 8: 0x1c ACK, reg0x00=0x01     ← 只有这里应答
       其它全部无 ACK
   → 光感在 i2c-8、7 位地址 0x1c（现有静态配置正确）

2) 设备树里其实什么都有（/proc/device-tree 实测）：
       /cust_alsps@0     compatible = "mediatek,AP3220"
                         i2c_num=4  i2c_addr=0x1c/0x48/0x78
                         polling_mode_als=1  polling_mode_ps=0
                         als_level / als_value / ps_threshold_* 齐全
       /alspseint@0      compatible = "mediatek,alsps_int"
                         interrupts = <0x61 0x08>   （EINT 97, LEVEL_LOW）
                         int-gpio   = <&gpio 0x61 0>
       /soc/.../alsps@1c compatible = "mediatek,alsps"

3) 驱动里写死的两个名字都是错的：
       行1675: of_find_compatible_node(NULL, NULL, "mediatek, ALS-eint")
               ↑ 逗号后多一个空格，而且真实名字是 "mediatek,alsps_int"
               → irq_node = NULL → setup_eint 走 else 分支 → return -EINVAL
               → probe of 8-001c failed with error -22
       行1880: const char *name = "mediatek,CM36686";
               真实 cust 节点是 "mediatek,AP3220" → get_alsps_dts_func 失败
               → 由 patch_cm36686_dts.py 兜底到静态 alsps_cust（这部分已修好）

4) dmesg 实录（刷了带 cm36686 的内核后）：
       <ALS/PS> cm36686_setup_eint 1065 : Cannot find alsps pinctrl default!
       <ALS/PS> cm36686_setup_eint 1099 : null irq node!!
       <ALS/PS> cm36686_init_client 1523 : setup eint: -22
       cm36686: probe of 8-001c failed with error -22

5) ALS 其实不依赖 EINT：核心层 alsps.c 用定时器轮询
       mod_timer(&cxt->timer_als, jiffies + delay_als/(1000/HZ));
       → 只要 probe 成功、polling_mode_als=1，ALS 就会周期读 I2C 出数据
   EINT 只用于 PS 阈值中断。

=== 本补丁做四件事 ===

A. 修 irq_node 的 compatible 名字 → 找到 alspseint@0，EINT 97 能映射
B. 保护 debounce 段：DTB 的 alspseint@0 没有 "debounce" 属性，
   原代码会把 ints[0]=0 传给 gpio_request(0)，等于乱抓 GPIO 0。
   改成「只有真读到 debounce 才碰 GPIO」。
C. EINT 失败不再致命 → 即使 A/B 在你机器上仍不成立，probe 也能成功，
   ALS/PS 走轮询。
D. （配套）patch_cm36686_dts.py 里的 alsps_cust 标定表换成 DTS 里的真实值。

用法：
    python patch_alsps_eint.py <内核树根> [--dry-run]
"""
import os
import sys

TARGET = os.path.join("drivers", "misc", "mediatek", "alsps",
                      "cm36686-new", "cm36686.c")

# ---------------------------------------------------------------- A. irq_node
A_OLD = '\tobj->irq_node = of_find_compatible_node(NULL, NULL, "mediatek, ALS-eint");'

A_NEW = """\t/* S3PRO: 原代码写的是 "mediatek, ALS-eint"（逗号后多一个空格，
\t * 而且名字本身也不对）。本机 DTB 的 EINT 节点是：
\t *     /alspseint@0 { compatible = "mediatek,alsps_int";
\t *                    interrupts = <0x61 0x08>; int-gpio = <&gpio 0x61 0>; }
\t * 名字对不上 → irq_node 为 NULL → setup_eint 直接 return -EINVAL
\t * → probe of 8-001c failed with error -22，光感根本没注册。 */
\tobj->irq_node = of_find_compatible_node(NULL, NULL, "mediatek,alsps_int");
\tif (!obj->irq_node)
\t\tobj->irq_node = of_find_compatible_node(NULL, NULL, "mediatek, ALS-eint");"""

# ---------------------------------------------------------------- B. debounce
B_OLD = """\tif (cm36686_obj->irq_node) {
\t\tof_property_read_u32_array(cm36686_obj->irq_node, "debounce", ints,
\t\t\t\t\t   ARRAY_SIZE(ints));
\t\tgpio_request(ints[0], "p-sensor");
\t\tgpio_set_debounce(ints[0], ints[1]);
\t\tAPS_LOG("ints[0] = %d, ints[1] = %d!!\\n", ints[0], ints[1]);
"""

B_NEW = """\tif (cm36686_obj->irq_node) {
\t\t/* S3PRO: 本机 DTB 的 alspseint@0 只有 int-gpio，没有 "debounce" 属性。
\t\t * 原代码在读不到 debounce 时 ints[] 保持 {0,0}，接着就
\t\t * gpio_request(0) —— 那会去抢 GPIO 0，非常危险。
\t\t * 改成：读到 debounce 才操作 GPIO，读不到就跳过（不影响中断）。 */
\t\tif (of_property_read_u32_array(cm36686_obj->irq_node, "debounce", ints,
\t\t\t\t\t       ARRAY_SIZE(ints)) == 0) {
\t\t\tgpio_request(ints[0], "p-sensor");
\t\t\tgpio_set_debounce(ints[0], ints[1]);
\t\t\tAPS_LOG("debounce gpio=%d, ms=%d\\n", ints[0], ints[1]);
\t\t} else {
\t\t\tAPS_LOG("no debounce prop, skip gpio config\\n");
\t\t}
"""

# ---------------------------------------------------------------- C. 非致命
C_OLD = """\tres = cm36686_setup_eint(client);
\tif (res != 0) {
\t\tAPS_ERR("setup eint: %d\\n", res);
\t\treturn res;
\t}"""

C_NEW = """\tres = cm36686_setup_eint(client);
\tif (res != 0) {
\t\t/* S3PRO: EINT 建不起来不再致命。
\t\t * 依据：核心层 alsps.c 的 ALS 数据是【定时器轮询】出来的
\t\t *       （mod_timer(&cxt->timer_als, ...) → 周期调 als_get_data() 读 I2C），
\t\t *       不依赖 EINT；PS 在本机静态配置里也是 polling_mode_ps=1。
\t\t *       原来这里 return res 直接让 probe 失败，光感就永远不出现。 */
\t\tAPS_LOG("setup eint failed(%d), continue in polling mode\\n", res);
\t}"""

MARKER = "S3PRO: 原代码写的是"


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

    if MARKER in src:
        print("[ok]  alsps EINT 补丁已打过，跳过")
        return

    changes = []

    if A_OLD in src:
        src = src.replace(A_OLD, A_NEW, 1)
        changes.append("A:irq_node 名字")
    else:
        raise SystemExit("A 段没匹配上（irq_node 那行），中止以免改错")

    if B_OLD in src:
        src = src.replace(B_OLD, B_NEW, 1)
        changes.append("B:debounce 保护")
    else:
        raise SystemExit("B 段没匹配上（debounce 段），中止以免改错")

    if C_OLD in src:
        src = src.replace(C_OLD, C_NEW, 1)
        changes.append("C:EINT 非致命")
    else:
        raise SystemExit("C 段没匹配上（init_client 的 setup_eint），中止")

    if dry:
        print("[dry] 会改：%s" % ", ".join(changes))
        return

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print("[ok]  alsps EINT 补丁完成：%s" % ", ".join(changes))


if __name__ == "__main__":
    main()
