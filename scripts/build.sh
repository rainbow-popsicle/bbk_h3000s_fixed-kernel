#!/usr/bin/env bash
# 在 GitHub Actions (ubuntu-latest) 上构建 aarch64 安卓内核，并把新内核塞回原厂 boot.img。
#
# 全部行为由环境变量控制，默认值按步步高 S3 Pro（MTK8176）能用的最近似开源树设置：
#   KERNEL_REPO     内核源码地址
#   KERNEL_BRANCH   分支
#   DEFCONFIG       树里的 defconfig 名（没有内嵌 config 时的兜底）
#   KERNEL_TARGET   默认 Image；MTK 树自带 DTB 时可用 Image.gz-dtb
#   STOCK_BOOT      原厂 boot.img 路径（有则从中提取真实 .config，并作为打回模板）
#   INPUT_DEFCONFIG 外部 defconfig 文件（社区补丁仓库的 h3000s_defconfig）
#   INPUT_DTB       外部 DTB（社区修正版），优先于从 boot.img 里拆出的那份
#   TOOLCHAIN       gcc49（默认）| host
#   DRY_RUN         1 = 只做工具链/源码/配置三段校验，不编译
#   JOBS            并行度
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${WORK:-$ROOT/work}"
OUT="${OUT:-$ROOT/out}"
PY="${PY:-python3}"

KERNEL_REPO="${KERNEL_REPO:-https://github.com/Haoduo9/android_kernel_bbk_h3000s.git}"
KERNEL_BRANCH="${KERNEL_BRANCH:-lineage-15.1-h3000s}"
DEFCONFIG="${DEFCONFIG:-h3000s_defconfig}"
KERNEL_TARGET="${KERNEL_TARGET:-Image}"
STOCK_BOOT="${STOCK_BOOT:-$ROOT/inputs/boot.img}"
INPUT_CONFIG="${INPUT_CONFIG:-$ROOT/inputs/config.txt}"
INPUT_DEFCONFIG="${INPUT_DEFCONFIG:-$ROOT/inputs/defconfig}"
INPUT_DTB="${INPUT_DTB:-$ROOT/inputs/boot.dtb}"
TOOLCHAIN="${TOOLCHAIN:-gcc49}"
JOBS="${JOBS:-$(nproc)}"

