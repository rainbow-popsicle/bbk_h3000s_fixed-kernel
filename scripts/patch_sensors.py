#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打开传感器驱动：LIS3DH 加速度计 + CM36686 光感/距离。

=== 硬件是怎么确认的 ===
设备 DTB、I2C 总线、内核配置三方对齐，并用自写的静态 ARM 探测工具
（s3pro_i2c.c，只用裸系统调用、不依赖 libc）直接读 I2C 寄存器：

  加速度计  /dev/i2c-6  地址 0x19
      reg 0x0F (WHO_AM_I) = 0x33   → ST LIS3DH 的铁证
      （LIS3DSH=0x3F、KXTJ2=0x03/0x05、BMA250=0x03 全部排除）

  光线/距离 /dev/i2c-8  地址 0x1C
      reg 0x80 = 0xEE, reg 0x92 = 0xEE, reg 0x00 = 0x00
      → Capella CM36686（STK3x1x 的 0x00 会返回 0x2x，不匹配）

=== 两处缺失，性质不同 ===
1) CM36686（光感）：Makefile 里有
       obj-$(CONFIG_MTK_CM36686_NEW) += cm36686-new/
   只是配置没打开 → 开 CONFIG_MTK_CM36686_NEW 即可。

2) LIS3DH（加速度计）：accelerometer/Makefile 里【完全没有】lis3dh/ 这一行
   （其它二十多个驱动都有 obj-$(CONFIG_MTK_XXX) += 子目录，唯独它没有），
   所以光开配置没用，必须补一行：
       obj-$(CONFIG_MTK_LIS3DH) += lis3dh/
   另外 lis3dh/Makefile 里写的是 obj-y := lis3dh.o（无条件编译），
   由父 Makefile 决定是否进入这个子目录，所以补这一行就能生效。

=== 配置的两层结构 ===
  CONFIG_CUSTOM_KERNEL_ACCELEROMETER=y   ← 打开整个传感器类别
  CONFIG_MTK_LIS3DH=y                    ← 选中具体芯片
两者都要开。

用法：
    python patch_sensors.py <内核树根> [--dry-run]
