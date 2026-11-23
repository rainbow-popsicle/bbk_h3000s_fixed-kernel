#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""刷机前离线验证：在 mock 树上按 build.sh 默认开关跑完整面板补丁链，
检查最终生成的驱动代码是原厂配置（BOE表 + 原厂时序 40/40/4/200/200）。

不需要编译、不需要刷机、不需要内核源码树——只用 s3pro-kernel 仓库自己的文件。

用法：
    python3 tests/test_panel_patch_chain.py
退出码 0 = 全部通过；1 = 有失败。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DRIVER = os.path.join(ROOT, "inputs", "nt35523_wqxga_dsi_vdo_sharp.c")
STOCK_TABLES = os.path.join(ROOT, "kernel-patches", "lcm-table", "stock_tables.c")
SCRIPTS = os.path.join(ROOT, "scripts")
PY = sys.executable

# build.sh 默认开关下的面板补丁调用顺序（行号见 build.sh）
# 注意：
#   1) patch_rst_new.py 与 patch_lk_replay.py 互斥（前者锚点是 stock 版 lcm_resume 原文，
#      后者整体替换该函数，且已自带 RST_EN_NEW 开关）。build.sh 里 RST_NEW_FIX 默认已改 0。
#   2) patch_lcm_gpio_fix.py 默认关 —— build.sh 里在它之前已用 sed 把
#      "mediatek,mt8173-lcm"→"mediatek,nt35523_fhd"、gpio_lcm_*→gpio_lcd_*、goto out;→; 改完。
#      所以这里先用 SED_LINES 模拟那段 sed，再跑补丁链。
#   3) patch_dimming_early.py 默认关（workflow 的 dimming_early=false）——
#      它会多推一次 dimming 表，验证 PLL_CLOCK 根因期间先排除掉，减少变量。
CHAIN = [
    ("patch_lcm_stock.py", ["--tables", STOCK_TABLES]),   # LCM_STOCK=1, SHARP_TABLES=0 → BOE表
    ("patch_lk_replay.py", []),                            # LK_REPLAY=1（默认）
    ("patch_stock_timing.py", []),                         # STOCK_TIMING=1（含 PLL_CLOCK 修复）
]

# 与 build.sh 里那段 sed 完全一致（顺序也一致：先 rst_en_new 再 rst_en）
SED_LINES = [
    ('"mediatek,mt8173-lcm"', '"mediatek,nt35523_fhd"'),
    ("gpio_lcm_rst_en_new", "gpio_lcd_rst"),
    ("gpio_lcm_rst_en", "gpio_lcd_rst"),
    ("gpio_lcm_vsp_en", "gpio_lcd_enp"),
    ("gpio_lcm_vsn_en", "gpio_lcd_enn"),
    ("gpio_lcm_1v8_en", "gpio_lcd_enp"),
    ("goto out;", ";"),
]

EXPECT = {
    "S3PRO_STOCK_LCM": 1,
    "S3PRO_LKREPLAY": 1,
    "S3PRO_STOCK_TIMING": 1,
}


def apply_sed(path):
    """模拟 build.sh 里对驱动做的那段 sed。"""
    t = open(path, encoding="utf-8", errors="surrogateescape").read()
    for old, new in SED_LINES:
        t = t.replace(old, new)
    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)


