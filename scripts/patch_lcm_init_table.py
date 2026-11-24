#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""让 lcm_init() 像 LOS 14.1 一样做「三级选表 + 推表」。

=== 为什么（LOS 14.1 反汇编依据，见 docs §8.1）===

LOS 14.1 的 lcm_init @0x458344 是三级选表：

    vendor id [x19+0xd8] == 1  -> 291 项表 @0xf097b0
    否则读 GPIO lcd_id (pio 89) :
        电平 == 0 ->  23 项表 @0xf0e988 (BOE，首项 FF{AA55A580})
        电平 == 1 -> 109 项表 @0xf0f000 (首项 F0{55AA520800})
        其它      -> 报错，不推表
    推送统一走 push_table(table, count)

vendor id 来自 cmdline：get_lcm_vendor_id @0x458608 在 g_cmdline 里找
"lcm_vendor_id="，取第 14 字节 - '0'。本机 cmdline 没有该字段 -> 0。

而我们的 lcm_init() 只有两行：

    static void lcm_init(void)
    {
        LCM_LOGI("%s enter\\n", __func__);
        lcm_get_gpio_infor();
        update_hardware_info(TYPE_PANEL, 5);   // 从不推表
    }

后果：开机时 LOS 14.1 由 kernel 主动推一次表；我们完全不推，开机亮全靠 LK。
唤醒时 LOS 14.1 是「lcm_resume 末尾 bl lcm_init」走同一条选表路径，我们是直接 push。
两边「kernel 对面板状态的认知」因此不同 —— 这是唯一还没测过的结构性差异。

=== 本补丁 ===
把 lcm_init() 换成三级选表 + push_table，并全程 pr_alert 打印：
    [S3PRO_LCM_INIT] cmdline vendor_id=N
    [S3PRO_LCM_INIT] lcd_id gpio=978 level=0
    [S3PRO_LCM_INIT] level=0 -> 23-item BOE table
刷机后 `dmesg | grep S3PRO_LCM_INIT` 即可看到实际电平与选中的表。

291 项表未编译进本驱动（本机 vendor_id=0 永远走不到），
若 vendor_id==1 会打印 "291-item table NOT compiled, skip" 并安全返回。

=== 用法 ===
    python patch_lcm_init_table.py <内核树根> [--dry-run]
