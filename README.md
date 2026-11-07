# 步步高家教机 S3 Pro（MT8176 / Android 7.0）内核构建包

这是一个**在 GitHub Actions 上云编译**的工具包：把原厂 `boot.img` 丢进 `inputs/`，
Actions 里点一下，就能拿到"新内核 + 原厂 ramdisk + 原厂 cmdline + 原厂 DTB"拼好的可刷 `boot.img`。

> **先读这里**：真机已确认为 Android 7.0 / 内核 3.18.35 / 非 Treble。
> 想升级到安卓 9 是**不可行**的，原因和替代方案见紧跟着的第一节。
> 工具包本身是好的（拼接做过逐字节回归），但它解决不了"缺屏驱动"这件事。

> **当前就绪状态**：`inputs/boot.img` 已就位；内核树、defconfig、分支都已指向社区 H3000S 移植
> （`Haoduo9/android_kernel_bbk_h3000s@lineage-15.1-h3000s`，3.18.140）。
> 把仓库推到 GitHub、点 Actions 就能开始构建——剩下的不确定性全在上游那棵树本身。
>
> 只想看结论、不想读全文 → **[docs/verdict.md](docs/verdict.md)**（一页）。
>
> **第一次接触 GitHub / 完全新手** → **[docs/新手教程-GitHub编译内核.md](docs/新手教程-GitHub编译内核.md)**（手把手：注册、上传、编译、刷机、救砖）

---

## ★ 实机体检结论（基于 `s3pro_crack_v2.1/images/` 固件包）

已经拿真实固件跑过一遍，结论如下（全部是读出来的，不是猜的）：

| 项 | 实测值 | 依据 |
| --- | --- | --- |
| 内核 | **Linux 3.18.35**，gcc 4.9，2020-05-16 构建 | 解压 boot 内核取 `Linux version` 串 |
| 内核形态 | **Image.gz-dtb**：gzip 流 7,017,867 字节 + 追加 DTB **55,049 字节** | gzip 流结束后是合法 FDT（`d00dfeed`） |
| 平台 | MT8173/MT8176（DTB 里是 `mediatek,mt8173` 兼容） | DTB strings |
| 系统 | **Android 7.0 / SDK 24 / NRD90M** | `system.img` 里的 build.prop |
| 机型 | `ro.product.model=S3 Pro`，板号 `H3000S` | 同上 |
| 版本 | `V1.6.0_200516`，指纹 `EEBBK/full_H3000S/H3000S:7.0/NRD90M` | 同上 |
| 分区 | 无 `vendor`、无 `vbmeta`、无 `super`，A-only | 固件包目录 + printgpt 风格清单 |
| Treble | **不是 Treble** | 无 vendor 分区 |
| 内嵌 config | **没有**（`CONFIG_IKCONFIG` 未开） | 内核里搜不到 `IKCFG_ST` |
| boot 分区 | 16MB，已用 9.1MB，**余量 7.3MB** | 头部 + 实际使用量 |

### 这些数字意味着什么

1. **内核版本对得上**：`android_kernel_mt8176_common` 就是 **3.18.35**，与这台机器
   **完全同版本**——不是"3.18 段对得上"，是逐位相同。自编内核这条路是通的。
2. **必须保住 DTB**：原厂把板级 DTB（屏/触摸/PMIC 参数）直接追加在内核 gzip 流后面。
   换内核时如果不把它接回去，等于把整机硬件描述丢了。工具已经支持（见第 6 节）。
3. **GSI 这条路断了**：没有 vendor 分区就不是 Treble，GSI 刷不进去。
   想要"原生安卓 9"，只能整机移植用户空间（system/vendor），跟编内核是两件不同的事。
4. **拿不到原厂配置**：没有 `CONFIG_IKCONFIG`，只能靠设备 root 后
   `zcat /proc/config.gz`——但**你这台机器连这个也不行**，原厂没开 `CONFIG_IKCONFIG`，内核里根本没存配置（详见 [twrp-dump/README.md](twrp-dump/README.md)）。只能拿公开树的 defconfig 当基线，有黑屏风险。
