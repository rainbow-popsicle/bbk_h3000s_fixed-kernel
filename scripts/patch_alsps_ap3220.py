#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 cm36686 驱动改造成能驱动本机 AP3220（原厂驱动内部名 ap3xx6）。

=== 为什么这样做 ===
本机光感是 AP3220，原厂 7.0 内核里编的是 ap3xx6 驱动；我们这棵 3.18.119
树里没有它，只有一系列别的 ALS 芯片驱动。cm36686 和 AP3220 都是
Capella 系 ALS/PS，alsps 核心层接口完全一样，所以最小改动 = 把 cm36686
驱动的寄存器层换成 AP3220 的语义，Kconfig/Makefile 一个字都不用动。

=== 依据（全部来自原厂 7.0 内核反汇编 + 实机验证，不是猜测）===
原厂内核解压后是 raw arm64 Image，用 llvm-objdump 反汇编 ap3xx6 驱动：

  ap3xx6_write_reg @0x8380f30:
      buf[0]=reg; buf[1]=val; i2c_master_send(client, buf, 2)
  ap3xx6_read_reg  @0x83810c4:
      i2c_master_send(client, &reg, 1); i2c_master_recv(client, &v, 1)
      → 没有任何 I2C_WR_FLAG / I2C_RS_FLAG！
  ap3xx6_enable_als @0x83811c8:
      v = read_reg(0x00); orr v,#0x1 → write_reg(0x00, v); msleep(30)
  ap3xx6_enable_ps  @0x8381300/0x8381350:
      v = read_reg(0x00); orr v,#0x2 → write_reg(0x00, v)
  ap3xx6_read_als   @0x83816f0:
      lo = read_reg(0x0C); hi = read_reg(0x0D); val = lo | (hi << 8)
  ap3xx6_read_ps    @0x83817ac:
      lo = read_reg(0x0E); hi = read_reg(0x0F); val = lo | (hi << 8)

实机验证（裸 syscall 探针，root 下直接读写 /dev/i2c-8@0x1C）：
  reg0x00 |= 0x01 后，0x0C/0x0D 从恒 0 变成持续变化
  24 秒采样 57 次：0x11 0x12 0x13 0x14 0x17 0x19 ... 手电筒直照时
  出现 0x6F24(28452) / 0x5E99(24217) —— 光感确实在工作

=== 原 cm36686 驱动有 4 个不兼容点 ===
1) 读路径用 MTK I2C 扩展位（addr |= 0x1000|0x0800）。本平台没开
   CONFIG_MTK_I2C_EXTENSION，i2c-mt65xx 直接 addr<<1 → 0x181c，
   从机地址变成垃圾 → 所有读必然失败。
2) 使能位语义相反：CM36686 的 reg0x00 bit0 是 shutdown（清 0 才开），
   AP3220 的 bit0 是 enable（置 1 才开）。
3) 数据寄存器不同：CM36686 用 0x08/0x09，AP3220 用 0x0E/0x0C。
4) 写寄存器宽度不同：CM36686 写 3 字节 {reg,lo,hi}（16 位寄存器），
   AP3220 写 2 字节 {reg,val}（8 位寄存器）。用 3 字节写会把值打到
   相邻寄存器上。

用法：
    python patch_alsps_ap3220.py <内核树根> [--dry-run]
