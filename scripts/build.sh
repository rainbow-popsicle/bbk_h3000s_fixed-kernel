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
BUILD_SH_VERSION="v27-check-uncompressed"

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
# 作者出货内核的 DTB（触控节点 cap_touch，和树里的 GT911 驱动匹配）优先于树里编的 FIXED5 DTS
USE_INPUT_DTB="${USE_INPUT_DTB:-1}"
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
# 前置检查：确认所有补丁脚本都在。
#   为什么要有这一步：CI 是从 GitHub 仓库 checkout 的，如果某个 scripts/*.py
#   没被推上去，后面只会走到某个 [verify] 闸门报“补丁未生效”，
#   完全看不出真正原因是“文件压根没上传”，白烧一次编译。
step "0/6 检查补丁脚本是否齐全"
MISSING=""
for s in \
    patch_binder_modern.py \
    patch_gt911_touch.py patch_gt911_keys.py \
    patch_sensors.py \
    patch_battery_qmax.py patch_battery_soc.py patch_battery_fgadc.py \
    patch_lcm_stock.py patch_lcm_init.py patch_lcm_table.py \
    patch_lcm_gpio_fix.py patch_lcm_init_table.py \
    patch_lcm_debug.py patch_lcm_esd_off.py patch_dsi_start.py \
    patch_disp_debug.py patch_dsi_config_start.py patch_resume_no_wait_token.py patch_vdo_eof_event.py patch_no_stop_loop.py patch_lcm_trace.py patch_lcm_power_seq.py patch_lcm_readid.py \
    patch_dimming_cmdmode.py patch_dimming_early.py patch_rst_new.py patch_stock_timing.py patch_lab_push.py \
    patch_lk_replay.py \
    patch_video_kick.py patch_vmcmd.py \
    patch_cm36686_i2c.py patch_cm36686_dts.py \
    patch_alsps_eint.py patch_alsps_ap3220.py patch_alsps_abs.py \
    patch_vcodec_irq.py patch_vcodec_clk.py patch_bt_bluez.py \
    patch_ramdisk.py \
    bootimg.py
do
    [ -f "$ROOT/scripts/$s" ] || MISSING="$MISSING $s"
done
if [ -n "$MISSING" ]; then
    printf '\033[1;31m[error] 以下补丁脚本在仓库里缺失：\033[0m\n' >&2
    for s in $MISSING; do
        printf '\033[1;31m          scripts/%s\033[0m\n' "$s" >&2
    done
    printf '\033[1;31m[error] CI 是从 GitHub 仓库 checkout 的，把缺失文件推到仓库后重跑。\033[0m\n' >&2
    exit 1
