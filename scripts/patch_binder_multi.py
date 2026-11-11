#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 MTK 3.18 的单设备 binder 驱动补上「每设备独立 context」。

问题（设备实测）：
    [1.87s] vndservicemanager: binder: BINDER_SET_CONTEXT_MGR already set  → -EBUSY
    [1.94s] hwservicemanager:  binder: BINDER_SET_CONTEXT_MGR already set  → -EBUSY
  → vndservicemanager 起不来 → vendor HAL 注册全失败(-2147483648) → SurfaceFlinger 崩

原因：这棵树只有一份全局 context
    static struct binder_node *binder_context_mgr_node;
    static kuid_t binder_context_mgr_uid = INVALID_UID;
  三个 misc 设备（binder/hwbinder/vndbinder）共用它，第二个注册必然失败。

本补丁（对齐 AOSP 8.1 做法）：
  1) struct binder_context + binder_contexts[3]，按 misc minor 区分
  2) struct binder_proc 增加 ->context
  3) binder_open 按打开的设备选 context
  4) 全部全局引用改成 proc->context-> 形式
  5) binder_init 记录三个设备的 minor

用法：
    python patch_binder_multi.py <binder.c> [--dry-run]
"""
import difflib
import re
import sys

MARKER = "/* S3PRO_MULTI_BINDER_CONTEXT */"

NEW_GLOBALS = MARKER + """
/*
 * Each binder device (/dev/binder, /dev/hwbinder, /dev/vndbinder) needs its
 * OWN context manager. With a single global one, the second
 * BINDER_SET_CONTEXT_MGR fails with -EBUSY, which breaks Android 8.x where
 * hwservicemanager and vndservicemanager each register their own context.
 */
#define BINDER_DEV_NUM       3
#define BINDER_DEV_BINDER    0
#define BINDER_DEV_HWBINDER  1
#define BINDER_DEV_VNDBINDER 2

struct binder_context {
\tstruct binder_node *binder_context_mgr_node;
\tkuid_t binder_context_mgr_uid;
\tconst char *name;
};

static struct binder_context binder_contexts[BINDER_DEV_NUM] = {
\t[BINDER_DEV_BINDER]    = { .name = "binder" },
\t[BINDER_DEV_HWBINDER]  = { .name = "hwbinder" },
\t[BINDER_DEV_VNDBINDER] = { .name = "vndbinder" },
};
static int binder_dev_minor[BINDER_DEV_NUM] = { -1, -1, -1 };
"""

HELPER = """static int binder_dev_index(int minor)
{
\tint i;

\tfor (i = 0; i < BINDER_DEV_NUM; i++)
\t\tif (binder_dev_minor[i] == minor)
\t\t\treturn i;
\treturn BINDER_DEV_BINDER;
}

