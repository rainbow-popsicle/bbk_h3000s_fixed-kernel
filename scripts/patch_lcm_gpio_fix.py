#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复面板 GPIO 节点匹配失败：驱动读 gpio_lcm_*，但设备 DT 只有 gpio_lcd_*。

=== 证据（2026-11-22 实测）===
设备上 `/proc/device-tree/soc/lcm` 的全部属性：
    compatible        = "mediatek,nt35523_fhd"
    gpio_lcd_id       = <0x14 0x59 0>      /* pio 89 */
    gpio_lcd_enp      = <0x14 0x60 0>      /* pio 96  = 供电1/VSP */
    gpio_lcd_enn      = <0x14 0x62 0>      /* pio 98  = 供电2/VSN */
    gpio_lcd_rst      = <0x14 0x7f 0>      /* pio 127 = 复位 */
    reg-lcm-1v8-supply = <0x42>            /* 1V8 走 regulator，不是 GPIO */

而驱动（nt35523_wqxga_dsi_vdo_sharp）这样取：
    node = of_find_compatible_node(NULL, NULL, "mediatek,mt8173-lcm");
    GPIO_LCD_1V8_EN     = of_get_named_gpio(node, "gpio_lcm_1v8_en", 0);
    GPIO_LCD_VSP_EN     = of_get_named_gpio(node, "gpio_lcm_vsp_en", 0);
    GPIO_LCD_VSN_EN     = of_get_named_gpio(node, "gpio_lcm_vsn_en", 0);
    GPIO_LCD_RST_EN     = of_get_named_gpio(node, "gpio_lcm_rst_en", 0);
    GPIO_LCD_RST_EN_NEW = of_get_named_gpio(node, "gpio_lcm_rst_en_new", 0);

DT 里**没有** compatible="mediatek,mt8173-lcm" 的节点（mt8173.dtsi 那个被 h3000s.dts 的
同名 `lcm` 节点合并覆盖成 nt35523_fhd），也**没有**任何 gpio_lcm_* 属性 ——
gpio_lcm_* 只存在于小米平板3 的 xiaomi8176_tb_c9_n.dts。

后果：node 为 NULL → of_get_named_gpio 全部返回 -ENOENT →
赋给 unsigned int 变成 4294967294 → gpio_request 失败 → goto out →
**整个 lcm_resume 里的 GPIO 操作全部空转**。
开机屏幕能亮完全是 LK 的功劳，内核重建时没有任何 GPIO 控制。

=== 本补丁 ===
1) node 查找两级回退：mediatek,mt8173-lcm → mediatek,nt35523_fhd
2) GPIO 属性名两级回退：gpio_lcm_xxx → gpio_lcd_xxx（正确映射见上）
3) 1V8 / RST_EN_NEW 两个 DT 里不存在的脚改成「可选」：取不到就跳过 request，
   不再 goto out（1V8 由 regulator 供，面板也没有 RST_NEW）
4) 每个 GPIO 的取值和 request 结果都用 pr_alert 打出来，刷机后
   `dmesg | grep S3PRO_GPIO` 即可确认

=== 用法 ===
    python patch_lcm_gpio_fix.py <内核树根> [--dry-run]
