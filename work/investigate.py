import sys, zlib, os, struct

sys.path.insert(0, "scripts")
import bootimg

PATH = r"C:\Users\momei\Downloads\s3pro_crack_v2.1\images\boot.img"
data = open(PATH, "rb").read()
h = bootimg.parse_header(data)
L = bootimg.layout(data, h)
koff, ksize, kind = L["kernel"]
k = data[koff:koff + ksize]

print("=" * 60)
print("内核负载: off=%d size=%d kind=%s" % (koff, ksize, kind))
print("头部:", k[:8].hex())

d = zlib.decompressobj(31)
out = d.decompress(k)
try:
    out += d.flush()
except Exception:
    pass
print("解压后: %d 字节  eof=%s  未用尾部=%d" % (len(out), d.eof, len(d.unused_data)))
print("前 8 字节:", out[:8].hex())
print("offset 56 的 magic:", out[56:60], "（ARM64 Image 应为 b'ARMd'）")
print("offset 36 的 u32: 0x%08x（ARM32 zImage 应为 0x016f2818）"
      % struct.unpack_from("<I", out, 36)[0])

idx = out.find(b"Linux version ")
if idx >= 0:
    end = out.find(b"\x00", idx)
    print("版本:", out[idx:end].decode("utf-8", "replace"))
else:
    print("版本: 仍然没找到")

for marker in (b"CONFIG_IKCONFIG", b"IKCFG_ST", b"4123456789"):
    print("包含 %-16r: %s" % (marker, marker in out))

# ---------------------------------------------------------------- ramdisk
print()
print("=" * 60)
roff, rsize = L["ramdisk"]
r = data[roff:roff + rsize]
print("ramdisk: off=%d size=%d 头=%s" % (roff, rsize, r[:4].hex()))

rd = zlib.decompressobj(31)
rb = rd.decompress(r)
try:
    rb += rd.flush()
except Exception:
    pass
print("ramdisk 解压后: %d 字节 eof=%s" % (len(rb), rd.eof))

# 解析 newc cpio
names = []
pos = 0
while pos + 110 <= len(rb):
    if rb[pos:pos + 6] not in (b"070701", b"070702"):
        break
    try:
        namesize = int(rb[pos + 94:pos + 102], 16)
        filesize = int(rb[pos + 54:pos + 62], 16)
    except ValueError:
        break
    name = rb[pos + 110:pos + 110 + namesize - 1].decode("utf-8", "replace")
    data_start = pos + 110 + namesize
    data_start += (-data_start) % 4
    names.append((name, filesize, data_start))
    pos = data_start + filesize
    pos += (-pos) % 4
    if name == "TRAILER!!!":
        break

print("cpio 条目: %d" % len(names))
for n, sz, off in names[:40]:
    print("   %-40s %8d" % (n, sz))

blob = rb.decode("latin-1")
for kw in ("magisk", "Magisk", "MAGISK", "supersu", "SuperSU", "kitsune"):
    if kw in blob:
        print("!! ramdisk 里出现关键字:", kw)

# 找 prop 文件里的版本线索
for n, sz, off in names:
    if n.endswith("default.prop") or n.endswith("prop.default") or n.endswith("build.prop"):
        text = rb[off:off + sz].decode("latin-1")
        keep = [ln for ln in text.splitlines()
                if any(key in ln for key in ("ro.build.version", "ro.product", "ro.zygote",
                                             "ro.treble", "ro.secure", "ro.debuggable",
                                             "ro.sf.lcd", "ro.build.fingerprint"))]
        print()
        print("--- %s ---" % n)
        print("\n".join(keep) if keep else "(没有匹配的键)")
