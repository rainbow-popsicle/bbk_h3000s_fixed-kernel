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
CHAIN = [
    ("patch_lcm_stock.py", ["--tables", STOCK_TABLES]),   # LCM_STOCK=1, SHARP_TABLES=0 → BOE表
    ("patch_stock_timing.py", []),                         # STOCK_TIMING=1
    ("patch_rst_new.py", []),                              # RST_NEW_FIX=1
    ("patch_dimming_early.py", []),                        # DIMMING_EARLY=1
]

EXPECT = {
    "S3PRO_STOCK_LCM": 1,
    "S3PRO_STOCK_TIMING": 1,
    "S3PRO_RST_NEW_FIX": 2,
    "S3PRO_DIM_EARLY": 3,
}


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
    print("工作目录: %s" % tmp)

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

        print("\n--- 原厂时序参数（根因修复判据）---")
        timing = {
            "vertical_backporch = 40;": "v_bp=40",
            "vertical_frontporch = 40;": "v_fp=40",
            "horizontal_sync_active = 4;": "h_sync=4",
            "horizontal_backporch = 200;": "h_bp=200",
            "horizontal_frontporch = 200;": "h_fp=200",
        }
        for needle, label in timing.items():
            ok = needle in code
            print("  [%s] %-12s" % ("OK" if ok else "FAIL", label))
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
