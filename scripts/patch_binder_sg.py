#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给老版 binder 补上 BC_TRANSACTION_SG / BC_REPLY_SG（命令 17 / 18）。

问题（设备实测 dmesg）：
    binder: 506:506 unknown command 1078485777      ← 0x40486311
    1078485777 = _IOW('c', 17, 72 字节) = BC_TRANSACTION_SG
  → 8.1 的 libbinder（hwbinder）第一条事务就用 SG 形式，驱动不认识 → -EINVAL
  → 所有 HAL/SF 进程的 binder 通信直接失败（实测 603 次）
  → registerAsService() 返回 -2147483648 → vndservicemanager 死循环 → 黑屏

对照：作者能显示的内核 dmesg 里这种报错是 0 次，说明他的 binder 支持 SG。

本补丁（最小实现）：
  1) uapi/binder.h 增加 struct binder_transaction_data_sg + 两个命令号
  2) binder.c 的 binder_thread_write 增加两个 case：
     读 72 字节结构；buffers_size == 0 时按普通事务处理（语义完全等价）；
     buffers_size != 0 时打印出来并返回 -EINVAL（与现状相同，但能看到值）

  这样既能让 8.1 的 SG 事务跑通，又能在真遇到带 buffer 的 SG 时从 dmesg 里看到
  实际 buffers_size，判断是否还需要实现真正的 scatter-gather。

用法：
    python patch_binder_sg.py <内核树根> [--dry-run]
"""
import os
import re
import sys

MARK_H = "S3PRO_BINDER_SG_HEADER"
MARK_C = "S3PRO_BINDER_SG_CASE"

HDR_STRUCT = """/*
 * %s: Android 8.x libbinder/hwbinder 用 BC_TRANSACTION_SG 发送事务，
 * buffers_size 是随事务携带的额外缓冲区大小（本树暂不支持非 0）。
 */
struct binder_transaction_data_sg {
\tstruct binder_transaction_data transaction_data;
\tbinder_size_t buffers_size;
};

""" % MARK_H

HDR_ENUM = """\tBC_TRANSACTION_SG = _IOW('c', 17, struct binder_transaction_data_sg),
\tBC_REPLY_SG = _IOW('c', 18, struct binder_transaction_data_sg),

"""

CASE_CODE = """
\t\t/* %s: Android 8.x 的 SG 事务（命令 17/18） */
\t\tcase BC_TRANSACTION_SG:
\t\tcase BC_REPLY_SG: {
\t\t\tstruct binder_transaction_data_sg trsg;

\t\t\tif (copy_from_user(&trsg, ptr, sizeof(trsg)))
\t\t\t\treturn -EFAULT;
\t\t\tptr += sizeof(trsg);
\t\t\tif (trsg.buffers_size != 0) {
\t\t\t\tpr_err("%%d:%%d BC_TRANSACTION_SG buffers_size=%%llu unsupported\\n",
\t\t\t\t       proc->pid, thread->pid,
\t\t\t\t       (unsigned long long)trsg.buffers_size);
\t\t\t\treturn -EINVAL;
\t\t\t}
\t\t\tbinder_transaction(proc, thread, &trsg.transaction_data,
\t\t\t\t\t   cmd == BC_REPLY_SG);
\t\t\tbreak;
\t\t}
""" % MARK_C


def patch_header(path, dry):
    t = open(path, encoding="utf-8", errors="surrogateescape").read()
    if MARK_H in t:
        print("  头文件已打过补丁，跳过")
        return 0
    # 1) 结构体：插到 struct binder_transaction_data {...}; 之后
    start = t.find("struct binder_transaction_data {")
    if start < 0:
        print("  !! 头文件里找不到 struct binder_transaction_data")
        return 1
    # 花括号配平扫描，找到结构体结束的 "};"
    i = t.find("{", start)
    depth = 0
    end = -1
    while i < len(t):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                j = t.find(";", i)
                end = j + 1 if j >= 0 else -1
                break
        i += 1
    if end <= 0:
        print("  !! 结构体花括号配平失败")
        return 1
    t = t[:end] + "\n" + HDR_STRUCT + t[end:]
    # 2) 枚举：插到 BC_REPLY 之后
    m2 = re.search(r"(\tBC_REPLY = _IOW\('c', 1, struct binder_transaction_data\),\n)", t)
    if not m2:
        print("  !! 头文件里找不到 BC_REPLY 枚举项")
        return 1
    t = t[:m2.end()] + HDR_ENUM + t[m2.end():]
    if dry:
        print("  头文件 dry-run：将插入 struct binder_transaction_data_sg + 2 个枚举项")
        return 0
    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)
    print("  头文件已修改：+struct binder_transaction_data_sg, +BC_TRANSACTION_SG/BC_REPLY_SG")
    return 0


def patch_binder(path, dry):
    t = open(path, encoding="utf-8", errors="surrogateescape").read()
    if MARK_C in t:
        print("  binder.c 已打过补丁，跳过")
        return 0
    anchor = re.search(
        r"\t\tcase BC_TRANSACTION:\n\t\tcase BC_REPLY: \{\n(?:.*?\n)*?\t\t\tbreak;\n\t\t\}\n", t)
    if not anchor:
        print("  !! binder.c 里找不到 BC_TRANSACTION/BC_REPLY 处理块")
        return 1
    t = t[:anchor.end()] + CASE_CODE + t[anchor.end():]
    # 校验
    if t.count("{") != t.count("}"):
        print("  !! 花括号不平衡，中止")
        return 1
    if t.count("(") != t.count(")"):
        print("  !! 圆括号不平衡，中止")
        return 1
    if dry:
        print("  binder.c dry-run：将插入 BC_TRANSACTION_SG / BC_REPLY_SG 处理")
        print("  插入位置：第 %d 行之后（BC_TRANSACTION 块末尾）"
              % (t[:anchor.end()].count("\n") + 1))
        return 0
    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)
    print("  binder.c 已修改：+BC_TRANSACTION_SG / BC_REPLY_SG 处理（buffers_size==0 时按普通事务）")
    return 0


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    root = sys.argv[1]
    dry = "--dry-run" in sys.argv
    hdr = os.path.join(root, "drivers", "staging", "android", "uapi", "binder.h")
    bnd = os.path.join(root, "drivers", "staging", "android", "binder.c")
    for p in (hdr, bnd):
        if not os.path.exists(p):
            raise SystemExit("找不到 %s" % p)
    print("=== 补 SG 命令支持 ===")
    rc = patch_header(hdr, dry)
    if rc:
        return rc
    return patch_binder(bnd, dry)


if __name__ == "__main__":
    sys.exit(main())
