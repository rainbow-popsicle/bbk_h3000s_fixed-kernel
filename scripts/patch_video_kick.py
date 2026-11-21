#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S3PRO_VIDEO_KICK：黑屏态手动强制触发视频流，验证 CMDQ trigger loop 死锁假设。

=== 背景（实测证据） ===
黑屏态：DSI_START=1 / MODE=3(BURST_VDO) / 面板回读 0x9C 正常 / 背光亮，
但 DSI_FRM_BC 帧计数器恒为 0（连续采样 3 次不增长），
present_fence 持续刷 "wait VSYNC timeout on primary_rdma0_color_disp"。
→ 面板活着、命令通道通，但视频帧根本没发出去。

源码定位：video 模式 CMDQ trigger loop 首条指令是
  _cmdq_build_trigger_loop() : cmdqRecWait(..., CMDQ_EVENT_MUTEX0_STREAM_EOF)
而 _cmdq_start_trigger_loop() 里设置 STREAM_EOF 的分支被 #if 0 屏蔽。
若首帧没送出 → MUTEX0_STREAM_EOF 永不 assert → loop 卡死 → 后续无 trigger。

=== 本补丁做什么 ===
新增一个 debugfs 节点 s3pro_video_kick，手动执行三种诊断动作：
  echo 1 > 节点  force trigger 一帧（照抄 resume 的 dpmgr_path_trigger(NULL)，CPU 直写、非阻塞）
                 → 若屏幕亮/frmbc 涨，证明首帧能送出，问题在 loop 没 kick
  echo 2 > 节点  stop loop → force trigger → start loop（照抄 ESD recovery 序列）
  echo 3 > 节点  只打印状态（不动作）
全部用 pr_alert 输出，不受 printk 级别过滤。只加诊断节点，不改任何显示逻辑/时序。

