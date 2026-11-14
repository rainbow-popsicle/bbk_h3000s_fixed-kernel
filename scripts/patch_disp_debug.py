#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""显示修复 v6：把面板 sleep 命令提前到关闭 DSI 之前 + 诊断 + 运行时开关。

=== 实机证据链（这台设备实测） ===
 1. 开机画面正常：面板由 bootloader(LK) 初始化并点亮，内核开机路径不调用
    ddp_dsi_start（实测 dsi_start=0 次）。
 2. 第一次熄屏后永久黑屏（只有背光）。
 3. 唤醒后寄存器逐字段对比，DSI 通路全部正常：
       mipitx=2 dsiclk=2，DSI 不 busy，GPIO(985/987/1016) 全高，
       COM_CTRL/MODE_CTRL/PHY_LCCON/全部 timing 一致。
    唯一差异 DSI_START 已由 patch_dsi_start.py 修复（修复后实测 START=1）。
 4. 初始化表已换成 LK 的完整 132 项（含 0xFF AA55A580 解锁），
    并在视频流启动后重推过一次 —— 仍然黑屏。
 5. 供电轨与工作状态一致（只有 DVFS 电压不同，均 enabled）。
 6. ★ 决定性实验：开机后立刻写 skip_stop=1（完全不拆显示管线，只关背光），
    熄屏→唤醒后 **屏幕正常显示** → 面板本身没问题，
    问题 100% 出在 "拆卸 → 重建" 这条链路。

=== 由此定位到的具体错误：顺序 ===
  primary_display_suspend() 的原始顺序：
      3256  _cmdq_stop_trigger_loop();      停 CMDQ
      3261  dpmgr_path_stop();              停 DSI ← 数据通路没了
      3279  disp_lcm_suspend();             才发 0x28/0x10 ← 命令被丢弃
      3290  dpmgr_path_power_off();         断电
  → 面板的"进入休眠"命令在 DSI 停止之后才发，根本没有通路，
    于是面板以**完全清醒**的状态被硬断电，内部电源时序状态机没复位，
    之后再上电 + 推表也起不来。

=== 本补丁做的事 ===
 把 disp_lcm_suspend() 提前到 _cmdq_stop_trigger_loop() 之前执行，
 让面板先干净地进入 sleep，再停 DSI、再断电。
 原位置改为条件跳过（可用 debugfs 关掉以回归旧行为）。

节点（普通 u32，默认值见注释）：
  s3pro_skip_stop           默认 0；写 1 → 熄屏完全不拆管线（仅诊断用）
  s3pro_reinit_on_resume    默认 0；写 1 → 唤醒后 start 之后再推一次表
  s3pro_lcm_suspend_early   默认 1；写 0 → 回到"先停 DSI 再发 sleep"的旧顺序

用法：
    python patch_disp_debug.py <内核树根> [--dry-run]
