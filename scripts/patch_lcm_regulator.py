#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对齐 LOS 14.1：用 MT6397 的 VGP4 LDO（ldo_vgp4 / regulator-name "vgp4"）给面板供 1.8V。

=== 为什么 ===
反编译 LOS 14.1 的 DTB 得到（与我们自己的 boot.dtb 逐字段一致）：

    /soc/lcm {
        compatible          = "mediatek,nt35523_fhd"
        gpio_lcd_enp        = <&pio 0x60 0>        pio96  VSP+ 升压使能
        gpio_lcd_enn        = <&pio 0x62 0>        pio98  VSN- 升压使能
        gpio_lcd_rst        = <&pio 0x7f 0>        pio127 复位
        reg-lcm-1v8-supply  = <0x42>               → phandle 0x42
    }

phandle 0x42 →
    /soc/pwrap@1000d000/mt6397/mt6397regulator/ldo_vgp4
        regulator-name            = "vgp4"
        regulator-min-microvolt   = <0x124f80>     1.20V
        regulator-max-microvolt   = <0x325aa0>     3.30V

设备实测（132，熄屏唤醒后）：
    /sys/class/regulator/regulator.20/name  = vgp4
    /sys/class/regulator/regulator.20/state = disabled      ← 1.8V 关着
    而 GPIO_LCD_1V8_EN/VSN_EN/RST_EN 全是 hi
    S3PRO_REG start dsi0 D  trig=00000000                    ← 面板不回应

也就是说：GPIO 全高，但面板的 1.8V 逻辑供电从未被内核打开，TPS65132 升不出
VPOS/VNEG，面板没电，不回应任何 DCS。开机亮是因为 LK 自己开了 vgp4。

LOS 14.1 三个函数（反编译 @0x458420 / 0x458508 / 0x45817c）：
    lcm_resume_power : lcm_vgp_supply_enable() → regulator_enable(vgp); mdelay(20)
    lcm_resume       : enp↑2 → enn↑15 → RST(高5/低2/高15) → lcm_init() 二次推表
    lcm_suspend_power: enn↓2 → enp↓5 → regulator_disable(vgp) → rst↓5

本补丁把这三处对齐到 LOS：

1) primary_display.c
   - 定义非 static 的 `struct regulator *s3pro_lcm_vgp;`（给面板驱动 extern 用）
   - 在 primary_display_init 里 `pregulator = dev;` 之后
     `regulator_get(&dev->dev, "reg-lcm-1v8")` + `regulator_set_voltage(1.8V)`
   - 注意：primary_display.c 本来就 #include <linux/regulator/consumer.h>

2) 面板驱动
   - #include <linux/regulator/consumer.h> + extern struct regulator *s3pro_lcm_vgp;
   - lcm_resume_power : regulator_enable(vgp) + 20ms（LOS 完全不碰 GPIO）
   - lcm_suspend_power: 改成 LOS 的逆序 —— enn↓2 → enp↓5 → regulator_disable → rst↓5
   - lcm_resume       : RST 第三段 32ms → 15ms（LOS 值）

3) 保留原有开关
   - s3pro_lk_no_power == 0 时仍可回到"拉 ENP"的旧行为，便于对照
   - s3pro_keep_power == 1 时仍然整段跳过 suspend_power

为什么不动 lcm_resume 的推表部分：
    我们的 lcm_resume 已经在 enp/enn/RST 之后 push_table(s3pro_init_table_a)，
    与 LOS 的 "RST 之后 lcm_init() 推表" 等价，不需要再叠加一次。

顺序：必须在 patch_lk_replay.py 之后运行（它才生成最终的 lcm_resume/power 函数）。

用法：
    python patch_lcm_regulator.py <内核树根> [--dry-run]