fi
echo "  全部补丁脚本都在 ✓（共 22 个）"

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
    if [ "${USE_INPUT_DTB:-0}" = "1" ] && [ -s "$INPUT_DTB" ]; then
        # 优先用外部 DTB：树里的 h3000s.dts 等同于公开的 FIXED5，
        # 而作者实际出货的内核用的是另一版 DTB（触控节点 cap_touch、带 reg-vcamio-p2-supply），
        # 想和他那套“能显示”的硬件配置完全对齐，就必须用他 boot.img 里拆出来的这份。
        echo "使用外部 DTB（$INPUT_DTB，$(stat -c%s "$INPUT_DTB" 2>/dev/null || wc -c < "$INPUT_DTB") 字节），不再编树里的 DTS"
        echo "编译目标保持 Image.gz，打包时把这份 DTB 追加进去"
        KERNEL_TARGET="Image.gz"
    elif ls arch/arm64/boot/dts/*h3000s* >/dev/null 2>&1; then
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

# ★ Android 8.1 的 HAL 全靠 /dev/hwbinder 与 /dev/vndbinder，而且 8.1 的
#   libbinder/hwbinder 会发 BC_TRANSACTION_SG（命令 17）并携带 BINDER_TYPE_PTR /
#   BINDER_TYPE_FDA 对象。本树自带的 binder 是 Android 6/7 时代的老驱动
#   （单文件、单 context、无 SG、无 PTR/FDA）。实机日志证据：
#     23 次  binder: BINDER_SET_CONTEXT_MGR already set          (-EBUSY)
#     603 次 binder: unknown command 1078485777                  (=0x40486311, SG)
#     241 次 BC_TRANSACTION_SG buffers_size=24/80/88/96/104      (无法投递)
#   结果：vndservicemanager 起不来 → vendor HAL 注册全失败(-2147483648)
#         → SurfaceFlinger 拿不到 gralloc → IMGeglMakeCurrent 崩 → 黑屏。
#
#   所以整套换成 cappu（小米平板3，3.18.123）的现代 binder：
#     多设备 context（默认 binder,hwbinder,vndbinder）+ SG + PTR/FDA 全套，
#     仍是 3.18 时代 API（无 get_user_pages/refcount_t/kvmalloc/timer_setup），
#     且本树已有 security_binder_* 四个钩子 → 可直接替换。
if [ -f "$ROOT/scripts/patch_binder_modern.py" ]; then
    echo "--- 安装现代 binder 驱动（多设备 context + SG + PTR/FDA）---"
    "$PY" "$ROOT/scripts/patch_binder_modern.py" . \
        --src "$ROOT/kernel-patches/binder-3.18" || die "现代 binder 安装失败"
    # 安装后立刻自检
    if grep -q 'binder_devices_param' drivers/staging/android/binder.c \
       && grep -q 'BINDER_TYPE_PTR' drivers/staging/android/binder.c \
       && grep -q 'binder_alloc.o' drivers/staging/android/Makefile \
       && grep -q 'config ANDROID_BINDER_DEVICES' drivers/staging/android/Kconfig; then
        echo "现代 binder 安装自检：通过"
    else
        die "现代 binder 安装自检失败（驱动/Makefile/Kconfig 检查不通过）"
    fi
else
    die "scripts/patch_binder_modern.py 不存在 —— 老 binder 在 8.1 上跑不通"
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

# ★ 光感核心层 + cm36686 驱动必须真的编进内核，否则对 alsps.c 的任何修改
#   （见 scripts/patch_alsps_abs.py）都不会被编译，等于没改。
#   为什么需要这道兜底（实机证据，2026-11-20）：
#     · 本机 m_alsps_input 存在，且 /sys/module 下没有 alsps/cm36686 模块
#       → 它是内建（obj-y），不是 ko；
#     · 但仓库里的 arch/arm64/configs/h3000s_defconfig 第 234 行却是
#           # CONFIG_CUSTOM_KERNEL_ALSPS is not set
#       两者对不上，说明实际出包用的配置不是这份 defconfig（或曾经手工改过）。
#       仓库副本一旦被当成基线使用，整个 ALSPS 会静默消失，光感直接没有。
#   对已经打开的配置是幂等的；对“配置漂了”的情况是保险。
enable CONFIG_CUSTOM_KERNEL_ALSPS
enable CONFIG_MTK_CM36686_NEW

make ARCH=arm64 HOSTCC="$HOSTCC_FIX" HOSTCFLAGS="$HOSTCFLAGS_FIX" olddefconfig

# 兜底是否真的落地：olddefconfig 在符号不存在/依赖不满足时会把它丢掉，
# 这里当场发现，而不是等编完刷机才发现光感又没了。
if [ -f drivers/misc/mediatek/alsps/alsps.c ]; then
    grep -q "^CONFIG_CUSTOM_KERNEL_ALSPS=y" .config \
        || die "配置里 CONFIG_CUSTOM_KERNEL_ALSPS 没能打开 —— ALSPS 核心层不会被编译（光感消失）"
    grep -q "^CONFIG_MTK_CM36686_NEW=y" .config \
        || die "配置里 CONFIG_MTK_CM36686_NEW 没能打开 —— cm36686 驱动不会被编译（光感读不到数）"
    echo "  [config] ALSPS 核心层 + cm36686 驱动已编入内核 ✓"
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

# ★ 用原厂内核提取的完整初始化表替换驱动里的残缺表。
#   证据链：
#     1) 开机画面能显示，但 lcm_resume 从未被调用（靠 bootloader 初始化）
#     2) 熄屏断电后，唤醒时 lcm_resume 第一次真正初始化面板 → 黑屏
#     3) 诊断打印确认：唤醒后 mipitx=2 dsiclk=2、DSI 不 busy、
#        面板 GPIO(985/987/1016) 全高、SurfaceFlinger 正常出图 —— 通路全好
#     4) 原厂/作者内核的初始化表有 109 项（含 25 条 gamma + display-on 0x29），
#        我们的表只有 12 项且内容不同 → 面板收不到有效配置
#   表数据来自原厂 7.0 内核二进制（kernel-patches/lcm-table/nt35523_fhd_init.c）
# ★ 按原厂驱动重写面板回调（默认开启 LCM_STOCK=1）
#
#   依据：反汇编原厂 3.18.35 内核里的 nt35523_fhd_vdo_boe 驱动得到，不是猜的。
#   原厂 LCM_DRIVER 结构体 @0xe8bcd0 的函数指针：
#     init           0x3d9198  variant → 推表A(23) 或 表B(109)
#     suspend        0x3d916c  只推睡眠表 4 项（0x28 / 0x10 / DELAY 120），不动 GPIO
#     resume         0x3d9240  rst=1→5ms→rst=0→2ms→rst=1→25ms
#                              → vsp=1→2ms → vsn=1→2ms → 推表（A 或 B 二选一）
#     suspend_power  0x3d9390  rst=0→5ms, vsn=0→2ms, vsp=0→2ms
#     resume_power   0x3d8ed8  1V8 上电 + 20ms
#   三张表紧邻存放于 0xe89690：睡眠表 4 项 / 表A 23 项 / 表B 109 项
#
#   本树原实现的问题（本步全部修正）：
#     * GPIO 顺序相反：先 VSP/VSN 再复位（原厂先复位再 VSP/VSN）
#     * 延时 12/12/32/24/12，原厂 5/2/25/2/2
#     * 把表A+表B 当成一张 132 项表全推（原厂二选一）
#     * 睡眠表 5 项（含 0x53 和两段延时），原厂只有 4 项
#     * suspend 里调 ddp_dsi_power_off、拉低所有 rails、还写本机没有的 tps65132
#
#   S3PRO_CLEANUP: 表选择节点已移除，固定为实测的表A（variant=0）。
#   ★2026-11-22 表来源：反汇编原厂 lcm_get_params 证实原厂屏=1536x2048，
#     原厂 LK 用 BOE 表(23项)能亮。16:05"BOE表错配"和 16:26"分辨率错配"均已自我推翻。
#     真正差异在时序参数(见 patch_stock_timing.py)，不在表。
#     SHARP_TABLES 默认 0 = 用原厂 BOE 表(能亮的那一套)；1=夏普表(实验用,已作废方向)。
if [ "${SHARP_TABLES:-0}" = "1" ]; then
    LCM_TABLE_FILE="$ROOT/kernel-patches/lcm-table/sharp_tables.c"
    echo "--- 使用夏普表（实验方向，已作废）---"
else
    LCM_TABLE_FILE="$ROOT/kernel-patches/lcm-table/stock_tables.c"
    echo "--- 使用原厂 BOE 表（原厂能亮的那一套）---"
fi
# ---- 面板 GPIO 修复补丁：默认关闭（已被上面的 sed 覆盖，不要重复）----
# 上面行 ~500 处的 sed 已经完成了同样的事：
#   "mediatek,mt8173-lcm" -> "mediatek,nt35523_fhd"
#   gpio_lcm_1v8_en/vsp_en -> gpio_lcd_enp, gpio_lcm_vsn_en -> gpio_lcd_enn
#   gpio_lcm_rst_en/rst_en_new -> gpio_lcd_rst
#   goto out; -> ;   （避免第二次 request 返回 -EBUSY 时放弃整个初始化）
# 所以 patch_lcm_gpio_fix.py 会因锚点不匹配而失败，默认不跑。
# 如需启用（例如换了一棵没做过 sed 的树），设 LCM_GPIO_FIX=1。
if [ "${LCM_GPIO_FIX:-0}" = "1" ] && [ -f "$ROOT/scripts/patch_lcm_gpio_fix.py" ]; then
    echo "--- 面板 GPIO 修复补丁（node/属性名两级回退）---"
    "$PY" "$ROOT/scripts/patch_lcm_gpio_fix.py" . || die "面板 GPIO 修复失败"
else
    echo "跳过面板 GPIO 修复补丁（已由前置 sed 完成；LCM_GPIO_FIX=${LCM_GPIO_FIX:-0}）"
fi

if [ "${LCM_STOCK:-1}" = "1" ] && [ "${LCMPATCH:-1}" = "1" ] \
   && [ -f "$ROOT/scripts/patch_lcm_stock.py" ]; then
    echo "--- 按原厂实现重写面板驱动（反汇编依据）---"
    "$PY" "$ROOT/scripts/patch_lcm_stock.py" . \
        --tables "$LCM_TABLE_FILE" || die "原厂驱动重写失败"
else
    echo "跳过原厂驱动重写（LCM_STOCK=${LCM_STOCK:-1}）"
fi

# ★★★ lcm_init 三级选表 + 推表（默认开）—— 对齐 LOS 14.1
#   LOS 14.1 的 lcm_init @0x458344 是三级选表（见 docs §8.1）：
#     vendor id(cmdline lcm_vendor_id=)==1 -> 291 项表
#     否则读 GPIO lcd_id(pio 89)：电平 0 -> 23 项(BOE) / 1 -> 109 项
#   而我们的 lcm_init 只有 gpio+hwinfo 两行，从不推表 —— 开机亮全靠 LK。
#   本补丁让 kernel 在开机时也主动推一次表，并打印 vendor id / pio89 电平 / 选中哪张表。
#   必须在 patch_lcm_stock.py 之后（stock 会重写驱动，但不动 lcm_init）。
if [ "${LCM_INIT_TABLE:-1}" = "1" ] && [ -f "$ROOT/scripts/patch_lcm_init_table.py" ]; then
    echo "--- lcm_init 三级选表 + 推表（对齐 LOS 14.1）---"
    "$PY" "$ROOT/scripts/patch_lcm_init_table.py" . || die "lcm_init 三级选表失败"
else
    echo "跳过 lcm_init 三级选表（LCM_INIT_TABLE=${LCM_INIT_TABLE:-1}）"
fi

# ★ LK(cappu bootloader) 上电/复位时序复刻实验台（默认开，运行期 debugfs 可切）
#   驱动原始实现是 VSP/VSN 先 → RST 12/12/32（= cappu LK 的实测时序，且带 RST_EN_NEW）；
#   patch_lcm_stock 照抄 bbk 原厂 7.0（另一块屏）换成了 RST 5/2/25 → VSP/VSN、丢了 RST_EN_NEW。
#   排除清单第 5 项「改 GPIO 顺序」之所以无效，是因为当时把延时一起改了（没控制变量）。
#   本补丁把 顺序 / 三个延时 / RST_EN_NEW 拆成独立开关，
#   一次刷机即可做受控实验。默认 variant=0 = 保持现有行为不变。
#   必须在 LCM_STOCK 之后运行（依赖 s3pro_lcm_variant 与 stock 版 lcm_resume 结构）。
if [ "${LK_REPLAY:-1}" = "1" ] && [ -f "$ROOT/scripts/patch_lk_replay.py" ]; then
    echo "--- LK 时序复刻实验台（debugfs: /sys/kernel/debug/s3pro_lk/）---"
    "$PY" "$ROOT/scripts/patch_lk_replay.py" . || die "LK 时序实验台补丁失败"
else
    echo "跳过 LK 时序实验台（LK_REPLAY=${LK_REPLAY:-1}）"
fi


# ★★ 核心修复：DSI 时序参数改为原厂 3.18.35 实测值（默认开）。
#   反汇编原厂 lcm_get_params @0x458f40，用 LANE_NUM=4 锚点反推 DSI 起始 0x1d4，
#   得到原厂时序 v_bp=40/v_fp=40/h_sync=4/h_bp=200/h_fp=200；
#   而移植驱动(小米平板3)用 8/14/2/55/55 —— H/V blanking 不匹配 → 熄屏唤醒黑屏。
#   开机能亮是 LK 用原厂时序初始化的；kernel 重建用错时序就黑。
#   必须在 LCM_STOCK 之后运行（get_params 不在 stock 重写区间，锚点保留，已实测）。
if [ "${STOCK_TIMING:-1}" = "1" ] && [ -f "$ROOT/scripts/patch_stock_timing.py" ]; then
    echo "--- 核心修复：DSI 时序改回原厂值（40/40/4/200/200）---"
    "$PY" "$ROOT/scripts/patch_stock_timing.py" . || die "原厂时序修复失败"
else
    echo "跳过原厂时序修复（STOCK_TIMING=${STOCK_TIMING:-1}）"
fi


# ★ 修复候选：补回 cappu 特有的 PMIC 复位脚 RST_EN_NEW(pio6397 14)（默认关·已证伪）。
#   patch_lcm_stock 照搬 bbk 原厂 3.18.35（不同设备、无 cappu 的 RST_NEW 概念），
#   重写 lcm_resume/suspend_power 时把 RST_EN_NEW 丢了；本地 legacy 驱动与 LK
#   都会同步拉这个脚。开机 kernel 不调 lcm_resume（LK 初始化直接亮），只有熄屏
#   唤醒走 kernel 重建——怀疑缺这个复位脚导致重建后面板不显示。
#   实测结论：两个 RST 都是 1016 同一脚 → 空操作，已证伪。
#   ★ 与 LK_REPLAY 互斥：两者都改 RST_EN_NEW，且本补丁锚点是 stock 版 lcm_resume 原文，
#     而 LK_REPLAY 会整体替换该函数（后者已自带 RST_EN_NEW 开关，功能覆盖）。
if [ "${LK_REPLAY:-1}" = "1" ] && [ "${RST_NEW_FIX:-0}" = "1" ]; then
    die "LK_REPLAY 与 RST_NEW_FIX 互斥：LK_REPLAY 已含 RST_EN_NEW 开关（debugfs 可切），请关掉 RST_NEW_FIX"
fi
if [ "${RST_NEW_FIX:-0}" = "1" ] && [ -f "$ROOT/scripts/patch_rst_new.py" ]; then
    if [ "${LCM_STOCK:-1}" != "1" ]; then
        die "rst_new 修复需要 LCM_STOCK=1（锚点为 stock 形态的 resume）"
    fi
    echo "--- 修复候选：补回 RST_EN_NEW 复位脚（pio6397）---"
    "$PY" "$ROOT/scripts/patch_rst_new.py" . || die "RST_NEW 修复失败"
else
    echo "跳过 RST_NEW 修复（RST_NEW_FIX=${RST_NEW_FIX:-0}）"
fi


# ★ 替换面板初始化表（旧方案，默认关闭 LCM_TABLE=0）
#
#   旧方案把 23+109 两张表当成一张 132 项表推进去，原厂实际是二选一。
#   已由上面的 LCM_STOCK 取代；保留此步仅用于回退对比。
if [ "${LCM_TABLE:-0}" = "1" ] && [ "${LCMPATCH:-1}" = "1" ] \
   && [ -f "$ROOT/scripts/patch_lcm_table.py" ]; then
    echo "--- 替换面板初始化表（旧方案，132 项）---"
    "$PY" "$ROOT/scripts/patch_lcm_table.py" . \
        --table "$ROOT/kernel-patches/lcm-table/nt35523_fhd_init.c" || die "面板初始化表替换失败"
else
    echo "跳过面板初始化表替换（LCM_TABLE=${LCM_TABLE:-0}）"
fi


# ★ 面板初始化时序（实验性，默认关闭 LCM_INIT_PATCH=0）
#
#   背景：本树驱动的 lcm_init() 是空壳（只取 GPIO），真正的上电+复位+下发初始化表
#   全在 lcm_resume() 里，而 resume 要等 SurfaceFlinger 打开显示（本机 81 秒）才
#   第一次被调用。作者内核在开机 0.638 秒就完成初始化。
#
#   实机三轮结果（这是关键证据，别再盲目叠加）：
#     v1 只做 sed 改名            → 有开机动画 ✅
#     v2 + 本补丁（lcm_init 里初始化）→ 开机动画消失 ❌
#     v3 + 关 ESD（本补丁内）      → 阴阳屏：左半纯黑、右半灰色虚线、渐变消失 ❌
#
#   结论：v2/v3 反而破坏了 v1 已经能工作的显示路径。原因是本树的面板驱动是
#   nt35523_wqxga_dsi_vdo_sharp（1536x2048，双 DSI 拆 768+768），而原厂 DTB 的
#   compatible 是 mediatek,nt35523_fhd，原厂内核里真正的驱动叫 nt35523_fhd_vdo_boe
#   —— 这个驱动【不在本树里】，所以分辨率/时序/初始化表全都是错的。
#   在拿到正确的面板驱动之前，不要动显示路径，保持 v1 的行为。
#
#   想要再试这个补丁：设 LCM_INIT_PATCH=1。
if [ "${LCM_INIT_PATCH:-0}" = "1" ] && [ "${LCMPATCH:-1}" = "1" ] \
   && [ -f "$ROOT/scripts/patch_lcm_init.py" ]; then
    echo "--- [实验] 修正面板初始化时序（把上电+初始化放进 lcm_init）---"
    "$PY" "$ROOT/scripts/patch_lcm_init.py" . || die "面板初始化时序补丁失败"
else
    echo "跳过面板初始化时序补丁（LCM_INIT_PATCH=${LCM_INIT_PATCH:-0}）—— 保持能显示的 v1 行为"
fi

# ★ 关掉面板 ESD 自检。
#   实机证据（v1 版：画面正常，但约 72 秒后黑屏、背光仍亮，之后按电源键也无法恢复）：
#     [72.4s] 起 CMDQ 错误块 ×37
#     display_esd_che 相关日志 ×2308、primary_display_esd_recovery ×5
#     DSI0 / DSI1 最终都 idle（显示引擎停转），背光仍是 102
#   机制：ESD 线程每 2 秒读面板寄存器（cmd 0x0A/期望 0x9C），读超时即判定面板异常，
#         触发 ddp_dsi_stop → DSI_Reset → lcm_init，反复把显示引擎停下，画面再也回不来。
#   作者内核全程 0 次 esd_recovery。
#   本补丁【只关 ESD】，不动 lcm_init/lcm_resume 的时序（那是 v2 的改动，默认关闭）。
if [ "${LCM_ESD_OFF:-0}" = "1" ] && [ "${LCMPATCH:-1}" = "1" ] \
   && [ -f "$ROOT/scripts/patch_lcm_esd_off.py" ]; then
    echo "--- 关闭面板 ESD 自检（切断 recovery 死循环）---"
    "$PY" "$ROOT/scripts/patch_lcm_esd_off.py" . || die "关闭 ESD 失败"
else
    echo "跳过 ESD 关闭（LCM_ESD_OFF=${LCM_ESD_OFF:-0}）"
fi


# ★ 移除 ddp_dsi_start 尾部【自加】的 DSI_Start()（原厂反汇编纠错，见文档 §7.11）。
#   历史：早期据"开机 DSI_START=1 / 唤醒黑屏=0、其余寄存器一致"加过 DSI_Start()。
#   纠错：定点反汇编原厂 3.18.35 证明 ddp_dsi_start 单/双 DSI 分支都不调 DSI_Start；
#        原厂只在 ddp_dsi_trigger（每帧触发）里调，而我们 trigger 路径本来就有，
#        start 里这处属于无原厂依据的重复动作 → 移除。
#   脚本幂等：找不到标记块安全跳过；只删 start 尾部那块，不动 trigger 的 DSI_Start。
#   ⚠️ 2026-11-23 发现：源码里从来就没有那个标记块，所以本步实际是**空操作**，
#      无论开关如何都不会增删 DSI_Start。默认改成 0（本步实际为空操作）。
if [ "${DSI_START_FIX:-0}" = "1" ] && [ -f "$ROOT/scripts/patch_dsi_start.py" ]; then
    echo "--- 移除 ddp_dsi_start 自加的 DSI_Start()（对齐原厂）---"
    "$PY" "$ROOT/scripts/patch_dsi_start.py" . || die "移除自加 DSI_Start 失败"
else
    echo "跳过移除自加 DSI_Start（DSI_START_FIX=${DSI_START_FIX:-0}；该步实际为空操作）"
fi




# ★ 显示诊断打印（研究"熄屏唤醒后黑屏"用，默认开启）
#   目的：在 ddp_dsi_start / ddp_dsi_stop 结束时打印硬件状态：
#     mipitx  : MIPI TX PLL 是否使能
#     dsiclk  : DSI 控制器时钟是否使能
#     intsta  : DSI_INTSTA 原始寄存器（bit31 = busy）
#   判读：唤醒后若 mipitx 或 dsiclk 为 0，说明 MIPI PLL/DSI 时钟没恢复，
#         面板收不到信号 → 背光亮但无画面。这段只加 printk，不改逻辑。
#   研究完成后设 DISP_DEBUG=0 关掉。
if [ "${DISP_DEBUG:-1}" = "1" ] && [ -f "$ROOT/scripts/patch_disp_debug.py" ]; then
    echo "--- 加装显示诊断打印（DSI 启停状态）---"
    "$PY" "$ROOT/scripts/patch_disp_debug.py" . || die "显示诊断补丁失败"
else
    echo "跳过显示诊断打印（DISP_DEBUG=${DISP_DEBUG:-1}）"
fi

# ★★★★ ddp_dsi_config 的 video 分支补 DSI_Start（默认开）—— 死锁的「第一推动」
#   LOS 14.1 的 ddp_dsi_config @0x476b88 收尾段（0x4792f0-0x479310）是：
#       ldr w0,[x29,#0x70] / cbz -> 跳过     ; = dsi_config->mode != CMD_MODE
#       bl DSI_Start                          ; ★ 先启动 DSI
#       bl Dsi_Config_VDO_Timing              ; 再配时序
#   我们的顺序少了 DSI_Start。
#
#   为什么这一句决定生死（全链路来自源码）：
#     _should_trigger_path() 在 CMDQ 模式下 return 0 -> _trigger_display_interface 不调 dpmgr_path_trigger
#     _should_insert_wait_frame_done_token() 在 CMDQ 模式下 return 1
#       -> config 队列插入 cmdqRecWaitNoClear(CMDQ_EVENT_MUTEX0_STREAM_EOF)  等事件 53
#     _cmdq_build_trigger_loop 第一条也是 cmdqRecWait(53)（CMDQ_CODE_WFE，等待并清除）
#     而 dpmgr_path_start 里的 ddp_dsi_start 把 DSI_START 清 0 -> DSI 不输出 -> 无帧 -> 53 永不来
#       -> 两个队列全卡 -> trigger loop 里的 dpmgr_path_trigger 永不执行
#       -> ddp_dsi_trigger 永不执行 -> DSI_Start 永不调用 -> DSI_START 永远 0（闭环）
#     dpmgr_path_trigger 在 CMDQ 模式下【只有】trigger loop 一个调用点，所以 trigger loop 一卡就全死。
#
#   config 在 path_start 之前，此处 DSI_Start 能让 DSI 真实输出一帧 -> MUTEX0 产生真实的 53
#   -> 两道等待都放行 -> 链条自己转起来。（这与软件伪造事件不同，不依赖任何竞态。）
if [ "${DSI_CFG_START:-0}" = "1" ] && [ -f "$ROOT/scripts/patch_dsi_config_start.py" ]; then
    echo "--- ddp_dsi_config 补 DSI_Start（死锁第一推动，对齐 LOS 14.1）---"
    "$PY" "$ROOT/scripts/patch_dsi_config_start.py" . || die "ddp_dsi_config 点火补丁失败"
else
    echo "跳过 ddp_dsi_config 点火（DSI_CFG_START=${DSI_CFG_START:-0}）"
fi

# ★★★ video mode 的 trigger-loop 点火（默认开）—— 死锁根因的直接修复
#   背景（LOS 14.1 逆向确认，见 docs §8.19）：
#     LOS 的 primary_display_suspend 不调用 _cmdq_stop_trigger_loop，
#     trigger loop 全程不停，醒来不需要点火。
#     我们的 suspend 无条件 _cmdq_stop_trigger_loop()，resume 重启 loop 后
#     它卡在第一个等待事件上 -> ddp_dsi_start 清 START=0 -> 无 vsync/RDMA EOF
#     -> ddp_dsi_trigger 不执行 -> START 永远 0（死锁）。
#   而 _cmdq_start_trigger_loop 里 video mode 分支的点火代码被 #if 0 关掉了，
#   MTK 自己在 cmd mode 那行写的注释是「第一次必须设 STREAM_EOF，否则卡死循环」。
#   本补丁把那段 #if 0 取消，让 video mode 也在 path 空闲时补一次 MUTEX0_STREAM_EOF。
if [ "${VDO_EOF_EVENT:-0}" = "1" ] && [ -f "$ROOT/scripts/patch_vdo_eof_event.py" ]; then
    echo "--- 启用 video mode 的 trigger-loop 点火（MUTEX0_STREAM_EOF）---"
    "$PY" "$ROOT/scripts/patch_vdo_eof_event.py" . || die "video mode 点火补丁失败"
else
    echo "跳过 video mode 点火（VDO_EOF_EVENT=${VDO_EOF_EVENT:-0}）"
fi

# ★★★ 让 suspend 不停 CMDQ trigger loop（默认开）—— 对齐 LOS 14.1
#   LOS 14.1 的 primary_display_suspend @0x4a8e0c 全程不调用 _cmdq_stop_trigger_loop，
#   trigger loop 一直活着；resume 后硬件产生 MUTEX0_STREAM_EOF(53)，链条自然接上。
#   我们停了 loop，resume 重启后卡在 cmdqRecWait(53) 上：
#     ddp_dsi_start 清 DSI_START=0 -> DSI 不输出 -> 无帧 -> 53 不来
#     -> ddp_dsi_trigger 不执行 -> DSI_START 永远 0（死锁）。
if [ "${NO_STOP_LOOP:-1}" = "1" ] && [ -f "$ROOT/scripts/patch_no_stop_loop.py" ]; then
    echo "--- suspend 不停 CMDQ trigger loop（对齐 LOS 14.1）---"
    "$PY" "$ROOT/scripts/patch_no_stop_loop.py" . || die "no_stop_loop 补丁失败"
else
    echo "跳过 no_stop_loop（NO_STOP_LOOP=${NO_STOP_LOOP:-1}）"
fi

# ★★★★ resume 去掉 video mode 的 wait token（默认开）—— 死锁链里唯一「我们有、LOS 没有」的调用
#   我们 primary_display_resume 里有：
#       dpmgr_path_trigger(...);
#       _cmdq_insert_wait_frame_done_token();   <- 往 config 队列插 cmdqRecWaitNoClear(MUTEX0_STREAM_EOF)
#   原注释： "make sure first config after resume will config to HW when HW idle"
#   但 resume 时 dpmgr_path_start 已把 DSI_START 清 0 -> DSI 不输出 -> 没有帧 -> 事件 53 永不来
#   -> config 队列末尾那条等待永远不完成。
#   LOS 14.1 的 primary_display_resume（457 条逐指令反汇编）里
#   bl _cmdq_insert_wait_frame_done_token(0x49e7c4) 【0 处】，
#   video mode 分支只调 dpmgr_path_trigger 就直接 start_trigger_loop。本补丁照此对齐。
if [ "${RESUME_NO_WAIT_TOKEN:-1}" = "1" ] && [ -f "$ROOT/scripts/patch_resume_no_wait_token.py" ]; then
    echo "--- resume 去掉 wait token（对齐 LOS 14.1）---"
    "$PY" "$ROOT/scripts/patch_resume_no_wait_token.py" . || die "resume 去 wait token 补丁失败"
else
    echo "跳过 resume 去 wait token（RESUME_NO_WAIT_TOKEN=${RESUME_NO_WAIT_TOKEN:-1}）"
fi


# ★ 面板推表路径诊断（研究"resume 时命令有没有发到面板"，默认关闭）
#   在 push_table 每条命令、lcm_resume/lcm_suspend 入口插入无条件 pr_alert。
#   pr_alert 级别 0，必进 dmesg，不受 printk 级别过滤，无需 dynamic_debug。
#   只加日志，不改任何推表逻辑/时序/命令内容。研究完成后保持 LCM_TRACE=0。
if [ "${LCM_TRACE:-0}" = "1" ] && [ -f "$ROOT/scripts/patch_lcm_trace.py" ]; then
    echo "--- 加装面板推表诊断日志（pr_alert，命令级）---"
    "$PY" "$ROOT/scripts/patch_lcm_trace.py" . || die "面板推表诊断补丁失败"
else
    echo "跳过面板推表诊断日志（LCM_TRACE=${LCM_TRACE:-0}）"
fi


# ★ 实验：反转 lcm_resume 的 GPIO 上电顺序（默认关闭，LCM_POWER_SEQ=1 开启）
#   把「先 RST 复位 → 再 VSP/VSN 供电」改成「先 VSP/VSN 供电 → 再 RST 复位」。
#   验证假设：复位打在没上电的面板上 = 无效复位，导致"命令全发/寄存器健康但纯黑"。
#   必须在 LCM_STOCK 之后运行（后者会重写 lcm_resume 整段）。仅实验用，默认 0。
if [ "${LCM_POWER_SEQ:-0}" = "1" ] && [ -f "$ROOT/scripts/patch_lcm_power_seq.py" ]; then
    echo "--- 实验：反转面板 GPIO 上电顺序（先供电后复位）---"
    "$PY" "$ROOT/scripts/patch_lcm_power_seq.py" . || die "GPIO 时序实验补丁失败"
else
    echo "跳过 GPIO 时序实验（LCM_POWER_SEQ=${LCM_POWER_SEQ:-0}）"
fi


# ★ read_id 探针：把 mtk_lcm_s3pro_readback() 链接进来，直接问面板"收到命令没"。
#   底层 DCS 回读在 video 模式（MODE=3）下会短路 return 0，所以探针先临时
#   切 CMD 模式（MODE=0）再回读 0x0A/0xDA/0xDB，读完切回 video。
#   依赖：s3pro_dsi_set_mode() 由 DISP_DEBUG 补丁定义在 ddp_dsi.c，必须一起开。
#   只加诊断函数，不改任何显示逻辑/时序；默认关闭，LCM_READID=1 开启。
if [ "${LCM_READID:-1}" = "1" ] && [ -f "$ROOT/scripts/patch_lcm_readid.py" ]; then
    if [ "${DISP_DEBUG:-1}" != "1" ]; then
        die "read_id 探针依赖 DISP_DEBUG=1（提供 s3pro_dsi_set_mode），请一起开启"
    fi
    echo "--- 链接 read_id 面板回读探针（切CMD回读 0x0A/0xDA/0xDB）---"
    "$PY" "$ROOT/scripts/patch_lcm_readid.py" . || die "read_id 探针补丁失败"
else
    echo "跳过 read_id 探针（LCM_READID=${LCM_READID:-1}）"
fi


# ★【默认修复·CMDQ-safe】dimming(0x53/0x2C) 前移到 lcm_resume 内同步推表。
#   唤醒后 lcm_resume 末尾 schedule 的 500ms 延时工作在 video mode(MODE=3) 推表，
#   命令卡进 VM_CMD staging(0x2c531521, VM_CMD_START 悬挂/DONE 不到)
#   → 面板 0x53 未写入 → 背光亮但物理纯黑（实测根因，见报告 §12:20）。
#   lcm_resume 执行时 MODE 仍是 0(CMD)，切换 video 发生在其后的 dpmgr_path_start。
#   把 dimming 同步前移到此处走正常命令队列，不切 MODE、不碰 CMDQ。
#   必须在 LCM_STOCK 之后运行（后者重写 lcm_resume）。与 DIMMING_CMDMODE 互斥。
if [ "${DIMMING_EARLY:-0}" = "1" ] && [ -f "$ROOT/scripts/patch_dimming_early.py" ]; then
    if [ "${DIMMING_CMDMODE:-0}" = "1" ]; then
        die "DIMMING_EARLY 与 DIMMING_CMDMODE 互斥，且后者已证伪（CPU 裸写 MODE 触发 CMDQ 风暴）；请关闭 dimming_cmdmode"
    fi
    if [ "${LCM_STOCK:-1}" != "1" ]; then
        die "dimming early 修复需要 LCM_STOCK=1（lcm_resume 锚点由原厂重写脚本生成）"
    fi
    echo "--- 根因修复：dimming 前移到 lcm_resume 同步推表（CMDQ-safe）---"
    "$PY" "$ROOT/scripts/patch_dimming_early.py" . || die "dimming early 修复失败"
else
    echo "跳过 dimming early 修复（DIMMING_EARLY=${DIMMING_EARLY:-0}）"
fi


# ★【已证伪·保留供回退对比，默认关】dimming 推表切 CMD mode（CPU 裸写 MODE）。
#   旧方案能清掉 VM_CMD 卡死，但 s3pro_dsi_set_mode 内部 CPU 裸写 MODE 破坏
#   CMDQ 同步 → 实测 78+ 次 CMDQ SW timeout（WDMA0_SOF/MUTEX0_STREAM_EOF），
#   回退后风暴消失 → 因果坐实，不再作为修复使用。默认 0，勿与 DIMMING_EARLY 同开。
if [ "${DIMMING_CMDMODE:-0}" = "1" ] && [ -f "$ROOT/scripts/patch_dimming_cmdmode.py" ]; then
    if [ "${DISP_DEBUG:-1}" != "1" ]; then
        die "dimming CMD-mode（已证伪）依赖 DISP_DEBUG=1（提供 s3pro_dsi_set_mode），请一起开启"
    fi
    if [ "${DIMMING_EARLY:-0}" = "1" ]; then
        die "dimming CMD-mode（已证伪）与 DIMMING_EARLY 互斥，请先关闭 dimming_early"
    fi
    echo "--- [已证伪实验] dimming 推表切 CMD mode（CPU 裸写，仅供回退对比）---"
    "$PY" "$ROOT/scripts/patch_dimming_cmdmode.py" . || die "dimming CMD-mode 修复失败"
else
    echo "跳过 dimming CMD-mode（已证伪，DIMMING_CMDMODE=${DIMMING_CMDMODE:-0}）"
fi


# ★ 在线重推表实验台（实验用，默认关）。
#   复用现成 fb_dispparam 节点，拦截特殊 param 在线重推表，无需重新刷机：
#     echo 0x9999 > /sys/class/graphics/fb0/fb_dispparam  → 重推当前 init 表
#     echo 0x9998 > ... → 推睡眠表（对照）
#     echo 0x9997 > ... → 只推 0x11 SLEEPOUT + delay + 0x29 DISPLAY ON
#   必须在 LCM_STOCK 之后（需要 s3pro_init_table_a 等表数组）。
if [ "${LAB_PUSH:-0}" = "1" ] && [ -f "$ROOT/scripts/patch_lab_push.py" ]; then
    if [ "${LCM_STOCK:-1}" != "1" ]; then
        die "lab_push 需要 LCM_STOCK=1（表数组由 stock 补丁注入）"
    fi
    echo "--- 在线重推表实验台（fb_dispparam 0x9999/0x9998/0x9997）---"
    "$PY" "$ROOT/scripts/patch_lab_push.py" . || die "lab_push 补丁失败"
else
    echo "跳过在线重推表实验台（LAB_PUSH=${LAB_PUSH:-0}）"
fi


# ★ video_kick 诊断：黑屏态手动强制触发视频流，验证 CMDQ trigger loop 死锁。
#   新增 debugfs 节点 /sys/kernel/debug/s3pro_video_kick：
#     echo 1 > 节点   force trigger 一帧（CPU 直写，照抄 resume）
#     echo 2 > 节点   stop loop → force trigger → start loop（照抄 ESD recovery）
#     echo 3 > 节点   只打印状态（含 frmbc）
#   依赖：s3pro_dbg_init_once()（节点注册入口）由 DISP_DEBUG 补丁提供，必须一起开。
#   只加诊断节点，不改任何显示逻辑；默认关闭，VIDEO_KICK=1 开启。
if [ "${VIDEO_KICK:-0}" = "1" ] && [ -f "$ROOT/scripts/patch_video_kick.py" ]; then
    if [ "${DISP_DEBUG:-1}" != "1" ]; then
        die "video_kick 诊断依赖 DISP_DEBUG=1（提供节点注册入口），请一起开启"
    fi
    echo "--- 加 video_kick 诊断节点（手动强制触发视频流）---"
    "$PY" "$ROOT/scripts/patch_video_kick.py" . || die "video_kick 诊断补丁失败"
else
    echo "跳过 video_kick 诊断（VIDEO_KICK=${VIDEO_KICK:-0}）"
fi


# ★ vmcmd 诊断：清 VM_CMD_START(bit16) 悬挂，验证黑屏根因。
#   新增 debugfs 节点 /sys/kernel/debug/s3pro_vmcmd（在 ddp_dsi.c，独立注册）：
#     echo snap    > 节点  打印 START 各位 / INTSTA(VM_CMD_DONE) / TRIG / VMCTL_STATE
#     echo clr     > 节点  只清 VM_CMD_START(bit16)，保留 DSI_START(bit0)
#     echo restart > 节点  清 bit16 + 重新脉冲 bit0（照抄 DSI_Start 内部）
#   黑屏态实测 start=0x00010001(bit16卡1) + VM_CMD_DONE=0，VIDEO_KICK 两档无效，
#   源码确认无代码单独清 bit16 —— 本节点验证清掉它能否恢复 video 帧。
#   不依赖 DISP_DEBUG（自己在 ddp_dsi.c 注册）；只加节点，不改显示逻辑；默认关闭。
if [ "${VMCMD:-0}" = "1" ] && [ -f "$ROOT/scripts/patch_vmcmd.py" ]; then
    echo "--- 加 vmcmd 诊断节点（清 VM_CMD_START bit16）---"
    "$PY" "$ROOT/scripts/patch_vmcmd.py" . || die "vmcmd 诊断补丁失败"
else
    echo "跳过 vmcmd 诊断（VMCMD=${VMCMD:-0}）"
fi


# ★ 触摸修复：GT911 分辨率 800x1280 → 1536x2048 + 校准矩阵改恒等变换。
#   实机证据（getevent）：
#     ABS_MT_POSITION_X = 739    正常
#     ABS_MT_POSITION_Y = -466   负数 → Android 直接丢弃 → "触摸完全没反应"
#   根因：
#     1) GT911 配置里 X_MAX=800, Y_MAX=1280（小米平板3 的屏，不是这台）
#     2) 校准矩阵 {0,2559,0, -6557,0,5238784} = 800x1280 屏的 90° 旋转，
#        x 超过 799 后 y' 变负
if [ "${TOUCH_FIX:-1}" = "1" ] && [ -f "$ROOT/scripts/patch_gt911_touch.py" ]; then
    echo "--- 修正 GT911 触摸参数（分辨率 + 校准矩阵）---"
    "$PY" "$ROOT/scripts/patch_gt911_touch.py" . || die "触摸参数修正失败"
else
    echo "跳过触摸修正（TOUCH_FIX=${TOUCH_FIX:-1}）"
fi


# ★ 实体电容触摸按键（默认开启 GTP_KEYS=1）
#
#   依据（都是从原厂/本机数据里取出来的，不是猜的）：
#    1) 本机 DTB（authorparts/kernel.dtb）已声明按键：
#         tpd-key-num       = 4
#         tpd-key-local     = 139(MENU) 172(HOMEPAGE) 158(BACK) 0
#         tpd-key-dim-local = (60,850,50,30) (180,850,50,30) (300,850,50,30) (420,850,50,30)
#    2) 原厂 7.0 内核里有 4 套 GT911 配置数组（@0xaccbf0 等），
#       其 +0x07 字节（GTP_REG_HAVE_KEY=0x804E）为 0x01 = 有按键；
#       本树的 sample 配置是 0x06/0x00 → IC 根本不上报按键。
#    3) 驱动里的 TPD_KEY_COUNT / TPD_KEYS / GTP_KEY_MAP_ARRAY 在本树完全没定义，
#       打开 CONFIG_GTP_HAVE_TOUCH_KEY 会直接编译失败。
#
#   本步：把 defconfig 里该开关打开 + 用原厂配置数组替换 + 补齐三个宏。
if [ "${GTP_KEYS:-1}" = "1" ] && [ -f "$ROOT/scripts/patch_gt911_keys.py" ]; then
    echo "--- 启用 GT911 实体触摸按键（原厂配置 + DTB 键值）---"
    # 注意：构建实际使用的是从原厂 boot.img 提取的 .config，
    # 树里的 defconfig 根本不会被用（build.sh 第 4 步已确定 .config）。
    # 只改 defconfig 是无效的 —— 上一版就是这么白跑的。
    for df in arch/arm64/configs/h3000s_defconfig arch/arm64/configs/defconfig .config; do
        [ -f "$df" ] || continue
        sed -i 's/^# CONFIG_GTP_HAVE_TOUCH_KEY is not set/CONFIG_GTP_HAVE_TOUCH_KEY=y/' "$df"
        grep -q '^CONFIG_GTP_HAVE_TOUCH_KEY=y' "$df" || echo 'CONFIG_GTP_HAVE_TOUCH_KEY=y' >> "$df"
        echo "  $df → $(grep '^CONFIG_GTP_HAVE_TOUCH_KEY' "$df")"
    done
    "$PY" "$ROOT/scripts/patch_gt911_keys.py" . || die "触摸按键补丁失败"
else
    echo "跳过触摸按键（GTP_KEYS=${GTP_KEYS:-1}）"
fi


# ★ 面板驱动诊断：**只做 DCS 回读，不改任何时序**。
#   build-50 已确认：休眠命令提前到关 DSI 之前、DSI_START=1、GPIO 全高、
#   帧在流动，但屏幕仍然全黑；而 skip_stop=1（完全不拆）时屏幕正常。
#   → 面板硬件、DSI 通路、视频流都没问题，病灶在"断电后重建"这条链。
#   本步只加测量，不做任何猜测性改动：
#     推表后回读 0x0A（power mode，正常 0x9C）、0xDA/0xDB（面板 ID）
#     读回有效 → 面板活着、上电复位正常 → 病灶在视频流参数（去 LK 对比 DSI 配置）
#     读回全 0 → 面板没被正确上电/复位 → 再研究电源/复位时序
#   注意：实测该面板的 DCS 读取通路不工作（面板健康时读回也是全 0），
#   所以这个回读没有诊断价值，默认关闭 LCM_DEBUG=0。
#   而且它把 mtk_lcm_s3pro_readback() 插在 lcm_suspend 之前，
#   与原厂重写（LCM_STOCK）的区间重叠，同时开启会冲突。
if [ "${LCM_DEBUG:-0}" = "1" ] && [ -f "$ROOT/scripts/patch_lcm_debug.py" ]; then
    echo "--- 面板驱动诊断（DCS 回读，纯测量）---"
    "$PY" "$ROOT/scripts/patch_lcm_debug.py" . || die "面板 DCS 回读补丁失败"
else
    echo "跳过面板驱动诊断（LCM_DEBUG=${LCM_DEBUG:-0}）"
fi


# ★ 电量显示 0%：本树 drivers/power/mt81xx/Makefile 漏了 mt6397_fgadc.o，
#   导致 bm_ctrl_cmd() 没有真实实现，只能用 build.sh 注入的弱占位（永远返回 0）。
#   而 battery_meter_get_battery_voltage() 里 `int val = 5;` 只是采样次数占位，
#   调用没写回时就直接返回 5 → VBAT=5 → "VBAT < 3400" → UI_SOC 强制 0。
#   实机证据：voltage_now=5000µV、capacity=0、temp=0，
#             kallsyms 里 bm_ctrl_cmd 是 W（weak）。
#   作者内核也是 0%，因为他同样绕过了这个缺失的 Makefile 条目。
if [ -f "$ROOT/scripts/patch_battery_fgadc.py" ]; then
    echo "--- 修电量显示（把真正的 MT6397 FGADC 驱动加入编译）---"
    "$PY" "$ROOT/scripts/patch_battery_fgadc.py" . || die "FGADC 补丁失败"
else
    warn "scripts/patch_battery_fgadc.py 不存在 —— 电量会一直是 0%"
fi

# ★ 电量恒为 0%（第二层）：FGADC 驱动所有 bm 命令都 `return 0`（状态码），
#   真正的值写进 data 缓冲区；而 mt_battery_meter.c 却把返回值当电量：
#       gFG_capacity_by_c = battery_meter_ctrl(...GET_HW_FG_CAR, &gFG_columb);
#   → 电量恒等于 0。实机证据：voltage/current/ZCV 都是真值，只有 SOC=0。
#   源码里本来就留着一行正确写法的注释：
#       /*gFG_capacity = fgauge_read_capacity(0);*/
#   这个补丁把它恢复成正式代码（三处：initialization / algo_run / algo_run_init）。
if [ -f "$ROOT/scripts/patch_battery_soc.py" ]; then
    echo "--- 修电量显示（把库仑计读数换算成真正的百分比）---"
    "$PY" "$ROOT/scripts/patch_battery_soc.py" . || die "电量 SOC 补丁失败"
