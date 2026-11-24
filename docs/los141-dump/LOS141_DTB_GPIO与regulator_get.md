# LOS14.1 补充 dump：A. DTB lcm GPIO 定义；B. regulator_get 调用点

来源：`lineage-14.1-20260822-UNOFFICIAL-h3000s/boot.img`
DTB：`kernel.dtb`；内核 Image：`VA→file = VA - 0xffffffc000080000`

---

## A. DTB `/soc/lcm` 节点全部 GPIO 定义

```
compatible              = "mediatek,nt35523_fhd"
gpio_lcd_id             = <0x14 0x59 0x0>   → phandle 0x14(pio), pin 89(0x59), flags 0
gpio_lcd_enp            = <0x14 0x60 0x0>   → pin 96(0x60)  = VSP+ 升压使能
gpio_lcd_enn            = <0x14 0x62 0x0>   → pin 98(0x62)  = VSN- 升压使能
gpio_lcd_rst            = <0x14 0x7f 0x0>   → pin 127(0x7f) = 复位
reg-lcm-1v8-supply      = <0x42>            → regulator phandle（VGP 1.8V）
```

**注意**：这颗正常内核的 lcm 节点**只有 4 个 GPIO**（id/enp/enn/rst），
**没有 `gpio_lcm_rst_en_new`、没有 vsp_en/vsn_en/1v8_en** 这些属性名。

对比我们内核的 DTS（`xiaomi8176_tb_c9_n.dts`）：
```
gpio_lcm_vsp_en = <&pio 8 0>;
gpio_lcm_vsn_en = <&pio 13 0>;
gpio_lcm_rst_en = <&pio 127 0>;
gpio_lcm_rst_en_new = <&pio6397 14 0>;
gpio_lcm_1v8_en = <&pio 71 0>;
```
——**GPIO 定义完全不同**！正常内核用 enp=pio96 / enn=pio98，我们用 vsp=pio8 / vsn=pio13。
正常内核的 RST=pio127 和我们的 rst_en=pio127 一致。

---

## B. regulator_get 调用点（lcm_vgp_regulator_init @0x458a30）

### 完整反汇编

```
458a30 mov  w2,#0x8f
458a34 mov  x29,sp
458a38 stp  x19,x20,[sp,#0x10]
458a3c mov  x20,x0                    ; dev 指针
458a40 adrp x0,0xd79000
458a44 add  x1,x1,#0x8a8
458a48 add  x0,x0,#0xf88
458a4c bl   printk                    ; "[DDP/OVL]idle" 无关
458a50 mov  x0,x20
458a54 bl   0x45890c                  ; lcm_get_gpio_infor(dev) 读 DTB GPIO
458a58 adrp x0,0xd13000; add x0,#0x60
458a60 bl   printk                    ; "LCM: lcm_get_vgp_supply is going"
458a64 adrp x1,0xd13000
458a68 mov  x0,x20
458a6c add  x1,x1,#0x88               ; ★ 字符串 "reg-lcm-1v8"
458a70 bl   0x34e568                  ; ★★ regulator_get(dev, "reg-lcm-1v8")
458a74 cmn  x0,#1,lsl #12             ; IS_ERR 检查
458a78 mov  x19,x0
458a7c b.hi 0x458ad0                  ; 失败 → 报错分支
458a80 adrp x0,0xd13000; add x0,#0xb8
458a8c bl   printk                    ; "LCM: lcm get supply ok."
458a90 mov  x0,x19                    ; regulator
458a94 bl   0x34a364                  ; ★ regulator_set_voltage(reg, 1800000, 1800000)
458a98 mov  w1,w0
458a9c adrp x0,0xd13000; add x0,#0xd8
458aa4 bl   printk                    ; "lcm LDO voltage = %d in kernel stage"
458aa8 str  x19,[x20,#0x830]          ; ★★ 存 regulator 指针到全局（enable/disable 用）
458aac adrp x0,0xd12000; add x0,#0xe98
458ab4 bl   printk                    ; "LCM: lcm_vgp_supply_enable"
458ab8 ldr  x0,[x20,#0x830]
458abc cbnz x0,0x458aec
458ac0 mov  w0,#0
458ac4 ldp  ...
458acc ret
```

### 关键结论

1. **regulator 名字**：`regulator_get(dev, "reg-lcm-1v8")`（字符串 @0xd13088）
2. **电压**：`regulator_set_voltage(reg, 1800000, 1800000)` → 1.8V
   （日志 "LCM: set regulator voltage lcm_vgp voltage to 1.8V" @0xc92d98 佐证）
3. **全局指针**：regulator 存到 `[0x122f830]`，被 `lcm_vgp_supply_enable`(0x458840,
   调 `regulator_enable` @0xa71928) 和 `lcm_vgp_supply_disable`(0x458874, 调 `regulator_disable`) 使用
4. **配套 DTB**：`reg-lcm-1v8-supply = <0x42>` phandle 指向 PMIC 的 1.8V LDO

### lcm_vgp_supply_disable 完整（@0x458874）

```
458874 printk("LCM: lcm_vgp_supply_disable")
458890 ldr x0,[x19,#0x830]           ; regulator 指针
458894 cbz → 返回0
4588ac bl 0x349ea4                   ; regulator_is_enabled(reg)
4588bc printk("LCM: lcm query regulator enable status[0x%x]")
4588c8 bl 0x34b3b0                   ; (检查/读电压)
4588cc regulator_disable(reg)
4588ec printk("LCM: lcm regulator disable pass") / 失败日志
```

---

## 两个最重要的差异（对黑屏调查）

1. **GPIO 引脚完全不同**：正常内核供电用 `enp=pio96 / enn=pio98`；
   我们用 `vsp=pio8 / vsn=pio13`。这些是控制升压电路的实际引脚，接错就不供电。
2. **1.8V 供电机制不同**：正常内核用 **regulator**（DTB phandle 0x42 → PMIC LDO，set 1.8V）；
   我们用 GPIO(pio71) 直接拉。如果这块板子的 1.8V 实际由 PMIC LDO 而非 GPIO 控制，
   我们的 GPIO 拉高就是无效的。
