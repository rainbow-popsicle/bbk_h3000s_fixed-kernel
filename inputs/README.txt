这个目录里的 boot.img 是"打包模板"：新内核会被塞进它的 ramdisk / cmdline / DTB 里。

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
