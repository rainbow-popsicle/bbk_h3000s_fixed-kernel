#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""显示诊断插桩 v5：节点开机即建 + 运行时实验开关 + 寄存器快照。

实机已确认的事实链：
  * 开机画面能显示：panel 由 bootloader 初始化，开机路径不调用 ddp_dsi_start。
  * 第一次熄屏：primary_display_suspend 完整拆除（stop + 断电 + 面板 sleep-in）。
  * 唤醒：lcm_resume 重推 109 项原厂表 → 仍然黑屏（只有背光）。
  * 寄存器对比唯一差异 DSI_START（开机 1 / 唤醒 0），已在 patch_dsi_start.py 修复；
    修复后实测唤醒 DSI_START=1，但屏幕**仍然黑**。
  * 在视频流已启动后重推一次初始化表（reinit_on_resume=1）——仍然黑。
  * skip_stop=1（不拆管线）——仍然黑，但这是**无效实验**：该节点原本是在
    primary_display_suspend 里创建的，第一次熄屏时标志必然还是 0，
    面板已经被拆过一次，之后再设 1 只是"防止二次拆卸"，救不回来。

本补丁 v5 的关键改动：
  把两个 debugfs 节点提前到 **primary_display_init** 阶段创建，
  这样开机后（面板还处于 bootloader 初始化好的正常状态、还没熄过屏）
  就能先把 skip_stop=1 写进去，再做"完全不拆管线"的干净实验。

节点：
  /sys/kernel/debug/s3pro_skip_stop          写 1 → 熄屏时不拆显示管线
  /sys/kernel/debug/s3pro_reinit_on_resume   写 1 → 唤醒后在 start 之后重推表

用法：
    python patch_disp_debug.py <内核树根> [--dry-run]
"""
import os
import sys

MARK = "S3PRO_DBG"

REGS_FUNC = '''/* S3PRO_DBG: DSI 寄存器快照，用于对比"能显示"与"黑屏"两种状态 */
static void s3pro_dsi_regs(const char *tag)
{
	int i;

	for (i = 0; i < 2; i++) {
		DISPMSG("S3PRO_REG %s dsi%d A start=%08x intsta=%08x com=%08x mode=%08x txrx=%08x ps=%08x\\n",
			tag, i,
			INREG32(&DSI_REG[i]->DSI_START), INREG32(&DSI_REG[i]->DSI_INTSTA),
			INREG32(&DSI_REG[i]->DSI_COM_CTRL), INREG32(&DSI_REG[i]->DSI_MODE_CTRL),
			INREG32(&DSI_REG[i]->DSI_TXRX_CTRL), INREG32(&DSI_REG[i]->DSI_PSCTRL));
		DISPMSG("S3PRO_REG %s dsi%d B vsa=%08x vbp=%08x vfp=%08x vact=%08x hsa=%08x hbp=%08x hfp=%08x\\n",
			tag, i,
			INREG32(&DSI_REG[i]->DSI_VSA_NL), INREG32(&DSI_REG[i]->DSI_VBP_NL),
			INREG32(&DSI_REG[i]->DSI_VFP_NL), INREG32(&DSI_REG[i]->DSI_VACT_NL),
			INREG32(&DSI_REG[i]->DSI_HSA_WC), INREG32(&DSI_REG[i]->DSI_HBP_WC),
			INREG32(&DSI_REG[i]->DSI_HFP_WC));
		DISPMSG("S3PRO_REG %s dsi%d C phylc=%08x phyld0=%08x tim0=%08x tim3=%08x vmcmd=%08x dbg0=%08x dbg1=%08x\\n",
			tag, i,
			INREG32(&DSI_REG[i]->DSI_PHY_LCCON), INREG32(&DSI_REG[i]->DSI_PHY_LD0CON),
			INREG32(&DSI_REG[i]->DSI_PHY_TIMECON0), INREG32(&DSI_REG[i]->DSI_PHY_TIMECON3),
			INREG32(&DSI_REG[i]->DSI_VM_CMD_CON),
			INREG32(&DSI_REG[i]->DSI_STATE_DBG0), INREG32(&DSI_REG[i]->DSI_STATE_DBG1));
	}
}

'''

# 节点定义（放在文件靠前位置，保证 primary_display_init 能用）
NODES_FUNC = '''/* S3PRO_DBG: 运行时实验节点（普通 u32，默认 0 = 不改变行为）
 *
 * s3pro_skip_stop         写 1 → 熄屏时不拆显示管线（等同作者内核）
 * s3pro_reinit_on_resume  写 1 → 唤醒后在 dsi_start 之后重推面板初始化表
 *
 * 注意：本函数在 primary_display_init 里就会被调用一次，
 * 因此开机后立即可写这两个节点，不必等第一次熄屏。
 */
static u32 s3pro_skip_stop;
static u32 s3pro_reinit_on_resume;
static struct dentry *s3pro_skip_dent;
static struct dentry *s3pro_reinit_dent;

static void s3pro_dbg_init_once(void)
{
	if (s3pro_skip_dent == NULL)
		s3pro_skip_dent = debugfs_create_u32("s3pro_skip_stop", 0644,
						     NULL, &s3pro_skip_stop);
	if (s3pro_reinit_dent == NULL)
		s3pro_reinit_dent = debugfs_create_u32("s3pro_reinit_on_resume", 0644,
						       NULL, &s3pro_reinit_on_resume);
}

'''

CONFIG_DEF = "int ddp_dsi_config(DISP_MODULE_ENUM module, disp_ddp_path_config *config, void *cmdq)"

CONFIG_TAIL = """done:
	return 0;
}