开关：LCM_GPIO_FIX=1 时由 build.sh 调用（默认 1）。
"""
import os
import sys

MARK = "S3PRO_GPIO_FIX"

OLD_BLOCK = '''	node = of_find_compatible_node(NULL, NULL, "mediatek,mt8173-lcm");

	GPIO_LCD_1V8_EN = of_get_named_gpio(node, "gpio_lcm_1v8_en", 0);
	GPIO_LCD_VSP_EN = of_get_named_gpio(node, "gpio_lcm_vsp_en", 0);
	GPIO_LCD_VSN_EN = of_get_named_gpio(node, "gpio_lcm_vsn_en", 0);
	GPIO_LCD_RST_EN = of_get_named_gpio(node, "gpio_lcm_rst_en", 0);
	GPIO_LCD_RST_EN_NEW = of_get_named_gpio(node, "gpio_lcm_rst_en_new", 0);

	ret = gpio_request(GPIO_LCD_1V8_EN, "GPIO_LCD_1V8_EN");
	if (ret) {
		LCM_LOGD("gpio request GPIO_LCD_1V8_EN = 0x%x fail with %d\\n", GPIO_LCD_1V8_EN, ret);
		goto out;
	}

	ret = gpio_request(GPIO_LCD_VSP_EN, "GPIO_LCD_VSP_EN");
	if (ret) {
		LCM_LOGD("gpio request GPIO_LCD_VSP_EN = 0x%x fail with %d\\n", GPIO_LCD_VSP_EN, ret);
		goto out;
	}

	ret = gpio_request(GPIO_LCD_VSN_EN, "GPIO_LCD_VSN_EN");
	if (ret) {
		LCM_LOGD("gpio request GPIO_LCD_VSN_EN = 0x%x fail with %d\\n", GPIO_LCD_VSN_EN, ret);
		goto out;
	}

	ret = gpio_request(GPIO_LCD_RST_EN, "GPIO_LCD_RST_EN");
	if (ret) {
		LCM_LOGD("gpio request GPIO_LCD_RST_EN = 0x%x fail with %d\\n", GPIO_LCD_RST_EN, ret);
		goto out;
	}

	ret = gpio_request(GPIO_LCD_RST_EN_NEW, "GPIO_LCD_RST_EN_NEW");
	if (ret) {
		LCM_LOGD("gpio request GPIO_LCD_RST_EN_NEW = 0x%x fail with %d\\n", GPIO_LCD_RST_EN_NEW, ret);
		goto out;
	}

out:
	return;
}'''

NEW_BLOCK = '''	/* S3PRO_GPIO_FIX: node 两级回退。
	 * mt8173.dtsi 的 lcm 节点本该是 mediatek,mt8173-lcm，但 h3000s.dts 里
	 * 同名 lcm 节点把它合并/覆盖成了 mediatek,nt35523_fhd，所以第一级必然失败。 */
	node = of_find_compatible_node(NULL, NULL, "mediatek,mt8173-lcm");
	if (!node) {
		node = of_find_compatible_node(NULL, NULL, "mediatek,nt35523_fhd");
		pr_alert("[S3PRO_GPIO] node: mt8173-lcm miss, fallback to nt35523_fhd -> %s\\n",
			 node ? node->full_name : "NULL");
	} else {
		pr_alert("[S3PRO_GPIO] node: %s\\n", node->full_name);
	}
	if (!node) {
		pr_alert("[S3PRO_GPIO] FATAL: no lcm node found, GPIO will not work\\n");
		return;
	}

	/* S3PRO_GPIO_FIX: 属性名两级回退。
	 * 本设备 DT 里只有 gpio_lcd_enp/enn/rst（pio 96/98/127），没有 gpio_lcm_*。
	 * 1V8 由 reg-lcm-1v8-supply 供电、面板也没有 RST_NEW，这两个取不到就放过。 */
	GPIO_LCD_VSP_EN = of_get_named_gpio(node, "gpio_lcm_vsp_en", 0);
	if (!gpio_is_valid(GPIO_LCD_VSP_EN))
		GPIO_LCD_VSP_EN = of_get_named_gpio(node, "gpio_lcd_enp", 0);

	GPIO_LCD_VSN_EN = of_get_named_gpio(node, "gpio_lcm_vsn_en", 0);
	if (!gpio_is_valid(GPIO_LCD_VSN_EN))
		GPIO_LCD_VSN_EN = of_get_named_gpio(node, "gpio_lcd_enn", 0);

	GPIO_LCD_RST_EN = of_get_named_gpio(node, "gpio_lcm_rst_en", 0);
	if (!gpio_is_valid(GPIO_LCD_RST_EN))
		GPIO_LCD_RST_EN = of_get_named_gpio(node, "gpio_lcd_rst", 0);

	/* 这两个可选：本设备 DT 里没有对应属性 */
	GPIO_LCD_1V8_EN = of_get_named_gpio(node, "gpio_lcm_1v8_en", 0);
	GPIO_LCD_RST_EN_NEW = of_get_named_gpio(node, "gpio_lcm_rst_en_new", 0);

	pr_alert("[S3PRO_GPIO] read: VSP=%d VSN=%d RST=%d 1V8=%d RST_NEW=%d\\n",
		 (int)GPIO_LCD_VSP_EN, (int)GPIO_LCD_VSN_EN, (int)GPIO_LCD_RST_EN,
		 (int)GPIO_LCD_1V8_EN, (int)GPIO_LCD_RST_EN_NEW);

	if (!gpio_is_valid(GPIO_LCD_VSP_EN) || !gpio_is_valid(GPIO_LCD_VSN_EN)
	    || !gpio_is_valid(GPIO_LCD_RST_EN)) {
		pr_alert("[S3PRO_GPIO] FATAL: VSP/VSN/RST missing, cannot drive panel\\n");
		return;
	}

	ret = gpio_request(GPIO_LCD_VSP_EN, "GPIO_LCD_VSP_EN");
	pr_alert("[S3PRO_GPIO] req VSP(%d)=%d\\n", (int)GPIO_LCD_VSP_EN, ret);
	if (ret)
		goto out;

	ret = gpio_request(GPIO_LCD_VSN_EN, "GPIO_LCD_VSN_EN");
	pr_alert("[S3PRO_GPIO] req VSN(%d)=%d\\n", (int)GPIO_LCD_VSN_EN, ret);
	if (ret)
		goto out;

	ret = gpio_request(GPIO_LCD_RST_EN, "GPIO_LCD_RST_EN");
	pr_alert("[S3PRO_GPIO] req RST(%d)=%d\\n", (int)GPIO_LCD_RST_EN, ret);
	if (ret)
		goto out;

	/* 1V8：DT 没有就跳过（regulator 供） */
	if (gpio_is_valid(GPIO_LCD_1V8_EN)) {
		ret = gpio_request(GPIO_LCD_1V8_EN, "GPIO_LCD_1V8_EN");
		pr_alert("[S3PRO_GPIO] req 1V8(%d)=%d\\n", (int)GPIO_LCD_1V8_EN, ret);
	} else {
		pr_alert("[S3PRO_GPIO] 1V8 not present (regulator powered), skip\\n");
	}

	/* RST_EN_NEW：DT 没有就跳过 */
	if (gpio_is_valid(GPIO_LCD_RST_EN_NEW)) {
		ret = gpio_request(GPIO_LCD_RST_EN_NEW, "GPIO_LCD_RST_EN_NEW");
		pr_alert("[S3PRO_GPIO] req RST_NEW(%d)=%d\\n", (int)GPIO_LCD_RST_EN_NEW, ret);
	} else {
		pr_alert("[S3PRO_GPIO] RST_NEW not present, skip\\n");
	}

	pr_alert("[S3PRO_GPIO] all requested OK\\n");

out:
	return;
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
        for root, _d, files in os.walk(base):
            if "nt35523" not in root:
                continue
            for f in files:
                if f.endswith(".c"):
                    cands.append(os.path.join(root, f))
    for p in cands:
        t = open(p, encoding="utf-8", errors="replace").read()
        if "gpio_lcm_vsp_en" in t or "gpio_lcd_enp" in t:
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
        raise SystemExit("找不到面板驱动")
    print("  驱动: %s" % os.path.relpath(p, tree))

    t = open(p, encoding="utf-8", errors="surrogateescape").read()
    if MARK in t:
        print("  已打过 GPIO 修复补丁，跳过")
        return 0

    if t.count(OLD_BLOCK) != 1:
        print("!! GPIO 段锚点匹配 %d 次（应为 1），中止" % t.count(OLD_BLOCK))
        print("   （驱动版本不对，可能已被其它补丁改过）")
        return 1

    t = t.replace(OLD_BLOCK, NEW_BLOCK, 1)
    print("  已替换 GPIO 获取/request 段（node + 属性名两级回退，1V8/RST_NEW 可选）")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(p, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "标记存在": MARK in c,
        "node 两级回退": "of_find_compatible_node(NULL, NULL, \"mediatek,nt35523_fhd\")" in c,
        "VSP 回退 gpio_lcd_enp": "GPIO_LCD_VSP_EN = of_get_named_gpio(node, \"gpio_lcd_enp\", 0)" in c,
        "VSN 回退 gpio_lcd_enn": "GPIO_LCD_VSN_EN = of_get_named_gpio(node, \"gpio_lcd_enn\", 0)" in c,
        "RST 回退 gpio_lcd_rst": "GPIO_LCD_RST_EN = of_get_named_gpio(node, \"gpio_lcd_rst\", 0)" in c,
        "1V8 可选": "1V8 not present (regulator powered), skip" in c,
        "RST_NEW 可选": "RST_NEW not present, skip" in c,
        "诊断打印": "[S3PRO_GPIO] read:" in c,
        "request 结果打印": "[S3PRO_GPIO] req VSP" in c,
        "花括号配平": brace_balance(c) == 0,
    }
    print()
    ok = True
    for k, v in checks.items():
        print("  %-24s %s" % (k, "OK" if v else "FAIL"))
        if not v:
            ok = False
    if not ok:
        print("\n!! 校验失败")
        return 1
    print("\n  面板 GPIO 修复补丁完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
