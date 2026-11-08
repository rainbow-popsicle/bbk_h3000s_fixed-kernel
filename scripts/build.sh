#!/usr/bin/env bash
# 在 GitHub Actions (ubuntu-latest) 上构建 aarch64 安卓内核，并把新内核塞回原厂 boot.img。
#
# 全部行为由环境变量控制，默认值按步步高 S3 Pro（MTK8176）能用的最近似开源树设置：
#   KERNEL_REPO     内核源码地址
#   KERNEL_BRANCH   分支
#   DEFCONFIG       树里的 defconfig 名（没有内嵌 config 时的兜底）
#   KERNEL_TARGET   默认 Image；MTK 树自带 DTB 时可用 Image.gz-dtb
#   LCMPATCH=0      不改 LCM 驱动（默认会把节点名/GPIO 属性名改成 DTS 里真实存在的）
#   ADB_NOAUTH=0    不关闭 adb 授权要求（默认会改 ramdisk 的 ro.adb.secure=0）
#   STOCK_BOOT      原厂 boot.img 路径（有则从中提取真实 .config，并作为打回模板）
#   INPUT_DEFCONFIG 外部 defconfig 文件（社区补丁仓库的 h3000s_defconfig）
#   INPUT_DTB       外部 DTB（社区修正版），优先于从 boot.img 里拆出的那份
#   TOOLCHAIN       gcc49（默认）| host
#   DRY_RUN         1 = 只做工具链/源码/配置三段校验，不编译
#   JOBS            并行度
set -euo pipefail

# 脚本版本号：日志第 1 步会打印，用来确认跑的是不是最新版
BUILD_SH_VERSION="v25-binder-miscdev"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${WORK:-$ROOT/work}"
OUT="${OUT:-$ROOT/out}"
PY="${PY:-python3}"

KERNEL_REPO="${KERNEL_REPO:-https://github.com/Haoduo9/android_kernel_bbk_h3000s.git}"
KERNEL_BRANCH="${KERNEL_BRANCH:-lineage-15.1-h3000s}"
# 编哪个提交。留空 = 用分支最新提交（默认）。
# 注意：H3000S 支持只有 442b953 这一个提交，它的父提交是 2018 年 8 月的
# 原始 cappu 代码，没有 S3 Pro 支持，所以别往 HEAD~1 走。
# 需要指定某个提交时在这里填哈希，例如 KERNEL_REF="442b953"。
KERNEL_REF="${KERNEL_REF:-}"
DEFCONFIG="${DEFCONFIG:-h3000s_defconfig}"
KERNEL_TARGET="${KERNEL_TARGET:-Image}"
STOCK_BOOT="${STOCK_BOOT:-$ROOT/inputs/boot.img}"
INPUT_CONFIG="${INPUT_CONFIG:-$ROOT/inputs/config.txt}"
INPUT_DEFCONFIG="${INPUT_DEFCONFIG:-$ROOT/inputs/defconfig}"
INPUT_DTB="${INPUT_DTB:-$ROOT/inputs/boot.dtb}"
TOOLCHAIN="${TOOLCHAIN:-gcc49}"
JOBS="${JOBS:-$(nproc)}"

# 宿主工具（conf/dtc/modpost/fixdep）必须带这些 flag：
#   -fcommon   GCC 10+ 取消默认值，老代码的重复定义会炸
#   -fno-PIE -no-pie  Ubuntu 新版默认 PIE，老内核宿主工具不是 PIC
# 关键：必须以**命令行变量**的形式传给 make，才能自动传到 scripts/kconfig
#       这种子 make 里去；写进 Makefile 或 export 都不保证生效。
HOSTCFLAGS_FIX="-Wall -Wmissing-prototypes -Wstrict-prototypes -O2 -fomit-frame-pointer -std=gnu89 -fcommon -fno-PIE -no-pie"
export HOSTCFLAGS_FIX

# 终极手段：包一层 gcc。
# 试过 HOSTCFLAGS（命令行、Makefile、export）都没用——scripts/kconfig 那套
# Makefile 有自己的一套变量，不吃 HOSTCFLAGS。那就让"编译器本身"带 flag，
# 任何 Makefile 都绕不过去。
mkdir -p "$WORK"
cat > "$WORK/hostcc.sh" <<'WRAPEOF'
#!/bin/sh
# 由 build.sh 注入：老内核宿主工具需要 -fcommon/-fno-PIE/-no-pie
exec gcc -fcommon -fno-PIE -no-pie "$@"
WRAPEOF
chmod +x "$WORK/hostcc.sh"
HOSTCC_FIX="$WORK/hostcc.sh"
export HOSTCC_FIX