"""
import os
import re
import sys

TREE_C = os.path.join("drivers", "misc", "mediatek", "alsps",
                      "cm36686-new", "cm36686.c")
TREE_H = os.path.join("drivers", "misc", "mediatek", "alsps",
                      "cm36686-new", "cm36686.h")

MARKER = "AP3220-REMAP"

# ---------------------------------------------------------------- 1) I2C 读
I2C_OLD = """\tcase I2C_FLAG_READ:
\t\tclient->addr &= I2C_MASK_FLAG;
\t\tclient->addr |= I2C_WR_FLAG;
\t\tclient->addr |= I2C_RS_FLAG;
\t\tres = i2c_master_send(client, buf, count);
\t\tclient->addr &= I2C_MASK_FLAG;
\t\tbreak;"""

I2C_NEW = """\tcase I2C_FLAG_READ: {
\t\t/* AP3220-REMAP: 原来的写法依赖 CONFIG_MTK_I2C_EXTENSION
\t\t * （addr |= I2C_WR_FLAG|I2C_RS_FLAG，让 MTK i2c 驱动解析
\t\t * “先写寄存器号再读数据”）。本平台没开这个选项，
\t\t * i2c-mt65xx.c 里是 addr_reg = msgs->addr << 1，不做任何掩码，
\t\t * 于是 0x1C|0x1000|0x0800 = 0x181C 被当成从机地址 → 必然 NAK，
\t\t * 所有读操作都会失败。
\t\t * 改成显式 send+recv：count 低字节=要发的字节数(寄存器号)，
\t\t * 高字节=要读的字节数（调用点都是 0x201 / 0x101）。 */
\t\tu8 wlen = (u8)(count & 0xff);
\t\tu8 rlen = (u8)((count >> 8) & 0xff);
\t\tu8 rbuf[8];

\t\tif (rlen > sizeof(rbuf))
\t\t\trlen = sizeof(rbuf);
\t\tres = 0;
\t\tif (wlen > 0) {
\t\t\tres = i2c_master_send(client, buf, wlen);
\t\t\tif (res <= 0)
\t\t\t\tbreak;
\t\t}
\t\tres = i2c_master_recv(client, rbuf, rlen);
\t\tif (res > 0) {
\t\t\tmemcpy((void *)buf, rbuf, rlen);
\t\t\tres = rlen;
\t\t}
\t\tbreak;
\t}"""

# ---------------------------------------------------------------- 2) enable_ps
PS_NEW = """int cm36686_enable_ps(struct i2c_client *client, int enable)
{
\tstruct cm36686_priv *obj = i2c_get_clientdata(client);
\tint res;
\tu8 databuf[2];
\tu8 v;

\t/* AP3220-REMAP: PS 使能 = reg0x00 的 bit1
\t * （原厂 ap3xx6_enable_ps: and/orr w2,w2,#0xfffffffd / #0x2 @0x8381300/0x8381350） */
\tdatabuf[0] = 0x00;
\tres = CM36686_i2c_master_operate(client, databuf, 0x101, I2C_FLAG_READ);
\tif (res < 0) {
\t\tAPS_ERR("ps: read enable reg failed\\n");
\t\treturn res;
\t}
\tv = databuf[0];
\tif (enable == 1)
\t\tv |= 0x02;
\telse
\t\tv &= (u8)~0x02;

\tdatabuf[0] = 0x00;
\tdatabuf[1] = v;
\tres = CM36686_i2c_master_operate(client, databuf, 0x2, I2C_FLAG_WRITE);
\tif (res < 0) {
\t\tAPS_ERR("ps: write enable reg failed\\n");
\t\treturn res;
\t}

\tif (enable == 1) {
\t\tatomic_set(&obj->ps_deb_on, 1);
#ifdef CONFIG_64BIT
\t\tatomic64_set(&obj->ps_deb_end,
\t\t\t     jiffies + atomic_read(&obj->ps_debounce) / (1000 / HZ));
#else
\t\tatomic_set(&obj->ps_deb_end,
\t\t\t   jiffies + atomic_read(&obj->ps_debounce) / (1000 / HZ));
#endif
\t\tintr_flag = 1;	/* reset hw status to away after enable. */
\t} else {
\t\tatomic_set(&obj->ps_deb_on, 0);
\t}

\treturn 0;
}"""

# ---------------------------------------------------------------- 3) enable_als
ALS_NEW = """int cm36686_enable_als(struct i2c_client *client, int enable)
{
\tstruct cm36686_priv *obj = i2c_get_clientdata(client);
\tint res;
\tu8 databuf[2];
\tu8 v;

\t/* AP3220-REMAP: ALS 使能 = reg0x00 的 bit0（1=开）
\t * 注意与 CM36686 相反：CM36686 的 bit0 是 shutdown，清 0 才开。
\t * 原厂 ap3xx6_enable_als: orr w2,w2,#0x1 @0x8381234，之后 msleep(30)。 */
\tdatabuf[0] = 0x00;
\tres = CM36686_i2c_master_operate(client, databuf, 0x101, I2C_FLAG_READ);
\tif (res < 0) {
\t\tAPS_ERR("als: read enable reg failed\\n");
\t\treturn res;
\t}
\tv = databuf[0];
\tif (enable == 1)
\t\tv |= 0x01;
\telse
\t\tv &= (u8)~0x01;

\tdatabuf[0] = 0x00;
\tdatabuf[1] = v;
\tres = CM36686_i2c_master_operate(client, databuf, 0x2, I2C_FLAG_WRITE);
\tif (res < 0) {
\t\tAPS_ERR("als: write enable reg failed\\n");
\t\treturn res;
\t}

\tif (enable == 1) {
\t\tmsleep(30);
\t\tatomic_set(&obj->als_deb_on, 1);
#ifdef CONFIG_64BIT
\t\tatomic64_set(&obj->als_deb_end,
\t\t\t     jiffies + atomic_read(&obj->als_debounce) / (1000 / HZ));
#else
\t\tatomic_set(&obj->als_deb_end,
\t\t\t   jiffies + atomic_read(&obj->als_debounce) / (1000 / HZ));
#endif
\t} else {
\t\tatomic_set(&obj->als_deb_on, 0);
\t}

\treturn 0;
}"""

# ---------------------------------------------------------------- 4) init_client
INIT_NEW = """static int cm36686_init_client(struct i2c_client *client)
{
\tu8 databuf[2];
\tu8 v;
\tint res = 0;

\tAPS_FUN();

\t/* AP3220-REMAP: 初始化只做一件事 —— 保证 ALS/PS 初始为关。
\t * 原来那段是 CM36686 的 16 位寄存器配置（写 {reg,lo,hi} 三字节到
\t * 0x00/0x03/0x04/0x05），对 AP3220 会打到相邻寄存器上，必须去掉。 */
\tdatabuf[0] = 0x00;
\tres = CM36686_i2c_master_operate(client, databuf, 0x101, I2C_FLAG_READ);
\tif (res < 0) {
\t\tAPS_ERR("init: read enable reg failed\\n");
\t\tgoto EXIT_ERR;
\t}
\tv = databuf[0] & (u8)~0x03;

\tdatabuf[0] = 0x00;
\tdatabuf[1] = v;
\tres = CM36686_i2c_master_operate(client, databuf, 0x2, I2C_FLAG_WRITE);
\tif (res <= 0) {
\t\tAPS_ERR("init: write enable reg failed\\n");
\t\tgoto EXIT_ERR;
\t}
\tAPS_LOG("AP3220 init done, enable reg = 0x%x\\n", v);

\t/* EINT 建不起来不致命：ALS/PS 都走 alsps 核心层的定时器轮询 */
\tres = cm36686_setup_eint(client);
\tif (res != 0)
\t\tAPS_LOG("setup eint failed(%d), continue in polling mode\\n", res);

\treturn CM36686_SUCCESS;

EXIT_ERR:
\tAPS_ERR("init dev: %d\\n", res);
\treturn res;
}"""


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    path = os.path.join(tree, TREE_C)
    hpath = os.path.join(tree, TREE_H)
    if not os.path.isfile(path):
        raise SystemExit("找不到 %s" % path)

    src = open(path, "r", encoding="utf-8", errors="replace").read()
    if MARKER in src:
        print("[ok]  AP3220 重映射补丁已打过，跳过")
        return

    changes = []

    # --- 1) I2C 读路径 ---
    if I2C_OLD not in src:
        raise SystemExit("I2C_FLAG_READ 段没匹配上，中止以免改错")
    src = src.replace(I2C_OLD, I2C_NEW, 1)
    changes.append("I2C 读路径(send+recv)")

    # --- 2) enable_ps ---
    pat_ps = re.compile(
        r"int cm36686_enable_ps\(struct i2c_client \*client, int enable\)\n"
        r"\{.*?\n\treturn res;\n\}", re.S)
    if not pat_ps.search(src):
        raise SystemExit("cm36686_enable_ps 结构不符，中止")
    src = pat_ps.sub(lambda m: PS_NEW, src, count=1)
    changes.append("enable_ps(bit1)")

    # --- 3) enable_als ---
    pat_als = re.compile(
        r"int cm36686_enable_als\(struct i2c_client \*client, int enable\)\n"
        r"\{.*?\n\treturn res;\n\}", re.S)
    if not pat_als.search(src):
        raise SystemExit("cm36686_enable_als 结构不符，中止")
    src = pat_als.sub(lambda m: ALS_NEW, src, count=1)
    changes.append("enable_als(bit0)")

    # --- 4) init_client ---
    pat_init = re.compile(
        r"static int cm36686_init_client\(struct i2c_client \*client\)\n"
        r"\{.*?\nEXIT_ERR:\n\tAPS_ERR\(\"init dev: %d\\n\", res\);\n"
        r"\treturn res;\n\}", re.S)
    if not pat_init.search(src):
        raise SystemExit("cm36686_init_client 结构不符，中止")
    src = pat_init.sub(lambda m: INIT_NEW, src, count=1)
    changes.append("init_client(2字节写)")

    # --- 5) 数据寄存器（改头文件）---
    if not os.path.isfile(hpath):
        raise SystemExit("找不到 %s" % hpath)
    hsrc = open(hpath, "r", encoding="utf-8", errors="replace").read()
    h_old = hsrc
    hsrc = re.sub(r"(#define\s+CM36686_REG_PS_DATA\s+)0[xX]08",
                  r"\g<1>0X0E", hsrc)
    hsrc = re.sub(r"(#define\s+CM36686_REG_ALS_DATA\s+)0[xX]09",
                  r"\g<1>0X0C", hsrc)
    if hsrc == h_old:
        raise SystemExit("寄存器宏没改成（PS_DATA/ALS_DATA），中止")
    changes.append("数据寄存器(0x0C/0x0E)")

    if dry:
        print("[dry] 会改：%s" % ", ".join(changes))
        return

    open(path, "w", encoding="utf-8").write(src)
    open(hpath, "w", encoding="utf-8").write(hsrc)
    print("[ok]  AP3220 重映射完成：%s" % ", ".join(changes))


if __name__ == "__main__":
    main()
