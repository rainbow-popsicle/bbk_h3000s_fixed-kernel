#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""恢复 read_id 探针：把 mtk_lcm_s3pro_readback() 真正链接进内核。

=== 背景 ===
primary_display.c（patch_disp_debug.py 注入）提供 debugfs 节点
  /sys/kernel/debug/s3pro_read_id
它调用 weak 符号 mtk_lcm_s3pro_readback()；但历史上定义该符号的
patch_lcm_debug.py 默认关闭（LCM_DEBUG=0），所以节点一直打印
"readback symbol not linked"。

=== 为什么历史回读"健康态也全 0"是假象 ===
底层 DSI_dcs_read_lcm_reg_v2() 第 1616 行：
    if (DSI_REG[d]->DSI_MODE_CTRL.MODE) return 0;
MODE 枚举：0=CMD, 1=SYNC_PULSE_VDO, 2=SYNC_EVENT_VDO, 3=BURST_VDO。
正常显示态 mode=3（video BURST），所以回读函数**第一行就返回 0，
根本没发 BTA 总线翻转** —— 这与面板是否响应无关。

=== 本补丁的关键改进 ===
回读前先把 DSI 临时切到 CMD 模式（MODE=0），让 BTA 真正发出；
读完立即切回原 video 模式。这样"健康态能否读到 0x9C/面板 ID"
才成为有效对照，可一刀切判断"面板是否收到命令/是否活着"。

=== 只加诊断，不改任何显示逻辑/时序 ===
函数定义插在 lcm_resume 之前，不在 resume 里调用（避免与
patch_lcm_stock/patch_lcm_trace/patch_lcm_power_seq 冲突）。
只由 debugfs 节点手动触发。

用法：
    python patch_lcm_readid.py <内核树根> [--dry-run]
开关：LCM_READID=1 时由 build.sh 调用。
"""
import os
import sys

MARK = "S3PRO_READID"

# 插入在 lcm_resume 定义之前；只定义函数，不调用。
FUNC_ANCHOR = "static void lcm_resume(void)"

RDBACK_FUNC = r'''/* S3PRO_READID: DCS 回读探针（由 s3pro_read_id debugfs 节点触发）。
 * 关键：先临时切 CMD 模式，否则 video 模式下底层回读直接 return 0
 * （DSI_dcs_read_lcm_reg_v2: if (MODE_CTRL.MODE) return 0）。
 * 用 pr_alert 无条件输出，不受 printk 级别过滤。 */
extern void s3pro_dsi_set_mode(int cmd_mode);

void mtk_lcm_s3pro_readback(void)
{
	unsigned char rb[4];
	unsigned int ret;

	pr_alert("[S3PRO_RD] === readback begin ===\n");

	/* video 模式下 dcs_read 会短路返回 0，必须先切 CMD */
	s3pro_dsi_set_mode(1);
	MDELAY(5);

	memset(rb, 0, sizeof(rb));
	ret = read_reg_v2(0x0A, rb, 1);
	pr_alert("[S3PRO_RD] 0x0A power_mode: ret=%u val=0x%02X (0x9C=healthy)\n",
		 ret, rb[0]);

	memset(rb, 0, sizeof(rb));
	ret = read_reg_v2(0xDA, rb, 2);
	pr_alert("[S3PRO_RD] 0xDA id1: ret=%u val=0x%02X%02X\n",
		 ret, rb[0], rb[1]);

	memset(rb, 0, sizeof(rb));
	ret = read_reg_v2(0xDB, rb, 2);
	pr_alert("[S3PRO_RD] 0xDB id2: ret=%u val=0x%02X%02X\n",
		 ret, rb[0], rb[1]);

	/* 恢复原来的 video 模式，避免破坏正常显示 */
	s3pro_dsi_set_mode(0);
	pr_alert("[S3PRO_RD] === readback end ===\n");
}

'''


def find_driver(tree):
    """按 .config 的 CONFIG_CUSTOM_KERNEL_LCM 精确定位实际编译的驱动。"""
    base = os.path.join(tree, "drivers", "misc", "mediatek", "lcm")
    name = None
    cfg = os.path.join(tree, ".config")
    if os.path.exists(cfg):
        for line in open(cfg, encoding="utf-8", errors="replace"):
            if line.startswith("CONFIG_CUSTOM_KERNEL_LCM="):
                name = line.split("=", 1)[1].strip().strip('"')
                break
    # 优先 sharp（build77 确认编译的就是它）
    cands = []
    if name:
        d = os.path.join(base, name)
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.endswith(".c"):
                    cands.append(os.path.join(d, f))
    if not cands:
        d = os.path.join(base, "nt35523_wqxga_dsi_vdo_sharp")
        if os.path.isdir(d):
            for f in os.listdir(d):
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
        if "lcm_resume" in t and "read_reg_v2" in t:
            return p
    return None


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
        print("  已打过补丁，跳过")
        return 0

    cnt = t.count(FUNC_ANCHOR)
    if cnt != 1:
        print("!! lcm_resume 锚点匹配 %d 次（应为 1），中止" % cnt)
        return 1

    t = t.replace(FUNC_ANCHOR, RDBACK_FUNC + FUNC_ANCHOR, 1)
    print("  已插入 mtk_lcm_s3pro_readback()（切CMD回读+pr_alert）")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(p, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "标记存在": MARK in c,
        "回读函数已定义": "void mtk_lcm_s3pro_readback(void)" in c,
        "切CMD模式": "s3pro_dsi_set_mode(1);" in c,
        "切回video": "s3pro_dsi_set_mode(0);" in c,
        "pr_alert打印": c.count('pr_alert("[S3PRO_RD]') >= 5,
        "回读0x0A": "read_reg_v2(0x0A, rb, 1);" in c,
        "回读0xDA/0xDB": ("read_reg_v2(0xDA, rb, 2);" in c) and ("read_reg_v2(0xDB, rb, 2);" in c),
        "花括号配平": c.count("{") == c.count("}"),
    }
    print()
    bad = 0
    for k, v in checks.items():
        print("    %-20s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1
    if bad:
        return 1
    print("\nread_id 探针链接完成（只加诊断，不改时序）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
