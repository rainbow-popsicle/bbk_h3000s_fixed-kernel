#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定点反汇编原厂 3.18.35 的 DSI 核心函数，按符号边界完整输出，
用于与当前 3.18.119 源码逐寄存器写入序列对比。

关注函数：
  DSI_Start       0xffffffc000476f64 - 0xffffffc00047726c
  DSI_Set_VM_CMD  0xffffffc00047726c - 0xffffffc0004775ac
  ddp_dsi_config  0xffffffc0004775ac - 0xffffffc000478564
  DSI_EnableVM_CMD 0xffffffc000478564 - 0xffffffc000478878
  ddp_dsi_start   0xffffffc00047a6d0 - (下一符号)

VA -> fileoff = VA - 0xffffffc000080000
"""
import os
import sys
import struct
import bisect

IMAGE = r"C:/Users/momei/Documents/deepseek-harness/default-workspace/work/kernel_stock.bin"
SYMS = r"C:/Users/momei/Documents/deepseek-harness/default-workspace/s3pro-kernel/work/stock70_symbols/stock70_kallsyms.txt"
VA_DELTA = 0xffffffc000080000


def load_syms(path):
    syms = []
    for line in open(path, encoding='utf-8'):
        if line.startswith('#') or not line.strip():
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        syms.append((int(parts[0], 16), parts[1], parts[2].strip()))
    return syms


def main():
    import capstone
    d = open(IMAGE, 'rb').read()
    syms = load_syms(SYMS)
    addr2name = {a: nm for a, _, nm in syms}
    sorted_addr = sorted(addr2name.keys())

    def nearest(a):
        i = bisect.bisect_right(sorted_addr, a) - 1
        if i < 0:
            return '?'
        base = sorted_addr[i]
        return addr2name[base] + (f"+0x{a-base:x}" if a > base else "")

    targets = [
        ('DSI_Start', 0xffffffc000476f64, 0xffffffc00047726c),
        ('DSI_Set_VM_CMD', 0xffffffc00047726c, 0xffffffc0004775ac),
        ('ddp_dsi_config', 0xffffffc0004775ac, 0xffffffc000478564),
        ('DSI_EnableVM_CMD', 0xffffffc000478564, 0xffffffc000478878),
        ('ddp_dsi_start', 0xffffffc00047a6d0, 0xffffffc00047aa40),
    ]

    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_LITTLE_ENDIAN)
    md.detail = True
    for name, va, end in targets:
        fo = va - VA_DELTA
        size = end - va
        print(f"\n===== {name} @ 0x{va:x} .. 0x{end:x} ({size} bytes) =====")
        off = 0
        for ins in md.disasm(d[fo:fo + size], va):
            ann = ''
            if ins.mnemonic in ('bl', 'b', 'cbz', 'cbnz') and ins.op_str.startswith('#'):
                try:
                    t = int(ins.op_str[1:], 16)
                    ann = '  ; -> ' + nearest(t)
                except ValueError:
                    pass
            # 标注构造大立即数（掩码/寄存器值）
            imm_hint = ''
            for op in ins.operands:
                if op.type == capstone.arm64.ARM64_OP_IMM:
                    v = op.imm
                    if v > 0xff:
                        imm_hint += f' [imm=0x{v:x}]'
            print(f"  0x{ins.address:x}: {ins.mnemonic:<9} {ins.op_str:<34}{ann}{imm_hint}")
            off += ins.size
        print(f"  (decoded {off} bytes)")


if __name__ == '__main__':
    main()