5. **容量够用**：新内核只要不超过"原内核 + 7.3MB"就装得进 boot 分区。

### 需要你确认的一件事

### 已确认：设备就是 Android 7.0，全套用这个包刷入

机主确认：**这台 S3 Pro 跑的就是 Android 7.0**，并且是用这套
`s3pro_crack_v2.1` 整包刷入的。所以上面表里的 `boot.img` **就是你设备的真实内核**，
前面的分析（3.18.35 / Image.gz-dtb / 非 Treble / `nt35523_fhd_vdo_boe`）全部成立。

### 那么"编一个安卓 9 内核"这个目标，直接结论是：不可行

不是思路问题，是四道各自独立的墙：

| 墙 | 事实 | 为什么过不去 |
| --- | --- | --- |
| 1. 内核版本 | 3.18.35（MTK Android N 世代 BSP） | 安卓 9 的 HAL / `init` / SELinux 策略是按 4.4+ 设计的，只有内核没法把用户空间抬到 9 |
| 2. 没有 Treble | 无 `vendor` 分区、无 `vbmeta`、无 `super` | GSI 刷不进去，"换个原生系统"这条捷径不存在 |
| 3. 屏驱动是私有二进制 | `nt35523_fhd_vdo_boe` 编在内核里，无源码 | 任何重编的内核都点不亮这块屏 |
| 4. 配置不可得 | 内核没开 `CONFIG_IKCONFIG` | 连"原样重编"都缺一份准确 config |

换句话说：**安卓 9 需要的是整机移植（内核 + vendor + system 一起重做），
不是编译一个内核。** 而第 3 条墙决定了连"整机移植"的起点都没有。

### 那这台机器上还能做什么

| 目标 | 可行性 | 怎么做 |
| --- | --- | --- |
| 装第三方 App / 解除安装限制 | ✅ 已经实现了 | 你现在刷的这套包就是这个路线 |
| 原样重编同版本内核（版本号、打包都正确） | ⚠️ 能编出来 | 需要先拿一份 config 基线；且大概率黑屏 |
| 让重编内核点亮屏幕 | ❌ | 需要 `nt35523_fhd_vdo_boe` 源码或等价 init 序列，BBK 不公开 |
| 升级到安卓 9 | ❌ | 见上面四道墙 |

> 这个包叫 `s3pro_crack_v2.1`，来源不明。我只读取分析了里面的文件，
> 没有执行任何程序。既然你已经整包刷过并正常使用，就以它为准。

---

## 0. 先把话说清楚：为什么"直接编一个安卓 9 内核"编不了

| 事实 | 依据 |
| --- | --- |
| S3 Pro 是 **MT8176**（2×Cortex-A72@2.1GHz + 4×Cortex-A53@1.7GHz，64 位六核） | 步步高官网参数页原文："64 位六核平板专用处理器，2X2.1GHz 高性能核心 + 4X1.7GHz 高效能核心" |
| 步步高**不公开**这台机器的 GPL 内核源码 | 学习机产品线惯例；官网下载区只有固件/助手，没有 kernel source |
| 目前能用的最接近开源树是**小米平板 3（cappu，同为 MT8176）**的 MTK 源码，版本 **3.18.123** | `Goayandi/android_kernel_xiaomi_cappu`，分支 `yael`，defconfig `cappu_defconfig` |

所以"编译内核"这件事的真实前提是三条，缺一条都白搭：

1. **源码版本要和你的机器一致**（原厂是 3.18 还是 4.4？）——这决定用哪棵树；
2. **配置要来自真机**（boot.img 里的 `IKCFG`，或 `/proc/config.gz`）——你这台两者都没有，这是本项目最大的硬缺口；
3. **DTB 要来自真机**（屏、触摸、PMIC、音频的硬件描述都在里面）——本工具包的策略是**不动 DTB**，只换内核，其余字节原样保留。

> 我已经把 2 和 3 做成了自动化：`scripts/bootimg.py` 会从你的 `boot.img` 里
> 提取真实 `.config` 和内核版本，再把新内核"拼接"回原镜像，ramdisk / cmdline /
> MTK 私有头 / 对齐填充全部原样不动。

