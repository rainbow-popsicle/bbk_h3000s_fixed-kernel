#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 DSI 启停加上状态打印，用来定位"熄屏唤醒后黑屏"。

诊断对象：
  ddp_dsi_start()  —— resume 时被调用（显示引擎启动）
  ddp_dsi_stop()   —— suspend 时被调用（显示引擎停止）

打印内容：
  mipitx  : MIPI TX PLL 是否使能（MIPITX_IsEnabled）
  dsiclk  : DSI 控制器时钟是否使能（DSI_ClkIsEnabled）
  intsta0/1 : DSI0/DSI1 的 DSI_INTSTA 原始寄存器（bit31 = busy）
  dualen    : DSI_COM_CTRL 的 DUAL_EN（双通道使能）

判读方法：
  正常 resume 后应看到 mipitx=2 dsiclk=2（两条 DSI 都在）
  如果 mipitx=0 或 dsiclk=0 → MIPI PLL / DSI 时钟没恢复 → 面板收不到信号（黑屏）
  如果 intsta 的 bit31 一直为 1 → DSI 卡在忙状态

用法：
    python patch_disp_debug.py <内核树根> [--dry-run]
"""
import os
import sys

MARK = "S3PRO_DISP_DBG"

START_ANCHOR = """		DSI_SetMode(module, cmdq, _dsi_context[i].dsi_params.mode);
		DSI_clk_HS_mode(module, cmdq, true);
	}

	return 0;
}
"""

START_NEW = """		DSI_SetMode(module, cmdq, _dsi_context[i].dsi_params.mode);
		DSI_clk_HS_mode(module, cmdq, true);
	}

	/* S3PRO_DISP_DBG: 记录 start 结束时的硬件状态 */
	pr_info("S3PRO_DBG start_end: mipitx=%d dsiclk=%d intsta0=0x%08x intsta1=0x%08x\\n",
		MIPITX_IsEnabled(module, NULL), DSI_ClkIsEnabled(module, NULL),
		INREG32(&DSI_REG[0]->DSI_INTSTA), INREG32(&DSI_REG[1]->DSI_INTSTA));

	return 0;
}
"""

STOP_ANCHOR = """	DSI_clk_HS_mode(module, cmdq_handle, false);
	return 0;
}

int ddp_dsi_switch_lcm_mode"""

STOP_NEW = """	DSI_clk_HS_mode(module, cmdq_handle, false);

	/* S3PRO_DISP_DBG: 记录 stop 结束时的硬件状态 */
	pr_info("S3PRO_DBG stop_end: mipitx=%d dsiclk=%d intsta0=0x%08x intsta1=0x%08x\\n",
		MIPITX_IsEnabled(module, NULL), DSI_ClkIsEnabled(module, NULL),
		INREG32(&DSI_REG[0]->DSI_INTSTA), INREG32(&DSI_REG[1]->DSI_INTSTA));

	return 0;
}

int ddp_dsi_switch_lcm_mode"""


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    path = os.path.join(tree, "drivers", "misc", "mediatek", "video", "mt8173",
                        "dispsys", "ddp_dsi.c")
    if not os.path.exists(path):
        raise SystemExit("找不到 ddp_dsi.c：%s" % path)

    t = open(path, encoding="utf-8", errors="surrogateescape").read()
    if MARK in t:
        print("  已打过诊断补丁（找到 %s），跳过" % MARK)
        return 0

    problems = []
    for name, old, new in (("ddp_dsi_start", START_ANCHOR, START_NEW),
                           ("ddp_dsi_stop", STOP_ANCHOR, STOP_NEW)):
        cnt = t.count(old)
        if cnt != 1:
            problems.append("%s 锚点匹配 %d 次（应为 1 次）" % (name, cnt))
            continue
        t = t.replace(old, new, 1)
        print("  %s: 已插入状态打印" % name)

    if problems:
        print("\n!! 问题：")
        for p in problems:
            print("   - " + p)
        return 1

    if dry:
        print("  [dry-run] 未写文件")
        return 0

    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(path, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "start 里的打印": "S3PRO_DBG start_end" in c,
        "stop 里的打印": "S3PRO_DBG stop_end" in c,
        "标记存在": MARK in c,
        "花括号配平": c.count("{") == c.count("}"),
    }
    print()
    bad = 0
    for k, v in checks.items():
        print("    %-26s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1
    if bad:
        return 1
    print("\nDSI 状态诊断补丁完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
