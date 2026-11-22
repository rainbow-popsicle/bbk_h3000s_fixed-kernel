#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板 DSI video timing 运行时可切（原厂 BOE / sharp 两套 + 自定义）。

=== 背景（2026-11-22 反汇编原厂 lcm_get_params @0xffffffc000458f40）===
用 LANE_NUM=4 (原厂 str [0x1e8]=4) 反推 DSI 结构起始 = 0x1e8-0x14 = 0x1d4，
映射出的两套时序：

    字段                    原厂 BOE(能亮)  sharp/米板3(当前移植)  DSI内偏移
    vertical_sync_active        2              2                 0x3c  (相同)
    vertical_backporch         40              8                 0x40  ← 差
    vertical_frontporch        40             14                 0x44  ← 差
    vertical_active_line     2048           2048                 0x4c  (相同)
    horizontal_sync_active      4              2                 0x50  ← 差
    horizontal_backporch      200             55                 0x54  ← 差
    horizontal_frontporch     200             55                 0x58  ← 差
    horizontal_active_pixel  1536           1536                 0x60  (相同)

板上这块屏是 1536x2048（用户已确认），两套时序的 active 值相同，只有 blanking 不同。
之前只试过"编译期固定成 40/40/4/200/200"一种，无法在运行时对比。

=== 本补丁 ===
把 timing 赋值改成读运行期变量，并提供 debugfs 预设切换：

    /sys/kernel/debug/s3pro_tmg/preset
        0 = 原厂 BOE 40/40/4/200/200   ★默认（保持现有行为）
        1 = sharp / 米板3 8/14/2/55/55
        2 = 自定义（读下面 5 个节点）
    /sys/kernel/debug/s3pro_tmg/vbp vfp hsync hbp hfp

每次 lcm_get_params 都会 pr_alert 打印实际生效值，可 dmesg 核对。

=== 用法 ===
    python patch_stock_timing.py <内核树根> [--dry-run]

