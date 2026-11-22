#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复候选：在 stock lcm_resume/lcm_suspend_power 里补回 GPIO_LCD_RST_EN_NEW。

=== 依据（2026-11-22，代码证据） ===
- 开机 kernel 不调 lcm_resume（屏幕亮是纯 LK 初始化）；熄屏唤醒才走 kernel 的
  suspend→resume 重建。问题在 kernel 重建路径。
- 本地 legacy 驱动 lcm_resume (:401/406/411) 把 GPIO_LCD_RST_EN_NEW(pio6397 14)
  与主 RST(GPIO_LCD_RST_EN, pio127) **同步拉三次**；lcm_suspend (:316) 拉低。
- LK（为 cappu 编译）的日志 `ok100 rst/enp/enn` 表明它控制复位。
- patch_lcm_stock.py 照搬 bbk 原厂 3.18.35（不同设备，无 cappu 的 RST_NEW 概念），
  重写 lcm_resume/suspend_power 时**把 RST_EN_NEW 丢了**。
- DTS: gpio_lcm_rst_en_new = <&pio6397 14 0>，是 cappu 特有；gpio debug 里它从未被 request。
- 早期 GPIO 顺序实验(19:55)用 legacy 形态（RST_NEW 本就拉），改顺序无效 != RST_NEW 无效。
- 时钟维度已排除（健康/黑屏 clk_summary 完全一致）。

=== 修复 ===
在 stock 形态的 lcm_resume 主 RST 三次脉冲处，同步加 RST_EN_NEW；
在 lcm_suspend_power 的 RST 拉低处，同步拉低 RST_EN_NEW。
加 pr_alert 打印 GPIO 号 + 操作后电平，便于实测确认。

必须在 patch_lcm_stock.py 之后运行。与 LCM_POWER_SEQ 兼容（power_seq 改的是
VSP/VSN 与 RST 的相对顺序，本补丁只补 RST_NEW 同步脉冲）。

用法：
    python patch_rst_new.py <内核树根> [--dry-run]
开关：RST_NEW_FIX=1 时由 build.sh 调用（默认 1）。
"""
import os
import sys

MARK = "S3PRO_RST_NEW_FIX"

# stock lcm_resume 主 RST 三次脉冲（patch_lcm_stock.py NEW_REGION :78-83）
RESUME_OLD = '''	/* 原厂 resume：先复位，再使能 VSP/VSN（原实现顺序相反） */
	lcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ONE);
	MDELAY(5);
	lcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ZERO);
	MDELAY(2);
	lcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ONE);
	MDELAY(25);'''

RESUME_NEW = '''	/* S3PRO_RST_NEW_FIX: 补回 cappu 特有的 PMIC 复位脚 RST_EN_NEW(pio6397 14)，
	 * 与主 RST(pio127) 同步。patch_lcm_stock 照搬 bbk 原厂时漏了它。 */
	pr_alert("[S3PRO_RSTNEW] resume rst_new=%d main_rst=%d\\n",
		 GPIO_LCD_RST_EN_NEW, GPIO_LCD_RST_EN);

	/* 原厂 resume：先复位，再使能 VSP/VSN（原实现顺序相反） */
	lcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ONE);
	lcm_set_gpio_output(GPIO_LCD_RST_EN_NEW, GPIO_OUT_ONE);
	MDELAY(5);
	lcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ZERO);
	lcm_set_gpio_output(GPIO_LCD_RST_EN_NEW, GPIO_OUT_ZERO);
	MDELAY(2);
	lcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ONE);
	lcm_set_gpio_output(GPIO_LCD_RST_EN_NEW, GPIO_OUT_ONE);
	MDELAY(25);
	pr_alert("[S3PRO_RSTNEW] after reset pulse: rst=%d rst_new=%d\\n",
		 gpio_get_value(GPIO_LCD_RST_EN), gpio_get_value(GPIO_LCD_RST_EN_NEW));'''

# stock lcm_suspend_power RST 拉低（patch_lcm_stock.py NEW_REGION :63-65）
SUSPEND_OLD = '''	/* 原厂 suspend_power：按上电的逆序掉电 */
	lcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ZERO);
	MDELAY(5);'''

SUSPEND_NEW = '''	/* 原厂 suspend_power：按上电的逆序掉电 */
	/* S3PRO_RST_NEW_FIX: 同步拉低 cappu 的 PMIC 复位脚 */
	lcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ZERO);
	lcm_set_gpio_output(GPIO_LCD_RST_EN_NEW, GPIO_OUT_ZERO);
	MDELAY(5);'''


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
        if "lcm_resume" in t and "GPIO_LCD_RST_EN_NEW" in t:
            return p
    return None


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    p = find_driver(tree)
    if not p:
        raise SystemExit("找不到含 lcm_resume/GPIO_LCD_RST_EN_NEW 的面板驱动")
    print("  驱动: %s" % os.path.relpath(p, tree))

    t = open(p, encoding="utf-8", errors="surrogateescape").read()
    if MARK in t:
        print("  已打过补丁，跳过")
        return 0

    # 检测 legacy 形态（已有 RST_NEW 脉冲）——说明 stock 未生效，不需打
    if t.count("lcm_set_gpio_output(GPIO_LCD_RST_EN_NEW, GPIO_OUT_ONE);") >= 2:
        print("!! 检测到 legacy 形态已含 RST_NEW 脉冲，本补丁是给 stock 形态用的，跳过")
        return 0

    errs = []
    cnt_r = t.count(RESUME_OLD)
    cnt_s = t.count(SUSPEND_OLD)
    if cnt_r != 1:
        errs.append("resume 锚点匹配 %d 次（应 1）" % cnt_r)
    if cnt_s != 1:
        errs.append("suspend_power 锚点匹配 %d 次（应 1）" % cnt_s)
    if errs:
        for e in errs:
            print("!! " + e)
        return 1

    t = t.replace(RESUME_OLD, RESUME_NEW, 1)
    t = t.replace(SUSPEND_OLD, SUSPEND_NEW, 1)
    print("  已在 lcm_resume 补 RST_NEW 三次脉冲、suspend_power 补拉低")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(p, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "标记存在": MARK in c,
        "resume 补 RST_NEW 拉高": c.count("lcm_set_gpio_output(GPIO_LCD_RST_EN_NEW, GPIO_OUT_ONE);") >= 2,
        "resume 补 RST_NEW 拉低": "lcm_set_gpio_output(GPIO_LCD_RST_EN_NEW, GPIO_OUT_ZERO);" in c,
        "suspend 补 RST_NEW 拉低": c.count("lcm_set_gpio_output(GPIO_LCD_RST_EN_NEW, GPIO_OUT_ZERO);") >= 2,
        "pr_alert 诊断": c.count('pr_alert("[S3PRO_RSTNEW]') >= 2,
        "花括号配平": c.count("{") == c.count("}"),
    }
    print()
    bad = 0
    for k, v in checks.items():
        print("    %-22s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1
    if bad:
        return 1
    print("\nRST_NEW 修复补丁完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
