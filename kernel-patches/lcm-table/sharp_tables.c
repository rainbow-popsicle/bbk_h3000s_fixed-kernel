/* 夏普 WQXGA (1536x2048) 屏的原生初始化表 —— 来自 GitHub 干净树
 * drivers/misc/mediatek/lcm/nt35523_wqxga_dsi_vdo_sharp/nt35523_wqxga_dsi_vdo_sharp.c
 * （Copyright 2018 XiaoMi，设备实际面板，wm size = 1536x2048）
 *
 * 2026-11-22：替换原先错误使用的 BOE FHD 表（stock_tables.c，从 bbk 原厂
 * 3.18.35 反汇编、属于另一块屏）。用 BOE 表初始化夏普屏 → 寄存器配置全错 →
 * LK 初始化能亮（LK 用夏普表），kernel 熄屏重建用 BOE 表 → 黑屏。
 *
 * 表A = 夏普初始化表（12 项）；表B 同表A（variant=0 默认走表A，表B 仅占位）。
 * 睡眠表 = 夏普 deep sleep 表（6 项）。 */

/* 睡眠表（夏普 lcm_deep_sleep_mode_in_setting） */
static struct LCM_setting_table s3pro_sleep_table[] = {
	{0x53, 1, {0x00} },
	{0x28, 0, {} },
	{REGFLAG_DELAY, 100, {} },
	{0x10, 0, {} },
	{REGFLAG_DELAY, 10, {} },
	{REGFLAG_END_OF_TABLE, 0x00, {} },
};

/* 初始化表 A（夏普原生，12 项） */
static struct LCM_setting_table s3pro_init_table_a[] = {
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

/* 初始化表 B（占位：与表A 相同，variant=0 默认不用） */
static struct LCM_setting_table s3pro_init_table_b[] = {
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
