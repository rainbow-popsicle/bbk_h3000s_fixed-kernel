#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 disp_lcm_resume 从 dpmgr_path_config 之后挪到 dpmgr_path_trigger 之后。

=== 为什么 ===
primary_display_resume 的原顺序：
    dpmgr_path_config(...)      配好 DSI，但 phylc=0（MIPI 时钟还没开）
    disp_lcm_resume(pgc->plcm)  ★ 推 23 项初始化表（含 sleep-out 0x11）
    dpmgr_path_start(...)       ddp_dsi_start 里 DSI_clk_HS_mode(true) 才把 phylc 置 1

实测（128）：
    config  dsi0 C ... phylc=00000000     ← config 后时钟还是关的
    start   dsi0 C ... phylc=00000001     ← start 之后才开

于是推表的那一刻 MIPI 时钟是关闭的，命令没有传输通道，全部被丢弃。
面板收不到 sleep-out(0x11)，就停在 suspend 时收到的 sleep-in(0x10) 深睡里，
从此不回应任何 DCS，表现为「熄屏唤醒后永久黑屏」。

LK 能亮的顺序正好相反：LK 先开时钟、再推表。

=== 为什么插在 trigger 之后而不是 start 之后 ===
resume 里 start 之后紧跟着两个 dpmgr_path_is_busy() 检查：
    1) "[POWER]Fatal error, we didn't start display path but it's already busy"
    2) "[POWER]Fatal error, we didn't trigger display path but it's already busy"
而 lcm_resume 推表会调 DSI_Start 让 DSI 变 busy，若插在检查 2 之前，
检查 2 立刻报 Fatal error 并 ret=-1。

再往后是 _cmdq_start_trigger_loop()，trigger loop 一旦跑起来就会
cmdqRecWait(MUTEX0_STREAM_EOF)（event 53）。若 lcm_resume 在它之后执行并切 CMD
模式，会打断视频流，event 53 永不到，trigger loop 死锁 —— 这正是 read_id
踩过的坑（实测 454 次 CMDQ SW timeout，整机卡死）。

所以唯一同时满足三个约束的位置是：
    dpmgr_path_trigger(...)  之后
    _cmdq_start_trigger_loop() 之前

=== 配套 ===
cmd_mode_init 应设为 0（走 VM_CMD，不切 CMD）。
推表现在已在视频流启动之后，VM_CMD 有 VFP 空隙可注入，不需要再切 CMD。

=== 实现 ===
锚点用 DISPCHECK("[POWER]dpmgr path start[end]\\n");（实测唯一），
再从它往后找第一个 _cmdq_start_trigger_loop();，把整块插到那个调用之前。
两段式定位避免依赖 dpmgr_path_trigger(...) 这一行（源码里有 5 处，不唯一）。

用法：
    python patch_resume_order.py <内核树根> [--dry-run]
