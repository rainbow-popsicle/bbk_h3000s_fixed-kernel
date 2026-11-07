import struct, re, sys

dtb = open(r"work\real-parts\kernel.dtb", "rb").read()
magic, totalsize, off_struct, off_strings, off_rsvmap, version, lastcomp, bootcpu, size_strings, size_struct = \
    struct.unpack_from(">10I", dtb, 0)
print("DTB: %d 字节, version=%d, strings=%d, struct=%d" % (totalsize, version, size_strings, size_struct))

strings = dtb[off_strings:off_strings + size_strings]
names = [s.decode("utf-8", "replace") for s in strings.split(b"\x00") if s]

print("字符串条目: %d" % len(names))

KEYWORDS = ("lcm", "LCM", "panel", "touch", "gt9", "ft5", "nt36", "synaptics",
            "goodix", "himax", "novatek", "ili", "chipone", "sensor", "bbk",
            "eebbk", "s3", "h3000", "gsl", "mstar")

hits = [n for n in names if any(k in n for k in KEYWORDS)]
print("\n--- 与屏/触摸/板型相关的字符串（%d 条）---" % len(hits))
for n in sorted(set(hits)):
    print("  ", n)

# 从 struct 块里找 compatible 值（property 名 "compatible" 后面跟的字符串）
comp = []
for m in re.finditer(rb"compatible\x00", dtb[off_struct:off_struct + size_struct]):
    pass

# 更靠谱：直接扫整个 DTB 的可打印串，挑含逗号的（compatible 风格）
allstr = set()
for m in re.finditer(rb"[\x20-\x7e]{6,60}", dtb):
    s = m.group(0).decode("latin-1")
    if "," in s and s.count(",") <= 2:
        allstr.add(s)

board = [s for s in allstr if any(k in s.lower() for k in
                                  ("mediatek,mt8", "mt8173", "mt8176", "bbk", "h3000", "huashan"))]
print("\n--- 板级 compatible（%d 条）---" % len(board))
for s in sorted(board):
    print("  ", s)
