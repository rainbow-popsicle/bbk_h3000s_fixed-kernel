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

节点（普通 u32）：
  保留：s3pro_read_id / s3pro_dsi_dump / s3pro_cmd_mode_init
  已移除（实验结束，值固定为实测状态，逻辑不动）：
    s3pro_skip_stop=0 / s3pro_reinit_on_resume=0 / s3pro_lcm_suspend_early=1
    （表选择 s3pro_lcm_variant=0 在 patch_lcm_stock.py，节点也已移除）

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

/* S3PRO_CMDMODE: 临时把 DSI 切到 CMD 模式。
 *
 * 原因（实测）：video 模式下 DSI_set_cmdq_V2() 会把 DCS 命令写进
 * DSI_VM_CMD 缓冲，靠视频流的 VFP 空隙注入（每帧只能送一条）。
 * 而唤醒时初始化表是在 dsi_start 之前推的——那时视频流没跑、没有 VFP，
 * 132 条命令全部丢失，只留下 VM_CMD_START 卡在 1（实测 DSI_START=
 * 0x00010001，正常应为 0x00000001），面板永远收不到初始化序列 → 黑屏。
 *
 * 切到 CMD 模式后，命令走正常的命令队列逐条发送，不依赖视频流。
 * 推完表再切回 video 模式，然后才启动视频流。
 *
 * 运行时开关：/sys/kernel/debug/s3pro_cmd_mode_init（默认 1 开启） */
int s3pro_cmd_mode_init = 1;

void s3pro_dsi_set_mode(int cmd_mode)
{
	int i;
	unsigned int m;

	m = cmd_mode ? CMD_MODE : _dsi_context[0].dsi_params.mode;
	for (i = 0; i < 2; i++)
		DSI_OUTREGBIT(NULL, DSI_MODE_CTRL_REG,
			      DSI_REG[i]->DSI_MODE_CTRL, MODE, m);
	DISPMSG("S3PRO_DBG dsi mode -> %u (cmd=%d)\\n", m, cmd_mode);
}

'''

NODES_FUNC = '''/* S3PRO_DBG: 诊断节点（普通 u32）
 *
 * 保留三个节点：read_id / dsi_dump / cmd_mode_init。
 * 四个实验开关（skip_stop / reinit_on_resume / lcm_suspend_early /
 * lcm_variant）实验已结束，节点已移除；其值固定为最后一次实测状态，
 * 背后的修复逻辑保持不动。详见下方变量定义处注释。
 *
 * 本函数在 primary_display_init 里调用，所以开机后立即可用。
 */
/* S3PRO_CLEANUP: 实验结束的开关固定为实测值，节点已移除。
 * 保留变量是为了不改任何已验证的逻辑；如需恢复开关改本脚本即可。
 *   skip_stop=0           完整拆卸（复现/修复黑屏的真实场景）
 *   reinit_on_resume=0    不在 start 后重推表（实验证明无效）
 *   lcm_suspend_early=1   sleep 命令提前到关闭 DSI 之前 */
static u32 s3pro_skip_stop;
static u32 s3pro_reinit_on_resume;
static u32 s3pro_lcm_suspend_early = 1;
static struct dentry *s3pro_readid_dent;
static struct dentry *s3pro_dsidump_dent;
static struct dentry *s3pro_cmdmode_dent;

/* S3PRO_CMDMODE: 推表前把 DSI 切到 CMD 模式（定义在 ddp_dsi.c） */
extern int s3pro_cmd_mode_init;
extern void s3pro_dsi_set_mode(int cmd_mode);

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
	/* S3PRO_CLEANUP: 只保留诊断/修复必需的节点：
	 *   dsi_dump      手动寄存器快照
	 *   read_id       DCS 回读（面板是否活着）
	 *   cmd_mode_init 推表前切 CMD 模式（修复逻辑）
	 * skip_stop / reinit_on_resume / lcm_suspend_early / lcm_variant
	 * 四个实验开关已移除，值固定为实测状态（见上方变量定义）。 */
	if (s3pro_dsidump_dent == NULL)
		s3pro_dsidump_dent = debugfs_create_file("s3pro_dsi_dump", 0200,
							 NULL, NULL, &s3pro_dsi_dump_fops);
	if (s3pro_readid_dent == NULL)
		s3pro_readid_dent = debugfs_create_file("s3pro_read_id", 0200,
							NULL, NULL, &s3pro_read_id_fops);
	if (s3pro_cmdmode_dent == NULL)
		s3pro_cmdmode_dent = debugfs_create_u32("s3pro_cmd_mode_init", 0644,
							NULL, (u32 *)&s3pro_cmd_mode_init);
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

