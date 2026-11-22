#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在线重推表实验（极简版）：复用现成 fb_dispparam 节点，拦截特殊 param 重推表。

用户要求（2026-11-22）：像最早测表A/B 一样，在线推表验证表和时序的含金量，
不用反复编译刷机。

原理：
  fb_dispparam 节点 (mtkfb.c:2538) → primary_display_set_panel_param(param)
  → video mode + cmdq → disp_lcm_set_param → lcm_set_disp_param(param)
  本补丁在 lcm_set_disp_param 里拦截特殊 param：
    echo 0x9999 > /sys/class/graphics/fb0/fb_dispparam  → 重推 s3pro_init_table_a（当前编译的表）
    echo 0x9998 > ...                                   → 重推 s3pro_sleep_table（睡眠表，对照）
    echo 0x9997 > ...                                   → 只推 0x11 SLEEPOUT + 0x29 DISPLAY ON

这样能在任意时刻（健康态/黑屏态）在线重推表，观察屏幕反应。
注意：本补丁必须在 patch_lcm_stock 之后（需要 s3pro_init_table_a/sleep_table 存在）。
与 dimming_early 兼容（dimming 前移不改 lcm_set_disp_param）。

用法：python patch_lab_push.py <内核树根> [--dry-run]
开关：LAB_PUSH=1 由 build.sh 调用（默认 0，实验用手动开）。
"""
import os
import sys

MARK = "S3PRO_LAB_PUSH"

OLD = '''static void lcm_set_disp_param(unsigned int param)
{
	int ret;

	LCM_LOGI("lcm_set_disp_paramt: param = %d\\n", param);
	ret = set_panel_disp_param(param, push_table);
	(void)ret;
}'''

NEW = '''static void lcm_set_disp_param(unsigned int param)
{
	int ret;

	LCM_LOGI("lcm_set_disp_paramt: param = %d\\n", param);

	/* S3PRO_LAB_PUSH: 在线重推表实验（通过 fb_dispparam 触发） */
	if (param == 0x9999) {
		LCM_LOGI("S3PRO_LAB_PUSH: re-push init table A (%d)\\n",
			 (int)(sizeof(s3pro_init_table_a)/sizeof(struct LCM_setting_table)));
		push_table(s3pro_init_table_a,
			   sizeof(s3pro_init_table_a)/sizeof(struct LCM_setting_table), 1);
		return;
	}
	if (param == 0x9998) {
		LCM_LOGI("S3PRO_LAB_PUSH: push sleep table\\n");
		push_table(s3pro_sleep_table,
			   sizeof(s3pro_sleep_table)/sizeof(struct LCM_setting_table), 1);
		return;
	}
	if (param == 0x9997) {
		struct LCM_setting_table lab_11_29[] = {
			{0x11, 1, {0x00} },
			{REGFLAG_DELAY, 120, {} },
			{0x29, 1, {0x00} },
			{REGFLAG_DELAY, 40, {} },
			{REGFLAG_END_OF_TABLE, 0x00, {} },
		};
		LCM_LOGI("S3PRO_LAB_PUSH: push 0x11+delay+0x29\\n");
		push_table(lab_11_29,
			   sizeof(lab_11_29)/sizeof(struct LCM_setting_table), 1);
		return;
	}

	ret = set_panel_disp_param(param, push_table);
	(void)ret;
}'''


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
        if "lcm_set_disp_param" in t and "s3pro_init_table_a" in t:
            return p
    return None


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    p = find_driver(tree)
    if not p:
        raise SystemExit("找不到含 lcm_set_disp_param/s3pro_init_table_a 的驱动")
    print("  驱动: %s" % os.path.relpath(p, tree))

    t = open(p, encoding="utf-8", errors="surrogateescape").read()
    if MARK in t:
        print("  已打过补丁，跳过")
        return 0

    cnt = t.count(OLD)
    if cnt != 1:
        print("!! 锚点匹配 %d 次（应为 1），中止" % cnt)
        return 1

    t = t.replace(OLD, NEW, 1)
    print("  已加在线重推表拦截（0x9999/0x9998/0x9997）")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(p, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "标记存在": MARK in c,
        "0x9999重推表": "param == 0x9999" in c,
        "0x9998睡眠表": "param == 0x9998" in c,
        "0x9997_11_29": "param == 0x9997" in c,
        "推init表": "push_table(s3pro_init_table_a" in c,
        "花括号配平": c.count("{") == c.count("}"),
    }
    print()
    bad = 0
    for k, v in checks.items():
        print("    %-16s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1
    if bad:
        return 1
    print("\n在线重推表补丁完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
