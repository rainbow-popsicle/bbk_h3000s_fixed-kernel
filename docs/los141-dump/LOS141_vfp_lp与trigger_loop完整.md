# LOS14.1 补充：vertical_vfp_lp 实际值 + _cmdq_start_trigger_loop 完整反汇编

## 1. vertical_vfp_lp 实际值 = 0（不是 200）

`get_params`@0x458674 两个分支的**所有 str 写入**里，**没有任何对 0x21c 的写入**。

- 0x21c = `vertical_frontporch_for_low_power`（vertical_vfp_lp）
  （字段顺序：0x210=v_sync, 0x214=v_bp, 0x218=v_fp, **0x21c=v_fp_lp**, 0x220=v_act）
- 函数开头 `bl 0x306f00`（memset params, 0, 0x408）把整个结构体清零
- → **vertical_vfp_lp 保持默认的 0**

**重要差异**：我们的驱动里写了 `params->dsi.vertical_vfp_lp = 200`（注释"For dynamic fps, idle LP mode set fps to 55Hz"），
而功能全好的 LOS14.1 内核**根本不设这个字段（=0）**。

> 注意：设备上观测到的 `vfp=0xc8(200)` 是 `DSI_VFP_NL` 硬件寄存器值，
> 由 ddp_dsi_config 配置，**不是** params->vertical_vfp_lp 的直接映射——两者不能等同。

## 2. _cmdq_start_trigger_loop @0xffffffc0004a1224 完整反汇编（含所有分支）

之前只 dump 到 0x4a12b8 的 ret（38 条），**漏了 0x4a12bc-0x4a1300 的分支逻辑**。完整如下：

```
4a1224 stp x29,x30,[sp,#-0x20]!
4a1228 mov x29,sp
4a122c stp x19,x20,[sp,#0x10]
4a1230 adrp x19, 0x1265000
4a1234 add x0, x19, #0x9e8
4a1238 ldr w0, [x0, #0x1e0]           ; [global+0x1e0] = CMDQ loop 标志
4a123c cbz w0, 0x4a12c4                ; 标志=0 → 走 C4 分支
; --- 标志!=0 主路径 ---
4a1240 add x20, x19, #0x9e8
4a1244 ldr x0, [x20, #0x110]
4a1248 bl 0x42fac0                     ; (A)
4a124c ldr w0, [x20, #0x1e0]
4a1250 cbz w0, 0x4a12bc                ; 标志变0 → 走 BC 分支
4a1254 add x20, x19, #0x9e8
4a1258 ldr x0, [x20, #0x110]
4a125c bl 0x42fa24                     ; (B)
4a1260 ldr w0, [x20, #0x1e0]
4a1264 cbnz w0, 0x4a126c               ; 标志非0 → 跳过
4a1268 bl 0x49de98                     ; 标志变0时的清理
4a126c add x0, x19, #0x9e8
4a1270 ldr x0, [x0, #0x100]
4a1274 bl 0x4aaa60                     ; (C) 返回 w0
4a1278 cbz w0, 0x4a12d4                ; 返回0 → 走 D4 分支
4a127c add x0, x19, #0x9e8
4a1280 ldr w0, [x0, #0x1e0]
4a1284 cbz w0, 0x4a12cc                ; 标志=0 → 走 CC 分支
4a1288 add x19, x19, #0x9e8
4a128c ldr w0, [x19, #0x44]
4a1290 sub w0, w0, #2
4a1294 tst w0, #0xfffffffd
4a1298 b.ne 0x4a12a4
4a129c mov w0, #0x50
4a12a0 bl 0x43259c                     ; MMProfileEnableEvent(0x50)
4a12a4 adrp x0, 0xd22000; add x0,#0x150
4a12ac bl printk
4a12b0 ldp x19,x20,[sp,#0x10]
4a12b4 ldp x29,x30,[sp],#0x20
4a12b8 ret
; ===== 以下为之前漏掉的分支 =====
4a12bc bl 0x49de98                     ; 清理
4a12c0 b 0x4a1254                      ; 回到主路径
4a12c4 bl 0x49de98                     ; 清理(标志初始=0)
4a12c8 b 0x4a1240                      ; 进入主路径
4a12cc bl 0x49de98                     ; 清理
4a12d0 b 0x4a1288                      ; 回到主路径
; --- D4 分支: (C)返回0 时 ---
4a12d4 bl 0x4a1170                     ; (D) 检查
4a12d8 cbnz w0, 0x4a12f8               ; 非0 → F8
4a12dc mov w0, #0x192
4a12e0 bl 0x43259c                     ; MMProfile(0x192)
4a12e4 mov w0, #0x194
4a12e8 bl 0x43259c                     ; MMProfile(0x194)
4a12ec mov w0, #0xff02
4a12f0 bl 0x4977c0                     ; (E)
4a12f4 b 0x4a127c                      ; 回主路径
4a12f8 mov w0, #0x193
4a12fc bl 0x43259c                     ; MMProfile(0x193)
4a1300 b 0x4a12dc                      ; 回 12dc
; 0x4a1304 = 下一个函数入口(stp -0x40) → 函数到此结束
```

### 调用目标（地址→符号，用原厂索引+7 对照）
| 地址 | 符号 |
|---|---|
| 0x42fac0 / 0x42fa24 | cmdq 相关（start loop 的两次调用） |
| 0x49de98 | 清理/复位 helper（被 4 处分支调用） |
| 0x4aaa60 | 状态检查函数（返回 w0，为0走错误路径） |
| 0x43259c | MMProfileEnableEvent（事件 0x50/0x192/0x193/0x194） |
| 0x4977c0 | 错误上报（w0=0xff02） |
| 0x4a1170 | (C)==0 时的检查函数 |

### 逻辑总结
`_cmdq_start_trigger_loop` 检查全局 `[0x1265fe8+0x1e0]` 的 CMDQ loop 标志，
通过多次 cmdq API(0x42fac0/0x42fa24) 尝试启动 loop；若中间标志被清或状态检查失败，
走错误路径（0x4a12bc/cc/d4）——记录 MMProfile 事件 0x192/0x193/0x194 + 错误上报 0xff02。
这是一个**带重试和错误上报的 CMDQ loop 启动函数**。