开关：RESUME_ORDER=1 时由 build.sh 调用（默认开）。
"""
import os
import sys

MARK = "S3PRO_RESUME_ORDER"

SRC_BEGIN = 'DISPCHECK("[POWER]lcm resume[begin]\\n");'
SRC_END = 'DISPCHECK("[POWER]lcm resume[end]\\n");'
# 唯一锚点：从它往后找 _cmdq_start_trigger_loop()
ANCHOR = 'DISPCHECK("[POWER]dpmgr path start[end]\\n");'
# 插入位置：这个调用之前
INSERT_BEFORE = '_cmdq_start_trigger_loop();'

NOTE = (
    '\t/* S3PRO_RESUME_ORDER: lcm resume 必须放在 trigger 之后、start_trigger_loop 之前。\n'
    '\t *\n'
    '\t * 原顺序问题：config 之后立刻推表，而 dpmgr_path_start 里的\n'
    '\t * DSI_clk_HS_mode(true) 还没执行（实测 config 后 phylc=0，start 后 phylc=1）。\n'
    '\t * 推表时 MIPI 时钟是关的，命令没有传输通道被全部丢弃，\n'
    '\t * 面板收不到 sleep-out(0x11)，停在 suspend 时的 sleep-in(0x10) 深睡，\n'
    '\t * 从此不回应任何 DCS，表现为熄屏唤醒后永久黑屏。\n'
    '\t *\n'
    '\t * 为什么不能放在 start 之后：start 后面紧跟两个 dpmgr_path_is_busy 检查，\n'
    '\t * 其中「didnt trigger but already busy」那条会被推表造成的 DSI busy 误伤，\n'
    '\t * 直接 ret=-1。\n'
    '\t * 为什么不能放在 start_trigger_loop 之后：那会因切换 CMD 模式打断视频流，\n'
    '\t * 让 trigger loop 永远等不到 MUTEX0_STREAM_EOF(event 53) 而死锁\n'
    '\t * （read_id 就是踩了这个坑，实测 454 次 CMDQ SW timeout 卡死整机）。\n'
    '\t *\n'
    '\t * 配套：cmd_mode_init=0（走 VM_CMD）。此时视频流已启动，VFP 有空隙可注入。 */\n'
)


def find_file(tree):
    return os.path.join(tree, "drivers", "misc", "mediatek", "video", "mt8173",
                        "videox", "primary_display.c")


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    path = find_file(tree)
    if not os.path.exists(path):
        print("!! 找不到 %s" % path)
        return 1
    t = open(path, encoding="utf-8", errors="surrogateescape").read()

    if MARK in t:
        print("  已经打过补丁（找到 %s），跳过" % MARK)
        return 0

    # ---- 1) 定位并取出 lcm resume 块 ----
    i0 = t.find(SRC_BEGIN)
    if i0 < 0:
        print("!! 找不到 %r" % SRC_BEGIN)
        return 1
    i1 = t.find(SRC_END, i0)
    if i1 < 0:
        print("!! 找不到 %r" % SRC_END)
        return 1
    i1 += len(SRC_END)

    block = t[i0:i1]
    if "disp_lcm_resume(pgc->plcm);" not in block:
        print("!! 块里没有 disp_lcm_resume，锚点可能不对")
        return 1

    # ---- 2) 定位唯一锚点，再往后找 _cmdq_start_trigger_loop ----
    a = t.find(ANCHOR)
    if a < 0:
        print("!! 找不到唯一锚点 %r" % ANCHOR)
        return 1
    if t.find(ANCHOR, a + 1) >= 0:
        print("!! 锚点 %r 在文件里不唯一，拒绝继续" % ANCHOR)
        return 1
    if a < i1:
        print("!! 锚点在 lcm resume 块之前，不符合预期")
        return 1

    ins = t.find(INSERT_BEFORE, a)
    if ins < 0:
        print("!! 锚点之后找不到 %r" % INSERT_BEFORE)
        return 1

    # ---- 3) 删除原块（连同前导空白）----
    cut_from = i0
    while cut_from > 0 and t[cut_from - 1] in " \t":
        cut_from -= 1
    new_t = t[:cut_from] + t[i1:]

    # ---- 4) 重新定位插入点（偏移已变）----
    a2 = new_t.find(ANCHOR)
    if a2 < 0:
        print("!! 删除后找不到锚点")
        return 1
    ins2 = new_t.find(INSERT_BEFORE, a2)
    if ins2 < 0:
        print("!! 删除后找不到插入点")
        return 1
    # 退到该行行首，补上缩进
    line_start = new_t.rfind("\n", 0, ins2) + 1
    indent = new_t[line_start:ins2]

    ALERT = (indent + 'pr_alert("[S3PRO_RESUME_ORDER] lcm resume moved after trigger '
             '(dpmgr_path_start done, trigger loop not started yet)\\n");\n')
    moved = indent + NOTE.replace("\t", indent) + ALERT + indent + block + "\n"
    new_t = new_t[:line_start] + moved + new_t[line_start:]

    # ---- 5) 自检 ----
    checks = []

    def chk(name, cond):
        checks.append((name, bool(cond)))

    chk("标记已写入", MARK in new_t)
    chk("标记出现在 print 里（否则不进 .rodata）",
        'pr_alert("[S3PRO_RESUME_ORDER]' in new_t)
    chk("disp_lcm_resume 次数不变",
        new_t.count("disp_lcm_resume(pgc->plcm);") == t.count("disp_lcm_resume(pgc->plcm);"))
    chk("lcm resume[begin] 仍 1 次", new_t.count(SRC_BEGIN) == 1)
    chk("lcm resume[end] 仍 1 次", new_t.count(SRC_END) == 1)
    chk("path start[end] 仍 1 次", new_t.count(ANCHOR) == 1)
    chk("start_trigger_loop 次数不变",
        new_t.count(INSERT_BEFORE) == t.count(INSERT_BEFORE))

    # 关键顺序：lcm_resume 在 path start 之后、start_trigger_loop 之前
    p_anchor = new_t.find(ANCHOR)
    p_lcm = new_t.find("disp_lcm_resume(pgc->plcm);", p_anchor)
    p_loop = new_t.find(INSERT_BEFORE, p_anchor)
    chk("lcm_resume 在 path start 之后", p_lcm > p_anchor)
    chk("lcm_resume 在 start_trigger_loop 之前", 0 < p_lcm < p_loop)

    chk("花括号平衡", new_t.count("{") == new_t.count("}"))

    import re
    bad = any("\n" in m.group(1) for m in re.finditer(r'DISP(?:MSG|CHECK|ERR)\("([^"]*)"', new_t))
    chk("打印字符串不跨行", not bad)

    print()
    for name, ok in checks:
        print("    %-42s %s" % (name, "OK" if ok else "失败!"))
    print()
    if not all(ok for _, ok in checks):
        print("!! 自检未通过，不写文件")
        return 1

    if dry:
        print("--- dry-run，未写文件 ---")
        return 0

    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(new_t)
    print("  %s 完成：lcm resume 已挪到 trigger 之后、start_trigger_loop 之前" % MARK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