else
    warn "scripts/patch_battery_soc.py 不存在 —— 电量仍会恒为 0%"
fi

# ★ 电池容量（Qmax）配错：本机 7200mAh，内核按 4000mAh 累积库仑计。
#   设备 DTB 里 battery = "ariel_battery"，dmesg 确认走的是
#       ariel8_custom_battery_init()
#   而该函数里作者留了 todo，Qmax 全是 4000（MTK 通用占位值）：
#       /* todo: should update ariel8 battery capacity here */
#       p_meter_data->q_max_pos_50 = 4000;
#   用 4000 当分母算 DOD，放掉 4000mAh 就认为放完了，实际还剩 3200mAh，
#   于是出现"显示 66% 一夜关机""20 分钟只涨 1%""拔电瞬间断电"等现象。
#   本机 nvram 里没有电池参数（只有 BT_Addr/WIFI），运行期补不回来，只能改源码。
if [ -f "$ROOT/scripts/patch_battery_qmax.py" ]; then
    echo "--- 修电池容量（Qmax 4000 → 7200 mAh）---"
    "$PY" "$ROOT/scripts/patch_battery_qmax.py" . || die "Qmax 补丁失败"
else
    warn "scripts/patch_battery_qmax.py 不存在 —— 电量估算仍会偏差 1.8 倍"
