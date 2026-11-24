#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启用 video mode 下被 `#if 0` 关掉的 trigger-loop 初始事件（点火）。

=== 完整证据链（2026-11-23）===

【我们源码 primary_display.c:1363 _cmdq_start_trigger_loop】

    ret = cmdqRecStartLoop(pgc->cmdq_handle_trigger);
    if (!primary_display_is_video_mode()) {
        if (_need_wait_esd_eof())
            cmdqCoreSetEvent(CMDQ_SYNC_TOKEN_ESD_EOF);
        /* need to set STREAM_EOF for the first time, otherwise we will stuck in dead loop */
        cmdqCoreSetEvent(CMDQ_SYNC_TOKEN_STREAM_EOF);      <- cmd mode：点火了
        cmdqCoreSetEvent(CMDQ_SYNC_TOKEN_CABC_EOF);
        dprec_event_op(DPREC_EVENT_CMDQ_SET_EVENT_ALLOW);
    } else {
#if 0
        if (dpmgr_path_is_idle(pgc->dpmgr_handle))
            cmdqCoreSetEvent(CMDQ_EVENT_MUTEX0_STREAM_EOF); <- video mode：点火被 #if 0 掐了
#endif
    }

    MTK 自己在 cmd mode 那行写的注释就是：「第一次必须设 STREAM_EOF，否则会卡在死循环」。

【LOS 14.1 逆向对比（逐指令确认）】

  primary_display_suspend @0x4a8e0c（56 条）
      -> 不调用 _cmdq_stop_trigger_loop(0x49e0b4)
      -> 只清 pgc[+0x3d0]，不碰 [+0x1e0]（CMDQ loop 使能标志）
      => LLOS 的 trigger loop 全程不停，醒来不需要重新点火

  dpmgr_path_start @0x491330
      -> 只遍历模块调 start 回调（[obj+0x20]），不调 trigger

  cmdqRecStartLoop @0x42faf4
      -> 纯包装（printk + 检查 + 调 cmdq_task_start_loop），不设任何初始事件

【我们内核的 suspend（源码 primary_display.c:3199）】

    DISPCHECK("[POWER]display cmdq trigger loop stop[begin]\n");
    _cmdq_stop_trigger_loop();          <- 无条件调用（不在任何 #ifdef 里）
    dpmgr_path_stop(pgc->dpmgr_handle, CMDQ_DISABLE);

  => 我们在 suspend 停掉了 loop；resume 时重启后它在等第一个事件。

【死锁闭环】

  suspend:  _cmdq_stop_trigger_loop()          loop 停
  resume :  _cmdq_start_trigger_loop()          loop 重启，卡在「等第一个事件」
            ddp_dsi_start() 清 DSI_START=0      DSI 不输出
            -> 无 vsync / RDMA0 EOF            事件永远不来
            -> ddp_dsi_trigger() 不执行         唯一置 START=1 的地方
            -> DSI_START 永远 0                 回到第一行

  而 video mode（我们就是 BURST_VDO_MODE）的点火代码被 #if 0 关着。

【本补丁】

  把 video mode 分支那段 `#if 0 ... #endif` 取消，让它在 path 空闲时补一次
  CMDQ_EVENT_MUTEX0_STREAM_EOF（事件号 53，cmdq_event.h:72 已定义）。

  效果：resume 后软件伪造成「第 0 帧已结束」-> trigger loop 跑第一帧
        -> ddp_dsi_trigger -> DSI_Start -> START=1 -> DSI 出图
        -> 真实 vsync 产生 -> 之后完全靠硬件自持，不再需要软件点火。

【风险】
  - dpmgr_path_is_idle() 若判断为「不空闲」，这句不执行（等于没开）
  - MTK 为什么 #if 0 原因不明，可能在特定场景（如 memout/decouple）有副作用
  - 这不是原厂 LOS 14.1 的做法（它根本不停 loop），属于「用原厂自带代码
    补偿两边 suspend 行为差异」

=== 用法 ===
    python patch_vdo_eof_event.py <内核树根> [--dry-run]