开关：LCM_VGP=1 时由 build.sh 调用（默认开）。
"""
import os
import re
import sys

MARK = "S3PRO_VGP"

# ---------------------------------------------------------------- primary_display.c
PD_REL = os.path.join("drivers", "misc", "mediatek", "video", "mt8173",
                      "videox", "primary_display.c")

# ★ 锚点必须"只追加、不替换"原有语句。
# 曾经的错误写法：PD_ANCHOR = "\tpregulator = dev;\n" 然后把整段替换成自己的代码，
# 结果把紧跟其后的 `dprec_init();`（Display Recorder 初始化）以及
# patch_disp_debug 插入的 `s3pro_dbg_init_once();` 一起删掉了 ——
# 编译直接报 `s3pro_dbg_init_once defined but not used`（-Werror=unused-function）。
#
# 现在改成：先找到这两个语句的完整尾部，再在它们之后插入，绝不覆盖。
# 两种锚点：
#   A（DISP_DEBUG=1，默认）：pregulator = dev; / dprec_init(); / s3pro_dbg_init_once();
#   B（DISP_DEBUG=0）      ：pregulator = dev; / dprec_init();
PD_ANCHOR_A = "\tpregulator = dev;\n\tdprec_init();\n\ts3pro_dbg_init_once();\n"
PD_ANCHOR_B = "\tpregulator = dev;\n\tdprec_init();\n"

PD_DEF = (
    "\n"
    "/* S3PRO_VGP: 面板 1.8V 由 MT6397 的 VGP4 LDO 提供（DTB reg-lcm-1v8-supply\n"
    " * 指向 /soc/pwrap@1000d000/mt6397/mt6397regulator/ldo_vgp4）。\n"
    " * 这里定义成非 static，供面板驱动 extern 后在本驱动里 enable/disable。\n"
    " * 反编译 LOS 14.1 确认：它的 lcm_vgp_supply_enable/disable 就是\n"
    " * regulator_enable/disable(&dev->dev, \"reg-lcm-1v8\")。 */\n"
    "struct regulator *s3pro_lcm_vgp;\n"
)

# 只追加，不含 pregulator = dev; / dprec_init();
PD_INIT_ADD = (
    "\n"
    "\t/* S3PRO_VGP: 取面板 1.8V regulator 并设到 1.8V（对齐 LOS 14.1 的\n"
    "\t * lcm_vgp_regulator_init）。这里只 get + set_voltage，不 enable ——\n"
    "\t * enable 交给面板驱动的 lcm_resume_power，和 LOS 的分工一致。 */\n"
    "\tif (s3pro_lcm_vgp == NULL) {\n"
    "\t\ts3pro_lcm_vgp = regulator_get(&dev->dev, \"reg-lcm-1v8\");\n"
    "\t\tif (IS_ERR(s3pro_lcm_vgp)) {\n"
    "\t\t\tpr_err(\"[S3PRO_VGP] regulator_get(reg-lcm-1v8) failed: %ld\\n\",\n"
    "\t\t\t       PTR_ERR(s3pro_lcm_vgp));\n"
    "\t\t\ts3pro_lcm_vgp = NULL;\n"
    "\t\t} else {\n"
    "\t\t\tint s3pro_vgp_ret;\n"
    "\n"
    "\t\t\ts3pro_vgp_ret = regulator_set_voltage(s3pro_lcm_vgp,\n"
    "\t\t\t\t\t\t\t      1800000, 1800000);\n"
    "\t\t\tpr_alert(\"[S3PRO_VGP] got reg-lcm-1v8, set 1.8V ret=%d\\n\",\n"
    "\t\t\t\t s3pro_vgp_ret);\n"
    "\t\t}\n"
    "\t}\n"
)

# ---------------------------------------------------------------- 驱动
DRV_REL = os.path.join("drivers", "misc", "mediatek", "lcm",
                       "nt35523_wqxga_dsi_vdo_sharp", "nt35523_wqxga_dsi_vdo_sharp.c")

# 驱动里的 include 锚点（patch_lk_replay 已经加了这些 include）
DRV_INC_ANCHOR = "#include <linux/debugfs.h>\n"

DRV_INC_NEW = (
    "#include <linux/debugfs.h>\n"
    "#include <linux/regulator/consumer.h>\n"
)

DRV_EXTERN = (
    "\n"
    "/* S3PRO_VGP: 面板 1.8V regulator（定义在 primary_display.c，非 static）。\n"
    " * LOS 14.1 用 regulator_enable/disable(\"reg-lcm-1v8\") 控制它；\n"
    " * 我们的驱动以前只拉 GPIO_LCD_1V8_EN，而那个引脚经 build.sh 的 sed 实际是\n"
    " * ENP(pio96)，从来不是 1.8V 开关 —— 这是面板没电的直接原因。 */\n"
    "extern struct regulator *s3pro_lcm_vgp;\n"
)

# resume_power 整段（patch_lk_replay 生成的版本）
RP_OLD = """	pr_alert("[S3PRO_LKREPLAY] resume_power no_power=%u\\n", s3pro_lk_no_power);
	if (s3pro_lk_no_power) {
		MDELAY(20);
	} else {
		lcm_set_gpio_output(GPIO_LCD_1V8_EN, GPIO_OUT_ONE);
		MDELAY(20);
	}