"""
import os
import re
import sys

ACCEL_MK = os.path.join("drivers", "misc", "mediatek", "accelerometer", "Makefile")
ACCEL_KCONF = os.path.join("drivers", "misc", "mediatek", "accelerometer", "Kconfig")

# 要打开的布尔配置
WANT_BOOL = [
    ("CONFIG_CUSTOM_KERNEL_ACCELEROMETER", "加速度计类别开关"),
    ("CONFIG_MTK_LIS3DH", "LIS3DH 驱动（WHO_AM_I=0x33）"),
    ("CONFIG_CUSTOM_KERNEL_ALSPS", "光感/距离类别开关"),
    ("CONFIG_MTK_CM36686_NEW", "CM36686 驱动（0x80=0xEE）"),
]

MK_LINE = "obj-$(CONFIG_MTK_LIS3DH)   +=  lis3dh/"
# 插在 lsm6ds3 那一行后面（同类驱动聚集处）
MK_ANCHOR = "obj-$(CONFIG_MTK_LSM6DS3)   +=  lsm6ds3/"

# Kconfig 里同样没有 source lis3dh，必须补上，否则 CONFIG_MTK_LIS3DH
# 这个符号根本不存在，.config 里写了也会被 Kconfig 当成非法项忽略。
KCONF_LINE = 'source "drivers/misc/mediatek/accelerometer/lis3dh/Kconfig"'
KCONF_ANCHOR = 'source "drivers/misc/mediatek/accelerometer/lsm6ds3/Kconfig"'


def set_bool(text, key, value="y"):
    """把 =y / is not set / 不存在 统一设成 key=value"""
    lines = text.split("\n")
    out = []
    done = False
    pat_eq = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    pat_unset = re.compile(r"^\s*#\s*" + re.escape(key) + r"\s+is not set\s*$")

    for ln in lines:
        if pat_eq.match(ln) or pat_unset.match(ln):
            if not done:
                out.append("%s=%s" % (key, value))
                done = True
            continue
        out.append(ln)

    if not done:
        out.append("%s=%s" % (key, value))
        done = True
    return "\n".join(out), done


def patch_makefile(text):
    """给 accelerometer/Makefile 补上 lis3dh/ 子目录"""
    if MK_LINE in text or re.search(r"lis3dh/", text):
        return text, False
    lines = text.split("\n")
    out = []
    inserted = False
    for ln in lines:
        out.append(ln)
        if not inserted and MK_ANCHOR in ln:
            out.append(MK_LINE)
            inserted = True
    if not inserted:
        # 退路：插在 endif 之前
        out2 = []
        for ln in out:
            if not inserted and ln.strip() == "endif":
                out2.append(MK_LINE)
                inserted = True
            out2.append(ln)
        out = out2
    return "\n".join(out), inserted


def patch_kconfig(text):
    """给 accelerometer/Kconfig 补上 lis3dh 的 source"""
    if KCONF_LINE in text or re.search(r"lis3dh/Kconfig", text):
        return text, False
    lines = text.split("\n")
    out = []
    inserted = False
    for ln in lines:
        if not inserted and KCONF_ANCHOR in ln:
            out.append(KCONF_LINE)
            inserted = True
        out.append(ln)
    if not inserted:
        # 退路：追加到末尾
        out.append(KCONF_LINE)
        inserted = True
    return "\n".join(out), inserted


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    cfg_path = os.path.join(tree, ".config")
    mk_path = os.path.join(tree, ACCEL_MK)
    kconf_path = os.path.join(tree, ACCEL_KCONF)
    if not os.path.isfile(cfg_path):
        raise SystemExit("找不到 %s（本项目构建从 stock-ikconfig 取配置）" % cfg_path)
    if not os.path.isfile(mk_path):
        raise SystemExit("找不到 %s" % mk_path)
    if not os.path.isfile(kconf_path):
        raise SystemExit("找不到 %s" % kconf_path)

    with open(cfg_path, "r", encoding="utf-8", errors="replace") as f:
        cfg = f.read()
    with open(mk_path, "r", encoding="utf-8", errors="replace") as f:
        mk = f.read()
    with open(kconf_path, "r", encoding="utf-8", errors="replace") as f:
        kconf = f.read()

    if (re.search(r"^CONFIG_MTK_LIS3DH=y", cfg, re.M)
            and "lis3dh/" in mk and "lis3dh/Kconfig" in kconf):
        print("[ok]  传感器补丁已打过，跳过")
        return

    changes = []
    for key, why in WANT_BOOL:
        cfg, ok = set_bool(cfg, key)
        if ok:
            changes.append(key)

    mk_new, mk_changed = patch_makefile(mk)
    if mk_changed:
        changes.append("Makefile += lis3dh/")

    # 关键：Kconfig 里没有 source lis3dh 的话，CONFIG_MTK_LIS3DH
    # 这个符号压根不存在，.config 里写了也会被当成非法项丢掉。
    kconf_new, kconf_changed = patch_kconfig(kconf)
    if kconf_changed:
        changes.append("Kconfig += source lis3dh")

    expect = len(WANT_BOOL) + 2
    if len(changes) != expect:
        raise SystemExit("只完成 %d/%d 项：%s" % (len(changes), expect, ", ".join(changes)))

    if dry:
        print("[dry] 会做：%s" % ", ".join(changes))
        return

    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(cfg)
    if kconf_changed:
        with open(kconf_path, "w", encoding="utf-8") as f:
            f.write(kconf_new)
    if mk_changed:
        with open(mk_path, "w", encoding="utf-8") as f:
            f.write(mk_new)
    print("[ok]  传感器补丁完成：%s" % ", ".join(changes))


if __name__ == "__main__":
    main()
