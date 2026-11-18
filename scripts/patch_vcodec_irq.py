#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补 MT8173 vcodec 驱动的 VDEC/VENC 中断号映射（在 probe 阶段重映射）。

=== 已确认的现象链（实机验证）===
1) /system/build.prop 里 ro.mtk_sec_video_path_support=1
   → 用户态 OMX 走"安全视频路径"（bSecureInst=TRUE）
   → 内核驱动跳过 request_irq，交给 MTEE/TrustZone
   → 用户态报 Cannot init driver ×30 → InitVideoDecodeHW failed (not compatible
   → ACodec 放弃 → VLC 退回软解 → 卡成 PPT
   【已用把该属性改成 0 验证：该错误串消失】

2) 属性改成 0 后，解码器能起来了，但紧接着：
       ## [ERROR] MtkOmxVdecDecodeThread() line: 1400 WAIT timeout...
   即解码线程在等硬件中断，永远等不到。
   同一时刻 /proc/interrupts 里【没有】VDEC(SPI 204) 那一行，
   说明 request_irq 从未成功。

=== 根因（本次要修的）===
videocodec_kernel_driver.c 的 vcodec_driver_init()（module_init 阶段）里：
       VDEC_IRQ_ID = irq_of_parse_and_map(node, 0);
MTK 的 mtk-sysirq 是层级中断控制器，module_init 阶段它往往还没初始化完，
irq_of_parse_and_map 失败返回 0；之后 request_irq(0, ...) 必然失败，
VPU 永远收不到完成中断 → DecodeThread 超时 → 画面卡死。

=== 修法 ===
在 vcodec_probe()（平台驱动探测阶段，中断控制器已就绪）里，
若 IRQ 号为 0 就重新映射一次；顺便若 BASE 为 0 也重新 iomap，
并用 pr_err（MODULE_MFV_LOGE，始终可见）打印结果，便于下次验证。

========= 先决条件 1：ro.mtk_sec_video_path_support 必须为 0（在 build.prop 里改，不在内核）
========= 先决条件 2：无（本补丁独立可用）

用法：
    python patch_vcodec_irq.py <内核树根> [--dry-run]
"""
import os
import sys

TARGET = os.path.join(
    "drivers", "misc", "mediatek", "videocodec", "mt8173",
    "videocodec_kernel_driver.c")

MARK = "S3PRO: VDEC IRQ 重映射补丁"

# vcodec_probe 里注册字符设备前的唯一锚点（注意 unregister_* 不算）
ANCHOR = "\tret = register_chrdev_region(vcodec_devno, 1, VCODEC_DEVNAME);\n"


def patch(src):
    if MARK in src:
        return src, 0

    if src.count(ANCHOR) != 1:
        return src, -1

    insert = (
        "\t/* " + MARK + "\n"
        "\t * module_init 阶段 irq_of_parse_and_map 常因 sysirq 未就绪而返回 0，\n"
        "\t * 导致后面对 IRQ 0 做 request_irq 必失败，VPU 收不到完成中断\n"
        "\t * （现象：MtkOmxVdecDecodeThread WAIT timeout，画面卡死）。\n"
        "\t * 这里在 probe 阶段（中断控制器已就绪）补一次映射。*/\n"
        "\tif (VDEC_IRQ_ID == 0 || VENC_IRQ_ID == 0 || VENC_LT_IRQ_ID == 0) {\n"
        "\t\tstruct device_node *np;\n"
        "\n"
        "\t\tnp = of_find_compatible_node(NULL, NULL,\n"
        "\t\t\t\t\t\"mediatek,mt8173-vdec\");\n"
        "\t\tif (np) {\n"
        "\t\t\tVDEC_IRQ_ID = irq_of_parse_and_map(np, 0);\n"
        "\t\t\tif (!KVA_VDEC_BASE) {\n"
        "\t\t\t\tKVA_VDEC_BASE = (VAL_ULONG_T) of_iomap(np, 0);\n"
        "\t\t\t\tKVA_VDEC_MISC_BASE = KVA_VDEC_BASE + 0x0000;\n"
        "\t\t\t\tKVA_VDEC_VLD_BASE = KVA_VDEC_BASE + 0x1000;\n"
        "\t\t\t\tKVA_VDEC_MC_BASE = KVA_VDEC_VLD_BASE + 0x1000;\n"
        "\t\t\t}\n"
        "\t\t\tof_node_put(np);\n"
        "\t\t}\n"
        "\n"
        "\t\tnp = of_find_compatible_node(NULL, NULL,\n"
        "\t\t\t\t\t\"mediatek,mt8173-venc\");\n"
        "\t\tif (np) {\n"
        "\t\t\tif (VENC_IRQ_ID == 0)\n"
        "\t\t\t\tVENC_IRQ_ID = irq_of_parse_and_map(np, 0);\n"
        "\t\t\tif (!KVA_VENC_BASE)\n"
        "\t\t\t\tKVA_VENC_BASE = (VAL_ULONG_T) of_iomap(np, 0);\n"
        "\t\t\tof_node_put(np);\n"
        "\t\t}\n"
        "\n"
        "\t\tnp = of_find_compatible_node(NULL, NULL,\n"
        "\t\t\t\t\t\"mediatek,mt8173-venclt\");\n"
        "\t\tif (np) {\n"
        "\t\t\tif (VENC_LT_IRQ_ID == 0)\n"
        "\t\t\t\tVENC_LT_IRQ_ID = irq_of_parse_and_map(np, 0);\n"
        "\t\t\tif (!KVA_VENC_LT_BASE)\n"
        "\t\t\t\tKVA_VENC_LT_BASE = (VAL_ULONG_T) of_iomap(np, 0);\n"
        "\t\t\tof_node_put(np);\n"
        "\t\t}\n"
        "\t}\n"
        "\tMODULE_MFV_LOGE(\"[S3PRO-DIAG] VDEC_IRQ=%u VENC_IRQ=%u VENC_LT_IRQ=%u \"\n"
        "\t\t\t \"VDEC_BASE=0x%lx GCON_BASE=0x%lx\\n\",\n"
        "\t\t\t (unsigned int)VDEC_IRQ_ID, (unsigned int)VENC_IRQ_ID,\n"
        "\t\t\t (unsigned int)VENC_LT_IRQ_ID,\n"
        "\t\t\t (unsigned long)KVA_VDEC_BASE,\n"
        "\t\t\t (unsigned long)KVA_VDEC_GCON_BASE);\n"
        "\n"
        + ANCHOR
    )

    src = src.replace(ANCHOR, insert, 1)
    return src, 1


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    path = os.path.join(tree, TARGET)
    if not os.path.isfile(path):
        raise SystemExit("找不到 %s" % path)

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        src = f.read()

    if MARK in src:
        print("[ok]  IRQ 补丁已打过，跳过")
        return

    new_src, n = patch(src)
    if n < 0:
        raise SystemExit("锚点不唯一或未匹配（%d 处），结构不符，中止" % n)

    if dry:
        print("[dry] 会在 vcodec_probe 里补 VDEC/VENC IRQ 映射并打印结果")
        return

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_src)
    print("[ok]  IRQ 补丁完成：probe 阶段会重映射 VDEC/VENC 中断")


if __name__ == "__main__":
    main()
