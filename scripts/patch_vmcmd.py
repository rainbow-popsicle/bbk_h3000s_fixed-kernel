#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S3PRO_VMCMD：清 DSI_START 的 VM_CMD_START(bit16) 悬挂，验证黑屏根因。

=== 背景（实测 + 源码确认） ===
熄屏唤醒后纯黑态：DSI_START=0x00010001（bit16 VM_CMD_START 卡1）、
DSI_STA=0、DSI_TRIG_STA=0、VM_CMD_DONE=0，但 D-PHY 在 HS、面板回读 0x9C 正常。
手动 dpmgr_path_trigger / stop+trigger+start loop 都救不回来。

源码因果链：
  lcm_resume -> DSI_set_cmdq(force_update=1) -> DSI_EnableVM_CMD()
    置 VM_CMD_START=1（ddp_dsi.c:1586-1592, 1961）
  此时 DSI 引擎/HS/VFP 未就绪 -> VM_CMD_DONE 永不到来 -> bit16 悬挂
  VM 控制状态机被占住 -> video 帧出不来
  全树没有任何代码把 bit16 单独清 0 作为收尾；
  现有 S3PRO_DSI_START_FIX / DSI_Start() 只脉冲 bit0，所以无效。
  switch_mode(V2C) 路径(ddp_dsi.c:2983)会 0->1 清理 bit16 —— 本节点复用同款写法。

=== 本补丁做什么 ===
在 ddp_dsi.c（DSI_REG/DSI_OUTREGBIT/DSI_Start 都可见）新增 debugfs 节点
  /sys/kernel/debug/s3pro_vmcmd
写入命令（用字符串 echo 即可，本节点在 ddp_dsi.c 内自己解析）：
  echo snap    -> pr_alert 打印 START 各位 + INTSTA(VM_CMD_DONE/VM_DONE/BUSY)
                  + TRIG_STA + VM_CMD_CON + STATE_DBG7.VMCTL_STATE
  echo clr     -> 只清 VM_CMD_START(bit16)，保留 DSI_START(bit0)
  echo restart -> 清 bit16 + 重新脉冲 bit0（更对症）
  echo inject  -> 根因验证：对待发命令的 port 恢复 VM_CMD_EN/TS_VFP_EN(低8位=0x21)
                  + 重新脉冲 bit16，让卡在队列的亮度表真正注入发出去

实测：clr/restart 能让 START 回 0x00000001 但仍黑；bit16 悬挂背后是
VM_CMD_CON 低8位被清零(0x2c531500)、VM_CMD_DONE 永不到、INTSTA BUSY=1 卡死。
inject 用于验证"注入使能被清零"是否根因。

只加诊断节点，不改任何显示逻辑/时序。全部 pr_alert 无条件输出。

=== 插入位置 ===
文件头 include 后加 <linux/debugfs.h>/<linux/uaccess.h>；
函数实体插在 ddp_dsi_init() 定义之前（2525行），此处 DSI_REG 等符号均可见；
节点注册在 ddp_dsi_init() 里 DSI_REG[...] 赋值之后（2537行后）。

用法：
    python patch_vmcmd.py <内核树根> [--dry-run]
