import os, sys, gzip, zlib
sys.path.insert(0, os.path.join("tests"))
import test_bootimg as t
cfg = b"CONFIG_ARM64=y\nCONFIG_MTK_MT8176=y\nCONFIG_X=1\n"
def kern(ver):
    body = b"Linux version " + ver + b" (bbk@build) #1 SMP PREEMPT\x00" + b"\x00" * 500
    body += b"IKCFG_ST" + zlib.compress(cfg) + b"IKCFG_ED"
    return gzip.compress(body)
open("work/ps-test/plain.img", "wb").write(t.make_boot(kern(b"3.18.123-gebc1f9a"), b"R" * 3000))
open("work/ps-test/mtk.img", "wb").write(t.make_boot(kern(b"4.4.146"), b"R" * 3000, mtk=True))
print("生成完成")
