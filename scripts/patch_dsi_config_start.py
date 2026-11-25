#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 ddp_dsi_config 的【出口处】补一次 DSI_Start（对齐 LOS 14.1）。

=== 为什么是「出口处」而不是 video timing 分支里 ===

第一版把 DSI_Start 加在 video timing 那个 if 块里：

    if (dsi_config->mode != CMD_MODE || ...) {
        DSI_Start(module, cmdq);          <- 第一版放这里
        DSI_Config_VDO_Timing(...);
        DSI_Set_VM_CMD(...);
    }

2026-11-25 刷机实测：pr_alert 一次都没打印，trig 仍为 0。原因是函数中段有：

    if ((MIPITX_IsEnabled(module, cmdq)) && (atomic_read(&PMaster_enable) == 0)) {
        DISPCHECK("mipitx is already init\\n");
        goto done;                        <- 直接跳到函数末尾
    }

resume 时 MIPITX 已经初始化过，MIPITX_IsEnabled() 为真，于是 goto done
把上面整个 video timing 分支跳过了 —— DSI_Start 永远执行不到。
（DSI_Config_VDO_Timing / DSI_Set_VM_CMD 同样被跳过，video timing 沿用开机那次的值。）

注意 goto 是【向前】跳到 done: 标签，所以放在 done: 【之前】仍然会被跳过。
必须放在 done: 标签【之后】才会无条件执行。

=== LOS 14.1 的依据 ===