fi

# ★ 传感器驱动没打开（自动亮度 / 自动旋转用不了）
#   硬件确认方式：自写的静态 ARM 探测工具直接读 I2C 寄存器
#     加速度计 /dev/i2c-6 @0x19, WHO_AM_I(0x0F) = 0x33  → LIS3DH
#     光线距离 /dev/i2c-8 @0x1C, 0x80=0xEE 0x92=0xEE     → CM36686
#   两处缺失性质不同：
#     CM36686  → accelerometer/alsps 的 Makefile 里有引用，只是配置没开
#     LIS3DH   → accelerometer/Makefile 里【完全没有】lis3dh/ 这一行，
#                必须补一行，光开配置不生效
if [ -f "$ROOT/scripts/patch_sensors.py" ]; then
    echo "--- 打开传感器驱动（LIS3DH 加速度计 + CM36686 光感）---"
    "$PY" "$ROOT/scripts/patch_sensors.py" . || warn "传感器补丁失败（不影响内核其它部分）"
else
    warn "scripts/patch_sensors.py 不存在 —— 自动亮度/自动旋转用不了"
fi

# ★ cm36686 编译失败的兜底：它用了 I2C_MASK_FLAG / I2C_WR_FLAG / I2C_RS_FLAG，
#   这三个宏在 include/linux/i2c.h 里被 #ifdef CONFIG_MTK_I2C_EXTENSION 包着，
#   而 MT8173 不在该选项的 def_bool 平台列表里 → 宏不存在 → 编译报
#       error: 'I2C_MASK_FLAG' undeclared
#   不去打开 CONFIG_MTK_I2C_EXTENSION（那会改变整条 I2C 驱动的消息处理路径，
#   触摸屏等设备共用同一条总线，风险太大），改为在 cm36686.c 里补同值兜底定义。
if [ -f "$ROOT/scripts/patch_cm36686_i2c.py" ]; then
    echo "--- 修 cm36686 的 I2C 宏缺失（补兜底定义）---"
    "$PY" "$ROOT/scripts/patch_cm36686_i2c.py" . || die "cm36686 I2C 宏补丁失败"