"""

RP_NEW = """	pr_alert("[S3PRO_LKREPLAY] resume_power no_power=%u\\n", s3pro_lk_no_power);
	if (s3pro_lk_no_power) {
		/* S3PRO_VGP: 对齐 LOS 14.1 —— resume_power 只做
		 *   regulator_set_voltage(1.8V) + regulator_enable(vgp)
		 *   mdelay(20)
		 * 完全不碰 GPIO。1.8V 断了，enp/enn 拉高也升不出 VPOS/VNEG。 */
		if (!IS_ERR_OR_NULL(s3pro_lcm_vgp)) {
			int s3pro_vgp_ret;

			s3pro_vgp_ret = regulator_set_voltage(s3pro_lcm_vgp,
							      1800000, 1800000);
			if (s3pro_vgp_ret)
				pr_alert("[S3PRO_VGP] set_voltage ret=%d\\n",
					 s3pro_vgp_ret);
			s3pro_vgp_ret = regulator_enable(s3pro_lcm_vgp);
			pr_alert("[S3PRO_VGP] resume_power: enable vgp ret=%d is_enabled=%d\\n",
				 s3pro_vgp_ret,
				 regulator_is_enabled(s3pro_lcm_vgp));
		} else {
			pr_alert("[S3PRO_VGP] resume_power: vgp is NULL\\n");
		}
		MDELAY(20);
	} else {
		lcm_set_gpio_output(GPIO_LCD_1V8_EN, GPIO_OUT_ONE);
		MDELAY(20);
	}
"""

# suspend_power 整段（patch_lcm_stock 生成 + keep_power）
SP_OLD = """	/* 原厂 suspend_power：按上电的逆序掉电 */
	lcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ZERO);
	MDELAY(5);
	lcm_set_gpio_output(GPIO_LCD_VSN_EN, GPIO_OUT_ZERO);
	MDELAY(2);
	lcm_set_gpio_output(GPIO_LCD_VSP_EN, GPIO_OUT_ZERO);
	MDELAY(2);
"""

SP_NEW = """	/* S3PRO_VGP: 对齐 LOS 14.1 的 lcm_suspend_power 顺序
	 *   enn↓2ms → enp↓5ms → regulator_disable(1.8V) → rst↓5ms
	 * 注意 LOS 是先关两路升压、再关 1.8V、最后拉低复位。
	 * gpio_lcd_enn = pio98 = VSN- 使能；gpio_lcd_enp = pio96 = VSP+ 使能。 */
	lcm_set_gpio_output(GPIO_LCD_VSN_EN, GPIO_OUT_ZERO);
	MDELAY(2);
	lcm_set_gpio_output(GPIO_LCD_VSP_EN, GPIO_OUT_ZERO);
	MDELAY(5);

	if (!IS_ERR_OR_NULL(s3pro_lcm_vgp)) {
		int s3pro_vgp_ret;

		s3pro_vgp_ret = regulator_disable(s3pro_lcm_vgp);
		pr_alert("[S3PRO_VGP] suspend_power: disable vgp ret=%d is_enabled=%d\\n",
			 s3pro_vgp_ret,
			 regulator_is_enabled(s3pro_lcm_vgp));
	} else {
		pr_alert("[S3PRO_VGP] suspend_power: vgp is NULL\\n");
	}

	lcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ZERO);
	MDELAY(5);
