import sys, zlib, struct

sys.path.insert(0, "scripts")
import bootimg

PATH = r"C:\Users\momei\Downloads\s3pro_crack_v2.1\images\boot.img"
data = open(PATH, "rb").read()
h = bootimg.parse_header(data)
L = bootimg.layout(data, h)
koff, ksize, _ = L["kernel"]
k = data[koff:koff + ksize]

d = zlib.decompressobj(31)
img = d.decompress(k)
img += d.flush()
tail = d.unused_data

print("gzip 流占用: %d 字节" % (ksize - len(tail)))
print("尾部剩余  : %d 字节" % len(tail))
print("尾部前 4 字节: %s (FDT magic 应为 d0 0d fe ed)" % tail[:4].hex())

if len(tail) >= 40:
    magic, totalsize, off_struct, off_strings, off_rsvmap, version, lastcomp, bootcpu, size_strings, size_struct = \
        struct.unpack_from(">10I", tail, 0)
    print("  magic      = 0x%08x" % magic)
    print("  totalsize  = %d" % totalsize)
    print("  version    = %d" % version)
    print("  size_strings = %d  size_struct = %d" % (size_strings, size_struct))
    ok = magic == 0xD00DFEED
    print("  是合法 DTB :", ok)
    if ok:
        # 在 strings 块里找主板 compatible
        strings = tail[off_strings:off_strings + size_strings]
        for kw in (b"mediatek,mt8176", b"mediatek,mt8173", b"mt8176", b"mt8173",
                   b"bbk", b"eebbk", b"s3pro"):
            if kw in strings or kw in tail[:totalsize]:
                print("  DTB 里出现:", kw.decode())
        # 列出 struct 块里的前若干字符串属性值
        import re
        found = set()
        for m in re.finditer(rb"[\x20-\x7e]{6,40}", strings):
            s = m.group(0)
            if b"," in s and (b"mediatek" in s or b"mt8" in s or b"bbk" in s.lower()):
                found.add(s)
        for s in sorted(found)[:20]:
            print("   compatible:", s.decode("utf-8", "replace"))

# 同时确认 Image 里的内核版本与编译器
i = img.find(b"Linux version ")
print()
print("内核:", img[i:img.find(b"\x00", i)].decode("utf-8", "replace"))
print("Image 是否带 ARMd:", img[56:60] == b"ARMd", "总大小 %d" % len(img))
