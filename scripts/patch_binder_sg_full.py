#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实现真正的 binder scatter-gather（配合 patch_binder_sg.py 的别名版升级）。

设备实测：别名版（buffers_size==0 才放行）在真机上出现 241 次
    binder: 286:286 BC_TRANSACTION_SG buffers_size=96 unsupported
取值分布：24 / 80 / 88 / 96 / 104 字节，来自 android.hardware.* / allocator /
keystore / audioserver / configstore / cas / cameraserver 等进程。
→ 说明 8.1 的 hwbinder 常态携带 buffers 区，必须是真 SG 支持。

本补丁（对齐 AOSP 8.1 的 "extra_buffers_size" 做法）：
  1) struct binder_buffer 增加 extra_buffers_size
  2) binder_alloc_buf(proc, data_size, offsets_size, extra_buffers_size, is_async)
  3) 分配 size / 溢出检查 / 记账都计入 extra_buffers_size
  4) binder_free_buf 释放时同样计入
  5) binder_transaction(..., binder_size_t extra_buffers_size) 透传给分配器
  6) BC_TRANSACTION/BC_REPLY 传 0；SG 命令传 trsg.buffers_size 并去掉 EINVAL
  7) binder_init 打一条 "SG transaction support enabled" 供镜像自检

用法：
    python patch_binder_sg_full.py <内核树根> [--dry-run]