# ---- ddp_dsi_trigger 的寄存器快照（唯一还没观测过的关键函数）----
#   用 pr_alert 而不是 DISPMSG：DISPMSG 会被 printk 限流吞掉，
#   而这里恰恰要确认「trigger 到底有没有被调用」，不能被吞。
TRIGGER_TAIL = ("	DSI_Start(module, cmdq);\n"
                "\n"
                "	return 0;\n"
                "}\n"
                "\n"
                "int ddp_dsi_reset")
TRIGGER_TAIL_NEW = ("	DSI_Start(module, cmdq);\n"
                    "\n"
                    "	/* S3PRO_TRIGGER: 确认 ddp_dsi_trigger 被调用 + 调用后 START/INTSTA 的真实值。\n"
                    "	 * 前 5 次全打，之后每 200 次打一次，避免刷屏。 */\n"
                    "	{\n"
                    "		static unsigned int s3pro_trig_cnt;\n"
                    "		s3pro_trig_cnt++;\n"
                    "		if (s3pro_trig_cnt <= 5 || (s3pro_trig_cnt % 200) == 0)\n"
                    "			pr_alert(\"[S3PRO_TRIGGER] #%u start=%08x intsta=%08x mode=%08x trig=%08x sta=%08x\\n\",\n"
                    "				 s3pro_trig_cnt,\n"
                    "				 INREG32(&DSI_REG[0]->DSI_START),\n"
                    "				 INREG32(&DSI_REG[0]->DSI_INTSTA),\n"
                    "				 INREG32(&DSI_REG[0]->DSI_MODE_CTRL),\n"
                    "				 INREG32(&DSI_REG[0]->DSI_TRIG_STA),\n"
                    "				 INREG32(&DSI_REG[0]->DSI_STA));\n"
                    "	}\n"
                    "\n"
                    "	return 0;\n"
                    "}\n"
                    "\n"
                    "int ddp_dsi_reset")
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
    '	/* S3PRO_CMDMODE: video 模式下 DCS 命令走 VM_CMD 缓冲、需要视频流的\n'
    '	 * VFP 空隙才能注入；而此处视频流还没启动（dsi_start 在后面），\n'
    '	 * 132 条初始化命令会全部丢失。临时切 CMD 模式让命令走正常队列。 */\n'
    '	if (s3pro_cmd_mode_init)\n'
    '		s3pro_dsi_set_mode(1);\n'
    '	disp_lcm_resume(pgc->plcm);\n'
    '	if (s3pro_cmd_mode_init)\n'
    '		s3pro_dsi_set_mode(0);\n'
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
    # 节点函数必须放在所有 include 之后：
    #   1) primary_display_init（约 2800 行）要调用它 —— 不能放在它后面
    #   2) 函数体用到 struct file / file_operations，依赖 <linux/fs.h>
    #      —— 锚在 debugfs.h 上会把函数插到 fs.h 之前，类型未知导致编译失败
    ('#include <linux/fs.h>',
     '#include <linux/fs.h>\n\n' + NODES_FUNC),
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
     '\t\tif (s3pro_cmd_mode_init)\n'
     '\t\t\ts3pro_dsi_set_mode(1);\n'
     '\t\tdisp_lcm_resume(pgc->plcm);\n'
     '\t\tif (s3pro_cmd_mode_init)\n'
     '\t\t\ts3pro_dsi_set_mode(0);\n'
     '\t\tDISPMSG("S3PRO_DBG resume: re-push done\\n");\n'
     '\t}'),
]

DSI_PATCHES = [
    (CONFIG_DEF, REGS_FUNC + CONFIG_DEF),
    (CONFIG_TAIL, CONFIG_TAIL_NEW),
    (DSI_TAIL, DSI_TAIL_NEW),
    (STOP_TAIL, STOP_TAIL_NEW),
    (TRIGGER_TAIL, TRIGGER_TAIL_NEW),
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
        "寄存器 dump": "void s3pro_dsi_regs(const char *tag)" in d,
        "config 快照": 's3pro_dsi_regs("config")' in d,
        "start 快照": 's3pro_dsi_regs("start")' in d,
        "stop 快照": 's3pro_dsi_regs("stop")' in d,
        "trigger 快照": "[S3PRO_TRIGGER]" in d,
        "skip_stop 实验节点已移除": 'debugfs_create_u32("s3pro_skip_stop"' not in p,
        "reinit 实验节点已移除": 'debugfs_create_u32("s3pro_reinit_on_resume"' not in p,
        "early 实验节点已移除": 'debugfs_create_u32("s3pro_lcm_suspend_early"' not in p,
        "variant 实验节点已移除": 'debugfs_create_u32("s3pro_lcm_variant"' not in p,
        "read_id 节点保留": "s3pro_read_id" in p,
        "dsi_dump 节点保留": "s3pro_dsi_dump" in p,
        "cmd_mode 节点保留": "s3pro_cmd_mode_init" in p,
        "CMD 模式切换": "s3pro_dsi_set_mode(1)" in p,
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
