#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「系统层修复」固化成一个可刷的 TWRP 包。

为什么要这个脚本
----------------
内核修复可以靠 build.sh 打补丁 → 编 boot.img 复现；
但系统层修复（补库、相机库、音频 HAL、传感器、蓝牙、时间同步、属性）
过去只存在于设备上——**一刷 ROM 就全没了，等于没做**。

本脚本做三件事：
  1) --stage   从实机把清单里的文件拉到仓库 systemfix/files/（一次性）
  2) --pack    校验暂存文件与 manifest 的 MD5 一致，打成 TWRP 可刷 zip
  3) --verify  只校验，不打包

设计要点（都是踩过的坑）
------------------------
* 用「清单 + MD5」而不是「凭记忆列文件」：漏一个就会开机循环或某功能失效。
* 安装脚本自己备份被覆盖的文件到 /sdcard/s3pro-fix-backup/<时间戳>/，
  刷错了能自己退回去，不必重刷 ROM。
* 属性按「键值」改，不整份替换 build.prop：换 ROM 版本也不会带进不匹配的属性。
* update-binary 必须 LF + 0755（make_twrp_zip.py 里记过这个坑，这里同样处理）。
* 文件来自 zip 内的 files/ 目录，用 unzip 解到 /tmp 再装——
  不依赖 recovery 把整个 zip 解开到某个未知路径。

用法
----
    # 一次性：从设备抓取（需要 adb + root）
    python scripts/pack_systemfix.py --stage

    # 打包（默认动作，会先校验 MD5）
    python scripts/pack_systemfix.py --pack
    python scripts/pack_systemfix.py --pack --out out/s3pro-systemfix.zip

    # 只校验暂存文件是否与清单一致
    python scripts/pack_systemfix.py --verify
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
SYSFIX = os.path.join(ROOT, "systemfix")
MANIFEST = os.path.join(SYSFIX, "manifest.txt")
PROPS = os.path.join(SYSFIX, "props.txt")
DELETE = os.path.join(SYSFIX, "delete.txt")
OWNERS = os.path.join(SYSFIX, "owners.txt")
STAGE = os.path.join(SYSFIX, "files")

META_BIN = "META-INF/com/google/android/update-binary"
META_SCRIPT = "META-INF/com/google/android/updater-script"


# ---------------------------------------------------------------- 清单解析
def load_manifest(path=MANIFEST):
    """返回 [(mode, size, md5, relpath), ...]"""
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            # 去掉 BOM / 全角空格之类的隐形字符（Windows 编辑器很容易带进来）
            s = line.strip().lstrip("\ufeff").strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split(None, 3)
            if len(parts) != 4:
                raise SystemExit("%s:%d 格式不对（需要 4 列）: %s"
                                 % (path, lineno, s))
            mode_s, size, md5, rel = parts
            if not mode_s.isdigit() or not size.isdigit():
                raise SystemExit("%s:%d 权限/大小不是数字: %s"
                                 % (path, lineno, s))
            if len(md5) != 32:
                raise SystemExit("%s:%d MD5 长度不对: %s"
                                 % (path, lineno, s))
            # 权限按【八进制】解析："755" -> 0o755
            # （如果当十进制会得到 755 == 0o1363，chmod 和 zip 属性全错）
            mode = int(mode_s, 8)
            items.append((mode, int(size), md5.lower(), rel))
    return items


def load_props(path=PROPS):
    """返回 [(propfile, key, value), ...]"""
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            s = line.strip().lstrip("\ufeff").strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split(None, 2)
            if len(parts) != 3:
                raise SystemExit("%s:%d 格式不对（需要 3 列）: %s"
                                 % (path, lineno, s))
            items.append(tuple(parts))
    return items


def load_delete(path=DELETE):
    """返回 [relpath, ...]。有些修复是“删掉某个文件”（如 TWRP 保持）。"""
    items = []
    if not os.path.isfile(path):
        return items
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip().lstrip("\ufeff").strip()
            if not s or s.startswith("#"):
                continue
            items.append(s)
    return items


