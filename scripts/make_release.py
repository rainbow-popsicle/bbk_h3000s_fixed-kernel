#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「内核修复 + 系统修复」组装成一个可发布的整包。

产物（默认放 out/release-<日期>/）
----------------------------------
    boot.img                     内核（build.sh 的产物，含全部内核修复）
    s3pro-systemfix.zip          TWRP 可刷的系统层修复包
    INSTALL.md                   安装步骤（同 docs/固化成包.md）
    SHA256SUMS.txt               所有产物的校验值
    release-info.txt             版本/日期/字节数/清单摘要
    ↑ 以上再打成一个 s3pro-h3000s-los151-<日期>.zip 方便传

为什么分两个包而不是一个
------------------------
* boot.img 要刷到 boot 分区（TWRP: Install Image，或 SP Flash Tool 只勾 BOOT）
* 系统修复要往 /system 写文件（TWRP: Install zip，或 adb 脚本）
两者刷法不同，合成一个 zip 会让"刷哪一档"变得含糊，容易刷错分区。

用法
----
    python scripts/make_release.py --boot out/boot.img
    python scripts/make_release.py --boot out/boot.img --skip-systemfix
    python scripts/make_release.py --boot out/boot.img --out out/release-test
"""

import argparse
import datetime
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)


def sha256_of(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def git_rev():
    for cmd in (["git", "rev-parse", "--short", "HEAD"],
                ["git", "log", "-1", "--format=%h %s"]):
        try:
            p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
        except OSError:
            pass
    return "(无 git 信息)"


def build_systemfix(out_zip):
    """调用 pack_systemfix 生成系统修复包。"""
    p = subprocess.run(
        [sys.executable, os.path.join(HERE, "pack_systemfix.py"),
         "--pack", "--out", out_zip],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.stdout:
        sys.stdout.write(p.stdout)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        raise SystemExit("生成系统修复包失败")
    return out_zip


RELEASE_README = """# S3 Pro (h3000s) LineageOS 15.1 —— 修复整包

本包把之前所有修复固化成了两个可复现的产物：

## 1. boot.img —— 内核

含以下内核修复（全部由 `scripts/build.sh` 在编译时打补丁，可复现）：

| # | 修复 | 实现位置 |
|---|---|---|
| K1 | 底部三大金刚键 | `patch_gt911_keys.py`（GT911 触摸按键）+ 属性 `qemu.hw.mainkeys=1` |
| K2 | 电量百分比（一半） | `patch_battery_qmax.py` / `patch_battery_soc.py` / `patch_battery_fgadc.py` |
| K3 | 加速度计（自动旋转） | `patch_sensors.py`（LIS3DH） |
| -  | 屏 / 触摸 / 亮度 | `patch_lcm_*.py` / `patch_gt911_touch.py` / `patch_cm36686_*.py` |
| -  | binder（hwbinder/vndbinder） | `patch_binder_modern.py` |
| -  | VDEC/VENC 中断重映射 | `patch_vcodec_irq.py` |

## 2. s3pro-systemfix.zip —— 系统层

含 66 个文件（33.6 MB 未压缩）+ 4 条属性补丁，清单见包内 `manifest.txt`，
每个文件都带 MD5，安装时逐个校验。

对应你的系统修复记录：

| # | 修复 | 由本包覆盖 |
|---|---|---|
| S1 | 补库修复开机问题 | `vendor/lib/hw/*.so`、`libui_shim.so`、`libmemtrack_GL.so` |
| S2 | 补 wifi 库 | 本机不需要（wifi 走 firmware + 内核驱动，已在 boot.img 内） |
| S3 | 添加 ndp 时间同步 | `etc/timesync.sh` + `vendor/etc/init/timesync.rc` |
| S4 | webview 更新 | ⚠️ 走 APK 安装，不在本包内（见下） |
| S5 | app 访问不到文件 | 属性/权限配合，见 INSTALL.md |
| S6 | 音频修复 | `vendor/lib64/hw/audio.primary.mt8173.so` |
| S7 | 对齐设备指纹 | 属性 `ro.build.fingerprint` / `ro.build.description` |

## 安装顺序

1. 刷 `boot.img`（内核）
2. 刷 `s3pro-systemfix.zip`（系统层，TWRP）
3. 重启，等开机完成

详细步骤（含回退方法）见 `INSTALL.md`。