else
    warn "scripts/patch_cm36686_i2c.py 不存在 —— cm36686 会编译失败"
fi

# ★ cm36686 运行期失败：光感注册不上。
#   实机 dmesg（build 68）：
#       [Sensor dts] Device Tree: can not find alsps node!. Go to use old cust info
#       <ALS/PS> cm36686_init 1903 : get_alsps_dts_func fail
#       <ALS/PS> alsps_real_driver_init fail
#   原因：cm36686.c 找的是 compatible="mediatek,CM36686"，
#   而本机 DTB 里 alsps 节点的 compatible 是 "mediatek,alsps"，必然找不到。
#   且它失败后直接 return 0（一次都不尝试），而 alsps_cust 又只是个
#   未初始化的全局变量（全零，i2c_num=0）。
#   对照：同批的 LIS3DH 加速度计成功了 —— 因为它的 init 有 builtin 兜底。
#   修法：照 LIS3DH 的模式，给 alsps_cust 填真实参数（i2c-8 @ 0x1C），
#         并让 DTS 查找失败时兜底到它。
if [ -f "$ROOT/scripts/patch_cm36686_dts.py" ]; then
    echo "--- 让 cm36686 在 DTB 无匹配节点时兜底到静态配置 ---"
    "$PY" "$ROOT/scripts/patch_cm36686_dts.py" . || die "cm36686 DTS 兜底补丁失败"
