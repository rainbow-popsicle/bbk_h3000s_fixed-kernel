#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复 MT8173 VPU 硬解无时钟问题（纯驱动侧，不改 DTS）。

=== 现象 ===
VLC 开完全加速播 4K：花屏 1 秒 → 正常内容 → 流畅约 3 秒 → 之后 PPT 卡顿。
dumpsys/logcat：InitVideoDecodeHW failed、Cannot init driver。
系统播放器只有声音黑屏；MT 管理器不能播。

=== 实机证据（/sys/kernel/debug/clk/clk_summary）===
    vcodecpll         0  0  1521000000   enable=0
      vcodecpll_ck    0  0   507000000   enable=0
        vdec_sel      0  0   507000000   enable=0
          vdec_cken   0  0   507000000   enable=0   ★
      vcodecpll_370p5 0  0   380250000
        vdec_larb_cken 0 0   399999939   enable=0   ★

时钟源本身是对的（vdec_sel 的父时钟已是 vcodecpll_ck 507MHz），
只是整条链的 gate 都没打开。

=== 根因 ===
drivers/misc/mediatek/videocodec/mt8173/videocodec_kernel_driver.c
的 vdec_power_on() 在 CONFIG_OF 分支只做了一件事：
        mtk_smi_larb_clock_on(1, true);
文件里所有 clk_prepare/clk_enable 都被注释掉了（probe 里的
devm_clk_get 也在 /* */ 和 #if 0 里）。

对照同内核树的 V4L2 驱动 drivers/media/platform/mtk-vcodec/
mtk_vcodec_dec_pm.c 的 mtk_vcodec_dec_clock_on()：
        clk_prepare_enable(pm->vdecpll);
        clk_prepare_enable(pm->vdec_sel);
        clk_set_parent(pm->vdec_sel, pm->vdecpll);
        mtk_smi_larb_get(pm->larbvdec);

=== 为什么不用 clk_get(NULL, ...) / 不用改 DTS ===
· clk_get(NULL, "vdec_sel") → clk_get_sys → clk_find
  但 clk-mt8173.c 只用 of_clk_add_provider 注册时钟，没有
  clk_register_clkdev，全局 clkdev 表里查不到 → 必然 -ENOENT。
· 改 DTS 也不行：build.sh 用的是外部预编译 DTB
  （inputs/boot.dtb，USE_INPUT_DTB=1），根本不编译树里的
  h3000s.dts；强行改成自编译会因触控/显示节点不符而起不来。

所以改用内核正统 API of_clk_get_from_provider()：
  自己构造 of_phandle_args（np = topckgen 节点，args[0] = 时钟 ID），
  直接从 OF clock provider 取时钟，不需要 DTS 里有 clocks 属性。

时钟 ID 取自 include/dt-bindings/clock/mt8173-clk.h：
  CLK_TOP_VDEC_SEL   = 87  (vdec_sel)
  CLK_TOP_VCODECPLL  = 77  (vcodecpll_ck, 507MHz)

两个都在 topckgen（compatible = "mediatek,mt8173-topckgen"）里。

=== 补偿点 ===
    1) 头部 include <dt-bindings/clock/mt8173-clk.h>
    2) 全局 clk 句柄 + s3pro_vdec_clk_get()（懒加载，只跑一次）
    3) vdec_power_on() 里使能 vcodecpll_ck / vdec_sel 并 clk_set_parent

诊断日志用 MODULE_MFV_LOGE（= pr_err，始终可见），
不开 MT8173_VCODEC_DEBUG（那会把 MODULE_MFV_LOGD 变成 printk 刷屏）。

用法：
    python patch_vcodec_clk.py <内核树根> [--dry-run]
