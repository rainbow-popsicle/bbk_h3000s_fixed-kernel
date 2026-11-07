P = "scripts/build.sh"
t = open(P, encoding="utf-8").read()

old = '''if [ ! -f .config ]; then
    [ -f "arch/arm64/configs/$DEFCONFIG" ] \\
        || die "源码树里找不到 arch/arm64/configs/$DEFCONFIG，请改 DEFCONFIG"
    make ARCH=arm64 "$DEFCONFIG"
fi'''

new = '''if [ ! -f .config ]; then
    if [ ! -f "arch/arm64/configs/$DEFCONFIG" ]; then
        echo "源码树里没有 arch/arm64/configs/$DEFCONFIG，这份树自带的可选项："
        ls arch/arm64/configs/ 2>/dev/null | grep '_defconfig$' | sed 's/^/  /' | head -30
        die "请把 DEFCONFIG 改成上面某个名字，或把外部 defconfig 放到 inputs/defconfig"
    fi
    make ARCH=arm64 "$DEFCONFIG"
fi'''

assert old in t, "未命中预检块"
t = t.replace(old, new, 1)
open(P, "w", encoding="utf-8", newline="\n").write(t)
print("已加 defconfig 预检，build.sh 现为 %d 字节" % len(t))
