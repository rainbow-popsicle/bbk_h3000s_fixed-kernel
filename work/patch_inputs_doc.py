def patch(path, pairs):
    t = open(path, encoding="utf-8").read()
    n = 0
    for old, new in pairs:
        if old in t:
            t = t.replace(old, new, 1)
            n += 1
        else:
            print("  [未命中] %s :: %s" % (path, old.strip().splitlines()[0][:50]))
    open(path, "w", encoding="utf-8", newline="\n").write(t)
    print("  %s 改 %d/%d 处" % (path, n, len(pairs)))


patch("inputs/README.txt", [
    ("""按需放：
  inputs/defconfig     社区补丁仓库的 h3000s_defconfig
                       （Haoduo9/android_kernel_bbk_h3000s_patches/defconfig/h3000s_defconfig）
                       有它就不必依赖内核树自带的 defconfig""",
     """按需放：
  inputs/defconfig     可选。H3000S 内核树**自带** arch/arm64/configs/h3000s_defconfig
                       （与 patches 仓库那份 sha 相同），一般不用放。
                       只有想覆盖它时才需要。"""),
    ("""  inputs/boot.dtb      社区修正版 DTB（.../dts/h3000s-FIXED5.dtb）
                       优先于从 boot.img 里拆出来的那份；不打这个就用原厂 DTB""",
     """  inputs/boot.dtb      可选。社区修正版 DTB（patches 仓库 dts/h3000s-FIXED5.dtb，55,013 字节）
                       放了就用它；不放则自动从 boot.img 里拆出原厂 DTB（55,049 字节）"""),
])

patch("README.md", [
    ("   - `defconfig` = `h3000s_defconfig`（也可把该文件放到 `inputs/defconfig`）",
     "   - `defconfig` = `h3000s_defconfig`（**内核树里已经自带**，不用另外下载；跑错了脚本会列出可选值）"),
])
