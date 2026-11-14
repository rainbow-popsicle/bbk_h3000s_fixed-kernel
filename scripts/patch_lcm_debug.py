#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板驱动诊断：**只加 DCS 回读，不改任何时序**。

=== 设计原则 ===
之前的改动（复位后 120ms、VSN 后 40ms）都是猜测，已全部撤销。
本补丁唯一的动作是"测量"：推完初始化表后回读面板寄存器，
用客观数据回答"面板到底有没有被正确上电/复位、MIPI 双向是否通"。

=== 判读方法 ===
  S3PRO_LCM rd 0x0A = 0x9C
      → 面板活着、上电与复位正常、MIPI 双向通信正常
      → 病灶在视频流参数（porch / lane / clock），去 LK 里逐字段对比 DSI 配置

  S3PRO_LCM rd 0x0A = 0x00 或 LCD_DA/DB 全 0
      → 面板没被正确上电或复位
      → 才轮到研究电源/复位时序（届时从 LK 反推真实延时，不拍脑袋）

  0xDA/0xDB 是面板 ID（NT35523 常见 0x80 / 0x90 开头），能读到即有值。

用法：
    python patch_lcm_debug.py <内核树根> [--dry-run]
"""
import os
import sys

MARK = "S3PRO_LCM_RDBACK"


def find_driver(tree):
    base = os.path.join(tree, "drivers", "misc", "mediatek", "lcm")
    for root, _d, files in os.walk(base):
        for f in files:
            if not f.endswith(".c"):
                continue
            p = os.path.join(root, f)
            t = open(p, encoding="utf-8", errors="replace").read()
            if "lcm_initialization_setting[]" in t and "static void lcm_resume(void)" in t:
                return p
    return None


OLD_PUSH = """	LCM_LOGI("push table\\n");
	push_table(lcm_initialization_setting,
		   sizeof(lcm_initialization_setting) / sizeof(struct LCM_setting_table), 1);
"""

NEW_PUSH = """	LCM_LOGI("push table\\n");
	push_table(lcm_initialization_setting,
		   sizeof(lcm_initialization_setting) / sizeof(struct LCM_setting_table), 1);

	/* S3PRO_LCM_RDBACK: 推表后回读面板寄存器，判断面板是否活着。
	 *   0x0A = power mode，正常应为 0x9C（sleep out + display on 完成）
	 *   0xDA/0xDB = 面板 ID，能读到说明 MIPI 双向通信正常
	 * 本段只读取，不改变任何时序或状态。 */
	{
		unsigned char rb[4];

		rb[0] = 0;
		read_reg_v2(0x0A, rb, 1);
		LCM_LOGI("S3PRO_LCM rd 0x0A = 0x%02X\\n", rb[0]);

		rb[0] = 0; rb[1] = 0;
		read_reg_v2(0xDA, rb, 2);
		LCM_LOGI("S3PRO_LCM rd 0xDA = 0x%02X%02X\\n", rb[0], rb[1]);

		rb[0] = 0; rb[1] = 0;
		read_reg_v2(0xDB, rb, 2);
		LCM_LOGI("S3PRO_LCM rd 0xDB = 0x%02X%02X\\n", rb[0], rb[1]);
	}
"""


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    p = find_driver(tree)
    if not p:
        raise SystemExit("找不到面板驱动文件")
    print("  驱动: %s" % os.path.relpath(p, tree))

    t = open(p, encoding="utf-8", errors="surrogateescape").read()
    if MARK in t:
        print("  已加过 DCS 回读，跳过")
        return 0

    cnt = t.count(OLD_PUSH)
    if cnt != 1:
        print("!! push table 锚点匹配 %d 次（应为 1），中止" % cnt)
        return 1
    t = t.replace(OLD_PUSH, NEW_PUSH, 1)
    print("  推表后加入 DCS 回读（0x0A / 0xDA / 0xDB）")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(p, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "标记存在": MARK in c,
        "回读 0x0A": "read_reg_v2(0x0A, rb, 1);" in c,
        "回读 0xDA": "read_reg_v2(0xDA, rb, 2);" in c,
        "回读 0xDB": "read_reg_v2(0xDB, rb, 2);" in c,
        "未改任何 MDELAY": ("MDELAY(120)" not in c) and ("MDELAY(40)" not in c),
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
    print("\nDCS 回读补丁完成（纯测量，时序未改动）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
