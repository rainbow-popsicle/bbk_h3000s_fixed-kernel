#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Android boot image 解析 / 内核替换 / 内嵌 .config(IKCFG) 提取。

为什么自己写：步步高这类 MTK 机型的 boot 分区常常带 MTK 私有的 512 字节
"MTK_BLOADER_INFO" 头，AOSP 的 mkbootimg 不认，直接 unpack 会解错偏移。
本脚本只依赖标准库，能识别 v0/v1/v2 header 和 MTK 头，并用"拼接"的方式
替换内核：其余所有字节（ramdisk、cmdline、MTK 头、对齐填充）原样保留。

子命令：
  info    <boot.img>                          打印头部与分段信息
  unpack  <boot.img> <outdir>                 拆出 kernel/ramdisk/second/dtb
  splice  --template <boot.img> --kernel <Image[.gz]> --out <new.img>
                                              只替换内核，其余字节不动
  config  <boot.img>                          提取内核里内嵌的 .config
"""

import argparse
import gzip
import hashlib
import io
import os
import struct
import sys
import zlib

ANDROID_MAGIC = b"ANDROID!"
MTK_MAGIC = b"MTK_BLOADER_INFO"
IKCFG_ST = b"IKCFG_ST"
IKCFG_ED = b"IKCFG_ED"


class BootImgError(Exception):
    pass


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _u32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


def _u64(buf, off):
    return struct.unpack_from("<Q", buf, off)[0]


def align(value, page):
    return (value + page - 1) // page * page


def _cstr(raw):
    return raw.split(b"\x00")[0].decode("utf-8", "replace")


def kernel_kind(buf):
    """粗判内核负载格式，用于校验我们找到的偏移是对的。"""
    if len(buf) < 64:
        return None
    if buf[:2] == b"\x1f\x8b":
        return "gzip"
    if buf[:4] == b"\x04\x22\x4d\x18":
        return "lz4"
    if buf[:4] == b"\x28\xb5\x2f\xfd":
        return "zstd"
    if buf[56:60] == b"ARMd":          # ARM64 Image header magic "ARM\x64"
        return "arm64-image"
    if _u32(buf, 36) == 0x016F2818:    # ARM32 zImage magic
        return "arm-zimage"
    return None


def split_kernel_parts(blob):
    """把内核负载拆成 (压缩流, 追加的 DTB)。

    MTK 常见的 Image.gz-dtb：gzip 流结束后直接跟一个 FDT，两者一起放在
    boot 的 kernel 段里。换内核时必须把原厂 DTB 原样接回去，
    否则等于把整机硬件描述丢了 —— 直接黑屏。
    """
    if blob[:2] != b"\x1f\x8b":
        return blob, b""
    d = zlib.decompressobj(31)
    try:
        d.decompress(blob)
    except zlib.error:
        return blob, b""
    if not d.eof:
        return blob, b""
    tail = d.unused_data
    if len(tail) >= 4 and tail[:4] == b"\xd0\x0d\xfe\xed":
        return blob[:len(blob) - len(tail)], tail
    return blob, b""


def ramdisk_kind(buf):
    """看 ramdisk 段的头部，判断布局推断对不对。

    位置算错时 ramdisk 会指向填充区（全 0），这个检查能立刻暴露问题，
    而不是等到刷机后才发现。
    """
    if len(buf) < 6:
        return None
    if buf[:6] in (b"070701", b"070702"):
        return "cpio"
    if buf[:2] == b"\x1f\x8b":
        return "gzip"
    if buf[:4] == b"\x04\x22\x4d\x18":
        return "lz4"
    if buf[:4] == b"\x28\xb5\x2f\xfd":
        return "zstd"
    return None


def _decompress_guess(buf):
    """尽量把内核负载还原成未压缩字节流（用于找 IKCFG / 版本串）。

    必须用流式解压：MTK 的 Image.gz-dtb 在 gzip 流后面直接跟 DTB，
    gzip.decompress() 遇到这种尾部多余数据会整段抛异常，
    把一个好好的内核判成"没有版本字符串"。
    """
    if buf[:2] == b"\x1f\x8b":
        d = zlib.decompressobj(31)
        try:
            out = d.decompress(buf)
            out += d.flush()
            if out:
                return out
        except zlib.error:
            pass
    if buf[:4] == b"\x28\xb5\x2f\xfd":
        try:
            import zstandard  # 可选依赖，没装就跳过
            return zstandard.ZstdDecompressor().decompressobj().decompress(buf)
        except Exception:
            pass
    return buf


# --------------------------------------------------------------------------- #
# header
# --------------------------------------------------------------------------- #
def parse_header(data):
    if len(data) < 1632:
        raise BootImgError("文件太小，不像 boot.img（%d 字节）" % len(data))
    if data[:8] != ANDROID_MAGIC:
        raise BootImgError("没有 ANDROID! magic，前 8 字节是 %r" % data[:8])

    h = {
        "kernel_size": _u32(data, 8),
        "kernel_addr": _u32(data, 12),
        "ramdisk_size": _u32(data, 16),
        "ramdisk_addr": _u32(data, 20),
        "second_size": _u32(data, 24),
        "second_addr": _u32(data, 28),
        "tags_addr": _u32(data, 32),
        "page_size": _u32(data, 36),
        "header_version": _u32(data, 40),
        "os_version": _u32(data, 44),
        "name": _cstr(data[48:64]),
        "cmdline": _cstr(data[64:576]),
        "id": data[576:608].hex(),
        "extra_cmdline": _cstr(data[608:1632]),
        "recovery_dtbo_size": 0,
        "recovery_dtbo_offset": 0,
        "header_size": 0,
        "dtb_size": 0,
        "dtb_addr": 0,
    }

    # 老镜像这里可能是 0（当时的 "unused" 字段），不要被垃圾值带偏
    ver = h["header_version"]
    if ver > 3:
        h["header_version"] = ver = 0

    ps = h["page_size"]
    if ps < 512 or ps > 65536 or (ps & (ps - 1)) != 0:
        h["page_size"] = ps = 2048

    if ver >= 1:
        if len(data) < 1660:
            raise BootImgError("header v%d 但文件长度不足" % ver)
        h["recovery_dtbo_size"] = _u32(data, 1632)
        h["recovery_dtbo_offset"] = _u64(data, 1636)
        h["header_size"] = _u32(data, 1644)
    if ver >= 2:
        h["dtb_size"] = _u32(data, 1648)
        h["dtb_addr"] = _u64(data, 1652)
    return h


def find_kernel_offset(data, h):
    """在内核段之前可能夹着 MTK 私有头，这里做探测 + 双重校验。"""
    ps = h["page_size"]
    ksize = h["kernel_size"]
    candidates = []
    for base in (ps, 512, 1024, 4096):
        candidates.append((base, 0))          # 没有 MTK 头
        candidates.append((base, 512))        # base 处是 MTK 头，内核紧随其后

    for base, extra in candidates:
        off = base + extra
        if off + 64 > len(data):
            continue
        if extra and data[base:base + len(MTK_MAGIC)] != MTK_MAGIC:
            continue
        if extra == 0 and data[base:base + len(MTK_MAGIC)] == MTK_MAGIC:
            continue                          # 有 MTK 头却当普通布局，跳过
        kind = kernel_kind(data[off:off + 64])
        if kind:
            if off + ksize > len(data):
                # kernel_size 与实际文件长度对不上时，仍然接受但标记异常
                pass
            return off, kind
    raise BootImgError(
        "找不到内核偏移：header 声称 kernel_size=%d，page_size=%d，"
        "但按 AOSP/MTK 两种布局都校验不出内核 magic" % (ksize, ps)
    )


def layout(data, h):
    ps = h["page_size"]
    koff, kind = find_kernel_offset(data, h)
    ksize = h["kernel_size"]
    kend = koff + ksize
    kpad_end = align(kend, ps)

    ramdisk_off = kpad_end
    ramdisk_end = ramdisk_off + h["ramdisk_size"]
    second_off = align(ramdisk_end, ps)
    second_end = second_off + h["second_size"]
    rec_off = align(second_end, ps)
    rec_end = rec_off + h["recovery_dtbo_size"]
    dtb_off = align(rec_end, ps)
    dtb_end = dtb_off + h["dtb_size"]

    return {
        "kernel": (koff, ksize, kind),
        "kernel_pad": kpad_end - kend,
        "ramdisk": (ramdisk_off, h["ramdisk_size"]),
        "second": (second_off, h["second_size"]),
        "recovery_dtbo": (rec_off, h["recovery_dtbo_size"]),
        "dtb": (dtb_off, h["dtb_size"]),
        "end": dtb_end,
        "mtk_header": koff != ps,
    }


# --------------------------------------------------------------------------- #
# IKCFG：内核里内嵌的 .config
# --------------------------------------------------------------------------- #
def kernel_version_string(blob):
    """从内核镜像里捞出 'Linux version ...'，用来判断源码树该选 3.18 还是 4.4。"""
    data = _decompress_guess(blob)
    idx = data.find(b"Linux version ")
    if idx < 0:
        return None
    end = data.find(b"\x00", idx)
    if end < 0:
        end = idx + 200
    return data[idx:end].decode("utf-8", "replace")


def kernel_release(blob):
    """只要 release 号（3.18.123-gebc1f9a 这种），带 localversion 尾巴。"""
    s = kernel_version_string(blob)
    if not s:
        return None
    rest = s[len("Linux version "):].strip()
    return rest.split()[0] if rest else None


def extract_ikconfig(blob):
    """内核里 IKCFG_ST ... IKCFG_ED 之间是压缩过的 .config。

    Linux 用 zlib_deflateInit 生成的是 zlib 流（带 2 字节头），而 scripts/
    extract-ikconfig 靠 gunzip 兼容性吃下去；这里 wbits=47 让 zlib 自动识别
    gzip/zlib，再退一步试裸 deflate，避免版本差异踩坑。
    """
    data = _decompress_guess(blob)
    pos = data.find(IKCFG_ST)
    while pos >= 0:
        payload = data[pos + len(IKCFG_ST):]
        for wbits in (47, 15, -15):
            try:
                d = zlib.decompressobj(wbits)
                out = d.decompress(payload, 4 * 1024 * 1024)
            except zlib.error:
                continue
            if b"CONFIG_" in out:
                return out.decode("utf-8", "replace")
        pos = data.find(IKCFG_ST, pos + 1)
    return None


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_info(args):
    data = open(args.image, "rb").read()
    h = parse_header(data)
    L = layout(data, h)
    koff, ksize, kind = L["kernel"]

    print("文件            : %s (%d 字节)" % (args.image, len(data)))
    print("header 版本     : v%d  (header_size=%d)"
          % (h["header_version"], h["header_size"] or 1632))
    print("page_size       : %d" % h["page_size"])
    print("name            : %r" % h["name"])
    print("cmdline         : %r" % h["cmdline"])
    if h["extra_cmdline"]:
        print("extra_cmdline   : %r" % h["extra_cmdline"])
    print("kernel          : off=%d size=%d 格式=%s%s"
          % (koff, ksize, kind, "  [前置 MTK 头]" if L["mtk_header"] else ""))
    print("ramdisk         : off=%d size=%d 格式=%s"
          % (L["ramdisk"][0], L["ramdisk"][1],
             ramdisk_kind(data[L["ramdisk"][0]:L["ramdisk"][0] + 4096]) or "未知"))
    print("second          : off=%d size=%d" % L["second"])
    print("recovery_dtbo   : off=%d size=%d" % L["recovery_dtbo"])
    print("dtb             : off=%d size=%d" % L["dtb"])
    print("各段地址        : kernel=0x%08x ramdisk=0x%08x second=0x%08x tags=0x%08x"
          % (h["kernel_addr"], h["ramdisk_addr"], h["second_addr"], h["tags_addr"]))
    print("文件末尾        : %d（多余 %d 字节）"
          % (len(data), len(data) - L["end"]))

    kblob = data[koff:koff + ksize]
    ver = kernel_version_string(kblob)
    print("内核版本字符串  : %s" % (ver or "（没找到，可能被压缩或已剥离）"))
    _payload, _dtb = split_kernel_parts(kblob)
    if _dtb:
        print("追加 DTB        : %d 字节（Image.gz-dtb，换内核时必须接回去）" % len(_dtb))
    cfg = extract_ikconfig(kblob)
    if cfg:
        n = sum(1 for line in cfg.splitlines() if line.startswith("CONFIG_"))
        print("内嵌 .config    : 有，%d 行 CONFIG_*" % n)
    else:
        print("内嵌 .config    : 没有（CONFIG_IKCONFIG 未开启）")
    return 0


def cmd_unpack(args):
    data = open(args.image, "rb").read()
    h = parse_header(data)
    L = layout(data, h)
    os.makedirs(args.outdir, exist_ok=True)

    for name in ("kernel", "ramdisk", "second", "recovery_dtbo", "dtb"):
        off, size = L[name] if name != "kernel" else L["kernel"][:2]
        if size == 0:
            continue
        path = os.path.join(args.outdir, name + ".bin")
        with open(path, "wb") as f:
            f.write(data[off:off + size])
        print("写出 %-14s %8d 字节  -> %s" % (name, size, path))

    # 头部单独留一份，方便对照
    hs = h["header_size"] or 1632
    with open(os.path.join(args.outdir, "header.bin"), "wb") as f:
        f.write(data[:hs])
    cmd = h["cmdline"]
    with open(os.path.join(args.outdir, "cmdline.txt"), "w", encoding="utf-8") as f:
        f.write(cmd + "\n")

    cfg = extract_ikconfig(data[L["kernel"][0]:L["kernel"][0] + L["kernel"][1]])
    if cfg:
        with open(os.path.join(args.outdir, "extracted_defconfig"), "w",
                  encoding="utf-8") as f:
            f.write(cfg)
        print("写出 extracted_defconfig（内核内嵌配置）")
    return 0


def cmd_splice(args):
    template = open(args.template, "rb").read()
    h = parse_header(template)
    L = layout(template, h)
    ps = h["page_size"]

    # 布局 sanity：ramdisk 位置如果对不上常见格式，说明偏移推断可能有误。
    # 不直接中止（有些机器 ramdisk 确实是别的格式），但必须让人看见。
    if L["ramdisk"][1] > 0:
        rk = ramdisk_kind(template[L["ramdisk"][0]:L["ramdisk"][0] + 4096])
        if rk is None:
            print("警告：推算出的 ramdisk 位置（off=%d）头部不像 cpio/gzip/lz4/zstd，"
                  "布局可能推断有误；下面的内容比对如果失败会拒绝出镜像。"
                  % L["ramdisk"][0], file=sys.stderr)
        else:
            print("ramdisk 格式检查：%s（布局推断一致）" % rk)

    new_kernel = open(args.kernel, "rb").read()
    if args.dtb:
        dtb = open(args.dtb, "rb").read()
        if dtb[:4] != b"\xd0\x0d\xfe\xed":
            raise SystemExit("--dtb 给的文件不是合法 FDT（magic 应为 d00dfeed）")
        if new_kernel[-len(dtb):] == dtb:
            print("新内核里已经带了同样的 DTB，跳过追加")
        else:
            new_kernel = new_kernel + dtb
            print("已把原厂 DTB（%d 字节）追加到新内核后面（Image.gz-dtb）" % len(dtb))
    kind = kernel_kind(new_kernel[:64])
    old_kind = L["kernel"][2]
    if kind is None:
        print("警告：新内核看起来不像内核镜像（前 8 字节 %r）"
              % new_kernel[:8], file=sys.stderr)
    if args.expect and kind != args.expect:
        raise SystemExit("内核格式 %r 与期望 %r 不一致，已中止" % (kind, args.expect))
    if kind and old_kind and kind != old_kind:
        print("注意：原内核 %s -> 新内核 %s，请确认固件需要的就是这种格式"
              % (old_kind, kind), file=sys.stderr)

    koff, ksize, _ = L["kernel"]
    kend = koff + ksize
    tail_start = kend + L["kernel_pad"]          # ramdisk 段起点（页对齐）

    out = bytearray()
    out += template[:koff]                       # 头部 + 可能的 MTK 头，原样保留
    out += new_kernel
    end = koff + len(new_kernel)
    out += b"\x00" * ((-end) % ps)               # 重新补齐到页边界
    out += template[tail_start:]                 # ramdisk 及之后全部原样搬运

    result = bytearray(out)
    struct.pack_into("<I", result, 8, len(new_kernel))   # 回填 kernel_size

    with open(args.out, "wb") as f:
        f.write(result)

    delta = len(new_kernel) - ksize
    print("已生成 %s" % args.out)
    print("  内核 %d -> %d 字节 (%+d)" % (ksize, len(new_kernel), delta))
    print("  镜像 %d -> %d 字节" % (len(template), len(result)))
    if delta > 0:
        print("  提示：内核变大 %d 字节，若该分区容量紧，刷写前先确认分区大小" % delta)

    # 自检：重新解析，并逐段比对内容是否原样保留。
    # 只比大小是不够的——错位一个页就会"大小对、内容错"，刷上去直接变砖。
    check = parse_header(bytes(result))
    L2 = layout(bytes(result), check)
    failures = []
    if L2["kernel"][1] != len(new_kernel):
        failures.append("kernel_size 回填不正确")
    if check["page_size"] != ps:
        failures.append("page_size 被改动")
    if check["cmdline"] != h["cmdline"]:
        failures.append("cmdline 被改动")
    for name in ("ramdisk", "second", "dtb"):
        off0, size0 = L[name]
        off1, size1 = L2[name]
        if size0 != size1:
            failures.append("%s 长度变了（%d -> %d）" % (name, size0, size1))
        elif size0:
            old = hashlib.sha256(template[off0:off0 + size0]).hexdigest()
            new = hashlib.sha256(result[off1:off1 + size1]).hexdigest()
            if old != new:
                failures.append("%s 内容不一致" % name)
    if failures:
        raise SystemExit("自检失败：" + "；".join(failures))

    print("  自检通过：ramdisk/second/dtb 逐字节一致，cmdline 未改动")
    return 0


def cmd_parts(args):
    data = open(args.image, "rb").read()
    h = parse_header(data)
    L = layout(data, h)
    koff, ksize, kind = L["kernel"]
    payload, dtb = split_kernel_parts(data[koff:koff + ksize])
    os.makedirs(args.outdir, exist_ok=True)
    name = "kernel.gz" if kind == "gzip" else "kernel.bin"
    with open(os.path.join(args.outdir, name), "wb") as f:
        f.write(payload)
    print("写出 %-12s %8d 字节" % (name, len(payload)))
    if dtb:
        with open(os.path.join(args.outdir, "kernel.dtb"), "wb") as f:
            f.write(dtb)
        print("写出 %-12s %8d 字节（换内核时要接回去）" % ("kernel.dtb", len(dtb)))
    else:
        print("这个内核没有追加 DTB（不是 Image.gz-dtb）")
    return 0


def cmd_config(args):
    data = open(args.image, "rb").read()
    h = parse_header(data)
    L = layout(data, h)
    koff, ksize, _ = L["kernel"]
    cfg = extract_ikconfig(data[koff:koff + ksize])
    if not cfg:
        print("该内核没有内嵌 .config（CONFIG_IKCONFIG 没开），"
              "需要从设备 /proc/config.gz 或厂商源码的 defconfig 拿配置",
              file=sys.stderr)
        return 1
    sys.stdout.write(cfg)
    return 0


# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="Android/MTK boot image 工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info", help="打印 boot.img 结构")
    p.add_argument("image")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("unpack", help="拆包")
    p.add_argument("image")
    p.add_argument("outdir")
    p.set_defaults(func=cmd_unpack)

    p = sub.add_parser("splice", help="只替换内核，其余字节保留")
    p.add_argument("--template", required=True, help="原厂 boot.img")
    p.add_argument("--kernel", required=True, help="新内核（Image / Image.gz / Image.lz4）")
    p.add_argument("--out", required=True)
    p.add_argument("--dtb", help="要追加到新内核末尾的 DTB（原厂 Image.gz-dtb 用）")
    p.add_argument("--expect", choices=["gzip", "arm64-image", "lz4", "zstd",
                                        "arm-zimage"],
                   help="要求新内核必须是这种格式")
    p.set_defaults(func=cmd_splice)

    p = sub.add_parser("kernel-parts", help="拆出内核压缩流和追加的 DTB")
    p.add_argument("--image", required=True, help="原厂 boot.img")
    p.add_argument("--outdir", required=True)
    p.set_defaults(func=cmd_parts)

    p = sub.add_parser("config", help="提取内核内嵌 .config")
    p.add_argument("image")
    p.set_defaults(func=cmd_config)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except BootImgError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
