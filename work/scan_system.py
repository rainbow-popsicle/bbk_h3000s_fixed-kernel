import os, sys, time, zlib

sys.path.insert(0, "scripts")
import bootimg

IMGDIR = r"C:\Users\momei\Downloads\s3pro_crack_v2.1\images"

KEYS = [b"ro.build.version.release", b"ro.build.version.sdk",
        b"ro.build.fingerprint", b"ro.product.model", b"ro.build.display.id",
        b"ro.treble.enabled", b"ro.build.version.security_patch"]

CHUNK = 64 * 1024 * 1024
OVERLAP = 4096


def scan(path, keys):
    found = {}
    size = os.path.getsize(path)
    t0 = time.time()
    with open(path, "rb") as f:
        pos = 0
        while pos < size:
            f.seek(pos)
            buf = f.read(CHUNK)
            if not buf:
                break
            for k in keys:
                if k in found:
                    continue
                i = buf.find(k)
                while i >= 0:
                    chunk = buf[i:i + 160]
                    # 取 "key=value" 那一段
                    line = chunk.split(b"\x00")[0].split(b"\n")[0]
                    if b"=" in line:
                        found[k] = line.decode("utf-8", "replace").strip()
                        break
                    i = buf.find(k, i + 1)
            pos += CHUNK - OVERLAP
    dt = time.time() - t0
    return found, size, dt


print("=" * 70)
print("扫描 system.img（%s）" % "5.0GB")
found, size, dt = scan(os.path.join(IMGDIR, "system.img"), KEYS)
print("耗时 %.1fs，扫完 %d 字节" % (dt, size))
for k in KEYS:
    v = found.get(k.decode())
    print("  %-32s %s" % (k.decode(), v or "（没找到）"))

print()
print("=" * 70)
print("recovery.img 体检")
rpath = os.path.join(IMGDIR, "recovery.img")
data = open(rpath, "rb").read()
try:
    h = bootimg.parse_header(data)
    L = bootimg.layout(data, h)
    koff, ksize, kind = L["kernel"]
    k = data[koff:koff + ksize]
    d = zlib.decompressobj(31)
    img = d.decompress(k)
    img += d.flush()
    i = img.find(b"Linux version ")
    ver = img[i:img.find(b"\x00", i)].decode("utf-8", "replace") if i >= 0 else "未找到"
    print("  内核: %s" % ver)
    print("  格式: %s  kernel_size=%d  尾部DTB=%d 字节" % (kind, ksize, len(d.unused_data)))
    print("  内嵌 config: %s" % ("有" if bootimg.extract_ikconfig(k) else "无"))
    print("  cmdline: %r" % h["cmdline"])
except bootimg.BootImgError as e:
    print("  解析失败:", e)
