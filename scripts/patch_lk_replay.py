#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LK(cappu bootloader) 上电/复位时序复刻实验台（运行时 debugfs 可切）。

=== 为什么做这个（关键推理）===
驱动原始实现（本树 nt35523_wqxga_dsi_vdo_sharp.c）的 lcm_resume 是：

    VSP=1 ->2ms-> VSN=1 ->24ms -> RST=1 ->12ms-> RST=0 ->12ms-> RST=1 ->32ms -> 推表

这**恰好就是 LK 的时序**（原研究报告实测反推：rst up1→12ms→rst down→12ms→rst up2→32ms），
而且它同时控制 `GPIO_LCD_RST_EN_NEW`（cappu 特有的第二复位脚）。

`patch_lcm_stock.py` 把它换成了 bbk 原厂 7.0 的时序（那块屏不是这块）：

    RST=1 ->5ms-> RST=0 ->2ms-> RST=1 ->25ms -> VSP=1 ->2ms-> VSN=1 ->2ms -> 推表A

即：**顺序反了 + 延时缩水 2~6 倍 + RST_EN_NEW 丢了**。

=== 为什么之前的「改 GPIO 顺序」实验无效 ===
`docs/屏幕黑屏调查全记录.md` §3 排除清单第 5 项写「改 GPIO 上电顺序 → 两种顺序都黑」。
但那次实验在改顺序的同时把**延时也一起换了**，而且用的是 stock 的 `RST_EN`（不含 `RST_EN_NEW`）。
两个变量同时动 = 没控制变量，所以结论不成立。

本补丁把「顺序」「三个延时」「RST_EN_NEW」三者**拆成独立开关**，
可以在同一颗内核上单独变一个、其余保持，做真正的受控实验。

=== 用法 ===
    python patch_lk_replay.py <内核树根> [--dry-run]

运行期（adb root 后）：
    echo 1 > /sys/kernel/debug/s3pro_lk/variant
        0 = 原厂顺序（当前默认，2026-11-26 起）：VSP+TPS -> VSN+TPS -> RST 12/12/32 -> 表
        1 = ★原驱动/LK 完整时序：VSP -> 12 -> VSN -> 24 -> RST 12/12/32 -> 表A
        2 = LK 纯复位时序：RST 12/12/32 -> VSP/VSN -> 表A
        3 = 原驱动顺序 + 加长延时：VSP -> 20 -> VSN -> 20 -> RST 20/20/50 -> 表A
    echo 1 > /sys/kernel/debug/s3pro_lk/rst_new      # 1=RST_EN_NEW 与主 RST 同步（cappu 第二复位脚）
    echo 1 > /sys/kernel/debug/s3pro_lk/vsp_before   # 1=强制 VSP/VSN 先；2=强制 RST 先；0=按 variant
    echo 15 > /sys/kernel/debug/s3pro_lk/dly1        # 单独覆盖 d1(ms)；0 = 用 variant 默认
    echo 2 > /sys/kernel/debug/s3pro_lk/table        # ★0=BOE A(23) 1=BOE B(109) 2=BOE A→B(LK) 3=BOE B→A
                                                     #  4=SHARP(12) 5=SHARP→BOE A 6=BOE A→SHARP
    echo 20 > /sys/kernel/debug/s3pro_lk/table_gap   # 两次推表之间的间隔 ms（默认 20）

判据：
    dmesg | grep S3PRO_LKREPLAY   每次 resume 打印实际参数与实际 jiffies
    肉眼确认是否点亮（screencap 不可信，已写进方法论）
