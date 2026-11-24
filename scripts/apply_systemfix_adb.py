#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 adb 装系统层修复（不需要 TWRP）。

和 pack_systemfix.py 生成的 TWRP 包是同一套清单、同一套逻辑，
区别只是"谁来解包、谁来回拷文件"：这里用 adb 推送 + 设备端 shell 安装。

为什么需要两条路
----------------
* TWRP 包适合"没装 adb / 想要一键"的场景；
* adb 路适合"机器能开机、只想把修复补上"的场景，也能在改完立刻验证。

做了什么
--------
1. 校验本地 systemfix/files 与 manifest 的 MD5
2. 打 tar → 推到 /data/local/tmp → 解到临时目录
3. 逐个文件：备份原文件到 /sdcard/s3pro-fix-backup/<时间戳>/ → 覆盖 → chmod（不 chown，保留属主）→ 校验 MD5
4. 按 props.txt 改属性（只改这几个键）
5. 汇报成功/失败数，失败会列出来

不做：格式化、清数据、动 boot 分区。

用法
----
    python scripts/apply_systemfix_adb.py --adb "C:\\path\\to\\adb.exe"
    python scripts/apply_systemfix_adb.py --dry-run     # 只校验和列计划，不动机器
"""

import argparse
import os
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import pack_systemfix as P  # noqa: E402

REMOTE_TAR = "/data/local/tmp/s3pro-sfx.tar"
REMOTE_LIST = "/data/local/tmp/s3pro-sfx-list.txt"
REMOTE_WORK = "/data/local/tmp/s3pro-sfx"
WORKTMP = os.path.join(ROOT, "out", ".tmp")


def _tmp(name):
    """工作区内的临时文件路径（不用系统 temp：沙箱下可能不可写）"""
    os.makedirs(WORKTMP, exist_ok=True)
    return os.path.join(WORKTMP, name)

INSTALLER = r"""#!/system/bin/sh
# 设备端安装器：逻辑与 TWRP 包里的 update-binary 保持一致
PKG="__PKG__"
LIST="__LIST__"
BK="/sdcard/s3pro-fix-backup/$(date +%Y%m%d-%H%M%S)"

OK=0; BAD=0; BADLIST=""

install_file() {
    _mode="$1"; _md5="$2"; _rel="$3"
    _src="$PKG/$_rel"; _dst="/system/$_rel"
    [ -f "$_src" ] || { echo "  包内缺失: $_rel"; return 1; }
    if [ -f "$_dst" ]; then
        mkdir -p "$BK/$(dirname "$_rel")"
        cp -f "$_dst" "$BK/$_rel" 2>/dev/null
    fi
    mkdir -p "$(dirname "$_dst")" || { echo "  建目录失败: $_rel"; return 1; }
    cp -f "$_src" "$_dst" || { echo "  拷贝失败: $_rel"; return 1; }
    chmod "$_mode" "$_dst" 2>/dev/null
    # 故意不 chown：已存在文件保留原属主（wpa_supplicant 必须 root:shell）
    _got=$(md5sum "$_dst" 2>/dev/null | cut -d' ' -f1)
    [ "$_got" = "$_md5" ] || { echo "  校验不符: $_rel"; return 1; }
    return 0
}

apply_prop() {
    _file="$1"; _key="$2"; _val="$3"
    _p="/system/$_file"
    [ -f "$_p" ] || { echo "  跳过（文件不存在）: $_file"; return 0; }
    if grep -q "^${_key}=" "$_p"; then
        _old=$(grep "^${_key}=" "$_p" | head -1 | cut -d= -f2-)
        if [ "$_old" = "$_val" ]; then
            echo "  已是目标值: $_key"
            return 0
        fi
        sed -i "s|^${_key}=.*|${_key}=${_val}|" "$_p" || { echo "  失败: $_key"; return 1; }
        echo "  $_key: $_old -> $_val"
    else
        echo "${_key}=${_val}" >> "$_p" || { echo "  失败(追加): $_key"; return 1; }
        echo "  $_key: (新增) $_val"
    fi
    return 0
}