开关：VDO_EOF_EVENT=1（默认 1）。
"""
import os
import sys

MARK = "S3PRO_VDO_EOF"

OLD = '''	} else {
#if 0
		if (dpmgr_path_is_idle(pgc->dpmgr_handle))
			cmdqCoreSetEvent(CMDQ_EVENT_MUTEX0_STREAM_EOF);

#endif
	}'''

NEW = '''	} else {
		/* S3PRO_VDO_EOF: video mode 下也补一次初始 STREAM_EOF 事件。
		 *
		 * MTK 在上面的 cmd mode 分支里自己写着：
		 *   "need to set STREAM_EOF for the first time,
		 *    otherwise we will stuck in dead loop"
		 * 而 video mode 分支里同样的事被 #if 0 关掉了。
		 *
		 * LOS 14.1 逆向对比：它的 primary_display_suspend 不调用
		 * _cmdq_stop_trigger_loop（只清 pgc[+0x3d0]，不碰 [+0x1e0] 的 loop 标志），
		 * 所以 trigger loop 全程不停，醒来无需点火。
		 * 而我们的 suspend 无条件 _cmdq_stop_trigger_loop()，
		 * resume 重启 loop 后它卡在第一个等待事件上：
		 *   ddp_dsi_start 清 DSI_START=0 -> 无 vsync / RDMA0 EOF
		 *   -> ddp_dsi_trigger 不执行 -> DSI_START 永远 0（死锁）。
		 *
		 * 这里补一次 MUTEX0_STREAM_EOF 把第一帧推起来，
		 * 之后靠真实 vsync 自持，不再需要软件点火。
		 *
		 * 【为什么去掉 MTK 的 dpmgr_path_is_idle() 保护】
		 * dpmgr_path_is_idle() = !dpmgr_path_is_busy()，后者遍历 path 上所有模块
		 * 调 is_busy 回调；resume 刚 dpmgr_path_start 完，OVL/RDMA 等很可能报忙，
		 * is_idle 返回 0 -> 点火被跳过（2026-11-24 实测：补了但 trig 仍 0）。
		 * 而「等 path 空闲」本身就是死锁的一部分——没有帧它永远不会空闲。
		 * 所以这里无条件点火，并把 is_idle 的值打出来备查。 */
		pr_alert("[S3PRO_VDO_EOF] kick MUTEX0_STREAM_EOF (is_idle=%d)\\n",
			 dpmgr_path_is_idle(pgc->dpmgr_handle));
		cmdqCoreSetEvent(CMDQ_EVENT_MUTEX0_STREAM_EOF);
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
                        "videox", "primary_display.c")
    if not os.path.exists(path):
        raise SystemExit("找不到 primary_display.c: %s" % path)
    print("  目标: %s" % os.path.relpath(path, tree))

    t = open(path, encoding="utf-8", errors="surrogateescape").read()

    if MARK in t:
        print("  已打过 S3PRO_VDO_EOF，跳过")
        return 0

    cnt = t.count(OLD)
    if cnt != 1:
        print("!! 锚点匹配 %d 次（应为 1），中止" % cnt)
        print("   （_cmdq_start_trigger_loop 的 else 分支结构可能变了）")
        return 1

    t = t.replace(OLD, NEW, 1)
    print("  已启用 video mode 的 MUTEX0_STREAM_EOF 点火")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(path, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "标记存在": MARK in c,
        "MUTEX0_STREAM_EOF 已启用": "cmdqCoreSetEvent(CMDQ_EVENT_MUTEX0_STREAM_EOF);" in c,
        "#if 0 已移除": "#if 0\n\t\tif (dpmgr_path_is_idle(pgc->dpmgr_handle))" not in c,
        "无条件点火": "cmdqCoreSetEvent(CMDQ_EVENT_MUTEX0_STREAM_EOF);" in c,
        "is_idle 打印": "[S3PRO_VDO_EOF] kick" in c,
        "花括号配平": brace_balance(c) == 0,
    }
    print()
    ok = True
    for k, v in checks.items():
        print("  %-32s %s" % (k, "OK" if v else "FAIL"))
        if not v:
            ok = False
    if not ok:
        print("\n!! 校验失败")
        return 1
    print("\n  video mode 点火补丁完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
