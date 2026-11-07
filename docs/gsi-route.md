# 路线 A：把 S3 Pro 变成原生安卓 9（GSI）

> ⚠️ **对这台机器不适用**：S3 Pro 实测没有 `vendor` 分区，不是 Treble 机型，GSI 刷不进去。
> 本文保留下来，一是说明"为什么不行"，二是万一你以后拿到别的 MTK 平板可以重用。

> 这条路**不需要编译内核**。GSI 只替换 `system` 分区，内核在 `boot` 分区里原封不动。
> 只有确认 GSI 能被内核"喂饱"之后，才有必要考虑路线 B（自编内核）。

---

## 0. 一句话流程

```
确认 Treble → 备份 system/vendor/boot → 刷 arm64 A-only 的 AOSP 9 GSI → 清 data → 开机
```

任何一步失败，回写三个备份分区就能回到原厂状态。

---

## 1. 前置条件（逐条打勾）

- [ ] **已解锁 BL**（mtkclient 在 BROM 模式下解锁；解锁后开机通常会出现 `unlocked` 小字）
- [ ] **有完整分区备份**：`python mtk rl backup_dir`（或至少 `boot` / `system` / `vendor` / `userdata`）
- [ ] **确认是 Treble 机型**：dump 里有 `vendor` 分区（≥1MB）
- [ ] 确认分区方案：`boot_a`/`boot_b` 都没有 → **A-only**
- [ ] 手上至少有 **20GB 空闲磁盘**放镜像和备份

先用 [analyze_dump.py](../scripts/analyze_dump.py) 一次性确认后两条：

```bash
python scripts/analyze_dump.py backup_dir/
```

看到 `vendor 分区 : 有` 和 `分区方案 : A-only` 才继续。

---

## 2. 判断 Treble 的三种办法

| 办法 | 命令 | 判据 |
| --- | --- | --- |
| dump 看分区（最可靠，不需要开机） | `python scripts/analyze_dump.py dump/` | 有 `vendor` 分区 |
| 系统里看属性 | `adb shell getprop ro.treble.enabled` | `true` |
| 看目录 | `adb shell ls /vendor` | 有内容（不是空目录/不存在） |

三条里有一条成立就可以试 GSI。**都没有 → 别刷 GSI**，走路线 B 或改原厂包。

---

## 3. 选镜像

| 项 | 怎么定 | 说明 |
| --- | --- | --- |
| 架构 | **arm64** | MT8176 是 64 位，S3 Pro 系统是 64 位 |
| 分区方案 | **A-only** | `getprop ro.boot.slot_suffix` 为空就是 A-only |
| Android 版本 | **9.0** | 与设备 vendor 的 API 级别对齐，跨版本（10/11）会引入更多 HAL 缺失 |
| 带不带 GApps | 先选**不带** | 先证明能开机；GApps 会额外占用 system 空间，容易刷不进 |

镜像来源（自行搜索当前可用的发布，这里不写死链接）：

- phhusson 的 `treble_experimentations` 发布页（AOSP 9 分支的 `arm64-aonly` 镜像）；
- ErfanGSIs 一类第三方 GSI 站点（质量参差，先用 AOSP 原版验证）。

**大小检查**：解压出来的 `system.img` 必须 **不大于你的 `system` 分区容量**。
超了有两种处理：换更小的 GSI，或者对镜像做 `e2fsck -f system.img && resize2fs system.img <更小容量>`。

---

## 4. 刷写：两条路，选一条

### 4a. fastboot（有 fastboot 就用它，最标准）

```bash
fastboot devices                     # 先确认能认到设备
fastboot flash system system.img
fastboot --disable-verity --disable-verification flash vbmeta vbmeta.img
fastboot -w                          # 清 data，第一次必须
fastboot reboot
```

注意：部分 MTK 平板的 bootloader 把 fastboot 命令裁得很狠，`flash system` 可能直接不支持——
那就换 4b。

### 4b. mtkclient（BROM 直写，几乎不会被裁）