开关：STOCK_TIMING=1 时由 build.sh 调用（默认 1）。
"""
import os
import sys

MARK = "S3PRO_STOCK_TIMING"

# 原始（sharp / 小米平板3）赋值块
OLD = '''	params->dsi.vertical_sync_active = 2;
	params->dsi.vertical_backporch = 8;
	params->dsi.vertical_frontporch = 14;
	params->dsi.vertical_active_line = FRAME_HEIGHT;

	params->dsi.horizontal_sync_active = 2;
	params->dsi.horizontal_backporch = 55;
	params->dsi.horizontal_frontporch = 55;
	params->dsi.horizontal_active_pixel = FRAME_WIDTH;'''

# 改成读局部变量
NEW = '''	/* S3PRO_STOCK_TIMING: timing 由 s3pro_tmg_resolve() 按预设给出 */
	params->dsi.vertical_sync_active = 2;
	params->dsi.vertical_backporch = t_vbp;
	params->dsi.vertical_frontporch = t_vfp;
	params->dsi.vertical_active_line = FRAME_HEIGHT;

	params->dsi.horizontal_sync_active = t_hsync;
	params->dsi.horizontal_backporch = t_hbp;
	params->dsi.horizontal_frontporch = t_hfp;
	params->dsi.horizontal_active_pixel = FRAME_WIDTH;'''

# lcm_get_params 函数头锚点
FUNC_HEAD_OLD = '''static void lcm_get_params(LCM_PARAMS *params)
{
	LCM_LOGI("%s enter\\n", __func__);
	memset(params, 0, sizeof(LCM_PARAMS));'''

FUNC_HEAD_NEW = '''static void lcm_get_params(LCM_PARAMS *params)
{
	u32 t_vbp, t_vfp, t_hsync, t_hbp, t_hfp;

	s3pro_tmg_dbg_init();
	s3pro_tmg_resolve(&t_vbp, &t_vfp, &t_hsync, &t_hbp, &t_hfp);
	pr_alert("[S3PRO_TMG] get_params preset=%u vbp/vfp=%u/%u hsync/hbp/hfp=%u/%u/%u\\n",
		 s3pro_tmg_preset, t_vbp, t_vfp, t_hsync, t_hbp, t_hfp);

	LCM_LOGI("%s enter\\n", __func__);
	memset(params, 0, sizeof(LCM_PARAMS));'''

# 插在 lcm_get_params 之前的全局变量与辅助函数
PRELUDE = '''/* S3PRO_STOCK_TIMING: DSI video timing runtime/compile-time switch
 *
 * IMPORTANT: lcm_get_params() is only called once when the display subsystem
 * initialises (normally at boot). Changing the debugfs preset afterwards has NO
 * effect until the next boot, and the variable itself resets on reboot. So the
 * effective preset is read from the kernel cmdline at first use:
 *
 *     s3pro_tmg=0   stock BOE 40/40/4/200/200 (default)
 *     s3pro_tmg=1   sharp / Mi Pad 3 8/14/2/55/55
 *     s3pro_tmg=2   custom, from the vbp/vfp/hsync/hbp/hfp nodes
 *
 * Add it to the boot image cmdline (scripts/set_boot_cmdline.py) and reboot.
 * The debugfs node stays for inspection only.
 */
u32 s3pro_tmg_preset;
u32 s3pro_tmg_vbp;
u32 s3pro_tmg_vfp;
u32 s3pro_tmg_hsync;
u32 s3pro_tmg_hbp;
u32 s3pro_tmg_hfp;

extern char *saved_command_line;

static u32 s3pro_tmg_from_cmdline(void)
{
	const char *p = saved_command_line;
	const char *key = "s3pro_tmg=";
	u32 v = 0;

	if (!p)
		return 0;
	p = strstr(p, key);
	if (!p)
		return 0;
	p += 10;	/* strlen("s3pro_tmg=") == 10, NOT 11 */
	while (*p >= '0' && *p <= '9')
		v = v * 10 + (u32)(*p++ - '0');
	return v;
}

static void s3pro_tmg_dbg_init(void)
{
	static int done;
	struct dentry *d;

	if (done)
		return;
	done = 1;
	s3pro_tmg_preset = s3pro_tmg_from_cmdline();
	pr_alert("[S3PRO_TMG] from cmdline: preset=%u (cmdline_len=%u)\\n",
		 s3pro_tmg_preset,
		 (u32)(saved_command_line ? strlen(saved_command_line) : 0));

	d = debugfs_create_dir("s3pro_tmg", NULL);
	if (IS_ERR_OR_NULL(d)) {
		pr_alert("[S3PRO_TMG] debugfs unavailable, preset=%u (from cmdline)\\n",
			 s3pro_tmg_preset);
		return;
	}
	debugfs_create_u32("preset", 0644, d, &s3pro_tmg_preset);
	debugfs_create_u32("vbp", 0644, d, &s3pro_tmg_vbp);
	debugfs_create_u32("vfp", 0644, d, &s3pro_tmg_vfp);
	debugfs_create_u32("hsync", 0644, d, &s3pro_tmg_hsync);
	debugfs_create_u32("hbp", 0644, d, &s3pro_tmg_hbp);
	debugfs_create_u32("hfp", 0644, d, &s3pro_tmg_hfp);
	pr_alert("[S3PRO_TMG] debugfs ready: /sys/kernel/debug/s3pro_tmg/ preset=%u\\n",
		 s3pro_tmg_preset);
}

static void s3pro_tmg_resolve(u32 *vbp, u32 *vfp, u32 *hsync, u32 *hbp, u32 *hfp)
{
	switch (s3pro_tmg_preset) {
	case 1:
		*vbp = 8; *vfp = 14; *hsync = 2; *hbp = 55; *hfp = 55;
		break;
	case 2:
		*vbp = s3pro_tmg_vbp ? s3pro_tmg_vbp : 40;
		*vfp = s3pro_tmg_vfp ? s3pro_tmg_vfp : 40;
		*hsync = s3pro_tmg_hsync ? s3pro_tmg_hsync : 4;
		*hbp = s3pro_tmg_hbp ? s3pro_tmg_hbp : 200;
		*hfp = s3pro_tmg_hfp ? s3pro_tmg_hfp : 200;
		break;
	default:
		*vbp = 40; *vfp = 40; *hsync = 4; *hbp = 200; *hfp = 200;
		break;
	}
}

'''


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


def brace_balance(s):
    depth = 0
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == '"':
            i += 1
            while i < n:
                if s[i] == "\\":
                    i += 2
                    continue
                if s[i] == '"':
                    break
                i += 1
        elif c == "/" and i + 1 < n and s[i + 1] == "*":
            i += 2
            while i + 1 < n and not (s[i] == "*" and s[i + 1] == "/"):
                i += 1
            i += 1
        elif c == "/" and i + 1 < n and s[i + 1] == "/":
            while i < n and s[i] != "\n":
                i += 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return depth


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

    # 1) 替换 timing 赋值块
    cnt = t.count(OLD)
    if cnt != 1:
        print("!! 时序锚点匹配 %d 次（应为 1），中止" % cnt)
        # 如果已经被别的补丁改成 40/40/... 形态，给出明确提示
        if "vertical_backporch = 40;" in t:
            print("   （看起来时序已是 40/40 形态，可能是旧版本补丁产物）")
        return 1
    t = t.replace(OLD, NEW, 1)
    print("  已把 timing 赋值改成读运行期变量")

    # 2) 在 lcm_get_params 头部插入局部变量 + 计算
    if t.count(FUNC_HEAD_OLD) != 1:
        print("!! lcm_get_params 头部锚点匹配 %d 次（应为 1），中止" % t.count(FUNC_HEAD_OLD))
        return 1
    t = t.replace(FUNC_HEAD_OLD, FUNC_HEAD_NEW, 1)
    print("  已插入 timing 解析逻辑")

    # 3) 在 lcm_get_params 之前插入全局变量 + debugfs
    fi = t.find("static void lcm_get_params(LCM_PARAMS *params)")
    if fi < 0:
        print("!! 找不到 lcm_get_params 定义")
        return 1
    t = t[:fi] + PRELUDE + t[fi:]
    print("  已插入 debugfs 预设节点（s3pro_tmg）")

    # 4) 补 include
    need = []
    for hdr in ("linux/debugfs.h", "linux/err.h"):
        if "#include <%s>" % hdr not in t:
            need.append(hdr)
    if need:
        first_inc = t.find("#include <")
        if first_inc < 0:
            print("!! 找不到 #include")
            return 1
        t = t[:first_inc] + "".join("#include <%s>\n" % h for h in need) + t[first_inc:]
        print("  已补 include: %s" % ", ".join(need))

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(p, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "标记存在": MARK in c,
        "preset 变量": "u32 s3pro_tmg_preset;" in c,
        "debugfs preset 节点": 'debugfs_create_u32("preset"' in c,
        "BOE 预设(40/40/4/200/200)": "*vbp = 40; *vfp = 40; *hsync = 4; *hbp = 200; *hfp = 200;" in c,
        "sharp 预设(8/14/2/55/55)": "*vbp = 8; *vfp = 14; *hsync = 2; *hbp = 55; *hfp = 55;" in c,
        "自定义预设": "s3pro_tmg_vbp ? s3pro_tmg_vbp : 40" in c,
        "get_params 用变量": "params->dsi.vertical_backporch = t_vbp;" in c,
        "pr_alert 打印": "[S3PRO_TMG] get_params" in c,
        "include debugfs": "#include <linux/debugfs.h>" in c,
        "花括号配平": brace_balance(c) == 0,
    }
    print()
    ok = True
    for k, v in checks.items():
        print("  %-28s %s" % (k, "OK" if v else "FAIL"))
        if not v:
            ok = False
    if not ok:
        print("\n!! 校验失败")
        return 1
    print("\n  DSI timing 可切换到补丁完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
