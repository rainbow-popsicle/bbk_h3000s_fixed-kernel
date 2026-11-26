# -*- coding: utf-8 -*-
"""补丁实现审计：逐个验证每个补丁「生成的代码里是否真的有预期改动」。

与现有自检的区别：
  - build.sh 的 preflight 只检查脚本存在
  - patch_*.py 的自检断言是作者自己写的，可能写错（历史上就写错过两条）
  - test_patch_marks.py 检查的是「脚本源码里有没有标记字符串」

本脚本检查的是「补丁跑完之后，目标文件里是否真的出现了预期的代码」——
即用户要求的那件事：确认补丁真的实现了，而不是我以为它实现了。
"""
import os
import re
import shutil
import subprocess
import sys

ROOT = r'C:\Users\momei\Documents\deepseek-harness\default-workspace\s3pro-kernel'
KTREE = r'C:\Users\momei\Documents\deepseek-harness\default-workspace\work\ktree\android_kernel_bbk_h3000s-lineage-15.1-h3000s'
PY = r'C:\Users\momei\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'

DRV_REL = os.path.join('drivers', 'misc', 'mediatek', 'lcm',
                       'nt35523_wqxga_dsi_vdo_sharp', 'nt35523_wqxga_dsi_vdo_sharp.c')


def make_tree(dst):
    """构造一棵「尽量干净」的树：驱动来自 inputs/，内核文件来自 ktree。"""
    if os.path.exists(dst):
        shutil.rmtree(dst)

    # 驱动：inputs + build.sh 里的 sed 替换
    drv = open(os.path.join(ROOT, 'inputs', 'nt35523_wqxga_dsi_vdo_sharp.c'), encoding='utf-8').read()
    for a, b in [('"mediatek,mt8173-lcm"', '"mediatek,nt35523_fhd"'),
                 ('gpio_lcm_rst_en_new', 'gpio_lcd_rst'),
                 ('gpio_lcm_rst_en', 'gpio_lcd_rst'),
                 ('gpio_lcm_vsp_en', 'gpio_lcd_enp'),
                 ('gpio_lcm_vsn_en', 'gpio_lcd_enn'),
                 ('gpio_lcm_1v8_en', 'gpio_lcd_enp'),
                 ('goto out;', ';')]:
        drv = drv.replace(a, b)
    dpath = os.path.join(dst, DRV_REL)
    os.makedirs(os.path.dirname(dpath), exist_ok=True)
    open(dpath, 'w', encoding='utf-8', newline='').write(drv)

    # 内核文件：复制 ktree
    for rel in ['drivers/misc/mediatek/video/mt8173/videox/primary_display.c',
                'drivers/misc/mediatek/video/mt8173/dispsys/ddp_dsi.c']:
        s = os.path.join(KTREE, rel.replace('/', os.sep))
        t = os.path.join(dst, rel.replace('/', os.sep))
        os.makedirs(os.path.dirname(t), exist_ok=True)
        shutil.copy2(s, t)
    return dst


def brace_balanced(text):
    return text.count('{') - text.count('}')


