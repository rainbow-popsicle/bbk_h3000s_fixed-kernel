#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""让真正的 MT6397 FGADC 驱动参与编译，修好电量显示 0%。

实机证据链：
  1) /sys/class/power_supply/battery/voltage_now = 5000 µV = 5 mV
     capacity = 0，temp = 0
  2) dmesg 每 10 秒：
        AvgVbat=(5),bat_vol=(5),AvgI=(0),I=(0),VChr=(5),AvgT=(0),ZCV=(5)
        Can't find closest level, small value first
        [Battery] mt_battery_0Percent_tracking_check(), VBAT < 3400 UI_SOC = (0)
  3) 源码：battery_meter_get_battery_voltage() 里 `int val = 5;` 是"采样次数"占位，
     battery_meter_ctrl(cmd, &val) 成功后才会把电压写进 val。值一直是 5 说明调用没写回。
  4) /proc/kallsyms 里 bm_ctrl_cmd 是 `W`（weak），而 battery_meter_ctrl = bm_ctrl_cmd
     （mt_battery_meter.c:2869）→ 拿到的是 build.sh 注入的弱占位：
        __attribute__((weak)) int bm_ctrl_cmd(void *arg) { (void)arg; return 0; }
     签名都不对（真实的是 (int cmd, void *data)），永远返回 0、不写结果。
  5) 真正的实现在 drivers/power/mt81xx/mt6397_fgadc.c，
     但 drivers/power/mt81xx/Makefile 里根本没有 mt6397_fgadc.o → 从未编译。

修法：把 mt6397_fgadc.o 加进 Makefile。强符号会自动覆盖 build.sh 里的弱占位，
      于是 VBAT / 温度 / 充电电压都走真正的 PMIC FGADC。

用法：
    python patch_battery_fgadc.py <内核树根> [--dry-run]
"""
import os
import sys

LINE = "obj-$(CONFIG_MTK_SMART_BATTERY) += mt6397_fgadc.o"
MARK = "mt6397_fgadc.o"


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    mk = os.path.join(tree, "drivers", "power", "mt81xx", "Makefile")
    src = os.path.join(tree, "drivers", "power", "mt81xx", "mt6397_fgadc.c")
    if not os.path.exists(mk):
        raise SystemExit("找不到 %s" % mk)
    if not os.path.exists(src):
        raise SystemExit("找不到 mt6397_fgadc.c，本树的电源目录结构可能不同")

    t = open(mk, encoding="utf-8", errors="surrogateescape").read()
    if MARK in t:
        print("  Makefile 已含 mt6397_fgadc.o，跳过")
    else:
        # 插在第一条 mt_battery_meter.o 规则后面，保持同组
        anchor = "obj-$(CONFIG_MTK_SMART_BATTERY) += mt_battery_meter.o mt_battery_common.o mt_switch_charging.o mt_bm_data.o"
        if anchor in t:
            new = t.replace(anchor, anchor + "\n# S3PRO_BATTERY_FGADC: 真正提供 bm_ctrl_cmd() 的 MT6397 电量计 ADC 驱动\n" + LINE, 1)
        else:
            new = t.rstrip("\n") + "\n\n# S3PRO_BATTERY_FGADC\n" + LINE + "\n"
        if dry:
            print("  [dry-run] Makefile 将加入: " + LINE)
        else:
            open(mk, "w", encoding="utf-8", errors="surrogateescape", newline="").write(new)
            print("  Makefile: 已加入 mt6397_fgadc.o")

    if dry:
        return 0

    # 校验
    c = open(mk, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "Makefile 含 mt6397_fgadc.o": MARK in c,
        "mt6397_fgadc.c 存在": os.path.exists(src),
    }
    print()
    bad = 0
    for k, v in checks.items():
        print("    %-34s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1

    # 说明 bm_ctrl_cmd 的真实实现来自哪里
    src_t = open(src, encoding="utf-8", errors="surrogateescape").read()
    has_ctrl = "s32 bm_ctrl_cmd(" in src_t
    print("    %-34s %s" % ("mt6397_fgadc.c 定义了 bm_ctrl_cmd", "OK" if has_ctrl else "失败!"))
    if not has_ctrl:
        bad += 1

    if bad:
        print("\n!! 有 %d 项校验失败" % bad)
        return 1
    print("\nFGADC 补丁完成（电量计将使用真正的 PMIC ADC）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
