#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板通信诊断：把 DCS 回读做成可随时触发的函数（只读，不改时序）。

=== 为什么需要 ===
build-52 实测：
  * 推表后立即回读 → 0x0A/0xDA/0xDB 全 0，但那次读发生在 dsi_start 之前 91us，
    没有数据通路，0 不能说明问题。
  * 用 reinit_on_resume 让它在 start 之后再读 → 仍然全 0。
  * 但缺一个关键对照：**面板健康时，这套读命令能不能读到？**
    如果健康时也是 0，说明读通路本身不可用，0 就毫无意义。

=== 本补丁做两件事 ===
 1) 把回读封装成非 static 函数 mtk_lcm_s3pro_readback()，
    lcm_resume 推表后仍然会调用它（保留原有观测点）。
 2) 让 primary_display.c 能通过 debugfs 节点随时调用它
    （节点由 patch_disp_debug.py 提供：/sys/kernel/debug/s3pro_read_id）

=== 用法（刷入后）===
    # 开机后面板处于健康状态，直接触发回读做对照：
    echo 1 > /sys/kernel/debug/s3pro_read_id
    dmesg | grep "S3PRO_LCM rd"

    读到非 0  → 读通路可用，之前的 0 说明"断电重建后面板确实没响应"
    仍读到 0  → 读通路本身不可用，需要用别的方式判断面板状态

用法：
    python patch_lcm_debug.py <内核树根> [--dry-run]
"""
import os
import sys

MARK = "S3PRO_LCM_RDBACK"

RDBACK_FUNC = '''/* S3PRO_LCM_RDBACK: 回读面板寄存器，判断面板是否在通信。
 *   0x0A = power mode（正常 0x9C：sleep out + display on 完成）
 *   0xDA/0xDB = 面板 ID
 * 非 static，方便 primary_display.c 的 debugfs 节点随时触发对照测量。
 * 只做读取，不改变任何状态或时序。 */
void mtk_lcm_s3pro_readback(void)
{
	unsigned char rb[4];

	rb[0] = 0;
	read_reg_v2(0x0A, rb, 1);
	LCM_LOGI("S3PRO_LCM rd 0x0A = 0x%02X\\n", rb[0]);

	rb[0] = 0;
	rb[1] = 0;
	read_reg_v2(0xDA, rb, 2);
	LCM_LOGI("S3PRO_LCM rd 0xDA = 0x%02X%02X\\n", rb[0], rb[1]);

	rb[0] = 0;
	rb[1] = 0;
	read_reg_v2(0xDB, rb, 2);
	LCM_LOGI("S3PRO_LCM rd 0xDB = 0x%02X%02X\\n", rb[0], rb[1]);
}

'''

FUNC_ANCHOR = "static void lcm_resume(void)"

OLD_PUSH = (
    '	LCM_LOGI("push table\\n");\n'
    '	push_table(lcm_initialization_setting,\n'
    '		   sizeof(lcm_initialization_setting) / sizeof(struct LCM_setting_table), 1);\n'
)

NEW_PUSH = (
    '	LCM_LOGI("push table\\n");\n'
    '	push_table(lcm_initialization_setting,\n'
    '		   sizeof(lcm_initialization_setting) / sizeof(struct LCM_setting_table), 1);\n'
    '\n'
    '	mtk_lcm_s3pro_readback();\n'
)


def find_driver(tree):
    """按 .config 的 CONFIG_CUSTOM_KERNEL_LCM 精确定位（树里有多个面板驱动）。"""
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

    p = find_driver(tree)
    if not p:
        raise SystemExit("找不到面板驱动文件（检查 CONFIG_CUSTOM_KERNEL_LCM）")
    print("  驱动: %s" % os.path.relpath(p, tree))

    t = open(p, encoding="utf-8", errors="surrogateescape").read()
    if MARK in t:
        print("  已打过补丁，跳过")
        return 0

    # 1) 插入回读函数
    cnt = t.count(FUNC_ANCHOR)
    if cnt != 1:
        print("!! lcm_resume 锚点匹配 %d 次（应为 1），中止" % cnt)
        return 1
    t = t.replace(FUNC_ANCHOR, RDBACK_FUNC + FUNC_ANCHOR, 1)
    print("  已插入 mtk_lcm_s3pro_readback()")

    # 2) lcm_resume 推表后调用
    cnt = t.count(OLD_PUSH)
    if cnt != 1:
        print("!! push table 锚点匹配 %d 次（应为 1），中止" % cnt)
        return 1
    t = t.replace(OLD_PUSH, NEW_PUSH, 1)
    print("  lcm_resume 推表后调用回读")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(p, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "标记存在": MARK in c,
        "回读函数已定义": "void mtk_lcm_s3pro_readback(void)" in c,
        "lcm_resume 里调用": "mtk_lcm_s3pro_readback();" in c,
        "回读 0x0A": "read_reg_v2(0x0A, rb, 1);" in c,
        "回读 0xDA/0xDB": ("read_reg_v2(0xDA, rb, 2);" in c) and ("read_reg_v2(0xDB, rb, 2);" in c),
        "未改任何 MDELAY": ("MDELAY(120)" not in c) and ("MDELAY(40)" not in c),
        "花括号配平": c.count("{") == c.count("}"),
    }
    print()
    bad = 0
    for k, v in checks.items():
        print("    %-24s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1
    if bad:
        return 1
    print("\n面板回读诊断完成（只读，时序未改动）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