**关于"原生安卓 9"**：办法只有一个——刷 GSI，它要求设备是 Treble 机型。
你这台**不是**（无 `vendor` 分区），所以路线 A 对它不成立，保留在第 4 节仅供对照。

---

## 1. 你手上必须有的东西

```bash
# 用 mtkclient 进 BROM（关机、按住音量上+下、插 USB），先看分区表
python mtk printgpt
# 备份 boot（全盘备份更好：python mtk rl backup_dir）
python mtk r boot boot.img
# 顺手把判断 Treble 用的分区也读出来
python mtk r vendor vendor.img     # 有 vendor 分区 = 大概率 Treble
```

拿到 `boot.img` 后，**先本地跑一遍体检**（Windows / Linux 都行，只要装了 Python 3）：

```bash
python scripts/bootimg.py info boot.img
```

没装 Python 也没关系，Windows 自带 PowerShell 就能跑同一套检查：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\check-bootimg.ps1 inputs\boot.img
```

输出里的这几行，直接决定后面走哪条路：

```
内核版本字符串  : Linux version 3.18.xxx ...   ← 决定选哪棵源码树
内嵌 .config    : 有，xxxx 行 CONFIG_*         ← 有它就能百分百还原原厂配置
kernel          : off=2048 size=... 格式=gzip  ← 决定新内核要不要 gzip
```

### 更省事：直接跑整合分析

回读了多个分区之后，一条命令给出整体结论：

```bash
python scripts/analyze_dump.py dump目录/ [--printgpt printgpt.txt]
```

它会输出分区清单、内核 release/格式、`vendor`/`vbmeta`/`super`/A-B 判断，
并直接给结论：**能不能刷 GSI**、**公开源码树的版本段对不对得上**。

### 已知的 MT8176 开源树（我已核实版本）

| 仓库 | 机型 | 内核版本 | 说明 |
| --- | --- | --- | --- |
| **`Haoduo9/android_kernel_bbk_h3000s`（`lineage-15.1-h3000s`）** | **S3 Pro 专用移植** | 3.18.140 | ★ **默认使用这棵**：社区为 H3000S 做的 LineageOS 15.1 移植，配套 defconfig + DTB 都在 `android_kernel_bbk_h3000s_patches` |
| `Goayandi/android_kernel_mt8176_common`（`master`） | 通用 MT8176（GPD XD+ 等） | 3.18.35 | 与实机内核逐位同版本，但没有 H3000S 板级支持，可作对照 |
| `Goayandi/android_kernel_xiaomi_cappu`（`yael`） | 小米平板 3 | **3.18.123** | 同平台、sublevel 更新；板级更偏小米 |
| `Goayandi/android_kernel_asus_z500m` | 华硕 ZenPad 3S 10 | 3.18.x（未逐字核实） | ASUS 官方发布 |
| `Goayandi/android_kernel_acer_titan` | 宏碁 Iconia Tab 10 a3-a50 | 3.18.x（未逐字核实） | 仓库描述 |

**关键结论：MT8176 目前没有任何公开的 4.4 源码树。**
所以如果你的原厂内核是 4.4.x，自编这条路要先做跨平台移植，不是改个参数能解决的。

### 板级依赖（H3000S）——这才是真正的工作量所在

从原厂内核**镜像本体**里挖出来的东西（解压后扫字符串，不是猜）：

| 发现 | 值 | 依据 |
| --- | --- | --- |
| 厂商源码树路径 | `/data/cp/H3000S-8176/kernel-3.18/` | 内核里 855 条 `__FILE__` 路径 |
| 工程名 | `H3000S-8176`（板号 + SoC） | 同上 |
| 编译进内核的屏驱动 | **`nt35523_fhd_vdo_boe`**（Novatek NT35523，VDO 模式，BOE 屏） | 该串紧邻 `LCM: Register lcm driver`、`[KERNEL/LCM]lcd_id` 日志 |
| 同一个驱动在 **LK（bootloader）里也有** | `lk.bin` 偏移 0x48a50 处就是 `nt35523_fhd_vdo_boe`，同处还有 `platform/mt8173/disp_lcm.c`、`[LK/LCM] lcm_init() enter`、板号 `H3000S` | 说明 MTK 的 LCM 源码在 LK 和内核里各编译了一次 |
| 这个屏驱动公开吗 | **不在公开树里**：`mt8176_common` 用的是 `lq101r1sx01a_wqxga_dsi_vdo`（GPD 的 2560×1600 屏） | 对比该树 defconfig |
| 触摸 IC | **Goodix GT9xx** | 内核里 `3GTP GT9 series touch panel driver init`；DTB 里 `mediatek,goodix_touch` |
| 触摸可移植性 | **较好**：GT9xx 是 MTK 参考设计常用驱动，公开树多半带源码 | — |
| 硬件版本 | `mainboard_id-gpio1/2` | DTB 属性 |

**这意味着什么，必须说清楚**：

拿公开的 3.18.35 树 + 原厂 DTB 编出来的内核，最可能的结果是
**能启动、但屏幕不亮**——因为 `nt35523_fhd_vdo_boe` 不在树里，而 BBK 不发布它。
这个驱动还带 `lcd_id` 检测逻辑，说明面板识别也是定制过的一环。

不过 `lk.bin` 这个发现把"完全无迹可寻"改成了"有二进制可挖"：
LK 里那份驱动必须能点亮 logo，所以**面板初始化序列就在这 337KB 里**。
有 IDA/Ghidra 基础的人可以：定位 `nt35523_fhd_vdo_boe` 字符串（0x48a50）→
找到引用它的 `LCM_DRIVER` 结构 → 反汇编其中的 `init` 函数 → 取出 DSI 初始化表 →
移植到内核的 LCM 驱动骨架里。这是重活，但路径是通的。

自动化工具做不到这一步——反汇编表结构没有可靠的特征可以盲扫，
硬猜出来的数据只会让人白折腾。

触摸反而有希望：GT9xx 驱动在 MTK 参考树里普遍存在，移植成本比屏低一个数量级。

所以自编内核在这个机器上能达成的目标要现实一点看待：

| 目标 | 可行性 |
| --- | --- |
| 编出版本正确、能被打包进 boot 分区的内核 | ✅ 可以 |
| 编出能开机、显示正常的内核 | ⚠️ 只能靠反汇编 `lk.bin` 里的那份驱动取 init 序列，属重度逆向 |
| 编出能开机、触摸可用的内核 | ⚠️ 有希望：GT9xx 驱动可移植 |
| 靠换内核把系统升级到安卓 9 | ❌ 不是内核能解决的事 |

---

## 2. 目录结构

```
s3pro-kernel/
├── .github/workflows/build-kernel.yml   # 云编译流水线（先自测，再编译）
├── docs/gsi-route.md                    # 路线 A：GSI 刷写 / 验收 / 故障 / 回滚
├── twrp-dump/                           # （可选）TWRP 只读模块，已打好包，本机用不上
├── inputs/                              # ★ boot.img / config.txt 放这里
├── out/                                 # 构建产物（本地跑时生成）
├── scripts/
│   ├── bootimg.py                       # boot.img 解析/拆包/换内核/提 .config
│   ├── analyze_dump.py                  # 分区 dump 体检：Treble / A-B / AVB / 内核版本
│   ├── mine_kernel.py                   # 从内核镜像挖版本/源码树路径/屏驱动名/触摸型号
│   ├── check-bootimg.ps1                # Windows 版体检（不用装 Python）
│   ├── make_twrp_zip.py                 # 打包 TWRP 只读模块（强制 LF + 可执行位）
│   └── build.sh                         # Actions 里跑的完整构建脚本
└── tests/
    ├── test_bootimg.py                  # 12 组 / 77 项：解析、拼接、DTB、格式、内核挖掘
    ├── sh_syntax.py                     # bash 结构 + set -u 变量检查器
    ├── test_sh_syntax.py                # 48 项，含对 build.sh 的变异测试
    ├── test_workflow.py                 # 19 项：工作流结构检查
    └── test_twrp_zip.py                 # 11 项：打包器（LF / 可执行位 / 必要条目）