else
    warn "scripts/patch_cm36686_dts.py 不存在 —— 光感仍注册不上"
fi

# ★ EINT 补丁 —— 光感 probe 失败的真正原因（2026-11-17 实机定位）
#   现象：刷了带 cm36686 的内核后，光感依然没有，dmesg：
#       <ALS/PS> cm36686_setup_eint 1065 : Cannot find alsps pinctrl default!
#       <ALS/PS> cm36686_setup_eint 1099 : null irq node!!
#       cm36686: probe of 8-001c failed with error -22
#   根因（两处名字写错 + 一处危险写法）：
#     1) 驱动找 EINT 节点用的是 "mediatek, ALS-eint"（逗号后多一个空格，
#        名字本身也不对），而本机 DTB 里是 /alspseint@0
#        compatible = "mediatek,alsps_int"，interrupts = <0x61 0x08>。
#        名字对不上 → irq_node=NULL → setup_eint 走 else → return -EINVAL。
#     2) alspseint@0 没有 "debounce" 属性，原代码会把 ints[0]=0 传给
#        gpio_request(0)，等于乱抢 GPIO 0。
#     3) EINT 失败被当成致命错误。但核心层 alsps.c 的 ALS 数据是定时器
#        轮询出来的（mod_timer(&cxt->timer_als, ...)），不依赖 EINT。
#   修法：见 scripts/patch_alsps_eint.py（A/B/C 三段）。
if [ -f "$ROOT/scripts/patch_alsps_eint.py" ]; then
    echo "--- 修 cm36686 的 EINT 节点名（光感 probe 失败根因）---"
    "$PY" "$ROOT/scripts/patch_alsps_eint.py" . || die "alsps EINT 补丁失败"
else
    warn "scripts/patch_alsps_eint.py 不存在 —— 光感仍会 probe 失败(-22)"
fi

# ★ 把驱动改成说 AP3220 的话 —— 光感真正能出数的关键
#   本机光感是 AP3220（原厂驱动内部名 ap3xx6），不是 CM36686：
#     · 原厂 7.0 内核字符串：ap3xx6 ×54、AP3220 ×11、cm36686 零次
#     · 实机裸 syscall 探针：i2c-8@0x1C 有 ACK；CM36686 的 0x60 全总线无 ACK
#   原厂驱动寄存器表（反汇编 ap3xx6 得出）：
#     reg0x00  bit0 = ALS 使能（orr #0x1 @0x8381234）
#              bit1 = PS  使能（orr #0x2 @0x8381350）
#     reg0x0C/0x0D = ALS 数据(小端)，reg0x0E/0x0F = PS 数据(小端)
#     I2C：写 = send({reg,val},2)；读 = send(reg,1)+recv(val,1)，无 MTK 扩展标志
#   实机已验证：置 bit0 后 ALS 由恒 0 变为持续变化，手电筒直照达 28452(0x6F24)。
if [ -f "$ROOT/scripts/patch_alsps_ap3220.py" ]; then
    echo "--- 把 cm36686 驱动重映射为 AP3220（寄存器/使能位/I2C 读法）---"
    "$PY" "$ROOT/scripts/patch_alsps_ap3220.py" . || die "AP3220 重映射补丁失败"
else
    warn "scripts/patch_alsps_ap3220.py 不存在 —— 光感仍按 CM36686 解析，读不到数据"
fi

# ★ 光感上报方式：EV_REL → EV_ABS（让 HAL 真正收得到 lux）
#   2026-11-20 实机证据（只读取证，非推测）：
#     · m_alsps_input 的能力位是 REL=107 / ABS=101 —— bit0(REL_X)/bit8 都在 REL 里，
#       这正是 alsps.c（EV_REL 版）的特征；若编译的是 alsps_module.c（EV_ABS 版），
#       REL 只会是 0x6（只留 PS 的 bit1/bit2）。
#     · 头文件里 EVENT_TYPE_ALS_STATUS 本来就写成 ABS_WHEEL，却用 input_report_rel
#       发出去 —— 名字是 ABS 轴、实际按 REL 发，语义自己就矛盾。
#     · 框架侧：mRecentLightSamples 一直涨（事件在流），但
#       mAmbientLux / mLastObservedLux 恒为 0.0，sensorservice 的 LIGHT 事件全是
#       0.00 —— 典型的「按 REL 发、按 ABS 收」，HAL 永远匹配不到事件。
#   修法：scripts/patch_alsps_abs.py（A/B/C 三段，写法对齐树里现成的
#   alsps_module.c：报告用 input_report_abs、能力位用 EV_ABS）。
if [ -f "$ROOT/scripts/patch_alsps_abs.py" ]; then
    echo "--- 把 ALSPS 光感上报从 EV_REL 改成 EV_ABS（对齐 HAL 期望）---"
    "$PY" "$ROOT/scripts/patch_alsps_abs.py" . || die "ALSPS ABS 补丁失败"
