#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修正 GT911 触摸：分辨率 + 校准矩阵。

实机证据（getevent 抓到的真实事件）：
    ABS_MT_POSITION_X  0x000002e3 = 739      ← X 正常
    ABS_MT_POSITION_Y  0xfffffe2e = -466     ← Y 是负数，Android 直接丢弃
    → 表现为"触摸完全没反应"

根因（两处都是按小米平板3 / 800x1280 写的）：
  1) GT911 配置里 X_MAX=800, Y_MAX=1280      （这台实际是 1536x2048）
     由 gt9xx_driver.c 的 RESOLUTION_LOC 读取：X=(c[2]<<8)|c[1], Y=(c[4]<<8)|c[3]
  2) 校准矩阵 TPD_CALIBRATION_MATRIX_ROTATION_* = {0,2559,0, -6557,0,5238784,0,0}
     即 x' = y*0.625, y' = 1279 - x*1.6   （800x1280 屏的 90° 旋转）
     x 一旦超过 799，y' 就变负数。

修正：
  - 配置分辨率 → 1536x2048
  - 校准矩阵 → 恒等变换 {4096,0,0, 0,4096,0}
    （面板原生坐标直接上报；若方向不对，改这一行即可快速迭代）

用法：
    python patch_gt911_touch.py <内核树根> [--dry-run]
"""
import os
import re
import sys

MARK = "S3PRO_TOUCH_FIX"
TARGET_W, TARGET_H = 1536, 2048

IDENTITY = ("{4096, 0, 0, 0, 4096, 0, 0, 0}")


def find_gt911(tree):
    base = os.path.join(tree, "drivers", "input", "touchscreen", "mediatek", "GT911")
    return base if os.path.isdir(base) else None


def cfg_dir(gt):
    """从 Kconfig/Makefile 里找出当前选用的 config 变体（默认 config3）"""
    mk = os.path.join(gt, "Makefile")
    name = "config3"
    if os.path.exists(mk):
        t = open(mk, encoding="utf-8", errors="replace").read()
        # 变体名来自 CONFIG_GT911_CONFIG，Makefile 里是 $(CONFIG_GT911_CONFIG)
        pass
    return name


def patch_config(gt, dry):
    name = cfg_dir(gt)
    p = os.path.join(gt, "include", name, "gt9xx_config.h")
    if not os.path.exists(p):
        return ["找不到配置头文件: %s" % p]
    t = open(p, encoding="utf-8", errors="surrogateescape").read()
    if MARK + "_CFG" in t:
        print("  配置文件: 已修过，跳过")
        return []

    # 数组前 5 个字节：c[0]=标志, c[1..2]=X_MAX(lo,hi), c[3..4]=Y_MAX(lo,hi)
    m = re.search(r"(0x[0-9A-Fa-f]{2},\s*)(0x[0-9A-Fa-f]{2}),\s*(0x[0-9A-Fa-f]{2}),\s*"
                  r"(0x[0-9A-Fa-f]{2}),\s*(0x[0-9A-Fa-f]{2}),", t)
    if not m:
        return ["%s: 没找到配置数组开头" % name]
    c0 = m.group(1)
    old = [int(x, 16) for x in (m.group(2), m.group(3), m.group(4), m.group(5))]
    ox = (old[1] << 8) | old[0]
    oy = (old[3] << 8) | old[2]
    print("  配置文件: %s  当前 X_MAX=%d Y_MAX=%d" % (name, ox, oy))

    new_vals = [
        TARGET_W & 0xFF, (TARGET_W >> 8) & 0xFF,
        TARGET_H & 0xFF, (TARGET_H >> 8) & 0xFF,
    ]
    new = c0 + "".join("0x%02X, " % v for v in new_vals)
    t2 = t[:m.start()] + new + t[m.end():]
    # 在 #define CTP_CFG_GROUP1 之前插入标记
    t2 = t2.replace("#define CTP_CFG_GROUP1",
                    "/* %s_CFG: 分辨率改为 %dx%d（原 %dx%d）*/\n#define CTP_CFG_GROUP1"
                    % (MARK, TARGET_W, TARGET_H, ox, oy), 1)
    print("  配置文件: X_MAX/Y_MAX → %d/%d" % (TARGET_W, TARGET_H))
    if not dry:
        open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t2)
    return []


def patch_matrix(gt, dry):
    p = os.path.join(gt, "include", "tpd_gt9xx_common.h")
    if not os.path.exists(p):
        return ["找不到 %s" % p]
    t = open(p, encoding="utf-8", errors="surrogateescape").read()
    if MARK + "_MAT" in t:
        print("  校准矩阵: 已修过，跳过")
        return []
    hits = 0
    for macro in ("TPD_CALIBRATION_MATRIX_ROTATION_NORMAL",
                  "TPD_CALIBRATION_MATRIX_ROTATION_FACTORY"):
        pat = re.compile(r"#define\s+" + macro + r"\s+\{[^}]*\}")
        m = pat.search(t)
        if not m:
            continue
        print("  %s:" % macro)
        print("    原: %s" % m.group(0).split(None, 1)[1])
        t = t[:m.start()] + ("#define %s %s /* %s_MAT: 恒等变换 */" % (macro, IDENTITY, MARK)) + t[m.end():]
        print("    新: %s" % IDENTITY)
        hits += 1
    if not hits:
        return ["没找到校准矩阵宏"]
    if not dry:
        open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)
    return []


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    gt = find_gt911(tree)
    if not gt:
        raise SystemExit("找不到 GT911 驱动目录")

    problems = patch_config(gt, dry)
    problems += patch_matrix(gt, dry)
    if problems:
        print("\n!! 问题:")
        for p in problems:
            print("   - " + p)
        return 1
    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    # 校验
    cfgp = os.path.join(gt, "include", cfg_dir(gt), "gt9xx_config.h")
    mathp = os.path.join(gt, "include", "tpd_gt9xx_common.h")
    c1 = open(cfgp, encoding="utf-8", errors="surrogateescape").read()
    c2 = open(mathp, encoding="utf-8", errors="surrogateescape").read()
    m = re.search(r"0x[0-9A-Fa-f]{2},\s*(0x[0-9A-Fa-f]{2}),\s*(0x[0-9A-Fa-f]{2}),\s*"
                  r"(0x[0-9A-Fa-f]{2}),\s*(0x[0-9A-Fa-f]{2}),", c1)
    ok_cfg = False
    if m:
        v = [int(x, 16) for x in m.groups()]
        ok_cfg = ((v[1] << 8) | v[0]) == TARGET_W and ((v[3] << 8) | v[2]) == TARGET_H
    print()
    checks = {
        "配置分辨率 = %dx%d" % (TARGET_W, TARGET_H): ok_cfg,
        "矩阵改成恒等变换": "4096, 0, 0, 0, 4096, 0" in c2,
        "矩阵标记存在": MARK + "_MAT" in c2,
    }
    bad = 0
    for k, v in checks.items():
        print("    %-28s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1
    if bad:
        return 1
    print("\n触摸参数修正完成（分辨率 + 校准矩阵）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
