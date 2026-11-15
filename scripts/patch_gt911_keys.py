#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启用 GT911 实体电容触摸按键（按原厂固件的配置重建）。

=== 依据 ===
1) 原厂 7.0 内核里有 4 套 GT911 配置数组（KIMAGE_BASE=0xffffffc000080000）：
     0xaccbf0 / 0xacccb0 / 0xaccd70，均 X=1536 Y=2048
   原厂驱动把其中一套发给 IC。其 +0x07 字节（GTP_REG_HAVE_KEY = 0x804E，
   相对数组起点 0x8047）为 0x01 = 有按键；本树的 sample 配置是 0x06/0x00。
2) 本机 DTB（authorparts/kernel.dtb）里已经写好了按键：
     tpd-key-num       = 4
     tpd-key-local     = 139(KEY_MENU), 172(KEY_HOMEPAGE), 158(KEY_BACK), 0
     tpd-key-dim-local = (60,850,50,30) (180,850,50,30) (300,850,50,30) (420,850,50,30)
3) 驱动 gt9xx_driver.c 的按键路径需要三个宏，而本树完全没定义：
     TPD_KEY_COUNT / TPD_KEYS / GTP_KEY_MAP_ARRAY
   它们被 #ifdef CONFIG_GTP_HAVE_TOUCH_KEY 包着，开关一开就编译失败。

=== 本补丁做三件事 ===
  a) 用原厂 186 字节配置数组整体替换当前 config 变体的 CTP_CFG_GROUP1
  b) 在 tpd_gt9xx_common.h 里补齐三个宏（值取自 DTB）
  c) 打印提示：defconfig 里的 CONFIG_GTP_HAVE_TOUCH_KEY 由 build.sh 打开

用法：
    python patch_gt911_keys.py <内核树根> [--dry-run]
