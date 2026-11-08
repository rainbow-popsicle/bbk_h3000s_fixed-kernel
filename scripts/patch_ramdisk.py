#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 boot.img 的 ramdisk 里改属性（默认关掉 adb 授权要求）。

为什么需要它：
    这台设备的屏不亮时根本没法点"允许 USB 调试"那个弹窗，adb 会一直
    unauthorized，日志抓不到。把 default.prop 里的 ro.adb.secure 改成 0，
    adb 就直接连上，不用授权。

用法：
    python patch_ramdisk.py boot.img
    python patch_ramdisk.py boot.img --prop ro.adb.secure=0 --prop persist.adb.notify=0
    python patch_ramdisk.py boot.img --check        # 只检查，不改

只用标准库，内核部分逐字节保持不变（只重写 ramdisk 段）。
"""

from __future__ import annotations

import argparse
import gzip
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootimg  # noqa: E402

PROP_FILES = ("default.prop", "prop.default")
DEFAULT_PROPS = [("ro.adb.secure", "0"), ("persist.adb.notify", "0")]


def pad4(buf: bytes) -> bytes:
    return buf + b"\x00" * ((-len(buf)) % 4)


def align(buf: bytes, page: int) -> bytes:
    return buf + b"\x00" * ((-len(buf)) % page)


def parse_cpio(cpio: bytes):
    """解析 newc 格式，返回 [[name, 110字节头, 数据], ...]"""
    entries = []
    pos = 0
    while pos + 110 <= len(cpio) and cpio[pos:pos + 6] in (b"070701", b"070702"):
        hdr = cpio[pos:pos + 110]
        namesize = int(hdr[94:102], 16)
        filesize = int(hdr[54:62], 16)
        name = cpio[pos + 110:pos + 110 + namesize - 1].decode("utf-8", "replace")
        dstart = pos + 110 + namesize
        dstart += (-dstart) % 4
        entries.append([name, hdr, cpio[dstart:dstart + filesize]])
        pos = dstart + filesize
        pos += (-pos) % 4
        if name == "TRAILER!!!":
            break
    return entries


def build_cpio(entries) -> bytes:
    out = b""
    for name, hdr, data in entries:
        h = bytearray(hdr)
        h[54:62] = b"%08X" % len(data)
        out += bytes(h) + name.encode("utf-8") + b"\x00"
        out = pad4(out)
        out += data
        out = pad4(out)
    return out


def decompress_ramdisk(rd: bytes) -> bytes:
    if rd[:2] != b"\x1f\x8b":
        raise SystemExit("ramdisk 不是 gzip（头部 %s），本脚本不处理" % rd[:2].hex())
    d = zlib.decompressobj(31)
    try:
        return d.decompress(rd) + d.flush()
    except zlib.error:
        return gzip.decompress(rd)


def set_props(text: str, props) -> tuple[str, list[str]]:
    """在属性文本里设置键值，返回 (新文本, 改动说明)"""
    lines = text.splitlines()
    changes = []
    for key, value in props:
        found = False
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith(key + "="):
                old = s.split("=", 1)[1]
                if old != value:
                    lines[i] = "%s=%s" % (key, value)
                    changes.append("%s: %s -> %s" % (key, old, value))
                found = True
                break
        if not found:
            lines.append("%s=%s" % (key, value))
            changes.append("%s: 新增 = %s" % (key, value))
    return "\n".join(lines) + "\n", changes


def main() -> int:
    ap = argparse.ArgumentParser(description="修改 boot.img 里 ramdisk 的属性")
    ap.add_argument("image", help="boot.img 路径")
    ap.add_argument("--prop", action="append", default=None,
                    metavar="KEY=VALUE", help="要设置的属性，可重复")
    ap.add_argument("--check", action="store_true", help="只检查，不修改")
    ap.add_argument("--out", default=None, help="另存为新文件（默认原地改）")
    args = ap.parse_args()

    props = DEFAULT_PROPS
    if args.prop:
        props = []
        for p in args.prop:
            if "=" not in p:
                raise SystemExit("--prop 需要 KEY=VALUE 形式: %s" % p)
            k, v = p.split("=", 1)
            props.append((k.strip(), v.strip()))

    data = open(args.image, "rb").read()
    h = bootimg.parse_header(data)
    layout = bootimg.layout(data, h)
    page = h["page_size"] if isinstance(h, dict) else h.page_size
    koff, ksize, _ = layout["kernel"]
    roff, rsize = layout["ramdisk"]
    kernel = data[koff:koff + ksize]
    ramdisk = data[roff:roff + rsize]

    cpio = decompress_ramdisk(ramdisk)
    entries = parse_cpio(cpio)
    if not entries:
        raise SystemExit("ramdisk 里没解析出 cpio 条目")

    targets = [e for e in entries if e[0] in PROP_FILES]
    if not targets:
        raise SystemExit("ramdisk 里没有 %s，无法改属性" % "/".join(PROP_FILES))

    if args.check:
        for e in targets:
            txt = e[2].decode("utf-8", "replace")
            for key, _ in props:
                for line in txt.splitlines():
                    if line.strip().startswith(key + "="):
                        print("  %s: %s" % (e[0], line.strip()))
        return 0

    all_changes = []
    for e in targets:
        txt = e[2].decode("utf-8", "replace")
        new_txt, changes = set_props(txt, props)
        if changes:
            e[2] = new_txt.encode("utf-8")
            all_changes.extend("%s: %s" % (e[0], c) for c in changes)

    if not all_changes:
        print("属性已是目标值，没有改动")
        return 0

    new_cpio = build_cpio(entries)
    new_rd = gzip.compress(new_cpio, 9, mtime=0)

    hdr = bytearray(data[:1632] if len(data) >= 1632 else data[:page])
    struct.pack_into("<I", hdr, 16, len(new_rd))

    img = bytearray()
    img += align(bytes(hdr), page)
    img += kernel
    img += b"\x00" * ((-len(img)) % page)
    img += new_rd
    img += b"\x00" * ((-len(img)) % page)

    out = args.out or args.image
    open(out, "wb").write(bytes(img))

    # ---- 自检 ----
    chk = open(out, "rb").read()
    h2 = bootimg.parse_header(chk)
    l2 = bootimg.layout(chk, h2)
    if l2["kernel"][1] != ksize or chk[l2["kernel"][0]:l2["kernel"][0] + ksize] != kernel:
        raise SystemExit("自检失败：内核段被改动了")
    c2 = parse_cpio(decompress_ramdisk(chk[l2["ramdisk"][0]:l2["ramdisk"][0] + l2["ramdisk"][1]]))
    for key, value in props:
        hit = False
        for e in c2:
            if e[0] in PROP_FILES and ("%s=%s" % (key, value)) in e[2].decode("utf-8", "replace"):
                hit = True
        if not hit:
            raise SystemExit("自检失败：%s=%s 没写进去" % (key, value))

    print("改动：")
    for c in all_changes:
        print("  " + c)
    print("输出: %s（%d 字节，内核段未变，自检通过）" % (out, len(chk)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
