#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CMDQ-safe 根因修复：把 dimming(0x53/0x2C) 前移到 lcm_resume 内同步推表。

=== 为什么改（2026-11-22 实测证据） ===
原实现：lcm_resume 末尾 schedule_delayed_work(open_lcm_dimming, 500ms)。
唤醒 500ms 后 work 触发 primary_display_set_panel_param(0x500)
→ lcm_set_disp_param → set_panel_disp_param(param, push_table)
→ case 0x500 push_table({0x53,1,{0x2C}})（panel_disp_param.c:48）。
此时 dpmgr_path_start 已把 DSI 切到 video mode(MODE=3)，push_table 走
DSI_set_cmdq_V2 的 VM_CMD 分支，命令卡进 VM_CMD staging：
  START=0x00010001、vmcmd dsi0=0x2c531521、TRIG=0、VM_CMD_DONE 不到、BUSY=1
面板 0x53 未写入 → 背光亮（PWM 独立）但物理纯黑。用户肉眼确认。

=== 为什么不用 patch_dimming_cmdmode.py 的方案 ===
该补丁用 s3pro_dsi_set_mode(1)/(0) 切 CMD mode，内部是
DSI_OUTREGBIT(NULL, ...) 的 **CPU 裸写 MODE**，绕过 CMDQ 破坏同步：
实测引入 78+ 次 CMDQ SW timeout（等 WDMA0_SOF / MUTEX0_STREAM_EOF），
且回退后风暴消失 → 已证伪。原厂切 mode 走 CMDQ，不是 CPU 裸写。

=== 本方案（最小、CMDQ-safe） ===
MODE 时序（行号证据）：
  primary_display.c:3414 disp_lcm_resume → lcm_resume（此时 MODE=0 CMD）
  → :3428 dpmgr_path_start → ddp_dsi.c:2880/2885 DSI_SetMode(mode=3)
即 lcm_resume 内推表时仍是 CMD mode，走 DSI_Start + DSI_WaitForNotBusy
正常命令队列（与 23/109 项初始化表同路径），根本不进 VM_CMD staging。

做法：把 lcm_resume 末尾那句 schedule_delayed_work 替换为
  cancel_delayed_work_sync(&open_lcm_dimming);  // 兜底，防旧 work 残留
  块级 extern + set_panel_disp_param(0x500, push_table)  // 同步推，CMD mode
  pr_alert 验证日志
不切 MODE、不碰 CMDQ、不改初始化表、不删 dimming 功能。

=== 声明顺序坑 ===
stock(CI) 形态中 `typedef send_cmd` / `extern set_panel_disp_param` 在
lcm_resume **之后**（patch_lcm_stock.py:116-117）；legacy 形态同理(:423-424)。
直接在 lcm_resume 里调用会 implicit declaration → -Werror 编译失败。
故在替换块内用 **块级 extern + 完整函数指针类型**（与 send_cmd 兼容）声明，
再调用；与文件后面那句 extern 重复声明合法。

=== 互斥 ===
与 patch_dimming_cmdmode.py（S3PRO_DIMMING_FIX）互斥：两者都打会叠加
CPU 裸写。检测到旧标记即中止。build.sh 用 DIMMING_EARLY / DIMMING_CMDMODE 保证。

用法：
    python patch_dimming_early.py <内核树根> [--dry-run]
开关：DIMMING_EARLY=1 时由 build.sh 调用（默认 1）。
"""
import os
import sys

MARK = "S3PRO_DIM_EARLY"
OLD_MARK = "S3PRO_DIMMING_FIX"  # 已证伪的旧方案标记，存在则中止

# stock(CI) 与 legacy 两种形态的这行完全相同（patch_lcm_stock.py:101 / 本地树:418）
OLD_LINE = "\tschedule_delayed_work(&open_lcm_dimming, msecs_to_jiffies(500));\n"

NEW_BLOCK = """\t/* S3PRO_DIM_EARLY: 在 CMD mode 内同步推 {0x53,1,{0x2C}}，取代 500ms
\t * delayed work。原 work 在 video mode 推表会卡 VM_CMD staging
\t * (vmcmd=0x2c531521, START=0x00010001) → 面板亮度寄存器没写入 → 黑屏。 */
\tcancel_delayed_work_sync(&open_lcm_dimming);
\t{
\t\t/* 声明在文件后面（send_cmd typedef 也在后面），故块级 extern
\t\t * 用完整函数指针类型，与 send_cmd 兼容。 */
\t\textern int set_panel_disp_param(unsigned int param,
\t\t\tvoid (*p_func)(struct LCM_setting_table *table, unsigned int count, unsigned char force_update));
\t\tint __dim_ret;
\t\tpr_alert("[S3PRO_DIM_EARLY] sync dimming push in lcm_resume (CMD mode)\\n");
\t\t__dim_ret = set_panel_disp_param(0x500, push_table);
\t\tpr_alert("[S3PRO_DIM_EARLY] set_panel_disp_param ret=%d\\n", __dim_ret);
\t}
"""


def find_driver(tree):
    """按 .config 的 CONFIG_CUSTOM_KERNEL_LCM精确定位实际编译的驱动。"""
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
    for p in cands:
        t = open(p, encoding="utf-8", errors="replace").read()
        if "lcm_resume" in t and "open_lcm_dimming" in t:
            return p
    return None


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    p = find_driver(tree)
    if not p:
        raise SystemExit("找不到含 lcm_resume/open_lcm_dimming 的面板驱动文件")
    print("  驱动: %s" % os.path.relpath(p, tree))

    t = open(p, encoding="utf-8", errors="surrogateescape").read()
    if MARK in t:
        print("  已打过补丁，跳过")
        return 0
    if OLD_MARK in t:
        print("!! 检测到已证伪的 %s（CPU 裸写 MODE 旧方案），中止。" % OLD_MARK)
        print("   请关闭 dimming_cmdmode 并从干净树重新构建，避免两方案叠加。")
        return 1

    cnt = t.count(OLD_LINE)
    if cnt != 1:
        print("!! schedule_delayed_work 锚点匹配 %d 次（应为 1），中止" % cnt)
        i = t.find("open_lcm_dimming")
        if i >= 0:
            print("   附近内容：\n" + t[max(0, i - 200):i + 300])
        return 1

    t = t.replace(OLD_LINE, NEW_BLOCK, 1)
    print("  已把 dimming 前移到 lcm_resume 同步推表（CMD mode）")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(p, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "标记存在": MARK in c,
        "旧 schedule 行已移除": OLD_LINE not in c,
        "旧 CPU 裸写方案未叠加": OLD_MARK not in c,
        "块级 extern 声明": "extern int set_panel_disp_param(unsigned int param," in c,
        "完整函数指针类型": "void (*p_func)(struct LCM_setting_table *table, unsigned int count, unsigned char force_update))" in c,
        "同步推 dimming": "set_panel_disp_param(0x500, push_table);" in c,
        "cancel 兜底": "cancel_delayed_work_sync(&open_lcm_dimming);" in c,
        "pr_alert 日志两条": c.count('pr_alert("[S3PRO_DIM_EARLY]') >= 2,
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
    print("\ndimming early（CMDQ-safe）修复完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