"""
import os
import sys

MARK = "S3PRO_LKREPLAY"

PRELUDE = '''/* S3PRO_LKREPLAY: LK power/reset timing replay bench
 *
 * The driver's original lcm_resume was:
 *   VSP=1 ->2ms-> VSN=1 ->24ms -> RST=1 ->12ms-> RST=0 ->12ms-> RST=1 ->32ms -> push table
 * which matches the cappu LK sequence (rst up1 12ms / rst down 12ms / rst up2 32ms)
 * and also drives GPIO_LCD_RST_EN_NEW (the second reset pin this panel has).
 *
 * patch_lcm_stock.py replaced it with bbk stock 7.0 timing (a different panel):
 *   RST 5/2/25ms, RST before VSP/VSN, and RST_EN_NEW dropped.
 *
 * This bench splits the three variables (order / three delays / RST_EN_NEW) so each
 * can be changed alone, without recompiling.
 *
 * Runtime switches in /sys/kernel/debug/s3pro_lk/:
 *   variant     0=bbk stock(current) 1=original/LK full 2=LK reset-only 3=original+longer
 *   rst_new     1 = drive GPIO_LCD_RST_EN_NEW together with main RST
 *   vsp_before  1 = force VSP/VSN first, 2 = force RST first, 0 = follow variant
 *   dly1/2/3    override each MDELAY in ms, 0 = variant default
 */
u32 s3pro_lk_variant;
u32 s3pro_lk_rst_new;
u32 s3pro_lk_vsp_before;
u32 s3pro_lk_dly1;
u32 s3pro_lk_dly2;
u32 s3pro_lk_dly3;
/* Table selection. NOTE: the original report says LK pushes table A then table B
 * back-to-back (A = unlock + config + display-on, B = the rest), while this driver
 * only ever pushes one of them. Mode 2 reproduces the LK behaviour.
 *   0 = A only (23)  1 = B only (109)  2 = A then B (LK way)  3 = B then A
 */
u32 s3pro_lk_table;
/* gap between the two pushes in mode 2/3, ms */
u32 s3pro_lk_table_gap;
/* Wait between VSN↑ and RST↑, ms. 0 = use the variant default (24ms).
 * The reference kernel that lights this panel uses 15ms here. */
u32 s3pro_lk_vsn_dly;
/* How many times the init table is pushed:
 *   0 = once (default, what this port did)
 *   1 = twice  (what the reference kernel does: resume pushes, then init() pushes again)
 *   2 = three times
 */
u32 s3pro_lk_repeat;
/* ★ resume_power 行为（LOS 14.1 实测：只开 regulator + 20ms，完全不碰 GPIO）
 *   1 = 只等 20ms，不碰 GPIO   ← 默认，与 LOS 14.1 等价
 *   0 = 保持原行为：拉 GPIO_LCD_1V8_EN（sed 后 = gpio_lcd_enp = ENP = pio96）+ 20ms
 * 每次唤醒都会重新读这个变量，所以改完下次熄屏唤醒就生效，不需要重启。
 */
u32 s3pro_lk_no_power = 1;

/* Sharp native init table, copied from kernel-patches/lcm-table/sharp_tables.c
 * (Copyright 2018 XiaoMi, the panel this device actually has, wm 1536x2048).
 *
 * It is COMPLETELY DIFFERENT from the BOE table (23 items, starts with the
 * 0xFF AA55A580 unlock). Sharp is 12 items, no 0xFF unlock, and uses
 *   0xCE{F0,1F} / 0xCE{06} / 0xD7{00,2F} / 0x51{FF} / 0x55{02} / 0x53{24}
 * instead. Keeping both compiled in lets us cross table x timing at runtime.
 */
static struct LCM_setting_table s3pro_sharp_table[] = {
	{0x11, 0, {} },
	{REGFLAG_DELAY, 120, {} },
	{0xF0, 5, {0x55, 0xAA, 0x52, 0x08, 0x05} },
	{0xCE, 2, {0xF0, 0x1F} },
	{0xF0, 5, {0x55, 0xAA, 0x52, 0x08, 0x01} },
	{0xCE, 1, {0x06} },
	{0xD7, 2, {0x00, 0x2F} },
	{0x51, 1, {0xFF} },
	{0x55, 1, {0x02} },
	{0x53, 1, {0x24} },
	{0x29, 0, {} },
	{REGFLAG_END_OF_TABLE, 0x00, {} },
};

static void s3pro_lk_dbg_init(void)
{
	static int done;
	struct dentry *d;

	if (done)
		return;
	done = 1;

	d = debugfs_create_dir("s3pro_lk", NULL);
	if (IS_ERR_OR_NULL(d)) {
		pr_alert("[S3PRO_LKREPLAY] debugfs unavailable, using compile-time defaults\\n");
		return;
	}
	debugfs_create_u32("variant", 0644, d, &s3pro_lk_variant);
	debugfs_create_u32("rst_new", 0644, d, &s3pro_lk_rst_new);
	debugfs_create_u32("vsp_before", 0644, d, &s3pro_lk_vsp_before);
	debugfs_create_u32("dly1", 0644, d, &s3pro_lk_dly1);
	debugfs_create_u32("dly2", 0644, d, &s3pro_lk_dly2);
	debugfs_create_u32("dly3", 0644, d, &s3pro_lk_dly3);
	debugfs_create_u32("table", 0644, d, &s3pro_lk_table);
	debugfs_create_u32("table_gap", 0644, d, &s3pro_lk_table_gap);
	debugfs_create_u32("vsn_dly", 0644, d, &s3pro_lk_vsn_dly);
	debugfs_create_u32("repeat", 0644, d, &s3pro_lk_repeat);
	debugfs_create_u32("no_power", 0644, d, &s3pro_lk_no_power);
	pr_alert("[S3PRO_LKREPLAY] debugfs ready: /sys/kernel/debug/s3pro_lk/\\n");
}

'''

NEW_RESUME = '''static void lcm_resume(void)
{
	u32 d1, d2, d3, vsn_d;
	u32 reps, gap;
	int vsp_first;
	int use_rst_new;
	int tps_ret;

	s3pro_lk_dbg_init();

	vsn_d = 24;	/* default: the value this port always used */

	switch (s3pro_lk_variant) {
	case 1:  d1 = 12; d2 = 12; d3 = 32; vsp_first = 1; break;	/* original / LK full */
	case 2:  d1 = 12; d2 = 12; d3 = 32; vsp_first = 0; break;	/* LK reset only */
	case 3:  d1 = 20; d2 = 20; d3 = 50; vsp_first = 1; break;	/* original + longer */
	case 4:  d1 = 5;  d2 = 2;  d3 = 15; vsp_first = 1; vsn_d = 15; break;	/* EXACT reference kernel */
	default: d1 = 12; d2 = 12; d3 = 32; vsp_first = 1; break;	/* 原厂顺序（2026-11-26 改为默认） */
	}
	if (s3pro_lk_vsp_before == 1)
		vsp_first = 1;
	else if (s3pro_lk_vsp_before == 2)
		vsp_first = 0;
	if (s3pro_lk_dly1)
		d1 = s3pro_lk_dly1;
	if (s3pro_lk_dly2)
		d2 = s3pro_lk_dly2;
	if (s3pro_lk_dly3)
		d3 = s3pro_lk_dly3;
	if (s3pro_lk_vsn_dly)
		vsn_d = s3pro_lk_vsn_dly;

	use_rst_new = (s3pro_lk_rst_new != 0);
	reps = (s3pro_lk_repeat >= 2) ? 3 : ((s3pro_lk_repeat == 1) ? 2 : 1);
	gap = s3pro_lk_table_gap ? s3pro_lk_table_gap : 20;

	pr_alert("[S3PRO_LKREPLAY] resume var=%u vsp_first=%d rst_new=%d dly=%u/%u/%u vsn_dly=%u reps=%u j=%lu\\n",
		 s3pro_lk_variant, vsp_first, use_rst_new, d1, d2, d3, vsn_d, reps, jiffies);

	if (vsp_first) {
		lcm_set_gpio_output(GPIO_LCD_VSP_EN, GPIO_OUT_ONE);
		pr_alert("[S3PRO_LKREPLAY] VSP=1 j=%lu\\n", jiffies);
		MDELAY(2);
		/* S3PRO_LKTPS: TPS65132 正电源使能。VSP_EN 只打开芯片 EN 脚，
		 * 芯片要收到 I2C 命令才输出 VPOS；缺这条写面板正电源就是 0V。
		 * 原厂驱动有这两条，本实验台以前把它们丢了。 */
		tps_ret = tps65132_write_bytes(0x00, 0x10);
		pr_alert("[S3PRO_LKREPLAY] tps65132 pos ret=%d j=%lu\\n", tps_ret, jiffies);
		MDELAY(12);
		lcm_set_gpio_output(GPIO_LCD_VSN_EN, GPIO_OUT_ONE);
		pr_alert("[S3PRO_LKREPLAY] VSN=1 then wait %ums j=%lu\\n", vsn_d, jiffies);
		MDELAY(2);
		tps_ret = tps65132_write_bytes(0x01, 0x10);
		pr_alert("[S3PRO_LKREPLAY] tps65132 neg ret=%d j=%lu\\n", tps_ret, jiffies);
		MDELAY(vsn_d);
	}

	lcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ONE);
	if (use_rst_new)
		lcm_set_gpio_output(GPIO_LCD_RST_EN_NEW, GPIO_OUT_ONE);
	pr_alert("[S3PRO_LKREPLAY] RST=1 j=%lu\\n", jiffies);
	MDELAY(d1);

	lcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ZERO);
	if (use_rst_new)
		lcm_set_gpio_output(GPIO_LCD_RST_EN_NEW, GPIO_OUT_ZERO);
	pr_alert("[S3PRO_LKREPLAY] RST=0 j=%lu after=%ums\\n", jiffies, d1);
	MDELAY(d2);

	lcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ONE);
	if (use_rst_new)
		lcm_set_gpio_output(GPIO_LCD_RST_EN_NEW, GPIO_OUT_ONE);
	pr_alert("[S3PRO_LKREPLAY] RST=1 j=%lu after=%ums\\n", jiffies, d2);
	MDELAY(d3);

	if (!vsp_first) {
		lcm_set_gpio_output(GPIO_LCD_VSP_EN, GPIO_OUT_ONE);
		pr_alert("[S3PRO_LKREPLAY] VSP=1 j=%lu\\n", jiffies);
		MDELAY(2);
		tps_ret = tps65132_write_bytes(0x00, 0x10);
		pr_alert("[S3PRO_LKREPLAY] tps65132 pos ret=%d j=%lu\\n", tps_ret, jiffies);
		MDELAY(12);
		lcm_set_gpio_output(GPIO_LCD_VSN_EN, GPIO_OUT_ONE);
		pr_alert("[S3PRO_LKREPLAY] VSN=1 j=%lu\\n", jiffies);
		MDELAY(2);
		tps_ret = tps65132_write_bytes(0x01, 0x10);
		pr_alert("[S3PRO_LKREPLAY] tps65132 neg ret=%d j=%lu\\n", tps_ret, jiffies);
		MDELAY(2);
	}

	{
		u32 i;

		/* The reference kernel pushes the init table once in resume and then
		 * calls init(), which pushes the SAME table again. s3pro_lk_repeat
		 * reproduces that: 0 = once, 1 = twice, 2 = three times. */
		for (i = 0; i < reps; i++) {
			if (i > 0) {
				pr_alert("[S3PRO_LKREPLAY] re-push table #%u gap=%ums j=%lu\\n",
					 i + 1, gap, jiffies);
				MDELAY(gap);
			}

		switch (s3pro_lk_table) {
		case 1:
			pr_alert("[S3PRO_LKREPLAY] push BOE table B (109) j=%lu\\n", jiffies);
			push_table(s3pro_init_table_b,
				   sizeof(s3pro_init_table_b) / sizeof(struct LCM_setting_table), 1);
			break;
		case 2:
			pr_alert("[S3PRO_LKREPLAY] push BOE A (23) then BOE B (109) gap=%ums j=%lu\\n",
				 gap, jiffies);
			push_table(s3pro_init_table_a,
				   sizeof(s3pro_init_table_a) / sizeof(struct LCM_setting_table), 1);
			MDELAY(gap);
			push_table(s3pro_init_table_b,
				   sizeof(s3pro_init_table_b) / sizeof(struct LCM_setting_table), 1);
			break;
		case 3:
			pr_alert("[S3PRO_LKREPLAY] push BOE B (109) then BOE A (23) gap=%ums j=%lu\\n",
				 gap, jiffies);
			push_table(s3pro_init_table_b,
				   sizeof(s3pro_init_table_b) / sizeof(struct LCM_setting_table), 1);
			MDELAY(gap);
			push_table(s3pro_init_table_a,
				   sizeof(s3pro_init_table_a) / sizeof(struct LCM_setting_table), 1);
			break;
		case 4:
			pr_alert("[S3PRO_LKREPLAY] push SHARP table (12) j=%lu\\n", jiffies);
			push_table(s3pro_sharp_table,
				   sizeof(s3pro_sharp_table) / sizeof(struct LCM_setting_table), 1);
			break;
		case 5:
			pr_alert("[S3PRO_LKREPLAY] push SHARP (12) then BOE A (23) gap=%ums j=%lu\\n",
				 gap, jiffies);
			push_table(s3pro_sharp_table,
				   sizeof(s3pro_sharp_table) / sizeof(struct LCM_setting_table), 1);
			MDELAY(gap);
			push_table(s3pro_init_table_a,
				   sizeof(s3pro_init_table_a) / sizeof(struct LCM_setting_table), 1);
			break;
		case 6:
			pr_alert("[S3PRO_LKREPLAY] push BOE A (23) then SHARP (12) gap=%ums j=%lu\\n",
				 gap, jiffies);
			push_table(s3pro_init_table_a,
				   sizeof(s3pro_init_table_a) / sizeof(struct LCM_setting_table), 1);
			MDELAY(gap);
			push_table(s3pro_sharp_table,
				   sizeof(s3pro_sharp_table) / sizeof(struct LCM_setting_table), 1);
			break;
		default:
			pr_alert("[S3PRO_LKREPLAY] push BOE table A (23) j=%lu\\n", jiffies);
			push_table(s3pro_init_table_a,
				   sizeof(s3pro_init_table_a) / sizeof(struct LCM_setting_table), 1);
			break;
		}
		}
	}
	pr_alert("[S3PRO_LKREPLAY] table done (reps=%u) j=%lu\\n", reps, jiffies);

	/* NOTE: patch_dimming_early.py anchors on this exact line, keep it intact */
	schedule_delayed_work(&open_lcm_dimming, msecs_to_jiffies(500));

	LCM_LOGI("%s exit\\n", __func__);
}
'''

RESUME_START = "static void lcm_resume(void)"
RESUME_END_HINT = "/* 旧初始化表/睡眠表仍留在文件里但不再被调用"

# ---- lcm_resume_power：LOS 14.1 的 resume_power 不碰 GPIO（regulator 供 1.8V）----
# 锚点是 patch_lcm_stock.py 重写后的样子
RESUME_POWER_OLD = '''static void lcm_resume_power(void)
{
\tLCM_LOGI("%s, begin\\n", __func__);

\t/* 原厂 resume_power：1V8 上电 + 20ms */
\tlcm_set_gpio_output(GPIO_LCD_1V8_EN, GPIO_OUT_ONE);
\tMDELAY(20);

\tLCM_LOGI("%s, end\\n", __func__);
}'''

RESUME_POWER_NEW = '''static void lcm_resume_power(void)
{
\tLCM_LOGI("%s, begin\\n", __func__);

\t/* S3PRO_LKREPLAY: LOS 14.1 实测的 resume_power 是
\t *     lcm_vgp_supply_enable()  (regulator_set_voltage 1.8V + enable)
\t *     mdelay(20)
\t * 完全不碰任何 GPIO。1.8V 由 PMIC 的 VGP LDO 提供（DTB: reg-lcm-1v8-supply）。
\t *
\t * 而我们的 GPIO_LCD_1V8_EN 被 build.sh 的 sed 映射成了 gpio_lcd_enp
\t * （= ENP = pio 96，和 VSP 同一个脚），原代码会在 resume 之前就
\t * 把 ENP 拉高，比 LOS 14.1 提前了一段。
\t *
\t * no_power=1（默认）-> 只等 20ms，与 LOS 14.1 等价
\t * no_power=0        -> 保持原行为（拉 ENP + 20ms），用于对照
\t * 每次唤醒都会读这个变量，改完下次熄屏唤醒即生效，无需重启。
\t */
\tpr_alert("[S3PRO_LKREPLAY] resume_power no_power=%u\\n", s3pro_lk_no_power);
\tif (s3pro_lk_no_power) {
\t\tMDELAY(20);
\t} else {
\t\tlcm_set_gpio_output(GPIO_LCD_1V8_EN, GPIO_OUT_ONE);
\t\tMDELAY(20);
\t}

\tLCM_LOGI("%s, end\\n", __func__);
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
    if name:
        d = os.path.join(base, name)
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.endswith(".c"):
                    p = os.path.join(d, f)
                    t = open(p, encoding="utf-8", errors="replace").read()
                    if "lcm_initialization_setting[]" in t:
                        return p
    for root, _d, files in os.walk(base):
        if "nt35523" not in root:
            continue
        for f in files:
            if f.endswith(".c"):
                p = os.path.join(root, f)
                t = open(p, encoding="utf-8", errors="replace").read()
                if "lcm_initialization_setting[]" in t:
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
        elif c == "'":
            i += 1
            while i < n:
                if s[i] == "\\":
                    i += 2
                    continue
                if s[i] == "'":
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
        raise SystemExit("找不到面板驱动")
    print("  驱动: %s" % os.path.relpath(p, tree))

    t = open(p, encoding="utf-8", errors="surrogateescape").read()

    if MARK in t:
        print("  已打过 LK 时序实验台补丁，跳过")
        return 0

    if "s3pro_lcm_variant" not in t:
        print("!! 未找到 s3pro_lcm_variant —— 必须先跑 patch_lcm_stock.py")
        return 1
    if "GPIO_LCD_RST_EN_NEW" not in t:
        print("!! 未找到 GPIO_LCD_RST_EN_NEW —— 无法做第二复位脚实验")
        return 1

    si = t.find(RESUME_START)
    if si < 0:
        print("!! 找不到 lcm_resume")
        return 1
    if t.count(RESUME_START) != 1:
        print("!! lcm_resume 锚点匹配 %d 次（应为 1），中止" % t.count(RESUME_START))
        return 1

    ei = t.find(RESUME_END_HINT, si)
    if ei < 0:
        print("!! 找不到 lcm_resume 结束锚点")
        return 1

    old_fn = t[si:ei]
    if "schedule_delayed_work(&open_lcm_dimming" not in old_fn:
        print("!! 旧 lcm_resume 里没有 schedule_delayed_work，结构不符，中止")
        return 1
    if "GPIO_LCD_RST_EN" not in old_fn:
        print("!! 旧 lcm_resume 里没有 GPIO_LCD_RST_EN，结构不符，中止")
        return 1
    if brace_balance(old_fn) != 0:
        print("!! 旧 lcm_resume 花括号不配平（%d），中止" % brace_balance(old_fn))
        return 1

    t = t[:si] + PRELUDE + NEW_RESUME + "\n" + t[ei:]
    print("  已替换 lcm_resume 为可调时序版本（含 RST_EN_NEW 开关）")

    # lcm_resume_power：加 no_power 开关（默认不碰 GPIO，对齐 LOS 14.1）
    if t.count(RESUME_POWER_OLD) != 1:
        print("!! lcm_resume_power 锚点匹配 %d 次（应为 1），中止"
              % t.count(RESUME_POWER_OLD))
        print("   （patch_lcm_stock.py 的 resume_power 模板可能变了？）")
        return 1
    t = t.replace(RESUME_POWER_OLD, RESUME_POWER_NEW, 1)
    print("  已给 lcm_resume_power 加 no_power 开关（默认=1，不碰 GPIO）")

    need = []
    for hdr in ("linux/debugfs.h", "linux/err.h", "linux/jiffies.h"):
        if "#include <%s>" % hdr not in t:
            need.append(hdr)
    if need:
        first_inc = t.find("#include <")
        if first_inc < 0:
            print("!! 找不到任何 #include，无法插入")
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
        "变量已定义": "u32 s3pro_lk_variant;" in c,
        "debugfs variant 节点": 'debugfs_create_u32("variant"' in c,
        "debugfs rst_new 节点": 'debugfs_create_u32("rst_new"' in c,
        "debugfs table 节点": 'debugfs_create_u32("table"' in c,
        "debugfs vsn_dly 节点": 'debugfs_create_u32("vsn_dly"' in c,
        "debugfs repeat 节点": 'debugfs_create_u32("repeat"' in c,
        "debugfs no_power 节点": 'debugfs_create_u32("no_power"' in c,
        "no_power 变量默认1": "u32 s3pro_lk_no_power = 1;" in c,
        "resume_power 用 no_power": "if (s3pro_lk_no_power) {" in c,
        "resume_power 仍保留原分支": "lcm_set_gpio_output(GPIO_LCD_1V8_EN, GPIO_OUT_ONE);" in c,
        "variant4 精确复刻(5/2/15)": "case 4:  d1 = 5;  d2 = 2;  d3 = 15;" in c,
        "vsn_dly 变量生效": "MDELAY(vsn_d);" in c,
        "二次推表循环": "re-push table #%u" in c,
        "table done 带 reps": "table done (reps=%u)" in c,
        "BOE A→B 连发": "push BOE A (23) then BOE B (109)" in c,
        "BOE B→A 连发": "push BOE B (109) then BOE A (23)" in c,
        "sharp 表定义": "s3pro_sharp_table[]" in c,
        "sharp 表特征 0xCE": "0xCE, 2, {0xF0, 0x1F}" in c,
        "sharp 表分支": "push SHARP table (12)" in c,
        "sharp+BOE 组合": "push SHARP (12) then BOE A (23)" in c,
        "BOE+sharp 组合": "push BOE A (23) then SHARP (12)" in c,
        "pr_alert 时间戳": "[S3PRO_LKREPLAY] resume" in c,
        "下游锚点保留": "schedule_delayed_work(&open_lcm_dimming, msecs_to_jiffies(500));" in c,
        "RST_EN_NEW 开关": "GPIO_LCD_RST_EN_NEW" in c,
        "VSP 顺序开关": "s3pro_lk_vsp_before" in c,
        "延时覆盖开关": "s3pro_lk_dly1" in c,
        "include debugfs": "#include <linux/debugfs.h>" in c,
        "花括号配平": brace_balance(c) == 0,
    }
    print()
    ok = True
    for k, v in checks.items():
        print("  %-20s %s" % (k, "OK" if v else "FAIL"))
        if not v:
            ok = False
    if not ok:
        print("\n!! 校验失败")
        return 1
    print("\n  LK 时序实验台补丁完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