"""

# 定点替换（这些地方必须精确匹配上下文）
TARGETED = [
    ("!(node == binder_context_mgr_node && node->has_strong_ref)",
     "!(node->proc && node == node->proc->context->binder_context_mgr_node && node->has_strong_ref)"),
    ("new_ref->desc = (node == binder_context_mgr_node) ? 0 : 1;",
     "new_ref->desc = (node == proc->context->binder_context_mgr_node) ? 0 : 1;"),
    ("target_node = binder_context_mgr_node;",
     "target_node = proc->context->binder_context_mgr_node;"),
    ("if (target == 0 && binder_context_mgr_node &&",
     "if (target == 0 && proc->context->binder_context_mgr_node &&"),
    ("if (binder_context_mgr_node != NULL) {",
     "if (proc->context->binder_context_mgr_node != NULL) {"),
]

PLACEHOLDER = "@@S3PRO_BINDER_GLOBALS@@\n"


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    path = sys.argv[1]
    dry = "--dry-run" in sys.argv
    src = open(path, encoding="utf-8", errors="surrogateescape").read()

    if MARKER in src:
        print("已经打过补丁（找到标记），跳过")
        return 0

    orig = src
    problems = []

    # ---- 1) 两个全局变量 -> 占位符（最后再替换成新结构，避免被后面的全局替换污染）----
    pat = re.compile(
        r"static struct binder_node \*binder_context_mgr_node;\s*\n"
        r"\s*static kuid_t binder_context_mgr_uid = INVALID_UID;")
    if not pat.search(src):
        raise SystemExit("找不到两个全局变量定义，源码不匹配")
    src = pat.sub(PLACEHOLDER, src, count=1)

    # ---- 2) binder_proc 增加 context 字段 ----
    if "\tint pid;\n" not in src:
        raise SystemExit("找不到 binder_proc 里的 int pid;")
    src = src.replace("\tint pid;\n",
                      "\tint pid;\n\tstruct binder_context *context;\n", 1)

    # ---- 3) 定点替换 ----
    for old, new in TARGETED:
        if old not in src:
            problems.append("定点替换未命中: %s" % old[:56])
            continue
        src = src.replace(old, new, 1)

    # 跨行的一处：binder_get_ref_for_node(proc,\n\t\t\t\tbinder_context_mgr_node);
    src = re.sub(r"(binder_get_ref_for_node\(proc,\s*\n\s*)binder_context_mgr_node\)",
                 r"\1proc->context->binder_context_mgr_node)", src, count=1)

    # ---- 4) 其余裸引用统一加前缀 ----
    src = re.sub(r"(?<!->)\bbinder_context_mgr_node\b",
                 "proc->context->binder_context_mgr_node", src)
    src = re.sub(r"(?<!->)\bbinder_context_mgr_uid\b",
                 "proc->context->binder_context_mgr_uid", src)

    # ---- 4b) 还原字符串字面量内部（全局替换会误伤调试字符串）----
    def _fix_strings(text):
        def _repl(m):
            s = m.group(0)
            s = s.replace("proc->context->binder_context_mgr_node", "binder_context_mgr_node")
            s = s.replace("proc->context->binder_context_mgr_uid", "binder_context_mgr_uid")
            return s
        return re.sub(r'"(?:[^"\\]|\\.)*"', _repl, text)

    src = _fix_strings(src)
    # ---- 5) 占位符 -> 新的 context 结构（必须放在全局替换之后）----
    src = src.replace(PLACEHOLDER, NEW_GLOBALS + "\n")

    # ---- 6) binder_open：插入 helper + 选 context ----
    open_anchor = "static int binder_open(struct inode *nodp, struct file *filp)"
    if open_anchor not in src:
        raise SystemExit("找不到 binder_open")
    src = src.replace(open_anchor, HELPER + open_anchor, 1)

    alloc = ("\tproc = kzalloc(sizeof(*proc), GFP_KERNEL);\n"
             "\tif (proc == NULL)\n\t\treturn -ENOMEM;\n")
    if alloc not in src:
        problems.append("binder_open 里找不到 kzalloc 段")
    else:
        src = src.replace(
            alloc,
            alloc + "\tproc->context = &binder_contexts[binder_dev_index(iminor(nodp))];\n",
            1)

    # ---- 7) binder_init：记录 minor + 初始化 uid ----
    anchor = ("\tif (misc_register(&vndbinder_miscdev))\n"
              "\t\tpr_err(\"binder: failed to register vndbinder\\n\");\n")
    if anchor not in src:
        problems.append("binder_init 里找不到 vndbinder 注册段")
    else:
        add = anchor + (
            "\tbinder_dev_minor[BINDER_DEV_BINDER] = binder_miscdev.minor;\n"
            "\tbinder_dev_minor[BINDER_DEV_HWBINDER] = hwbinder_miscdev.minor;\n"
            "\tbinder_dev_minor[BINDER_DEV_VNDBINDER] = vndbinder_miscdev.minor;\n"
            "\tfor (i = 0; i < BINDER_DEV_NUM; i++)\n"
            "\t\tbinder_contexts[i].binder_context_mgr_uid = INVALID_UID;\n"
            "\tpr_info(\"binder: multi-context ready binder=%d hwbinder=%d vndbinder=%d\\n\",\n"
            "\t\tbinder_dev_minor[BINDER_DEV_BINDER],\n"
            "\t\tbinder_dev_minor[BINDER_DEV_HWBINDER],\n"
            "\t\tbinder_dev_minor[BINDER_DEV_VNDBINDER]);\n")
        src = src.replace(anchor, add, 1)

    init_anchor = "static int __init binder_init(void)\n{\n\tint ret;\n"
    if init_anchor in src:
        src = src.replace(init_anchor,
                          "static int __init binder_init(void)\n{\n\tint ret;\n\tint i;\n", 1)
    else:
        problems.append("binder_init 头部不匹配，int i 未加")

    # ---- 8) 校验 ----
    # 允许结构体自身的两行字段声明
    chk = src.replace("\tstruct binder_node *binder_context_mgr_node;\n", "")
    chk = chk.replace("\tkuid_t binder_context_mgr_uid;\n", "")
    chk = chk.replace("binder_contexts[i].binder_context_mgr_uid", "")
    chk = re.sub(r'"(?:[^"\\]|\\.)*"', '""', chk)   # 剔除字符串字面量
    left_node = len(re.findall(r"(?<!->)\bbinder_context_mgr_node\b", chk))
    left_uid = len(re.findall(r"(?<!->)\bbinder_context_mgr_uid\b", chk))
    if left_node:
        problems.append("仍有 %d 处未加前缀的 binder_context_mgr_node" % left_node)
    if left_uid:
        problems.append("仍有 %d 处未加前缀的 binder_context_mgr_uid" % left_uid)

    # 字符串字面量里不应出现 proc->context->
    for _m in re.finditer(r'"(?:[^"\\]|\\.)*"', src):
        if "proc->context->" in _m.group(0):
            problems.append("字符串里残留指针表达式: " + _m.group(0)[:60])
            break

    # 防止 "proc->context->context->" 之类的重复
    if "->context->context->" in src:
        problems.append("出现了重复的 ->context->context->")

    if src.count("{") != src.count("}"):
        problems.append("花括号不平衡: %d vs %d" % (src.count("{"), src.count("}")))
    if src.count("(") != src.count(")"):
        problems.append("圆括号不平衡: %d vs %d" % (src.count("("), src.count(")")))

    n_repl = len(re.findall(r"proc->context->binder_context_mgr_", src))
    print("改动统计:")
    print("  行数 %d -> %d" % (orig.count("\n"), src.count("\n")))
    print("  新增 struct binder_context / binder_contexts[3] / binder_dev_minor[3]")
    print("  binder_proc 增加 ->context")
    print("  binder_open 按 minor 选 context（helper: binder_dev_index）")
    print("  proc->context-> 引用共 %d 处" % n_repl)
    print("  binder_init 记录三个 minor + 初始化 uid")

    if problems:
        print("\n!! 有问题:")
        for p in problems:
            print("   - " + p)
        return 1

    if dry:
        print("\n(dry-run，未写文件) 关键片段：")
        for m in re.finditer(r".{0,60}proc->context->binder_context_mgr_(node|uid).{0,30}", src):
            print("   " + m.group(0).replace("\n", " ")[:110])
        return 0

    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(src)
    print("\n已写入 %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
