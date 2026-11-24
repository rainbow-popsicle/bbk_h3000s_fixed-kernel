# 自动亮度（用户态实现）

## 为什么需要它

本 ROM 的框架自动亮度是坏的，两条路都堵死（都有实测证据）：

| 问题 | 证据 |
|---|---|
| 框架不订阅光感 | `dumpsys display` → `mUseSoftwareAutoBrightnessConfig=false`；`Active sensors` 里只有 ACCELEROMETER |
| 亮度曲线数组被移植丢了 | 8.1 的 `framework-res.apk` 里 `config_autoBrightnessLevels` / `LcdBacklightValues` 都是 `size=0`；原厂 7.0 里分别是 **17 / 18** 项真实数值 |
| RRO overlay 补不回来 | 用原厂 17/18 项做了 overlay，装进 `/system/app`、`cmd overlay enable` 成功、idmap 也生成了，但那个标志**仍是 false** |
| 改 Settings 无效 | `settings put system screen_brightness 50/200`，实际背光一直停在 102 |

所以只能用户态直接写背光 sysfs。

## 光感驱动本身是好的

内核侧修好后（AP3220 重映射），实测：

```
基线 0x0049(73)  →  手电筒 0xEE56(61,014)  →  移开 0x0050(80)
```

驱动细节见 `docs/视频硬解调查全记录.md` 之外的 `patch_alsps_*.py` 注释，
以及 `s3pro-kernel/scripts/patch_alsps_ap3220.py` 顶部说明。

## 装了什么

| 文件 | 权限 | 作用 |
|---|---|---|
| `/system/bin/s3pro-alsd` | `755 root:shell` | 守护脚本（shell，无需编译） |
| `/system/etc/init/s3pro-alsd.rc` | `644 root:root` | 开机自启（Android 8 自动加载该目录） |
| `/system/app/S3ProAutoBrightness/` | — | RRO overlay（补亮度数组，当前无效但无害，可删） |

启动条件：`sys.boot_completed=1`（`class late_start`，`user root`）。
本机 SELinux = Permissive，所以不需要 sepolicy。

## 行为

```
熄屏                → 什么都不做
亮屏瞬间            → 等 2.5s → 读一次光感 → 按原厂曲线 → 写一次
亮起后 8 秒内       → 若被框架覆盖（实测会）则补回
8 秒后              → 彻底撒手，直到下次亮屏
```

一次亮屏周期内最多写 9 次 sysfs（1 次 + 8 秒窗口内的补写），之后静默。

## 曲线（原厂 7.0 原值）

从原厂 `system.img` 抽出的 `framework-res.apk` 里 dump 得到：

```
levels(17)    = 128 256 384 512 640 768 896 1024 2048 4096 6144 8192
                10240 12288 14336 16384 18432
backlight(18) = 8 64 98 104 110 116 122 128 134 182 255 255 255 255 255 255 255 255
```

脚本里只实现到 4096（再往上都是 255，合并了）。

## 安装 / 卸载 / 调参

```bash
# 安装（脚本必须是 LF 换行 + 755）
adb root
adb push overlay/autobrightness/files/s3pro-alsd     /data/local/tmp/alsd_bin
adb push overlay/autobrightness/files/s3pro-alsd.rc  /data/local/tmp/alsd_rc
adb shell 'mount -o rw,remount /system
           tr -d "\r" < /data/local/tmp/alsd_bin > /system/bin/s3pro-alsd
           chmod 755 /system/bin/s3pro-alsd
           tr -d "\r" < /data/local/tmp/alsd_rc > /system/etc/init/s3pro-alsd.rc
           chmod 644 /system/etc/init/s3pro-alsd.rc
           mount -o ro,remount /system'
adb reboot

# 卸载
adb root
adb shell 'mount -o rw,remount /system
           rm -f /system/bin/s3pro-alsd /system/etc/init/s3pro-alsd.rc
           mount -o ro,remount /system'
adb reboot

# 改完曲线后只重启服务（不用重启机器）
adb shell 'setprop ctl.stop s3pro-alsd; sleep 1; setprop ctl.start s3pro-alsd'

# 单点自测：给一个 raw 值，看映射出的背光
adb shell '/system/bin/s3pro-alsd map 5000'

# 看日志（只记实际下发的值，最多 200 行）
adb shell 'cat /data/local/tmp/s3pro-alsd.log'
```

## 注意

- 这是**绕过框架的 workaround**，不是正规修复。正规做法是改 `services.jar`
  里 `DisplayPowerController` 的判断（需要反编译 dex + 平台签名），工作量和风险都大得多。
- 只在亮屏那一刻调一次，所以**屏幕亮着期间光照大幅变化不会跟**。这是刻意的取舍
  （避免和框架抢背光导致闪烁）。
- 如果哪天框架修好了（`mUseSoftwareAutoBrightnessConfig=true`），把这套删掉即可。