```

没有 Python 也能体检（Windows 自带 PowerShell 即可）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\check-bootimg.ps1 inputs\boot.img
```

它输出的字段与 `bootimg.py info` 一一对应。两个实现的字段一致性已用合成镜像逐项核对过
（page_size、内核偏移/大小/格式、版本号、内嵌 config 行数全部相同）。

> 该脚本以 **UTF-8 with BOM** 保存 —— Windows PowerShell 5.1 没有 BOM 会按本地代码页
> 解码，中文注释会把脚本语法搞坏。别用会去掉 BOM 的编辑器重存。

`bootimg.py` 的能力（已被自测覆盖）：

- 解析 Android boot header v0 / v1 / v2，自动识别 **MTK 私有 512 字节头**（`MTK_BLOADER_INFO`）导致的偏移错位；
- `splice` 只替换内核段，重新补齐页对齐、回填 `kernel_size`，ramdisk 及之后字节**逐字节保留**；
- 从内核里提取 `Linux version` / release 号 / 内嵌 `.config`（兼容 zlib 流 / gzip 流 / 整包被 gzip 压缩三种情况）。

本地验证一下工具本身（不用连手机）：

```bash
python tests/test_bootimg.py     # boot.img 解析/拼接/配置提取，应输出 "全部通过"
python tests/test_sh_syntax.py   # build.sh 结构检查，同上
python tests/test_workflow.py    # 工作流 YAML 结构检查，同上
```

