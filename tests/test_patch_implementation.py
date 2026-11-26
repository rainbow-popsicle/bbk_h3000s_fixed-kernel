# -*- coding: utf-8 -*-
"""补丁实现审计：逐个验证每个补丁「生成的代码里是否真的有预期改动」。

与其它检查的分工：
  - build.sh preflight       只检查脚本文件存在
  - patch_*.py 的自检断言      作者自己写的，可能写错（历史上写错过两条）
  - test_patch_marks.py      检查「脚本源码里有没有标记字符串」
  - 本脚本                    检查「补丁跑完后目标文件里是否真的出现预期代码」

范围：只覆盖 5 个「只改面板驱动」的补丁
      （lcm_stock / lcm_init_table / lk_replay / stock_timing / lcm_readid）。
      它们全部作用于 inputs/nt35523_wqxga_dsi_vdo_sharp.c，
      因此不需要内核源码树 —— CI 的 selftest job 也能跑。

注意：本脚本必须保持「不依赖本机绝对路径」，否则 CI（Linux）会 FileNotFoundError。
"""
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
SCRIPTS = os.path.join(ROOT, 'scripts')
INPUTS = os.path.join(ROOT, 'inputs')

DRV_REL = os.path.join('drivers', 'misc', 'mediatek', 'lcm',
                       'nt35523_wqxga_dsi_vdo_sharp', 'nt35523_wqxga_dsi_vdo_sharp.c')

# build.sh 里对这行驱动做的 sed 替换，必须在这儿复现，否则补丁会因锚点不符而跳过
SED_RULES = [
    ('"mediatek,mt8173-lcm"', '"mediatek,nt35523_fhd"'),
    ('gpio_lcm_rst_en_new', 'gpio_lcd_rst'),
    ('gpio_lcm_rst_en', 'gpio_lcd_rst'),
    ('gpio_lcm_vsp_en', 'gpio_lcd_enp'),
    ('gpio_lcm_vsn_en', 'gpio_lcd_enn'),
    ('gpio_lcm_1v8_en', 'gpio_lcd_enp'),
    ('goto out;', ';'),
]

DRV_CHAIN = [
    ('patch_lcm_stock.py',
     ['--tables', os.path.join(ROOT, 'kernel-patches', 'lcm-table', 'stock_tables.c')]),
    ('patch_lcm_init_table.py', []),
    ('patch_lk_replay.py', []),
    ('patch_stock_timing.py', []),
    ('patch_lcm_readid.py', []),
]


def make_tree(dst):
    """只准备面板驱动（不需要内核源码树）。"""
    if os.path.exists(dst):
        shutil.rmtree(dst, ignore_errors=True)
    src = open(os.path.join(INPUTS, 'nt35523_wqxga_dsi_vdo_sharp.c'), encoding='utf-8').read()
    for a, b in SED_RULES:
        src = src.replace(a, b)
    p = os.path.join(dst, DRV_REL)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, 'w', encoding='utf-8', newline='').write(src)
    return dst


def run_patch(tree, script, args=None):
    cmd = [PY, os.path.join(SCRIPTS, script), tree] + (args or [])
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    return r.returncode, (r.stdout or ''), (r.stderr or '')


