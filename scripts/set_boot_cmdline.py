#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 boot.img 的 cmdline 加/删内核参数（纯字节操作，不动 kernel/ramdisk/dtb）。

为什么需要：
    内核里 s3pro_tmg 这类参数必须在 lcm_get_params 之前就确定，而 lcm_get_params
    只在显示子系统初始化时调用一次（通常是开机）。debugfs 变量重启会重置，
    所以「开机参数」只能靠 cmdline 传。

用法：
    # 给 boot.img 加上 s3pro_tmg=1（sharp timing）
    python scripts/set_boot_cmdline.py in.img out.img --set s3pro_tmg=1

    # 删掉
    python scripts/set_boot_cmdline.py in.img out.img --del s3pro_tmg

    # 只看当前 cmdline
    python scripts/set_boot_cmdline.py in.img

Android boot image 布局（本工具只依赖前 1088 字节）：
    0    magic "ANDROID!"
    8    kernel_size / 12 kernel_addr / 16 ramdisk_size / 20 ramdisk_addr
    24   second_size / 28 second_addr / 32 tags_addr / 36 page_size
    40   dt_size
    48   name[16]
    64   cmdline[512]        ← 本工具改这里
    576  id[8]
    584  extra_cmdline[1024] ← MTK 有时也用，一并处理
"""
import argparse
import os
import sys

CMDLINE_OFF = 64
CMDLINE_MAX = 512
EXTRA_OFF = 584
EXTRA_MAX = 1024


def read_cstr(buf, off, maxlen):
    end = off
    limit = min(off + maxlen, len(buf))
    while end < limit and buf[end] != 0:
        end += 1
    return buf[off:end].decode("utf-8", "replace")


def write_cstr(buf, off, maxlen, text):
    raw = text.encode("utf-8")
    if len(raw) >= maxlen:
        raise ValueError("cmdline 太长：%d 字节 >= 上限 %d" % (len(raw), maxlen))
    buf[off:off + maxlen] = raw + b"\x00" * (maxlen - len(raw))


def strip_key(cmdline, key):
    """删掉所有 key=... 或单独的 key（空格分隔）。"""
    out = []
    for tok in cmdline.split():
        if tok == key or tok.startswith(key + "="):
            continue
        out.append(tok)
    return " ".join(out)


def _looks_textual(s):
    """判断一段字符串是否像 cmdline（全可打印 + 至少一个 =）。"""
    if not s:
        return False
    if "=" not in s:
        return False
    for ch in s:
        o = ord(ch)
        if o < 0x20 or o > 0x7e:
            return False
    return True


def main():
    ap = argparse.ArgumentParser(description="改 boot.img cmdline")
    ap.add_argument("img", help="输入 boot.img")
    ap.add_argument("out", nargs="?", help="输出 boot.img（省略则只打印）")
    ap.add_argument("--set", action="append", default=[],
                    metavar="KEY=VALUE", help="设置参数（可多次）")
    ap.add_argument("--del", action="append", default=[], dest="dels",
                    metavar="KEY", help="删除参数（可多次）")
    args = ap.parse_args()

    if not os.path.exists(args.img):
        raise SystemExit("找不到 %s" % args.img)

    data = bytearray(open(args.img, "rb").read())
    if data[:8] != b"ANDROID!":
        raise SystemExit("不是 Android boot image（magic=%r）" % bytes(data[:8]))

    cur = read_cstr(data, CMDLINE_OFF, CMDLINE_MAX)
    extra = read_cstr(data, EXTRA_OFF, EXTRA_MAX)

    print("  当前 cmdline      : %r" % cur)
    if extra:
        print("  当前 extra_cmdline: %r" % extra)

    if not args.set and not args.dels:
        return 0

    new = cur
    for key in args.dels:
        new = strip_key(new, key)
    for kv in args.set:
        if "=" not in kv:
            raise SystemExit("--set 需要 KEY=VALUE 形式：%s" % kv)
        key = kv.split("=", 1)[0]
        new = strip_key(new, key)
        new = (new + " " + kv).strip()

    print("  新 cmdline        : %r" % new)
    if not args.out:
        print("  （未给输出文件，只打印）")
        return 0

    write_cstr(data, CMDLINE_OFF, CMDLINE_MAX, new)
    # extra_cmdline 只有在「看起来确实是文本」时才同步改。
    # 这台设备的 boot.img 在 584 偏移处是二进制（MTK 自定义布局），不能盲改。
    if extra and _looks_textual(extra):
        newe = extra
        for key in args.dels:
            newe = strip_key(newe, key)
        for kv in args.set:
            key = kv.split("=", 1)[0]
            newe = strip_key(newe, key)
            newe = (newe + " " + kv).strip()
        write_cstr(data, EXTRA_OFF, EXTRA_MAX, newe)
        print("  新 extra_cmdline  : %r" % newe)
    elif extra:
        print("  extra_cmdline 区域是二进制，跳过（只改 cmdline）")

    open(args.out, "wb").write(bytes(data))
    print("  已写出: %s (%d 字节)" % (args.out, len(data)))

    # 回读校验
    chk = bytearray(open(args.out, "rb").read())
    got = read_cstr(chk, CMDLINE_OFF, CMDLINE_MAX)
    print("  回读校验          : %r  %s" % (got, "OK" if got == new else "不一致!"))
    return 0 if got == new else 1


if __name__ == "__main__":
    sys.exit(main())
