# S3 Pro (h3000s) LineageOS 15.1 内核

> **设备**：步步高家教机 S3 Pro（h3000s / MT8173）
> **系统**：LineageOS 15.1（Android 8.1）
> **内核**：3.18.119+ · 非 Treble · boot 分区 16 MiB
> **上游**：基于 [Haoduo9/android_kernel_bbk_h3000s](https://github.com/Haoduo9/android_kernel_bbk_h3000s)（`lineage-15.1-h3000s`）移植树

---

## 内核做了什么

这棵内核是在社区 H3000S 移植树的基础上，补齐 **S3 Pro 这块板子在 Android 8.1 下跑不起来的硬件驱动**。

原厂只提供了 Android 7.0 的内核，社区移植树虽然能开机，但屏幕、触摸、电池、传感器、光感、视频编解码等外设都不工作。本内核通过**编译期打补丁**（`scripts/build.sh` + `kernel-patches/`）逐项修复，全部可复现——不做二进制替换，每次从源码重新编译。

核心修复四项：

| # | 修复 | 用户可感知的效果 |
|---|---|---|
| 1 | 底部三大金刚键 | 返回 / 主页 / 多任务 三个触摸键可用 |
| 2 | 电量百分比 | 电量显示正确（不再减半 / 乱跳） |
| 3 | 加速度计 | 屏幕自动旋转 |
| 4 | 自动亮度 | 环境光可读，亮度能随光线变化 |

---

## 四项核心修复详解

### 1. 底部三大金刚键

**问题**：S3 Pro 的三个触摸键走 GT911 触摸芯片的按键通道，社区内核没有配置按键映射，导致按下无反应、并且系统误判为"无导航键"而显示虚拟导航栏。

**修复**：

```
patch_gt911_keys.py     # GT911 按键节点 + 键值映射
patch_gt911_touch.py    # GT911 触摸主驱动（与按键共用）
```

配合 ramdisk 属性 `qemu.hw.mainkeys=1` 告知系统"有物理按键"，不再显示虚拟导航栏。

**效果**：三大金刚键正常工作，状态栏/导航栏布局回到设备原生形态。

---

### 2. 电量百分比

**问题**：MT8173 + MT6397 PMIC 的电池曲线参数在原厂 NVRAM 里，移植版内核没有正确读取，表现为电量显示约为实际的一半、或百分比乱跳。

**修复**：

```
patch_battery_qmax.py   # 电池容量 7200 mAh（原厂真实值，非猜测）
patch_battery_soc.py    # SOC 计算改用库仑计，而非固定 OCV 表
patch_battery_fgadc.py  # FGADC 日志级别（调试用）
```

关键点：`q_max = 7200 mAh` 是原厂给定的真实值；SOC 改为基于硬件库仑计（`fgauge_read_columb()`，单位 mAh，充电为正、放电为负）做增量累加，比查表更贴近真实电量。

**效果**：电量百分比与实际使用时长一致，充电/放电曲线平滑。

---

### 3. 加速度计（自动旋转）

**问题**：S3 Pro 使用 LIS3DH 加速度计，社区内核的传感器配置里没有注册该设备，导致自动旋转失效。

**修复**：

```
patch_sensors.py        # LIS3DH 注册 + 上报通道
```

**效果**：屏幕随设备方向自动旋转。

---

### 4. 自动亮度

**问题**：这是最曲折的一项。原厂 Android 7.0 使用 CM36686 光线传感器，但社区移植树里的传感器驱动与实际硬件不匹配，光感读数始终为 0。

**修复过程**（分三层）：

| 层 | 问题 | 修复 |
|---|---|---|
| DTS 配置 | I2C 地址 / 中断脚不对 | `patch_cm36686_dts.py` / `patch_cm36686_i2c.py` |
| 中断 | EINT 触发方式不对，永远收不到中断 | `patch_alsps_eint.py` |
| **驱动** | **实际硬件是 AP3220，不是 CM36686** | **`patch_alsps_ap3220.py`（根因修复）** |

最终确认：这块板子的光感芯片实际是 **AP3220**，与内核里写的 CM36686 不是同一颗。补丁新增了 AP3220 的驱动并关掉 CM36686 的误匹配，光感才真正开始出数。

> ⚠️ 说明：内核侧光感已经能读到正确的 lux 值；但 Android 8.1 框架层的
> `mUseSoftwareAutoBrightnessConfig=false`，系统级自动亮度策略不生效。
> 这是框架层限制，不是内核问题。

---

## 其他内核修复（非用户直接感知，但必需）

| 修复 | 补丁 | 说明 |
|---|---|---|
| 屏幕点亮 | `patch_lcm_stock.py` / `patch_lcm_init.py` / `patch_lcm_table.py` | 原厂 LCM 驱动 + 初始化序列 + 时序表 |
| ESD 恢复 | `patch_lcm_esd_off.py` | 关闭误触发的 ESD 检测，防止随机黑屏 |
| DSI 启动 | `patch_dsi_start.py` | 显示接口启动时序 |
| 显示调试 | `patch_lcm_debug.py` / `patch_disp_debug.py` | 调试节点（保留） |
| binder | `patch_binder_modern.py` | 补齐 hwbinder / vndbinder，Android 8.1 必需 |
| 视频编解码 | `patch_vcodec_clk.py` / `patch_vcodec_irq.py` | VDEC/VENC 时钟 + 中断重映射 |
| ramdisk | `patch_ramdisk.py` | adb / 调试属性 / 启动参数 |

---

## 已知问题

| 项 | 状态 | 说明 |
|---|---|---|
| **蓝牙** | ❌ 不可用 | 卡在内核 WMT 层 `mtk_wcn_wmt_func_off(WMTDRV_TYPE_BT)` 返回 FALSE，触发硬件复位死循环。需要 MT6630 芯片层资料。详见 `docs/蓝牙调查全记录.md` |
| **视频硬解** | ❌ 不可行 | MT8173 的 vcodec 与 Android 8.1 框架层不兼容。详见 `docs/视频硬解调查全记录.md` |
| **系统级自动亮度** | ⚠️ 框架限制 | 光感驱动已修好（能读到 lux），但 8.1 框架的自动亮度策略开关未启用 |

---

## Contribution

这个内核不是我一个人从零写的，它站在两拨人的肩膀上：

### 上游

- **[@Haoduo9](https://github.com/Haoduo9)** — `android_kernel_bbk_h3000s` 的作者。
  这棵 H3000S 移植树是整个工作的地基：MT8173 的板级支持、defconfig、DTB、
  LineageOS 15.1 的设备树适配，都来自他的仓库。没有这个移植树，S3 Pro 根本进不了 8.1。

- **LineageOS 团队** — 15.1 的框架层、HAL 接口、以及 MT8173 相关的基础适配。

### 本仓库新增

- **编译期补丁体系** — `scripts/build.sh` + `kernel-patches/`，
  把上述所有硬件修复做成可复现的源码补丁（不替换二进制、不 port boot.img）。
- **22 个内核补丁** — 覆盖显示、触摸、电池、传感器、光感、vcodec、binder、ramdisk。
- **调查记录** — `docs/` 下的蓝牙、视频硬解、自动亮度等完整排查过程，
  记录了什么可行、什么不可行、为什么。

### 如何贡献

如果你也在折腾 MT8173 / H3000S 或类似的 MTK 老设备：

- 发现补丁有误 → 提 Issue，附内核日志（`dmesg`）
- 有新硬件适配 → PR 到 `kernel-patches/`，每个补丁一个脚本
- 蓝牙有进展（尤其是 MT6630 的 WMT 协议）→ 非常欢迎，这是目前唯一的大坑

---

## 构建方式

全部在 GitHub Actions 上云编译，不做本地二进制修改：

```bash
# 触发 build-kernel workflow
# 输入 → 输出 boot.img + Image.gz + build-info.txt + build.log
```

关键环境变量：

| 变量 | 默认 | 作用 |
|---|---|---|
| `S3PRO_BT_BLUEZ` | `0` | 蓝牙 STP 模式（0=MTK 私有帧，1=BlueZ/H4） |

---

## 校验

| 文件 | SHA-256 |
|---|---|
| `boot.img` | `032d5214fe3bd202851335a908c6b86241bc37c318fdadc936c1935b113d5e46` |
| `Image.gz` | `32814045d6593aa95ccf47bef4fd335ae58d054c864039967ccf49d14954d59e` |

---

*内核 3.18.119+ · gcc 4.9 · 2026-11-20*
