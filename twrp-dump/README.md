# s3pro-twrp-dump —— 只读取证模块（TWRP）

> 这个模块**只读**：读分区 → 写 /sdcard/s3pro/。不写分区、不格式化、不清数据。
> 刷完重启就回到原系统，删掉 `/sdcard/s3pro` 就等于没发生过。

---

## 先纠正一个常见误解

很多教程会说"root 之后 `zcat /proc/config.gz` 就能拿到内核配置"。
**你这台机器不行**，原因不是没权限，而是原厂编译内核时**没开 `CONFIG_IKCONFIG`**。

判断依据（在电脑上验证过）：把 `boot.img` 里的内核解压出来，搜 `IKCFG_ST` 标记，
**一个字节都没有**。没有这个标记，内核里就根本没存配置，`/proc/config.gz` 也就不存在——
root 也好、TWRP 也好，都变不出来。

还有一个坑要避开：**在 TWRP 里读 `/proc/config.gz` 读到的是 TWRP 自己的内核配置**，
那是 recovery 的 kernel，和你的系统内核完全是两码事。拿它去编译，只会白白黑屏。

那这个模块还有用吗？有——它能把你**设备真实的 boot 分区**读出来。
如果你设备真跑安卓 9，它和我手上那份 7.0 固件包可能压根不是同一个内核，
那才是该拿来分析的原始材料。

---

## 怎么用

1. 把 `s3pro-twrp-dump.zip` 传到手机上（U 盘 / SD 卡 / TWRP 的 MTP 都行）；
2. 进 TWRP → **Install** → 选这个 zip → 滑动确认；
3. 屏幕上的日志会打印每个分区的读取情况；
4. 重启，去 `/sdcard/s3pro/` 取文件。

如果 `/sdcard` 不可写，模块会自动退到 `/tmp/s3pro`，日志里会说明。

---

## 会产出什么

```
/sdcard/s3pro/
├── report.txt        这个模块最重要的产物：内核版本、分区清单、有没有内嵌 config
├── boot.img          ★ 关键，约 16MB
├── recovery.img      TWRP 自己的镜像（可选留着）
├── lk.bin            bootloader
├── preloader.bin
├── logo.bin / secro.img / tz.img / vbmeta.img / dtbo.img / misc.img（存在才读）
├── config.txt        仅当内核**真的**内嵌了 config 时才会出现
├── sha256.txt
└── listing.txt
```

每个文件最多读 64MB（`lk`/`preloader` 等限制 8MB），防止误读超大分区把存储塞满。

---

## 拿回来以后

在电脑上（需要 Python 3）：

```bash
python scripts/check-bootimg.ps1  boot.img      # Windows 自带 PowerShell 也能跑
python scripts/mine_kernel.py     boot.img      # 挖内核版本 / 屏驱动名 / 触摸型号
python scripts/analyze_dump.py    s3pro目录/
```

`report.txt` 里如果有这两行，就说明能直接开工：

```
内核版本: Linux version 3.18.xx ...
内嵌 config: 已提取 -> config.txt
```

如果写的是"内嵌 config: 没有"，那就是你这台机器的真实情况——
配置只能拿公开树的 defconfig 当基线，或者按内核里的二进制线索反推。

---

## 故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 刷入时报 `not found` 或闪退 | zip 被 Windows 改成了 CRLF 换行 | 用仓库里的 `python scripts/make_twrp_zip.py` 重新打包（它会强制 LF 并设可执行位） |
| 日志里全是"找不到分区节点" | 这台机器的 by-name 路径和常见 MTK 不同 | 在 TWRP 的 Terminal 里跑 `ls /dev/block/platform`，把结果告诉我 |
| 只读出了 boot，其他都跳过 | 那些分区不存在 | 正常，不影响分析 |
| `/sdcard` 里什么都没有 | 存储没挂载 | TWRP → Mount → 勾上 Data / Internal Storage 再刷一次 |

---

## 自己重新打包

```bash
python scripts/make_twrp_zip.py            # 生成 out/s3pro-twrp-dump.zip
python tests/test_twrp_zip.py              # 打包器的自测（11 项断言）
```

打包脚本会自检：两个必要条目是否齐全、`update-binary` 是不是 LF 换行、
有没有可执行位。这三样任何一项不对，recovery 里都只会给你一句看不懂的报错。
