#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修正 nt35523 面板驱动：把"上电+复位+下发初始化表"补进 lcm_init()。

为什么必须改（实机证据链）：
  作者内核（能显示）在开机 0.638 秒就完成面板初始化：
      disp_lcm_init → lcm_init enter
      [LCM] gpio: vsp(enp)=985 vsn(enn)=987 rst=1016
      [LCM] lcm get reg-lcm LDO success
      push full init table                       ← 初始化序列在 lcm_init 里下发

  本树这份驱动的 lcm_init() 却是空壳，只取 GPIO：
      static void lcm_init(void) {
          LCM_LOGI("%s enter\\n", __func__);
          lcm_get_gpio_infor();
          update_hardware_info(TYPE_PANEL, 5);
      }
  真正的上电/复位/push_table 全在 lcm_resume() 里，而 resume 直到 SurfaceFlinger
  打开显示（本机 81 秒）才第一次被调用。

  后果（实测 dmesg）：
      [80.91] primary_display_suspend  dsi1 is busy
      [83.44] CMDQ SW timeout           ← 命令队列等不到 DSI 应答
      [84.44] CMDQ ERR timeout / dsi1_sel: Not Valid, Not Ready
      [之后]  wait VSYNC timeout ×485、underrun ×16 → GPU fence 永不完成 → 黑屏

  修法：在 lcm_init() 里补上 lcm_resume_power() + lcm_resume()，
        与作者内核行为对齐。resume 本身保持不变（后续唤醒照常工作）。

用法：
    python patch_lcm_init.py <内核树根> [--file <面板驱动路径>] [--dry-run]
"""
import os
import sys

MARK = "S3PRO_LCM_INIT_ON"          # 幂等标记
FWD = """/* S3PRO_LCM_INIT_ON: lcm_init 里要调用下面两个函数，先做前置声明 */
static void lcm_resume(void);
static void lcm_resume_power(void);

"""


def find_panel(tree):
    """优先按 .config 里的 CONFIG_CUSTOM_KERNEL_LCM 找，找不到再扫目录。"""
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
                    if "lcm_init(void)" in t:
                        return p
    # 兜底：全目录搜带 lcm_init 的 nt35523 驱动
    for root, _dirs, files in os.walk(base):
        if "nt35523" not in root.lower():
            continue
        for f in files:
            if not f.endswith(".c"):
                continue
            p = os.path.join(root, f)
            t = open(p, encoding="utf-8", errors="replace").read()
            if "void lcm_init(void)" in t and "update_hardware_info" in t:
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
        print("  已打过补丁（找到 %s），跳过" % MARK)
        return 0

    # ---- 1) lcm_init 必须存在 ----
    if "static void lcm_init(void)" not in t and "void lcm_init(void)" not in t:
        raise SystemExit("驱动里没有 lcm_init(void)，无法继续")

    # ---- 2) 找到 lcm_init 函数体里的 update_hardware_info 行 ----
    idx = t.find("void lcm_init(void)")
    if idx < 0:
        raise SystemExit("定位 lcm_init 失败")
    seg_end = t.find("\n}", idx)
    if seg_end < 0:
        raise SystemExit("定位 lcm_init 函数体结束失败")
    body = t[idx:seg_end]
    anchor = "update_hardware_info(TYPE_PANEL, 5);"
    if anchor not in body:
        raise SystemExit("lcm_init 里找不到 '%s' 作为插入锚点" % anchor)

    insert = (
        anchor + "\n"
        "\t/* S3PRO_LCM_INIT_ON: 面板必须在这里完成上电+复位+初始化。\n"
        "\t * 本树原始代码把这段放在 lcm_resume 里，而 resume 要等 SurfaceFlinger\n"
        "\t * 打开显示才被调用，开机阶段面板始终未初始化 → DSI/CMDQ 超时 → 黑屏。\n"
        "\t * 作者内核就是在 lcm_init 里下发初始化表的（push full init table）。\n"
        "\t */\n"
        "\tlcm_resume_power();\n"
        "\tlcm_resume();"
    )
    t = t[:idx] + body.replace(anchor, insert, 1) + t[seg_end:]

    # ---- 3) 插入前置声明（放在 lcm_get_gpio_infor 之前） ----
    gpio_fn = "void lcm_get_gpio_infor(void)"
    gi = t.find(gpio_fn)
    if gi < 0:
        raise SystemExit("找不到 lcm_get_gpio_infor，无法插入前置声明")
    # 找到该函数前的注释块起点，尽量插在函数定义之前
    t = t[:gi] + FWD + t[gi:]

    if dry:
        print("  [dry-run] 将插入上电调用与前置声明（未写文件）")
        i2 = t.find(MARK)
        for line in t[max(0, i2 - 400):i2 + 400].split("\n"):
            print("     " + line.rstrip())
        return 0

    open(path, "w", encoding="utf-8", errors="surrogateescape", newline="").write(t)

    # ---- 4) 校验 ----
    c = open(path, encoding="utf-8", errors="surrogateescape").read()
    checks = {
        "lcm_init 里调用 lcm_resume_power()": "lcm_resume_power();" in c,
        "lcm_init 里调用 lcm_resume()": "lcm_resume();" in c,
        "有前置声明": "static void lcm_resume(void);" in c,
        "标记已写入": MARK in c,
        "花括号配平": c.count("{") == c.count("}"),
    }
    print()
    bad = 0
    for k, v in checks.items():
        print("    %-34s %s" % (k, "OK" if v else "失败!"))
        if not v:
            bad += 1
    if bad:
        print("\n!! 有 %d 项校验失败" % bad)
        return 1
    print("\n面板驱动补丁完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