"""
import os
import re
import sys

MARK = "S3PRO_BINDER_SG_FULL"

EDITS = [
    # (说明, 旧串, 新串, 必须唯一)
    ("struct binder_buffer 增加 extra_buffers_size",
     "\tsize_t data_size;\n\tsize_t offsets_size;\n\tuint8_t data[0];",
     "\tsize_t data_size;\n\tsize_t offsets_size;\n\tsize_t extra_buffers_size;\t/* %s */\n\tuint8_t data[0];" % MARK,
     True),

    ("binder_alloc_buf 签名",
     "static struct binder_buffer *binder_alloc_buf(struct binder_proc *proc,\n"
     "\t\t\t\t\t      size_t data_size, size_t offsets_size, int is_async)",
     "static struct binder_buffer *binder_alloc_buf(struct binder_proc *proc,\n"
     "\t\t\t\t\t      size_t data_size, size_t offsets_size,\n"
     "\t\t\t\t\t      size_t extra_buffers_size, int is_async)",
     True),

    ("分配 size 计入 extra",
     "\tsize = ALIGN(data_size, sizeof(void *)) + ALIGN(offsets_size, sizeof(void *));\n",
     "\tsize = ALIGN(data_size, sizeof(void *)) + ALIGN(offsets_size, sizeof(void *)) +\n"
     "\t    ALIGN(extra_buffers_size, sizeof(void *));\n",
     True),

    ("溢出检查计入 extra",
     "\tif (size < data_size || size < offsets_size) {",
     "\tif (size < data_size || size < offsets_size ||\n"
     "\t    size < extra_buffers_size) {",
     True),

    ("记录 extra_buffers_size",
     "\tbuffer->data_size = data_size;\n\tbuffer->offsets_size = offsets_size;",
     "\tbuffer->data_size = data_size;\n\tbuffer->offsets_size = offsets_size;\n"
     "\tbuffer->extra_buffers_size = extra_buffers_size;",
     True),

    ("binder_free_buf 计入 extra",
     "\tsize = ALIGN(buffer->data_size, sizeof(void *)) +\n"
     "\t    ALIGN(buffer->offsets_size, sizeof(void *));",
     "\tsize = ALIGN(buffer->data_size, sizeof(void *)) +\n"
     "\t    ALIGN(buffer->offsets_size, sizeof(void *)) +\n"
     "\t    ALIGN(buffer->extra_buffers_size, sizeof(void *));",
     True),

    ("binder_transaction 签名",
     "\t\t\t       struct binder_transaction_data *tr, int reply)",
     "\t\t\t       struct binder_transaction_data *tr, int reply,\n"
     "\t\t\t       binder_size_t extra_buffers_size)",
     True),

    ("分配调用透传 extra",
     "\tt->buffer = binder_alloc_buf(target_proc, tr->data_size,\n"
     "\t\t\t\t     tr->offsets_size, !reply && (t->flags & TF_ONE_WAY));",
     "\tt->buffer = binder_alloc_buf(target_proc, tr->data_size,\n"
     "\t\t\t\t     tr->offsets_size, extra_buffers_size,\n"
     "\t\t\t\t     !reply && (t->flags & TF_ONE_WAY));",
     True),

    ("普通事务传 0",
     "binder_transaction(proc, thread, &tr, cmd == BC_REPLY);",
     "binder_transaction(proc, thread, &tr, cmd == BC_REPLY, 0);",
     True),

    ("SG 事务改为真支持（去掉 EINVAL）",
     "\t\t\tif (trsg.buffers_size != 0) {\n"
     "\t\t\t\tpr_err(\"%d:%d BC_TRANSACTION_SG buffers_size=%llu unsupported\\n\",\n"
     "\t\t\t\t       proc->pid, thread->pid,\n"
     "\t\t\t\t       (unsigned long long)trsg.buffers_size);\n"
     "\t\t\t\treturn -EINVAL;\n"
     "\t\t\t}\n"
     "\t\t\tbinder_transaction(proc, thread, &trsg.transaction_data,\n"
     "\t\t\t\t\t   cmd == BC_REPLY_SG);",
     "\t\t\tbinder_transaction(proc, thread, &trsg.transaction_data,\n"
     "\t\t\t\t\t   cmd == BC_REPLY_SG, trsg.buffers_size);",
     True),

    ("binder_init 增加启动标记（供镜像自检）",
     "\tpr_info(\"binder: multi-context ready binder=%d hwbinder=%d vndbinder=%d\\n\",\n"
     "\t\tbinder_dev_minor[BINDER_DEV_BINDER],\n"
     "\t\tbinder_dev_minor[BINDER_DEV_HWBINDER],\n"
     "\t\tbinder_dev_minor[BINDER_DEV_VNDBINDER]);",
     "\tpr_info(\"binder: multi-context ready binder=%d hwbinder=%d vndbinder=%d\\n\",\n"
     "\t\tbinder_dev_minor[BINDER_DEV_BINDER],\n"
     "\t\tbinder_dev_minor[BINDER_DEV_HWBINDER],\n"
     "\t\tbinder_dev_minor[BINDER_DEV_VNDBINDER]);\n"
     "\tpr_info(\"binder: SG transaction support enabled\\n\");",
     True),
]


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    root = sys.argv[1]
    dry = "--dry-run" in sys.argv
    path = os.path.join(root, "drivers", "staging", "android", "binder.c")
    if not os.path.exists(path):
        raise SystemExit("找不到 %s" % path)
    src = open(path, encoding="utf-8", errors="surrogateescape").read()

    if MARK in src and "SG transaction support enabled" in src:
        print("已经打过完整 SG 补丁，跳过")
        return 0

    orig = src
    problems = []
    for desc, old, new, uniq in EDITS:
        cnt = src.count(old)
        if cnt == 0:
            problems.append("未命中: %s" % desc)
            continue
        if uniq and cnt > 1:
            problems.append("命中 %d 次（应为 1）: %s" % (cnt, desc))
            continue
        src = src.replace(old, new, 1)
        print("  ✓ %s" % desc)

    if src.count("{") != src.count("}"):
        problems.append("花括号不平衡 %d/%d" % (src.count("{"), src.count("}")))
    if src.count("(") != src.count(")"):
        problems.append("圆括号不平衡 %d/%d" % (src.count("("), src.count(")")))

    # 关键点复查
    for kw in ("extra_buffers_size", "trsg.buffers_size"):
        pass
    n_extra = src.count("extra_buffers_size")
    print("\n  extra_buffers_size 出现 %d 次（期望 >= 8）" % n_extra)
    if n_extra < 8:
        problems.append("extra_buffers_size 出现次数偏少: %d" % n_extra)
    if "BC_TRANSACTION_SG buffers_size=%llu unsupported" in src:
        problems.append("旧的 EINVAL 分支还在")

    if problems:
        print("\n!! 有问题：")
        for p in problems:
            print("   - " + p)
        return 1

    print("\n  行数 %d -> %d" % (orig.count("\n"), src.count("\n")))
    if dry:
        print("  (dry-run，未写文件)")
        return 0
    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(src)
    print("  已写入 %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