"""
import os
import sys

MARK = "S3PRO_DBG"

REGS_FUNC = '''/* S3PRO_DBG: DSI 寄存器快照，用于对比"能显示"与"黑屏"两种状态 */
void s3pro_dsi_regs(const char *tag)
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
		DISPMSG("S3PRO_REG %s dsi%d D sta=%08x trig=%08x frmbc=%08x dbg2=%08x hs_tx=%u ctl=%u\\n",
			tag, i,
			INREG32(&DSI_REG[i]->DSI_STA), INREG32(&DSI_REG[i]->DSI_TRIG_STA),
			INREG32(&DSI_REG[i]->DSI_FRM_BC), INREG32(&DSI_REG[i]->DSI_STATE_DBG2),
			(unsigned int)DSI_REG[i]->DSI_STATE_DBG0.DPHY_HS_TX_STATE_C,
			(unsigned int)DSI_REG[i]->DSI_STATE_DBG0.DPHY_CTL_STATE_C);
	}
}

'''

NODES_FUNC = '''/* S3PRO_DBG: 运行时开关（普通 u32）
 *
 * s3pro_skip_stop          默认 0：写 1 → 熄屏完全不拆显示管线（仅诊断用）
 * s3pro_reinit_on_resume   默认 0：写 1 → 唤醒后 start 之后再推一次初始化表
 * s3pro_lcm_suspend_early  默认 1：面板 sleep 命令是否提前到关 DSI 之前
 *                                 写 0 → 回到旧顺序（先停 DSI 再发 sleep）
 *
 * 本函数在 primary_display_init 里调用，所以开机后立即可用。
 */
static u32 s3pro_skip_stop;
static u32 s3pro_reinit_on_resume;
static u32 s3pro_lcm_suspend_early = 1;
static struct dentry *s3pro_skip_dent;
static struct dentry *s3pro_reinit_dent;
static struct dentry *s3pro_early_dent;
static struct dentry *s3pro_readid_dent;
static struct dentry *s3pro_dsidump_dent;

/* S3PRO_DBG: 手动触发面板 DCS 回读（用于"面板健康时"的对照测量）
 * 用法：echo 1 > /sys/kernel/debug/s3pro_read_id */
extern void mtk_lcm_s3pro_readback(void) __attribute__((weak));

extern void s3pro_dsi_regs(const char *tag);

static ssize_t s3pro_dsi_dump_write(struct file *file, const char __user *buf,
				    size_t count, loff_t *ppos)
{
	DISPMSG("S3PRO_DBG dsi_dump: manual register snapshot\\n");
	s3pro_dsi_regs("manual");
	return count;
}

static const struct file_operations s3pro_dsi_dump_fops = {
	.owner = THIS_MODULE,
	.write = s3pro_dsi_dump_write,
};

static ssize_t s3pro_read_id_write(struct file *file, const char __user *buf,
				   size_t count, loff_t *ppos)
{
	DISPMSG("S3PRO_DBG read_id: manual DCS readback\\n");
	if (mtk_lcm_s3pro_readback)
		mtk_lcm_s3pro_readback();
	else
		DISPMSG("S3PRO_DBG read_id: readback symbol not linked\\n");
	return count;
}

static const struct file_operations s3pro_read_id_fops = {
	.owner = THIS_MODULE,
	.write = s3pro_read_id_write,
};

static void s3pro_dbg_init_once(void)
{
	if (s3pro_skip_dent == NULL)
		s3pro_skip_dent = debugfs_create_u32("s3pro_skip_stop", 0644,
						     NULL, &s3pro_skip_stop);
	if (s3pro_reinit_dent == NULL)
		s3pro_reinit_dent = debugfs_create_u32("s3pro_reinit_on_resume", 0644,
						       NULL, &s3pro_reinit_on_resume);
	if (s3pro_early_dent == NULL)
		s3pro_early_dent = debugfs_create_u32("s3pro_lcm_suspend_early", 0644,
						      NULL, &s3pro_lcm_suspend_early);
	if (s3pro_dsidump_dent == NULL)
		s3pro_dsidump_dent = debugfs_create_file("s3pro_dsi_dump", 0200,
							 NULL, NULL, &s3pro_dsi_dump_fops);
	if (s3pro_readid_dent == NULL)
		s3pro_readid_dent = debugfs_create_file("s3pro_read_id", 0200,
							NULL, NULL, &s3pro_read_id_fops);
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

# ---- 核心修复 1：把 sleep 命令提前到关 DSI 之前 ----
EARLY_ANCHOR = (
    '	DISPCHECK("[POWER]display cmdq trigger loop stop[begin]\\n");\n'
    '	_cmdq_stop_trigger_loop();'
)
EARLY_NEW = (
    '	DISPCHECK("[POWER]display cmdq trigger loop stop[begin]\\n");\n'
    '	/* S3PRO_LCM_EARLY_SUSPEND: 面板的 display-off(0x28)/sleep-in(0x10)\n'
    '	 * 必须在关闭 DSI 之前发出去。原代码把 disp_lcm_suspend 放在\n'
    '	 * dpmgr_path_stop 之后，命令没有数据通路、被面板丢弃，面板会以\n'
    '	 * "完全清醒"状态被断电，之后重新上电初始化不起来（实测永久黑屏）。\n'
    '	 * 出问题时 echo 0 > /sys/kernel/debug/s3pro_lcm_suspend_early 可回退。 */\n'
    '	if (s3pro_lcm_suspend_early) {\n'
    '		DISPMSG("S3PRO_DBG suspend: lcm_suspend BEFORE dsi stop\\n");\n'
    '		disp_lcm_suspend(pgc->plcm);\n'
    '		DISPMSG("S3PRO_DBG suspend: early lcm_suspend done\\n");\n'
    '	}\n'
    '	_cmdq_stop_trigger_loop();'
)

# ---- 原位置的 lcm_suspend 改为条件执行 ----
LCM_SUSPEND_BLOCK = (
    '	DISPCHECK("[POWER]lcm suspend[begin]\\n");\n'
    '	disp_lcm_suspend(pgc->plcm);\n'
    '	DISPCHECK("[POWER]lcm suspend[end]\\n");'
)
LCM_SUSPEND_BLOCK_NEW = (
    '	DISPCHECK("[POWER]lcm suspend[begin]\\n");\n'
    '	if (!s3pro_lcm_suspend_early) {\n'
    '		DISPMSG("S3PRO_DBG suspend: call lcm_suspend here, state=%d\\n", pgc->state);\n'
    '		disp_lcm_suspend(pgc->plcm);\n'
    '		DISPMSG("S3PRO_DBG suspend: lcm_suspend returned\\n");\n'
    '	} else {\n'
    '		DISPMSG("S3PRO_DBG suspend: skipped here (already done before dsi stop)\\n");\n'
    '	}\n'
    '	DISPCHECK("[POWER]lcm suspend[end]\\n");'
)

INIT_ANCHOR = "	pregulator = dev;\n	dprec_init();"
INIT_NEW = ("	pregulator = dev;\n	dprec_init();\n	s3pro_dbg_init_once();\n"
            '	DISPMSG("S3PRO_DBG init: debug nodes ready\\n");')

SUSPEND_SKIP_ANCHOR = "	MMProfileLogEx(ddp_mmp_get_events()->primary_suspend, MMProfileFlagStart, 0, 0);"
SUSPEND_SKIP_NEW = ('''	if (s3pro_skip_stop) {
		DISPMSG("S3PRO_DBG suspend: SKIP teardown (skip_stop=1)\\n");
		return DISP_STATUS_OK;
	}
''' + SUSPEND_SKIP_ANCHOR)

SUSPEND_ANCHOR = "int primary_display_suspend(void)\n{\n"
SUSPEND_NEW = NODES_FUNC + SUSPEND_ANCHOR

PD_PATCHES = [
    ('#include "disp_drv_log.h"',
     '#include "disp_drv_log.h"\n#include <linux/debugfs.h>\n#include <linux/fs.h>'),
    # 节点函数必须放在文件靠前位置：primary_display_init 在第 2801 行就要调用它，
    # 若插在 primary_display_suspend（3184 行）之前会变成"先用后定义"。
    ('#include <linux/debugfs.h>',
     '#include <linux/debugfs.h>\n\n' + NODES_FUNC),
    (INIT_ANCHOR, INIT_NEW),
    (SUSPEND_SKIP_ANCHOR, SUSPEND_SKIP_NEW),
    (EARLY_ANCHOR, EARLY_NEW),
    (LCM_SUSPEND_BLOCK, LCM_SUSPEND_BLOCK_NEW),
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
    nodes_at = p.find("static void s3pro_dbg_init_once")
    init_at = p.find("int primary_display_init(")
    early_at = p.find("S3PRO_LCM_EARLY_SUSPEND")
    stop_at = p.find("_cmdq_stop_trigger_loop();", early_at if early_at > 0 else 0)
    checks = {
        "寄存器 dump": "static void s3pro_dsi_regs" in d,
        "config 快照": 's3pro_dsi_regs("config")' in d,
        "start 快照": 's3pro_dsi_regs("start")' in d,
        "stop 快照": 's3pro_dsi_regs("stop")' in d,
        "skip_stop 节点": "s3pro_skip_stop" in p,
        "reinit 节点": "s3pro_reinit_on_resume" in p,
        "early_suspend 节点": "s3pro_lcm_suspend_early" in p,
        "read_id 节点": "s3pro_read_id" in p,
        "dsi_dump 节点": "s3pro_dsi_dump" in p,
        "节点定义在 init 之前": 0 <= nodes_at < init_at,
        "sleep 提前到关 DSI 之前": 0 <= early_at < stop_at,
        "原位置改为条件": "if (!s3pro_lcm_suspend_early)" in p,
        "suspend 打印": "S3PRO_DBG suspend" in p,
        "resume 打印": "S3PRO_DBG resume" in p,
    }
    print()
    bad = 0
    for k, v in checks.items():
        print("    %-28s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1
    for f in (dsi, pd):
        c = open(f, encoding="utf-8", errors="surrogateescape").read()
        ok = c.count("{") == c.count("}")
        print("    %-28s %s" % (os.path.basename(f) + " 花括号", "OK" if ok else "失败!"))
        if not ok:
            bad += 1
    if bad:
        return 1
    print("\n显示修复 v6 完成（susp sleep 提前 + 诊断 + 3 个运行时开关）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
