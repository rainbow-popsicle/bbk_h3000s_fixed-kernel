#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 MTK 魔改版 binder 驱动补上 hwbinder / vndbinder 两个设备节点。

背景：这棵树 drivers/staging/android/binder.c 里的 miscdevice 只注册了一个
"binder"，没有 Android 8 需要的 hwbinder 和 vndbinder，导致：
    hwservicemanager: Failed to aquire binder FD. Aborting...
    hw-ProcessState: Opening '/dev/hwbinder' failed: No such file or directory
所有 HAL 起不来，SurfaceFlinger 拿不到显示 HAL，屏幕永远黑。

做法：在 binder_miscdev 定义后面追加两个同名同 fops 的 miscdevice，
并在 binder_init() 里注册它们。

用法：
    python patch_binder.py <内核源码根目录>
    python patch_binder.py <源文件>          # 也可以直接指定 binder.c
"""

from __future__ import annotations

import os
import re
import sys

EXTRA_DEFS = '''
/*
 * Android 8 的 HAL 走 hwbinder，vendor 进程走 vndbinder。
 * 这棵树的驱动只注册了 "binder"，这里补上另外两个设备节点。
 */
static struct miscdevice hwbinder_miscdev = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "hwbinder",
	.fops = &binder_fops
};

static struct miscdevice vndbinder_miscdev = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "vndbinder",
	.fops = &binder_fops
};
'''

EXTRA_REGS = '''	/* 补齐 Android 8 需要的两个 binder 设备 */
	if (misc_register(&hwbinder_miscdev))
		pr_err("binder: failed to register hwbinder\\n");
	if (misc_register(&vndbinder_miscdev))
		pr_err("binder: failed to register vndbinder\\n");
'''


def find_binder_c(path: str) -> str:
    if os.path.isfile(path) and path.endswith(".c"):
        return path
    for cand in ("drivers/staging/android/binder.c", "drivers/android/binder.c"):
        p = os.path.join(path, cand)
        if os.path.isfile(p):
            return p
    raise SystemExit("在 %s 里找不到 binder.c" % path)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = open(find_binder_c(sys.argv[1]), encoding="utf-8", errors="replace").read()
    path = find_binder_c(sys.argv[1])
    print("目标文件: %s (%d 字节)" % (path, len(src)))

    if 'hwbinder_miscdev' in src:
        print("已经补过了，跳过")
        return 0

    # 1) 找到 binder_miscdev 的定义块
    m = re.search(
        r'static\s+struct\s+miscdevice\s+binder_miscdev\s*=\s*\{.*?\};\s*\n',
        src, re.S)
    if not m:
        raise SystemExit("没找到 binder_miscdev 的定义")
    print("找到 binder_miscdev 定义，位置 %d" % m.start())
    src = src[:m.end()] + EXTRA_DEFS + src[m.end():]

    # 2) 在 binder_init 里补注册
    anchor = "\tret = misc_register(&binder_miscdev);\n"
    if anchor not in src:
        # 有些版本用 4 空格或不同缩进
        m2 = re.search(r'[ \t]*ret\s*=\s*misc_register\(&binder_miscdev\);\s*\n', src)
        if not m2:
            raise SystemExit("没找到 misc_register(&binder_miscdev) 调用")
        anchor = m2.group(0)
    src = src.replace(anchor, anchor + EXTRA_REGS, 1)

    out = find_binder_c(sys.argv[1])
    open(out, "w", encoding="utf-8", newline="\n").write(src)
    print("已写入 %s（%d 字节）" % (out, len(src)))

    # ---- 自检 ----
    chk = open(out, encoding="utf-8", errors="replace").read()
    ok = True
    for need in ('"hwbinder"', '"vndbinder"', "misc_register(&hwbinder_miscdev)",
                 "misc_register(&vndbinder_miscdev)"):
        hit = need in chk
        print("  %-40s %s" % (need, "OK" if hit else "缺失"))
        ok &= hit
    if chk.count("{") != chk.count("}"):
        print("  !! 大括号不配平 %d/%d" % (chk.count("{"), chk.count("}")))
        ok = False
    else:
        print("  大括号配平: OK")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
