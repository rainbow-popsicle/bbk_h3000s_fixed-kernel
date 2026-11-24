#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从原厂 Android boot.img 解出的裸 ARM64 Image 中提取 kallsyms 符号表，
并对指定函数做 capstone 反汇编。用于「原厂 3.18.35 vs 我们 3.18.119 显示路径对比」。

背景（2026-11-21 实测解出）：
  原厂 kernel = Linux 3.18.35，从 s3pro_crack_v2.1/images/boot.img 提取：
    - boot.img kernel 是 gzip（page=2048），gzip 流后追加 ~55KB DTB，
      故 gzip.open() 会报 BadGzipFile，必须用 zlib.decompressobj()。
    - 解压后是裸 ARM64 Image（15,854,792 字节，ARMd magic，text_offset=0x80000）。
    - VA -> fileoff 映射：fileoff = VA - 0xffffffc000080000

kallsyms 布局（本 Image 实测偏移）：
  kallsyms_addresses : 0xb12500  (53803 × u64, 非递减; 首 0xffffffc000081000=stext)
  kallsyms_num_syms  : 0xb7b700  (u32 = 53803)
  kallsyms_names     : 0xb7b800  (666853 字节; 每符号 [len_byte][token编号...])
  kallsyms_token_table: 0xc1ec00 (256 个 \0 结尾串)
  kallsyms_token_index: 0xc1f000 (256 × u16, token 在表中的偏移, 末值 859)

解码要点（之前踩坑）：
  names 流里的每个字节是 **token 编号**，必须经 token_index[编号] 查到偏移，
  再从 token_table 取字符串。直接把字节当字符串下标会得到乱码。
  符号名首字符是类型位（T=全局文本, t=局部, D/d=数据, R=只读...）。

用法：
  python scripts/kallsyms_extract.py            # 导出符号表 + 反汇编默认关键函数
  python scripts/kallsyms_extract.py --list     # 只导出符号表
  python scripts/kallsyms_extract.py --func ddp_dsi_start   # 反汇编指定函数
"""
import os
import sys
import struct
import argparse

DEFAULT_IMAGE = r"C:/Users/momei/Documents/deepseek-harness/default-workspace/work/kernel_stock.bin"
OUT_DIR = r"C:/Users/momei/Documents/deepseek-harness/default-workspace/s3pro-kernel/work/stock70_symbols"

# 实测结构偏移（可按需改）
ADDR_OFF = 0xb12500
NUM_OFF = 0xb7b700
NAMES_OFF = 0xb7b800
TOKT_OFF = 0xc1ec00
TOKI_OFF = 0xc1f000
VA_DELTA = 0xffffffc000080000  # VA -> fileoff

# 类型位映射（kallsyms 首字符）
TYPE_MAP = {
    'A': 'A', 'B': 'B', 'D': 'D', 'R': 'R', 'T': 'T',
    'V': 'V', 'W': 'W', 'b': 'b', 'd': 'd', 'r': 'r',
    't': 't', 'w': 'w',
}


def load_image(path):
    if not os.path.isfile(path):
        raise SystemExit(f"原厂 Image 不存在: {path}")
    return open(path, 'rb').read()


def parse_kallsyms(d):
    n = struct.unpack_from('<I', d, NUM_OFF)[0]
    if not (1000 < n < 200000):
        raise SystemExit(f"num_syms 异常: {n} (offset 错?)")

    toki = struct.unpack_from('<256H', d, TOKI_OFF)
    toks = []
    for i in range(256):
        s = TOKT_OFF + toki[i]
        e = d.find(b'\0', s)
        if e < 0:
            raise SystemExit(f"token {i} 未找到结尾")
        toks.append(d[s:e])

    addrs = struct.unpack_from(f'<{n}Q', d, ADDR_OFF)

    syms = []
    pos = NAMES_OFF
    for i in range(n):
        L = d[pos]
        pos += 1
        raw = d[pos:pos + L]
        pos += L
        name = ''.join(toks[b].decode('ascii', 'replace') if b < 256 else '?' for b in raw)
        # 首字符是类型位
        typ = name[0] if name else '?'
        sym_name = name[1:] if name else ''
        syms.append((addrs[i], typ, sym_name))

    return syms


def dump_symbols(syms, out_path):
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write("# 原厂 3.18.35 kallsyms (从 boot.img 裸 Image 提取)\n")
        f.write(f"# 符号数: {len(syms)}\n\n")
        for addr, typ, name in syms:
            f.write(f"{addr:016x} {typ} {name}\n")
    print(f"符号表已导出: {out_path} ({len(syms)} 项)")


def disasm_func(d, syms, name_or_addr, max_ins=400):
    try:
        import capstone
    except ImportError:
        print("capstone 未安装，跳过反汇编", file=sys.stderr)
        return

    # 找函数
    target = None
    for addr, typ, nm in syms:
        if nm == name_or_addr:
            target = (addr, typ, nm)
            break
    if target is None:
        try:
            a = int(name_or_addr, 16)
            target = (a, '?', name_or_addr)
        except ValueError:
            print(f"未找到符号: {name_or_addr}")
            return

    va, typ, nm = target
    fo = va - VA_DELTA
    if not (0 <= fo < len(d)):
        print(f"函数 {nm} VA 0x{va:x} -> fileoff 0x{fo:x} 越界")
        return

    # 建地址->符号映射（含最近符号推断调用目标）
    addr2name = {a: nm for a, _, nm in syms}
    sorted_addr = sorted(addr2name.keys())
    import bisect

    def nearest(a):
        i = bisect.bisect_right(sorted_addr, a) - 1
        if i < 0:
            return '?'
        base = sorted_addr[i]
        return addr2name[base] + (f"+0x{a-base:x}" if a > base else "")

    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_LITTLE_ENDIAN)
    print(f"\n===== {nm} ({typ}) @ 0x{va:x} (fileoff 0x{fo:x}) =====")
    cnt = 0
    for ins in md.disasm(d[fo:fo + max_ins * 4], va):
        ann = ''
        if ins.mnemonic in ('bl', 'b') and ins.op_str.startswith('#'):
            try:
                t = int(ins.op_str[1:], 16)
                ann = '  ; -> ' + nearest(t)
            except ValueError:
                pass
        print(f"  0x{ins.address:x}: {ins.mnemonic:<10} {ins.op_str:<32}{ann}")
        cnt += 1
        if ins.mnemonic == 'ret' and cnt > 5:
            break
    print(f"  (共 {cnt} 条)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--image', default=DEFAULT_IMAGE, help='裸 ARM64 Image 路径')
    ap.add_argument('--list', action='store_true', help='只导出符号表')
    ap.add_argument('--func', action='append', default=[], help='要反汇编的函数名(可多次)')
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    d = load_image(args.image)
    print(f"Image 大小: {len(d)} 字节")

    syms = parse_kallsyms(d)
    print(f"解出符号: {len(syms)} 项")
    dump_symbols(syms, os.path.join(OUT_DIR, 'stock70_kallsyms.txt'))

    if args.list:
        return

    # 默认关键函数（显示/DSI/LCM resume 路径）
    defaults = [
        'primary_display_resume',
        'ddp_dsi_start',
        'DSI_set_cmdq',
        'DSI_EnableVM_CMD',
        'ddp_dsi_config',
    ]
    funcs = args.func if args.func else defaults
    for f in funcs:
        disasm_func(d, syms, f)


if __name__ == '__main__':
    main()