LOS 的 ddp_dsi_config @0x476b88 收尾段（0x4792f0-0x479310）：

    4792f0 ldr w0,[x29,#0x70]     ; 条件
    4792f4 cbz w0, 0x478f70       ; 不满足则跳过
    479304 bl DSI_Start           ; ★ 先启动 DSI
    479310 bl Dsi_Config_VDO_Timing
    47932c ret

=== 为什么这一句是死锁的「第一推动」===

完整死锁链（全部来自源码）：

  _trigger_display_interface()（primary_display.c:1551）
    ├─ _should_trigger_path()               (行807)  CMDQ 模式 -> return 0
    │    => 这里【不调】dpmgr_path_trigger
    ├─ _should_flush_cmdq_config_handle()   (行849)  CMDQ 模式 -> return 1
    │    => 刷新 config 队列
    └─ _should_insert_wait_frame_done_token() (行885) CMDQ 模式 -> return 1
         => config 队列插入 cmdqRecWaitNoClear(CMDQ_EVENT_MUTEX0_STREAM_EOF)  等事件 53

  _cmdq_build_trigger_loop()（行1288）
    => trigger loop 第一条指令也是 cmdqRecWait(53)（CMDQ_CODE_WFE = 等待并清除）

  事件 53 由 MUTEX0 硬件在「一帧结束」时产生。
  dpmgr_path_start 里的 ddp_dsi_start 把 DSI_START 清 0
    -> DSI 不输出 -> 没有帧 -> MUTEX0 不产生 53
    -> config 队列与 trigger loop 双双卡死
    -> trigger loop 里的 dpmgr_path_trigger 永不执行
    -> ddp_dsi_trigger 永不执行 -> DSI_Start 永不调用 -> DSI_START 永远 0（闭环）

  而 CMDQ 模式下 dpmgr_path_trigger 【只有】trigger loop 这一个调用点，
  所以 loop 一卡，就再没有任何地方能调 DSI_Start。

在 config 出口处无条件 DSI_Start，能让 DSI 真实输出一帧 -> MUTEX0 产生真实的 53
-> 两道等待都放行 -> 链条自己转起来。这不是伪造事件，不依赖任何竞态。

=== 与另外两个尝试的区别 ===

  patch_dsi_start_en.py   加在 ddp_dsi_start 末尾：位置在清 0 的同一个函数里，且 START
                          状态互相打架 -> 无效（§3 第6/14条）
  patch_vdo_eof_event.py  软件 cmdqCoreSetEvent(53) 伪造事件：
                          2026-11-25 实测 is_idle=0 且 trig 仍 0 -> 无效
  本补丁                  让硬件真的产生一帧 -> 不依赖竞态与软件事件

=== 用法 ===
    python patch_dsi_config_start.py <内核树根> [--dry-run]
开关：DSI_CFG_START=1（默认 1）。
"""
import os
import sys

MARK = "S3PRO_DSI_CFG_START"

# 锚点：ddp_dsi_config 的出口（done: 标签 + 只有本函数才有的 s3pro_dsi_regs("config")）
OLD = '''done:
	s3pro_dsi_regs("config");
	return 0;
}'''

NEW = '''done:
	/* S3PRO_DSI_CFG_START: 在函数出口无条件启动 DSI（对齐 LOS 14.1 的 config 收尾）。
	 *
	 * 为什么放在 done: 之后而不是上面的 video timing 分支里：
	 *   函数中段有 `if ((MIPITX_IsEnabled(...)) && ...) goto done;`
	 *   resume 时 MIPITX 已初始化 -> 直接跳到 done，把整个 video timing 分支跳过。
	 *   2026-11-25 实测：放在分支里时本 pr_alert 一次都没打印，trig 仍为 0。
	 *   goto 是向前跳，所以放在 done: 之前仍会被跳过；必须放在 done: 之后。
	 *
	 * 为什么这一句能破死锁：
	 *   本函数由 dpmgr_path_config 调用，位于 dpmgr_path_start（会清 DSI_START=0）之前。
	 *   DSI_Start 让 DSI 立刻输出一帧 -> MUTEX0 硬件产生 CMDQ_EVENT_MUTEX0_STREAM_EOF(53)。
	 *   而 config 队列（_cmdq_insert_wait_frame_done_token）和 trigger loop
	 *   （_cmdq_build_trigger_loop 第一条 cmdqRecWait）都在等这个 53。
	 *   53 真实发生过一次，两道等待都放行，trigger loop 里的
	 *   dpmgr_path_trigger -> ddp_dsi_trigger -> DSI_Start 就能把 DSI_START 重新置 1，
	 *   之后靠真实 vsync 自持。 */
	pr_alert("[S3PRO_DSI_CFG_START] dsi_config tail: DSI_Start (mode=%d)\\n",
		 dsi_config->mode);
	DSI_Start(module, cmdq);
	s3pro_dsi_regs("config");
	return 0;
}'''


def brace_balance(s):
    depth = 0
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == '"':
            i += 1
            while i < n:
                if s[i] == "\\":
                    i += 2
                    continue
                if s[i] == '"':
                    break
                i += 1
        elif c == "/" and i + 1 < n and s[i + 1] == "*":
            i += 2
            while i + 1 < n and not (s[i] == "*" and s[i + 1] == "/"):
                i += 1
            i += 1
        elif c == "/" and i + 1 < n and s[i + 1] == "/":
            while i < n and s[i] != "\n":
                i += 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return depth


def find_func_body(text, sig):
    """定位函数体 (start, end)，end 为闭合大括号之后。"""
    i = text.find(sig)
    if i < 0:
        return None
    j = text.find("{", i + len(sig))
    if j < 0:
        return None
    depth = 0
    k = j
    n = len(text)
    while k < n:
        c = text[k]
        if c == '"':
            k += 1
            while k < n:
                if text[k] == "\\":
                    k += 2
                    continue
                if text[k] == '"':
                    break
                k += 1
        elif c == "/" and k + 1 < n and text[k + 1] == "*":
            k += 2
            while k + 1 < n and not (text[k] == "*" and text[k + 1] == "/"):
                k += 1
            k += 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return (i, k + 1)
        k += 1
    return None


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    path = os.path.join(tree, "drivers", "misc", "mediatek", "video", "mt8173",
                        "dispsys", "ddp_dsi.c")
    if not os.path.exists(path):
        raise SystemExit("找不到 ddp_dsi.c: %s" % path)
    print("  目标: %s" % os.path.relpath(path, tree))

    t = open(path, encoding="utf-8", errors="surrogateescape").read()

    if MARK in t:
        print("  已打过 S3PRO_DSI_CFG_START，跳过")
        return 0

    cnt = t.count(OLD)
    if cnt != 1:
        print("!! 锚点匹配 %d 次（应为 1），中止" % cnt)
        return 1

    t = t.replace(OLD, NEW, 1)
    print("  已在 ddp_dsi_config 出口补上 DSI_Start")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(path, encoding="utf-8", errors="surrogateescape").read()

    # 只在 ddp_dsi_config 函数体里判定，避免误匹配 push_table 里的 DSI_Start
    rng = find_func_body(c, "int ddp_dsi_config(")
    body = c[rng[0]:rng[1]] if rng else ""
    i_mark = body.find("S3PRO_DSI_CFG_START")
    i_start = body.find("DSI_Start(module, cmdq);")
    i_done = body.find("done:")
    i_regs = body.find('s3pro_dsi_regs("config");')

    checks = {
        "标记存在": MARK in c,
        "config 函数体里有 DSI_Start": i_start >= 0,
        "DSI_Start 在 done: 之后（goto 跳不到）": (i_done >= 0 and i_start > i_done),
        "pr_alert 在 done: 之后": (i_done >= 0 and i_mark > i_done),
        "s3pro_dsi_regs 仍保留": i_regs >= 0,
        "一处 pr_alert": c.count("[S3PRO_DSI_CFG_START]") == 1,
        "花括号配平": brace_balance(c) == 0,
    }
    print()
    ok = True
    for k, v in checks.items():
        print("  %-38s %s" % (k, "OK" if v else "FAIL"))
        if not v:
            ok = False
    if not ok:
        print("\n!! 校验失败")
        return 1
    print("\n  ddp_dsi_config 出口点火补丁完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
