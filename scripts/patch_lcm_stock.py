#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按原厂 nt35523_fhd_vdo_boe 驱动重写面板驱动的 suspend/resume。

=== 依据（反汇编原厂 3.18.35 内核得到，非猜测）===
原厂 LCM_DRIVER 结构体 @0xe8bcd0：
  +0x18 init          0x3d9198   variant → push(表A 23 项) 或 push(表B 109 项)
  +0x20 suspend       0x3d916c   push(睡眠表 4 项)，不动 GPIO、不断电
  +0x28 resume        0x3d9240   rst=1→5ms→rst=0→2ms→rst=1→25ms
                                 → vsp=1→2ms→vsn=1→2ms → push(表A 或 表B)
  +0x38 suspend_power 0x3d9390   rst=0→5ms, vsn=0→2ms, vsp=0→2ms
  +0x40 resume_power  0x3d8ed8   1V8 上电 + 20ms

数据表紧邻存放于 0xe89690：
  +0x000 睡眠表 4 项    {0x28},{0x10},DELAY 120,END
  +0x120 初始化表 A 23 项
  +0x798 初始化表 B 109 项

=== 原实现（本树驱动）的问题 ===
  * GPIO 顺序相反：先 VSP/VSN，再复位（原厂是先复位再 VSP/VSN）
  * 延时不同：12/12/32/24/12 vs 原厂 5/2/25/2/2
  * 把表A+表B 当成一张 132 项表全推（原厂二选一）
  * 睡眠表 5 项（含 0x53 与两段延时），原厂只有 4 项
  * suspend 里调 ddp_dsi_power_off 并拉低所有 rails、还写 tps65132（本机无此 IC）

=== 用法 ===
    python patch_lcm_stock.py <内核树根> --tables <stock_tables.c> [--dry-run]