"""
import os
import sys

TARGET = os.path.join(
    "drivers", "misc", "mediatek", "videocodec", "mt8173",
    "videocodec_kernel_driver.c")

MARK = "S3PRO: VPU 时钟使能补丁"


def patch(src):
    if MARK in src:
        return src, 0

    # ---- 1) 加 dt-bindings 头（拿 CLK_TOP_VDEC_SEL / CLK_TOP_VCODECPLL）----
    anchor_inc = '#include "videocodec_kernel_driver.h"\n'
    inc_add = (
        '#include "videocodec_kernel_driver.h"\n'
        '/* ' + MARK + ': 取 CLK_TOP_VDEC_SEL / CLK_TOP_VCODECPLL 的编号 */\n'
        '#include <dt-bindings/clock/mt8173-clk.h>\n'
    )
    if anchor_inc not in src:
        return src, -1
    src = src.replace(anchor_inc, inc_add, 1)

    # ---- 2) 全局句柄 + 取时钟函数 ----
    anchor_globals = (
        "struct platform_device *pvenc_dev = NULL;\n"
        "struct platform_device *pvenclt_dev = NULL;\n"
    )
    global_add = (
        "struct platform_device *pvenc_dev = NULL;\n"
        "struct platform_device *pvenclt_dev = NULL;\n"
        "\n"
        "/* " + MARK + "\n"
        " * VPU 硬解无时钟：vdec_power_on 原本只调 mtk_smi_larb_clock_on，\n"
        " * 所有 clk_prepare/clk_enable 都被注释，导致 vdec_sel / vdec_cken\n"
        " * 的 enable_count 恒为 0，VPU 初始化失败（InitVideoDecodeHW）。\n"
        " * 这里通过 of_clk_get_from_provider 从 topckgen 直接取时钟，\n"
        " * 不依赖 DTS 里的 clocks 属性（build.sh 用的是外部预编译 DTB）。*/\n"
        "static struct clk *s3pro_vdec_pll;\n"
        "static struct clk *s3pro_vdec_sel;\n"
        "static int s3pro_vdec_clk_ready;\n"
        "\n"
        "static void s3pro_vdec_clk_get(void)\n"
        "{\n"
        "\tstruct of_phandle_args spec;\n"
        "\tstruct device_node *np;\n"
        "\n"
        "\tif (s3pro_vdec_clk_ready)\n"
        "\t\treturn;\n"
        "\n"
        "\tnp = of_find_compatible_node(NULL, NULL,\n"
        "\t\t\t\t\"mediatek,mt8173-topckgen\");\n"
        "\tif (!np) {\n"
        "\t\tMODULE_MFV_LOGE(\"" + MARK +
        ": topckgen node not found\\n\");\n"
        "\t\treturn;\n"
        "\t}\n"
        "\n"
        "\tspec.np = np;\n"
        "\tspec.args_count = 1;\n"
        "\n"
        "\tspec.args[0] = CLK_TOP_VDEC_SEL;\n"
        "\ts3pro_vdec_sel = of_clk_get_from_provider(&spec);\n"
        "\n"
        "\tspec.args[0] = CLK_TOP_VCODECPLL;\n"
        "\ts3pro_vdec_pll = of_clk_get_from_provider(&spec);\n"
        "\n"
        "\tof_node_put(np);\n"
        "\n"
        "\tif (!IS_ERR_OR_NULL(s3pro_vdec_sel) &&\n"
        "\t    !IS_ERR_OR_NULL(s3pro_vdec_pll)) {\n"
        "\t\ts3pro_vdec_clk_ready = 1;\n"
        "\t\tMODULE_MFV_LOGE(\"" + MARK +
        ": got clocks sel=%p pll=%p\\n\",\n"
        "\t\t\t\ts3pro_vdec_sel, s3pro_vdec_pll);\n"
        "\t} else {\n"
        "\t\tMODULE_MFV_LOGE(\"" + MARK +
        ": clock get FAIL sel=%ld pll=%ld\\n\",\n"
        "\t\t\t\tPTR_ERR(s3pro_vdec_sel), PTR_ERR(s3pro_vdec_pll));\n"
        "\t}\n"
        "}\n"
    )
    if anchor_globals not in src:
        return src, -2
    src = src.replace(anchor_globals, global_add, 1)

    # ---- 3) vdec_power_on 里真正使能时钟 ----
    anchor_power = (
        "#ifdef CONFIG_OF\n"
        "\tMODULE_MFV_LOGD(\"vdec_power_on D+\\n\");\n"
        "\tmtk_smi_larb_clock_on(1, true);\n"
        "\tMODULE_MFV_LOGD(\"vdec_power_on D -\\n\");\n"
    )
    power_add = (
        "#ifdef CONFIG_OF\n"
        "\tMODULE_MFV_LOGD(\"vdec_power_on D+\\n\");\n"
        "\tmtk_smi_larb_clock_on(1, true);\n"
        "\t/* " + MARK + " */\n"
        "\ts3pro_vdec_clk_get();\n"
        "\tif (s3pro_vdec_clk_ready) {\n"
        "\t\tclk_prepare_enable(s3pro_vdec_pll);\n"
        "\t\tclk_prepare_enable(s3pro_vdec_sel);\n"
        "\t\tclk_set_parent(s3pro_vdec_sel, s3pro_vdec_pll);\n"
        "\t}\n"
        "\tMODULE_MFV_LOGD(\"vdec_power_on D -\\n\");\n"
    )
    if anchor_power not in src:
        return src, -3
    src = src.replace(anchor_power, power_add, 1)

    return src, 3


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    path = os.path.join(tree, TARGET)
    if not os.path.isfile(path):
        raise SystemExit("找不到 %s" % path)

    # 前置校验：确保时钟宏能读到，且值是我们预期的
    hdr = os.path.join(tree, "include", "dt-bindings", "clock",
                       "mt8173-clk.h")
    if os.path.isfile(hdr):
        vals = {}
        with open(hdr, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                # 必须精确匹配宏名，否则 CLK_TOP_VCODECPLL 会误匹配
                # CLK_TOP_VCODECPLL_370P5（它的值是 78）
                if len(parts) >= 3 and parts[0] == "#define" and \
                        parts[1] in ("CLK_TOP_VDEC_SEL",
                                     "CLK_TOP_VCODECPLL"):
                    try:
                        vals[parts[1]] = int(parts[2])
                    except ValueError:
                        pass
        if vals.get("CLK_TOP_VDEC_SEL") != 87 or \
                vals.get("CLK_TOP_VCODECPLL") != 77:
            raise SystemExit(
                "时钟宏值异常（VDEC_SEL=%s 期望 87, VCODECPLL=%s 期望 77），"
                "请重新核对" % (vals.get("CLK_TOP_VDEC_SEL"),
                                vals.get("CLK_TOP_VCODECPLL")))
        print("[chk] CLK_TOP_VDEC_SEL=87 CLK_TOP_VCODECPLL=77 ✓")
    else:
        raise SystemExit("找不到 %s" % hdr)

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        src = f.read()

    if MARK in src:
        print("[ok]  补丁已打过，跳过")
        return

    new_src, n = patch(src)
    if n < 0:
        raise SystemExit("补丁锚点未匹配（错误码 %d），结构不符，中止" % n)

    if dry:
        print("[dry] 会给 vdec_power_on 补上 vcodecpll_ck/vdec_sel 使能"
              "（经 of_clk_get_from_provider）")
        return

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_src)
    print("[ok]  补丁完成：vdec_power_on 已补时钟使能")


if __name__ == "__main__":
    main()
