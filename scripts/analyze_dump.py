#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析 mtkclient 回读出来的分区目录，回答三个决定性问题：

  1. 这台机器的内核是什么版本、什么格式（决定能不能用公开的 MT8176 源码树）
  2. 是不是 Treble 机型（决定能不能直接刷原生安卓 9 的 GSI，完全不用编内核）
  3. 有没有 AVB / A-B / 动态分区（决定刷机方式和救砖难度）

用法：
    python analyze_dump.py <dump目录>
    python analyze_dump.py <dump目录> --printgpt printgpt.txt

dump 目录一般是 `python mtk rl out` 或 `python mtk r boot boot.img ...` 的结果，
文件名就是分区名（boot.img / vendor.img / vbmeta.img ...，带不带 .img 都行）。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootimg  # noqa: E402

BOOT_CANDIDATES = ("boot", "boot_a", "boot_b")

# 关心的分区，按重要性排
WATCH = ("boot", "boot_a", "boot_b", "vendor", "system", "vbmeta", "dtbo",
         "super", "recovery", "lk", "preloader", "tee")


def human(n):
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return ("%d%s" % (v, unit)) if unit == "B" else ("%.1f%s" % (v, unit))
        v /= 1024.0
    return "%d" % n


def scan(directory):
    """返回 {分区名(无扩展名, 小写): (原文件名, 字节数)}"""
    found = {}
    if not os.path.isdir(directory):
        return found
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        base = os.path.splitext(name)[0].lower()
        found[base] = (name, os.path.getsize(path))
    return found


def read_printgpt(path):
    """尽量从 mtkclient printgpt 的文本里捞出分区名（格式不固定，只做宽松匹配）。"""
    names = set()
    if not path or not os.path.isfile(path):
        return names
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.replace(":", " ").replace("|", " ").split()
            for token in parts:
                t = token.lower()
                if t in WATCH or t in ("boot_a", "boot_b", "userdata"):
                    names.add(t)
                    break
    return names


def main(argv=None):
    ap = argparse.ArgumentParser(description="分析 MTK 分区 dump，判断 GSI / 内核路线")
    ap.add_argument("dumpdir", help="分区镜像所在目录")
    ap.add_argument("--printgpt", help="可选：mtkclient printgpt 的文本输出")
    args = ap.parse_args(argv)

    found = scan(args.dumpdir)
    gpt_names = read_printgpt(args.printgpt)
    if not found and not gpt_names:
        print("目录里没找到任何文件：%s" % args.dumpdir, file=sys.stderr)
        return 2

    def has(name):
        return name in found or name in gpt_names

    def size_of(name):
        return found[name][1] if name in found else None

    print("=" * 68)
    print("分区清单（%s）" % args.dumpdir)
    print("=" * 68)
    for key in sorted(found):
        name, size = found[key]
        print("  %-22s %10s" % (name, human(size)))
    if gpt_names:
        extra = sorted(gpt_names - set(found))
        if extra:
            print("  （printgpt 里还有：%s）" % " ".join(extra))

    # ---------------- 内核 ----------------
    print()
    print("=" * 68)
    print("内核")
    print("=" * 68)
    boot = None
    for cand in BOOT_CANDIDATES:
        if cand in found:
            boot = found[cand]
            break
    kernel_release = None
    if boot:
        name, size = boot
        print("  boot 镜像 : %s (%s)" % (name, human(size)))
        try:
            data = open(os.path.join(args.dumpdir, name), "rb").read()
            hdr = bootimg.parse_header(data)
            lay = bootimg.layout(data, hdr)
            koff, ksize, kind = lay["kernel"]
            kblob = data[koff:koff + ksize]
            kernel_release = bootimg.kernel_release(kblob)
            print("  内核 release : %s" % (kernel_release or "未找到（可能被剥离）"))
            print("  内核格式     : %s%s" % (
                kind, "（前置 MTK 私有头）" if lay["mtk_header"] else ""))
            print("  page_size    : %d" % hdr["page_size"])
            print("  ramdisk      : %s" % human(hdr["ramdisk_size"]))
            print("  内嵌 .config : %s" % ("有" if bootimg.extract_ikconfig(kblob) else "无"))
        except bootimg.BootImgError as exc:
            print("  解析失败 : %s" % exc)
    else:
        print("  没找到 boot.img / boot_a.img —— 先回读 boot 分区再来")

    # ---------------- 平台特性 ----------------
    print()
    print("=" * 68)
    print("平台特性")
    print("=" * 68)
    vsize = size_of("vendor")
    treble = has("vendor") and (vsize is None or vsize > 1024 * 1024)
    ab = has("boot_a") or has("boot_b")
    avb = has("vbmeta")
    dynamic = has("super")

    if vsize is not None:
        print("  vendor 分区 : 有（%s）" % human(vsize))
    else:
        print("  vendor 分区 : %s" % ("有（来自 printgpt）" if has("vendor") else "没有"))
    print("  分区方案    : %s" % ("A/B" if ab else "A-only"))
    print("  AVB         : %s" % ("有 vbmeta" if avb else "没看到 vbmeta"))
    print("  动态分区    : %s" % ("有 super" if dynamic else "无"))

    # ---------------- 结论 ----------------
    print()
    print("=" * 68)
    print("结论")
    print("=" * 68)
    if treble:
        print("  [Treble] 有 vendor 分区 —— 想要原生安卓 9，直接刷 arm64 GSI，")
        print("           内核完全不用动。刷之前把 boot/vendor 备份留好。")
    else:
        print("  [无 vendor] 大概率不是 Treble —— GSI 这条路走不通，")
        print("           只能改原厂 system 或整机移植，工作量和风险都大得多。")

    if kernel_release:
        base = ".".join(kernel_release.split("-")[0].split(".")[:3])
        if base.startswith("3.18"):
            print("  [3.18] 公开的 MT8176 源码版本段对得上"
                  "（cappu 3.18.123 / mt8176_common 3.18.35），可以试着自编。")
        elif base.startswith("4.4"):
            print("  [4.4]  MT8176 没有公开的 4.4 树，自编要从别的 MTK 4.4 平台移植，")
            print("         属于研究性工作量，不建议作为第一步。")
        else:
            print("  [%s] 不在已知的 MT8176 源码范围内，需要另找树。" % base)

    print()
    print("下一步：把本报告贴出来，就能定走 GSI 还是自编内核。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
