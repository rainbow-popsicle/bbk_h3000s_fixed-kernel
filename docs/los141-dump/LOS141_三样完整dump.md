# LOS14.1 正常内核三样东西完整 dump

来源：`lineage-14.1-20260822-UNOFFICIAL-h3000s/boot.img`（3.18.35，2021，功能全好）
内核 Image：`C:/Users/momei/AppData/Local/Temp/los141_parts/Image`
VA→file：`fileoff = VA - 0xffffffc000080000`

---

## ① 291 项初始化表（完整）

已导出为 C 数组文件：**`los141_table291.h`**（297 行，291 项全在）。

- 位置：VA `0xffffffc000f097b0`，每项 0x48 字节
- 编码：`cmd@+0, count@+4, para@+5`；`0xFC=DELAY(ms)`、`0xFD=END`、`0xFB` 也是特殊标记
- 首项 `{0xB0,1,{0x05}}`，尾段 `0x11 / DELAY120 / 0x29 / DELAY40 / END`
- **注意**：这颗内核用 `0xFC/0xFD` 做 delay/end，而我们的驱动 `REGFLAG_DELAY=0xFE`、`REGFLAG_END_OF_TABLE=0xFF`。移植时我已转成符号名，但编译器会用我们驱动的宏值，需要根据实际宏调整。

对比：之前一直在用的 23 项 BOE 表（@0xf0e988）首项 `{0xFF,4,{0xAA,0x55,0xA5,0x80}}`，
和 291 项表**内容完全不同**——291 项是另一套更完整的初始化序列。

---

## ② vendor id 初始化代码（完整链路）

vendor id 存在全局区 `[x19+0xd8]`（x19=0xffffffc00122f740）。

### 解析函数 `get_lcm_vendor_id` @0x458608（17 条）

```
458608 adrp x0, 0xfa6000
45860c adrp x1, 0xd12000
458610 stp  x29,x30,[sp,#-0x20]!
458614 add  x1,x1,#0xc90          ; 字符串 "lcm_vendor_id="
458618 mov  x29,sp
45861c ldr  x0,[x0,#0x30]         ; g_cmdline (全局命令行指针)
458620 str  x19,[sp,#0x10]
458624 bl   0x310b70              ; ★ cmdline_find / strstr(cmdline,"lcm_vendor_id=")
458628 cbz  x0, 0x458654          ; 没找到 → 返回 0
45862c ldrb w19,[x0,#0xe]         ; ★ 取匹配串第 14 字节 = 'lcm_vendor_id=' 后的字符
458630 adrp x0,0xd12000
458634 add  x0,x0,#0xcc8          ; "hgc lcm_id = %d"
458638 sub  w19,w19,#0x30         ; ★ ASCII 字符 - '0' → 数字 id
45863c mov  w1,w19
458640 bl   printk                ; 打印 "hgc lcm_id = %d"
458644 mov  w0,w19                ; 返回 id
458648 ldr  x19,[sp,#0x10]
45864c ldp  x29,x30,[sp],#0x20
458650 ret
```

字符串常量（已验证）：
- @0xd12c90 = `"lcm_vendor_id="`（长度 14，所以之后第 14 字符 `+0xe` 就是 id）
- @0xd12cc8 = `"hgc lcm_id = %d\n"`
- @0xd12ca0 = `"failed to get lcm id from cmd line\n"`

### 写入全局 `get_params` 里 @0x458720-0x45873c

```
458720 bl  0x458608              ; 调 get_lcm_vendor_id()
458724 mov w4,w0                 ; 返回值 = id
458730 mov w2,#0x49e             ; 行号 1182
458734 mov w3,w4
458738 str w4,[x21,#0xd8]        ; ★ 存到全局 vendor id
45873c bl  printk                ; "hgc lcm_id = %d"
```

本 boot.img 的 cmdline：`bootopt=64S3,32N2,64N2 androidboot.selinux=permissive buildvariant=userdebug`
——**没有 `lcm_vendor_id=`** → 函数走 cbz 分支返回 **0** → 全局 vendor id = **0**。

### vendor id 的使用（init 里的表选择）

```
init @0x458368: ldr w0,[x19,#0xd8]; cmp w0,#1
  ==1 → 291 项表
  !=1 → 读 GPIO lcd_id([x19+0xdc]): 电平0→23项表, 电平1→109项表
```
get_params 里：
```
  id==0 → timing 分支B (v_bp40/v_fp40/h_sync4/h_bp200/h_fp200, ssc=500)
  id!=0 → timing 分支A (v_sync12/v_fp20/h_sync24/h_bp80/h_fp80, ssc=425)
```

---

## ③ lcm_vgp_supply_enable 实现（完整）

函数 @0x458840（含 cbnz 分支）：

```
458840 stp  x29,x30,[sp,#-0x10]!
458844 adrp x0,0xd12000
458848 mov  x29,sp
45884c add  x0,x0,#0xe98         ; "LCM: lcm_vgp_supply_enable"
458850 bl   printk
458854 adrp x1,0x122f000
458858 mov  w0,#0
45885c ldr  x1,[x1,#0x830]        ; ★ 取全局 vgp regulator 指针
458860 cbnz x1, 0x45886c          ; regulator 存在 → enable
458864 ldp  x29,x30,[sp],#0x10
458868 ret
; ---- cbnz 分支 ----
45886c bl   0xa71928              ; ★ regulator_enable(vgp_regulator)
458870 b    0x458864              ; 返回
```

`[0x122f830]` 的 regulator 指针在配套的 `lcm_vgp_supply_disable`/初始化路径里通过
`regulator_get` 获得（同文件 0x458874 附近是 disable 的对称实现，检查电压=1800000）。

**关键**：resume_power 里 `bl 0x458840`（开 VGP regulator）+ mdelay(20ms)——
电源是通过 **regulator 框架**打开 1.8V（VGP），不是 GPIO 直接拉。这和我们内核的
`lcm_set_gpio_output(GPIO_LCD_1V8_EN, 1)` 是**不同的供电机制**。

---

## 三样东西与我们内核的差异总结

| 项 | 正常内核 | 我们 |
|---|---|---|
| 291项表 | 存在，vendor id==1 时用 | 没有，只有 23 项 BOE 表 |
| vendor id | cmdline `lcm_vendor_id=` 动态读，决定表和 timing | 完全没有，固定表A |
| VGP 供电 | regulator_enable + 20ms | GPIO 拉 1V8 + 12ms |

如果你说"三件事"还有第 2、第 3 件（我这条消息只收到了"1."开头的 dump 三样东西），
把剩下的要求发给我，我继续做。