> `sh_syntax.py` 是我自己写的启发式检查器，不起 shellcheck 的作用（这台机器没有 bash、
> 也没有网络装 shellcheck），但它做过**变异测试**：删掉 `fi`、`done`、`esac`、制造未闭合
> 引号，都必须被它报出来——否则这个检查器就是永远返回"没问题"的空壳。

---

## 3. 云编译：把仓库推上 GitHub

1. 新建一个 GitHub 仓库，把本目录整个推上去（`.github/` 要一起）；
2. `inputs/boot.img` **已经放好了**（16,777,216 字节，SHA256 `8B495FEB…`，就是你设备那份原厂镜像），
   直接 `git add inputs/` 提交即可；
   如果原厂没开 `CONFIG_IKCONFIG`（`info` 会告诉你），设备已 root 的话再放一份
   `inputs/config.txt`，它的优先级最高（**本机拿不到**：内核没开 `CONFIG_IKCONFIG`，见 [twrp-dump/README.md](twrp-dump/README.md)）；
3. 打开 **Actions → build-s3pro-kernel → Run workflow**，参数一般不用改：
   - `kernel_repo` = `https://github.com/Haoduo9/android_kernel_bbk_h3000s.git`（H3000S 移植树，3.18.140）
   - `kernel_branch` = `lineage-15.1-h3000s`
   - `defconfig` = `h3000s_defconfig`（**内核树里已经自带**，不用另外下载；跑错了脚本会列出可选值）
   - `kernel_target` = `Image`（若源码树是 MTK 那种"内核自带 DTB"的，改成 `Image.gz-dtb`）
   - `toolchain` = `gcc49`（Google 预编译 aarch64 GCC 4.9；3.18/4.4 老内核换新 GCC 必挂）
   - `dry_run` = **第一次建议打开**：只验证"工具链能跑、源码树可读、配置能定型"三段，
     2～3 分钟出结果，不用先熬 30 分钟编译才发现 defconfig 名字写错
4. 约 20～40 分钟后，在 Artifacts 里下载 `s3pro-kernel-<run>`，里面：
   - `boot.img` —— 已拼好的可刷镜像（有 `inputs/boot.img` 时才有）
   - `Image` / `Image.gz`、`*.sha256`、`build-info.txt`、`build.log`
5. 日志里 `config_source=stock-ikconfig` 说明用的是你机器的真实配置，那就对了；
   如果是 `config_source=defconfig`，说明原厂没开 `CONFIG_IKCONFIG`，得从别处搞配置（见第 6 节）。

