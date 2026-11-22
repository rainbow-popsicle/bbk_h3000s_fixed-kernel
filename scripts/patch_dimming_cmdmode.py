#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""根因修复：让 dimming(0x53/0x2C) 在 CMD mode 下推表，绕开卡死的 VM_CMD 通道。

=== 根因（2026-11-22 完整定位） ===
唤醒后 lcm_suspend 里 schedule 的 open_lcm_dimming 延时工作(500ms)触发：
  lcm_dimming_enable_delayed_work -> primary_display_set_panel_param(0x500)
  -> lcm_set_disp_param -> set_panel_disp_param -> case 0x500
  -> push_table({0x53,1,{0x2C}})  [dimmingon, panel_disp_param.c:48]
此时 DSI 已切 video mode(MODE=3)，push_table 走 DSI_set_cmdq_V2 的 VM_CMD 分支，
命令写进 VM_CMD_CON staging(0x2c531521)，置 VM_CMD_START 后等 VM_CMD_DONE——
但 video mode 下该 VM_CMD 注入卡死：START=0x00010001 悬挂、DONE 不到、BUSY=1、
命令残留在 staging 不清。面板 0x53(亮度控制)寄存器没被写入 -> 背光亮(背光 PWM 独立)
但图像不显示。

正常开机无此问题：开机时这条命令在 CMD mode 下推，走命令队列正常发出。

=== 修复 ===
在 lcm_set_disp_param 里，把 set_panel_disp_param 的推表包在
s3pro_dsi_set_mode(1)[切 CMD] ... s3pro_dsi_set_mode(0)[切回原 mode] 之间。
s3pro_dsi_set_mode(0) 内部恢复 _dsi_context[0].dsi_params.mode（原 video mode），
天然安全，不破坏正常显示。与 read_id 探针同一手法，已实测可用。

s3pro_dsi_set_mode 由 patch_disp_debug.py 提供（DISP_DEBUG 默认=1）。
若 DISP_DEBUG=0，本补丁注入的 extern 会链接失败，故 build.sh 需保证二者一致。

用法：
    python patch_dimming_cmdmode.py <内核树根> [--dry-run]
开关：DIMMING_CMDMODE=1 时由 build.sh 调用（默认 1）。
"""
import os
import sys

MARK = "S3PRO_DIMMING_FIX"

# 目标函数：lcm_set_disp_param 的函数体（跨补丁稳定，read_id 等补丁不碰它）
OLD_BODY = '''static void lcm_set_disp_param(unsigned int param)
{
	int ret;
	LCM_LOGI("lcm_set_disp_paramt: param = %d\\n", param);

	ret = set_panel_disp_param(param, push_table);
	return;
}'''

NEW_BODY = '''static void lcm_set_disp_param(unsigned int param)
{
	int ret;
	LCM_LOGI("lcm_set_disp_paramt: param = %d\\n", param);

	/* S3PRO_DIMMING_FIX: dimming(0x53/0x2C) 若在 video mode 推表会卡进
	 * VM_CMD staging(VM_CMD_START 悬挂/DONE 不到)，面板收不到亮度控制命令
	 * -> 唤醒后背光亮但无图像。临时切 CMD mode 走命令队列推表，推完切回。
	 * s3pro_dsi_set_mode(0) 恢复到原 mode，不影响正常显示。 */
	pr_alert("[S3PRO_DIM] param=0x%x switch to CMD mode\\n", param);
	s3pro_dsi_set_mode(1);
	ret = set_panel_disp_param(param, push_table);
	s3pro_dsi_set_mode(0);
	pr_alert("[S3PRO_DIM] back to video mode, ret=%d\\n", ret);
	return;
}'''

# extern 声明插在 typedef send_cmd 之前
EXTERN_ANCHOR = "typedef void (*send_cmd)("
EXTERN_LINE = "extern void s3pro_dsi_set_mode(int cmd_mode);  /* S3PRO_DIMMING_FIX */\n"


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
    cands = []
    if name:
        d = os.path.join(base, name)
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith(".c"):
                    cands.append(os.path.join(d, f))
    if not cands:
        d = os.path.join(base, "nt35523_wqxga_dsi_vdo_sharp")
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith(".c"):
                    cands.append(os.path.join(d, f))
    if not cands:
        for root, _d, files in os.walk(base):
            if "nt35523" not in root:
                continue
            for f in files:
                if f.endswith(".c"):
                    cands.append(os.path.join(root, f))
    # 必须含 lcm_set_disp_param 和 set_panel_disp_param 的那个文件
    for p in cands:
        t = open(p, encoding="utf-8", errors="replace").read()
        if "lcm_set_disp_param" in t and "set_panel_disp_param" in t:
            return p
    return None


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    p = find_driver(tree)
    if not p:
        raise SystemExit("找不到含 lcm_set_disp_param 的面板驱动文件")
    print("  驱动: %s" % os.path.relpath(p, tree))

    t = open(p, encoding="utf-8", errors="surrogateescape").read()
    if MARK in t:
        print("  已打过补丁，跳过")
        return 0

    # 校验锚点唯一性，防止误改
    cnt_body = t.count(OLD_BODY)
    if cnt_body != 1:
        print("!! lcm_set_disp_param 函数体匹配 %d 次（应为 1），中止" % cnt_body)
        return 1
    cnt_ext = t.count(EXTERN_ANCHOR)
    if cnt_ext < 1:
        print("!! typedef send_cmd 锚点匹配 %d 次（应 >=1），中止" % cnt_ext)
        return 1

    # 若 read_id 补丁已加过同名 extern，不重复插入
    if EXTERN_LINE.strip() in t or "extern void s3pro_dsi_set_mode(int cmd_mode);" in t:
        added_ext = False
    else:
        t = t.replace(EXTERN_ANCHOR, EXTERN_LINE + EXTERN_ANCHOR, 1)
        added_ext = True

    t = t.replace(OLD_BODY, NEW_BODY, 1)
    print("  已改 lcm_set_disp_param：推表包在切CMD/切回之间%s" %
          ("（含 extern 声明）" if added_ext else "（复用已存在的 extern）"))

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(p, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "标记存在": MARK in c,
        "原函数体已替换": OLD_BODY not in c,
        "切CMD模式": "s3pro_dsi_set_mode(1);" in c,
        "切回video": "s3pro_dsi_set_mode(0);" in c,
        "推表在两者之间": c.find("s3pro_dsi_set_mode(1);") < c.find("set_panel_disp_param(param, push_table);") < c.find("s3pro_dsi_set_mode(0);"),
        "extern声明": "extern void s3pro_dsi_set_mode(int cmd_mode);" in c,
        "pr_alert日志": c.count('pr_alert("[S3PRO_DIM]') >= 2,
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
    print("\ndimming CMD-mode 修复完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
