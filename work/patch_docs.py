import re

def patch(path, pairs, encoding="utf-8"):
    t = open(path, encoding=encoding).read()
    changed = 0
    for old, new in pairs:
        if old in t:
            t = t.replace(old, new, 1)
            changed += 1
        else:
            print("  [未命中] %s :: %s" % (path, old.strip().splitlines()[0][:60]))
    open(path, "w", encoding=encoding, newline="\n").write(t)
    print("  %s 改 %d/%d 处" % (path, changed, len(pairs)))

# ---------------------------------------------------------------- workflow
patch(".github/workflows/build-kernel.yml", [
    ("default: 'https://github.com/Goayandi/android_kernel_mt8176_common.git'",
     "default: 'https://github.com/Haoduo9/android_kernel_bbk_h3000s.git'"),
    ("default: 'master'", "default: 'lineage-15.1-h3000s'"),
    ("default: 'mt8176_defconfig'", "default: 'h3000s_defconfig'"),
    ("inputs.kernel_repo || 'Goayandi/android_kernel_mt8176_common'",
     "inputs.kernel_repo || 'Haoduo9/android_kernel_bbk_h3000s'"),
    ("inputs.kernel_branch || 'master'", "inputs.kernel_branch || 'lineage-15.1-h3000s'"),
    ("inputs.defconfig || 'mt8176_defconfig'", "inputs.defconfig || 'h3000s_defconfig'"),
    ("KERNEL_REPO: ${{ inputs.kernel_repo || 'https://github.com/Goayandi/android_kernel_mt8176_common.git' }}",
     "KERNEL_REPO: ${{ inputs.kernel_repo || 'https://github.com/Haoduo9/android_kernel_bbk_h3000s.git' }}"),
    ("KERNEL_BRANCH: ${{ inputs.kernel_branch || 'master' }}",
     "KERNEL_BRANCH: ${{ inputs.kernel_branch || 'lineage-15.1-h3000s' }}"),
    ("DEFCONFIG: ${{ inputs.defconfig || 'mt8176_defconfig' }}",
     "DEFCONFIG: ${{ inputs.defconfig || 'h3000s_defconfig' }}"),
    ("description: '分支（mt8176_common 是 master，cappu 是 yael）'",
     "description: '分支（H3000S 移植树是 lineage-15.1-h3000s）'"),
    ("description: 'defconfig 名（3.18.35 树是 mt8176_defconfig）'",
     "description: 'defconfig 名（H3000S 用 h3000s_defconfig，放在 inputs/defconfig 亦可）'"),
])

# ---------------------------------------------------------------- inputs 说明
open("inputs/README.txt", "w", encoding="utf-8", newline="\n").write(
"""把构建要用的文件放在这个目录。

必放（打回 boot.img 用）：
  inputs/boot.img      原厂 boot.img，作为打包模板（ramdisk / cmdline / 对齐全部沿用）

按需放：
  inputs/defconfig     社区补丁仓库的 h3000s_defconfig
                       （Haoduo9/android_kernel_bbk_h3000s_patches/defconfig/h3000s_defconfig）
                       有它就不必依赖内核树自带的 defconfig
  inputs/boot.dtb      社区修正版 DTB（.../dts/h3000s-FIXED5.dtb）
                       优先于从 boot.img 里拆出来的那份；不打这个就用原厂 DTB
  inputs/config.txt    手放的 .config（一般不需要：这台机器内核没开 CONFIG_IKCONFIG，
                       /proc/config.gz 不存在）

注意：boot.img 需要提交到 git（20~60MB），Actions 才读得到。
""")

# ---------------------------------------------------------------- README
patch("README.md", [
    ("| **`Goayandi/android_kernel_mt8176_common`（`master`）** | 通用 MT8176（GPD XD+ 等） | **3.18.35** | ★ **默认使用这棵**：与你这台机器内核**逐位同版本** |",
     "| **`Haoduo9/android_kernel_bbk_h3000s`（`lineage-15.1-h3000s`）** | **S3 Pro 专用移植** | 3.18.140 | ★ **默认使用这棵**：社区为 H3000S 做的 LineageOS 15.1 移植，配套 defconfig + DTB 都在 `android_kernel_bbk_h3000s_patches` |\n| `Goayandi/android_kernel_mt8176_common`（`master`） | 通用 MT8176（GPD XD+ 等） | 3.18.35 | 与实机内核逐位同版本，但没有 H3000S 板级支持，可作对照 |"),
    ("   - `kernel_repo` = `https://github.com/Goayandi/android_kernel_mt8176_common.git`（3.18.35，与实机同版本）\n   - `kernel_branch` = `master`\n   - `defconfig` = `mt8176_defconfig`（只在拿不到真机 config 时才用）",
     "   - `kernel_repo` = `https://github.com/Haoduo9/android_kernel_bbk_h3000s.git`（H3000S 移植树，3.18.140）\n   - `kernel_branch` = `lineage-15.1-h3000s`\n   - `defconfig` = `h3000s_defconfig`（也可把该文件放到 `inputs/defconfig`）"),
])

# ---------------------------------------------------------------- 结论书更正
t = open("docs/verdict.md", encoding="utf-8").read()
correction = """> **更正（重要）**：本文原先判定"屏驱动无源码、不可得"——**这条是错的**。
> 社区的 H3000S 移植已经公开在 GitHub 上：
> - 内核树：`Haoduo9/android_kernel_bbk_h3000s`，分支 `lineage-15.1-h3000s`（Linux **3.18.140**）
> - 配置与板级：`Haoduo9/android_kernel_bbk_h3000s_patches`（`defconfig/h3000s_defconfig`、`dts/h3000s-FIXED5.dtb`）
> - 设备树：`Haoduo9/android_device_bbk_h3000s`，vendor：`Haoduo9/android_vendor_bbk_h3000s`
>
> 所以真实瓶颈不是"没有代码"，而是"这份移植本身有大量 bug、需要有人修"。
> 安卓 9 仍然不可行（3.18 内核 + 非 Treble），但**安卓 8.1（LineageOS 15.1）是有公开路径的**。
> 下面的原始结论仅在第 3 节"安卓 9"部分仍然成立。

"""
marker = "一句话：**做不了，但原因不在工具，在硬件资料的可得性。**"
if correction not in t:
    t = t.replace(marker, marker + "\n\n" + correction, 1)
open("docs/verdict.md", "w", encoding="utf-8", newline="\n").write(t)
print("  docs/verdict.md 已加更正块" if correction in t else "  docs/verdict.md 更正块未写入")
