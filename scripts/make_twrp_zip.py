#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 twrp-dump/ 打成可刷的 zip，并保证那几个"上机才会发现"的细节是对的：

  - update-binary 必须是 **LF** 换行；CRLF 会让 shebang 变成 "#!/sbin/sh\\r"，
    recovery 直接报 "not found"，而且日志里看不出为什么
  - update-binary 要带可执行权限位（0755），部分 TWRP 会检查
  - 两个必要条目必须在 META-INF/com/google/android/ 下

用法：python scripts/make_twrp_zip.py [--out out/s3pro-twrp-dump.zip]
"""

import argparse
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
SRC = os.path.join(ROOT, "twrp-dump")

REQUIRED = [
    "META-INF/com/google/android/update-binary",
    "META-INF/com/google/android/updater-script",
]
EXECUTABLE = {"META-INF/com/google/android/update-binary"}


def collect(src=SRC):
    files = []
    for dirpath, _dirnames, filenames in os.walk(src):
        for name in filenames:
            if name.lower().endswith(".md"):
                continue          # 说明文档不进可刷包
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, src).replace(os.sep, "/")
            files.append((rel, full))
    return sorted(files)


def build(out_path, src=SRC):
    files = collect(src)
    missing = [r for r in REQUIRED if r not in {f[0] for f in files}]
    if missing:
        raise SystemExit("缺少必要文件：%s" % missing)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, full in files:
            with open(full, "rb") as f:
                data = f.read()
            if rel in EXECUTABLE:
                # 统一成 LF：Windows 上被改成 CRLF 会直接导致刷不进去
                data = data.replace(b"\r\n", b"\n")
                info = zipfile.ZipInfo(rel)
                info.external_attr = (0o100755 << 16)
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, data)
            else:
                zf.writestr(rel, data)
    return out_path, [f[0] for f in files]


def verify(path):
    """刷之前先自检一遍：换成别的机器/编辑器也能立刻发现问题。"""
    problems = []
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        for req in REQUIRED:
            if req not in names:
                problems.append("缺少 %s" % req)
        if "META-INF/com/google/android/update-binary" in names:
            info = zf.getinfo("META-INF/com/google/android/update-binary")
            data = zf.read(info)
            if not data.startswith(b"#!/sbin/sh"):
                problems.append("update-binary 开头不是 #!/sbin/sh")
            if b"\r\n" in data:
                problems.append("update-binary 含 CRLF（上机会报 not found）")
            mode = (info.external_attr >> 16) & 0o777
            if mode & 0o111 == 0:
                problems.append("update-binary 没有可执行位（当前 %o）" % mode)
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description="打包 TWRP 模块")
    ap.add_argument("--out", default=os.path.join(ROOT, "out", "s3pro-twrp-dump.zip"))
    args = ap.parse_args(argv)

    path, names = build(args.out)
    print("已生成 %s（%d 个文件）" % (path, len(names)))
    for n in names:
        print("   ", n)

    problems = verify(path)
    if problems:
        print("\n自检失败：", file=sys.stderr)
        for p in problems:
            print("   " + p, file=sys.stderr)
        return 1
    print("\n自检通过：必要条目齐全、LF 换行、带可执行位")
    return 0


if __name__ == "__main__":
    sys.exit(main())
