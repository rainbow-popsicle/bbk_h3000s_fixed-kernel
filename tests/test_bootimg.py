#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bootimg.py 的本地自测：合成镜像做往返验证，不需要真机。

覆盖：
  1) 标准 AOSP 布局（v0 / v2 header）
  2) MTK 私有头布局（header 页之后 512 字节 MTK_BLOADER_INFO，再是内核）
  3) splice：换一个大小不同的内核后，ramdisk 及其后所有字节必须一字不差
  4) IKCFG 提取：zlib 流、gzip 流、以及内核整体被 gzip 压缩的情况
"""

import gzip
import os
import random
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import bootimg  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILED.append(name)


def pad(data, page):
    return data + b"\x00" * ((-len(data)) % page)


def gz_kernel(size, seed):
    rnd = random.Random(seed)
    body = bytes(rnd.getrandbits(8) for _ in range(size))
    return gzip.compress(body)


def make_boot(kernel, ramdisk, page=2048, ver=0, dtb=b"", mtk=False,
              cmdline=b"console=ttyMT0,921600n1 root=/dev/ram"):
    hdr = bytearray(1632)
    hdr[0:8] = b"ANDROID!"
    struct.pack_into("<I", hdr, 8, len(kernel))
    struct.pack_into("<I", hdr, 12, 0x40080000)
    struct.pack_into("<I", hdr, 16, len(ramdisk))
    struct.pack_into("<I", hdr, 20, 0x43000000)
    struct.pack_into("<I", hdr, 24, 0)
    struct.pack_into("<I", hdr, 28, 0x40F00000)
    struct.pack_into("<I", hdr, 32, 0x40000100)
    struct.pack_into("<I", hdr, 36, page)
    struct.pack_into("<I", hdr, 40, ver)
    hdr[48:52] = b"test"
    hdr[64:64 + len(cmdline)] = cmdline
    if ver >= 2:
        hdr += bytearray(1660 - 1632)
        struct.pack_into("<I", hdr, 1644, 1660)
        struct.pack_into("<I", hdr, 1648, len(dtb))
        struct.pack_into("<Q", hdr, 1652, 0x4FF00000)

    out = bytearray(pad(bytes(hdr), page))
    if mtk:
        mtk_hdr = bytearray(512)
        mtk_hdr[0:16] = b"MTK_BLOADER_INFO"
        mtk_hdr[16:18] = b"_v"
        out += mtk_hdr
    out += kernel
    out = bytearray(pad(bytes(out), page))
    out += ramdisk
    out = bytearray(pad(bytes(out), page))
    if ver >= 2:
        out += dtb
        out = bytearray(pad(bytes(out), page))
    return bytes(out)


def test_plain():
    print("[1] 标准 AOSP 布局 v0")
    k = gz_kernel(5000, 1)
    r = b"R" * 3000 + b"ramdisk"
    img = make_boot(k, r)
    h = bootimg.parse_header(img)
    L = bootimg.layout(img, h)
    check("page_size", h["page_size"] == 2048, h["page_size"])
    check("kernel 偏移=page", L["kernel"][0] == 2048, L["kernel"][0])
    check("kernel 格式=gzip", L["kernel"][2] == "gzip", L["kernel"][2])
    check("kernel 长度", L["kernel"][1] == len(k), L["kernel"][1])
    check("ramdisk 页对齐", L["ramdisk"][0] % 2048 == 0, L["ramdisk"][0])
    check("ramdisk 内容", img[L["ramdisk"][0]:L["ramdisk"][0] + len(r)] == r)


def test_mtk():
    print("[2] MTK 私有头布局")
    k = gz_kernel(7000, 2)
    r = b"Q" * 4096
    img = make_boot(k, r, mtk=True)
    h = bootimg.parse_header(img)
    L = bootimg.layout(img, h)
    check("识别出 MTK 头", L["mtk_header"] is True)
    check("kernel 偏移=page+512", L["kernel"][0] == 2048 + 512, L["kernel"][0])
    check("kernel 长度", L["kernel"][1] == len(k), L["kernel"][1])
    check("ramdisk 内容", img[L["ramdisk"][0]:L["ramdisk"][0] + len(r)] == r)


def test_v2_dtb():
    print("[3] v2 header + dtb 段")
    k = gz_kernel(4000, 3)
    r = b"D" * 2000
    dtb = b"\xd0\x0d\xfe\xed" + b"x" * 3000
    img = make_boot(k, r, ver=2, dtb=dtb)
    h = bootimg.parse_header(img)
    L = bootimg.layout(img, h)
    check("header_version=2", h["header_version"] == 2, h["header_version"])
    check("dtb 长度", L["dtb"][1] == len(dtb), L["dtb"][1])
    check("dtb 内容", img[L["dtb"][0]:L["dtb"][0] + len(dtb)] == dtb)


def test_splice(mtk):
    tag = "MTK" if mtk else "AOSP"
    print("[4] splice 内核替换（%s，内核变大）" % tag)
    old_k = gz_kernel(5000, 11)
    r = os.urandom(6000)
    img = make_boot(old_k, r, mtk=mtk)
    new_k = gz_kernel(9000, 12)

    tmp_t = os.path.join(HERE, "_t_%s.img" % tag)
    tmp_k = os.path.join(HERE, "_t_%s.kernel" % tag)
    tmp_o = os.path.join(HERE, "_t_%s.out" % tag)
    open(tmp_t, "wb").write(img)
    open(tmp_k, "wb").write(new_k)

    rc = bootimg.main(["splice", "--template", tmp_t, "--kernel", tmp_k,
                       "--out", tmp_o, "--expect", "gzip"])
    check("splice 返回 0", rc == 0, rc)

    out = open(tmp_o, "rb").read()
    h2 = bootimg.parse_header(out)
    L2 = bootimg.layout(out, h2)
    check("kernel_size 已回填", h2["kernel_size"] == len(new_k), h2["kernel_size"])
    check("新内核就位", out[L2["kernel"][0]:L2["kernel"][0] + len(new_k)] == new_k)
    check("ramdisk 页对齐", L2["ramdisk"][0] % 2048 == 0, L2["ramdisk"][0])
    check("ramdisk 字节不变",
          out[L2["ramdisk"][0]:L2["ramdisk"][0] + len(r)] == r)
    check("cmdline 不变", h2["cmdline"] == bootimg.parse_header(img)["cmdline"])
    if mtk:
        check("MTK 头仍在", out[2048:2064] == b"MTK_BLOADER_INFO")
    check("镜像总长对齐", len(out) % 2048 == 0, len(out) % 2048)

    for p in (tmp_t, tmp_k, tmp_o):
        os.remove(p)


def test_splice_shrink():
    print("[5] splice 内核变小")
    old_k = gz_kernel(9000, 21)
    r = b"z" * 3000
    img = make_boot(old_k, r)
    new_k = gz_kernel(3000, 22)
    tmp_t = os.path.join(HERE, "_s.img")
    tmp_k = os.path.join(HERE, "_s.kernel")
    tmp_o = os.path.join(HERE, "_s.out")
    open(tmp_t, "wb").write(img)
    open(tmp_k, "wb").write(new_k)
    rc = bootimg.main(["splice", "--template", tmp_t, "--kernel", tmp_k,
                       "--out", tmp_o])
    out = open(tmp_o, "rb").read()
    h2 = bootimg.parse_header(out)
    L2 = bootimg.layout(out, h2)
    check("rc=0", rc == 0, rc)
    check("变大也变小都对：kernel_size", h2["kernel_size"] == len(new_k))
    check("ramdisk 完整", out[L2["ramdisk"][0]:L2["ramdisk"][0] + len(r)] == r)
    for p in (tmp_t, tmp_k, tmp_o):
        os.remove(p)


def test_ikconfig():
    print("[6] IKCFG .config 提取")
    cfg = ("CONFIG_ARM64=y\nCONFIG_MTK_MT8176=y\n"
           "CONFIG_X=1\n").encode()

    zlib_blob = zlib.compress(cfg)
    k1 = b"\x00" * 128 + bootimg.IKCFG_ST + zlib_blob + bootimg.IKCFG_ED
    got = bootimg.extract_ikconfig(k1)
    check("zlib 流", got is not None and "CONFIG_MTK_MT8176" in got)

    gz_blob = gzip.compress(cfg)
    k2 = b"\x00" * 64 + bootimg.IKCFG_ST + gz_blob + bootimg.IKCFG_ED
    got = bootimg.extract_ikconfig(k2)
    check("gzip 流", got is not None and "CONFIG_ARM64=y" in got)

    # 内核整体是 Image.gz 的情况
    k3_plain = b"\x00" * 128 + bootimg.IKCFG_ST + zlib_blob + bootimg.IKCFG_ED
    got = bootimg.extract_ikconfig(gzip.compress(k3_plain))
    check("内核被 gzip 包裹", got is not None and "CONFIG_X=1" in got)

    check("没有 IKCFG 时返回 None",
          bootimg.extract_ikconfig(b"\x00" * 999) is None)

    # 通过 CLI 从整张 boot.img 里取
    img = make_boot(gzip.compress(k3_plain), b"r" * 100)
    tmp = os.path.join(HERE, "_c.img")
    open(tmp, "wb").write(img)
    rc = bootimg.main(["config", tmp])
    os.remove(tmp)
    check("CLI config 返回 0", rc == 0, rc)


def test_version():
    print("[7] 内核版本字符串")
    ver = (b"Linux version 3.18.123 (bbk@buildhost) "
           b"(gcc version 4.9.x) #1 SMP PREEMPT Thu Aug 1 2024")
    k1 = ver + b"\x00" * 32
    got = bootimg.kernel_version_string(k1)
    check("明文", got is not None and got.startswith("Linux version 3.18.123"), got)
    got = bootimg.kernel_version_string(gzip.compress(k1))
    check("gzip 包裹", got is not None and "3.18.123" in got, got)
    check("没有时返回 None", bootimg.kernel_version_string(b"\x00" * 64) is None)


def _mktmp(name):
    """在 tests/ 下建一个干净目录。

    不用 tempfile.mkdtemp：它在 Windows 上建出来的目录带着 0700 语义的 ACL，
    在某些沙箱/ACL 环境下往里面写文件会被拒绝（Errno 13）。
    """
    import shutil
    d = os.path.join(HERE, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


def test_analyze():
    print("[8] dump 分析器")
    import contextlib
    import io
    import shutil

    import analyze_dump

    d = _mktmp("_dump_treble")
    try:
        # Treble + 3.18 的情况
        ver = b"Linux version 3.18.123-gebc1f9a (bbk@b) #1 SMP\x00"
        k = gzip.compress(ver + b"\x00" * 2000)
        open(os.path.join(d, "boot_a.img"), "wb").write(make_boot(k, b"R" * 3000))
        open(os.path.join(d, "vendor.img"), "wb").write(b"\x00" * (3 * 1024 * 1024))
        open(os.path.join(d, "vbmeta.img"), "wb").write(b"\x00" * 4096)
        open(os.path.join(d, "super.img"), "wb").write(b"\x00" * 8192)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = analyze_dump.main([d])
        out = buf.getvalue()
        check("返回 0", rc == 0, rc)
        check("识别 3.18", "3.18" in out and "cappu" in out)
        check("识别 Treble", "[Treble]" in out)
        check("识别 A/B", "A/B" in out)
        check("识别动态分区", "super" in out)

        # 非 Treble + 4.4 的情况
        d2 = _mktmp("_dump_notreble")
        ver4 = b"Linux version 4.4.146 (bbk@b) #1 SMP\x00"
        k4 = gzip.compress(ver4 + b"\x00" * 2000)
        open(os.path.join(d2, "boot.img"), "wb").write(make_boot(k4, b"R" * 3000))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = analyze_dump.main([d2])
        out2 = buf.getvalue()
        check("4.4 判定", "没有公开的 4.4 树" in out2, out2[-300:])
        check("非 Treble 判定", "走不通" in out2)
        shutil.rmtree(d2, ignore_errors=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def fake_arm64_image(size=6000, ver=b"3.18.123"):
    """伪造一个未压缩的 ARM64 Image：偏移 56 处是 'ARM\\x64' magic。"""
    body = bytearray(size)
    body[56:60] = b"ARMd"
    v = b"Linux version " + ver + b" (bbk@b) #1 SMP\x00"
    body[1000:1000 + len(v)] = v
    return bytes(body)


def test_arch_mismatch():
    print("[9] 未压缩 ARM64 Image / 格式不一致")
    raw = fake_arm64_image()
    r = b"r" * 2048
    img = make_boot(raw, r)

    h = bootimg.parse_header(img)
    L = bootimg.layout(img, h)
    check("识别 arm64-image", L["kernel"][2] == "arm64-image", L["kernel"][2])
    check("版本串可读", bootimg.kernel_release(raw) == "3.18.123",
          bootimg.kernel_release(raw))

    # 9a. raw -> raw
    t = os.path.join(HERE, "_raw.img")
    k = os.path.join(HERE, "_raw.kernel")
    o = os.path.join(HERE, "_raw.out")
    open(t, "wb").write(img)
    open(k, "wb").write(fake_arm64_image(9000, b"3.18.130"))
    rc = bootimg.main(["splice", "--template", t, "--kernel", k, "--out", o])
    out = open(o, "rb").read()
    L2 = bootimg.layout(out, bootimg.parse_header(out))
    check("raw->raw 成功", rc == 0, rc)
    check("raw->raw 内核就位", L2["kernel"][2] == "arm64-image")
    check("raw->raw ramdisk 不变",
          out[L2["ramdisk"][0]:L2["ramdisk"][0] + len(r)] == r)
    for p in (t, k, o):
        os.remove(p)

    # 9b. --expect 不匹配时应拒绝
    t = os.path.join(HERE, "_x.img")
    k = os.path.join(HERE, "_x.kernel")
    o = os.path.join(HERE, "_x.out")
    open(t, "wb").write(img)
    open(k, "wb").write(gz_kernel(3000, 41))       # gzip，但期望 arm64-image
    rejected = False
    try:
        bootimg.main(["splice", "--template", t, "--kernel", k, "--out", o,
                      "--expect", "arm64-image"])
    except SystemExit:
        rejected = True
    check("--expect 不匹配时拒绝", rejected)
    for p in (t, k, o):
        if os.path.exists(p):
            os.remove(p)


def test_ramdisk_kind():
    print("[10] ramdisk 格式识别")
    cpio = b"070701" + b"\x00" * 2000
    img = make_boot(gz_kernel(4000, 51), cpio)
    h = bootimg.parse_header(img)
    L = bootimg.layout(img, h)
    got = bootimg.ramdisk_kind(img[L["ramdisk"][0]:L["ramdisk"][0] + 4096])
    check("识别 cpio", got == "cpio", got)
    check("gzip ramdisk", bootimg.ramdisk_kind(b"\x1f\x8b\x08\x00" + b"x" * 20) == "gzip")
    check("全零时返回 None", bootimg.ramdisk_kind(b"\x00" * 64) is None)

    # 布局可疑时，splice 仍然要成功（内容比对才是最终裁判），但不能崩
    t = os.path.join(HERE, "_rk.img")
    k = os.path.join(HERE, "_rk.kernel")
    o = os.path.join(HERE, "_rk.out")
    open(t, "wb").write(img)
    open(k, "wb").write(gz_kernel(6000, 52))
    rc = bootimg.main(["splice", "--template", t, "--kernel", k, "--out", o])
    out = open(o, "rb").read()
    L2 = bootimg.layout(out, bootimg.parse_header(out))
    check("cpio ramdisk 拼接成功", rc == 0, rc)
    check("cpio ramdisk 原样保留",
          out[L2["ramdisk"][0]:L2["ramdisk"][0] + len(cpio)] == cpio)
    for p in (t, k, o):
        os.remove(p)


def fake_fdt(size=4096):
    """最小的 FDT 头 + 填充，够 magic 校验用。"""
    hdr = bytearray(size)
    hdr[0:4] = b"\xd0\x0d\xfe\xed"
    struct.pack_into(">I", hdr, 4, size)      # totalsize
    struct.pack_into(">I", hdr, 20, 17)       # version
    return bytes(hdr)


def test_gz_dtb():
    print("[11] Image.gz-dtb（内核后面追加 DTB）")
    img_only = gz_kernel(5000, 61)
    dtb = fake_fdt()
    kernel = img_only + dtb

    payload, got_dtb = bootimg.split_kernel_parts(kernel)
    check("拆出压缩流", payload == img_only)
    check("拆出 DTB", got_dtb == dtb, len(got_dtb))
    check("普通 gzip 无 DTB", bootimg.split_kernel_parts(img_only)[1] == b"")
    check("非 gzip 原样返回",
          bootimg.split_kernel_parts(b"\x00" * 100)[1] == b"")

    # 尾部带 DTB 时，版本串/配置仍然要能读出来（真机上踩过这个坑：
    # gzip.decompress 遇到尾部多余数据会整段抛异常）
    ver_blob = (b"Linux version 3.18.35 (cp@ubuntusrv2) #1 SMP\x00" + b"\x00" * 200)
    with_dtb = gzip.compress(ver_blob) + dtb
    got_ver = bootimg.kernel_version_string(with_dtb)
    check("尾部带 DTB 仍能读出内核版本",
          got_ver is not None and "3.18.35" in got_ver, got_ver)

    r = b"R" * 2048
    template = make_boot(kernel, r)

    # 用原内核 + 原 DTB 重打，应当逐字节还原
    t = os.path.join(HERE, "_dtb.img")
    k = os.path.join(HERE, "_dtb.kernel")
    d = os.path.join(HERE, "_dtb.dtb")
    o = os.path.join(HERE, "_dtb.out")
    open(t, "wb").write(template)
    open(k, "wb").write(img_only)
    open(d, "wb").write(dtb)
    rc = bootimg.main(["splice", "--template", t, "--kernel", k,
                       "--dtb", d, "--out", o])
    out = open(o, "rb").read()
    check("带 DTB 拼接成功", rc == 0, rc)
    check("与原镜像逐字节一致", out == template,
          "长度 %d vs %d" % (len(out), len(template)))
    check("kernel_size 含 DTB",
          bootimg.parse_header(out)["kernel_size"] == len(img_only) + len(dtb))

    # 非 FDT 的 --dtb 必须被拒绝（在清理临时文件之前做）
    bad = os.path.join(HERE, "_bad.dtb")
    open(bad, "wb").write(b"not a dtb")
    rejected = False
    try:
        bootimg.main(["splice", "--template", t, "--kernel", k,
                      "--dtb", bad, "--out", o])
    except SystemExit:
        rejected = True
    check("非 FDT 的 --dtb 被拒绝", rejected)

    for p in (t, k, d, o, bad):
        if os.path.exists(p):
            os.remove(p)


def test_mine():
    print("[12] 内核信息挖掘（mine_kernel）")
    import mine_kernel

    # 注意：相邻字节串可以隐式拼接，但中间不能夹 b"\x00" * 300 这种表达式，
    # 否则就是语法错误（Python 的报错是 "Perhaps you forgot a comma?"，很误导）
    head = (b"Linux version 3.18.35 (cp@ubuntusrv2) #1 SMP PREEMPT\x00"
            b"/data/cp/H3000S-8176/kernel-3.18/init/main.c\x00"
            b"/data/cp/H3000S-8176/kernel-3.18/fs/namei.c\x00"
            b"LCM: Register lcm driver done\x00")
    tail = (b"nt35523_fhd_vdo_boe\x00"
            b"gpio_lcd_rst\x00"
            b"3GTP GT9 series touch panel driver init\x00"
            b"mediatek,goodix_touch\x00")
    body = head + b"\x00" * 300 + tail
    r = mine_kernel.mine(body)
    check("版本", "3.18.35" in (r["linux_version"] or ""), r["linux_version"])
    check("源码树", r["source_root"] == "/data/cp/H3000S-8176/kernel-3.18/",
          r["source_root"])
    check("工程名", r["project"] == "H3000S-8176", r["project"])
    check("LCM 邻近命中", "nt35523_fhd_vdo_boe" in r["lcm_candidates"],
          r["lcm_candidates"])
    check("触摸线索", any("GT9" in s for s in r["touch_marks"]))
    check("板级 compatible", "mediatek,goodix_touch" in r["board_compatibles"],
          r["board_compatibles"])
    check("无 config 判定", r["has_ikconfig"] is False)
    check("有 config 判定",
          mine_kernel.mine(body + bootimg.IKCFG_ST)["has_ikconfig"] is True)

    # load_payload 要能吃 boot.img / Image.gz / 裸镜像三种输入
    tmp = os.path.join(HERE, "_mine.bin")
    open(tmp, "wb").write(make_boot(gzip.compress(body), b"r" * 512))
    image, note = mine_kernel.load_payload(tmp)
    check("boot.img 输入", b"nt35523_fhd_vdo_boe" in image, note)
    open(tmp, "wb").write(gzip.compress(body))
    image, _ = mine_kernel.load_payload(tmp)
    check("Image.gz 输入", b"nt35523_fhd_vdo_boe" in image)
    open(tmp, "wb").write(body)
    image, _ = mine_kernel.load_payload(tmp)
    check("裸镜像输入", b"nt35523_fhd_vdo_boe" in image)
    os.remove(tmp)

    # LK(bootloader) 用的是 [LK/LCM] 日志前缀，也要能高置信命中
    lk_like = (b"[LK/LCM] lcm_init() enter\x00"
               b"platform/mt8173/disp_lcm.c\x00"
               b"nt35523_fhd_vdo_boe\x00")
    rk = mine_kernel.mine(lk_like)
    check("LK 镜像也能命中屏驱动名",
          "nt35523_fhd_vdo_boe" in rk["lcm_candidates"], rk["lcm_candidates"])


def main():
    test_plain()
    test_mtk()
    test_v2_dtb()
    test_splice(mtk=False)
    test_splice(mtk=True)
    test_splice_shrink()
    test_ikconfig()
    test_version()
    test_analyze()
    test_arch_mismatch()
    test_ramdisk_kind()
    test_gz_dtb()
    test_mine()
    print()
    if FAILED:
        print("失败 %d 项：%s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