---

## 3.5 决策矩阵：体检结果直接对应哪条路

体检报告出来后，对照这张表就知道该干什么，不用再猜。

| 原厂内核 | vendor 分区 | 内嵌 config | 结论 | 下一步 |
| --- | --- | --- | --- | --- |
| 3.18.x | 有 | 有 | **最理想**：GSI 能刷，内核也能编 | 先走路线 A（半天出结果）；只有 GSI 被内核卡住才转 B |
| 3.18.x | 无 | 有 | 能自编内核，但 system 侧要自己改包 | 走路线 B（`mt8176_common` 树 + 原厂 config） |
| **3.18.x** | **无** | **无** | **← 你这台机器就是这个格子**：版本对得上、配置拿不到、非 Treble | GSI 不可行；配置只能用公开树 defconfig；注意 LCM 驱动缺失 |
| 3.18.x | 有 | 无 | 能自编，但缺配置会黑屏 | 用公开树 defconfig 起步，逐项对齐 |
| 4.4.x | 有 | 任意 | **自编不可行**（MT8176 无 4.4 公开树） | 只走路线 A，内核保持原厂 |
| 4.4.x | 无 | 任意 | 两条路都难走 | 维持原厂系统；整机移植属研究级，不推荐 |
| 未知 | — | — | 信息不足 | 先补 dump，跑 `analyze_dump.py` |

对应的命令：

```bash
# 有 dump 之后的第 0 步
python scripts/analyze_dump.py dump目录/

# 走路线 A（GSI）
# → docs/gsi-route.md

# 走路线 B（自编内核）
git add inputs/boot.img && git commit -m "add stock boot.img" && git push
# → GitHub Actions，先开 dry_run，通过后关掉正式编译
```

---

## 4. 两条路线，选一条

### 路线 A：刷原生 Android 9（GSI）——⚠️ 对你这台机器**不适用**

前提：**设备是 Treble 机型**（有独立 `vendor` 分区，或 `getprop ro.treble.enabled` 为 `true`）。
**S3 Pro 实测无 `vendor` 分区，不是 Treble，本节内容对它无效**，保留只为说明"为什么不行"。

- Android 9 的 GSI 需要 `arm64`、`A-only` 版本（S3 Pro 是 A-only 的概率大；有 A/B 就用 `ab` 版）；
- 常见来源：phhusson 的 `treble_experimentations` 发行版、ErfanGSIs 等；
- 刷写（bootloader 已解锁）：

```bash
fastboot flash system system.img
fastboot --disable-verity --disable-verification flash vbmeta vbmeta.img   # 视机型需要
fastboot -w
fastboot reboot
```

- 典型问题：开机卡 logo（多半是内核与 GSI 的 `init` 期望不符 / dm-verity）、无信号（基带）、
  触摸失灵（vendor 里的触摸 HAL 与内核驱动版本不匹配）。
- **优点**：不动内核，变砖风险最低，`boot` 分区保持原厂，随时能回退。

> 完整版（选镜像、fastboot 与 mtkclient 两种刷法、验收清单、故障对照表、回滚命令）见
> [docs/gsi-route.md](docs/gsi-route.md)。**动手前请把第 7 节的回滚命令抄下来。**

### 路线 B：自己编内核

适用场景：需要调频、加特性、修 GSI 踩到的内核问题，或者原厂内核与 GSI 不兼容必须换。

风险提醒（务必读）：

- 内核版本必须匹配（原厂 `3.18.x` 就编 `3.18.x`；原厂是 `4.4` 的话，MT8176 的 4.4 开源树更难找，
  大概率要去 MTK 4.4 的同平台机型源码里移植）；
- 驱动必须在那棵树里存在。屏、触摸、传感器、音频的**参数**在 DTB（我们不动），
  但**驱动代码**在源码树里——树里没有对应驱动，编出来的内核就是黑屏/无触摸；
- MTK 的 `boot` 分区容量有限，内核变大超过分区就刷不进去（`splice` 会提示增量）；
- 解锁 BL 后可能出现 `unlocked` 水印、OTA 失效、保修丢失；
- **必须先有完整备份**，救砖靠它。

