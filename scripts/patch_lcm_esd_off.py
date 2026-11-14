#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只关掉面板 ESD 自检，不动初始化时机。

实机证据链（v1 回退版，能显示画面但约 72 秒后黑屏、背光仍亮）：
  [72.4s]  CMDQ 错误开始出现（此后共 37 个错误块）
  [72~109] display_esd_che 相关日志 2308 行
           primary_display_esd_recovery ×5
           DSI0 / DSI1 最终都变成 idle（显示引擎停转）
  背光仍是 102（亮着），但没有任何画面；此后按电源键也无法恢复。

机制：
  MTK 的 ESD 自检线程每 2 秒读一次面板寄存器（本驱动配的是 cmd 0x0A，期望 0x9C）。
  读取走 CMDQ → DSI，一旦超时就判定面板异常，触发 esd_recovery：
      ddp_dsi_stop → DSI_Reset → lcm_init → ... → 显示引擎被反复停下
  读失败一次就进这个循环，画面再也回不来。

  作者内核全程 0 次 esd_recovery，所以从不黑屏。

本补丁只做一件事：把 esd_check_enable / customization_esd_check_enable 置 0。
【不动】lcm_init / lcm_resume 的时序（v2 的改动默认关闭，见 LCM_INIT_PATCH）。
这样既保住 v1 已经能工作的画面，又切断 ESD 死循环。

用法：
    python patch_lcm_esd_off.py <内核树根> [--file <面板驱动>] [--dry-run]
"""
import os
import sys

MARK = "S3PRO_ESD_OFF"


def find_panel(tree):
    name = None
    cfg = os.path.join(tree, ".config")
    if os.path.exists(cfg):
        for line in open(cfg, encoding="utf-8", errors="replace"):
            if line.startswith("CONFIG_CUSTOM_KERNEL_LCM="):
                name = line.split("=", 1)[1].strip().strip('"')
                break
    base = os.path.join(tree, "drivers", "misc", "mediatek", "lcm")
    if name:
        d = os.path.join(base, name)
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.endswith(".c"):
                    p = os.path.join(d, f)
                    t = open(p, encoding="utf-8", errors="replace").read()
                    if "esd_check_enable" in t:
                        return p
    for root, _d, files in os.walk(base):
        for f in files:
            if not f.endswith(".c"):
                continue
            p = os.path.join(root, f)
            t = open(p, encoding="utf-8", errors="replace").read()
            if "esd_check_enable" in t and "lcm_get_params" in t:
                return p
    return None


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv
    path = None
    for i, a in enumerate(sys.argv):
        if a == "--file" and i + 1 < len(sys.argv):
            path = sys.argv[i + 1]
    if path is None:
        path = find_panel(tree)
    if not path or not os.path.exists(path):
        raise SystemExit("找不到面板驱动文件（可用 --file 指定）")

    print("  目标驱动: %s" % os.path.relpath(path, tree))
    t = open(path, encoding="utf-8", errors="surrogateescape").read()

    if MARK in t:
        print("  已关过 ESD（找到 %s），跳过" % MARK)
        return 0

    n = 0
    for old, new in (
        ("params->dsi.esd_check_enable = 1;",
         "params->dsi.esd_check_enable = 0;\t/* S3PRO_ESD_OFF: 关 ESD，避免 recovery 死循环导致黑屏 */"),
        ("params->dsi.customization_esd_check_enable = 1;",
         "params->dsi.customization_esd_check_enable = 0;\t/* S3PRO_ESD_OFF */"),
    ):
        if old in t:
            t = t.replace(old, new, 1)
            n += 1

    if n == 0:
        # 可能已经是 0，或写法不同
        if "esd_check_enable = 0;" in t:
            print("  esd_check_enable 已经是 0，跳过")
            return 0
        raise SystemExit("没找到 esd_check_enable = 1 的赋值，无法关闭 ESD")

    if dry:
        print("  [dry-run] 将把 %d 处 ESD 开关置 0（未写文件）" % n)
        return 0

    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    c = open(path, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "esd_check_enable = 0": "params->dsi.esd_check_enable = 0;" in c,
        "customization_esd_check_enable = 0": "params->dsi.customization_esd_check_enable = 0;" in c,
        "没有残留 = 1": "esd_check_enable = 1;" not in c,
        "花括号配平": c.count("{") == c.count("}"),
    }
    print()
    bad = 0
    for k, v in checks.items():
        print("    %-36s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1
    if bad:
        print("\n!! 有 %d 项校验失败" % bad)
        return 1
    print("\nESD 已关闭（初始化时序未改动）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