def load_owners(path=OWNERS):
    """返回 [(relpath, "uid:gid"), ...]。只对需要特殊属主的文件生效。"""
    items = []
    if not os.path.isfile(path):
        return items
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            s = line.strip().lstrip("\ufeff").strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split(None, 1)
            if len(parts) != 2:
                raise SystemExit("%s:%d 格式不对（需要 2 列）: %s"
                                 % (path, lineno, s))
            items.append((parts[0], parts[1]))
    return items


def md5_of(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------- 校验
def verify_stage(items, quiet=False):
    problems = []
    for mode, size, md5, rel in items:
        p = os.path.join(STAGE, rel)
        if not os.path.isfile(p):
            problems.append("缺失: %s" % rel)
            continue
        real_size = os.path.getsize(p)
        if real_size != size:
            problems.append("大小不符: %s（清单 %d / 实际 %d）"
                            % (rel, size, real_size))
            continue
        real_md5 = md5_of(p)
        if real_md5 != md5:
            problems.append("MD5 不符: %s（清单 %s / 实际 %s）"
                            % (rel, md5, real_md5))
    if not quiet:
        if problems:
            print("暂存文件校验失败（%d 项）：" % len(problems), file=sys.stderr)
            for x in problems[:30]:
                print("   " + x, file=sys.stderr)
            if len(problems) > 30:
                print("   ...还有 %d 项" % (len(problems) - 30), file=sys.stderr)
        else:
            total = sum(i[1] for i in items)
            print("暂存文件校验通过：%d 个文件，%.1f MB，MD5 全部一致"
                  % (len(items), total / 1024.0 / 1024.0))
    return problems


# ---------------------------------------------------------------- 从设备抓取
def adb_cmd(adb, *args, check=True):
    cmd = [adb] + list(args)
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise SystemExit("adb 命令失败: %s\n%s\n%s"
                         % (" ".join(cmd), p.stdout, p.stderr))
    return p


def stage_from_device(adb, items):
    print("从设备抓取 %d 个文件（先在设备上打 tar，再一次性拉回）..." % len(items))
    rels = [i[3] for i in items]
    remote_tar = "/data/local/tmp/s3pro-systemfix.tar"

    # 用 tar 一次打包，避免 66 次 adb pull
    adb_cmd(adb, "shell", "rm", "-f", remote_tar)
    # 路径较长，分批传给 tar，避免单条命令过长
    batch, batches = [], []
    cur = 0
    for r in rels:
        if cur + len(r) > 100000:
            batches.append(batch)
            batch, cur = [], 0
        batch.append(r)
        cur += len(r) + 1
    if batch:
        batches.append(batch)

    first = True
    for i, b in enumerate(batches):
        # tar 第一次用 -c 创建，后续用 -r 追加
        flag = "-cf" if first else "-rf"
        adb_cmd(adb, "shell", "cd /system && tar %s %s %s" % (flag, remote_tar,
                                                              " ".join(b)))
        first = False
        print("   批次 %d/%d 完成" % (i + 1, len(batches)))

    local_tar = os.path.join(tempfile.gettempdir(), "s3pro-systemfix.tar")
    adb_cmd(adb, "pull", remote_tar, local_tar)
    adb_cmd(adb, "shell", "rm", "-f", remote_tar, check=False)

    if os.path.isdir(STAGE):
        shutil.rmtree(STAGE)
    os.makedirs(STAGE, exist_ok=True)

    import tarfile
    with tarfile.open(local_tar, "r") as tf:
        try:
            # Python 3.12+ 建议显式指定 filter；旧版本没有这个参数
            tf.extractall(STAGE, filter="data")
        except TypeError:
            tf.extractall(STAGE)
    os.remove(local_tar)
    print("已解到 %s" % STAGE)


# ---------------------------------------------------------------- 安装脚本生成
UPDATE_BINARY_TEMPLATE = r"""#!/sbin/sh
#
# 步步高 S3 Pro (h3000s) LineageOS 15.1 —— 系统层修复包
#
# 这个包只做两件事：
#   1) 把 files/ 里的文件装到 /system 对应位置（默认 644，bin/hw 下的服务 755）
#   2) 按 props.txt 的键值修改 /system/build.prop（其余属性一律不动）
#
# 被覆盖的原文件会先备份到 /sdcard/s3pro-fix-backup/<时间戳>/
# 想回退：把备份目录里的文件按原路径拷回去，再把属性改回来即可。
#
# 不做：格式化、清数据、改 boot 分区、动 recovery。

ZIPFILE="$3"
PKG=/tmp/s3pro-fix
BKROOT=/sdcard/s3pro-fix-backup
BK="$BKROOT/$(date +%Y%m%d-%H%M%S)"

ui_print() {
    echo "ui_print $*"
    echo "ui_print"
}

fail() {
    ui_print "!! $*"
    ui_print "!! 未完成，系统可能处于半安装状态，请查看上面的输出"
    exit 1
}

ui_print "=============================================="
ui_print " S3 Pro (h3000s) LOS 15.1 系统修复包"
ui_print "=============================================="

# ---------------------------------------------------------------- 找到 zip
if [ -z "$ZIPFILE" ] || [ ! -f "$ZIPFILE" ]; then
    for c in /sdcard/*.zip /tmp/*.zip /external_sd/*.zip; do
        case "$c" in *systemfix*|*s3pro-fix*) ZIPFILE="$c"; break;; esac
    done
fi
ui_print "安装包: $ZIPFILE"

# ---------------------------------------------------------------- 解包
UNZIP=""
if command -v unzip >/dev/null 2>&1; then
    UNZIP="unzip"
elif command -v busybox >/dev/null 2>&1 && busybox unzip -h >/dev/null 2>&1; then
    UNZIP="busybox unzip"
fi
[ -z "$UNZIP" ] && fail "找不到 unzip，无法解包（未做任何修改）"

rm -rf "$PKG"
mkdir -p "$PKG" || fail "无法创建 $PKG"
$UNZIP -o "$ZIPFILE" 'files/*' -d "$PKG" >/dev/null 2>&1
[ -d "$PKG/files" ] || fail "解包后找不到 files/ 目录"

# ---------------------------------------------------------------- 挂载 /system
ui_print "挂载 /system 为可写..."
mount /system >/dev/null 2>&1
mount -o rw,remount /system >/dev/null 2>&1
if ! touch /system/.sfx_write_test 2>/dev/null; then
    ui_print "  再试按块设备重挂..."
    for d in /dev/block/bootdevice/by-name/system \
             /dev/block/platform/mtk-msdc.0/by-name/system \
             /dev/block/platform/mtk-msdc.0/11230000.msdc0/by-name/system; do
        [ -e "$d" ] || continue
        mount -o rw,remount "$d" /system >/dev/null 2>&1 && break
    done
fi
if ! touch /system/.sfx_write_test 2>/dev/null; then
    fail "/system 不是可写状态（可能是 dm-verity / 只读挂载）"
fi
rm -f /system/.sfx_write_test
ui_print "  /system 可写 OK"

# ---------------------------------------------------------------- 安装文件
mkdir -p "$BK"
ui_print ""
ui_print "备份被覆盖的原文件到 $BK ..."
install_file() {
    _mode="$1"; _md5="$2"; _rel="$3"
    _src="$PKG/files/$_rel"
    _dst="/system/$_rel"

    [ -f "$_src" ] || { ui_print "  跳过（包内缺失）: $_rel"; return 0; }

    if [ -f "$_dst" ]; then
        mkdir -p "$BK/$(dirname "$_rel")"
        cp -f "$_dst" "$BK/$_rel" 2>/dev/null
    fi

    mkdir -p "$(dirname "$_dst")" || { ui_print "  失败（建目录）: $_rel"; return 1; }
    cp -f "$_src" "$_dst" || { ui_print "  失败（拷贝）: $_rel"; return 1; }
    chmod "$_mode" "$_dst" 2>/dev/null
    # 注意：这里【故意不 chown】。
    #   · 已存在的文件，cp 会保留原属主（例如 wpa_supplicant 必须是 root:shell）；
    #   · 新建的文件默认 root:root，对库文件来说正是想要的。
    #   之前无条件 chown 0:0 会把 wpa_supplicant 的属主改坏。

    _got=$(md5sum "$_dst" 2>/dev/null | cut -d' ' -f1)
    if [ "$_got" = "$_md5" ]; then
        ui_print "  OK   $_rel"
        return 0
    fi
    ui_print "  校验不符: $_rel（期望 $_md5 实际 $_got）"
    return 1
}

FAILCOUNT=0
while read -r _m _s _h _p; do
    [ -n "$_p" ] || continue
    case "$_p" in \#*) continue;; esac
    install_file "$_m" "$_h" "$_p" || FAILCOUNT=$((FAILCOUNT+1))
done <<'SFX_FILES'
__FILE_LIST__
SFX_FILES

# ---------------------------------------------------------------- 修属主
# 只有个别文件需要特殊属主（如 wpa_supplicant 必须 root:shell）。
# 没列在这里的一律不 chown，由 cp 保留原属主。
ui_print ""
ui_print "修正需要特殊属主的文件..."
while read -r _rel _owner; do
    [ -n "$_owner" ] || continue
    case "$_rel" in \#*) continue;; esac
    _dst="/system/$_rel"
    if [ ! -e "$_dst" ]; then
        ui_print "  跳过（文件不存在）: $_rel"
        continue
    fi
    if chown "$_owner" "$_dst" 2>/dev/null; then
        ui_print "  属主: $_rel -> $_owner"
    else
        ui_print "  属主设置失败: $_rel"
        FAILCOUNT=$((FAILCOUNT+1))
    fi
done <<'SFX_OWNERS'
__OWNER_LIST__
SFX_OWNERS

# ---------------------------------------------------------------- 删除文件
# 有些“修复”是删掉某个文件。最典型：/system/recovery-from-boot 会在开机时
# 把 recovery 恢复成原厂覆盖掉 TWRP，删掉它才能保住 TWRP。
ui_print ""
ui_print "处理需要删除的文件..."
while read -r _rel; do
    [ -n "$_rel" ] || continue
    case "$_rel" in \#*) continue;; esac
    _dst="/system/$_rel"
    if [ ! -e "$_dst" ]; then
        ui_print "  已不存在（OK）: $_rel"
        continue
    fi
    mkdir -p "$BK/$(dirname "$_rel")"
    cp -f "$_dst" "$BK/$_rel" 2>/dev/null
    if rm -f "$_dst"; then
        ui_print "  已删除: $_rel（已备份）"
    else
        ui_print "  删除失败: $_rel"
        FAILCOUNT=$((FAILCOUNT+1))
    fi
done <<'SFX_DELETE'
__DELETE_LIST__
SFX_DELETE

# ---------------------------------------------------------------- 改属性
ui_print ""
ui_print "应用属性补丁..."
apply_prop() {
    _file="$1"; _key="$2"; _val="$3"
    _p="/system/$_file"
    [ -f "$_p" ] || { ui_print "  跳过（文件不存在）: $_file"; return 0; }
    if grep -q "^${_key}=" "$_p"; then
        _old=$(grep "^${_key}=" "$_p" | head -1 | cut -d= -f2-)
        if [ "$_old" = "$_val" ]; then
            ui_print "  已是目标值: $_key"
            return 0
        fi
        # 用 | 作分隔符，避免值里的 / 和 : 需要转义
        sed -i "s|^${_key}=.*|${_key}=${_val}|" "$_p" \
            || { ui_print "  失败: $_key"; return 1; }
        ui_print "  $_key: $_old -> $_val"
    else
        echo "${_key}=${_val}" >> "$_p" || { ui_print "  失败(追加): $_key"; return 1; }
        ui_print "  $_key: (新增) $_val"
    fi
    return 0
}

while read -r _f _k _v; do
    [ -n "$_k" ] || continue
    case "$_k" in \#*) continue;; esac
    apply_prop "$_f" "$_k" "$_v" || FAILCOUNT=$((FAILCOUNT+1))
done <<'SFX_PROPS'
__PROP_LIST__
SFX_PROPS

# ---------------------------------------------------------------- 收尾
sync
mount -o ro,remount /system >/dev/null 2>&1
rm -rf "$PKG"

ui_print ""
ui_print "=============================================="
if [ "$FAILCOUNT" -eq 0 ]; then
    ui_print " 完成，全部文件已校验"
else
    ui_print " 完成，但有 $FAILCOUNT 项失败，请看上面"
fi
ui_print " 原文件备份: $BK"
ui_print " 回退方法: 把上面目录里的文件按原路径拷回 /system"
ui_print "=============================================="
exit 0
"""

UPDATER_SCRIPT = """# 这个 zip 的 update-binary 是 shell 脚本（不是 edify），
# TWRP 会直接执行它；本文件只是为了满足 recovery 的格式检查。
"""

ZIP_README = """S3 Pro (h3000s) LineageOS 15.1 系统层修复包
============================================

安装：TWRP -> Install -> 选本 zip -> 滑动确认
回退：见安装结束时打印的备份目录（/sdcard/s3pro-fix-backup/<时间戳>/）

本包内文件清单见 manifest.txt（含每个文件的 MD5，安装时会逐个校验）。
属性补丁见 props.txt（只改这几个键，其余属性不动）。

对应你的修复记录：
  系统 1 补库修复开机问题      -> vendor/lib/hw/*.so + shim 库
  系统 2 补 wifi 库            -> 本机不需要（wifi 由 vendor/firmware 与驱动处理）
  系统 3 添加 ndp 时间同步     -> etc/timesync.sh + vendor/etc/init/timesync.rc
  系统 4 webview 更新          -> 走 apk 安装，不在本包内（见 docs/固化成包.md）
  系统 5 app 访问不到文件      -> 由 build.prop/权限配合，见文档
  系统 6 音频修复              -> vendor/lib64/hw/audio.primary.mt8173.so
  系统 7 对齐设备指纹          -> props.txt 的 ro.build.fingerprint/description
  内核 1 三大金刚键            -> props.txt 的 qemu.hw.mainkeys=1（+ 内核 GT911 按键驱动）
  内核 2 电量百分比            -> 在内核（build.sh 的 Qmax/SOC 补丁），不在本包内
  内核 3 加速度计              -> 在内核（build.sh 的 sensors/LIS3DH 补丁），不在本包内
"""


def build_update_binary(items, props, deletes=(), owners=()):
    file_lines = []
    for mode, size, md5, rel in items:
        # 列顺序与脚本里 read -r _m _s _h _p 对应：mode size md5 path
        # 权限用八进制输出（chmod 认的是八进制文本）
        file_lines.append("%o %d %s %s" % (mode, size, md5, rel))
    prop_lines = []
    for f, k, v in props:
        prop_lines.append("%s %s %s" % (f, k, v))
    body = UPDATE_BINARY_TEMPLATE.replace("__FILE_LIST__", "\n".join(file_lines))
    body = body.replace("__PROP_LIST__", "\n".join(prop_lines))
    owner_lines = ["%s %s" % (rel, ow) for rel, ow in owners]
    body = body.replace("__DELETE_LIST__", "\n".join(deletes))
    body = body.replace("__OWNER_LIST__", "\n".join(owner_lines))
    # 统一 LF，避免 Windows 上被改成 CRLF 导致 recovery 报 not found
    return body.replace("\r\n", "\n").encode("utf-8")


def pack(items, props, out_path, deletes=(), owners=()):
    problems = verify_stage(items, quiet=True)
    if problems:
        print("暂存文件与清单不一致，拒绝打包：", file=sys.stderr)
        for x in problems[:30]:
            print("   " + x, file=sys.stderr)
        raise SystemExit(1)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + ".tmp"

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        # 安装器（LF + 0755）
        info = zipfile.ZipInfo(META_BIN)
        info.external_attr = (0o100755 << 16)
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, build_update_binary(items, props, deletes, owners))

        zf.writestr(META_SCRIPT, UPDATER_SCRIPT.replace("\r\n", "\n"))

        # 审计用文本
        for name in ("manifest.txt", "props.txt", "delete.txt", "owners.txt"):
            p = os.path.join(SYSFIX, name)
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    zf.writestr(name, f.read())
        zf.writestr("README.txt", ZIP_README.replace("\r\n", "\n"))

        # 负载
        written = 0
        for mode, size, md5, rel in items:
            src = os.path.join(STAGE, rel)
            zi = zipfile.ZipInfo("files/" + rel.replace(os.sep, "/"))
            zi.external_attr = ((0o100000 | mode) << 16)
            zi.compress_type = zipfile.ZIP_DEFLATED
            with open(src, "rb") as f:
                zf.writestr(zi, f.read())
            written += 1

    os.replace(tmp, out_path)
    return written