int ddp_dsi_start(DISP_MODULE_ENUM module, void *cmdq)"""
CONFIG_TAIL_NEW = """done:
	s3pro_dsi_regs("config");
	return 0;
}

int ddp_dsi_start(DISP_MODULE_ENUM module, void *cmdq)"""

DSI_TAIL = "	return 0;\n}\n\nint ddp_dsi_stop(DISP_MODULE_ENUM module, void *cmdq_handle)"
DSI_TAIL_NEW = (
    '	DISPMSG("S3PRO_DBG dsi_start_end: mipitx=%d dsiclk=%d intsta0=0x%08x intsta1=0x%08x\\n",\n'
    '		MIPITX_IsEnabled(module, NULL), DSI_ClkIsEnabled(module, NULL),\n'
    '		INREG32(&DSI_REG[0]->DSI_INTSTA), INREG32(&DSI_REG[1]->DSI_INTSTA));\n'
    '	s3pro_dsi_regs("start");\n'
    "\n" + DSI_TAIL
)

STOP_TAIL = "	return 0;\n}\n\nint ddp_dsi_switch_lcm_mode"
STOP_TAIL_NEW = (
    '	DISPMSG("S3PRO_DBG dsi_stop_end: mipitx=%d dsiclk=%d intsta0=0x%08x intsta1=0x%08x\\n",\n'
    '		MIPITX_IsEnabled(module, NULL), DSI_ClkIsEnabled(module, NULL),\n'
    '		INREG32(&DSI_REG[0]->DSI_INTSTA), INREG32(&DSI_REG[1]->DSI_INTSTA));\n'
    '	s3pro_dsi_regs("stop");\n'
    "\n" + STOP_TAIL
)

LCM_RESUME_BLOCK = (
    '	DISPCHECK("[POWER]lcm resume[begin]\\n");\n'
    '	disp_lcm_resume(pgc->plcm);\n'
    '	DISPCHECK("[POWER]lcm resume[end]\\n");'
)
LCM_RESUME_BLOCK_NEW = (
    '	DISPCHECK("[POWER]lcm resume[begin]\\n");\n'
    '	DISPMSG("S3PRO_DBG resume: call lcm_resume, state=%d\\n", pgc->state);\n'
    '	disp_lcm_resume(pgc->plcm);\n'
    '	DISPMSG("S3PRO_DBG resume: lcm_resume returned\\n");\n'
    '	DISPCHECK("[POWER]lcm resume[end]\\n");'
)

INIT_ANCHOR = "	pregulator = dev;\n	dprec_init();"
INIT_NEW = "	pregulator = dev;\n	dprec_init();\n	s3pro_dbg_init_once();\n" \
           '	DISPMSG("S3PRO_DBG init: debug nodes ready (skip_stop / reinit_on_resume)\\n");'

SUSPEND_SKIP_ANCHOR = "	MMProfileLogEx(ddp_mmp_get_events()->primary_suspend, MMProfileFlagStart, 0, 0);"
SUSPEND_SKIP_NEW = ('''	if (s3pro_skip_stop) {
		DISPMSG("S3PRO_DBG suspend: SKIP teardown (skip_stop=1)\\n");
		return DISP_STATUS_OK;
	}
''' + SUSPEND_SKIP_ANCHOR)

PD_PATCHES = [
    ('#include "disp_drv_log.h"',
     '#include "disp_drv_log.h"\n#include <linux/debugfs.h>'),
    ('#include <linux/debugfs.h>',
     '#include <linux/debugfs.h>\n\n' + NODES_FUNC),
    (INIT_ANCHOR, INIT_NEW),
    (SUSPEND_SKIP_ANCHOR, SUSPEND_SKIP_NEW),
    ('DISPCHECK("[POWER]lcm suspend[begin]\\n");\n\tdisp_lcm_suspend(pgc->plcm);',
     'DISPCHECK("[POWER]lcm suspend[begin]\\n");\n'
     '\tDISPMSG("S3PRO_DBG suspend: call lcm_suspend, state=%d\\n", pgc->state);\n'
     '\tdisp_lcm_suspend(pgc->plcm);\n'
     '\tDISPMSG("S3PRO_DBG suspend: lcm_suspend returned\\n");'),
    ('\tpgc->state = DISP_SLEPT;',
     '\tDISPMSG("S3PRO_DBG suspend: done -> SLEPT\\n");\n\tpgc->state = DISP_SLEPT;'),
    (LCM_RESUME_BLOCK, LCM_RESUME_BLOCK_NEW),
    ('DISPCHECK("[POWER]dpmgr path start[end]\\n");',
     'DISPCHECK("[POWER]dpmgr path start[end]\\n");\n'
     '\tDISPMSG("S3PRO_DBG resume: path started, state=%d\\n", pgc->state);\n'
     '\tif (s3pro_reinit_on_resume) {\n'
     '\t\tDISPMSG("S3PRO_DBG resume: RE-PUSH table after start\\n");\n'
     '\t\tdisp_lcm_resume(pgc->plcm);\n'
     '\t\tDISPMSG("S3PRO_DBG resume: re-push done\\n");\n'
     '\t}'),
]

DSI_PATCHES = [
    (CONFIG_DEF, REGS_FUNC + CONFIG_DEF),
    (CONFIG_TAIL, CONFIG_TAIL_NEW),
    (DSI_TAIL, DSI_TAIL_NEW),
    (STOP_TAIL, STOP_TAIL_NEW),
]


def patch_file(path, patches, dry, label):
    if not os.path.exists(path):
        return ["找不到文件: %s" % path]
    t = open(path, encoding="utf-8", errors="surrogateescape").read()
    problems = []
    applied = 0
    for old, new in patches:
        if new in t:
            continue
        if "S3PRO" in new and "S3PRO" in t and old not in t:
            continue
        cnt = t.count(old)
        if cnt != 1:
            problems.append("%s: 锚点匹配 %d 次（应为 1）: %r" % (label, cnt, old[:60]))
            continue
        t = t.replace(old, new, 1)
        applied += 1
    if problems:
        return problems
    print("  %s: 新插入 %d 处" % (label, applied))
    if applied and not dry:
        open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)
    return []


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    dsi = os.path.join(tree, "drivers", "misc", "mediatek", "video", "mt8173",
                       "dispsys", "ddp_dsi.c")
    pd = os.path.join(tree, "drivers", "misc", "mediatek", "video", "mt8173",
                      "videox", "primary_display.c")

    problems = patch_file(dsi, DSI_PATCHES, dry, "ddp_dsi.c")
    problems += patch_file(pd, PD_PATCHES, dry, "primary_display.c")
    if problems:
        print("\n!! 问题：")
        for p in problems:
            print("   - " + p)
        return 1
    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    d = open(dsi, encoding="utf-8", errors="surrogateescape").read()
    p = open(pd, encoding="utf-8", errors="surrogateescape").read()
    # 节点定义必须出现在 primary_display_init 之前
    nodes_at = p.find("static void s3pro_dbg_init_once")
    init_at = p.find("int primary_display_init(")
    checks = {
        "寄存器 dump": "static void s3pro_dsi_regs" in d,
        "config 快照": 's3pro_dsi_regs("config")' in d,
        "start 快照": 's3pro_dsi_regs("start")' in d,
        "stop 快照": 's3pro_dsi_regs("stop")' in d,
        "skip_stop 节点": "s3pro_skip_stop" in p,
        "reinit 节点": "s3pro_reinit_on_resume" in p,
        "节点定义在 init 之前": 0 <= nodes_at < init_at,
        "init 里调用创建": "s3pro_dbg_init_once();" in p,
        "suspend 打印": "S3PRO_DBG suspend" in p,
        "resume 打印": "S3PRO_DBG resume" in p,
    }
    print()
    bad = 0
    for k, v in checks.items():
        print("    %-26s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1
    for f in (dsi, pd):
        c = open(f, encoding="utf-8", errors="surrogateescape").read()
        ok = c.count("{") == c.count("}")
        print("    %-26s %s" % (os.path.basename(f) + " 花括号", "OK" if ok else "失败!"))
        if not ok:
            bad += 1
    if bad:
        return 1
    print("\n显示诊断插桩 v5 完成（节点开机即建）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