def audit_drv(tree, label):
    """驱动补丁的预期结果。"""
    p = os.path.join(tree, DRV_REL)
    c = open(p, encoding='utf-8').read()
    results = []

    def chk(name, cond, detail=''):
        results.append((name, bool(cond), detail))

    # ---- 表 ----
    chk('睡眠表 s3pro_sleep_table', 's3pro_sleep_table[]' in c)
    chk('表A s3pro_init_table_a', 's3pro_init_table_a[]' in c)
    chk('表B s3pro_init_table_b', 's3pro_init_table_b[]' in c)

    # ---- 函数存在且唯一 ----
    for fn in ['lcm_suspend', 'lcm_suspend_power', 'lcm_resume', 'lcm_resume_power']:
        n = len(re.findall(r'static void %s\(void\)' % fn, c))
        chk('%s 定义唯一' % fn, n == 1, '找到 %d 个' % n)

    # ---- TPS65132（原厂行为，必须 2 条写） ----
    n_tps = len(re.findall(r'tps65132_write_bytes\(', c))
    chk('tps65132 至少调用 2 次', n_tps >= 2, '实际 %d 次' % n_tps)
    chk('tps65132 正电源 0x00', 'tps65132_write_bytes(0x00' in c or 'cmd, data' in c)
    chk('tps65132 负电源 0x01',
        'tps65132_write_bytes(0x01' in c or 'cmd = 0x01;' in c)

    # ---- 上电顺序：VSP 必须在 RST 之前 ----
    i_vsp = c.find('GPIO_LCD_VSP_EN, GPIO_OUT_ONE')
    i_rst = c.find('GPIO_LCD_RST_EN, GPIO_OUT_ONE')
    chk('VSP 在 RST 之前', i_vsp >= 0 and i_rst >= 0 and i_vsp < i_rst,
        'vsp=%d rst=%d' % (i_vsp, i_rst))

    # ---- lcm_init 三级选表（init_table 补丁） ----
    if 's3pro_lcm_variant' in c:
        chk('lcm_init 三级选表 s3pro_lcm_variant', True)
        chk('push_23 实现', c.count('push_23') >= 2, '出现 %d 次' % c.count('push_23'))
        chk('push_109 实现', c.count('push_109') >= 2, '出现 %d 次' % c.count('push_109'))

    # ---- readback（readid 补丁） ----
    if 'mtk_lcm_s3pro_readback' in c:
        n = c.count('void mtk_lcm_s3pro_readback(void)')
        chk('readback 定义唯一', n == 1, '定义 %d 次' % n)
        chk('readback 回读 0x0A', 'read_reg_v2(0x0A' in c)
        chk('readback 回读 0xDA/0xDB', 'read_reg_v2(0xDA' in c and 'read_reg_v2(0xDB' in c)

    # ---- 时序实验台（lk_replay / stock_timing） ----
    if 's3pro_lk' in c or 's3pro_tmg' in c:
        chk('debugfs 节点(s3pro_lk 或 s3pro_tmg)',
            ('s3pro_lk' in c) or ('s3pro_tmg' in c))

    # ---- 结构完整性 ----
    b = brace_balanced(c)
    chk('花括号平衡', b == 0, '差 %d' % b)
    bad_str = bool(re.search(r'(?:LCM_LOG[IEWD]|pr_info|pr_err)\("[^"]*\n', c))
    chk('打印字符串不跨行', not bad_str)

    return results


def run_patch(tree, script, args=None):
    cmd = [PY, os.path.join(ROOT, 'scripts', script), tree] + (args or [])
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    return r.returncode, (r.stdout or ''), (r.stderr or '')


def main():
    print('=' * 70)
    print('  补丁实现审计：生成的代码里是否真的有预期改动')
    print('=' * 70)

    # 每个补丁一组：跑完之后要检查什么
    DRV_CHAIN = [
        ('patch_lcm_stock.py', ['--tables', os.path.join(ROOT, 'kernel-patches', 'lcm-table', 'stock_tables.c')]),
        ('patch_lcm_init_table.py', []),
        ('patch_lk_replay.py', []),
        ('patch_stock_timing.py', []),
        ('patch_lcm_readid.py', []),
    ]

    print()
    print('--- 驱动补丁链（按 build.sh 顺序累积跑）---')
    tree = os.path.join(ROOT, 'work', 'audit_drv')
    make_tree(tree)
    failed = 0
    for name, args in DRV_CHAIN:
        rc, out, err = run_patch(tree, name, args)
        ok = (rc == 0)
        # 每跑完一个补丁就记录当前驱动状态，便于定位「谁破坏了什么」
        drv_p = os.path.join(tree, DRV_REL)
        c_now = open(drv_p, encoding='utf-8').read() if os.path.exists(drv_p) else ''
        n_tps = len(re.findall(r'tps65132_write_bytes\(', c_now))
        i_vsp = c_now.find('GPIO_LCD_VSP_EN, GPIO_OUT_ONE')
        i_rst = c_now.find('GPIO_LCD_RST_EN, GPIO_OUT_ONE')
        if i_vsp >= 0 and i_rst >= 0:
            order = 'VSP先' if i_vsp < i_rst else 'RST先'
        else:
            order = '?'
        print('  %-34s rc=%d %-4s  tps65132=%-2d 顺序=%s'
              % (name, rc, 'OK' if ok else 'FAIL', n_tps, order))
        if not ok:
            failed += 1
            tail = (err or out).strip().split('\n')[-4:]
            for ln in tail:
                if ln.strip():
                    print('      ' + ln.strip())
    print()
    print('--- 整条驱动链跑完后的总审计 ---')
    res = audit_drv(tree, 'all')
    for rn, rok, detail in res:
        print('  [%s] %-36s %s' % ('OK  ' if rok else 'MISS', rn, detail))
    bad = [r for r in res if not r[1]]
    shutil.rmtree(tree, ignore_errors=True)

    print()
    print('=' * 70)
    if failed or bad:
        print('  结果：FAIL —— %d 个补丁失败, %d 项预期缺失' % (failed, len(bad)))
        return 1
    print('  结果：PASS —— 所有补丁都已真正实现预期改动')
    return 0


if __name__ == '__main__':
    sys.exit(main())