def verify_zip(path, items):
    problems = []
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        for req in (META_BIN, META_SCRIPT):
            if req not in names:
                problems.append("缺少 %s" % req)
        if META_BIN in names:
            info = zf.getinfo(META_BIN)
            data = zf.read(info)
            if not data.startswith(b"#!/sbin/sh"):
                problems.append("update-binary 开头不是 #!/sbin/sh")
            if b"\r\n" in data:
                problems.append("update-binary 含 CRLF（上机会报 not found）")
            mode = (info.external_attr >> 16) & 0o777
            if mode & 0o111 == 0:
                problems.append("update-binary 缺可执行位（当前 %o）" % mode)
        for mode, size, md5, rel in items:
            key = "files/" + rel.replace(os.sep, "/")
            if key not in names:
                problems.append("包内缺文件 %s" % key)
                continue
            # 抽查：解出来算 MD5（全部校验，代价可接受）
            got = hashlib.md5(zf.read(key)).hexdigest()
            if got != md5:
                problems.append("包内 MD5 不符 %s" % key)
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description="系统层修复包：固化 / 打包")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--stage", action="store_true", help="从设备抓取文件到 systemfix/files/")
    g.add_argument("--pack", action="store_true", help="打包（默认动作）")
    g.add_argument("--verify", action="store_true", help="只校验暂存文件")
    ap.add_argument("--adb", default="adb", help="adb 可执行文件路径")
    ap.add_argument("--out", default=os.path.join(ROOT, "out", "s3pro-systemfix.zip"))
    args = ap.parse_args(argv)

    items = load_manifest()
    props = load_props()
    deletes = load_delete()
    owners = load_owners()

    if args.stage:
        stage_from_device(args.adb, items)
        verify_stage(items)
        return 0

    if args.verify:
        problems = verify_stage(items)
        return 1 if problems else 0

    # 默认 --pack
    n = pack(items, props, args.out, deletes, owners)
    size = os.path.getsize(args.out)
    print("已生成 %s（%d 个文件 + %d 项属性 + %d 项删除 + %d 项属主，%.1f MB）"
          % (args.out, n, len(props), len(deletes), len(owners), size / 1024.0 / 1024.0))

    problems = verify_zip(args.out, items)
    if problems:
        print("\n自检失败：", file=sys.stderr)
        for p in problems[:30]:
            print("   " + p, file=sys.stderr)
        return 1
    print("自检通过：条目齐全、update-binary 为 LF 且带可执行位、包内文件 MD5 全部一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
