#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 build.sh 里那段"屏驱动补丁"真的能改对源码。

背景：这段补丁踩过两个坑，都是**只改脚本、没验证**造成的：
  1. 把 goto out; 删掉后 out: 标签没人引用，-Werror 直接编译失败；
  2. 补丁块被放在 .config 生成之前，sed 读不到 .config，bash -e 退出。

这个测试在 mock 的内核树里跑**真实的 bash 片段**（CI 上有 bash），断言：
  - 只改当前 .config 选中的那块屏，别的屏驱动不动
  - compatible 改成 mediatek,nt35523_fhd
  - 5 个 gpio_lcm_* 全部改掉
  - goto out; 和 out: 标签都没了
  - 改完的函数括号仍然配平

本机没有 bash 时退化成 Python 模拟同样的替换，仍然能抓出正则写错。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_SH = os.path.join(ROOT, "scripts", "build.sh")
WORK = os.path.join(ROOT, "tests", "tmp_lcm_patch")

LCM_NAME = "nt35523_wqxga_dsi_vdo_sharp"

DRIVER_C = '''\
#include <linux/of_gpio.h>

static unsigned int GPIO_LCD_VSP_EN;
static unsigned int GPIO_LCD_VSN_EN;
static unsigned int GPIO_LCD_RST_EN;
static unsigned int GPIO_LCD_RST_EN_NEW;
static unsigned int GPIO_LCD_1V8_EN;

void lcm_get_gpio_infor(void)
{
	int ret;
	static struct device_node *node;

	node = of_find_compatible_node(NULL, NULL, "mediatek,mt8173-lcm");

	GPIO_LCD_1V8_EN = of_get_named_gpio(node, "gpio_lcm_1v8_en", 0);
	GPIO_LCD_VSP_EN = of_get_named_gpio(node, "gpio_lcm_vsp_en", 0);
	GPIO_LCD_VSN_EN = of_get_named_gpio(node, "gpio_lcm_vsn_en", 0);
	GPIO_LCD_RST_EN = of_get_named_gpio(node, "gpio_lcm_rst_en", 0);
	GPIO_LCD_RST_EN_NEW = of_get_named_gpio(node, "gpio_lcm_rst_en_new", 0);

	ret = gpio_request(GPIO_LCD_1V8_EN, "GPIO_LCD_1V8_EN");
	if (ret) {
		printk("fail 1v8 %d\\n", ret);
		goto out;
	}

	ret = gpio_request(GPIO_LCD_VSP_EN, "GPIO_LCD_VSP_EN");
	if (ret) {
		printk("fail vsp %d\\n", ret);
		goto out;
	}

out:
	return;
}
'''

OTHER_C = '''\
void other_panel(void)
{
	int n = of_get_named_gpio(node, "gpio_lcm_vsp_en", 0);
	(void)n;
}
'''


def extract_block() -> str:
    """从 build.sh 里抠出 LCMPATCH 那段 if ... fi"""
    text = open(BUILD_SH, encoding="utf-8").read()
    start = text.find('if [ "${LCMPATCH:-1}" = "1" ]; then')
    if start < 0:
        raise SystemExit("build.sh 里找不到 LCMPATCH 块")
    depth = 0
    lines = text[start:].split("\n")
    out = []
    for line in lines:
        s = line.strip()
        out.append(line)
        if s.startswith("if ") or s.startswith("if["):
            depth += 1
        elif s == "fi":
            depth -= 1
            if depth == 0:
                break
    return "\n".join(out)


def build_mock_tree() -> str:
    if os.path.isdir(WORK):
        shutil.rmtree(WORK, ignore_errors=True)
    lcm_dir = os.path.join(WORK, "drivers", "misc", "mediatek", "lcm")
    sel_dir = os.path.join(lcm_dir, LCM_NAME)
    other_dir = os.path.join(lcm_dir, "some_other_panel")
    os.makedirs(sel_dir, exist_ok=True)
    os.makedirs(other_dir, exist_ok=True)

    with open(os.path.join(WORK, ".config"), "w", encoding="utf-8") as f:
        f.write('CONFIG_CUSTOM_KERNEL_LCM="%s"\n' % LCM_NAME)
        f.write("CONFIG_MTK_FB=y\n")

    sel_path = os.path.join(sel_dir, LCM_NAME + ".c")
    other_path = os.path.join(other_dir, "some_other_panel.c")
    with open(sel_path, "w", encoding="utf-8") as f:
        f.write(DRIVER_C)
    with open(other_path, "w", encoding="utf-8") as f:
        f.write(OTHER_C)
    return sel_path, other_path


def apply_with_python(block: str, sel_path: str) -> None:
    """没有 bash 时的退化路径：按 block 里同样的顺序做替换"""
    seds = re.findall(r"sed -i '([^']+)'", block)
    if not seds:
        raise SystemExit("block 里没解析出 sed 命令")
    text = open(sel_path, encoding="utf-8").read()
    for expr in seds:
        if not expr.startswith("s/"):
            continue
        body = expr[2:]
        sep = "/"
        parts = body.split(sep)
        if len(parts) < 3:
            continue
        old, new = parts[0], parts[1]
        flags = parts[2]
        old_py = (old.replace("\\(", "(").replace("\\)", ")")
                     .replace("[[:space:]]", r"\s"))
        flags_py = re.M if "g" not in flags else 0
        text = re.sub(old_py, new, text, flags=flags_py)
    open(sel_path, "w", encoding="utf-8").write(text)


def check(label: str, cond: bool, detail: str = "") -> bool:
    print(("  [ok]   " if cond else "  [FAIL] ") + label + (("  " + detail) if detail and not cond else ""))
    return cond


def main() -> int:
    block = extract_block()
    print("从 build.sh 取到 LCMPATCH 块：%d 行" % block.count("\n"))
    sel_path, other_path = build_mock_tree()
    ok = True

    if shutil.which("bash"):
        print("检测到 bash，直接用真实 shell 跑这一段")
        script = os.path.join(WORK, "block.sh")
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write("set -e\nLCMPATCH=1\n")
            f.write(block + "\n")
        r = subprocess.run(["bash", script], cwd=WORK, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        ok &= check("bash 执行成功", r.returncode == 0, r.stderr[-400:])
        if r.stdout.strip():
            print("  ---- 脚本输出 ----")
            for line in r.stdout.strip().splitlines():
                print("    " + line)
    else:
        print("本机没有 bash，用 Python 模拟相同的 sed 替换")
        apply_with_python(block, sel_path)

    sel = open(sel_path, encoding="utf-8").read()
    other = open(other_path, encoding="utf-8").read()

    ok &= check('compatible 改成了 mediatek,nt35523_fhd',
                '"mediatek,nt35523_fhd"' in sel and '"mediatek,mt8173-lcm"' not in sel)
    ok &= check("gpio_lcm_* 全部消失", "gpio_lcm_" not in sel)
    for name in ("gpio_lcd_enp", "gpio_lcd_enn", "gpio_lcd_rst"):
        ok &= check("出现了 %s" % name, name in sel)
    ok &= check("goto out; 已删除", "goto out;" not in sel)
    ok &= check("out: 标签已删除",
                not re.search(r"^[ \t]*out:[ \t]*$", sel, re.M))
    ok &= check("别的屏驱动没被动", other == OTHER_C)
    ok &= check("函数括号配平", sel.count("{") == sel.count("}"))

    if "--keep" not in sys.argv:
        shutil.rmtree(WORK, ignore_errors=True)

    print()
    if ok:
        print("全部通过")
        return 0
    print("有失败项")
    return 1


if __name__ == "__main__":
    sys.exit(main())
