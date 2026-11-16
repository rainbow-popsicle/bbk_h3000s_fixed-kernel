#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把电池容量（Qmax）改成 S3 Pro 的真实值 7200mAh。

=== 现状 ===
设备 DTB 里：
    /proc/device-tree/mediatek,battery_meter
        battery = "ariel_battery"
dmesg 确认：
    battery_meter mediatek,battery_meter: match battery profile: ariel_battery
所以运行时走的是 drivers/power/mt81xx/mt_bm_data.c 里的
    ariel8_custom_battery_init()

而这个函数里写着（作者自己留的 todo）：
    /* todo: should update ariel8 battery capacity here */
    p_meter_data->q_max_pos_50 = 4000;
    p_meter_data->q_max_pos_25 = 4000;
    p_meter_data->q_max_pos_0  = 4000;
    p_meter_data->q_max_neg_10 = 4000;

=== 为什么这是电量错乱的根源 ===
本机电池是 7200mAh，但内核按 4000mAh 累积库仑计：
    DOD = DOD0 - (已放出电荷 × 100 / Qmax)
用 4000 当分母，放掉 4000mAh 就认为 100% 放完，
可实际还有 3200mAh（44%）——于是出现：
    · 显示 66% 却一夜关机
    · 20 分钟充电只涨 1%
    · 拔电瞬间电压跌落就断电
    · 重启后电量跳成 100%（RTC 里存的是错值）

=== 顺带确认过的事实 ===
· cappu（小米平板3）内核树里 default 值也是 3029/2989，
  说明这是 MTK 的通用占位默认值，不是本机真实容量。
· 本机 nvram 里只有 BT_Addr / WIFI，没有电池参数，
  所以运行期不会从 nvram 拿到正确容量 → 只能改源码。
· ariel8 的 OCV 曲线表本身也是借用 p1v1 的（另一处 todo），
  但下面只先修容量，曲线表等容量生效后再看是否需要动。

用法：
    python patch_battery_qmax.py <内核树根> [--dry-run]
"""
import os
import re
import sys

TARGET = os.path.join("drivers", "power", "mt81xx", "mt_bm_data.c")

# 本机真实电池容量（7200mAh 原装）
QMAX = 7200
# 大电流档留 2% 余量，与 MTK 原写法一致（4000/3950 的比例）
QMAX_H = 7050

# ariel8 函数体里要替换的 8 行
REPLACEMENTS = [
    ("p_meter_data->q_max_pos_50 = 4000;",
     "p_meter_data->q_max_pos_50 = %d;" % QMAX),
    ("p_meter_data->q_max_pos_50_h_current = 3950;",
     "p_meter_data->q_max_pos_50_h_current = %d;" % QMAX_H),
    ("p_meter_data->q_max_pos_25 = 4000;",
     "p_meter_data->q_max_pos_25 = %d;" % QMAX),
    ("p_meter_data->q_max_pos_25_h_current = 3950;",
     "p_meter_data->q_max_pos_25_h_current = %d;" % QMAX_H),
    ("p_meter_data->q_max_pos_0 = 4000;",
     "p_meter_data->q_max_pos_0 = %d;" % QMAX),
    ("p_meter_data->q_max_pos_0_h_current = 3950;",
     "p_meter_data->q_max_pos_0_h_current = %d;" % QMAX_H),
    ("p_meter_data->q_max_neg_10 = 4000;",
     "p_meter_data->q_max_neg_10 = %d;" % QMAX),
    ("p_meter_data->q_max_neg_10_h_current = 3950;",
     "p_meter_data->q_max_neg_10_h_current = %d;" % QMAX_H),
]

MARK = "S3PRO: 本机电池 7200mAh"


def patch_ariel8(src):
    """只在 ariel8_custom_battery_init 函数体内替换，避免误伤 p1v1"""
    start = src.find("void ariel8_custom_battery_init(")
    if start < 0:
        return src, 0
    # 函数结束：下一个顶层 'struct mt_bm_item' 之前
    end = src.find("struct mt_bm_item", start)
    if end < 0:
        end = len(src)
    body = src[start:end]

    n = 0
    for old, new in REPLACEMENTS:
        if old in body:
            body = body.replace(old, new, 1)
            n += 1

    if n == 0:
        return src, 0

    # 在函数开头插入说明
    anchor = "\t/* todo: should update ariel8 board value here */"
    note = (
        "\t/* %s\n"
        "\t * 原值是 MTK 通用的 4000mAh 占位值（作者留了 todo），\n"
        "\t * 按实际电池改成 %d mAh，否则库仑计按 4000 累积会导致\n"
        "\t * 电量掉得比真实快、充满显示不到 100%%、拔电瞬间误判断电。 */\n"
        % (MARK, QMAX))
    if anchor in body and MARK not in body:
        body = body.replace(anchor, note + anchor, 1)

    return src[:start] + body + src[end:], n


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
        print("[ok]  Qmax 补丁已打过，跳过")
        return

    new_src, n = patch_ariel8(src)
    if n != 8:
        raise SystemExit(
            "ariel8_custom_battery_init 里只匹配到 %d/8 处 Qmax，结构不符，中止" % n)

    if dry:
        print("[dry] 会把 ariel_battery 的 8 处 Qmax 改成 %d / %d mAh"
              % (QMAX, QMAX_H))
        return

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_src)
    print("[ok]  Qmax 补丁完成：ariel_battery 8 处已改为 %d / %d mAh"
          % (QMAX, QMAX_H))


if __name__ == "__main__":
    main()