## 注意

* 系统修复包安装时会**先备份**被覆盖的原文件到
  `/sdcard/s3pro-fix-backup/<时间戳>/`，刷错可自己回退。
* 重刷 ROM 会让系统层修复全部丢失，但仓库里的 `systemfix/` 仍在，
  重新跑一次 `pack_systemfix.py --pack` 就能再生成同样的包。
* **视频硬解在本移植上不可用**（框架与厂商 OMX 参数协议不兼容），
  详见 `docs/视频硬解调查全记录.md`。
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="组装发布整包")
    ap.add_argument("--boot", required=True, help="boot.img 路径")
    ap.add_argument("--out", default=None, help="输出目录")
    ap.add_argument("--skip-systemfix", action="store_true",
                    help="不重新生成系统修复包（复用 out/s3pro-systemfix.zip）")
    ap.add_argument("--no-zip", action="store_true", help="只生成目录，不额外打 zip")
    args = ap.parse_args(argv)

    boot = os.path.abspath(args.boot)
    if not os.path.isfile(boot):
        raise SystemExit("找不到 boot.img: %s" % boot)
    if os.path.getsize(boot) == 0:
        raise SystemExit("boot.img 是空文件: %s" % boot)

    stamp = datetime.datetime.now().strftime("%Y%m%d")
    outdir = args.out or os.path.join(ROOT, "out", "release-%s" % stamp)
    os.makedirs(outdir, exist_ok=True)

    print("=== 组装发布包 -> %s" % outdir)

    # 1) boot.img
    dst_boot = os.path.join(outdir, "boot.img")
    shutil.copy2(boot, dst_boot)
    print("  boot.img                %10d 字节" % os.path.getsize(dst_boot))

    # 2) 系统修复包
    sfx = os.path.join(outdir, "s3pro-systemfix.zip")
    if args.skip_systemfix:
        src = os.path.join(ROOT, "out", "s3pro-systemfix.zip")
        if not os.path.isfile(src):
            raise SystemExit("--skip-systemfix 但 out/s3pro-systemfix.zip 不存在")
        shutil.copy2(src, sfx)
        print("  s3pro-systemfix.zip     %10d 字节（复用）" % os.path.getsize(sfx))
    else:
        build_systemfix(sfx)
        print("  s3pro-systemfix.zip     已生成")

    # 3) 文档
    install_md = os.path.join(ROOT, "docs", "固化成包.md")
    if os.path.isfile(install_md):
        shutil.copy2(install_md, os.path.join(outdir, "INSTALL.md"))
        print("  INSTALL.md              已复制")
    with open(os.path.join(outdir, "README.md"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(RELEASE_README)

    # 4) 校验值
    targets = [dst_boot, sfx]
    lines = []
    for p in targets:
        lines.append("%s  %s" % (sha256_of(p), os.path.basename(p)))
    with open(os.path.join(outdir, "SHA256SUMS.txt"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print("  SHA256SUMS.txt          已写入")

    # 5) 版本信息
    info = [
        "S3 Pro (h3000s) LineageOS 15.1 修复整包",
        "生成时间: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "内核仓库版本: %s" % git_rev(),
        "",
        "产物:",
    ]
    for p in targets:
        info.append("  %-24s %10d 字节  sha256=%s"
                    % (os.path.basename(p), os.path.getsize(p), sha256_of(p)))
    info.append("")
    info.append("系统修复包内容: %d 个文件 + 属性补丁，详见包内 manifest.txt"
                % _count_manifest())
    with open(os.path.join(outdir, "release-info.txt"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write("\n".join(info) + "\n")
    print("  release-info.txt        已写入")

    # 6) 打成一个大 zip
    if not args.no_zip:
        bundle = os.path.join(ROOT, "out",
                              "s3pro-h3000s-los151-%s.zip" % stamp)
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in sorted(os.listdir(outdir)):
                full = os.path.join(outdir, name)
                if os.path.isfile(full):
                    zf.write(full, name)
        print("  发布包: %s（%.1f MB）"
              % (bundle, os.path.getsize(bundle) / 1024.0 / 1024.0))
        return bundle
    return outdir


def _count_manifest():
    try:
        import pack_systemfix as P
        return len(P.load_manifest())
    except Exception:
        return -1


if __name__ == "__main__":
    main()