echo "=== 安装系统修复文件 ==="
while read -r _m _s _h _p; do
    [ -n "$_p" ] || continue
    if install_file "$_m" "$_h" "$_p"; then
        OK=$((OK+1))
    else
        BAD=$((BAD+1)); BADLIST="$BADLIST $_p"
    fi
done < "$LIST"
echo "  文件: OK=$OK 失败=$BAD"

echo ""
echo "=== 修正需要特殊属主的文件 ==="
OWNERS="__OWNERS__"
if [ -f "$OWNERS" ]; then
    while read -r _rel _owner; do
        [ -n "$_owner" ] || continue
        case "$_rel" in \#*) continue;; esac
        _dst="/system/$_rel"
        [ -e "$_dst" ] || { echo "  跳过（不存在）: $_rel"; continue; }
        if chown "$_owner" "$_dst" 2>/dev/null; then
            echo "  属主: $_rel -> $_owner"
        else
            echo "  属主设置失败: $_rel"
            BAD=$((BAD+1))
        fi
    done < "$OWNERS"
fi

echo ""
echo "=== 处理需要删除的文件 ==="
DELETES="__DELETES__"
if [ -f "$DELETES" ]; then
    while read -r _rel; do
        [ -n "$_rel" ] || continue
        case "$_rel" in \#*) continue;; esac
        _dst="/system/$_rel"
        if [ ! -e "$_dst" ]; then
            echo "  已不存在（OK）: $_rel"
            continue
        fi
        mkdir -p "$BK/$(dirname "$_rel")"
        cp -f "$_dst" "$BK/$_rel" 2>/dev/null
        if rm -f "$_dst"; then
            echo "  已删除: $_rel（已备份）"
        else
            echo "  删除失败: $_rel"
            BAD=$((BAD+1))
        fi
    done < "$DELETES"
fi

echo ""
echo "=== 应用属性补丁 ==="
# 属性清单由调用方写在 LIST 同目录的 props 文件里
PROPS="__PROPS__"
if [ -f "$PROPS" ]; then
    while read -r _f _k _v; do
        [ -n "$_k" ] || continue
        case "$_k" in \#*) continue;; esac
        apply_prop "$_f" "$_k" "$_v" || BAD=$((BAD+1))
    done < "$PROPS"
fi

