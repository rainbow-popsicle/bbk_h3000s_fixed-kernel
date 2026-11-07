import sys, zlib

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
dtb = d.unused_data

print("boot 分区镜像: %d 字节 (16MB)，实际使用到 %d" % (len(data), L["end"]))
print("内核 gzip 流 : %d 字节" % (ksize - len(dtb)))
print("追加 DTB     : %d 字节" % len(dtb))
print("解压后 Image : %d 字节" % len(img))
print()
print("分区余量     : %d 字节 (%.1f MB)" % (len(data) - L["end"], (len(data) - L["end"]) / 1048576.0))
print("若新内核 <= 原内核 + %.1f MB，boot 分区就装得下" % ((len(data) - L["end"]) / 1048576.0))
