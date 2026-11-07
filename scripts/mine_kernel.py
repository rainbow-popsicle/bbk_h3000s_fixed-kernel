#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从一个内核镜像（boot.img 或裸 Image / Image.gz）里挖出"这台机器到底编进了什么"。

为什么需要：厂商不给你 defconfig，但内核镜像本身是诚实的——
里面留着 Linux 版本、编译时的源码树绝对路径、屏驱动注册名、触摸驱动日志、
以及板级 compatible。这些信息足以判断"换内核时缺哪些驱动"。

用法：
    python mine_kernel.py <boot.img | Image | Image.gz> [--json]

注意：结果基于字符串匹配，是**线索**不是结论。最终以 dmesg 为准。
"""

import argparse
import json
import re
import sys
import zlib

import bootimg

STR_RE = re.compile(rb"[\x20-\x7e]{4,80}")

# MTK LCM 驱动名的常见形态：小写+数字+下划线，以 _vdo / _cmd 结尾再接屏厂
LCM_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*_(?:vdo|cmd)(?:_[a-z0-9]+)*$")
LCM_HINT = ("lcm", "LCM", "Register lcm", "disp_lcm")

TOUCH_MARKS = ("touch panel driver init", "touch panel driver exit",
               "gt9", "GT9", "ft5", "FT5", "nt36", "synaptics", "Synaptics",
               "himax", "Himax", "goodix", "Goodix", "chipone", "ilitek")

SRC_ROOT_RE = re.compile(r"/[A-Za-z0-9_./\-]+?/kernel-[0-9]+\.[0-9]+/")
FDT_COMPAT_RE = re.compile(r"^(mediatek|mtk),[A-Za-z0-9_\-]+$")


def load_payload(path):
    """接受 boot.img / 裸 Image / Image.gz，返回 (解压后的镜像, 说明)。"""
    blob = open(path, "rb").read()
    note = ""
    if blob[:8] == bootimg.ANDROID_MAGIC:
        hdr = bootimg.parse_header(blob)
        lay = bootimg.layout(blob, hdr)
        koff, ksize, kind = lay["kernel"]
        blob = blob[koff:koff + ksize]
        note = "boot.img，内核段 %d 字节（%s）" % (ksize, kind)
    if blob[:2] == b"\x1f\x8b":
        d = zlib.decompressobj(31)
        try:
            raw = d.decompress(blob) + d.flush()
            if raw:
                return raw, (note + "，gzip 解压后 %d 字节" % len(raw)).lstrip("，")
        except zlib.error:
            pass
    return blob, (note or "裸镜像 %d 字节" % len(blob))


def lcm_names_by_proximity(image, window=768):
    """在 LCM 日志锚点附近找驱动名。

    单靠命名规则会把 battery_cmd / msdc_cmd_done 这类内核符号全捞进来；
    真正可靠的线索是"紧挨着 LCM 注册日志"的那几个串——原厂屏驱动名
    nt35523_fhd_vdo_boe 就是这么找到的。
    """
    # 内核里是 [KERNEL/LCM]，LK(bootloader) 里是 [LK/LCM]，两处都要认
    anchors = [b"Register lcm driver", b"[KERNEL/LCM]", b"[LK/LCM]",
               b"disp_lcm.c", b"LCM: ", b"lcm_driver_list",
               b"lcm_init(", b"lcm_set_util_funcs"]
    found = {}
    for anchor in anchors:
        start = 0
        while True:
            i = image.find(anchor, start)
            if i < 0:
                break
            start = i + len(anchor)
            chunk = image[max(0, i - window):i + window]
            for m in STR_RE.finditer(chunk):
                s = m.group(0).decode("latin-1").strip()
                if not (6 <= len(s) <= 48):
                    continue
                if not re.match(r"^[A-Za-z0-9_]+$", s):
                    continue
                if s.lower().startswith(("lcm", "disp", "kernel", "register", "ddp")):
                    continue
                if "_" not in s and len(s) < 10:
                    continue
                found[s] = found.get(s, 0) + 1
    return [s for s, _ in sorted(found.items(), key=lambda kv: -kv[1])]


def mine(image):
    text = image.decode("latin-1")
    strings = [m.group(0).decode("latin-1") for m in STR_RE.finditer(image)]

    result = {
        "linux_version": None,
        "source_root": None,
        "project": None,
        "lcm_candidates": [],
        "touch_marks": [],
        "board_compatibles": [],
        "has_ikconfig": (b"IKCFG_ST" in image),
        "size": len(image),
    }

    i = text.find("Linux version ")
    if i >= 0:
        end = text.find("\x00", i)
        raw = text[i:end if end > 0 else i + 200]
        result["linux_version"] = raw.strip("\n\r\x00 ")

    roots = {}
    for m in SRC_ROOT_RE.finditer(text):
        roots[m.group(0)] = roots.get(m.group(0), 0) + 1
    if roots:
        result["source_root"] = max(roots, key=roots.get)
        parts = result["source_root"].strip("/").split("/")
        if len(parts) >= 2:
            result["project"] = parts[-2]

    uniq = sorted(set(strings))
    # 首选：LCM 日志锚点邻近的串；再用命名规则做补充（标注为低置信）
    prox = lcm_names_by_proximity(image)
    pattern = [s for s in uniq
               if LCM_RE.match(s) and len(s) < 48
               and re.search(r"(vdo|cmd)_[a-z]{2,}$", s)]
    result["lcm_candidates"] = prox
    result["lcm_low_confidence"] = [s for s in pattern if s not in prox][:10]
    result["board_compatibles"] = sorted({s for s in uniq if FDT_COMPAT_RE.match(s)})
    marks = []
    for s in uniq:
        if any(t in s for t in TOUCH_MARKS) and len(s) < 90:
            marks.append(s)
    result["touch_marks"] = marks[:20]
    return result


def render(r):
    print("镜像大小      : %d 字节" % r["size"])
    print("内核版本      : %s" % (r["linux_version"] or "未找到"))
    print("厂商源码树    : %s" % (r["source_root"] or "未找到（可能被 strip）"))
    print("工程名        : %s" % (r["project"] or "—"))
    print("内嵌 .config  : %s" % ("有" if r["has_ikconfig"] else "没有"))
    print()
    print("屏驱动候选（LCM 日志邻近，高置信 %d 个）:" % len(r["lcm_candidates"]))
    for s in r["lcm_candidates"]:
        print("   ", s)
    if r["lcm_low_confidence"]:
        print("   （低置信补充：%s）" % ", ".join(r["lcm_low_confidence"][:6]))
    print()
    print("触摸相关字符串（前 20）:")
    for s in r["touch_marks"]:
        print("   ", s)
    print()
    print("板级 compatible（%d）:" % len(r["board_compatibles"]))
    for s in r["board_compatibles"][:20]:
        print("   ", s)
    print()
    print("提示：以上是基于字符串的线索。确认某个驱动是否真的被注册，")
    print("      最可靠的是设备 dmesg 里的 'Register lcm driver' / 'tpd' 日志。")


def main(argv=None):
    ap = argparse.ArgumentParser(description="从内核镜像里挖板级信息（启发式）")
    ap.add_argument("image", help="boot.img / Image / Image.gz")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    try:
        image, note = load_payload(args.image)
    except bootimg.BootImgError as exc:
        print("解析失败：%s" % exc, file=sys.stderr)
        return 2
    print("输入          : %s（%s）" % (args.image, note))
    result = mine(image)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        render(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