"""

# RST 第三段：32 -> 15（LOS 值）
D3_OLD = "\tdefault: d1 = 12; d2 = 12; d3 = 32; vsp_first = 1; break;\t/* 原厂顺序（2026-11-26 改为默认） */"
D3_NEW = "\tdefault: d1 = 12; d2 = 12; d3 = 15; vsp_first = 1; break;\t/* S3PRO_VGP: RST 第三段对齐 LOS(15ms) */"


def read(p):
    return open(p, encoding="utf-8", errors="surrogateescape").read()


def write(p, s):
    open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(s)


def patch_pd(t):
    if "s3pro_lcm_vgp" in t:
        return t, []
    notes = []

    # 1) 在 include 区之后插入全局定义
    inc_anchor = '#include "disp_drv_log.h"\n'
    if inc_anchor not in t:
        raise SystemExit("!! primary_display.c 找不到 %r" % inc_anchor)
    t = t.replace(inc_anchor, inc_anchor + PD_DEF, 1)
    notes.append("primary_display.c: 定义 s3pro_lcm_vgp")

    # 2) 在 primary_display_init 的初始化语句之后【追加】，不覆盖任何原有行
    if PD_ANCHOR_A in t:
        if t.count(PD_ANCHOR_A) != 1:
            raise SystemExit("!! 锚点 A 出现 %d 次（应为 1）" % t.count(PD_ANCHOR_A))
        t = t.replace(PD_ANCHOR_A, PD_ANCHOR_A + PD_INIT_ADD, 1)
        notes.append("primary_display.c: 在 dprec_init()+s3pro_dbg_init_once() 之后追加 regulator_get")
    elif PD_ANCHOR_B in t:
        if t.count(PD_ANCHOR_B) != 1:
            raise SystemExit("!! 锚点 B 出现 %d 次（应为 1）" % t.count(PD_ANCHOR_B))
        t = t.replace(PD_ANCHOR_B, PD_ANCHOR_B + PD_INIT_ADD, 1)
        notes.append("primary_display.c: 在 dprec_init() 之后追加 regulator_get（DISP_DEBUG=0）")
    else:
        raise SystemExit("!! primary_display.c 找不到 pregulator = dev; + dprec_init(); 组合")

    return t, notes


def patch_drv(t):
    if MARK in t and "extern struct regulator *s3pro_lcm_vgp" in t:
        return t, []
    notes = []

    # 1) include
    if "#include <linux/regulator/consumer.h>" not in t:
        if DRV_INC_ANCHOR not in t:
            raise SystemExit("!! 驱动找不到 %r" % DRV_INC_ANCHOR)
        t = t.replace(DRV_INC_ANCHOR, DRV_INC_NEW, 1)
        notes.append("驱动: 加 #include <linux/regulator/consumer.h>")

    # 2) extern
    if "extern struct regulator *s3pro_lcm_vgp" not in t:
        # 锚在 extern int s3pro_keep_power; 之后
        ea = "extern int s3pro_keep_power;\n"
        if ea in t:
            t = t.replace(ea, ea + DRV_EXTERN, 1)
            notes.append("驱动: 加 extern s3pro_lcm_vgp")
        else:
            raise SystemExit("!! 驱动找不到 %r" % ea)

    # 3) resume_power
    if RP_OLD in t:
        t = t.replace(RP_OLD, RP_NEW, 1)
        notes.append("驱动: lcm_resume_power 改用 regulator_enable")
    else:
        raise SystemExit("!! 驱动里 resume_power 段未匹配")

    # 4) suspend_power
    if SP_OLD in t:
        t = t.replace(SP_OLD, SP_NEW, 1)
        notes.append("驱动: lcm_suspend_power 对齐 LOS 顺序 + regulator_disable")
    else:
        raise SystemExit("!! 驱动里 suspend_power 段未匹配")

    # 5) RST 第三段对齐 LOS 的 15ms（2026-11-27）
    #    default 已由 patch_lk_replay 改成 5/2/15（完全对齐 LOS），本步只做兼容：
    #    若仍是旧的 12/12/32（换树未跑 lk_replay），在这里补成 12/12/15；
    #    若已是 5/2/15（含 d3=15），无需再改。
    if D3_OLD in t:
        t = t.replace(D3_OLD, D3_NEW, 1)
        notes.append("驱动: RST 第三段 32ms -> 15ms（旧 default 兼容）")
    elif "default: d1 = 5;  d2 = 2;  d3 = 15;" in t:
        notes.append("驱动: default 已是 LOS 5/2/15（lk_replay 已对齐），跳过")
    else:
        raise SystemExit("!! 驱动里 default variant 未匹配（既非 12/12/32 也非 5/2/15）")

    # 标记
    t = t.replace("/***** S3PRO_VGP *****/", "")
    notes.append("驱动: 打上 %s 标记" % MARK)
    return t, notes


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    pd_path = os.path.join(tree, PD_REL)
    drv_path = os.path.join(tree, DRV_REL)

    if not os.path.exists(drv_path):
        raise SystemExit("!! 找不到驱动 %s" % drv_path)

    # primary_display.c 可能不存在：审计脚本（tests/test_patch_implementation.py）
    # 刻意只准备面板驱动，因为 CI 的 selftest job 里没有内核源码树。
    # 这种情况下只改驱动（驱动侧才是本次修复的主体），primary_display.c 那半
    # 交给真实构建时完成（那时内核源码齐全）。
    pd_exists = os.path.exists(pd_path)

    drv = read(drv_path)
    pd = read(pd_path) if pd_exists else ""

    # 幂等
    if MARK in drv and (not pd_exists or MARK in pd):
        print("  已经打过补丁（%s），跳过" % MARK)
        return 0

    if pd_exists:
        pd2, n1 = patch_pd(pd)
    else:
        pd2, n1 = pd, ["primary_display.c 不存在（无内核源码树），跳过 regulator_get 部分"]

    drv2, n2 = patch_drv(drv)
    notes = n1 + n2

    # ---------------- 自检 ----------------
    checks = []

    def chk(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    # 驱动侧
    chk("驱动: 有 regulator 头文件", "#include <linux/regulator/consumer.h>" in drv2)
    chk("驱动: extern s3pro_lcm_vgp", "extern struct regulator *s3pro_lcm_vgp;" in drv2)
    chk("驱动: resume_power 调 regulator_enable", "regulator_enable(s3pro_lcm_vgp)" in drv2)
    chk("驱动: suspend_power 调 regulator_disable", "regulator_disable(s3pro_lcm_vgp)" in drv2)
    chk("驱动: RST 第三段是 15", "d3 = 15;" in drv2)
    chk("驱动: 不再有 d3 = 32 的 default",
        "d3 = 32; vsp_first = 1; break;\t/* 原厂顺序" not in drv2)

    # 顺序：suspend_power 里 VSN 下降必须在 RST 下降之前
    i_vsn = drv2.find("lcm_set_gpio_output(GPIO_LCD_VSN_EN, GPIO_OUT_ZERO)")
    i_rst = drv2.find("lcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ZERO)")
    chk("驱动: suspend_power 里 VSN↓ 在 RST↓ 之前",
        i_vsn > 0 and i_rst > 0 and i_vsn < i_rst)

    # regulator_disable 必须在 VSP↓ 之后、RST↓ 之前
    i_vsp = drv2.find("lcm_set_gpio_output(GPIO_LCD_VSP_EN, GPIO_OUT_ZERO)")
    i_dis = drv2.find("regulator_disable(s3pro_lcm_vgp)")
    chk("驱动: regulator_disable 在 VSP↓ 之后、RST↓ 之前",
        0 < i_vsp < i_dis < i_rst)

    # primary_display.c 侧（只有源码树里有这个文件时才检查）
    if pd_exists:
        chk("PD: 定义 s3pro_lcm_vgp", "struct regulator *s3pro_lcm_vgp;" in pd2)
        chk("PD: regulator_get(reg-lcm-1v8)",
            'regulator_get(&dev->dev, "reg-lcm-1v8")' in pd2)
        chk("PD: set_voltage 1.8V", "1800000, 1800000" in pd2)
        chk("PD: 有 pr_alert 打印", 'pr_alert("[S3PRO_VGP]' in pd2)

    # ---- 绝不能破坏 primary_display_init 里原有的初始化语句 ----
    if pd_exists:
        chk("PD: dprec_init() 仍存在且唯一",
            pd2.count("\tdprec_init();") == 1,
            "出现 %d 次" % pd2.count("\tdprec_init();"))
        had_dbg = "s3pro_dbg_init_once();" in pd
        has_dbg = "s3pro_dbg_init_once();" in pd2
        chk("PD: s3pro_dbg_init_once() 调用未丢失",
            (not had_dbg) or has_dbg,
            "原 %s -> 现 %s" % (had_dbg, has_dbg))
        # 若函数有定义，就必须有调用（防 -Werror=unused-function）
        if "static void s3pro_dbg_init_once(void)" in pd2:
            chk("PD: s3pro_dbg_init_once 有调用（防 unused-function）",
                pd2.count("s3pro_dbg_init_once();") >= 1)

    # 结构
    chk("驱动 花括号平衡", drv2.count("{") == drv2.count("}"))
    if pd_exists:
        chk("PD 花括号平衡", pd2.count("{") == pd2.count("}"))

    # print 字符串不跨行
    bad = False
    for src_txt in ([drv2] + ([pd2] if pd_exists else [])):
        for m in re.finditer(r'pr_(?:alert|err|info|warn)\("([^"]*)"', src_txt):
            if "\n" in m.group(1):
                bad = True
    chk("print 字符串不跨行", not bad)

    print()
    for name, ok, detail in checks:
        print("    %-46s %s%s" % (name, "OK" if ok else "失败!",
                                  ("   " + detail) if detail else ""))
    print()
    if not all(ok for _, ok, _ in checks):
        print("!! 自检未通过，不写文件")
        return 1

    if dry:
        print("--- dry-run，未写文件 ---")
        for n in notes:
            print("    会做: " + n)
        print("    作用范围: %s" % ("驱动 + primary_display.c" if pd_exists else "仅驱动（无内核源码树）"))
        return 0

    if pd_exists:
        write(pd_path, pd2)
    write(drv_path, drv2)
    print("  %s 完成：%s" % (MARK, "驱动 + primary_display.c" if pd_exists else "仅驱动"))
    for n in notes:
        print("    - " + n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
