#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 LOS 14.1 行为，去掉 primary_display_resume 里 video mode 的 wait token。

=== 这是死锁链里【唯一一处「我们有、LOS 没有」】的关键调用 ===

我们的 primary_display.c（primary_display_resume 尾部）：

    if (primary_display_is_video_mode()) {
        MMProfileLogEx(...);
        /* for video mode, we need to force trigger here */
        /* for cmd mode, just set DPREC_EVENT_CMDQ_SET_EVENT_ALLOW when trigger loop start */
        dpmgr_path_trigger(pgc->dpmgr_handle, NULL, CMDQ_DISABLE);
        /* insert a wait token to make sure first config after resume will config to HW when HW idle */
        _cmdq_insert_wait_frame_done_token();      <- 本补丁去掉这一句
    }
    _cmdq_start_trigger_loop();

LOS 14.1 的 primary_display_resume @0x4a2e54（457 条，逐指令反汇编）：

    ...
    4a33fc bl   0x4a0f4c                  ; primary_display_is_video_mode
    4a3400 cbnz w0, 0x4a3488              ; 是 video mode -> 跳 3488
    4a3404 ... printk ...
    4a3428 bl   0x4a1224                  ; _cmdq_start_trigger_loop
    ...
    4a3488 bl   0x4963cc                  ; ddp_mmp_get_events
    4a349c bl   0x3e83b0                  ; MMProfileEnableEvent
    4a34b4 mov  x1,#0
    4a34b8 mov  w2,#0
    4a34bc ldr  x0,[x0,#0x120]            ; dpmgr_handle
    4a34c0 bl   0x491c3c                  ; dpmgr_path_trigger
    4a34c4 b    0x4a3404                  ; 回到 3404 -> 继续到 start_loop

  验证：整个 457 条里 bl 0xffffffc00049e7c4（_cmdq_insert_wait_frame_done_token）
        【0 处】；bl 0xffffffc00049e734（_cmdq_set_config_handle_dirty）也【0 处】。
        而 _cmdq_insert_wait_frame_done_token 在 LOS 的 _trigger_display_interface
        里是有的（0x4a48bc，1 处）—— 说明 LOS 只是不在 resume 里调。

=== 为什么这一句会死锁 ===

_cmdq_insert_wait_frame_done_token() 的实现在 primary_display.c:1599：

    if (primary_display_is_video_mode())
        cmdqRecWaitNoClear(pgc->cmdq_handle_config, CMDQ_EVENT_MUTEX0_STREAM_EOF);
    else
        cmdqRecWaitNoClear(pgc->cmdq_handle_config, CMDQ_SYNC_TOKEN_STREAM_EOF);
    dprec_event_op(DPREC_EVENT_CMDQ_WAIT_STREAM_EOF);

也就是往 config 队列里插一条「等事件 53」。而事件 53 由 MUTEX0 硬件在
「一帧结束」时产生。resume 流程里 dpmgr_path_start 已经把 DSI_START 清 0，
DSI 不输出 -> 没有帧 -> 53 不会来 -> config 队列末尾那条等待永远不完成。

原注释写着 "make sure first config after resume will config to HW when HW idle"
—— 前提是「硬件会变空闲」，而在黑屏状态下它永远不会。

=== 与其它尝试的关系 ===

  patch_vdo_eof_event.py   软件 cmdqCoreSetEvent(53) 想满足这个等待：
                           2026-11-25 实测 is_idle=0、事件设了 trig 仍 0 -> 无效
  patch_dsi_config_start.py 在 config 出口 DSI_Start，想让硬件真产生 53
  本补丁                   按照 LOS 的做法，干脆【不等】这个 53

三者针对同一个 53，本补丁最贴合原厂实现（LOS 就是这么跑的），
而且不依赖任何时序或竞态。

=== 用法 ===
    python patch_resume_no_wait_token.py <内核树根> [--dry-run]
开关：RESUME_NO_WAIT_TOKEN=1（默认 1）。
"""
import os
import sys

MARK = "S3PRO_NO_WAIT_TOKEN"

OLD = '''	if (primary_display_is_video_mode()) {
		MMProfileLogEx(ddp_mmp_get_events()->primary_resume, MMProfileFlagPulse, 1, 7);
		/* for video mode, we need to force trigger here */
		/* for cmd mode, just set DPREC_EVENT_CMDQ_SET_EVENT_ALLOW when trigger loop start */
		dpmgr_path_trigger(pgc->dpmgr_handle, NULL, CMDQ_DISABLE);
		/* insert a wait token to make sure first config after resume will config to HW when HW idle */
		_cmdq_insert_wait_frame_done_token();
	}'''

NEW = '''	if (primary_display_is_video_mode()) {
		MMProfileLogEx(ddp_mmp_get_events()->primary_resume, MMProfileFlagPulse, 1, 7);
		/* for video mode, we need to force trigger here */
		/* for cmd mode, just set DPREC_EVENT_CMDQ_SET_EVENT_ALLOW when trigger loop start */
		dpmgr_path_trigger(pgc->dpmgr_handle, NULL, CMDQ_DISABLE);
		/* S3PRO_NO_WAIT_TOKEN: 按 LOS 14.1 行为，这里【不】插 wait token。
		 *
		 * 原代码这一句是：
		 *   _cmdq_insert_wait_frame_done_token();
		 * 注释写 "insert a wait token to make sure first config after resume
		 * will config to HW when HW idle"。
		 * 它做的事是往 config 队列插 cmdqRecWaitNoClear(CMDQ_EVENT_MUTEX0_STREAM_EOF)
		 * —— 等事件 53（MUTEX0 一帧结束）。但 resume 时 dpmgr_path_start 已把
		 * DSI_START 清 0，DSI 不输出 -> 没有帧 -> 53 永不来 -> config 队列末尾
		 * 那条等待永远不完成。
		 *
		 * LOS 14.1 的 primary_display_resume（457 条逐指令反汇编）里
		 * bl _cmdq_insert_wait_frame_done_token(0x49e7c4) 【0 处】，
		 * video mode 分支只调 dpmgr_path_trigger 就直接 start_trigger_loop。
		 * 本补丁照此对齐。 */
		pr_alert("[S3PRO_NO_WAIT_TOKEN] skip _cmdq_insert_wait_frame_done_token (LOS 14.1 aligned)\\n");
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


def _strip_c_comments(text):
    """去掉 C 的块注释和行注释（保留字符串字面量）。"""
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            out.append(c)
            i += 1
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    out.append(text[i:i + 2])
                    i += 2
                    continue
                out.append(text[i])
                if text[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def find_func_body(text, sig):
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
                        "videox", "primary_display.c")
    if not os.path.exists(path):
        raise SystemExit("找不到 primary_display.c: %s" % path)
    print("  目标: %s" % os.path.relpath(path, tree))

    t = open(path, encoding="utf-8", errors="surrogateescape").read()

    if MARK in t:
        print("  已打过 S3PRO_NO_WAIT_TOKEN，跳过")
        return 0

    cnt = t.count(OLD)
    if cnt != 1:
        print("!! 锚点匹配 %d 次（应为 1），中止" % cnt)
        return 1

    t = t.replace(OLD, NEW, 1)
    print("  已去掉 resume 里 video mode 的 wait token")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(path, encoding="utf-8", errors="surrogateescape").read()

    # 只统计 primary_display_resume 函数体内的【真代码】调用（注释里出现的不算）
    rng = find_func_body(c, "int primary_display_resume(")
    body = c[rng[0]:rng[1]] if rng else ""
    body_code = _strip_c_comments(body)
    checks = {
        "标记存在": MARK in c,
        "带 pr_alert": "[S3PRO_NO_WAIT_TOKEN] skip" in c,
        "resume 函数体内已无该调用": "_cmdq_insert_wait_frame_done_token();" not in body_code,
        "别处调用仍在(>=6 处)": c.count("_cmdq_insert_wait_frame_done_token();") >= 6,
        "dpmgr_path_trigger 仍保留": "dpmgr_path_trigger(pgc->dpmgr_handle, NULL, CMDQ_DISABLE);" in body,
        "花括号配平": brace_balance(c) == 0,
    }
    print()
    ok = True
    for k, v in checks.items():
        print("  %-34s %s" % (k, "OK" if v else "FAIL"))
        if not v:
            ok = False
    if not ok:
        print("\n!! 校验失败")
        return 1
    print("\n  resume 去 wait token 补丁完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