mkdir -p "$WORK" "$OUT"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m[error] %s\033[0m\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
step "1/6 主机依赖（脚本版本 $BUILD_SH_VERSION）"
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
    # depth 50：要能拿到 HEAD~1，depth 1 不够
    git clone --depth 50 -b "$KERNEL_BRANCH" "$KERNEL_REPO" "$SRC" \
        || git clone --depth 50 "$KERNEL_REPO" "$SRC"
fi
cd "$SRC"
if [ -n "$KERNEL_REF" ]; then
    if git rev-parse --verify -q "$KERNEL_REF" >/dev/null 2>&1; then
        git checkout -q "$KERNEL_REF" || warn "切到 $KERNEL_REF 失败"
        echo "已切到 $KERNEL_REF"
    else
        warn "找不到 $KERNEL_REF（仓库历史太短？），继续用分支最新提交"
    fi
fi
echo "当前提交: $(git log -1 --format='%h %s' 2>/dev/null || echo '(无 git 信息)')"
echo "提交时间: $(git log -1 --format='%ci' 2>/dev/null || echo '-')"

# 如果树里自带 h3000s 的 DTS，就让 kbuild 自己把 DTB 打进 Image.gz-dtb。
# 理由：源码里的屏驱动已经换了一套 GPIO 定义（GPIO_LCD_1V8_EN / RST_EN_NEW），
# 而我们外挂的是从成品内核里拆出来的老 DTB（只有 gpio_lcd_enp/enn）——
# 对不上，内核会直接起不来。
AUTO_DTB=0
if [ "${KERNEL_TARGET_KEEP:-0}" != "1" ] && [ "$KERNEL_TARGET" = "Image" ]; then
    if ls arch/arm64/boot/dts/*h3000s* >/dev/null 2>&1; then
        KERNEL_TARGET="Image.gz-dtb"
        AUTO_DTB=1
        echo "发现自带 DTS: $(ls arch/arm64/boot/dts/*h3000s* | tr '\n' ' ')"
        echo "改用 Image.gz-dtb，让内核自己打包 DTB（不再外挂老 DTB）"
    else
        echo "树里没有 h3000s 的 DTS，继续用外挂 DTB"
    fi
fi

echo "编译目标: $KERNEL_TARGET"

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

# 老内核（3.18）+ 新宿主编译器（GCC 10+）的经典冲突：
# GCC 10 起默认 -fno-common，而 dtc 里 yylloc 在 lexer/parser 各定义了一次，
# 旧的 -fcommon 会把它们合并，新的直接链接报 multiple definition。
# 两步一起做：给宿主工具加回 -fcommon，并把 lexer 里的那份改成 extern。
# 老内核（3.18）+ 新宿主 GCC 的两个必踩坑：
#   ① GCC 10+ 取消 -fcommon 默认值  → dtc 的 yylloc 重复定义
#   ② Ubuntu 新版默认 PIE           → 宿主工具(conf/fixdep)链接报
#      "relocation R_X86_64_32 ... can not be used when making a PIE object"
#
# 关键点：这些宿主工具是在**子 make** 里编的（scripts/Makefile.host），
# 顶层 Makefile 改了 HOSTCFLAGS 但如果没 export，子 make 根本收不到。
# 所以这里不只改值，还显式 export 一次。
{
    echo ""
    echo "# ===== build.sh 追加：兼容新宿主编译器 ====="
    echo "HOSTCFLAGS += -fcommon -fno-PIE -no-pie"
    echo "HOSTCXXFLAGS += -fcommon -fno-PIE -no-pie"
    echo "export HOSTCFLAGS HOSTCXXFLAGS"
} >> Makefile
echo "已追加宿主工具兼容开关，当前定义："
grep -nE '^HOST(CFLAGS|CXXFLAGS)|^export HOST(CFLAGS|CXXFLAGS)' Makefile | tail -6 | sed 's/^/  /'

# 第三层保险：直接改内核树里脚本 Makefile 的原文。
# 前面试过"改顶层变量"和"命令行传变量"，都依赖 make 的变量传递规则；
# 这里直接改链接命令本身，只要文件被 sed 到就一定生效。
if [ -f scripts/Makefile.host ]; then
    # 编译：给宿主 C 文件加 -fno-PIE -fcommon
    sed -i 's|^_hostc_flags   = \$(HOSTCFLAGS)|_hostc_flags   = -fno-PIE -fcommon $(HOSTCFLAGS)|' \
        scripts/Makefile.host 2>/dev/null || true
    # 链接：在 $(HOSTCC) 后面硬插 -no-pie
    sed -i -E 's|^(cmd_ld[[:space:]]*=[[:space:]]*\$\(HOSTCC\))|\1 -no-pie|' \
        scripts/Makefile.host 2>/dev/null || true
    echo "scripts/Makefile.host 关键行："
    grep -nE '_hostc_flags|^cmd_ld' scripts/Makefile.host | head -5 | sed 's/^/  /'
fi
for f in scripts/dtc/dtc-lexer.lex.c scripts/dtc/dtc-lexer.l; do
    if [ -f "$f" ] && grep -qE '^[[:space:]]*(YYLTYPE|int)[[:space:]]+yylloc;' "$f"; then
        sed -i -E 's/^([[:space:]]*)(YYLTYPE|int)([[:space:]]+)yylloc;[[:space:]]*$/\1extern \2\3yylloc;/' "$f"
        echo "已把 $f 里的 yylloc 改成 extern"
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
    echo "将执行: make ARCH=arm64 HOSTCC=$HOSTCC_FIX $DEFCONFIG"
    make ARCH=arm64 HOSTCC="$HOSTCC_FIX" HOSTCFLAGS="$HOSTCFLAGS_FIX" "$DEFCONFIG"
fi

# 老内核 + 新环境常见的几处坑，按需兜底开启，保证能过编译
enable() {
    grep -q "^$1=y" .config || echo "$1=y" >> .config
}
enable CONFIG_IKCONFIG
enable CONFIG_IKCONFIG_PROC

make ARCH=arm64 HOSTCC="$HOSTCC_FIX" HOSTCFLAGS="$HOSTCFLAGS_FIX" olddefconfig

# ★ Android 8 的 HAL 全靠 /dev/hwbinder 和 /dev/vndbinder 通信。
#   这棵树是 MTK 魔改版 binder：drivers/staging/android/binder.c 里只注册了一个
#   miscdevice("binder")，压根没有 hwbinder / vndbinder，Kconfig 里也没有
#   ANDROID_BINDER_DEVICES 这个符号（实测追加配置会被 olddefconfig 丢掉）。
#   结果：hwservicemanager 打不开 /dev/hwbinder 直接 abort → 所有 HAL 起不来
#         → SurfaceFlinger 拿不到显示 HAL → 屏幕永远黑着。
#
#   所以用脚本给源码补上两个 miscdevice（只有新增，不改原有逻辑）。
if [ -f "$ROOT/scripts/patch_binder.py" ]; then
    echo "--- 给 binder 驱动补 hwbinder / vndbinder ---"
    "$PY" "$ROOT/scripts/patch_binder.py" . || warn "binder 补丁失败"
else
    warn "scripts/patch_binder.py 不存在，/dev/hwbinder 仍然不会有"
fi

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
make ARCH=arm64 HOSTCC="$HOSTCC_FIX" HOSTCFLAGS="$HOSTCFLAGS_FIX" olddefconfig

# ---- 屏驱动补丁：必须在 .config 生成之后、make 之前 ----
# ★ 屏点不亮的真正原因（三个问题叠加，缺一不可）：
#
#   1) 驱动去 of_find_compatible_node(NULL,NULL,"mediatek,mt8173-lcm") 找节点，
#      但设备树里根本没有这个 compatible（原厂 7.0 / FIXED5 / 作者成品三份 DTB
#      全都没有）→ node = NULL → 后面所有 of_get_named_gpio 返回 -2 → 屏永不点亮。
#      设备树里真正带 GPIO 属性的节点是 "mediatek,nt35523_fhd"。
#
#   2) 属性名对不上：驱动查 gpio_lcm_vsp_en/vsn/rst_en/rst_en_new/1v8_en，
#      设备树里只有 gpio_lcd_enp / gpio_lcd_enn / gpio_lcd_rst / gpio_lcd_id。
#
#   3) 硬件只有 4 个 GPIO（id/enp/enn/rst），驱动要 5 个 → 必然有两个指到同一引脚
#      → 第二次 gpio_request 返回 -EBUSY → 代码里 `goto out;` 直接放弃整个初始化。
#      去掉 goto 之后 out: 标签没人引用，-Werror 会报 unused-label，所以标签一起删。
#
#   映射关系：vsp→enp  vsn→enn  rst_en/rst_en_new→rst  1v8→enp（硬件无独立 1.8V 脚）
#   不想要这个行为就设 LCMPATCH=0。
if [ "${LCMPATCH:-1}" = "1" ]; then
    LCM_NAME=$(sed -n 's/^CONFIG_CUSTOM_KERNEL_LCM="\(.*\)"$/\1/p' .config 2>/dev/null | head -1 || true)
    LCM_DIR="drivers/misc/mediatek/lcm/$LCM_NAME"
    echo "当前选中的屏驱动: $LCM_NAME"
    if [ -z "$LCM_NAME" ] || [ ! -d "$LCM_DIR" ]; then
        warn "找不到 $LCM_DIR，退回全目录搜索"
        LCM_DIR="drivers/misc/mediatek/lcm"
    fi

    LCM_FILES=$(grep -rlE 'gpio_lcm_|mediatek,mt8173-lcm' "$LCM_DIR" 2>/dev/null || true)
    if [ -n "$LCM_FILES" ]; then
        for f in $LCM_FILES; do
            sed -i 's/"mediatek,mt8173-lcm"/"mediatek,nt35523_fhd"/g' "$f"
            sed -i 's/gpio_lcm_rst_en_new/gpio_lcd_rst/g' "$f"
            sed -i 's/gpio_lcm_rst_en/gpio_lcd_rst/g' "$f"
            sed -i 's/gpio_lcm_vsp_en/gpio_lcd_enp/g' "$f"
            sed -i 's/gpio_lcm_vsn_en/gpio_lcd_enn/g' "$f"
            sed -i 's/gpio_lcm_1v8_en/gpio_lcd_enp/g' "$f"
            echo "已改: $f"

            if grep -q 'lcm_get_gpio_infor' "$f"; then
                sed -i 's/goto out;/;/' "$f"
                sed -i 's/^[[:space:]]*out:[[:space:]]*$//' "$f"
                echo "  已去掉 goto out 和 out: 标签"
                sed -n '/void lcm_get_gpio_infor/,/^}/p' "$f" > /tmp/lcm_after.txt
                echo "  --- 改后的函数（只看关键行）---"
                grep -nE 'of_find_compatible_node|of_get_named_gpio|goto out|out:' /tmp/lcm_after.txt 2>/dev/null | sed 's/^/    /' || true
            fi
        done
    else
        warn "在 $LCM_DIR 里没找到需要改的字符串，跳过"
    fi
fi
grep -E '^CONFIG_LOCALVERSION' .config || true

# 把和上次链接失败相关的开关打出来，方便下一轮定位到底缺哪个提供者
echo "--- 电池 / SDIO / vcore 相关开关 ---"
grep -E '^CONFIG_MTK_(BATTERY|SDIO|VCORE|CHARGER|SMART)' .config || echo "  （一个都没有）"

# olddefconfig 会悄悄丢掉依赖不满足的选项。把 defconfig 里写了、
# 但最终 .config 里没有的键列出来——这些往往就是"驱动没编进去"的真凶。
if [ -f "arch/arm64/configs/$DEFCONFIG" ]; then
    echo "--- defconfig 里有、最终配置里没有的键（可能被 olddefconfig 丢了）---"
    grep -E '^CONFIG_[A-Z0-9_]+=' "arch/arm64/configs/$DEFCONFIG" \
        | sed 's/=.*//' | sort -u > "$WORK/cfg_want.txt"
    grep -E '^CONFIG_[A-Z0-9_]+=' .config \
        | sed 's/=.*//' | sort -u > "$WORK/cfg_has.txt"
    comm -23 "$WORK/cfg_want.txt" "$WORK/cfg_has.txt" | head -40 | sed 's/^/  /' || true
    echo "  （以上若为空，说明配置完全按 defconfig 生效）"
fi

# 社区树的 h3000s_defconfig 不完整：链接时 bm_ctrl_cmd / sdio_* 没有实现者。
# 先注入 weak 占位让内核能链接出来（weak 会被真实实现覆盖），
# 目的是先拿到一个能开机的东西，再回头把配置补对。
# 不想要这个行为就设 ALLOW_STUBS=0。
if [ "${ALLOW_STUBS:-1}" = "1" ]; then
    STUB_DIR="drivers/misc/mediatek"
    if [ -d "$STUB_DIR" ]; then
        cat > "$STUB_DIR/h3000s_stubs.c" <<'STUBEOF'
/*
 * 由 build.sh 自动生成的临时占位。
 * 原因：h3000s_defconfig 没有启用 bm_ctrl_cmd / sdio_start_ot_transfer 的提供者，
 * 链接 vmlinux 时报 undefined reference。weak 符号可被真实实现覆盖。
 */
__attribute__((weak)) int bm_ctrl_cmd(void *arg) { (void)arg; return 0; }
__attribute__((weak)) int sdio_stop_transfer(void *arg) { (void)arg; return 0; }
__attribute__((weak)) int sdio_start_ot_transfer(void *arg) { (void)arg; return 0; }
STUBEOF
        if ! grep -q 'h3000s_stubs.o' "$STUB_DIR/Makefile" 2>/dev/null; then
            echo 'obj-y += h3000s_stubs.o' >> "$STUB_DIR/Makefile"
        fi
        echo "已注入 weak 占位：$STUB_DIR/h3000s_stubs.c"
    fi
fi

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
make ARCH=arm64 CROSS_COMPILE="$CROSS_COMPILE" HOSTCC="$HOSTCC_FIX" HOSTCFLAGS="$HOSTCFLAGS_FIX" \
        -j"$JOBS" "$KERNEL_TARGET" 2>&1 | tee "$BUILD_LOG"
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
    if [ "$AUTO_DTB" = "1" ]; then
        echo "内核已自带 DTB（Image.gz-dtb），不再外挂"
    elif [ -f "$INPUT_DTB" ]; then
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

    if [ "$AUTO_DTB" = "1" ]; then
        echo "跳过压缩：$IMG 已经是 Image.gz-dtb"
    elif [ "$KIND" = "gzip" ] && [ "${IMG##*.}" != "gz" ]; then
        echo "原厂是压缩内核，把新 Image 压成 Image.gz"
        gzip -9 -n -c "$IMG" > "$OUT/Image.gz"
        IMG="$OUT/Image.gz"
    fi
    "$PY" "$ROOT/scripts/bootimg.py" splice \
        --template "$STOCK_BOOT" --kernel "$IMG" "${SPLICE_EXTRA[@]}" --out "$OUT/boot.img"

    # 屏不亮的时候根本没法点"允许 USB 调试"，adb 会一直 unauthorized，
    # 日志抓不到。把 ramdisk 里的 ro.adb.secure 改成 0，adb 直接连。
    # 不想关就设 ADB_NOAUTH=0。
    if [ "${ADB_NOAUTH:-1}" = "1" ]; then
        if [ -f "$ROOT/scripts/patch_ramdisk.py" ]; then
            echo "--- 关闭 adb 授权要求（ro.adb.secure=0），方便抓开机日志 ---"
            "$PY" "$ROOT/scripts/patch_ramdisk.py" "$OUT/boot.img" \
                || warn "改 ramdisk 属性失败，adb 仍需要授权"
        else
            warn "scripts/patch_ramdisk.py 不存在：adb 仍然需要授权（屏不亮时没法点同意）"
            warn "想要免授权，把 scripts/patch_ramdisk.py 也传到仓库里"
        fi
    fi


    # 自检：内核里必须带 binder,hwbinder,vndbinder，否则 /dev/hwbinder 不会创建，
    # Android 8 的 HAL 全部起不来（作者的内核里就有这个字符串）。
    if grep -qa 'binder,hwbinder,vndbinder' "$IMG"; then
        echo "自检通过：内核包含 binder,hwbinder,vndbinder"
    else
        die "内核里没有 binder,hwbinder,vndbinder —— /dev/hwbinder 不会出现，HAL 起不来"
    fi
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
