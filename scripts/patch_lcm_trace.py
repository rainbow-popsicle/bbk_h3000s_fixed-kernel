#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S3PRO 诊断补丁：在面板推表/resume 路径插入无条件 pr_alert 日志。

目的：确凿判定"熄屏唤醒后，初始化命令到底有没有发到面板"，解决调查报告里
      "缺解锁命令 vs 命令压根没发出"的互斥悬案。

为什么用 pr_alert：
  - 设备的 LCM_LOGI(pr_info) 日志实测没有输出到 dmesg（printk=7 4 1 7 本应显示），
    原因未明（可能 build 用的源里 LCM_LOGI 是 pr_debug，或该路径没走到）。
  - pr_alert 级别 0，无条件进 dmesg，不受 printk 级别过滤，也不需要 dynamic_debug。

只加日志，不改任何推表逻辑/时序/命令内容。幂等：重复运行不会重复插入。

用法：
    python patch_lcm_trace.py <内核树根> [--dry-run]
"""
import os
import sys

MARK = "S3PRO_TRACE"

# ---- 补丁 1：LCM 驱动里 push_table 的每条命令 + resume/suspend 入口 -----------
TRACE_INCLUDES = '''/* S3PRO_TRACE: 诊断日志所需头文件 */
#include <linux/printk.h>
'''

PUSH_TABLE_TRACE = '''/* S3PRO_TRACE: 推表命令级日志 —— 打印每条命令的 cmd/count */
#define S3PRO_T(fmt, args...) pr_alert("[S3PRO_TRACE] " fmt, ##args)

'''

# 在 push_table 的 default 分支里，dsi_set_cmdq_V2 调用之前插入日志
PUSH_CMD_ANCHOR = '''\t\tdefault:
\t\t\tdsi_set_cmdq_V2(cmd, table[i].count,
\t\t\t\t\ttable[i].para_list, force_update);'''
PUSH_CMD_REPL = '''\t\tdefault:
\t\t\tS3PRO_T("push_table cmd=0x%02x count=%u idx=%u/%u\\n",
\t\t\t\tcmd, table[i].count, i, count);
\t\t\tdsi_set_cmdq_V2(cmd, table[i].count,
\t\t\t\t\ttable[i].para_list, force_update);'''

# push_table 函数入口打印整表信息
PUSH_ENTRY_ANCHOR = '''static void push_table(struct LCM_setting_table *table, unsigned int count,
\t\t       unsigned char force_update)
{
\tunsigned int i;
'''
PUSH_ENTRY_REPL = '''static void push_table(struct LCM_setting_table *table, unsigned int count,
\t\t       unsigned char force_update)
{
\tunsigned int i;

\tS3PRO_T("push_table ENTER table=%p count=%u force=%u\\n", table, count, force_update);
'''

# push_table 函数结束（for 循环后）打印 EXIT
PUSH_EXIT_ANCHOR = '''\t\t}
\t}
}

/**
'''

# ---- 补丁 2：lcm_resume / lcm_suspend 入口 ------------------------------------
RESUME_ANCHOR = '''static void lcm_resume(void)
{
\tLCM_LOGI("%s enter\\n", __func__);
'''
RESUME_REPL = '''static void lcm_resume(void)
{
\tS3PRO_T("lcm_resume ENTER variant=%d\\n", s3pro_lcm_variant);
\tLCM_LOGI("%s enter\\n", __func__);
'''

SUSPEND_ANCHOR = '''static void lcm_suspend(void)
{
\tLCM_LOGI("%s enter\\n", __func__);
'''
SUSPEND_REPL = '''static void lcm_suspend(void)
{
\tS3PRO_T("lcm_suspend ENTER\\n");
\tLCM_LOGI("%s enter\\n", __func__);
'''


def patch_lcm(path, dry=False):
    """给 LCM 驱动打诊断日志补丁。"""
    with open(path, encoding="utf-8", errors="replace") as f:
        s = f.read()
    orig = s

    if MARK in s:
        print("  [skip] LCM 驱动已含 %s 标记" % MARK)
        return False

    # 插入头文件（在第一个 #include 之后）
    if PUSH_TABLE_TRACE not in s:
        idx = s.find("#include")
        if idx < 0:
            print("  [warn] 找不到 #include，跳过头文件插入")
        else:
            # 找到该 include 行末尾
            eol = s.find("\n", idx) + 1
            s = s[:eol] + TRACE_INCLUDES + PUSH_TABLE_TRACE + s[eol:]

    replacements = [
        (PUSH_ENTRY_ANCHOR, PUSH_ENTRY_REPL, "push_table 入口"),
        (PUSH_CMD_ANCHOR, PUSH_CMD_REPL, "push_table 命令级"),
        (RESUME_ANCHOR, RESUME_REPL, "lcm_resume 入口"),
        (SUSPEND_ANCHOR, SUSPEND_REPL, "lcm_suspend 入口"),
    ]

    for anchor, repl, name in replacements:
        if anchor not in s:
            print("  [warn] 锚点未找到: %s（跳过）" % name)
            continue
        s = s.replace(anchor, repl, 1)
        print("  [ok] 插入: %s" % name)

    if s == orig:
        print("  [warn] LCM 驱动没有任何改动")
        return False

    if dry:
        print("  [dry-run] 不写文件")
        return True

    with open(path, "w", encoding="utf-8") as f:
        f.write(s)
    return True


def find_lcm_driver(tree):
    """找到被注入 S3PRO 表的 nt35523 驱动（build 用 sharp）。"""
    base = os.path.join(tree, "drivers", "misc", "mediatek", "lcm")
    candidates = []
    for root, _d, files in os.walk(base):
        if "nt35523" not in root:
            continue
        for fn in files:
            if not fn.endswith(".c"):
                continue
            p = os.path.join(root, fn)
            with open(p, encoding="utf-8", errors="replace") as f:
                t = f.read()
            # 已打 S3PRO 表补丁的驱动（含 lcm_resume + s3pro_lcm_variant）
            if "s3pro_lcm_variant" in t or "s3pro_init_table_a" in t:
                return p
            if "lcm_initialization_setting[]" in t:
                candidates.append(p)
    # 优先 sharp
    for p in candidates:
        if "sharp" in p:
            return p
    return candidates[0] if candidates else None


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    lcm = find_lcm_driver(tree)
    if not lcm:
        raise SystemExit("找不到 nt35523 LCM 驱动")
    print("  LCM 驱动: %s" % os.path.relpath(lcm, tree))
    patch_lcm(lcm, dry)


if __name__ == "__main__":
    main()