=== 插入位置（关键） ===
必须插在 _cmdq_stop_trigger_loop() 定义之后（约 1405 行）：
  - pgc 是宏(#define pgc _get_context())，182 行才有，早插编译失败
  - _cmdq_start/stop_trigger_loop 是 static，1395/1363 行，需在其后
  - CMDQ_DISABLE(ddp_hal.h) / debugfs.h / ddp_reg.h 均已在顶部 include
锚点用紧随其后的 _cmdq_set_config_handle_dirty()，唯一。

注意：DSI_REG 只在 ddp_dsi.c 可见，本补丁在 primary_display.c 里不能直接解引用它。
寄存器快照（含 frmbc）改为复用 disp_debug 提供的 s3pro_dsi_regs(tag)，以
"S3PRO_REG <tag> dsi0 D ... frmbc=..." 输出。之前直接写 DSI_REG 导致编译失败
（'DSI_REG' undeclared），已修正。

用法：
    python patch_video_kick.py <内核树根> [--dry-run]
开关：VIDEO_KICK=1 时由 build.sh 调用；依赖 DISP_DEBUG=1。
"""
import os
import sys

MARK = "S3PRO_VIDEO_KICK"

# 锚点：_cmdq_stop_trigger_loop 之后的第一个函数。在其定义前插入整块。
ANCHOR = "static void _cmdq_set_config_handle_dirty(void)"

BLOCK = r'''/* S3PRO_VIDEO_KICK: 手动强制触发视频流，验证 CMDQ trigger loop 死锁。
 * 写节点：1=force trigger 一帧  2=stop+trigger+start loop  3=只打印状态。
 * 只加诊断节点，不改任何显示逻辑/时序。用 pr_alert 无条件输出。 */
static struct dentry *s3pro_vk_dent;

/* DSI 寄存器快照函数定义在 ddp_dsi.c（DSI_REG 只在该文件可见）。
 * 这里不能直接在 primary_display.c 解引用 DSI_REG —— 会编译失败。
 * 重复 extern 声明合法，保证本补丁自包含（disp_debug 已声明，这里再声明一次无妨）。 */
extern void s3pro_dsi_regs(const char *tag);

static void s3pro_vk_dump_state(const char *tag)
{
	pr_alert("[S3PRO_VK] %s state=%d video=%d idle=%d\n",
		 tag, pgc->state, primary_display_is_video_mode(),
		 dpmgr_path_is_idle(pgc->dpmgr_handle));
	/* start/mode/frmbc/sta/trig 等寄存器由 s3pro_dsi_regs() 以
	 * "S3PRO_REG <tag> dsi0 D sta=... frmbc=..." 打印，手动触发可抓到。 */
	s3pro_dsi_regs(tag);
}

static ssize_t s3pro_video_kick_write(struct file *file, const char __user *buf,
				      size_t count, loff_t *ppos)
{
	unsigned int cmd = 0;
	char cbuf[16] = {0};
	unsigned int clen = count < sizeof(cbuf) - 1 ? count : sizeof(cbuf) - 1;

	/* 正确解析 echo "1"/"2"/"3"（ASCII + 换行），不能把 buf 当整数读：
	 * 直接 copy_from_user(&cmd,buf,4) 会把 "3\n" 读成 0x00000a33=2603，
	 * 导致永远 unknown cmd。 */
	if (copy_from_user(cbuf, buf, clen))
		return -EFAULT;
	if (kstrtouint(cbuf, 10, &cmd))
		cmd = 0;

	s3pro_vk_dump_state("before");
	pr_alert("[S3PRO_VK] cmd=%u\n", cmd);

	_primary_path_lock(__func__);
	switch (cmd) {
	case 1:
		/* 照抄 primary_display_resume:3449，CPU 直写、非阻塞。
		 * 先送一帧产生 MUTEX0_STREAM_EOF，可能自行解开卡死的 loop。 */
		pr_alert("[S3PRO_VK] force trigger (NULL, CMDQ_DISABLE)\n");
		dpmgr_path_trigger(pgc->dpmgr_handle, NULL, CMDQ_DISABLE);
		break;
	case 2:
		/* 照抄 ESD recovery 2079-2128：stop → force trigger → start */
		pr_alert("[S3PRO_VK] stop loop\n");
		_cmdq_stop_trigger_loop();
		pr_alert("[S3PRO_VK] force trigger\n");
		dpmgr_path_trigger(pgc->dpmgr_handle, NULL, CMDQ_DISABLE);
		pr_alert("[S3PRO_VK] start loop\n");
		_cmdq_start_trigger_loop();
		break;
	case 3:
		pr_alert("[S3PRO_VK] state-only (no action)\n");
		break;
	default:
		pr_alert("[S3PRO_VK] unknown cmd=%u (use 1/2/3)\n", cmd);
		break;
	}
	_primary_path_unlock(__func__);

	s3pro_vk_dump_state("after");
	return count;
}

static const struct file_operations s3pro_video_kick_fops = {
	.owner = THIS_MODULE,
	.write = s3pro_video_kick_write,
};

static void s3pro_video_kick_init_node(void)
{
	if (s3pro_vk_dent == NULL)
		s3pro_vk_dent = debugfs_create_file("s3pro_video_kick", 0200,
						    NULL, NULL, &s3pro_video_kick_fops);
}

'''


def find_pd(tree):
    """按固定路径定位 primary_display.c（DISP_DEBUG 补丁依赖同一文件）。"""
    p = os.path.join(tree, "drivers", "misc", "mediatek", "video", "mt8173",
                     "videox", "primary_display.c")
    return p if os.path.exists(p) else None


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    p = find_pd(tree)
    if not p:
        raise SystemExit("找不到 primary_display.c")
    print("  目标: %s" % os.path.relpath(p, tree))

    t = open(p, encoding="utf-8", errors="surrogateescape").read()
    if MARK in t:
        print("  已打过补丁，跳过")
        return 0

    cnt = t.count(ANCHOR)
    if cnt != 1:
        print("!! 锚点匹配 %d 次（应为 1）: %r" % (cnt, ANCHOR))
        return 1

    t = t.replace(ANCHOR, BLOCK + ANCHOR, 1)
    print("  已在 _cmdq_stop_trigger_loop 之后插入 s3pro_video_kick 节点")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(p, encoding="utf-8", errors="surrogateescape").read()
    anchor_at = c.find(ANCHOR)
    block_at = c.find(MARK)
    checks = {
        "标记存在": MARK in c,
        "节点函数已定义": "s3pro_video_kick_write" in c,
        "节点创建函数": "debugfs_create_file(\"s3pro_video_kick\"" in c,
        "锚点唯一": c.count(ANCHOR) == 1,
        "插入在锚点之前": 0 <= block_at < anchor_at,
        "force trigger 调用": "dpmgr_path_trigger(pgc->dpmgr_handle, NULL, CMDQ_DISABLE)" in c,
        "stop loop 调用": "_cmdq_stop_trigger_loop();" in c,
        "start loop 调用": "_cmdq_start_trigger_loop();" in c,
        "pr_alert 输出": c.count('pr_alert("[S3PRO_VK]') >= 6,
        "复用 s3pro_dsi_regs 读寄存器(含frmbc)": "s3pro_dsi_regs(tag);" in c,
        "未直接解引用 DSI_REG(避免编译失败)": "&DSI_REG[" not in BLOCK and "DSI_REG[0]" not in BLOCK,
        "持锁保护": "_primary_path_lock(__func__);" in c and "_primary_path_unlock(__func__);" in c,
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

    # 节点注册：在 s3pro_dbg_init_once() 函数体开头插入调用。
    # 该函数定义在 78 行区域（早于 pgc），但这里只调函数、不碰 pgc，
    # 无可见性问题；函数实体在 1405 行后，链接期可见。
    fn_head = "static void s3pro_dbg_init_once(void)\n{"
    if fn_head not in c:
        print("\n  [warn] 找不到 s3pro_dbg_init_once() 定义，节点未注册！")
        return 1
    if "s3pro_video_kick_init_node();" not in c:
        c = c.replace(
            "static void s3pro_dbg_init_once(void)\n{",
            "static void s3pro_video_kick_init_node(void);\n\n"
            "static void s3pro_dbg_init_once(void)\n{\n"
            "\ts3pro_video_kick_init_node();",
            1)
        open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(c)
        print("  已在 s3pro_dbg_init_once() 开头挂接节点注册")

    # 复核挂接结果
    c = open(p, encoding="utf-8", errors="surrogateescape").read()
    ok_reg = ("static void s3pro_video_kick_init_node(void);" in c
              and "\ts3pro_video_kick_init_node();" in c)
    print("    节点注册挂接: %s" % ("OK" if ok_reg else "失败!"))
    print("\nvideo_kick 诊断补丁完成（只加节点，不改显示逻辑）")
    return 0 if ok_reg else 1


if __name__ == "__main__":
    sys.exit(main())