mkdir -p "$WORK" "$OUT"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m[error] %s\033[0m\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
step "1/6 主机依赖"
sudo apt-get update -qq
sudo apt-get install -y -qq --no-install-recommends \
    build-essential bc bison flex libssl-dev libelf-dev file curl \
    python3 zip unzip xz-utils lz4 device-tree-compiler cpio rsync \
    gcc-aarch64-linux-gnu >/dev/null || warn "部分依赖安装失败，继续"
gcc --version | head -1

# --------------------------------------------------------------------------- #
step "2/6 交叉工具链（$TOOLCHAIN）"
# Google 那支 aarch64 GCC 4.9 是 32 位宿主程序，在 ubuntu-24.04 上经常因为
# 缺 ld-linux.so.2 报 "No such file or directory"（看起来像文件不存在，其实是加载器缺失）。
# 这里做成"自动挑一支能跑的"：先本地已有的，再 Google，再 32 位库，最后 Linaro（64 位宿主）。
try_cc() {
    CC_BIN="$1"
    [ -x "$CC_BIN" ] || return 1
    "$CC_BIN" --version >/dev/null 2>&1
}

report_cc() {
    echo "  候选: $1"
    if [ -x "$1" ]; then
        file "$1" 2>/dev/null | sed 's/^/    file: /' || true
        ldd "$1" 2>&1 | grep -i 'not found' | sed 's/^/    ldd : /' || true
    else
        echo "    （文件不存在）"
    fi
}

setup_toolchain() {
    # ① 工程内已有的工具链目录
    if [ -d "$WORK/toolchain" ] && try_cc "$WORK/toolchain/bin/aarch64-linux-android-gcc"; then
        export CROSS_COMPILE="$WORK/toolchain/bin/aarch64-linux-android-"
        echo "复用已有工具链: $WORK/toolchain"
        return 0
    fi

    # ② AOSP / LineageOS 的预编译 GCC 4.9（多源，谁通用谁）
    TC="$WORK/toolchain"
    if [ ! -x "$TC/bin/aarch64-linux-android-gcc" ]; then
        rm -rf "$TC"
        for repo in \
            https://github.com/LineageOS/android_prebuilts_gcc_linux-x86_aarch64_aarch64-linux-android-4.9.git \
            https://android.googlesource.com/platform/prebuilts/gcc/linux-x86/aarch64/aarch64-linux-android-4.9
        do
            echo "克隆预编译 GCC 4.9：$repo"
            if git clone --depth 1 "$repo" "$TC" 2>&1 | tail -3; then
                [ -x "$TC/bin/aarch64-linux-android-gcc" ] && break
            fi
            rm -rf "$TC"
        done
    fi
    if try_cc "$TC/bin/aarch64-linux-android-gcc"; then
        export CROSS_COMPILE="$TC/bin/aarch64-linux-android-"
        return 0
    fi
    report_cc "$TC/bin/aarch64-linux-android-gcc"

    # ③ 补 32 位运行库后再试（这里不能再 || true 吞错了，
    #    上一次失败就是因为安装错误被静默掉）
    warn "32 位工具链跑不起来，尝试补齐 i386 运行库"
    sudo dpkg --add-architecture i386 || warn "add-architecture 失败"
    sudo apt-get update -qq || warn "apt update 失败"
    echo "外来架构：$(dpkg --print-foreign-architectures | tr '\n' ' ')"
    sudo apt-get install -y libc6:i386 libstdc++6:i386 zlib1g:i386 \
        || warn "i386 运行库安装失败（继续尝试下一支）"
    if try_cc "$TC/bin/aarch64-linux-android-gcc"; then
        export CROSS_COMPILE="$TC/bin/aarch64-linux-android-"
        echo "补库后可用"
        return 0
    fi
    ldd "$TC/bin/aarch64-linux-android-gcc" 2>&1 | sed 's/^/    ldd : /' || true

    # ④ Linaro（x86_64 宿主，不需要 32 位库）——多试几个版本
    LIN="$WORK/linaro"
    rm -rf "$LIN"; mkdir -p "$LIN"
    for url in \
        https://releases.linaro.org/components/toolchain/binaries/4.9-2017.01/aarch64-linux-gnu/gcc-linaro-4.9.4-2017.01-x86_64_aarch64-linux-gnu.tar.xz \
        https://releases.linaro.org/components/toolchain/binaries/4.9-2016.02/aarch64-linux-gnu/gcc-linaro-4.9.4-2016.02-x86_64_aarch64-linux-gnu.tar.xz \
        https://releases.linaro.org/components/toolchain/binaries/6.5-2018.12/aarch64-linux-gnu/gcc-linaro-6.5.0-2018.12-x86_64_aarch64-linux-gnu.tar.xz
    do
        echo "尝试 Linaro: $url"
        if curl -fL --retry 2 -o "$WORK/linaro.tar.xz" "$url" 2>/dev/null; then
            if tar -xJf "$WORK/linaro.tar.xz" -C "$LIN" --strip-components=1 2>/dev/null; then
                if try_cc "$LIN/bin/aarch64-linux-gnu-gcc"; then
                    export CROSS_COMPILE="$LIN/bin/aarch64-linux-gnu-"
                    echo "改用 Linaro 工具链: $("$LIN/bin/aarch64-linux-gnu-gcc" --version | head -1)"
                    return 0
                fi
            fi
        fi
    done

    return 1
}

if [ "$TOOLCHAIN" = "gcc49" ]; then
    if ! setup_toolchain; then
        die "没有可用的交叉工具链。把上面的 file/ldd/Linaro 输出发出来，我来换源"
    fi
    if command -v "${CROSS_COMPILE}gcc" >/dev/null 2>&1; then
        "${CROSS_COMPILE}gcc" --version | head -1
    else
        warn "选定的交叉编译器不在 PATH 里：${CROSS_COMPILE}gcc"
    fi
else
    export CROSS_COMPILE="aarch64-linux-gnu-"
    warn "使用发行版工具链，老内核大概率编不过；仅在你清楚原因时使用"
fi

# --------------------------------------------------------------------------- #
step "3/6 拉取内核源码"
SRC="$WORK/kernel"
if [ ! -d "$SRC/.git" ]; then
    git clone --depth 1 -b "$KERNEL_BRANCH" "$KERNEL_REPO" "$SRC" \
        || git clone --depth 1 "$KERNEL_REPO" "$SRC"
fi
cd "$SRC"
echo "HEAD: $(git log -1 --format='%h %s' 2>/dev/null || echo '(无 git 信息)')"

# 这棵 H3000S 树把 WireGuard 之类做成了子模块，--depth 1 克隆不会带下来。
# 不处理的话 kconfig 阶段会报 can't open file "net/wireguard/Kconfig" 直接退出。
if [ -f .gitmodules ]; then
    echo "检测到子模块，尝试初始化……"
    git submodule update --init --recursive --depth 1 2>&1 | tail -5 \
        || warn "子模块初始化失败"
fi
# 兜底：Kconfig 引用但文件不存在时，补一个空占位，别让配置阶段卡死
for kfile in net/wireguard/Kconfig; do
    if grep -q "$kfile" net/Kconfig 2>/dev/null && [ ! -f "$kfile" ]; then
        mkdir -p "$(dirname "$kfile")"
        : > "$kfile"
        warn "$kfile 缺失（子模块没同步），已补空占位"
    fi
done
if [ -f Makefile ]; then
    echo "版本: $(grep -E '^(VERSION|PATCHLEVEL|SUBLEVEL) =' Makefile | awk '{print $3}' | paste -sd.)"
fi

# --------------------------------------------------------------------------- #
step "4/6 确定 .config"
CONFIG_SOURCE="defconfig"

# 社区补丁仓库（Haoduo9/android_kernel_bbk_h3000s_patches）里的 defconfig
# 不在内核树的 arch/arm64/configs 下，先拷进去再当 make 目标用
if [ -f "$INPUT_DEFCONFIG" ]; then
    mkdir -p arch/arm64/configs
    cp "$INPUT_DEFCONFIG" arch/arm64/configs/h3000s_local_defconfig
    DEFCONFIG="h3000s_local_defconfig"
    echo "使用社区 defconfig：$INPUT_DEFCONFIG"
fi
STOCK_INFO="$WORK/stock-info.txt"
STOCK_VER=""

# 优先级：用户手放的 .config > boot.img 内嵌 .config > defconfig
if [ -f "$INPUT_CONFIG" ]; then
    cp "$INPUT_CONFIG" .config
    CONFIG_SOURCE="inputs/config.txt"
    echo "使用你提供的 .config（来自 /proc/config.gz 之类）：$(grep -c '^CONFIG_' .config || true) 项"
fi

if [ -f "$STOCK_BOOT" ]; then
    BOOT_SIZE=$(stat -c%s "$STOCK_BOOT" || echo 0)
    echo "发现 boot.img（$BOOT_SIZE 字节），先做体检（同时用于对齐 release 版本）"
    # 典型大小：原厂 16,777,216；作者成品内核 7~8MB。低于 1MB 一定是传错了。
    if [ "$BOOT_SIZE" -lt 1048576 ]; then
        die "boot.img 只有 $BOOT_SIZE 字节，明显不对（正常是 7~16MB）。
     多半是上传失败：到 GitHub 上看 inputs/boot.img 显示的大小，
     应该是 7.6 MB 左右。重新用 Add file → Upload files 传一遍。"
    fi
    "$PY" "$ROOT/scripts/bootimg.py" info "$STOCK_BOOT" | tee "$STOCK_INFO" || true
    STOCK_VER="$(grep -o 'Linux version [^ ]*' "$STOCK_INFO" | head -1 | awk '{print $3}' || true)"
    echo "原厂内核 release: ${STOCK_VER:-未知}"
    if [ ! -f .config ]; then
        if "$PY" "$ROOT/scripts/bootimg.py" config "$STOCK_BOOT" > "$WORK/stock.config" 2>/dev/null \
            && [ -s "$WORK/stock.config" ]; then
            cp "$WORK/stock.config" .config
            CONFIG_SOURCE="stock-ikconfig"
            echo "已使用设备真实配置：$(grep -c '^CONFIG_' .config || true) 项"
        else
            warn "boot.img 里没有内嵌 .config（CONFIG_IKCONFIG 没开），退回 defconfig"
        fi
    fi
fi

if [ ! -f .config ]; then
    if [ ! -f "arch/arm64/configs/$DEFCONFIG" ]; then
        echo "源码树里没有 arch/arm64/configs/$DEFCONFIG，这份树自带的可选项："
        ls arch/arm64/configs/ 2>/dev/null | grep '_defconfig$' | sed 's/^/  /' | head -30
        die "请把 DEFCONFIG 改成上面某个名字，或把外部 defconfig 放到 inputs/defconfig"
    fi
    make ARCH=arm64 "$DEFCONFIG"
fi

# 老内核 + 新环境常见的几处坑，按需兜底开启，保证能过编译
enable() {
    grep -q "^$1=y" .config || echo "$1=y" >> .config
}
enable CONFIG_IKCONFIG
enable CONFIG_IKCONFIG_PROC

make ARCH=arm64 olddefconfig

# ★ 让新内核的 release 字符串和原厂一模一样。
# 这不是洁癖：MTK 的 vendor 分区里有预编译内核模块（比如 wlan.ko），
# 加载时会比对 vermagic/`uname -r`，release 对不上就直接模块加载失败（WiFi 死、相机死）。
BASE_VER="$(grep -E '^(VERSION|PATCHLEVEL|SUBLEVEL) =' Makefile | awk '{print $3}' | paste -sd.)"
if [ -n "$STOCK_VER" ]; then
    case "$STOCK_VER" in
        "$BASE_VER"*)
            LOCAL="${STOCK_VER#"$BASE_VER"}"
            echo "把 CONFIG_LOCALVERSION 固定为 '$LOCAL'（对齐原厂 $STOCK_VER）"
            sed -i -e '/^CONFIG_LOCALVERSION=/d' -e '/^CONFIG_LOCALVERSION_AUTO=/d' .config
            printf 'CONFIG_LOCALVERSION="%s"\nCONFIG_LOCALVERSION_AUTO=n\n' "$LOCAL" >> .config
            ;;
        *)
            warn "原厂 release ($STOCK_VER) 与源码树 ($BASE_VER) 版本段不一致！"
            warn "编出来的内核极可能起不来——请换一棵同版本的源码树再试"
            ;;
    esac
