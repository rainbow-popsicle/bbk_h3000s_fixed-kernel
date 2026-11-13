#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 cappu（小米平板3，3.18.123）的现代 binder 驱动整套装进本树（3.18.119）。

为什么需要它（设备实测证据链）：
  旧驱动（Android 6/7 时代，单文件、无 binder_alloc）在 8.1 上会出现：
    1) 三个 binder 设备共用一个全局 context → 第二个 BINDER_SET_CONTEXT_MGR 返回 -EBUSY
       （实测 23 次，vndservicemanager 无限重启）
    2) 完全不认识 BC_TRANSACTION_SG（命令 17）
       （实测 603 次 unknown command 1078485777 = 0x40486311）
    3) 不认识 BINDER_TYPE_PTR / BINDER_TYPE_FDA 等 HIDL 对象
       → 8.1 的 hwbinder 事务无法投递（实测 241 次带 buffers 的 SG 事务）

  cappu 的 binder.c（169,299 字节）自带：
    - binder_device / binder_context 多设备（默认 binder,hwbinder,vndbinder）
    - BC_TRANSACTION_SG / BC_REPLY_SG + extra_buffers_size
    - BINDER_TYPE_PTR / BINDER_TYPE_FDA + validate/fixup/translate 全套
    且仍是 3.18 时代 API（无 get_user_pages / refcount_t / kvmalloc / timer_setup），
    security_binder_* 四个钩子本树都有 → 可直接替换。

安装内容（6 个文件 + 2 处构建文件改动）：
    drivers/staging/android/binder.c
    drivers/staging/android/binder.h
    drivers/staging/android/binder_alloc.c   （新增）
    drivers/staging/android/binder_alloc.h   （新增）
    drivers/staging/android/binder_trace.h
    drivers/staging/android/uapi/binder.h
    Makefile : obj-$(CONFIG_ANDROID_BINDER_IPC) += binder.o binder_alloc.o
    Kconfig  : 补 config ANDROID_BINDER_DEVICES（默认 binder,hwbinder,vndbinder）

用法：
    python patch_binder_modern.py <内核树根> [--src <binder-3.18目录>] [--dry-run]
"""
import os
import re
import shutil
import sys

MARK = "binder_devices_param"          # 现代驱动特有
FILES = [
    ("binder.c", "drivers/staging/android/binder.c"),
    ("binder.h", "drivers/staging/android/binder.h"),
    ("binder_alloc.c", "drivers/staging/android/binder_alloc.c"),
    ("binder_alloc.h", "drivers/staging/android/binder_alloc.h"),
    ("binder_trace.h", "drivers/staging/android/binder_trace.h"),
    ("uapi/binder.h", "drivers/staging/android/uapi/binder.h"),
]

KCONFIG_BLOCK = """config ANDROID_BINDER_DEVICES
\tstring "Android Binder devices"
\tdepends on ANDROID_BINDER_IPC
\tdefault "binder,hwbinder,vndbinder"
\t---help---
\t  Default value for the binder.devices parameter.

\t  The binder.devices parameter is a comma-separated list of strings
\t  that specifies the names of the binder device nodes that will be
\t  created. Each binder device has its own context manager, and is
\t  therefore logically separated from the other devices.

"""


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(root, "kernel-patches", "binder-3.18")
    for i, a in enumerate(sys.argv):
        if a == "--src" and i + 1 < len(sys.argv):
            src = sys.argv[i + 1]

    if not os.path.isdir(src):
        raise SystemExit("找不到 binder 源目录: %s" % src)
    missing = [f for f, _ in FILES if not os.path.exists(os.path.join(src, f.replace("/", os.sep)))]
    if missing:
        raise SystemExit("binder 源文件缺失: %s" % missing)

    problems = []

    # ---- 1) 复制文件 ----
    if dry:
        print("  [dry-run] 将复制 6 个文件:")
        for f, dst in FILES:
            print("    %-46s <- %s" % (dst, f))
    else:
        for f, dst in FILES:
            s = os.path.join(src, f.replace("/", os.sep))
            d = os.path.join(tree, dst.replace("/", os.sep))
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copyfile(s, d)
        print("  已复制 6 个文件到树里")

    # ---- 2) Makefile ----
    mk = os.path.join(tree, "drivers", "staging", "android", "Makefile")
    t = open(mk, encoding="utf-8", errors="surrogateescape").read()
    if "binder_alloc.o" in t:
        print("  Makefile 已含 binder_alloc.o，跳过")
    else:
        new, n = re.subn(r"(obj-\$\(CONFIG_ANDROID_BINDER_IPC\)\s*\+=\s*binder\.o)",
                         r"\1 binder_alloc.o", t, count=1)
        if n == 0:
            problems.append("Makefile 里找不到 binder.o 规则")
        elif dry:
            print("  [dry-run] Makefile: += binder.o binder_alloc.o")
        else:
            open(mk, "w", encoding="utf-8", errors="surrogateescape", newline="").write(new)
            print("  Makefile: 已加 binder_alloc.o")

    # ---- 3) Kconfig ----
    kc = os.path.join(tree, "drivers", "staging", "android", "Kconfig")
    t = open(kc, encoding="utf-8", errors="surrogateescape").read()
    if "config ANDROID_BINDER_DEVICES" in t:
        print("  Kconfig 已含 ANDROID_BINDER_DEVICES，跳过")
    else:
        anchor = "\nconfig ASHMEM\n"
        if anchor not in t:
            problems.append("Kconfig 里找不到 config ASHMEM 作为插入锚点")
        elif dry:
            print("  [dry-run] Kconfig: 插入 ANDROID_BINDER_DEVICES 配置块")
        else:
            t = t.replace(anchor, "\n" + KCONFIG_BLOCK + "config ASHMEM\n", 1)
            open(kc, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)
            print("  Kconfig: 已插入 ANDROID_BINDER_DEVICES")

    # ---- 4) 校验 ----
    print()
    bnd = os.path.join(tree, "drivers", "staging", "android", "binder.c")
    if os.path.exists(bnd) and not dry:
        c = open(bnd, encoding="utf-8", errors="surrogateescape").read()
        checks = {
            "binder.c 含 BINDER_TYPE_PTR": "BINDER_TYPE_PTR" in c,
            "binder.c 含 BC_TRANSACTION_SG": "BC_TRANSACTION_SG" in c,
            "binder.c 含 extra_buffers_size": "extra_buffers_size" in c,
            "binder.c 含 binder_devices_param": MARK in c,
            "binder.c 不含旧补丁标记 S3PRO_": "S3PRO_" not in c,
            "binder_alloc.c 存在": os.path.exists(os.path.join(tree, "drivers/staging/android/binder_alloc.c")),
            "Makefile 含 binder_alloc.o": "binder_alloc.o" in open(mk, encoding="utf-8", errors="surrogateescape").read(),
            "Kconfig 含 ANDROID_BINDER_DEVICES": "config ANDROID_BINDER_DEVICES" in open(kc, encoding="utf-8", errors="surrogateescape").read(),
        }
        for k, v in checks.items():
            print("  %-38s %s" % (k, "OK" if v else "失败!"))
            if not v:
                problems.append(k)

    if problems:
        print("\n!! 有问题:")
        for p in problems:
            print("   - " + p)
        return 1
    print("\n现代 binder 安装完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
