import io

# ---------------------------------------------------------------- 教程：改模板与刷机段落
P = "docs/新手教程-GitHub编译内核.md"
t = open(P, encoding="utf-8", newline="").read()

# 1) 顶部加"你已经刷了 8.1，所以流程不同"的提示
head_anchor = """> **先说清楚一件事**：编译出来的东西是**内核（boot.img）**，不是安卓 8.1 系统。
> 内核只是系统的一部分，第 9 节会讲清楚还缺什么。别跳过那一节。"""
head_new = """> **先说清楚一件事**：编译出来的东西是**内核（boot.img）**，不是安卓 8.1 系统本身。
>
> **重要**：如果你已经刷了作者的 LineageOS 15.1 并且能开机（大多数人到了这一步），
> 那么打包模板**必须用作者的成品内核**，不能用原厂 7.0 的 `boot.img`——
> 两者 ramdisk 完全不同，套错了直接开不了机。具体见第 2.5 节。"""
assert head_anchor in t
t = t.replace(head_anchor, head_new, 1)

# 2) 在"1. 你要做的事分成几步"之后插入 2.5 节
step_anchor = """**你需要准备：**"""
step_new = """### 2.5 先决定用哪个文件当"模板"（关键）

打包时会拿一个现成的 `boot.img` 当模板，把新编出来的内核塞进去，
沿用它的 ramdisk、命令行参数和 DTB。**模板选错 = 刷上去开不了机。**

| 你现在的情况 | 该用哪个当模板 |
|---|---|
| **已经刷了 LineageOS 15.1，能开机**（推荐路径） | 作者成品内核：`boot-batfix-0925.img` / `boot-camstream-0925.img` / `boot-lis3dh-final.img` 里**你正在用的那一个** |
| 还在原厂安卓 7.0 | 你从固件包里拿的 `boot.img`（16MB 那个） |

**如果你要修 8.1 的 bug，那就一定用第一种**——你要替换的是 LineageOS 系统里的内核，
不是 7.0 的。

> 作者那三个文件在 https://github.com/Haoduo9/bbk_s3pro_flashtools_and_boot/tree/main/boot_images
> 挑你**当前正在用**的那个下载即可（名字能看出来改了什么：电池、摄像头、加速度计）。

**你需要准备：**"""
assert step_anchor in t
t = t.replace(step_anchor, step_new, 1)

# 3) 改用文件清单那段，说明放哪个
old_list = """```
s3pro-kernel/
├── .github          ← 以点开头，重要！里面是编译配置
├── docs
├── inputs           ← 里面有 boot.img（16MB）
├── scripts
├── tests
├── README.md
└── ...
```"""
new_list = """```
s3pro-kernel/
├── .github          ← 以点开头，重要！里面是编译配置
├── docs
├── inputs           ← 里面有 boot.img（就是上面 2.5 说的模板）
├── scripts
├── tests
├── README.md
└── ...
```

**动手上传之前先做一件事**：把 `inputs\\boot.img` 换成 2.5 节里选中的那个模板。

- 原厂那个先**改名留作备份**，别删（哪天要刷回 7.0 还得用）
- 把作者成品内核改名为 `boot.img` 放进 `inputs\\`
- 改名后的文件大小应该是 **7～8MB**（作者成品）或 **16MB**（原厂）"""
assert old_list in t
t = t.replace(old_list, new_list, 1)

# 4) 第 9 节整体替换：从"没有系统"改成"系统已有，重点在模板与 bug"
start = "## 9. 关于\"安卓 8.1\"的真相（别跳过）"
end = "## 10. 出问题怎么办"
i, j = t.find(start), t.find(end)
assert i > 0 and j > i
new9 = """## 9. 你现在到底在做什么（重要）

你手上已经有作者的 LineageOS 15.1 系统、而且用他的成品内核能开机。
所以你要做的**不是"装 8.1"**，而是：

> 把作者的内核换成你自己编译的同版本内核，然后逐个修 bug。

这意味着三件事：

1. **模板必须是作者的成品内核**（见 2.5 节），因为它带着 LineageOS 15.1 的 ramdisk。
   用原厂的会直接开不了机。
2. **编译器目标必须是同一棵源码**：`Haoduo9/android_kernel_bbk_h3000s`，
   分支 `lineage-15.1-h3000s`（Linux 3.18.140）。默认值已经配好。
3. **第一次编出来的内核，功能应该和作者成品一模一样**（一个 bug 都没修）。
   这一步的意义是：证明"从源码到可刷内核"这条链在你机器上是通的。
   通了之后，改 defconfig / 改驱动 / 加补丁，才谈得上修 bug。

### 修 bug 的正确姿势

不要一上来就瞎改。顺序是：

```
① 先编出"零改动"的版本，刷进去，确认能开机
② 用出问题的功能触发 bug
③ 抓 dmesg：
      adb shell su -c "dmesg" > dmesg.txt
   （没有 adb 的话，TWRP 里有终端，或让我教你别的方式）
④ 把 dmesg 里相关的行发我，我告诉你改哪个配置/哪段代码
⑤ 改 inputs 或源码 → 重新编译 → 再刷
```

**没有 dmesg 的改代码 = 蒙。** 这也是为什么第 ③ 步不能省。

"""
t = t[:i] + new9 + t[j:]

# 5) 刷机段落：加"刷回作者成品内核"的备份
old_bak = """```bash
# 关机，按住音量上+下不放，插 USB，然后执行：
python mtk r boot backup-boot.img
python mtk rl backup-all
```

`backup-boot.img` 是你现在的原厂内核，**出事了靠它救回来**。"""
new_bak = """```bash
# 关机，按住音量上+下不放，插 USB，然后执行：
python mtk r boot backup-boot.img
python mtk rl backup-all
```

`backup-boot.img` 是你**现在正在用的内核**（如果你已经刷了 LineageOS，那就是作者的成品内核），
**出事了靠它救回来**。另外把原厂 7.0 的 `boot.img` 也留一份，那是最后的退路。"""
assert old_bak in t
t = t.replace(old_bak, new_bak, 1)

open(P, "w", encoding="utf-8", newline="\n").write(t)
print("教程已更新：%d 字节" % len(t))

# ---------------------------------------------------------------- inputs 说明
open("inputs/README.txt", "w", encoding="utf-8", newline="\n").write(
"""这个目录里的 boot.img 是"打包模板"：新内核会被塞进它的 ramdisk / cmdline / DTB 里。

选哪个当模板，取决于你要修的是哪套系统的内核：

  1) 你已经刷了 LineageOS 15.1（作者 Haoduo9 的移植），要修它的 bug
     → 换成作者的成品内核，任选你现在在用的那个：
         boot-batfix-0925.img / boot-camstream-0925.img / boot-lis3dh-final.img
       来源：https://github.com/Haoduo9/bbk_s3pro_flashtools_and_boot/tree/main/boot_images
       大小约 7～8MB

  2) 你还在原厂安卓 7.0
     → 用固件包里的 boot.img（16,777,216 字节）

两种都要保留另一份做备份：刷坏了用它回滚。

其他可选文件：
  inputs/boot.dtb   社区修正版 DTB（patches 仓库 dts/h3000s-FIXED5.dtb）
                    不放就自动从 boot.img 里拆
  inputs/config.txt / inputs/defconfig
                    一般不需要（内核树自带 h3000s_defconfig）
""")
print("inputs/README.txt 已重写")