"""
import os
import sys

MARK = "S3PRO_STOCK_LCM"

VARIANT_VAR = '''/* S3PRO_STOCK_LCM: 选择初始化表
 *   1 = 表B（109 项，原厂 variant==1 分支）
 *   0 = 表A（23 项，原厂 variant==0 分支）
 * S3PRO_CLEANUP: 运行时节点已移除（表A/表B 实验均已做、都黑，无结论差异）。
 * 固定为最后一次实测状态 0（表A），保持当前行为不变。 */
int s3pro_lcm_variant = 0;

'''

NEW_REGION = r'''/* S3PRO_KEEP_POWER: suspend 时是否跳断电（1=跳过，保持面板供电）
 * 定义在 patch_lk_replay.py，debugfs 节点 /sys/kernel/debug/s3pro_lk/keep_power
 * 动机：tps65132_probe 未被调用 -> i2_client 为 NULL -> resume 无法重新使能
 * TPS65132。不断电则 TPS65132 寄存器（LK 配好）保留，EN 拉高即恢复输出。 */
extern int s3pro_keep_power;

static void lcm_suspend(void)
{
	LCM_LOGI("%s enter\n", __func__);

	cancel_delayed_work_sync(&open_lcm_dimming);

	/* 原厂 suspend：只推睡眠表（0x28 display-off / 0x10 sleep-in / 120ms）。
	 * 不动 GPIO、不断电、不写 tps65132 —— 断电由框架 dpmgr_path_power_off
	 * 和 suspend_power 负责。 */
	push_table(s3pro_sleep_table,
		   sizeof(s3pro_sleep_table) / sizeof(struct LCM_setting_table), 1);

	LCM_LOGI("%s exit\n", __func__);
}

static void lcm_suspend_power(void)
{
	LCM_LOGI("%s enter\n", __func__);

	/* S3PRO_KEEP_POWER: keep_power=1 时完全跳过断电，让 TPS65132 的寄存器
	 * （以及面板内部状态）原样保留到下次 resume。用于验证「断电是否是
	 * 面板起不来的原因」——因为 tps65132 的 i2c client 是 NULL，
	 * 一旦断电，内核没有办法在 resume 时重新把 TPS65132 打开。 */
	if (s3pro_keep_power) {
		LCM_LOGI("%s: keep_power=1, skip power-off\n", __func__);
		LCM_LOGI("%s exit\n", __func__);
		return;
	}

	/* 原厂 suspend_power：按上电的逆序掉电 */
	lcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ZERO);
	MDELAY(5);
	lcm_set_gpio_output(GPIO_LCD_VSN_EN, GPIO_OUT_ZERO);
	MDELAY(2);
	lcm_set_gpio_output(GPIO_LCD_VSP_EN, GPIO_OUT_ZERO);
	MDELAY(2);

	LCM_LOGI("%s exit\n", __func__);
}

static void lcm_resume(void)
{
	LCM_LOGI("%s enter\n", __func__);

	/* ===== S3PRO_TPS65132 (2026-11-26 关键修复) =====
	 * 原实现只拉 VSP_EN/VSN_EN 两个 GPIO，但那只打开了 TPS65132 的 EN 脚，
	 * 芯片本身要通过 I2C 写 0x00/0x01 才会输出 VPOS/VNEG。缺了这两条写，
	 * 面板正负电源就是 0V —— 实测唤醒后 read_id 全 0、trig=0、面板完全不响应。
	 * 健康态（LK 初始化）能读到 0x9C，正是因为它配好了 TPS65132。
	 *
	 * 顺序也按原厂 inputs 驱动：先 VSP+TPS 再 VSN+TPS，最后 RST 三段。
	 * （旧实现在没电的情况下先按 RST，属于顺序颠倒。） */
	{
		unsigned char cmd = 0x00;
		unsigned char data = 0x10;
		int tps_ret;

		LCM_LOGI("enable vsp + tps65132 pos\n");
		lcm_set_gpio_output(GPIO_LCD_VSP_EN, GPIO_OUT_ONE);
		MDELAY(2);
		tps_ret = tps65132_write_bytes(cmd, data);
		if (tps_ret < 0)
			LCM_LOGI("tps65132-cmd=0x%x i2c write error\n", cmd);
		else
			LCM_LOGI("tps65132-cmd=0x%x i2c write success\n", cmd);
		MDELAY(12);

		LCM_LOGI("enable vsn + tps65132 neg\n");
		lcm_set_gpio_output(GPIO_LCD_VSN_EN, GPIO_OUT_ONE);
		MDELAY(2);
		cmd = 0x01;
		tps_ret = tps65132_write_bytes(cmd, data);
		if (tps_ret < 0)
			LCM_LOGI("tps65132-cmd=0x%x i2c write error\n", cmd);
		else
			LCM_LOGI("tps65132-cmd=0x%x i2c write success\n", cmd);
		MDELAY(24);
	}

	LCM_LOGI("rst up1\n");
	lcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ONE);
	MDELAY(12);

	LCM_LOGI("rst down\n");
	lcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ZERO);
	MDELAY(12);

	LCM_LOGI("rst up2\n");
	lcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ONE);
	MDELAY(32);

	/* 原厂按 variant 二选一；这里用运行时变量 */
	if (s3pro_lcm_variant) {
		LCM_LOGI("push init table B (109)\n");
		push_table(s3pro_init_table_b,
			   sizeof(s3pro_init_table_b) / sizeof(struct LCM_setting_table), 1);
	} else {
		LCM_LOGI("push init table A (23)\n");
		push_table(s3pro_init_table_a,
			   sizeof(s3pro_init_table_a) / sizeof(struct LCM_setting_table), 1);
	}

	schedule_delayed_work(&open_lcm_dimming, msecs_to_jiffies(500));

	LCM_LOGI("%s exit\n", __func__);
}

/* 旧初始化表/睡眠表仍留在文件里但不再被调用（改用原厂 s3pro_* 三张表）。
 * 内核开了 -Werror=unused-variable，必须显式引用一次，否则编译失败。 */
static void __attribute__((unused)) s3pro_keep_legacy_tables(void)
{
	(void)lcm_initialization_setting;
	(void)lcm_deep_sleep_mode_in_setting;
}

/* 这两行原本在 lcm_set_disp_param 之前，被本补丁的替换区间吃掉了，必须补回：
 * 少了 set_panel_disp_param 的声明会报 implicit declaration 错误。 */
typedef void (*send_cmd)(struct LCM_setting_table *table, unsigned int count, unsigned char force_update);
extern int set_panel_disp_param(unsigned int param, send_cmd p_func);

static void lcm_set_disp_param(unsigned int param)
{
	int ret;

	LCM_LOGI("lcm_set_disp_paramt: param = %d\n", param);
	ret = set_panel_disp_param(param, push_table);
	(void)ret;
}

static void lcm_resume_power(void)
{
	LCM_LOGI("%s, begin\n", __func__);

	/* 原厂 resume_power：1V8 上电 + 20ms */
	lcm_set_gpio_output(GPIO_LCD_1V8_EN, GPIO_OUT_ONE);
	MDELAY(20);

	LCM_LOGI("%s, end\n", __func__);
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


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv
    tables_path = None
    if "--tables" in sys.argv:
        tables_path = sys.argv[sys.argv.index("--tables") + 1]
    if not tables_path or not os.path.exists(tables_path):
        raise SystemExit("缺少 --tables 或文件不存在: %s" % tables_path)

    p = find_driver(tree)
    if not p:
        raise SystemExit("找不到面板驱动")
    print("  驱动: %s" % os.path.relpath(p, tree))

    t = open(p, encoding="utf-8", errors="surrogateescape").read()
    if MARK in t:
        print("  已打过原厂重写补丁，跳过")
        return 0

    tables = open(tables_path, encoding="utf-8", errors="replace").read()

    # 1) 在 lcm_suspend 之前插入数据表 + variant 变量
    anchor = "static void lcm_suspend(void)"
    if t.count(anchor) != 1:
        print("!! lcm_suspend 锚点匹配 %d 次（应为 1），中止" % t.count(anchor))
        return 1
    t = t.replace(anchor, tables + "\n" + VARIANT_VAR + anchor, 1)
    print("  已插入原厂数据表（睡眠 4 / 表A 23 / 表B 109）")

    # 2) 替换整段回调实现（锚点唯一，直接定位，不插入任何标记文本）
    start = t.find(anchor)
    if start < 0:
        print("!! 找不到 lcm_suspend")
        return 1
    end = t.find("LCM_DRIVER nt35523", start)
    if end < 0:
        print("!! 找不到 LCM_DRIVER 结构体")
        return 1
    old = t[start:end]
    if "lcm_resume_power" not in old or "tps65132_write_bytes" not in old:
        print("!! 待替换区间内容不符合预期（缺少 resume_power / tps65132）")
        return 1
    t = t[:start] + NEW_REGION + t[end:]
    print("  已按原厂重写 suspend / suspend_power / resume / resume_power")

    # 3) 结构体挂上 suspend_power
    old_s = "\t.resume_power   = lcm_resume_power,"
    if old_s not in t:
        print("!! 结构体锚点未找到")
        return 1
    t = t.replace(old_s,
                  "\t.suspend_power  = lcm_suspend_power,\n\t.resume_power   = lcm_resume_power,", 1)
    print("  已挂上 suspend_power 回调")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    # 4) 校验
    c = open(p, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "标记存在": MARK in c,
        "睡眠表已插入": "s3pro_sleep_table[]" in c,
        "表A 已插入": "s3pro_init_table_a[]" in c,
        "表B 已插入": "s3pro_init_table_b[]" in c,
        "variant 固定为表A(实测0)": "int s3pro_lcm_variant = 0;" in c,
        "先VSP后RST（原厂顺序）": (
            c.find("GPIO_LCD_VSP_EN, GPIO_OUT_ONE);") >= 0
            and c.find("tps65132_write_bytes(cmd") >= 0
            and c.find("GPIO_LCD_VSP_EN, GPIO_OUT_ONE);") < c.find("rst up1")
        ),
        "suspend_power 已实现": "static void lcm_suspend_power(void)" in c,
        "suspend_power 已挂接": ".suspend_power  = lcm_suspend_power," in c,
        "set_panel_disp_param 声明在调用前": ("extern int set_panel_disp_param(" in c
                                        and c.find("extern int set_panel_disp_param(") < c.find("ret = set_panel_disp_param(")),
        "send_cmd typedef 存在": "typedef void (*send_cmd)" in c,
        "旧表已引用(避免-unused)": "s3pro_keep_legacy_tables" in c,
        "含 tps65132 调用（原厂行为）": "tps65132_write_bytes(cmd" in c,
        "已移除 ddp_dsi_power_off": "ddp_dsi_power_off(32" not in c,
        "花括号配平": c.count("{") == c.count("}"),
    }
    print()
    bad = 0
    for k, v in checks.items():
        print("    %-26s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1
    if bad:
        return 1
    print("\n原厂驱动重写完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
