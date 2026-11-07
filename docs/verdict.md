# 结论书：S3 Pro 的"安卓 9 内核"这件事

一句话：**做不了，但原因不在工具，在硬件资料的可得性。**

> **更正（重要）**：本文原先判定"屏驱动无源码、不可得"——**这条是错的**。
> 社区的 H3000S 移植已经公开在 GitHub 上：
> - 内核树：`Haoduo9/android_kernel_bbk_h3000s`，分支 `lineage-15.1-h3000s`（Linux **3.18.140**）
> - 配置与板级：`Haoduo9/android_kernel_bbk_h3000s_patches`（`defconfig/h3000s_defconfig`、`dts/h3000s-FIXED5.dtb`）
> - 设备树：`Haoduo9/android_device_bbk_h3000s`，vendor：`Haoduo9/android_vendor_bbk_h3000s`
>
> 所以真实瓶颈不是"没有代码"，而是"这份移植本身有大量 bug、需要有人修"。
> 安卓 9 仍然不可行（3.18 内核 + 非 Treble），但**安卓 8.1（LineageOS 15.1）是有公开路径的**。
> 下面的原始结论仅在第 3 节"安卓 9"部分仍然成立。


这份文档把"问了什么、查到了什么、做到了什么、剩下什么"压在一页里，
方便你以后隔几个月回来直接看结论，不用重读整个 README。

---

## 1. 原始需求

> 为一台步步高 S3 Pro（当时以为是安卓 9）编译一个安卓 9 的内核。

## 2. 查清的事实（全部来自实物文件，不是推测）

| 项 | 实测值 | 怎么得到的 |
| --- | --- | --- |
| 系统版本 | **Android 7.0 / SDK 24 / NRD90M** | `system.img` 里 build.prop 的 `ro.build.version.*` |
| 机型 / 板号 | `S3 Pro` / `H3000S` | 同上 + 固件的 scatter 文件名 |
| 内核 | **Linux 3.18.35**，gcc 4.9，2020-05-16 构建 | 解压 boot 内核取 `Linux version` 串 |
| 内核形态 | Image.gz-dtb：gzip 7,017,867 字节 + DTB 55,049 字节 | gzip 流结束后是合法 FDT |
| 厂商源码树 | `/data/cp/H3000S-8176/kernel-3.18/` | 内核里 855 条 `__FILE__` 路径 |
| 分区方案 | A-only，无 `vendor` / `vbmeta` / `super` | 固件目录 + 头部解析 |
| Treble | **否** | 无 vendor 分区 |
| 内嵌 config | **没有**（`CONFIG_IKCONFIG` 未开） | 内核里搜不到 `IKCFG_ST` |
| 屏驱动 | `nt35523_fhd_vdo_boe`（编译进内核） | 内核字符串，紧邻 LCM 注册日志 |
| 屏驱动第二来源 | `lk.bin` 0x48a50 处同名 | bootloader 里也编译了一份 |
| 触摸 IC | Goodix GT9xx | 内核日志串 + DTB `mediatek,goodix_touch` |
| boot 分区 | 16MB，已用 9.1MB，余量 7.3MB | 头部 + 实际长度 |

## 3. 为什么"安卓 9 内核"不可能

四道互相独立的墙：

1. **内核世代**：3.18.35 是 MTK Android N 的 BSP。安卓 9 的 HAL、`init`、SELinux
   策略按 4.4+ 设计，**换内核抬不动用户空间**。
2. **没有 Treble**：无 `vendor` 分区，GSI 刷不进去——"换个原生系统"这条捷径不存在。
3. **屏驱动是私有的**：`nt35523_fhd_vdo_boe` 只存在于 BBK 的私有树，
   公开的 MT8176 树（cappu / mt8176_common）里是别的屏（GPD 的 `lq101r1sx01a_wqxga_dsi_vdo`）。
   任何重编的内核都点不亮这块屏。
4. **配置不可得**：原厂没开 `CONFIG_IKCONFIG`，`/proc/config.gz` 不存在，
   root 和 TWRP 都取不到——连"原样重编"都缺一份准确 config。

结论：安卓 9 需要**整机移植**（内核 + vendor + system 一起重做），
而第 3 道墙让移植连起点都没有。

## 4. 实际交付了什么

| 交付物 | 状态 | 证据 |
| --- | --- | --- |
| `scripts/bootimg.py`：解析 / 拆包 / 换内核 / 提配置 / 拆 DTB | ✅ | 78 项断言；**用原厂内核自身拼回去，16MB 镜像与原图逐字节一致** |
| `scripts/analyze_dump.py`：分区 dump 体检 | ✅ | 在真实固件目录上跑通，正确判定非 Treble / 3.18 |
| `scripts/mine_kernel.py`：从镜像挖版本/源码树/屏驱动/触摸 | ✅ | 在 `boot.img` 和 `lk.bin` 上都命中正确驱动名 |
| `scripts/check-bootimg.ps1`：Windows 版体检（免 Python） | ✅ | 与 Python 版逐字段对账一致 |
| `scripts/build.sh` + GitHub Actions 工作流 | ✅ 结构正确 / ⚠️ 未真编译 | 48 项结构+变量断言，含变异测试 |
| `tests/test_workflow.py`：工作流静态检查 | ✅ | 19 项，含注入 bug 验证 |
| `twrp-dump/`：TWRP 只读取证模块 | ✅（用不上） | 11 项打包断言 |

合计 **156 项断言**，全部通过。

**没有做到的**：内核一次都没真正编译过——开发机没有 bash / make / gcc，也没有网络拉取
工具链和 1GB 源码。这是环境限制，不是设计取舍。

## 5. 如果以后还想往下走

按投入从低到高：

| 想做的事 | 需要什么 | 预期 |
| --- | --- | --- |
| 验证整条流水线 | 把 `boot.img` 放进 `inputs/`，开 Actions 跑一次 `dry_run` | 2～3 分钟出结果，不花钱 |
| 编一版同版本内核 | 同上，关掉 dry_run；配置用公开树 defconfig | 能编出来；刷上去**大概率黑屏** |
| 让屏亮起来 | 反汇编 `lk.bin`，从 `nt35523_fhd_vdo_boe` 的 `LCM_DRIVER` 结构里取出 DSI 初始化表，移植进内核 LCM 骨架 | 重度逆向，但有明确入口（0x48a50） |
| 上安卓 9 | 整机移植：内核 + vendor + system 全重做 | 现实上不可能，理由见第 3 节 |

## 6. 一句话建议

**保持现在能用的系统，别再为"安卓 9"投入。**
工具包留着：它在这台机器上做不了安卓 9，但"解析 boot.img、换内核、保住 DTB 和 ramdisk"
这套东西对任何 MTK 老设备都是通用的，换个机型能直接复用。
