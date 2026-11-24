# systemfix/ —— 系统层修复的"事实来源"

这个目录是整个系统层修复的**唯一事实来源**。重刷 ROM 之后，
只要有这个目录，就能重新生成一模一样的修复包。

```
systemfix/
├── README.md        本文件（格式说明）
├── manifest.txt     要下发哪些文件（权限/大小/MD5/路径）
├── props.txt        要改哪些属性（文件/键/值）
└── files/           实际文件，按 /system 的目录结构存放
    ├── build.prop          ← 注意：这里不需要，属性走 props.txt
    ├── etc/timesync.sh
    └── vendor/lib/...
```

---

## manifest.txt 格式

一行一条，`#` 开头是注释：

```
<权限> <字节数> <MD5> <相对 /system 的路径>
```

示例：

```
755 944 bd18abc35b5ccb6b0aee44bdf68bec37 etc/timesync.sh
644 211452 <32 位 md5>  vendor/lib/libstagefrighthw.so
```

要点：

- **权限是八进制**：普通库 `644`，`bin/hw` 下的可执行服务 `755`。
  脚本按 `int(mode, 8)` 解析——如果当十进制会得到错误的 `chmod`。
- **MD5 必须是 32 位小写十六进制**。打包时会逐字节校验，不一致就拒绝打包。
- **路径相对 `/system`**：`vendor/lib/x.so` 表示 `/system/vendor/lib/x.so`。
  注意本机 `/vendor` 是 `/system/vendor` 的符号链接，两种写法等价，
  但清单统一用 `/system` 视角。
- 文件必须真实存在于 `files/<同样的路径>`，否则 `--pack` 报错。

## props.txt 格式

```
<属性文件相对 /system 的路径> <键> <值>
```

示例：

```
build.prop ro.build.fingerprint EEBBK/lineage_h3000s/h3000s:8.1.0/...:userdebug/test-keys
build.prop qemu.hw.mainkeys 1
```

要点：

- 第一列**不加** `system/` 前缀：`/system/build.prop` 就写 `build.prop`。
- 值里可以包含 `/` 和 `:`（安装脚本用 `|` 作 sed 分隔符），不能包含空格。
- 键已存在则替换全部同名行；不存在则追加。其余属性一律不动。

---

## 怎么维护

```bash
# 从设备重新采集清单里的文件（会覆盖 files/）
python scripts/pack_systemfix.py --stage --adb <adb路径>

# 校验 files/ 与 manifest 是否一致
python scripts/pack_systemfix.py --verify

# 生成 TWRP 可刷包
python scripts/pack_systemfix.py --pack
```

**纪律**：只把"已在设备上验证可用"的文件加进清单。
清单是唯一事实来源，多一个错文件就可能让下次刷机开不了机。

详见 `docs/固化成包.md`。