else
    warn "没读到原厂内核版本字符串，跳过 LOCALVERSION 对齐"
fi
make ARCH=arm64 olddefconfig
grep -E '^CONFIG_LOCALVERSION' .config || true

# --------------------------------------------------------------------------- #
# 干跑：只验证"工具链 + 源码 + 配置"这三段，几分钟出结果，不耗时编译。
# 第一次接新机型/新源码树时强烈建议先跑它。
if [ "${DRY_RUN:-0}" = "1" ]; then
    step "5/6 干跑（DRY_RUN=1），跳过编译"
    echo "源码版本      : $BASE_VER"
    echo "原厂 release  : ${STOCK_VER:-未知}"
    echo "配置来源      : $CONFIG_SOURCE"
    echo "CONFIG 条目   : $(grep -c '^CONFIG_' .config || true)"
    echo "  =y 条目     : $(grep -c '=y$' .config || true)"
    echo "  =m 条目     : $(grep -c '=m$' .config || true)"
    echo "LOCALVERSION  : $(grep -E '^CONFIG_LOCALVERSION=' .config | head -1 || true)"
    echo "编译目标      : $KERNEL_TARGET"
    echo ""
    echo "干跑通过：工具链能跑、源码树可读、配置能定型。"
    echo "去掉 DRY_RUN（或把 workflow 的 dry_run 关掉）即可正式编译。"
    exit 0