"""
import os
import re
import sys

MARK = "S3PRO_GTP_KEYS"

# 原厂 7.0 内核 @0xaccbf0 的 GT911 配置数组（186 字节）
STOCK_CFG = [
    0x43, 0x00, 0x06, 0x00, 0x08, 0x05, 0x34, 0x01, 0x01, 0x08, 0x28, 0x0A, 0x5A, 0x40, 0x03, 0x25,
    0x00, 0x00, 0xFF, 0x7F, 0x45, 0x22, 0x00, 0x16, 0x19, 0x1C, 0x14, 0x95, 0x35, 0xFF, 0x33, 0x35,
    0x31, 0x0D, 0x00, 0x00, 0x00, 0xBA, 0x33, 0x1C, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x22, 0x0F, 0x49, 0x54, 0xD0, 0x03, 0x00, 0x14, 0x00, 0x04, 0xFC, 0x11, 0x00, 0xC3,
    0x18, 0x00, 0x9F, 0x21, 0x00, 0x87, 0x2D, 0x00, 0x76, 0x3F, 0x00, 0x76, 0x08, 0x10, 0x18, 0x00,
    0xF0, 0x45, 0x35, 0xDD, 0xD0, 0x27, 0x0D, 0x00, 0x64, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x14, 0x50, 0x32, 0x03, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00, 0x00, 0x04, 0x00,
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F,
    0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x2A, 0x29,
    0x28, 0x27, 0x26, 0x25, 0x24, 0x23, 0x22, 0x21, 0x20, 0x1F, 0x1E, 0x1D, 0x1C, 0x1B, 0x19, 0x18,
    0x17, 0x16, 0x15, 0x14, 0x13, 0x12, 0x11, 0x10, 0x0F, 0x0E, 0x0D, 0x0C, 0x0B, 0x0A, 0x09, 0x08,
    0x07, 0x06, 0x05, 0x04, 0x03, 0x02, 0x01, 0x00, 0xD0, 0x01,
]

KEY_MACROS = '''
/* 注意：本块插在文件里已有的 CONFIG_GTP_HAVE_TOUCH_KEY 条件块内部，
 * 不要再自带条件编译收尾，否则层级会错乱。
 * S3PRO_GTP_KEYS: 本树的 GT911 驱动缺少 TPD_KEY_* / GTP_KEY_MAP_ARRAY 定义，
 * 打开 CONFIG_GTP_HAVE_TOUCH_KEY 后会编译失败。这里按本机 DTB 补齐：
 *   tpd-key-num       = 4
 *   tpd-key-local     = 139(KEY_MENU) 172(KEY_HOMEPAGE) 158(KEY_BACK) 0
 *   tpd-key-dim-local = (60,850,50,30) (180,850,50,30) (300,850,50,30) (420,850,50,30)
 * 驱动把 IC 上报的按键位掩码映射成这些坐标的虚拟触摸点，
 * 再由框架按 tpd-key-dim-local 转成键值。 */
#ifndef TPD_KEY_COUNT
#define TPD_KEY_COUNT 4
#endif
#ifndef TPD_KEYS
#define TPD_KEYS {KEY_MENU, KEY_HOMEPAGE, KEY_BACK, 0}
#endif
#ifndef GTP_KEY_MAP_ARRAY
#define GTP_KEY_MAP_ARRAY {{60, 850}, {180, 850}, {300, 850}, {420, 850}}
#endif
'''


def cfg_dir(gt):
    """从 Kconfig/Makefile 找出当前选用的 config 变体（默认 config3）。"""
    name = "config3"
    for f in ("Kconfig", "Makefile"):
        p = os.path.join(gt, f)
        if not os.path.exists(p):
            continue
        t = open(p, encoding="utf-8", errors="replace").read()
        for m in re.finditer(r"config(\d)", t):
            name = "config" + m.group(1)
        break
    return name


def patch_driver_key_path(drvp):
    """让驱动直接上报真实键值，绕开失效的框架虚拟按键机制。

    实测：/sys/board_properties/virtualkeys.mtk-tpd 不存在，
    说明 tpd_button_init() 没跑，框架那条"坐标→按键"的路是死的。
    而驱动里本来就有一行被注释掉的 input_report_key——它才是正确做法。
    """
    lines = open(drvp, encoding="utf-8", errors="surrogateescape").read().split("\n")

    # --- 1) 打开能力注册（原为 #if 0）---
    cap_i = None
    for i, l in enumerate(lines):
        if "#if 0/* #ifdef CONFIG_GTP_HAVE_TOUCH_KEY */" in l:
            cap_i = i
            break
    if cap_i is None:
        print("  !! 找不到能力注册块（#if 0 那段），跳过")
        return False, False
    cap_end = cap_i
    while cap_end < len(lines) and lines[cap_end].strip() != "#endif":
        cap_end += 1
    cap_new = [
        "/* S3PRO_GTP_KEYS: 注册按键能力（本块原为 #if 0，关了实体按键） */",
        "#ifdef CONFIG_GTP_HAVE_TOUCH_KEY",
        "\t{",
        "\t\tint _ki;",
        "",
        "\t\tfor (_ki = 0; _ki < TPD_KEY_COUNT; _ki++)",
        "\t\t\tinput_set_capability(tpd->dev, EV_KEY, touch_key_array[_ki]);",
        "\t}",
        "#endif",
    ]
    lines[cap_i:cap_end + 1] = cap_new

    # --- 2) 改写按键上报：直接报键值，不再伪造触摸坐标 ---
    rep_i = None
    for i, l in enumerate(lines):
        if "key_value = point_data[3 + 8 * touch_num];" in l:
            rep_i = i
            break
    if rep_i is None:
        print("  !! 找不到按键上报块，跳过")
        return True, False
    # 往回找到 #ifdef CONFIG_GTP_HAVE_TOUCH_KEY
    rep_start = rep_i
    while rep_start > 0 and "#ifdef CONFIG_GTP_HAVE_TOUCH_KEY" not in lines[rep_start]:
        rep_start -= 1
    # 往后找到该块结束（pre_key = key_value; 之前）
    rep_end = rep_i
    while rep_end < len(lines) and "pre_key = key_value;" not in lines[rep_end]:
        rep_end += 1
    rep_end -= 1  # 指向 #endif 上面的空行/尾行
    # 精确定位到 #endif
    k = rep_i
    while k < len(lines) and lines[k].strip() != "#endif":
        k += 1
    rep_end = k
    rep_new = [
        "#ifdef CONFIG_GTP_HAVE_TOUCH_KEY",
        "\t\tkey_value = point_data[3 + 8 * touch_num];",
        "",
        "\t\tif (key_value || pre_key) {",
        "\t\t\tfor (i = 0; i < TPD_KEY_COUNT; i++) {",
        "\t\t\t\tint _kd = (key_value & (0x01 << i)) ? 1 : 0;",
        "\t\t\t\tint _kp = (pre_key & (0x01 << i)) ? 1 : 0;",
        "",
        "\t\t\t\tif (_kd != _kp)",
        "\t\t\t\t\tinput_report_key(tpd->dev, touch_key_array[i], _kd);",
        "\t\t\t}",
        "\t\t\tinput_sync(tpd->dev);",
        "",
        "\t\t\ttouch_num = 0;",
        "\t\t\tpre_touch = 0;",
        "\t\t}",
        "",
        "#endif",
    ]
    lines[rep_start:rep_end + 1] = rep_new

    # --- 3) 映射表不再使用，加引用避免 -Werror=unused-variable ---
    for i, l in enumerate(lines):
        if "touch_key_point_maping_array[] = GTP_KEY_MAP_ARRAY;" in l:
            keep = [
                "",
                "/* S3PRO_GTP_KEYS: 改成直接上报键值后，坐标映射表不再被使用。",
                " * 内核开了 -Werror=unused-variable，这里显式引用一次避免编译失败。 */",
                "static void __attribute__((unused)) s3pro_keep_key_map(void)",
                "{",
                "\t(void)touch_key_point_maping_array;",
                "}",
            ]
            lines[i + 1:i + 1] = keep
            break

    open(drvp, "w", encoding="utf-8", errors="surrogateescape", newline="").write("\n".join(lines))
    return True, True


def find_gt911(tree):
    base = os.path.join(tree, "drivers", "input", "touchscreen", "mediatek", "GT911")
    return base if os.path.isdir(base) else None


def _ifdef_balanced(text):
    """检查条件编译配平。必须先剥离 C 注释——注释里出现的 #ifdef/#endif
    字面量会被误算（上一版就踩了这个坑）。"""
    t = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    t = re.sub(r"//[^\n]*", " ", t)
    n_if = len(re.findall(r"^\s*#\s*(?:ifdef|ifndef|if)\b", t, re.M))
    n_end = len(re.findall(r"^\s*#\s*endif\b", t, re.M))
    return n_if == n_end


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    gt = find_gt911(tree)
    if not gt:
        raise SystemExit("找不到 GT911 驱动目录")
    variant = cfg_dir(gt)
    cfgp = os.path.join(gt, "include", variant, "gt9xx_config.h")
    mathp = os.path.join(gt, "include", "tpd_gt9xx_common.h")
    print("  驱动: %s" % os.path.relpath(gt, tree))
    print("  配置变体: %s" % variant)

    # ---- a) 替换 CTP_CFG_GROUP1 ----
    t = open(cfgp, encoding="utf-8", errors="surrogateescape").read()
    if MARK in t:
        print("  配置数组已是原厂版本，跳过")
    else:
        m = re.search(r"#define\s+CTP_CFG_GROUP1\s*\{", t)
        if not m:
            print("!! 找不到 CTP_CFG_GROUP1")
            return 1
        # 找到该宏的结束大括号（后面可能跟注释/换行）
        i = m.end()
        depth = 1
        while i < len(t) and depth:
            if t[i] == "{":
                depth += 1
            elif t[i] == "}":
                depth -= 1
            i += 1
        if depth:
            print("!! CTP_CFG_GROUP1 大括号不配平")
            return 1
        body = []
        for r in range(0, len(STOCK_CFG), 16):
            chunk = STOCK_CFG[r:r + 16]
            body.append("  " + ", ".join("0x%02X" % b for b in chunk) + ",")
        new = ("#define CTP_CFG_GROUP1 {\\\n" + "\\\n".join(body) + "\\\n  }")
        t = t[:m.start()] + "/* %s: 原厂 7.0 内核 @0xaccbf0 的 GT911 配置（186 字节，含 have_key） */\n" % MARK + new + t[i:]
        open(cfgp, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)
        print("  已用原厂配置数组替换 CTP_CFG_GROUP1（186 字节）")

    # ---- b) 补三个宏 ----
    h = open(mathp, encoding="utf-8", errors="surrogateescape").read()
    if MARK in h:
        print("  按键宏已存在，跳过")
    else:
        anchor = "#define GTP_KEY_TAB\t {KEY_MENU, KEY_HOME, KEY_BACK, KEY_SEND}\n#endif"
        if anchor not in h:
            print("!! GTP_KEY_TAB 锚点未找到")
            return 1
        h = h.replace(anchor,
                      "#define GTP_KEY_TAB\t {KEY_MENU, KEY_HOME, KEY_BACK, KEY_SEND}\n" + KEY_MACROS.strip() + "\n#endif", 1)
        open(mathp, "w", encoding="utf-8", errors="surrogateescape", newline="").write(h)
        print("  已补齐 TPD_KEY_COUNT / TPD_KEYS / GTP_KEY_MAP_ARRAY")

    # ---- c) 驱动改为直接上报键值 ----
    drvp = os.path.join(gt, "gt9xx_driver.c")
    if MARK in open(drvp, encoding="utf-8", errors="surrogateescape").read():
        print("  驱动按键路径已改过，跳过")
    else:
        ok_cap, ok_rep = patch_driver_key_path(drvp)
        if ok_cap:
            print("  已打开按键能力注册（原为 #if 0）")
        if ok_rep:
            print("  已改为直接上报键值（不再伪造触摸坐标）")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    # ---- 校验 ----
    c1 = open(cfgp, encoding="utf-8", errors="surrogateescape").read()
    c2 = open(mathp, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "配置标记": MARK in c1,
        "原厂数组 186 字节": c1.count("0x") >= 180,
        "have_key=0x01": "0x34, 0x01, 0x01, 0x08" in c1,
        "TPD_KEY_COUNT": "#define TPD_KEY_COUNT 4" in c2,
        "TPD_KEYS": "#define TPD_KEYS {KEY_MENU, KEY_HOMEPAGE, KEY_BACK, 0}" in c2,
        "GTP_KEY_MAP_ARRAY": "#define GTP_KEY_MAP_ARRAY" in c2,
        "花括号配平(配置)": c1.count("{") == c1.count("}"),
        "花括号配平(头文件)": c2.count("{") == c2.count("}"),
        "ifdef/endif 配平": _ifdef_balanced(c2),
    }
    print()
    bad = 0
    for k, v in checks.items():
        print("    %-22s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1
    if bad:
        return 1
    print("\nGT911 触摸按键补丁完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
