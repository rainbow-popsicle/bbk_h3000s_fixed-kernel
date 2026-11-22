#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把面板 DSI 时序参数改为原厂 3.18.35 实测值（LK 用这套值能点亮屏幕）。

=== 依据（2026-11-22，反汇编原厂 lcm_get_params @0xffffffc000458f40） ===
用 LANE_NUM=4 (原厂 str [0x1e8]=4) 反推 DSI 结构起始 = 0x1e8-0x14 = 0x1d4，
映射出的原厂时序写入与当前驱动逐项对比：

  字段               原厂(能亮)   当前移植(黑)   DSI内偏移
  vertical_sync_active    2          2            0x3c   (相同)
  vertical_backporch     40          8            0x40   ← 差
  vertical_frontporch    40         14            0x44   ← 差
  vertical_active_line 2048       2048            0x4c   (相同)
  horizontal_sync_active  4          2            0x50   ← 差
  horizontal_backporch  200         55            0x54   ← 差
  horizontal_frontporch 200         55            0x58   ← 差
  horizontal_active_pixel 1536     1536           0x60   (相同)

原厂反汇编原始字节:
  0x458fb0: mov w7, #0x28 (40)  → str [0x214]=v_bp=40, str [0x218]=v_fp=40
  0x458fdc: str w7,[0x214]; 0x458fe0: str w7,[0x218]
  0x458fec: str w4,[0x228] (w4=0xc8=200) = h_bp
  0x458ff0: str w4,[0x22c] (200) = h_fp
  0x458fe4: str w6,[0x220] (2048) = v_act
  0x458fe8: str w5,[0x224] (w5=4) = h_sync
  0x458ff4: str w3,[0x234] (1536) = h_act

=== 为什么这样修 ===
开机屏幕亮是 LK（原厂驱动）初始化的；kernel 唤醒重建用移植驱动的时序（小米平板3 的
8/14/2/55/55）配这块屏 → H/V blanking 不匹配 → 黑。
把这些时序改回原厂值，使 kernel 重建与 LK 初始化等价。

=== 不改的字段 ===
ssc_range=500 等其余原厂值不影响显示或有不确定性，保持移植驱动原样，避免引入新风险。

用法：
    python patch_stock_timing.py <内核树根> [--dry-run]
开关：STOCK_TIMING=1 时由 build.sh 调用（默认 1）。
"""
import os
import sys

MARK = "S3PRO_STOCK_TIMING"

OLD = '''	params->dsi.vertical_sync_active = 2;
	params->dsi.vertical_backporch = 8;
	params->dsi.vertical_frontporch = 14;
	params->dsi.vertical_active_line = FRAME_HEIGHT;

	params->dsi.horizontal_sync_active = 2;
	params->dsi.horizontal_backporch = 55;
	params->dsi.horizontal_frontporch = 55;
	params->dsi.horizontal_active_pixel = FRAME_WIDTH;'''

NEW = '''	/* S3PRO_STOCK_TIMING: 原厂 3.18.35 实测时序（LK 用这套值能点亮屏幕）。
	 * 之前用小米平板3 的 8/14/2/55/55 配这块屏 → H/V blanking 不匹配 → 熄屏唤醒黑屏。 */
	params->dsi.vertical_sync_active = 2;
	params->dsi.vertical_backporch = 40;
	params->dsi.vertical_frontporch = 40;
	params->dsi.vertical_active_line = FRAME_HEIGHT;

	params->dsi.horizontal_sync_active = 4;
	params->dsi.horizontal_backporch = 200;
	params->dsi.horizontal_frontporch = 200;
	params->dsi.horizontal_active_pixel = FRAME_WIDTH;'''


def find_driver(tree):
    base = os.path.join(tree, "drivers", "misc", "mediatek", "lcm")
    name = None
    cfg = os.path.join(tree, ".config")
    if os.path.exists(cfg):
        for line in open(cfg, encoding="utf-8", errors="replace"):
            if line.startswith("CONFIG_CUSTOM_KERNEL_LCM="):
                name = line.split("=", 1)[1].strip().strip('"')
                break
    cands = []
    if name:
        d = os.path.join(base, name)
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith(".c"):
                    cands.append(os.path.join(d, f))
    if not cands:
        d = os.path.join(base, "nt35523_wqxga_dsi_vdo_sharp")
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith(".c"):
                    cands.append(os.path.join(d, f))
    for p in cands:
        t = open(p, encoding="utf-8", errors="replace").read()
        if "lcm_get_params" in t and "vertical_backporch" in t:
            return p
    return None


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    p = find_driver(tree)
    if not p:
        raise SystemExit("找不到含 lcm_get_params/vertical_backporch 的驱动")
    print("  驱动: %s" % os.path.relpath(p, tree))

    t = open(p, encoding="utf-8", errors="surrogateescape").read()
    if MARK in t:
        print("  已打过补丁，跳过")
        return 0

    cnt = t.count(OLD)
    if cnt != 1:
        print("!! 时序锚点匹配 %d 次（应为 1），中止" % cnt)
        return 1

    t = t.replace(OLD, NEW, 1)
    print("  已改时序：v_bp 8->40, v_fp 14->40, h_sync 2->4, h_bp 55->200, h_fp 55->200")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(p, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "标记存在": MARK in c,
        "v_bp=40": "vertical_backporch = 40;" in c,
        "v_fp=40": "vertical_frontporch = 40;" in c,
        "h_sync=4": "horizontal_sync_active = 4;" in c,
        "h_bp=200": "horizontal_backporch = 200;" in c,
        "h_fp=200": "horizontal_frontporch = 200;" in c,
        "旧值已移除": "vertical_backporch = 8;" not in c,
        "花括号配平": c.count("{") == c.count("}"),
    }
    print()
    bad = 0
    for k, v in checks.items():
        print("    %-18s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1
    if bad:
        return 1
    print("\n原厂时序修复完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
