# LOS14.1 补充：1. pio 89 电平  2. regulator_get 的 dev 指针来源

## 1. pio 89（gpio_lcd_id）电平 —— 决定 23 项还是 109 项表

### 软件读取尝试（都失败，如实记录）
- `/sys/kernel/debug/gpio`：pio89 未被 request（我们的内核不用这个脚），读不到
- `/sys/class/gpio/export`：内核未编 GPIO sysfs，`No such file or directory`
- `g_regr:0x10005100+`：ioremap 每次返回不同 VA（`0x...cc0100`/`0x...cc2108`），读数 `0x7edd` 重复不可信
- ramdisk 解压后搜 `lcm_vendor_id`/`lcd_id`：无任何覆盖
- **结论：pio89 的实际电平是硬件上拉/下拉决定的，软件侧读不到，需原理图或万用表**

### 但可以从反汇编确定它的作用链路（完整）

`lcm_init` @0x458344 的选表逻辑（已逐指令确认）：
```
458360 ldr w1,[x19,#0xd8]         ; vendor id (cmdline lcm_vendor_id, 本boot无→0)
458368 ldr w0,[x19,#0xd8]; cmp w0,#1
458370 b.eq 0x4583c0              ; id==1 → 291项表
458374 ldr w0,[x19,#0xdc]         ; ★ GPIO 号 = pio89 (由 lcm_get_gpio_infor 从DTB填入)
458378 bl mt_set_gpio_mode(w0)    ; 设为 GPIO 模式
45837c bl mt_get_gpio_in(w0)      ; ★ 读 pio89 电平 → w0
458380 mov w19,w0
458394 cbz w19, 0x4583e0          ; ★ 电平==0 → 23项表 @0xf0e988
458398 cmp w19,#1
45839c b.ne 0x458400              ; 其它 → 报错不推表
4583a0 电平==1 → 109项表 @0xf0f000
```

- 电平 0 → **23 项表**（首项 `FF{AA55A580}` = BOE 表，我们一直在用这张）
- 电平 1 → **109 项表**（@0xf0f000，首项 `F0{55AA520800}`，末段 11/DELAY120/29/DELAY40/END）

### 间接推断
这颗 ROM 是功能全好的量产包，开机 init 必须成功推表（否则黑屏）。
两条路径都合法，但从原厂旧 boot.img 反汇编提取的、且 LK 也在用的是 **23 项 BOE 表**；
两张表是同一块屏的不同配置（109 项是 23 项的超集扩展）。
**但我们无法 100% 断定 pio89==0**——这是目前唯一无法用软件确认的硬件事实。

---

## 2. regulator_get 的 dev 指针来源（完整确认）

### 函数入口是 0x458a28（不是之前说的 0x458a30，我漏了 prologue）

```
0x458a28: stp x29,x30,[sp,#-0x20]!     ← 函数入口
0x458a2c: adrp x1,0xb07000
0x458a30: mov w2,#0x8f
0x458a34: mov x29,sp
0x458a38: stp x19,x20,[sp,#0x10]
0x458a3c: mov x20,x0                   ← ★ dev = 第一个参数 x0
0x458a40: adrp x0,0xd79000
0x458a44: add x1,x1,#0x8a8
0x458a48: add x0,x0,#0xf88
0x458a4c: bl printk                    ; log line 143
0x458a50: mov x0,x20                   ; dev 传入
0x458a54: bl 0x45890c                  ; lcm_get_gpio_infor(dev) 读 DTB GPIO
0x458a58: printk("LCM: lcm_get_vgp_supply is going")
0x458a64: adrp x1,0xd13000
0x458a68: mov x0,x20                   ; ★ dev 作为 regulator_get 第1参数
0x458a6c: add x1,x1,#0x88              ; "reg-lcm-1v8"
0x458a70: bl 0x34e568                  ; ★★ regulator_get(dev, "reg-lcm-1v8")
0x458a74: cmn x0,#1,lsl #12            ; IS_ERR
0x458a78: mov x19,x0
0x458a7c: b.hi 0x458ad0                ; 失败分支
0x458a80: printk("LCM: lcm get supply ok.")
0x458a90: mov x0,x19
0x458a94: bl 0x34a364                  ; regulator_set_voltage(1800000,1800000)
0x458aa8: str x19,[x20,#0x830]         ; 存 regulator 指针
0x458aec... ret
```

### dev 指针来源结论

- **dev = probe 回调的第一个参数 `x0`**（`mov x20,x0` @0x458a3c）
- 这是 Linux platform driver 的 `probe(struct platform_device *pdev)` 回调，
  **dev 由内核在 DT 设备匹配时传入**，对应 DTB 的 `/soc/lcm` 节点
- 佐证：`lcm_get_gpio_infor`(0x45890c) 里 `ldr x0,[x0,#0x218]` —— 从 `platform_device*` 偏移 0x218
  取 `struct device*`（`pdev->dev`），再传给 `of_get_named_gpio`(0x33bc24) 读 DTB 属性
- **不是全局变量、不是硬编码**——是标准的内核 probe 参数传递

### 完整 dev 链路
```
内核 DT 匹配 /soc/lcm 节点
  → 调用 platform_driver.probe(pdev)        [pdev 由内核创建]
  → probe 第一个参数 x0 = pdev
  → x20 = pdev
  → regulator_get(&pdev->dev, "reg-lcm-1v8") [0x458a68-0x458a70]
  → regulator_set_voltage(..., 1800000, 1800000)
  → 存到全局 [0x122f830] 供 enable/disable 使用
```

---

## 一个重要观察（和我们的差异）

`lcm_get_gpio_infor`(0x45890c) 读的 GPIO 属性（从字符串常量确认）：
- `gpio_lcd_id`(0xf60) → 存 [0x81c]
- `gpio_lcd_rst`(0xfa0) → 存 [0x820]
- 还有 enp/enn/1v8 各一路（0xfe0/0x20 等）

这些GPIO号存到全局区 `0x122f81c/820/824/828`，**resume/init 用的是运行时值**（`[x19+0xe0/0xe4/0xe8]` 等），
不是硬编码。我们内核的做法（编译期 DTS + gpio_request）可能拿到的引脚映射就不同。
