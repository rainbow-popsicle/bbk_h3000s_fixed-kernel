#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""让 primary_display_suspend 不再停掉 CMDQ trigger loop（对齐 LOS 14.1）。

=== 证据链（LOS 14.1 逆向，2026-11-23）===

LOS 14.1 的 primary_display_suspend @0x4a8e0c（56 条，ret @0x4a8ee8）：
    4a8e50 add x0,x19,#0x9e8; ldr w0,[x0,#0x1e0]; cbz -> 跳过   ; 只做 mutex 保护
    4a8e64 bl disp_sw_mutex_lock
    4a8e70 bl primary_path_lock
    4a8e80 ldr w0,[x21,#0x28]; cmp #0xf1                        ; 状态检查
    4a8eb8 bl disp_sw_mutex_unlock
    4a8ebc str wzr,[x19,#0x3d0]                                 ; 清 pgc[+0x3d0]
    4a8ed4 bl MMProfileEnableEvent
  => 全程【没有调用 _cmdq_stop_trigger_loop(0x49e0b4)】，trigger loop 一直活着。

我们的 primary_display_suspend（源码 primary_display.c）：
    DISPCHECK("[POWER]display cmdq trigger loop stop[begin]\\n");
    _cmdq_stop_trigger_loop();          <- 无条件停掉（不在任何 #ifdef 里）
    DISPCHECK("[POWER]display cmdq trigger loop stop[end]\\n");
    dpmgr_path_stop(pgc->dpmgr_handle, CMDQ_DISABLE);

=== 这为什么导致死锁 ===

trigger loop 的第一条指令就是（_cmdq_build_trigger_loop 行1288）：

    cmdqRecWait(pgc->cmdq_handle_trigger, CMDQ_EVENT_MUTEX0_STREAM_EOF);

而 cmdqRecWait 编译成 CMDQ_CODE_WFE（等待并清除事件）。
这个事件 CMDQ_EVENT_MUTEX0_STREAM_EOF(53) 由 MUTEX 硬件在「一帧结束」时产生。

    LOS  ：loop 不停 -> 一直在等 53 -> resume 后 path 恢复、硬件产生 53 -> 继续
           -> 执行到 dpmgr_path_trigger -> ddp_dsi_trigger -> DSI_Start -> DSI_START=1
    我们 ：suspend 停 loop -> resume 重启 loop -> 仍卡在等 53
           -> 但 ddp_dsi_start 已把 DSI_START 清 0，DSI 不输出 -> 无帧 -> 53 永不来
           -> ddp_dsi_trigger 不执行 -> DSI_START 永远 0（死锁）

=== 为什么不全靠自己「点火」 ===

patch_vdo_eof_event.py 走的是软件 cmdqCoreSetEvent(53) 伪造事件，但它有三个不确定性：
  1. MTK 原代码外面包了 if (dpmgr_path_is_idle(...))，resume 刚 start 完模块报忙 -> 跳过
  2. cmdqRecWait 是 WFE（等待并清除），设下去的 token 可能在 loop 执行到 WFE 时被清掉
  3. cmdqRecStartLoop 是异步的，设 token 与 loop 执行 WFE 的先后不确定（竞态）
而「不停 loop」把这三个问题一次性绕开，且完全贴合 LOS 14.1 的真实行为。

=== 风险 ===

suspend 后 loop 仍活着。但 dpmgr_path_stop + power_off 之后 CMDQ 不会产生事件，
loop 只是空等，不会写寄存器。LOS 14.1 一直这么跑且稳定。

=== 用法 ===
    python patch_no_stop_loop.py <内核树根> [--dry-run]
开关：NO_STOP_LOOP=1（默认 1）。
"""
import os
import sys

MARK = "S3PRO_NO_STOP_LOOP"

# 锚点策略：不用「三行连续文本」，改为「定位 primary_display_suspend 函数体，
# 再找其中唯一的 _cmdq_stop_trigger_loop();」。
# 原因：patch_disp_debug.py 会在 DISPCHECK("...stop[begin]...") 之后插入
# S3PRO_LCM_EARLY_SUSPEND 一大段（把 disp_lcm_suspend 提前），三行连续锚点因此失效。
FUNC_SIG = "int primary_display_suspend(void)"
TARGET = "_cmdq_stop_trigger_loop();"

REPLACE = '''	/* S3PRO_NO_STOP_LOOP: 不停 CMDQ trigger loop（对齐 LOS 14.1）。
	 *
	 * LOS 14.1 的 primary_display_suspend @0x4a8e0c 全程不调用
	 * _cmdq_stop_trigger_loop(0x49e0b4)，只清 pgc[+0x3d0]；trigger loop 一直活着。
	 *
	 * 停掉它会死锁：
	 *   trigger loop 第一条指令是 cmdqRecWait(CMDQ_EVENT_MUTEX0_STREAM_EOF)，
	 *   编译成 CMDQ_CODE_WFE（等待并清除），事件 53 由 MUTEX 硬件在「一帧结束」时产生。
	 *   suspend 停 loop 后，resume 重启的 loop 仍卡在等 53；而 ddp_dsi_start 已把
	 *   DSI_START 清 0，DSI 不输出 -> 没有帧 -> 53 永不来 -> ddp_dsi_trigger 不执行
	 *   -> DSI_START 永远 0（死锁）。
	 *   loop 不停则 resume 后硬件自然产生 53，链条自己接上。
	 *
	 * 安全性：dpmgr_path_stop + power_off 之后 CMDQ 不再产生事件，loop 只是空等，
	 * 不会写寄存器。LOS 14.1 一直这么跑。
	 */
	pr_alert("[S3PRO_NO_STOP_LOOP] keep cmdq trigger loop alive (LOS 14.1 aligned)\\n");
	/* _cmdq_stop_trigger_loop();  <- 按 LOS 14.1 行为注释掉 */'''


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
    """定位 sig 对应函数的体范围 (start, end)，end 为闭合大括号之后。

    用大括号配对，能容忍函数体内被其它补丁插入的任意代码。
    """
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
        print("  已打过 S3PRO_NO_STOP_LOOP，跳过")
        return 0

    rng = find_func_body(t, FUNC_SIG)
    if not rng:
        print("!! 找不到 %s 的函数体，中止" % FUNC_SIG)
        return 1
    fs, fe = rng
    body = t[fs:fe]

    cnt = body.count(TARGET)
    if cnt != 1:
        print("!! primary_display_suspend 函数体内 %s 出现 %d 次（应为 1），中止" % (TARGET, cnt))
        return 1

    old_line = "\t" + TARGET
    if body.count(old_line) != 1:
        print("!! %s 的带缩进形式匹配 %d 次（应为 1），中止" % (TARGET, body.count(old_line)))
        return 1

    new_body = body.replace(old_line, REPLACE, 1)
    t = t[:fs] + new_body + t[fe:]
    print("  已让 primary_display_suspend 不停 trigger loop（函数定位法）")

    if dry:
        print("\n  [dry-run] 未写文件")
        return 0

    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(path, encoding="utf-8", errors="surrogateescape").read()
    rng2 = find_func_body(c, FUNC_SIG)
    body2 = c[rng2[0]:rng2[1]] if rng2 else ""
    checks = {
        "标记存在": MARK in c,
        "stop 调用已被注释": "/* _cmdq_stop_trigger_loop();" in c,
        "带 pr_alert": "[S3PRO_NO_STOP_LOOP] keep cmdq trigger loop alive" in c,
        "suspend 函数体内已无 stop 调用": "\t_cmdq_stop_trigger_loop();" not in body2,
        "其他 stop 调用仍在": c.count("_cmdq_stop_trigger_loop();") >= 3,
        "花括号配平": brace_balance(c) == 0,
    }
    print()
    ok = True
    for k, v in checks.items():
        print("  %-30s %s" % (k, "OK" if v else "FAIL"))
        if not v:
            ok = False
    if not ok:
        print("\n!! 校验失败")
        return 1
    print("\n  no_stop_loop 补丁完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