开关：LCM_INIT_TABLE=1（默认 1）。必须在 patch_lcm_stock.py 之后运行。
"""
import os
import sys

MARK = "S3PRO_LCM_INIT"

OLD = '''static void lcm_init(void)
{
	LCM_LOGI("%s enter\\n", __func__);
	lcm_get_gpio_infor();
	update_hardware_info(TYPE_PANEL, 5);
}'''

NEW = '''/* S3PRO_LCM_INIT: 读 cmdline 的 lcm_vendor_id=N（LOS 14.1 get_lcm_vendor_id 的等价实现） */
extern char *saved_command_line;

static int s3pro_lcm_vendor_id(void)
{
	const char *p = saved_command_line;
	int v = 0;

	if (!p)
		return 0;
	p = strstr(p, "lcm_vendor_id=");
	if (!p)
		return 0;
	p += 14;	/* strlen("lcm_vendor_id=") == 14 */
	while (*p >= '0' && *p <= '9')
		v = v * 10 + (int)(*p++ - '0');
	return v;
}

static void lcm_init(void)
{
	struct device_node *nd;
	unsigned int id_gpio = 0;
	int vid;
	int level = -1;

	LCM_LOGI("%s enter\\n", __func__);
	lcm_get_gpio_infor();
	update_hardware_info(TYPE_PANEL, 5);

	/* ---- 第一级：cmdline vendor id ---- */
	vid = s3pro_lcm_vendor_id();
	pr_alert("[S3PRO_LCM_INIT] cmdline vendor_id=%d\\n", vid);

	if (vid == 1) {
		/* LOS 14.1 这里推 291 项表，但本驱动没编译那张表 */
		pr_alert("[S3PRO_LCM_INIT] vendor_id==1 -> 291-item table NOT compiled, skip\\n");
		return;
	}

	/* ---- 第二/三级：GPIO lcd_id 电平 ---- */
	nd = of_find_compatible_node(NULL, NULL, "mediatek,nt35523_fhd");
	if (!nd) {
		pr_alert("[S3PRO_LCM_INIT] no lcm node, fallback to 23-item table\\n");
		push_table(s3pro_init_table_a,
			   sizeof(s3pro_init_table_a) / sizeof(struct LCM_setting_table), 1);
		return;
	}

	id_gpio = of_get_named_gpio(nd, "gpio_lcd_id", 0);
	if (gpio_is_valid(id_gpio)) {
		if (gpio_request(id_gpio, "GPIO_LCD_ID") == 0) {
			gpio_direction_input(id_gpio);
			level = gpio_get_value(id_gpio);
		} else {
			pr_alert("[S3PRO_LCM_INIT] gpio_request(%u) failed\\n", id_gpio);
		}
	} else {
		pr_alert("[S3PRO_LCM_INIT] gpio_lcd_id invalid (%d)\\n", (int)id_gpio);
	}
	pr_alert("[S3PRO_LCM_INIT] lcd_id gpio=%d level=%d\\n", (int)id_gpio, level);

	if (level == 0) {
		pr_alert("[S3PRO_LCM_INIT] level=0 -> 23-item BOE table\\n");
		push_table(s3pro_init_table_a,
			   sizeof(s3pro_init_table_a) / sizeof(struct LCM_setting_table), 1);
	} else if (level == 1) {
		pr_alert("[S3PRO_LCM_INIT] level=1 -> 109-item table\\n");
		push_table(s3pro_init_table_b,
			   sizeof(s3pro_init_table_b) / sizeof(struct LCM_setting_table), 1);
	} else {
		pr_alert("[S3PRO_LCM_INIT] level unknown, skip table\\n");
	}
}'''

INCLUDES = ("linux/of.h", "linux/of_gpio.h", "linux/gpio.h", "linux/string.h",
            "linux/kernel.h")


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
        for root, _d, files in os.walk(base):
            if "nt35523" not in root:
                continue
            for f in files:
                if f.endswith(".c"):
                    cands.append(os.path.join(root, f))
    for p in cands:
        t = open(p, encoding="utf-8", errors="replace").read()
        if "static void lcm_init(void)" in t:
            return p
    return None


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    p = find_driver(tree)
    if not p:
        raise SystemExit("找不到面板驱动（含 lcm_init）")
    print("  驱动: %s" % os.path.relpath(p, tree))

    t = open(p, encoding="utf-8", errors="surrogateescape").read()
    if MARK in t:
        print("  已打过 S3PRO_LCM_INIT，跳过")
        return 0

    if t.count(OLD) != 1:
        print("!! lcm_init 锚点匹配 %d 次（应为 1），中止" % t.count(OLD))
        print("   （lcm_init 结构可能已被别的补丁改过）")
        return 1

    t = t.replace(OLD, NEW, 1)
    print("  已把 lcm_init 换成三级选表 + 推表")

    need = []
    for hdr in INCLUDES:
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
        "vendor_id 解析": "s3pro_lcm_vendor_id" in c,
        "读 gpio_lcd_id": 'of_get_named_gpio(nd, "gpio_lcd_id", 0)' in c,
        "gpio_get_value": "gpio_get_value(id_gpio)" in c,
        "23 项分支": "level=0 -> 23-item BOE table" in c,
        "109 项分支": "level=1 -> 109-item table" in c,
        "291 项安全跳过": "291-item table NOT compiled, skip" in c,
        "lcm_init 里推表": "push_table(s3pro_init_table_a," in c,
        "花括号配平": brace_balance(c) == 0,
    }
    print()
    ok = True
    for k, v in checks.items():
        print("  %-30s %s" % (k, "OK" if v else "FAIL"))
        if not v:
            ok = False
    if not ok:
        print("\n!! 校验失败")
        return 1
    print("\n  lcm_init 三级选表补丁完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
