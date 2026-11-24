#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 ddp_dsi_config 的 video 分支补一次 DSI_Start（对齐 LOS 14.1）。

=== 这是死锁的「第一推动」 ===

【LOS 14.1 的 ddp_dsi_config @0x476b88，收尾段 0x4792f0】

    4792f0 ldr w0,[x29,#0x70]     ; 条件（对应 dsi_config->mode != CMD_MODE）
    4792f4 cbz w0, 0x478f70       ; 不满足则跳过
    4792f8 ldr w19,[x29,#0x74]    ; module
    4792fc mov x1,x27             ; cmdq
    479300 mov w0,w19
    479304 bl DSI_Start           ; ★ LOS 在 config 阶段就启动 DSI
    479308 mov w0,w19; mov x1,x27
    479310 bl Dsi_Config_VDO_Timing
    47932c ret

【我们的 ddp_dsi_config（源码 ddp_dsi.c 行2846-2850）】

    if (dsi_config->mode != CMD_MODE
        || ((dsi_config->switch_mode_enable == 1) && (dsi_config->switch_mode != CMD_MODE))) {
        DSI_Config_VDO_Timing(module, cmdq, dsi_config);   <- 少了 DSI_Start
        DSI_Set_VM_CMD(module, cmdq);
    }

=== 为什么这一句决定生死 ===

完整死锁链（全部来自源码，不是推测）：

  _trigger_display_interface()（primary_display.c:1551）
    ├─ _should_trigger_path()            (行807)  CMDQ 模式 -> return 0
    │    => 这里【不调】dpmgr_path_trigger
    ├─ _should_flush_cmdq_config_handle() (行849) CMDQ 模式 -> return 1
    │    => _cmdq_flush_config_handle() 刷新 config 队列
    └─ _should_insert_wait_frame_done_token() (行885) CMDQ 模式 -> return 1
         => _cmdq_insert_wait_frame_done_token() 往 config 队列插
            cmdqRecWaitNoClear(CMDQ_EVENT_MUTEX0_STREAM_EOF)   // 等事件 53

  _cmdq_build_trigger_loop()（行1288）
    => trigger loop 的第一条指令也是 cmdqRecWait(CMDQ_EVENT_MUTEX0_STREAM_EOF)
       （cmdqRecWait 编译成 CMDQ_CODE_WFE = 等待并清除）

  事件 53 = CMDQ_EVENT_MUTEX0_STREAM_EOF，由 MUTEX0 硬件在「一帧结束」时产生。

  dpmgr_path_start（在 config 之后）调 ddp_dsi_start，后者把 DSI_START 清 0：
      DSI 不输出 -> 没有帧 -> MUTEX0 不产生 53
      -> config 队列卡住 + trigger loop 卡住
      -> trigger loop 里的 dpmgr_path_trigger 永不执行
      -> ddp_dsi_trigger 永不执行 -> DSI_Start 永不调用 -> DSI_START 永远 0（闭环）

  dpmgr_path_trigger 在 CMDQ 模式下【只有】trigger loop 里那一个调用点，
  所以 trigger loop 一卡，就再没有任何地方能调 DSI_Start。

【为什么 ddp_dsi_config 里的 DSI_Start 能破环】

  ddp_dsi_config 由 dpmgr_path_config 调用，位置在 dpmgr_path_start【之前】：
      dpmgr_path_config -> ddp_dsi_config -> DSI_Start（DSI_START=1）
                        -> DSI 开始输出 -> MUTEX0 产生第一个 53
      ... 随后 ddp_dsi_start 把 0 清掉也没关系，因为 53 已经发生过一次，
      config 队列和 trigger loop 都过了那道等，链条自己转起来。

  这就是「第一推动」。LOS 14.1 靠它在 video 模式下正常点亮；
  我们缺这一句，所以每次 resume 都死锁。

=== 与之前几个尝试的区别 ===

  patch_dsi_start_en.py  把 DSI_Start 加在 ddp_dsi_start 末尾：
      位置在清 0 的同一个函数里，紧接着又被后续路径影响，且导致 START/trigger 状态互相打架 -> 无效（§3 第6/14条）
  patch_vdo_eof_event.py 软件伪造事件 53：
      受 dpmgr_path_is_idle() 与 CMDQ_CODE_WFE 清除语义影响，竞态不可控 -> 无效（实测 trig 仍 0）
  本补丁                在 config 阶段让 DSI 真的输出一帧：
      不依赖任何软件伪造事件，直接让硬件产生真实 53。位置、顺序都与 LOS 一致。

=== 用法 ===
    python patch_dsi_config_start.py <内核树根> [--dry-run]
开关：DSI_CFG_START=1（默认 1）。
"""
import os
import sys

MARK = "S3PRO_DSI_CFG_START"

OLD = '''	if (dsi_config->mode != CMD_MODE
	    || ((dsi_config->switch_mode_enable == 1) && (dsi_config->switch_mode != CMD_MODE))) {
		DSI_Config_VDO_Timing(module, cmdq, dsi_config);
		DSI_Set_VM_CMD(module, cmdq);
	}'''

NEW = '''	if (dsi_config->mode != CMD_MODE
	    || ((dsi_config->switch_mode_enable == 1) && (dsi_config->switch_mode != CMD_MODE))) {
		/* S3PRO_DSI_CFG_START: video 分支里先启动 DSI（对齐 LOS 14.1）。
		 *
		 * LOS ddp_dsi_config @0x476b88 的收尾段（0x4792f0-0x479310）是：
		 *     ldr w0,[x29,#0x70] / cbz -> 跳过    ; = dsi_config->mode != CMD_MODE
		 *     bl DSI_Start                         ; ★ 先启动
		 *     bl Dsi_Config_VDO_Timing             ; 再配时序
		 * 我们的顺序正好少了 DSI_Start。
		 *
		 * 为什么这一句能破死锁：
		 *   本函数由 dpmgr_path_config 调用，位于 dpmgr_path_start（会清 DSI_START=0）之前。
		 *   DSI_Start 让 DSI 立刻输出一帧 -> MUTEX0 硬件产生 CMDQ_EVENT_MUTEX0_STREAM_EOF(53)。
		 *   而 config 队列（_cmdq_insert_wait_frame_done_token）和 trigger loop
		 *   （_cmdq_build_trigger_loop 第一条 cmdqRecWait）都在等这个 53。
		 *   53 一旦真实发生过一次，两道等待都放行，trigger loop 里的
		 *   dpmgr_path_trigger -> ddp_dsi_trigger -> DSI_Start 就能把 DSI_START 重新置 1，
		 *   之后靠真实 vsync 自持。
		 *   反之（本补丁关闭时）DSI 从不输出 -> 53 永不来 -> 两个队列全卡 -> 死锁。 */
		DSI_Start(module, cmdq);
		DSI_Config_VDO_Timing(module, cmdq, dsi_config);
		DSI_Set_VM_CMD(module, cmdq);
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
        print("   （ddp_dsi_config 的 video timing 段可能已被别的补丁改过）")
        return 1

    t = t.replace(OLD, NEW, 1)
    print("  已在 ddp_dsi_config 的 video 分支补上 DSI_Start")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(path, encoding="utf-8", errors="surrogateescape").read()

    # 只取 ddp_dsi_config 的函数体来判定，避免误匹配 push_table 里原有的 DSI_Start
    _s = c.find("int ddp_dsi_config(")
    _e = c.find("int ddp_dsi_start(", _s + 1)
    _body = c[_s:_e] if (_s >= 0 and _e > _s) else ""
    _i_start = _body.find("DSI_Start(module, cmdq);")
    _i_timing = _body.find("DSI_Config_VDO_Timing(module, cmdq, dsi_config);")

    checks = {
        "标记存在": MARK in c,
        "config 函数体里有 DSI_Start": _i_start >= 0,
        "DSI_Start 在 Config_VDO_Timing 之前": (_i_start >= 0 and _i_timing >= 0 and _i_start < _i_timing),
        "Config_VDO_Timing 仍保留": "DSI_Config_VDO_Timing(module, cmdq, dsi_config);" in c,
        "DSI_Set_VM_CMD 仍保留": "DSI_Set_VM_CMD(module, cmdq);" in c,
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
    print("\n  ddp_dsi_config 点火补丁完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
