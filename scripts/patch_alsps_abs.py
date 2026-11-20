#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 ALSPS 核心层的光感上报从 EV_REL 改成 EV_ABS。

=== 为什么要改（实机证据，2026-11-20 实测）===

1) 设备上 m_alsps_input 的能力位：
       B: REL=107     （bit0 REL_X、bit1 REL_Y、bit2 REL_Z、bit8）
       B: ABS=101     （bit0 ABS_X、bit8 ABS_WHEEL）
   REL 含 bit0/bit8 正是 alsps.c（EV_REL 版）的特征；
   若是 alsps_module.c（EV_ABS 版），REL 只会是 0x6（只有 PS 的 bit1/bit2）。

2) 但头文件里事件语义本来就是 ABS 的：
       inc/alsps.h:  EVENT_TYPE_ALS_STATUS = ABS_WHEEL
   也就是说「名字是 ABS 轴，却用 input_report_rel 发出去」——语义错位。

3) HAL 那侧读的是这个 input 设备：
       sensors HAL 打开 /dev/hwmsensor（控制）
       event4（m_alsps_input）由持有传感器 HAL 的进程打开（数据）
   而框架侧一直拿不到非零值：
       mRecentLightSamples 持续增长，但 mAmbientLux = mLastObservedLux = 0.0
       sensorservice: LIGHT 事件全是 0.00
   → 事件在流，但按 EV_REL 发，按 EV_ABS 收，等于永远收不到有效 lux。

=== 本补丁做三处最小改动（与 alsps_module.c 的既有写法对齐）===

A. als_data_report：ALS_VALUE / ALS_STATUS 的 input_report_rel → input_report_abs
   （PS 仍保持 input_report_rel，因为 REL_Z / REL_Y 本来就是相对轴，且 PS
     在本机是 polling 模式、与本问题无关）
B. alsps_input_init：ALS 两项的 input_set_capability 从 EV_REL 改成 EV_ABS
   （EV_ABS 的 evbit 由 input_set_capability 自动置位；
     absbit/absmin/absmax 已由紧随其后的 input_set_abs_params 负责）
C. inc/alsps.h：EVENT_TYPE_ALS_VALUE 由 REL_X 改为 ABS_X
   （数值同为 0，不改 ABI；只是把语义写对，和 alsps_module.h /
    sensor_bio/alsps/inc/alsps.h 保持一致）

注意：EVENT_TYPE_ALS_VALUE 与 ABS_X 数值都是 0，所以 A 段即使不改头文件
也能工作；改头文件纯粹是为了让代码不再自相矛盾。

用法：
    python patch_alsps_abs.py <内核树根> [--dry-run]
"""
import os
import re
import sys

ALSPS_C = os.path.join("drivers", "misc", "mediatek", "alsps", "alsps.c")
ALSPS_H = os.path.join("drivers", "misc", "mediatek", "alsps", "inc", "alsps.h")

# ---------------------------------------------------------------- A. 上报方式
A_OLD = (
    "\tif (cxt->is_get_valid_als_data_after_enable == false) {\n"
    "\t\tinput_report_rel(dev, EVENT_TYPE_ALS_VALUE, value+1);\n"
    "\t\tcxt->is_get_valid_als_data_after_enable = true;\n"
    "\t}\n"
    "\tinput_report_rel(dev, EVENT_TYPE_ALS_VALUE, value);\n"
    "\tinput_report_rel(dev, EVENT_TYPE_ALS_STATUS, status);\n"
)

A_NEW = (
    "\tif (cxt->is_get_valid_als_data_after_enable == false) {\n"
    "\t\tinput_report_abs(dev, EVENT_TYPE_ALS_VALUE, value+1);\n"
    "\t\tcxt->is_get_valid_als_data_after_enable = true;\n"
    "\t}\n"
    "\tinput_report_abs(dev, EVENT_TYPE_ALS_VALUE, value);\n"
    "\tinput_report_abs(dev, EVENT_TYPE_ALS_STATUS, status);\n"
)

# ---------------------------------------------------------------- B. 能力位
B_OLD = (
    "\tinput_set_capability(dev, EV_REL, EVENT_TYPE_ALS_VALUE);\n"
    "\tinput_set_capability(dev, EV_REL, EVENT_TYPE_ALS_STATUS);\n"
)

B_NEW = (
    "\tinput_set_capability(dev, EV_ABS, EVENT_TYPE_ALS_VALUE);\n"
    "\tinput_set_capability(dev, EV_ABS, EVENT_TYPE_ALS_STATUS);\n"
)

# ---------------------------------------------------------------- C. 头文件
C_RE = re.compile(r"(#define\s+EVENT_TYPE_ALS_VALUE\s+)REL_X")
C_ABS_RE = re.compile(r"#define\s+EVENT_TYPE_ALS_VALUE\s+ABS_X")

# 幂等标记：一旦 A 段已被改过，就认为整体已打过
MARK = "input_report_abs(dev, EVENT_TYPE_ALS_VALUE, value)"


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    path_c = os.path.join(tree, ALSPS_C)
    path_h = os.path.join(tree, ALSPS_H)
    for p in (path_c, path_h):
        if not os.path.isfile(p):
            raise SystemExit("找不到 %s" % p)

    # 二进制读写：避免 Windows 文本模式把 LF 改成 CRLF，
    # 保证本地跑和 Linux CI 跑出来的文件逐字节一致。
    with open(path_c, "rb") as f:
        src_c = f.read().decode("utf-8", "surrogateescape")
    with open(path_h, "rb") as f:
        src_h = f.read().decode("utf-8", "surrogateescape")

    if MARK in src_c:
        print("[ok]  ALSPS ABS 补丁已打过，跳过")
        return

    changes = []

    if A_OLD in src_c:
        src_c = src_c.replace(A_OLD, A_NEW, 1)
        changes.append("A:ALS 上报 rel->abs")
    else:
        raise SystemExit("A 段没匹配上（als_data_report 的上报三行），中止以免改错")

    if B_OLD in src_c:
        src_c = src_c.replace(B_OLD, B_NEW, 1)
        changes.append("B:ALS 能力位 EV_REL->EV_ABS")
    else:
        raise SystemExit("B 段没匹配上（alsps_input_init 的能力位），中止")

    if C_RE.search(src_h):
        src_h = C_RE.sub(r"\1ABS_X", src_h, count=1)
        changes.append("C:头文件 REL_X->ABS_X")
    elif C_ABS_RE.search(src_h):
        print("     （头文件已是 ABS_X，跳过 C）")
    else:
        raise SystemExit("C 段没匹配上（EVENT_TYPE_ALS_VALUE 定义），中止")

    if dry:
        print("[dry] 会改：%s" % ", ".join(changes))
        return

    with open(path_c, "wb") as f:
        f.write(src_c.encode("utf-8", "surrogateescape"))
    with open(path_h, "wb") as f:
        f.write(src_h.encode("utf-8", "surrogateescape"))
    print("[ok]  ALSPS ABS 补丁完成：%s" % ", ".join(changes))


if __name__ == "__main__":
    main()