开关：VMCMD=1 时由 build.sh 调用；不依赖 DISP_DEBUG。
"""
import os
import sys

MARK = "S3PRO_VMCMD"
FUNC_ANCHOR = "int ddp_dsi_init(DISP_MODULE_ENUM module, void *cmdq)"
REG_ANCHOR = "\tDSI_REG[1] = (PDSI_REGS) DISPSYS_DSI1_BASE;"
INC_ANCHOR = '#include <linux/wait.h>'

BLOCK = r'''/* S3PRO_VMCMD: 清 VM_CMD_START(bit16) 悬挂，验证黑屏根因。诊断节点，不改显示逻辑。 */
static struct dentry *s3pro_vmcmd_dent;

static void s3pro_vmcmd_snap(const char *tag)
{
	int i;
	for (i = 0; i < 2; i++) {
		pr_alert("[S3PRO_VMCMD] %s dsi%d START=0x%08x vm_cmd_start=%u dsi_start=%u sleepout=%u "
			 "INTSTA=0x%08x vm_cmd_done=%u vm_done=%u busy=%u TRIG=0x%08x VMCMD_CON=0x%08x DBG7=0x%08x vmctl=%u\n",
			 tag, i,
			 INREG32(&DSI_REG[i]->DSI_START),
			 (unsigned)DSI_REG[i]->DSI_START.VM_CMD_START,
			 (unsigned)DSI_REG[i]->DSI_START.DSI_START,
			 (unsigned)DSI_REG[i]->DSI_START.SLEEPOUT_START,
			 INREG32(&DSI_REG[i]->DSI_INTSTA),
			 (unsigned)DSI_REG[i]->DSI_INTSTA.VM_CMD_DONE,
			 (unsigned)DSI_REG[i]->DSI_INTSTA.VM_DONE,
			 (unsigned)DSI_REG[i]->DSI_INTSTA.BUSY,
			 INREG32(&DSI_REG[i]->DSI_TRIG_STA),
			 INREG32(&DSI_REG[i]->DSI_VM_CMD_CON),
			 INREG32(&DSI_REG[i]->DSI_STATE_DBG7),
			 (unsigned)DSI_REG[i]->DSI_STATE_DBG7.VMCTL_STATE);
	}
}

static ssize_t s3pro_vmcmd_write(struct file *file, const char __user *buf,
				 size_t count, loff_t *ppos)
{
	char cmd[32] = {0};
	unsigned int clen = count < sizeof(cmd) - 1 ? count : sizeof(cmd) - 1;
	int i;

	if (copy_from_user(cmd, buf, clen))
		return -EFAULT;

	s3pro_vmcmd_snap("before");
	if (!strncmp(cmd, "clr", 3)) {
		/* 只清 VM_CMD_START(bit16)，保留 bit0（NULL -> CPU 直写非阻塞）。 */
		pr_alert("[S3PRO_VMCMD] clr VM_CMD_START only\n");
		for (i = 0; i < 2; i++)
			DSI_OUTREGBIT(NULL, DSI_START_REG, DSI_REG[i]->DSI_START,
				      VM_CMD_START, 0);
	} else if (!strncmp(cmd, "restart", 7)) {
		/* 清 bit16 + 重新脉冲 bit0（照抄 DSI_Start() 内部：0->1）。
		 * 不调 DSI_Start(module,...) 是为了避免枚举与端口索引对应出错；
		 * 这里直接对两个 port 各写一遍，等价于其函数体。 */
		pr_alert("[S3PRO_VMCMD] clr VM_CMD_START + pulse DSI_START\n");
		for (i = 0; i < 2; i++) {
			DSI_OUTREGBIT(NULL, DSI_START_REG, DSI_REG[i]->DSI_START,
				      VM_CMD_START, 0);
			DSI_OUTREGBIT(NULL, DSI_START_REG, DSI_REG[i]->DSI_START,
				      DSI_START, 0);
			DSI_OUTREGBIT(NULL, DSI_START_REG, DSI_REG[i]->DSI_START,
				      DSI_START, 1);
		}
	} else if (!strncmp(cmd, "inject", 6)) {
		/* 根因验证：VM_CMD_CON 低8位(VM_CMD_EN/TS_VFP_EN)被清零，导致
		 * VM_CMD_START=1 后无注入通道 -> VM_CMD_DONE 永不到 -> busy 卡死。
		 * 这里把低8位恢复成 0x21(VM_CMD_EN|TS_VFP_EN)，保留高位命令数据，
		 * 清 bit16 后重新脉冲，让 staging 的命令真正注入 VFP 发出去。 */
		unsigned int vc;
		pr_alert("[S3PRO_VMCMD] enable VM_CMD_EN/TS_VFP_EN + repulse\n");
		for (i = 0; i < 2; i++) {
			vc = INREG32(&DSI_REG[i]->DSI_VM_CMD_CON);
			pr_alert("[S3PRO_VMCMD] dsi%d VMCMD_CON before=0x%08x\n", i, vc);
			/* 只对"有待发命令"(高16位命令数据非0)的 port 恢复注入使能；
			 * 保留命令数据，只把低8位控制位恢复成 0x21。
			 * 健康态队列为空(vc 高位=0)时不动，避免破坏正常显示。 */
			if ((vc & 0xffff0000) != 0) {
				vc = (vc & 0xffffff00) | 0x21;
				DSI_OUTREG32(NULL, &DSI_REG[i]->DSI_VM_CMD_CON, vc);
				pr_alert("[S3PRO_VMCMD] dsi%d VMCMD_CON ->0x%08x\n", i, vc);
			}
			DSI_OUTREGBIT(NULL, DSI_START_REG, DSI_REG[i]->DSI_START,
				      VM_CMD_START, 0);
			DSI_OUTREGBIT(NULL, DSI_START_REG, DSI_REG[i]->DSI_START,
				      VM_CMD_START, 1);
		}
	} else if (!strncmp(cmd, "snap", 4)) {
		pr_alert("[S3PRO_VMCMD] snapshot only\n");
	} else {
		pr_alert("[S3PRO_VMCMD] unknown cmd (use snap/clr/restart/inject)\n");
	}
	s3pro_vmcmd_snap("after");
	return count;
}

static ssize_t s3pro_vmcmd_read(struct file *file, char __user *buf,
				size_t count, loff_t *ppos)
{
	s3pro_vmcmd_snap("read");
	return 0;
}

static const struct file_operations s3pro_vmcmd_fops = {
	.owner = THIS_MODULE,
	.read = s3pro_vmcmd_read,
	.write = s3pro_vmcmd_write,
};

static void s3pro_vmcmd_init_node(void)
{
	if (s3pro_vmcmd_dent == NULL)
		s3pro_vmcmd_dent = debugfs_create_file("s3pro_vmcmd", 0644, NULL,
						       NULL, &s3pro_vmcmd_fops);
}

'''


def find_ddsi(tree):
    p = os.path.join(tree, "drivers", "misc", "mediatek", "video", "mt8173",
                     "dispsys", "ddp_dsi.c")
    return p if os.path.exists(p) else None


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    p = find_ddsi(tree)
    if not p:
        raise SystemExit("找不到 ddp_dsi.c")
    print("  目标: %s" % os.path.relpath(p, tree))

    t = open(p, encoding="utf-8", errors="surrogateescape").read()
    if MARK in t:
        print("  已打过补丁，跳过")
        return 0

    for name, anchor in [("include", INC_ANCHOR), ("函数锚点", FUNC_ANCHOR),
                         ("注册锚点", REG_ANCHOR)]:
        cnt = t.count(anchor)
        if cnt != 1:
            print("!! %s 锚点匹配 %d 次（应为 1）: %r" % (name, cnt, anchor))
            return 1

    # 1) include
    t = t.replace(INC_ANCHOR, INC_ANCHOR + '\n#include <linux/debugfs.h>\n#include <linux/uaccess.h>', 1)
    # 2) 函数实体（在 ddp_dsi_init 之前）
    t = t.replace(FUNC_ANCHOR, BLOCK + FUNC_ANCHOR, 1)
    # 3) 节点注册（DSI_REG 赋值之后）
    t = t.replace(REG_ANCHOR, REG_ANCHOR + '\n\ts3pro_vmcmd_init_node();', 1)
    print("  已插入 s3pro_vmcmd 节点（include / 函数 / 注册 三处）")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(p, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "标记存在": MARK in c,
        "写函数已定义": "s3pro_vmcmd_write" in c,
        "节点创建": 'debugfs_create_file("s3pro_vmcmd"' in c,
        "注册调用": "\ts3pro_vmcmd_init_node();" in c,
        "include debugfs": "#include <linux/debugfs.h>" in c,
        "include uaccess": "#include <linux/uaccess.h>" in c,
        "清 bit16": c.count("VM_CMD_START, 0);") >= 2,
        "DSI_START 重脉冲": c.count("DSI_START, 1);") >= 1,
        "VM_CMD_DONE 快照": "DSI_REG[i]->DSI_INTSTA.VM_CMD_DONE" in c,
        "VMCTL_STATE 快照": "DSI_STATE_DBG7.VMCTL_STATE" in c,
        "inject 恢复注入使能": "vc & 0xffffff00) | 0x21" in c,
        "pr_alert 输出": c.count('pr_alert("[S3PRO_VMCMD]') >= 7,
        "花括号配平": c.count("{") == c.count("}"),
    }
    print()
    bad = 0
    for k, v in checks.items():
        print("    %-26s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1
    print("\nvmcmd 诊断补丁完成（只加节点，不改显示逻辑）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