> **如果你的原厂内核是 4.4.x**（`info` 会直接告诉你）：
> MT8176 没有公开的 4.4 树，本工具包里的源码树全部不适配。
> 此时自编的真实含义是"拿另一颗 MTK 芯片的 4.4 树（例如同世代的 MT8173）
> 移植 mt8176 的板级文件与驱动"，属于研究级工作量，不是跑一条流水线能解决的。
> **建议顺序：先按路线 A 试 GSI；只有确认 GSI 是被内核卡住时，再考虑移植。**

---

## 5. 刷写与救砖

```bash
# 刷回/刷入 boot（BROM 模式）
python mtk w boot out/boot.img

# 刷回原厂备份
python mtk w boot backup/boot.img

# 实在起不来：BROM 模式整盘回写
python mtk wl backup_dir
```

`mtk w` 走 BROM，不依赖 fastboot/adb，也不怕系统起不来——这是这台机器最大的安全网。

---

## 6. 已知的坑（按踩到的概率排序）

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 内核起来了，但 WiFi / 相机 / 传感器全废（`insmod` 报 version magic 不符） | MTK 的 vendor 分区里是**预编译内核模块**，会比对 `uname -r`。新内核的 release 字符串（`3.18.123-gebc1f9a` 这种尾巴）和原厂不一致就加载失败 | 构建脚本已自动把 `CONFIG_LOCALVERSION` 按原厂 release 固定，并把 `CONFIG_LOCALVERSION_AUTO=n`；日志里应看到"把 CONFIG_LOCALVERSION 固定为 ..." |
| `config` 提取为空 | 原厂没开 `CONFIG_IKCONFIG` | 从同平台机的 `/proc/config.gz`、或厂商固件里反推；也可拿接近机型的 defconfig 当基线，但要有黑屏的心理准备 |
| 编译报 `-Werror` 一类错误 | 用了新 GCC 编老内核 | 坚持 `toolchain=gcc49` |
| 刷完黑屏、点不亮 | 树里缺屏/触摸驱动，或 DTB 不匹配 | 别改 DTB；换更接近的源码树 |
| 刷不进：`size too large` | 内核超过 `boot` 分区容量 | 减小内核（关调试、`CONFIG_DEBUG_INFO=n`、换 `CONFIG_KERNEL_LZ4`） |
| 开机后卡 logo 或反复重启 | ramdisk/AVB 校验 | 用 `splice` 保留原 ramdisk；必要时关 vbmeta 校验 |
| `boot.img` 解析偏移全错 | MTK 私有头 | 本工具已自动识别；若仍报错，把 `info` 输出贴出来 |

---

## 7. 这个包没做什么（以及为什么）

- **没有实际编译过**：当前这台 Windows 没有 WSL、`make`、`gcc`、`git`，本地编不了 aarch64 内核；
  流水线也没在你的真机上跑过，第一次跑很可能要按报错微调（多半是 `kernel_target` 和源码树路径）。
- **没有源码树的移植工作**：从 cappu 树到步步高的板子，中间可能缺驱动、缺板级文件。
  这不是脚本能自动完成的，需要按 `boot.img` 里的真实版本和 config 逐项对齐。
- **没有承诺"编出来就能开机"**：内核替换涉及 AVB、分区容量、驱动匹配，任何一项不对都会 bootloop。

我能保证的是：`boot.img` 的解析与拼接逻辑是对的（7 组自测），配置和版本信息是从**你的**镜像里读的，
而不是猜的。

---

## 8. 下一步（我需要你给的东西）

1. `inputs/boot.img`（或 `python scripts/bootimg.py info boot.img` 的完整输出）——用来定版本、定配置、定压缩格式；
2. `mtk printgpt` 的输出——确认分区表、`boot` 分区大小、有没有 `vendor` / `dtbo`；
3. 你的目标：只是想装第三方 App / 想要类原生系统 / 要调内核参数。
   如果是第二个，**先做 Treble 判断**，那能省掉 90% 的工作。
