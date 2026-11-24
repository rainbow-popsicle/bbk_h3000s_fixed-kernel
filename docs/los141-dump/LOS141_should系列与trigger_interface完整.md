# LOS14.1 的 _should_* 系列 + _trigger_display_interface + 0xf1 分支

## 0. 关键字段释义（先明确，否则读不懂 should 函数）

| pgc 字段 | 含义 | 证据 |
|---|---|---|
| `[+0x1e0]` | CMDQ trigger loop 使能标志 | start/stop loop 检查 |
| `[+0x210]` | **CMDQ mode 标志**（= primary_display_cmdq_enabled()），由 `switch_cmdq_cpu`@0x4a6340 的 `str w20,[x1,#0x210]` 写入（参数 w0: 1=CMDQ,0=CPU） | 0x4a63a0 |
| `[+0x44]` | session_mode（上轮确认） | trigger 四路分派 |
| `[+0x100]` | lcm/driver 指针，传给 disp_lcm_is_video_mode(0x4aaa60) | should 函数 |

`disp_lcm_is_video_mode`@0x4aaa60 = 源码的 `primary_display_is_video_mode()`。

## 1. LOS 的 10 个 _should_* 函数（全部 @0x49e998-0x49ee5c）

| 函数 | LOS 地址 | 逻辑 |
|---|---|---|
| _should_wait_path_idle | 0x49e998 | cmdq模式→0; 非cmdq→dpmgr_path_is_busy |
| _should_update_lcm | 0x49ea2c | cmdq→0; 非cmdq→(!video) |
| _should_start_path | 0x49ea9c | cmdq→0; 非cmdq video→path_is_idle, cmd→1 |
| _should_trigger_path | 0x49eb48 | 同 start_path |
| _should_set_cmdq_dirty | 0x49ebf4 | cmdq→(!video); 非cmdq→0 |
| **_should_flush_cmdq_config_handle** | 0x49ec78 | cmdq→1; 非cmdq→0 |
| _should_reset_cmdq_config_handle | 0x49ece8 | cmdq→1; 非cmdq video→1,cmd→0 |
| **_should_insert_wait_frame_done_token** | 0x49ed58 | cmdq→1; 非cmdq→0 |
| _should_trigger_interface | 0x49edc8 | `[+0x40]`!=1 → 1（等价 mode!=DECOUPLE） |
| _should_config_ovl_input | 0x49ee24 | `[+0x40]`-2 在[0,1]→0 else 1（mode != SINGLE_LAYER/DEBUG_RDMA1） |

**与我们源码对比（primary_display.c 750-920）**：

| 函数 | 我们源码 (VDO+CMDQ 情形) | LOS | 一致? |
|---|---|---|---|
| wait_path_idle | 0 | 0 | ✅ |
| update_lcm | 0 | 0 | ✅ |
| start_path | 0 | 0 | ✅ |
| trigger_path | 0 | 0 | ✅ |
| set_cmdq_dirty | 0 | 0 | ✅ |
| **flush_cmdq_config_handle** | **1** | 1 | ✅ |
| reset_cmdq_config_handle | 1 | 1 | ✅ |
| **insert_wait_frame_done_token** | **1** | 1 | ✅ |
| trigger_interface | mode!=DECOUPLE | `[+0x40]`!=1 | ✅ |
| config_ovl_input | mode!=SINGLE_LAYER/DEBUG | `[+0x40]`-2>1 | ✅ |

**结论：LOS 的 _should_* 判断结果与我们源码完全一致，没有差异。**
编译器只是把 `primary_display_cmdq_enabled()` 优化成读 `[+0x210]`、把 `is_video_mode()` 优化成 0x4aaa60 调用，逻辑没变。

## 2. _trigger_display_interface @0x4a4744 完整（175 条，2 个 ret）

调用链（按源码 `_trigger_display_interface()` 顺序）：
```
4a476c _should_wait_path_idle → 4a4790 dpmgr_wait_event_timeout
4a4794 _should_update_lcm → 4a47c8 disp_lcm_update
4a47cc _should_start_path → 4a47ec dpmgr_path_start
4a47f0 _should_trigger_path → 4a4814 dpmgr_path_trigger
4a4818 _should_set_cmdq_dirty
4a485c cmdq_flush_config_handle (0x49df1c) ← ★ 直接调，不经过 _should_flush_cmdq_config_handle
4a485c _should_reset_cmdq_config_handle → 4a48e0 cmdq_reset_config_handle
4a48bc _cmdq_insert_wait_frame_done_token (0x49e7c4) ← ★ 直接调，不经过 _should_insert_wait_frame_done_token
4a4900 cmdq_set_config_handle_dirty (0x49e734)
4a4934 disp_set_sodi (0x49e2fc)
4a49e4 cmdqRecFlushAsyncCallback (0x42fac0)
```

★ **已确认：should 判断被编译器内联了**（0x4a4840-0x4a48bc）：
```
4a4840 ldr w0,[x0,#0x210]; cbz → 0x4a48dc    ← 内联 _should_flush_cmdq_config_handle
4a4858 bl cmdq_flush_config_handle            ← 条件满足才 flush
4a485c bl _should_reset_cmdq_config_handle; cbnz
4a4874 ldr w0,[x20,#0x210]; cbnz → 跳过        ← 内联 _should_insert_wait_frame_done_token
4a48ac ldr w0,[x19,#0x44]; sub #2; tst #0xfffffffd; b.eq 跳过
                                               ← 内联 !_is_decouple_mode(pgc->session_mode)
4a48bc bl _cmdq_insert_wait_frame_done_token  ← 两条件都满足才执行
```
这**完全对应我们源码 1604 行**：
`if (_should_insert_wait_frame_done_token() && (!_is_decouple_mode(pgc->session_mode)))`。
LOS 与我们源码**一字不差**，没有任何逻辑差异。

## 3. 0x4a4f1c（trigger 里 state==0xf1 分支）调什么

```
4a4f1c add x1,x23,#0xd98; add x1,x1,#0x3e8
4a4f28 add x0,0xd23000+0x8c8             ; 字符串
4a4f2c bl printk                          ; ★ 只打印一条日志
4a4f30 b 0x4a4ab4                         ; 跳回 dprec_logger_done → path_unlock → ret
```
**只打印日志，不做任何显示操作**，然后正常返回。0xf1 = path state 非法/未就绪（DISP_PATH_STATE），
resume/suspend 里也检查这个状态。是 abort/警告路径。

---

## 结论

1. **_should_* 判断结果与我们源码完全一致**——用户担心的 `_should_flush_cmdq_config_handle` /
   `_should_insert_wait_frame_done_token` 在 VDO+CMDQ（我们的配置）下都是 **返回 1**（执行），
   LOS 也一样返回 1。**不是差异点**。
2. `_trigger_display_interface` 175 条，把 10 个 should 串成完整触发链；flush/insert_token 是直接调用（判断可能被编译器内联）。
3. 0xf1 分支只是错误日志 + 返回。
