#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S3PRO 实验补丁：把 lcm_resume 的 GPIO 上电顺序反转。

实验假设（诊断内核实验报告.md 第五节）：
  build 77 的 lcm_resume 是「先 RST 复位脉冲 → 再 VSP/VSN 供电」，
  复位打在还没上电的面板上等于没复位 —— 能解释"命令全发/寄存器健康但面板纯黑"。

  本补丁把它改成标准顺序「先 VSP/VSN 供电 → 稳定 → 再 RST 复位脉冲」，
  保持所有延时值不变，只换 GPIO 操作先后，其余推表逻辑一律不动。

  对照实验：若刷入后断电重建能亮 → 假设成立；仍黑 → 排除，转查 D-PHY。

必须在 patch_lcm_stock.py 之后运行（后者会重写 lcm_resume 整段）。
幂等：重复运行不重复替换；已替换过则跳过。

用法：
    python patch_lcm_power_seq.py <内核树根> [--dry-run]
"""
import os
import sys

MARK = "S3PRO_POWER_SEQ"

# 当前（build 77 / patch_lcm_stock 注入后）的顺序：先复位 → 后供电
OLD = '''\t/* 原厂 resume：先复位，再使能 VSP/VSN（原实现顺序相反） */
\tlcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ONE);
\tMDELAY(5);
\tlcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ZERO);
\tMDELAY(2);
\tlcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ONE);
\tMDELAY(25);

\tlcm_set_gpio_output(GPIO_LCD_VSP_EN, GPIO_OUT_ONE);
\tMDELAY(2);
\tlcm_set_gpio_output(GPIO_LCD_VSN_EN, GPIO_OUT_ONE);
\tMDELAY(2);'''

# 实验顺序：先供电 → 稳定 → 再复位。延时值全部保留，不引入新变量。
NEW = '''\t/* S3PRO_POWER_SEQ: 实验——先供电稳定，再复位（对照原厂"先复位后供电"） */
\tlcm_set_gpio_output(GPIO_LCD_VSP_EN, GPIO_OUT_ONE);
\tMDELAY(2);
\tlcm_set_gpio_output(GPIO_LCD_VSN_EN, GPIO_OUT_ONE);
\tMDELAY(2);
\t/* 供电后多等一会，确保 VSP/VSN 轨道稳定再复位 */
\tMDELAY(25);

\tlcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ONE);
\tMDELAY(5);
\tlcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ZERO);
\tMDELAY(2);
\tlcm_set_gpio_output(GPIO_LCD_RST_EN, GPIO_OUT_ONE);
\tMDELAY(25);'''

# 已打补丁的标志（出现在注释里）
ALREADY = "S3PRO_POWER_SEQ: 实验"


def find_lcm_driver(tree):
    """找到被注入 S3PRO 表的 nt35523 驱动（build 用 sharp）。"""
    base = os.path.join(tree, "drivers", "misc", "mediatek", "lcm")
    candidates = []
    for root, _d, files in os.walk(base):
        if "nt35523" not in root:
            continue
        for fn in files:
            if not fn.endswith(".c"):
                continue
            p = os.path.join(root, fn)
            with open(p, encoding="utf-8", errors="replace") as f:
                t = f.read()
            if "s3pro_lcm_variant" in t or "s3pro_init_table_a" in t:
                return p
            if "lcm_initialization_setting[]" in t:
                candidates.append(p)
    for p in candidates:
        if "sharp" in p:
            return p
    return candidates[0] if candidates else None


def patch_file(path, dry):
    with open(path, encoding="utf-8", errors="replace") as f:
        src = f.read()

    if ALREADY in src:
        print("  已打过 S3PRO_POWER_SEQ，跳过（幂等）")
        return True

    n_old = src.count(OLD)
    if n_old != 1:
        print("  !! 锚点匹配 %d 处（期望 1），未修改" % n_old)
        # 打印近似位置帮助排查
        if "先复位，再使能 VSP" in src:
            print("     找到旧注释但整体不匹配——检查缩进/内容是否被其他补丁改过")
        return False

    if dry:
        print("  [dry-run] 锚点命中 1 处，将替换 GPIO 顺序")
        return True

    src = src.replace(OLD, NEW, 1)
    with open(path, "w", encoding="utf-8", errors="replace") as f:
        f.write(src)
    print("  已反转 GPIO 顺序：先 VSP/VSN 供电 → 再 RST 复位")
    return True


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    lcm = find_lcm_driver(tree)
    if not lcm:
        raise SystemExit("找不到 nt35523 LCM 驱动")
    print("  LCM 驱动: %s" % os.path.relpath(lcm, tree))
    if not patch_file(lcm, dry):
        raise SystemExit("GPIO 时序补丁失败")


if __name__ == "__main__":
    main()
