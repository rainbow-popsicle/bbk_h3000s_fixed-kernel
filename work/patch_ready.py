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


patch("README.md", [
    ("""2. `git add inputs/boot.img` —— 让 Actions 能读到原厂镜像（20～60MB，远小于 100MB 限制）；""",
     """2. `inputs/boot.img` **已经放好了**（16,777,216 字节，SHA256 `8B495FEB…`，就是你设备那份原厂镜像），
   直接 `git add inputs/` 提交即可；"""),
])

# 在开头定位段后面补一句"当前就绪状态"
t = open("README.md", encoding="utf-8").read()
anchor = "> 工具包本身是好的（拼接做过逐字节回归），但它解决不了\"缺屏驱动\"这件事。"
if anchor in t and "当前就绪状态" not in t:
    t = t.replace(anchor, anchor + """

> **当前就绪状态**：`inputs/boot.img` 已就位；内核树、defconfig、分支都已指向社区 H3000S 移植
> （`Haoduo9/android_kernel_bbk_h3000s@lineage-15.1-h3000s`，3.18.140）。
> 把仓库推到 GitHub、点 Actions 就能开始构建——剩下的不确定性全在上游那棵树本身。""", 1)
    open("README.md", "w", encoding="utf-8", newline="\n").write(t)
    print("  README 加了就绪状态段")
else:
    print("  README 就绪状态段未写入（锚点未命中或已存在）")
