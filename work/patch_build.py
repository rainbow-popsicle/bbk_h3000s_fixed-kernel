import io
import os

P = "scripts/build.sh"
t = open(P, encoding="utf-8").read()
orig = t

# 1) 默认目标换成 Haoduo9 的 H3000S 树
old = '''KERNEL_REPO="${KERNEL_REPO:-https://github.com/Goayandi/android_kernel_mt8176_common.git}"
KERNEL_BRANCH="${KERNEL_BRANCH:-master}"
DEFCONFIG="${DEFCONFIG:-mt8176_defconfig}"'''
new = '''KERNEL_REPO="${KERNEL_REPO:-https://github.com/Haoduo9/android_kernel_bbk_h3000s.git}"
KERNEL_BRANCH="${KERNEL_BRANCH:-lineage-15.1-h3000s}"
DEFCONFIG="${DEFCONFIG:-h3000s_defconfig}"'''
assert old in t, "默认值块没找到"
t = t.replace(old, new, 1)

# 2) 新增两个输入
old = '''STOCK_BOOT="${STOCK_BOOT:-$ROOT/inputs/boot.img}"
INPUT_CONFIG="${INPUT_CONFIG:-$ROOT/inputs/config.txt}"'''
new = '''STOCK_BOOT="${STOCK_BOOT:-$ROOT/inputs/boot.img}"
INPUT_CONFIG="${INPUT_CONFIG:-$ROOT/inputs/config.txt}"
INPUT_DEFCONFIG="${INPUT_DEFCONFIG:-$ROOT/inputs/defconfig}"
INPUT_DTB="${INPUT_DTB:-$ROOT/inputs/boot.dtb}"'''
assert old in t, "输入变量块没找到"
t = t.replace(old, new, 1)

# 3) 支持外部 defconfig（社区补丁仓库里的 h3000s_defconfig）
old = '''step "4/6 确定 .config"
CONFIG_SOURCE="defconfig"'''
new = '''step "4/6 确定 .config"
CONFIG_SOURCE="defconfig"

# 社区补丁仓库（Haoduo9/android_kernel_bbk_h3000s_patches）里的 defconfig
# 不在内核树的 arch/arm64/configs 下，先拷进去再当 make 目标用
if [ -f "$INPUT_DEFCONFIG" ]; then
    mkdir -p arch/arm64/configs
    cp "$INPUT_DEFCONFIG" arch/arm64/configs/h3000s_local_defconfig
    DEFCONFIG="h3000s_local_defconfig"
    echo "使用社区 defconfig：$INPUT_DEFCONFIG"
fi'''
assert old in t, "step4 开头没找到"
t = t.replace(old, new, 1)

# 4) DTB 来源优先级：inputs/boot.dtb（社区修正版）> 从原厂 boot.img 拆
old = '''    SPLICE_EXTRA=()
    DTB_PATH=""
    if "$PY" "$ROOT/scripts/bootimg.py" kernel-parts \\
            --image "$STOCK_BOOT" --outdir "$WORK/kparts" > "$WORK/kparts.log" 2>&1; then'''
new = '''    SPLICE_EXTRA=()
    DTB_PATH=""
    if [ -f "$INPUT_DTB" ]; then
        DTB_PATH="$INPUT_DTB"
        SPLICE_EXTRA=(--dtb "$DTB_PATH")
        echo "使用 inputs/boot.dtb（$(stat -c%s "$DTB_PATH") 字节，社区修正版）"
    elif "$PY" "$ROOT/scripts/bootimg.py" kernel-parts \\
            --image "$STOCK_BOOT" --outdir "$WORK/kparts" > "$WORK/kparts.log" 2>&1; then'''
assert old in t, "DTB 提取块没找到"
t = t.replace(old, new, 1)

# 5) 头部说明补上这两个变量
old = '''#   STOCK_BOOT      原厂 boot.img 路径（有则从中提取真实 .config，并作为打回模板）'''
new = '''#   STOCK_BOOT      原厂 boot.img 路径（有则从中提取真实 .config，并作为打回模板）
#   INPUT_DEFCONFIG 外部 defconfig 文件（社区补丁仓库的 h3000s_defconfig）
#   INPUT_DTB       外部 DTB（社区修正版），优先于从 boot.img 里拆出的那份'''
assert old in t, "头部注释没找到"
t = t.replace(old, new, 1)

open(P, "w", encoding="utf-8", newline="\n").write(t)
print("build.sh 已更新，%d -> %d 字节" % (len(orig), len(t)))
print("改动处数:", sum(1 for _ in range(1)))