```bash
python mtk w system system.img        # 大分区写入，10~20 分钟，别拔线
python mtk e userdata                 # 清数据
python mtk reset                      # 重启
```

- 写入过程屏幕是黑的、没有任何进度，靠电脑端日志确认；
- 中途断线不会立刻变砖（BROM 还能进），重来即可；
- 写入前确认镜像大小 ≤ 分区大小，否则会写坏 `system`。

---

## 5. 第一次开机

- 正常耗时 **3~10 分钟**，期间可能黑屏、自动重启一两次；
- 超过 **15 分钟**还停在 logo：按住电源键进 recovery 清一次 data，或再跑 `python mtk e userdata`；
- 开机后先去 `设置 → 关于` 确认版本、再测触摸/显示/WiFi/音频/相机，**一项一项记下来**。

### 验收清单

| 项 | 通过标准 |
| --- | --- |
| 触摸 | 全屏可点，无漂移 |
| 显示 | 分辨率/方向正确，亮度可调 |
| WiFi | 能扫描、能连、能上网 |
| 音频 | 扬声器、耳机、麦克风 |
| 相机 | 前后摄能出图 |
| 传感器 | 重力/光感（学习机可能本来就没有） |
| 睡眠唤醒 | 息屏后按电源键能亮 |

哪一项挂了，决定了你要不要回到路线 B——见下一节。

---

## 6. 常见故障对照表

| 现象 | 多半原因 | 处理 |
| --- | --- | --- |
| 卡 logo / 一直转圈 | data 没清干净，或 dm-verity 校验失败 | `fastboot -w` 或 `python mtk e userdata`；确认 vbmeta 已关校验 |
| 黑屏但能听到声音/能震动 | 显示通路（DPU/DSI 参数）不匹配 | GSI 修不了，回退原厂 system |
| 触摸完全失灵 | vendor 的触摸 HAL 依赖厂商内核里的触摸驱动/参数 | **典型的需要路线 B**（自编内核）或放弃 GSI |
| 亮度调节无效、屏幕偏色 | 面板参数在 vendor 里，GSI 覆盖不到 | 一般只能接受或回退 |
| WiFi / 蓝牙打不开 | 只在你换过内核后才出现：vendor 里预编译模块 vermagic 对不上 | 用原厂 `boot.img`；若必须自编，见 README 的 `CONFIG_LOCALVERSION` 对齐 |
| 无信号 / 基带异常 | radio/modem 分区与系统不匹配 | 回写 `modem`/`radio` 备份 |
| 刷完 system 后无法进系统也无法进 fastboot | 正常现象，BROM 还能进 | `python mtk w system backup/system.img` 回滚 |
| 开机提示"设备内部错误" | AVB 校验失败 | 重新关 vbmeta 校验，或回写 `vbmeta` 备份 |

**判断原则**：只要 **GSI 能起来、触摸能用**，剩下的问题基本都能在 system 层修；
如果 **起都起不来或触摸不可用**，那是内核/vendor 的边界，GSI 这条路到头了。

---

## 7. 回滚（开始之前先练一遍）

```bash
# 回到原厂（BROM 模式，逐条执行）
python mtk w boot   backup/boot.img
python mtk w system backup/system.img
python mtk w vendor backup/vendor.img
python mtk e userdata
python mtk reset
```

G 之前把这三条命令**写在纸上**。GSI 玩坏了不可怕，三条命令就能回来；可怕的是没备份。

---

## 8. 什么时候必须回到路线 B（自编内核）

只有这几种情况值得动内核：

1. GSI 下**触摸/显示异常**，且确认是内核驱动或 DTS 参数问题（需要看 `dmesg`）；
2. 需要内核本身的能力：`f2fs`、`docker`、外接存储、自定义调频；
3. 你要长期维护这套系统，需要一个能持续编译的内核。

其余情况（应用兼容、权限、GApps、DPI、导航栏）**都在 system 层解决，和内核无关**。

回到 [README](../README.md) 的自编内核部分，或直接看 [build.sh](../scripts/build.sh) 的用法。
