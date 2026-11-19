#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 STP 蓝牙通道切到 BlueZ(H4) 模式，让 AOSP 8.1 的 H4Protocol 能读懂数据。

=== 根因（实测 + 逆向确认，不是猜测）===
本 ROM 用 AOSP 8.1 标准 HIDL 蓝牙栈：
  android.hardware.bluetooth@1.0-impl.so 里的 H4Protocol::OnDataReady()
  用 read() 从 /dev/stpbt 读【标准 H4 HCI 包】。

但内核 stp_chrdev_bt.c 的 BT_open() 里写死：
    mtk_wcn_stp_set_bluez(0);
  即 STP 处于【MTK 私有帧模式】，收发的是 MTK 私有封装，不是 H4。
  → H4Protocol 解析失败 → "Unexpected EOF reading the packet type" → 崩溃。

=== 常量语义（stp_core.c）===
    #define STP_BLUE_Z           (1)
    #define STP_SET_BT_STK(x, v) ((x).f_bluez = (v))
    #define STP_BT_STK_IS_BLUEZ(x) ((x).f_bluez == STP_BLUE_Z)
  驱动里多处根据 f_bluez 走不同解析路径（stp_core.c:285/1289）。
  设成 1 = BlueZ/H4 模式，正是 AOSP H4Protocol 需要的。

=== 为什么不影响固件下载 ===
libbluetooth_mtk.so 的 mtk_fw_cfg 走 ioctl（它只导入 ioctl，不导入 set_bluez），
固件下载协议由 FW 自己处理，与 f_bluez 数据解析标志无关。
本补丁只改 BT_open 时的数据模式。

=== 补丁内容 ===
把 stp_chrdev_bt.c 里 BT_open() 的 mtk_wcn_stp_set_bluez(0)
改成 mtk_wcn_stp_set_bluez(1)。

可通过环境变量 S3PRO_BT_BLUEZ=0 关闭（回退到 MTK 模式）。
"""
import os
import sys

TARGET = os.path.join(
    "drivers", "misc", "mediatek", "connectivity",
    "common", "common_main", "linux", "stp_chrdev_bt.c")

OLD = "mtk_wcn_stp_set_bluez(0);"
NEW = "mtk_wcn_stp_set_bluez(1);"
MARKER = "S3PRO-BT-BLUEZ"


def patch_text(src):
    if MARKER in src:
        return src, False
    n = src.count(OLD)
    if n == 0:
        return src, False
    # 只替换 BT_open 里的那一处（文件里通常只有一处）
    out = src.replace(OLD, "/* %s */\n\t\t%s" % (MARKER, NEW))
    return out, True


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    dry = "--dry-run" in sys.argv

    # 环境变量可关闭
    if os.environ.get("S3PRO_BT_BLUEZ", "1") == "0":
        print("[skip] S3PRO_BT_BLUEZ=0，保持 MTK 模式")
        return

    path = os.path.join(root, TARGET)
    if not os.path.exists(path):
        print("[skip] 找不到 %s" % TARGET)
        return

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        src = f.read()

    if MARKER in src:
        print("[ok]  已打过 BT BlueZ 补丁，跳过")
        return

    out, changed = patch_text(src)
    if not changed:
        print("[warn] 没找到 '%s'，源码结构可能变了" % OLD)
        return

    if dry:
        print("[dry] 会把 %d 处 %s -> %s" % (src.count(OLD), OLD, NEW))
        return

    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    print("[ok]  BT BlueZ 补丁完成：%d 处 set_bluez(0) -> set_bluez(1)" % src.count(OLD))


if __name__ == "__main__":
    main()