def audit_driver(path):
    """返回 [(检查项, 是否通过, 细节)]。检查「生成的驱动里真的有预期代码」。"""
    c = open(path, encoding='utf-8').read()
    out = []

    def chk(name, cond, detail=''):
        out.append((name, bool(cond), detail))

    # ---- 数据表 ----
    chk('睡眠表 s3pro_sleep_table', 's3pro_sleep_table[]' in c)
    chk('表A s3pro_init_table_a', 's3pro_init_table_a[]' in c)
    chk('表B s3pro_init_table_b', 's3pro_init_table_b[]' in c)

    # ---- 四个电源函数各只定义一次 ----
    for fn in ['lcm_suspend', 'lcm_suspend_power', 'lcm_resume', 'lcm_resume_power']:
        n = len(re.findall(r'static void %s\(void\)' % fn, c))
        chk('%s 定义唯一' % fn, n == 1, '找到 %d 个' % n)

    # ---- TPS65132（关键：正负电源各一条 I2C 写） ----
    n_tps = len(re.findall(r'tps65132_write_bytes\(', c))
    chk('tps65132 调用 >= 2 次', n_tps >= 2, '实际 %d 次' % n_tps)
    chk('tps65132 正电源 0x00',
        'tps65132_write_bytes(0x00' in c or 'tps65132_write_bytes(cmd, data)' in c)
    chk('tps65132 负电源 0x01',
        'tps65132_write_bytes(0x01' in c or 'cmd = 0x01;' in c)

    # ---- 上电顺序：VSP 必须在 RST 之前（原厂顺序） ----
    i_vsp = c.find('GPIO_LCD_VSP_EN, GPIO_OUT_ONE')
    i_rst = c.find('GPIO_LCD_RST_EN, GPIO_OUT_ONE')
    chk('VSP 在 RST 之前', i_vsp >= 0 and i_rst >= 0 and i_vsp < i_rst,
        'vsp=%d rst=%d' % (i_vsp, i_rst))

    # ---- lcm_init 三级选表 ----
    chk('lcm_init 三级选表 s3pro_lcm_variant', 's3pro_lcm_variant' in c)
    chk('push_23 实现', c.count('push_23') >= 2, '出现 %d 次' % c.count('push_23'))
    chk('push_109 实现', c.count('push_109') >= 2, '出现 %d 次' % c.count('push_109'))

    # ---- readback 探针 ----
    n_rb = c.count('void mtk_lcm_s3pro_readback(void)')
    chk('readback 定义唯一', n_rb == 1, '定义 %d 次' % n_rb)
    chk('readback 回读 0x0A', 'read_reg_v2(0x0A' in c)
    chk('readback 回读 0xDA/0xDB', 'read_reg_v2(0xDA' in c and 'read_reg_v2(0xDB' in c)

    # ---- debugfs 实验台 ----
    chk('debugfs 节点(s3pro_lk/s3pro_tmg)', ('s3pro_lk' in c) or ('s3pro_tmg' in c))

    # ---- 时序参数（stock_timing） ----
    if 'PLL_CLOCK' in c:
        chk('PLL_CLOCK 用变量而非写死 395',
            ('PLL_CLOCK = t_pll' in c) or ('PLL_CLOCK = s3pro' in c),
            '需人工确认')
        chk('noncont_clock 已设', 'noncont_clock' in c)

    # ---- 结构完整性 ----
    bal = c.count('{') - c.count('}')
    chk('花括号平衡', bal == 0, '差 %d' % bal)
    chk('打印字符串不跨行', not re.search(r'(?:LCM_LOG[IEWD]|pr_info|pr_err)\("[^"]*\n', c))

    return out


def main():
    print('=' * 70)
    print('  补丁实现审计：生成的代码里是否真的有预期改动')
    print('=' * 70)

    if not os.path.exists(os.path.join(INPUTS, 'nt35523_wqxga_dsi_vdo_sharp.c')):
        print('  !! 找不到 inputs/nt35523_wqxga_dsi_vdo_sharp.c')
        return 1

    tree = os.path.join(ROOT, 'work', 'audit_drv')
    make_tree(tree)
    drv_p = os.path.join(tree, DRV_REL)

    print()
    print('--- 驱动补丁链（按 build.sh 顺序累积跑）---')
    failed = 0
    for name, args in DRV_CHAIN:
        rc, out, err = run_patch(tree, name, args)
        c_now = open(drv_p, encoding='utf-8').read() if os.path.exists(drv_p) else ''
        n_tps = len(re.findall(r'tps65132_write_bytes\(', c_now))
        i_vsp = c_now.find('GPIO_LCD_VSP_EN, GPIO_OUT_ONE')
        i_rst = c_now.find('GPIO_LCD_RST_EN, GPIO_OUT_ONE')
        order = '?' if i_vsp < 0 or i_rst < 0 else ('VSP先' if i_vsp < i_rst else 'RST先')
        ok = (rc == 0)
        print('  %-34s rc=%d %-4s tps65132=%-2d 顺序=%s'
              % (name, rc, 'OK' if ok else 'FAIL', n_tps, order))
        if not ok:
            failed += 1
            tail = (err or out).strip().split('\n')[-4:]
            for ln in tail:
                if ln.strip():
                    print('      ' + ln.strip())

    print()
    print('--- 整条链跑完后的总审计 ---')
    res = audit_driver(drv_p)
    for rn, rok, detail in res:
        print('  [%s] %-38s %s' % ('OK  ' if rok else 'MISS', rn, detail))
    bad = [r for r in res if not r[1]]

    shutil.rmtree(tree, ignore_errors=True)

    print()
    print('=' * 70)
    if failed or bad:
        print('  结果：FAIL —— %d 个补丁失败, %d 项预期缺失' % (failed, len(bad)))
        return 1
    print('  结果：PASS —— 所有驱动补丁都已真正实现预期改动')
    return 0


if __name__ == '__main__':
    sys.exit(main())