sync
mount -o ro,remount /system >/dev/null 2>&1
echo ""
echo "=== 汇总 ==="
echo "  失败项: $BAD"
[ -n "$BADLIST" ] && echo "  失败文件:$BADLIST"
echo "  原文件备份: $BK"
exit 0
"""


def sh(adb, *args, check=True, quiet=False):
    cmd = [adb] + list(args)
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if not quiet and p.stdout:
        sys.stdout.write(p.stdout)
    if p.stderr and p.returncode != 0:
        sys.stderr.write(p.stderr)
    if check and p.returncode != 0:
        raise SystemExit("adb 失败: %s" % " ".join(cmd))
    return p


def adb_path(adb, path):
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description="用 adb 安装系统层修复")
    ap.add_argument("--adb", default="adb")
    ap.add_argument("--dry-run", action="store_true",
                    help="只校验本地文件并打印计划，不动机器")
    args = ap.parse_args(argv)

    items = P.load_manifest()
    props = P.load_props()
    deletes = P.load_delete()
    owners = P.load_owners()

    problems = P.verify_stage(items)
    if problems:
        print("本地暂存文件与清单不一致，中止：", file=sys.stderr)
        for x in problems[:20]:
            print("   " + x, file=sys.stderr)
        return 1

    total = sum(i[1] for i in items)
    print("计划：%d 个文件（%.1f MB）+ %d 条属性 + %d 项删除"
          % (len(items), total / 1024.0 / 1024.0, len(props), len(deletes)))

    if args.dry_run:
        print("\n[dry-run] 只列清单，不动机器。目标示例：")
        for m, s, h, rel in items[:8]:
            print("   %o  /system/%s" % (m, rel))
        print("   ... 共 %d 个" % len(items))
        print("\n[dry-run] 属性：")
        for f, k, v in props:
            print("   /system/%s  %s=%s" % (f, k, v[:50]))
        return 0

    # 1) 打包 + 推送
    tarpath = _tmp("files.tar")
    with tarfile.open(tarpath, "w") as tf:
        for m, s, h, rel in items:
            tf.add(os.path.join(P.STAGE, rel), arcname=rel)
    print("已打包 %.1f MB，开始推送到设备..."
          % (os.path.getsize(tarpath) / 1024.0 / 1024.0))

    sh(args.adb, "push", tarpath, REMOTE_TAR)
    os.remove(tarpath)

    # 2) 推清单
    listpath = _tmp("sfx-list.txt")
    with open(listpath, "w", encoding="utf-8", newline="\n") as f:
        for m, s, h, rel in items:
            f.write("%o %d %s %s\n" % (m, s, h, rel))
    sh(args.adb, "push", listpath, REMOTE_LIST)
    os.remove(listpath)

    # 3) 推属性清单
    proppath = _tmp("sfx-props.txt")
    with open(proppath, "w", encoding="utf-8", newline="\n") as f:
        for fp, k, v in props:
            f.write("%s %s %s\n" % (fp, k, v))
    remote_props = REMOTE_LIST + ".props"
    sh(args.adb, "push", proppath, remote_props)
    os.remove(proppath)

    # 3b) 推删除清单
    delpath = _tmp("sfx-delete.txt")
    with open(delpath, "w", encoding="utf-8", newline="\n") as f:
        for rel in deletes:
            f.write("%s\n" % rel)
    remote_dels = REMOTE_LIST + ".delete"
    sh(args.adb, "push", delpath, remote_dels)
    os.remove(delpath)

    # 3c) 推属主清单
    ownpath = _tmp("sfx-owners.txt")
    with open(ownpath, "w", encoding="utf-8", newline="\n") as f:
        for rel, ow in owners:
            f.write("%s %s\n" % (rel, ow))
    remote_owns = REMOTE_LIST + ".owners"
    sh(args.adb, "push", ownpath, remote_owns)
    os.remove(ownpath)

    # 4) 设备端：解包
    sh(args.adb, "shell",
       "rm -rf %s && mkdir -p %s && cd %s && tar xf %s"
       % (REMOTE_WORK, REMOTE_WORK, REMOTE_WORK, REMOTE_TAR))

    # 5) 设备端：挂载可写（失败就在这里报，不会写坏东西）
    mount_cmd = (
        "mount /system >/dev/null 2>&1; "
        "mount -o rw,remount /system >/dev/null 2>&1; "
        "touch /system/.sfx_w 2>/dev/null && rm -f /system/.sfx_w && echo WRITABLE "
        "|| echo READONLY"
    )
    r = sh(args.adb, "shell", mount_cmd, quiet=True)
    if "WRITABLE" not in r.stdout:
        print("!! /system 不可写。请先 adb root，并确认 /system 能 remount rw。",
              file=sys.stderr)
        sh(args.adb, "shell", "rm -rf %s %s %s" % (REMOTE_WORK, REMOTE_TAR,
                                                   REMOTE_LIST), check=False)
        return 1
    print("/system 可写 OK")

    # 6) 推安装器并执行
    inst = INSTALLER.replace("__PKG__", REMOTE_WORK)
    inst = inst.replace("__LIST__", REMOTE_LIST)
    inst = inst.replace("__PROPS__", remote_props)
    inst = inst.replace("__DELETES__", remote_dels)
    inst = inst.replace("__OWNERS__", remote_owns)
    instpath = _tmp("sfx-install.sh")
    with open(instpath, "w", encoding="utf-8", newline="\n") as f:
        f.write(inst.replace("\r\n", "\n"))
    remote_inst = "/data/local/tmp/s3pro-sfx-install.sh"
    sh(args.adb, "push", instpath, remote_inst)
    os.remove(instpath)

    print("\n开始安装（会先备份原文件）...\n")
    r = sh(args.adb, "shell", "sh %s" % remote_inst, check=False)

    # 7) 清理
    sh(args.adb, "shell",
       "rm -rf %s %s %s %s %s %s" % (REMOTE_WORK, REMOTE_TAR, REMOTE_LIST,
                                   remote_props, remote_dels, remote_owns),
       check=False)
    sh(args.adb, "shell", "rm -f %s" % remote_inst, check=False)

    print("\n完成。建议重启：adb reboot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
