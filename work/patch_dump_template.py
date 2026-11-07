P = "docs/新手教程-GitHub编译内核.md"
t = open(P, encoding="utf-8", newline="").read()

old = """> 作者那三个文件在 https://github.com/Haoduo9/bbk_s3pro_flashtools_and_boot/tree/main/boot_images
> 挑你**当前正在用**的那个下载即可（名字能看出来改了什么：电池、摄像头、加速度计）。"""

new = """**最稳的做法：直接从你自己的设备里读出来。**

你设备里现在跑的那个内核，就是「100% 确定能用」的模板，比下载猜测强得多：

```bash
# 关机，按住音量上+下不放，插 USB
python mtk r boot inputs/boot.img
```

读出来之后：

- 这个文件就是你的模板，同时也是你的**回滚备份**（先另存一份 `backup-boot.img`）
- 文件大小应该在 **7～8MB** 左右（如果读出来是 16MB，说明你还在原厂 7.0）

**备选：从作者仓库下载。**
https://github.com/Haoduo9/bbk_s3pro_flashtools_and_boot/tree/main/boot_images
三个文件的区别从名字能看出来：`batfix` = 电池、`camstream` = 摄像头、`lis3dh` = 加速度计。"""

assert old in t, "2.5 节的下载段没找到"
t = t.replace(old, new, 1)
open(P, "w", encoding="utf-8", newline="\n").write(t)
print("2.5 节已补上「从设备读」的做法，文件 %d 字符" % len(t))