else
    warn "scripts/patch_alsps_abs.py 不存在 —— 光感仍按 EV_REL 上报，HAL 收不到 lux"
fi

# ★ VPU 时钟补丁 —— 默认【关闭】
#   假设：vdec_power_on 里所有 clk_prepare/clk_enable 被注释，VPU 无时钟。
#   疑点：clk_summary 里 enable_count=0 是在【空闲时】测的，空闲时时钟本来就该关，
#         所以这个假设【未被证实】，不能当已知根因而默认打开。
#   真正已证实的失败链是用户态日志里的：
#         Can not find MVA from VA → Cannot init driver
#         → InitVideoDecodeHW failed → OMX error 0x80001005
#         → ACodec 放弃 → VLC 回退软解 → PPT 卡顿
#   要 A/B 验证时钟假设时，用 VCODEC_CLK_FIX=1 显式打开。
if [ "${VCODEC_CLK_FIX:-0}" = "1" ]; then
    if [ -f "$ROOT/scripts/patch_vcodec_clk.py" ]; then
        echo "--- [实验] 修 VPU 硬解时钟（vdec_power_on 补 vcodecpll_ck/vdec_sel）---"
        "$PY" "$ROOT/scripts/patch_vcodec_clk.py" . || die "vcodec 时钟补丁失败"
    else
        warn "scripts/patch_vcodec_clk.py 不存在"
    fi
else
    echo "--- VPU 时钟补丁：未启用（VCODEC_CLK_FIX=1 可开启实验）---"
fi

# ★ VDEC/VENC 中断号重映射 —— 默认【开启】（有实机证据）
#   证据：属性 ro.mtk_sec_video_path_support=0 之后，解码器初始化通过了，
#         但紧接着报 MtkOmxVdecDecodeThread() line: 1400 WAIT timeout，
#         且 /proc/interrupts 里始终没有 VDEC(SPI 204) 那一行。
#   原因：vcodec_driver_init()（module_init）里 irq_of_parse_and_map 时
#         MTK 层级中断控制器 mtk-sysirq 还没就绪，返回 0；
#         之后 request_irq(0) 必失败，VPU 永远等不到完成中断。
#   修法：在 vcodec_probe()（中断控制器已就绪）里补一次映射。
if [ -f "$ROOT/scripts/patch_vcodec_irq.py" ]; then
    echo "--- 修 VDEC/VENC 中断映射（probe 阶段重映射）---"
    "$PY" "$ROOT/scripts/patch_vcodec_irq.py" . || die "vcodec IRQ 补丁失败"
else
    warn "scripts/patch_vcodec_irq.py 不存在 —— 硬解仍会 WAIT timeout 卡死"
fi

# ---- 蓝牙 bluez 补丁：已关闭（默认不执行）----
# 背景：曾把 stp_chrdev_bt.c 的 BT_open() 里 set_bluez(0) 改成 1，想让 STP 输出
#       标准 H4 帧给 AOSP 的 H4Protocol 读。实测 bluez=0/1 两种模式都失败，最终确认
#       真正卡点在内核 WMT 层 mtk_wcn_wmt_func_off(BT) 返回 FALSE（触发 HW RST 死循环），
#       与 STP 帧格式无关。详见 docs/蓝牙调查全记录.md。
# 恢复方式：设环境变量 S3PRO_BT_BLUEZ=1 重新开启。
if [ "${S3PRO_BT_BLUEZ:-0}" = "1" ]; then
    if [ -f "$ROOT/scripts/patch_bt_bluez.py" ]; then
        echo "--- 蓝牙 STP 切 BlueZ/H4 模式 ---"
        "$PY" "$ROOT/scripts/patch_bt_bluez.py" . || die "BT BlueZ 补丁失败"
    else
        warn "scripts/patch_bt_bluez.py 不存在 —— 蓝牙仍是 MTK 私有帧模式"
    fi
else
    echo "--- 蓝牙 bluez 补丁：已跳过（保持 MTK 私有帧模式 set_bluez(0)）---"
fi

# ★ 补丁生效性验证（防"脚本跑了但没改到"）
#   前几轮出现过：build.sh 里有调用、补丁脚本也正确，但内核里没有兜底字符串
#   （build 68/69 都是这样），导致光感始终注册不上却看不出原因。
#   这里直接检查源码里是否真的出现了兜底代码，没有就立即失败。
if [ -f "drivers/misc/mediatek/alsps/cm36686-new/cm36686.c" ]; then
    if grep -q "fall back to alsps_cust" drivers/misc/mediatek/alsps/cm36686-new/cm36686.c; then
        echo "  [verify] cm36686 DTS 兜底代码已写入源码 ✓"
    else
        die "cm36686 DTS 兜底补丁未生效（源码里找不到 fall back to alsps_cust）"
    fi
    if grep -q "struct alsps_hw alsps_cust = {" drivers/misc/mediatek/alsps/cm36686-new/cm36686.c; then
        echo "  [verify] alsps_cust 静态配置已初始化 ✓"
    else
        die "alsps_cust 静态配置未初始化（补丁未生效）"
    fi
    # EINT 补丁三段，任何一段没写进去都会让光感继续 probe 失败(-22)
    if grep -q "mediatek,alsps_int" drivers/misc/mediatek/alsps/cm36686-new/cm36686.c; then
        echo "  [verify] EINT 节点名已改为 mediatek,alsps_int ✓"
    else
        die "EINT 节点名补丁未生效（源码里找不到 mediatek,alsps_int）"
    fi
    if grep -q "no debounce prop" drivers/misc/mediatek/alsps/cm36686-new/cm36686.c; then
        echo "  [verify] debounce 保护已写入（不会乱抓 GPIO 0）✓"
    else
        die "debounce 保护补丁未生效"
    fi
    if grep -q "continue in polling mode" drivers/misc/mediatek/alsps/cm36686-new/cm36686.c; then
        echo "  [verify] EINT 失败已改为非致命 ✓"
    else
        die "EINT 非致命补丁未生效（probe 仍会因 -22 失败）"
    fi
    # 标定表项数必须与结构体一致：als_level 15 项、als_value 16 项
    if grep -q "0x2A8" drivers/misc/mediatek/alsps/cm36686-new/cm36686.c; then
        echo "  [verify] 标定表/PS 阈值已用 DTB 真实值 ✓"
    else
        warn "alsps_cust 标定表似乎还是旧值（缺 0x2A8）"
    fi
    # ---- AP3220 重映射：任何一段没进源码，光感都读不到数 ----
    if grep -q "AP3220-REMAP" drivers/misc/mediatek/alsps/cm36686-new/cm36686.c; then
        echo "  [verify] AP3220 重映射已写入源码 ✓"
    else
        die "AP3220 重映射补丁未生效（光感寄存器仍按 CM36686 解析）"
    fi
    if grep -q "i2c_master_recv" drivers/misc/mediatek/alsps/cm36686-new/cm36686.c; then
        echo "  [verify] I2C 读已改为 send+recv（不再用 MTK 扩展位）✓"
    else
        die "I2C 读路径补丁未生效（读操作会因 addr=0x181c 全部 NAK）"
    fi
    if grep -q "v |= 0x01;" drivers/misc/mediatek/alsps/cm36686-new/cm36686.c; then
        echo "  [verify] ALS 使能位已改为 reg0x00 bit0 置 1 ✓"
    else
        die "ALS 使能位补丁未生效（CM36686 语义是清 bit0，对 AP3220 正好相反）"
    fi
    if grep -q "CM36686_REG_ALS_DATA		0X0C" drivers/misc/mediatek/alsps/cm36686-new/cm36686.h 2>/dev/null || \
       grep -qE "CM36686_REG_ALS_DATA[[:space:]]+0X0C" drivers/misc/mediatek/alsps/cm36686-new/cm36686.h; then
        echo "  [verify] ALS 数据寄存器已改为 0x0C ✓"
    else
        die "头文件 ALS_DATA 未改为 0x0C（会读到 CM36686 的寄存器）"
    fi
    # ---- 光感上报方式必须是 EV_ABS，否则 HAL 收不到 lux ----
    if grep -q "input_report_abs(dev, EVENT_TYPE_ALS_VALUE, value)" drivers/misc/mediatek/alsps/alsps.c; then
        echo "  [verify] ALS 上报已改为 input_report_abs（EV_ABS）✓"
    else
        die "ALSPS ABS 补丁未生效（alsps.c 仍是 input_report_rel）"
    fi
    if grep -q "input_set_capability(dev, EV_ABS, EVENT_TYPE_ALS_VALUE)" drivers/misc/mediatek/alsps/alsps.c; then
        echo "  [verify] ALS 能力位已改为 EV_ABS ✓"
    else
        die "ALS 能力位补丁未生效（alsps.c 仍是 EV_REL）"
    fi
fi

# 同样的方式验证 Qmax 补丁
if [ -f "drivers/power/mt81xx/mt_bm_data.c" ]; then
    if grep -q "q_max_pos_50 = 7200" drivers/power/mt81xx/mt_bm_data.c; then
        echo "  [verify] Qmax 已改为 7200mAh ✓"
    else
        warn "Qmax 补丁未生效（源码里没有 q_max_pos_50 = 7200）"
    fi
fi

# 验证 vcodec 时钟补丁（仅在显式开启时检查）
if [ "${VCODEC_CLK_FIX:-0}" = "1" ]; then
    if [ -f "drivers/misc/mediatek/videocodec/mt8173/videocodec_kernel_driver.c" ]; then
        if grep -q "s3pro_vdec_pll" drivers/misc/mediatek/videocodec/mt8173/videocodec_kernel_driver.c; then
            echo "  [verify] VPU 时钟补丁已写入 vdec_power_on ✓"
        else
            die "VPU 时钟补丁未生效（源码里找不到 s3pro_vdec_pll）"
        fi
    fi
fi