fi

# --------------------------------------------------------------------------- #
step "5/6 编译（-j$JOBS，目标 $KERNEL_TARGET）"
BUILD_LOG="$OUT/build.log"
set +e
make ARCH=arm64 CROSS_COMPILE="$CROSS_COMPILE" -j"$JOBS" "$KERNEL_TARGET" 2>&1 | tee "$BUILD_LOG"
RC=${PIPESTATUS[0]}
set -e
if [ "$RC" -ne 0 ]; then
    echo "----- 最后 60 行编译错误 -----"
    tail -60 "$BUILD_LOG"
    die "编译失败（退出码 $RC）"
fi

IMG=""
for cand in "arch/arm64/boot/$KERNEL_TARGET" \
            "arch/arm64/boot/Image.gz" \
            "arch/arm64/boot/Image"; do
    if [ -f "$cand" ]; then IMG="$cand"; break; fi
done
[ -n "$IMG" ] || die "找不到编出来的内核镜像"
echo "产物: $IMG ($(stat -c%s "$IMG") 字节)"

# --------------------------------------------------------------------------- #
step "6/6 打包回 boot.img"
cp "$IMG" "$OUT/$(basename "$IMG")"
sha256sum "$IMG" | tee "$OUT/kernel.sha256"

if [ -f "$STOCK_BOOT" ]; then
    # 注意 || true：set -e 下 grep 找不到会直接终止整个脚本，
    # 那样连后面的兜底逻辑都走不到
    KIND="$(grep -o '格式=[A-Za-z0-9_-]*' "$STOCK_INFO" | head -1 | cut -d= -f2 || true)"
    [ -n "$KIND" ] || KIND="unknown"
    echo "原厂内核格式: $KIND"

    # MTK 常见 Image.gz-dtb：原厂 DTB 直接追加在内核后面。必须原样接回去，
    # 否则新内核里没有这块板子的硬件描述（屏、触摸、PMIC 全在里面）。
    SPLICE_EXTRA=()
    DTB_PATH=""
    if [ -f "$INPUT_DTB" ]; then
        DTB_PATH="$INPUT_DTB"
        SPLICE_EXTRA=(--dtb "$DTB_PATH")
        echo "使用 inputs/boot.dtb（$(stat -c%s "$DTB_PATH") 字节，社区修正版）"
    elif "$PY" "$ROOT/scripts/bootimg.py" kernel-parts \
            --image "$STOCK_BOOT" --outdir "$WORK/kparts" > "$WORK/kparts.log" 2>&1; then
        if [ -s "$WORK/kparts/kernel.dtb" ]; then
            DTB_PATH="$WORK/kparts/kernel.dtb"
            SPLICE_EXTRA=(--dtb "$DTB_PATH")
            echo "原厂带追加 DTB（$(stat -c%s "$DTB_PATH") 字节），打包时会接回新内核"
        fi
    else
        warn "拆内核分段失败，按没有追加 DTB 处理"
    fi

    # 公开树里 Image.gz-dtb 目标会把"它自己的" DTB 打进内核。
    # 那棵树的 DTB 描述的是 GPD XD+ 之类，不是 H3000S，接上去必定黑屏。
    if [ -n "$DTB_PATH" ]; then
        case "$(basename "$IMG")" in
            *dtb*)
                warn "当前目标 $(basename "$IMG") 会带上公开树自带的 DTB（机型不对）"
                warn "请把 KERNEL_TARGET 改成 Image，由本脚本 gzip 后接回原厂 DTB"
                ;;
        esac
    fi

    if [ "$KIND" = "gzip" ] && [ "${IMG##*.}" != "gz" ]; then
        echo "原厂是压缩内核，把新 Image 压成 Image.gz"
        gzip -9 -n -c "$IMG" > "$OUT/Image.gz"
        IMG="$OUT/Image.gz"
    fi
    "$PY" "$ROOT/scripts/bootimg.py" splice \
        --template "$STOCK_BOOT" --kernel "$IMG" "${SPLICE_EXTRA[@]}" --out "$OUT/boot.img"
    sha256sum "$OUT/boot.img" | tee "$OUT/boot.sha256"
else
    warn "没有 inputs/boot.img，只输出裸内核，不生成可刷的 boot.img"
fi

{
    echo "kernel_repo=$KERNEL_REPO"
    echo "kernel_branch=$KERNEL_BRANCH"
    echo "kernel_version=$(grep -E '^(VERSION|PATCHLEVEL|SUBLEVEL) =' Makefile | awk '{print $3}' | paste -sd.)"
    echo "stock_kernel_version=${STOCK_VER:-未知}"
    echo "defconfig=$DEFCONFIG"
    echo "config_source=$CONFIG_SOURCE"
    echo "toolchain=$TOOLCHAIN"
    echo "kernel_target=$KERNEL_TARGET"
    echo "kernel_image=$(basename "$IMG")"
} > "$OUT/build-info.txt"
cat "$OUT/build-info.txt"

printf '\n\033[1;32m完成：%s\033[0m\n' "$OUT"
ls -la "$OUT"