def main():
    print("=" * 64)
    print("面板补丁链离线验证（刷机前，无需编译）")
    print("=" * 64)

    if not os.path.exists(SRC_DRIVER):
        print("FAIL: 原始驱动备份不存在: %s" % SRC_DRIVER)
        return 1

    tmp = tempfile.mkdtemp(prefix="panel_chain_")
    drv_dir = os.path.join(tmp, "drivers", "misc", "mediatek", "lcm",
                           "nt35523_wqxga_dsi_vdo_sharp")
    os.makedirs(drv_dir)
    drv = os.path.join(drv_dir, "nt35523_wqxga_dsi_vdo_sharp.c")
    shutil.copy2(SRC_DRIVER, drv)
    apply_sed(drv)          # 模拟 build.sh 里对驱动做的那段 sed
    print("工作目录: %s" % tmp)
    print("已模拟 build.sh 的 GPIO sed（mt8173-lcm→nt35523_fhd、gpio_lcm_*→gpio_lcd_*、goto out;→;）")

    failed = False

    # 按顺序跑补丁
    for script, extra in CHAIN:
        sp = os.path.join(SCRIPTS, script)
        cmd = [PY, sp, tmp] + extra
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, encoding="utf-8", errors="replace")
        ok = (r.returncode == 0)
        print("  [%s] %-26s" % ("OK" if ok else "FAIL", script))
        if not ok:
            print(r.stdout)
            failed = True
            break

    if not failed:
        code = open(drv, encoding="utf-8", errors="replace").read()

        print("\n--- 关键标记 ---")
        for mark, n in EXPECT.items():
            c = code.count(mark)
            ok = (c >= n)
            print("  [%s] %-22s count=%d (需>=%d)" % ("OK" if ok else "FAIL", mark, c, n))
            if not ok:
                failed = True

        print("\n--- ★ 面板 GPIO（build.sh 的 sed 已改好 node/属性名）---")
        gpio_checks = {
            '"mediatek,nt35523_fhd"': "node 是 nt35523_fhd",
            'of_get_named_gpio(node, "gpio_lcd_enp", 0)': "读 gpio_lcd_enp(VSP/1V8)",
            'of_get_named_gpio(node, "gpio_lcd_enn", 0)': "读 gpio_lcd_enn(VSN)",
            'of_get_named_gpio(node, "gpio_lcd_rst", 0)': "读 gpio_lcd_rst(RST)",
            "gpio_lcm_": "旧 gpio_lcm_* 已清除",
            "goto out;": "goto out 已去掉（不再因 EBUSY 放弃）",
        }
        for needle, label in gpio_checks.items():
            if label.startswith("旧 ") or label.startswith("goto"):
                ok = needle not in code
            else:
                ok = needle in code
            print("  [%s] %-28s" % ("OK" if ok else "FAIL", label))
            if not ok:
                failed = True
        gpio_checks = {}
        for needle, label in gpio_checks.items():
            ok = needle in code
            print("  [%s] %-26s" % ("OK" if ok else "FAIL", label))
            if not ok:
                failed = True

        print("\n--- DSI timing 运行时可切（BOE / sharp 两套预设 + 变量赋值）---")
        timing = {
            "u32 s3pro_tmg_preset;": "preset 变量存在",
            "*vbp = 40; *vfp = 40; *hsync = 4; *hbp = 200; *hfp = 200;":
                "BOE 预设 40/40/4/200/200",
            "*vbp = 8; *vfp = 14; *hsync = 2; *hbp = 55; *hfp = 55;":
                "sharp 预设 8/14/2/55/55",
            "params->dsi.vertical_backporch = t_vbp;": "v_bp 用变量",
            "params->dsi.vertical_frontporch = t_vfp;": "v_fp 用变量",
            "params->dsi.horizontal_sync_active = t_hsync;": "h_sync 用变量",
            "params->dsi.horizontal_backporch = t_hbp;": "h_bp 用变量",
            "params->dsi.horizontal_frontporch = t_hfp;": "h_fp 用变量",
            'debugfs_create_u32("preset"': "debugfs preset 节点",
            # ★ PLL_CLOCK 根因修复（395 是 sharp 的值，BOE 要 500）
            "params->dsi.PLL_CLOCK = t_pll;": "PLL 用变量（不写死）",
            "*pll = 500;": "BOE PLL=500",
            "*pll = 395;": "sharp PLL=395",
            'debugfs_create_u32("pll"': "debugfs pll 节点",
            "params->dsi.noncont_clock = 1;": "noncont_clock=1（LOS 实测）",
            # ★ resume_power 不碰 GPIO（LOS 14.1 只开 regulator）
            "u32 s3pro_lk_no_power = 1;": "no_power 默认=1",
            'debugfs_create_u32("no_power"': "debugfs no_power 节点",
            "if (s3pro_lk_no_power) {": "resume_power 用 no_power 分支",
        }
        # 这两项必须是「不存在」
        if "params->dsi.PLL_CLOCK = 395;" in code:
            print("  [FAIL] %-34s" % "旧 PLL_CLOCK=395 未清除")
            failed = True
        else:
            print("  [OK] %-34s" % "旧 PLL_CLOCK=395 已清除")
        for needle, label in timing.items():
            ok = needle in code
            print("  [%s] %-34s" % ("OK" if ok else "FAIL", label))
            if not ok:
                failed = True

        print("\n--- 表选择实验台（BOE 表 + sharp 表 同时编进内核）---")
        table_checks = {
            "s3pro_init_table_a[]": "BOE 表 A 存在",
            "s3pro_sharp_table[]": "sharp 表存在",
            "0xCE, 2, {0xF0, 0x1F}": "sharp 特征命令 0xCE",
            "0xD7, 2, {0x00, 0x2F}": "sharp 特征命令 0xD7",
            "push SHARP table (12)": "sharp 单独推表分支",
            "push SHARP (12) then BOE A (23)": "sharp→BOE 连发",
            "push BOE A (23) then SHARP (12)": "BOE→sharp 连发",
            "push BOE A (23) then BOE B (109)": "BOE A→B 连发",
            'debugfs_create_u32("table"': "debugfs table 节点",
            'debugfs_create_u32("table_gap"': "debugfs table_gap 节点",
            'debugfs_create_u32("vsn_dly"': "debugfs vsn_dly 节点",
            'debugfs_create_u32("repeat"': "debugfs repeat 节点",
            "case 4:  d1 = 5;  d2 = 2;  d3 = 15;": "variant4 精确复刻 5/2/15",
            "MDELAY(vsn_d);": "vsn_dly 变量生效",
            "re-push table #%u": "二次推表循环",
            "table done (reps=%u)": "table done 带 reps",
        }
        for needle, label in table_checks.items():
            ok = needle in code
            print("  [%s] %-26s" % ("OK" if ok else "FAIL", label))
            if not ok:
                failed = True

        print("\n--- 错误时序已移除 ---")
        for needle in ["vertical_backporch = 8;", "horizontal_backporch = 55;"]:
            ok = needle not in code
            print("  [%s] 已移除 %-30s" % ("OK" if ok else "FAIL", needle))
            if not ok:
                failed = True

        print("\n--- BOE 表确认（原厂能亮的那套）---")
        # 只检查实际推的 s3pro_init_table_a[] 数组内容，不要全文搜（表尾和睡眠表本就含 0x11）
        m = re.search(r"s3pro_init_table_a\[\]\s*=\s*\{(.*?)\n\};", code, re.S)
        if not m:
            print("  [FAIL] 找不到 s3pro_init_table_a[] 数组")
            failed = True
        else:
            body = m.group(1)
            # 数以 tab+{ 开头的行 = 表项数（嵌套的 {} 不影响）
            init_items = [ln for ln in body.splitlines() if ln.strip().startswith("{")]
            boe_head = "0xFF, 4, {0xAA, 0x55, 0xA5, 0x80" in body
            ok_cnt = (len(init_items) == 23)
            print("  [%s] 初始化表项数=%d (BOE表应23项)" %
                  ("OK" if ok_cnt else "FAIL", len(init_items)))
            print("  [%s] BOE 表头 FF{AA55A580} 存在" % ("OK" if boe_head else "FAIL"))
            if not ok_cnt or not boe_head:
                failed = True

        print("\n--- 结构校验 ---")
        bc, bc2 = code.count("{"), code.count("}")
        ok = (bc == bc2)
        print("  [%s] 花括号配平 %d/%d" % ("OK" if ok else "FAIL", bc, bc2))
        if not ok:
            failed = True

    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 64)
    if failed:
        print("结果：FAIL —— 补丁链有问题，不要刷机，先修")
    else:
        print("结果：PASS —— 补丁链在默认开关下生成了正确的原厂配置驱动")
    print("=" * 64)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