# 验证 vcodec IRQ 重映射补丁（默认必查）
if [ -f "drivers/misc/mediatek/videocodec/mt8173/videocodec_kernel_driver.c" ]; then
    if grep -q "S3PRO-DIAG" drivers/misc/mediatek/videocodec/mt8173/videocodec_kernel_driver.c; then
        echo "  [verify] VDEC/VENC 中断重映射补丁已写入 probe ✓"
    else
        die "VDEC IRQ 重映射补丁未生效（源码里找不到 S3PRO-DIAG）"
    fi
fi

# 验证传感器驱动配置真的被打开
for k in CONFIG_MTK_LIS3DH CONFIG_MTK_CM36686_NEW CONFIG_CUSTOM_KERNEL_ALSPS; do
    if grep -q "^$k=y" .config; then
        echo "  [verify] $k=y ✓"
    else
        warn "$k 未打开（自动亮度/自动旋转可能失效）"
    fi
done
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
/* S3PRO: 签名必须与 mt_battery_meter_hal.h 里的声明一致；
 * 真正的实现在 drivers/power/mt81xx/mt6397_fgadc.c（由 patch_battery_fgadc.py 加入编译），
 * 强符号会覆盖这里的弱占位。签名写错会让电量一直读到占位值 5 → 0%。 */
__attribute__((weak)) int bm_ctrl_cmd(int cmd, void *data) { (void)cmd; (void)data; return 0; }
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
# ★ 验证关键补丁的标记是否真的进入了内核镜像（防「编译通过但补丁没生效」）
#
#   动机（2026-11-24 实测）：patch_vdo_eof_event.py 打了、CI 绿了、内核也编出来了，
#   但运行时 dpmgr_path_is_idle() 把点火挡住，trig 仍是 0 —— 编译期完全看不出。
#   为了至少有「补丁有没有被编进去」这一层保证，这里用 pr_alert 里的
#   "[S3PRO_XXX]" 字符串做静态验证：
#       标记不在镜像里  = 补丁根本没生效 -> 直接 die（不让坏包流出去）
#       标记在镜像里    = 补丁代码确实编进去了（运行时是否起作用仍需 dmesg 判据）
#
#   只检查【当前开启的】补丁，关掉的补丁当然不会有标记。
CHECK_MARKS=""
[ "${LCM_INIT_TABLE:-1}" = "1" ] && CHECK_MARKS="$CHECK_MARKS S3PRO_LCM_INIT"
[ "${STOCK_TIMING:-1}" = "1" ] && CHECK_MARKS="$CHECK_MARKS S3PRO_TMG"
[ "${LK_REPLAY:-1}" = "1" ] && CHECK_MARKS="$CHECK_MARKS S3PRO_LKREPLAY"
[ "${NO_STOP_LOOP:-1}" = "1" ] && CHECK_MARKS="$CHECK_MARKS S3PRO_NO_STOP_LOOP"
[ "${VDO_EOF_EVENT:-0}" = "1" ] && CHECK_MARKS="$CHECK_MARKS S3PRO_VDO_EOF"
[ "${DSI_CFG_START:-0}" = "1" ] && CHECK_MARKS="$CHECK_MARKS S3PRO_DSI_CFG_START"
[ "${RESUME_NO_WAIT_TOKEN:-1}" = "1" ] && CHECK_MARKS="$CHECK_MARKS S3PRO_NO_WAIT_TOKEN"

if [ -n "$CHECK_MARKS" ]; then
    echo "--- 验证补丁标记是否进入内核镜像 ---"
    # 镜像可能是 Image / Image.gz / Image.gz-dtb，按 gzip 魔数(1f8b)判断是否需要解压；
    # gz-dtb 尾部追加了 DTB，gzip -dc 会解出内核部分后报错，|| true 吞掉即可。
    UNPACKED="$WORK/kernel_unpacked"
    _magic="$(od -An -tx1 -N2 "$IMG" 2>/dev/null | tr -d ' \n')"
    if [ "$_magic" = "1f8b" ]; then
        gzip -dc "$IMG" > "$UNPACKED" 2>/dev/null || true
    else
        cp "$IMG" "$UNPACKED"
    fi
    MISS_MARK=""
    for m in $CHECK_MARKS; do
        if grep -qa -- "$m" "$UNPACKED" 2>/dev/null; then
            echo "  [OK]   $m"
        else
            echo "  [MISS] $m"
            MISS_MARK="$MISS_MARK $m"
        fi
    done
    # 只保留解压副本到打包结束，之后清掉避免产物变大
    if [ -n "$MISS_MARK" ]; then
        die "以下补丁标记未进入内核镜像（补丁没生效）：$MISS_MARK"
    fi
fi

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

    # 相机兼容垫片（libui_shim.so）不在这里编 —— 它是纯用户态的库，与内核无关。
    # 单独用 .github/workflows/build-camera-shim.yml 生成。

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

    # ★ 硬解必需：关掉"安全视频路径"。
    #   现象（实机）：VLC 播 4K 先花屏1秒→流畅约3秒→卡成PPT；
    #       logcat: Cannot init driver ×30 → InitVideoDecodeHW failed (not compatible
    #       → ACodec ERROR(0x80001005) → VLC 退回软解。
    #   原因：/system/build.prop 里 ro.mtk_sec_video_path_support=1，
    #        OMX 库据此创建"安全实例"（bSecureInst=TRUE），内核 vcodec 驱动
    #        跳过 request_irq 把活干给 MTEE/TrustZone；而本内核虽开了
    #        CONFIG_MTK_IN_HOUSE_TEE_SUPPORT，TEE 侧的 VPU 初始化却失败。
    #   修法：把该属性置 0，强制走普通（非安全）解码路径。
    #   放在 ramdisk 的 default.prop 里是因为 ro.* 属性"先设先赢"，
    #   它能盖过 /system/build.prop，这样只刷 boot.img 也能生效。
    if [ -f "$ROOT/scripts/patch_ramdisk.py" ]; then
        echo "--- 关闭安全视频路径（ro.mtk_sec_video_path_support=0）---"
        "$PY" "$ROOT/scripts/patch_ramdisk.py" "$OUT/boot.img" \
            --prop ro.mtk_sec_video_path_support=0 \
            || warn "关闭安全视频路径失败，硬解仍会因 TEE 初始化失败"
        if "$PY" "$ROOT/scripts/patch_ramdisk.py" "$OUT/boot.img" --check 2>/dev/null \
                | grep -qi "sec_video_path"; then
            echo "  [verify] ro.mtk_sec_video_path_support 已写入 ramdisk ✓"
        else
            warn "ramdisk 里没读到 sec_video_path 属性，硬解可能仍失败"
        fi
    else
        warn "scripts/patch_ramdisk.py 不存在：安全视频路径不会被关闭，硬解仍不可用"
    fi


    # 自检：内核里必须带 binder,hwbinder,vndbinder，否则 /dev/hwbinder 不会创建，
    # Android 8 的 HAL 全部起不来（作者的内核里就有这个字符串）。
    # 注意两点：
    #  1) 补丁加的是三个独立设备名（"binder"/"hwbinder"/"vndbinder"），
    #     不是 Kconfig 那种逗号连写的字符串；
    #  2) 必须查**未压缩**的内核（arch/arm64/boot/Image 或 vmlinux），
    #     在 Image.gz-dtb 里 grep 文本是查不到的（压缩过了）。
    CHK=""
    if [ -f arch/arm64/boot/Image ]; then
        CHK="arch/arm64/boot/Image"
    elif [ -f vmlinux ]; then
        CHK="vmlinux"
    fi
    HB=0
    VB=0
    if [ -n "$CHK" ]; then
        grep -qa 'hwbinder' "$CHK" && HB=1
        grep -qa 'vndbinder' "$CHK" && VB=1
        echo "自检（查 $CHK）: hwbinder=$HB vndbinder=$VB"
    else
        gzip -dc "$IMG" 2>/dev/null | grep -qa 'hwbinder' && HB=1
        gzip -dc "$IMG" 2>/dev/null | grep -qa 'vndbinder' && VB=1
        echo "自检（解压后查）: hwbinder=$HB vndbinder=$VB"
    fi
    if [ "$HB" = "1" ] && [ "$VB" = "1" ]; then
        echo "自检通过：hwbinder / vndbinder 设备名都在内核里"
    else
        die "内核里缺 hwbinder 或 vndbinder —— /dev/hwbinder 不会出现，HAL 起不来"
    fi
    # ★ 现代 binder 是否真的编进去了（两项检查，各自独立收尾）
    #   标记 1：CONFIG_ANDROID_BINDER_DEVICES 的默认值，编译后是字符串字面量
    MC=0
    if [ -f "$CHK" ]; then
        grep -qa 'binder,hwbinder,vndbinder' "$CHK" && MC=1
    else
        gzip -dc "$IMG" 2>/dev/null | grep -qa 'binder,hwbinder,vndbinder' && MC=1
    fi
    echo "自检（binder 设备名）: devices=$MC"
    if [ "$MC" = "1" ]; then
        echo "自检通过：binder,hwbinder,vndbinder 三设备（各自 context）已编入内核"
    else
        die "内核里没有 binder 设备名默认值 —— 现代 binder 没装进去，/dev/hwbinder 会被 -EBUSY 挡掉"
    fi

    #   标记 2：现代 binder 的事务日志格式串带第三个尺寸（extra_buffers_size），
    #           老驱动只有 data_size-offsets_size 两个，绝不会出现 %lld-%lld-%lld
    SG=0
    if [ -f "$CHK" ]; then
        grep -qa 'size %lld-%lld-%lld' "$CHK" && SG=1
    else
        gzip -dc "$IMG" 2>/dev/null | grep -qa 'size %lld-%lld-%lld' && SG=1
    fi
    echo "自检（SG / extra_buffers_size）: sg=$SG"
    if [ "$SG" = "1" ]; then
        echo "自检通过：BC_TRANSACTION_SG + extra_buffers_size 已编入内核"
    else
        die "内核里没有 SG/extra_buffers_size 痕迹 —— 8.1 的 hwbinder 事务会被拒，HAL 全废"
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
