#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修好电量恒为 0%（FGADC 路径把"读库仑计"的返回值当成电量了）。

实机证据链：
  1) /sys/class/power_supply/battery/capacity = 1（UI 下限钳位），actual SOC = 0
  2) dmesg（每 10 秒）：
        AvgVbat=(3734),AvgI=(5408),VChr=(4611),ZCV=(3731),T=(0),SOC=(0)
     电压/电流/充电电压都是真实值 → FGADC 驱动本身在正常工作
  3) 源码 drivers/power/mt81xx/mt6397_fgadc.c 里所有 bm 命令都是
        *(s32 *)data = 值;
        return 0;              ← 返回的是状态码，不是值
  4) 而 drivers/power/mt81xx/mt_battery_meter.c 却把"返回值"当电量：
        gFG_capacity = battery_meter_ctrl(BATTERY_METER_CMD_GET_HW_FG_CAR, &gFG_columb);
     → 电量恒等于 0（状态码）
  5) 同文件里真正的换算逻辑是 fgauge_read_capacity()：
        type=0 → fgauge_get_dod0()      按电压查 OCV 曲线表得初始 DOD
        type=1 → fgauge_update_dod()    DOD1 = DOD0 - (car*100)/Qmax
     而且源码里本来就留着一行被注释掉的正确写法：
        /*gFG_capacity = fgauge_read_capacity(0);*/

修法：保留 battery_meter_ctrl(...&gFG_columb) 作为"读 car 到缓冲区"的副作用调用，
      再用 fgauge_read_capacity() 把 car/电压换算成真正的百分比。
      三处：fgauge_initialization、fgauge_algo_run、fgauge_algo_run_init。

用法：
    python patch_battery_soc.py <内核树根> [--dry-run]
"""
import os
import re
import sys

TARGET = os.path.join("drivers", "power", "mt81xx", "mt_battery_meter.c")

CAR_CALL = "gFG_capacity_by_c = battery_meter_ctrl(BATTERY_METER_CMD_GET_HW_FG_CAR, &gFG_columb);"
CAR_CALL_INIT = "gFG_capacity = battery_meter_ctrl(BATTERY_METER_CMD_GET_HW_FG_CAR, &gFG_columb);"


def read_car_line():
    """读 car 到缓冲区的那一行（不带赋值）"""
    return "battery_meter_ctrl(BATTERY_METER_CMD_GET_HW_FG_CAR, &gFG_columb);"


def patch_text(src):
    """按函数分段做替换，避免同名行互相干扰"""
    out = src
    changes = []

    # ---- 1) fgauge_initialization：初始电量按 OCV 曲线换算 ----
    #
    # 注意：不能用 fgauge_read_capacity()！它从第 1250 行的 #if 0 一直屏蔽到
    # 第 1443 行（连同 fgauge_get_dod0 / fgauge_update_dod），源码里根本不参与
    # 编译 —— 作者那行 /*gFG_capacity = fgauge_read_capacity(0);*/ 是死代码。
    # 真正可用的是第 609 行的 fgauge_read_capacity_by_v()：按温度选 OCV 曲线表、
    # 插值出 DOD 再取 100-DOD，返回 0~100 的百分比，而且 fgauge_algo_run()
    # 里本来就在用它算 gFG_capacity_by_v（一直工作正常）。
    def fix_init(m):
        body = m.group(0)
        if CAR_CALL_INIT not in body:
            return body
        new = body.replace(
            CAR_CALL_INIT,
            "\t/* S3PRO: GET_HW_FG_CAR 返回的是状态码(0)，电量按 OCV 曲线换算 */\n"
            "\t\t" + read_car_line() + "\n"
            "\t\tgFG_capacity = fgauge_read_capacity_by_v(gFG_voltage);"
        )
        changes.append("fgauge_initialization")
        return new

    out = re.sub(r"void fgauge_initialization\(void\)\s*\{.*?\n\}", fix_init, out, flags=re.S)

    # ---- 2) fgauge_algo_run / fgauge_algo_run_init：复用已算好的电压法容量 ----
    #
    # 这两个函数里紧邻上面就有
    #     gFG_capacity_by_v = fgauge_read_capacity_by_v(...);
    # 所以直接把 gFG_capacity_by_c 指向它即可，不引入任何新依赖。
    def make_fixer(fname):
        def fix(m):
            body = m.group(0)
            if CAR_CALL not in body:
                return body
            new = body.replace(
                CAR_CALL,
                "\t/* S3PRO: 同理，直接用上面第 2 步已算好的电压法容量 */\n"
                "\t\t" + read_car_line() + "\n"
                "\t\tgFG_capacity_by_c = gFG_capacity_by_v;"
            )
            changes.append(fname)
            return new
        return fix

    out = re.sub(r"void fgauge_algo_run\(void\)\s*\{.*?\n\}",
                 make_fixer("fgauge_algo_run"), out, flags=re.S)
    out = re.sub(r"void fgauge_algo_run_init\(void\)\s*\{.*?\n\}",
                 make_fixer("fgauge_algo_run_init"), out, flags=re.S)

    # ---- 3) battery_meter_get_battery_temperature：改用 force_get_tbat() ----
    #
    # 原实现：
    #     int val = 0;                                       ← 这个参数是 ADC 采样次数
    #     battery_meter_ctrl(...GET_ADC_V_BAT_TEMP, &val);   ← 0 次采样 → 永远返回 0
    #     return val;                                        ← 而且 µV 原始值不是 °C
    # 同文件里的 force_get_tbat() 才是完整实现：
    #     设采样次数 → 读 ADC → IR 压降补偿 → BattVoltToTemp() 查 NTC 表
    # 实测证据：FG_g_fg_dbg_bat_temp = 0 → JEITA 判定"0~15°C" → 充电限流 512mA
    TEMP_OLD = re.compile(
        r"s32 battery_meter_get_battery_temperature\(void\)\s*\{.*?\n\}", re.S)

    def fix_temp(m):
        if "return force_get_tbat();" in m.group(0) and "S3PRO" in m.group(0):
            return m.group(0)
        changes.append("battery_meter_get_battery_temperature")
        return (
            "s32 battery_meter_get_battery_temperature(void)\n"
            "{\n"
            "\t/*\n"
            "\t * S3PRO: 原实现把 ADC 采样次数当 0 传进去（val = 0），ADC 直接返回 0；\n"
            "\t * 而且把 ADC 的 uV 原始值当温度返回。\n"
            "\t * 改用同文件里的 force_get_tbat()：设采样次数 -> 读 ADC ->\n"
            "\t * IR 压降补偿 -> BattVoltToTemp() 查 NTC 表换算成摄氏度。\n"
            "\t */\n"
            "\treturn force_get_tbat();\n"
            "}"
        )

    out, n_temp = TEMP_OLD.subn(fix_temp, out)
    if n_temp == 0:
        changes.append("温度函数未找到")

    # ---- 2.5) fgauge_read_capacity_by_v：修掉"永远返回 100%" ----
    #
    # 这是 SOC 恒为 100% 的真正根因。
    #
    # mt_bm_data.c 里：
    #     .temperature_t = 255,   /* This should be fixed, never change the value */
    #     .p_battery_profile_temperature = default_battery_profile_temperature,
    # 后者是一张【全 0 模板表】（75 个条目全是 {0,0}）。它本来就该是全 0 的，
    # 因为运行时要靠 fgauge_construct_battery_profile() 按当前温度把它填成真实曲线。
    #
    # 同文件的 fgauge_get_dod0() 就是这么用的：
    #     profile_p = fgauge_get_profile(p_bat_meter_data->temperature_t);
    #     fgauge_construct_battery_profile(temperature, profile_p);   ← 先填充
    #     ... 再按电压插值
    #
    # 但 fgauge_read_capacity_by_v() 【漏了填充这一步】，直接拿全 0 表用，
    # 于是第一句
    #     if (voltage > (profile_p + 0)->voltage)  return 100;
    # 里 profile_p[0].voltage == 0，对任何真实电压都成立 → 恒定返回 100%。
    #
    # 修法：照 fgauge_get_dod0() 的做法，取完表后先按当前温度填充。
    PROFILE_OLD = "\tprofile_p = fgauge_get_profile(p_bat_meter_data->temperature_t);"

    def fix_profile(text):
        idx = text.find("s32 fgauge_read_capacity_by_v(s32 voltage)")
        if idx < 0:
            return text, False
        end = text.find("s32 fgauge_read_v_by_capacity", idx)
        if end < 0:
            return text, False
        body = text[idx:end]
        if "S3PRO: 补上" in body:
            return text, False
        if PROFILE_OLD not in body:
            return text, False
        new_body = body.replace(
            PROFILE_OLD,
            "\t/* S3PRO: 补上 fgauge_get_dod0() 里有的那张\"按温度填充曲线表\"的步骤。\n"
            "\t * temperature_t(255) 指向的是全 0 模板表，不填充就会让本函数恒返回 100%。\n"
            "\t * 注意这里只能用 force_get_tbat()（本行上方已定义），\n"
            "\t * 不能用 battery_meter_get_battery_temperature()（定义在文件末尾，\n"
            "\t * 提前调用会触发 -Werror=implicit-function-declaration）。 */\n"
            "\tprofile_p = fgauge_get_profile(p_bat_meter_data->temperature_t);\n"
            "\tif (profile_p != NULL)\n"
            "\t\tfgauge_construct_battery_profile(force_get_tbat(), profile_p);",
            1)
        changes.append("fgauge_read_capacity_by_v(补上曲线表填充)")
        return text[:idx] + new_body + text[end:], True

    out, ok_prof = fix_profile(out)
    if not ok_prof:
        changes.append("fgauge_read_capacity_by_v 未匹配")

    # ---- 2.6) 给 fgauge_construct_battery_profile 补前置声明 ----
    #
    # 它的定义在行 811 附近，而我们在行 615 就调用它了。
    # 不加声明会触发 -Werror=implicit-function-declaration 编译失败。
    # 插在 battery_meter_ctrl 那个函数指针声明之后（文件前部）。
    DECL_ANCHOR = "static BATTERY_METER_CONTROL battery_meter_ctrl;"
    DECL_LINE = (
        "\n"
        "/* S3PRO: fgauge_construct_battery_profile 定义在文件后半部分，\n"
        " * 而 fgauge_read_capacity_by_v() 在前面就要调用它，这里补前置声明。 */\n"
        "void fgauge_construct_battery_profile(s32 temperature,\n"
        "\t\t\t\t      struct BATTERY_PROFILE_STRUCT *temp_profile_p);"
    )

    def add_decl(text):
        if "S3PRO: fgauge_construct_battery_profile 定义在文件后半部分" in text:
            return text, False
        if DECL_ANCHOR not in text:
            return text, False
        return text.replace(DECL_ANCHOR,
                            DECL_ANCHOR + DECL_LINE, 1), True

    out, ok_decl = add_decl(out)
    if ok_decl:
        changes.append("fgauge_construct_battery_profile 前置声明")
    else:
        changes.append("前置声明未添加")

    # ---- 3) battery_meter_initial：gFG_Is_Init 必须提前置位 ----
    #
    # 原顺序：
    #     fgauge_initialization();
    #     fgauge_algo_run_init();    ← 里面调 battery_meter_get_battery_voltage()，
    #                                  该函数开头 `if (gFG_Is_Init == false) return 0;`
    #                                  → 读到 0 → capacity_by_v(0) 被钳到 100
    #     gFG_Is_Init = true;        ← 太晚了
    # 后果：开机 dod_init() 里
    #     if (abs(cap_by_v_init - cap_by_v) > 5) cap_by_v = cap_by_v_init;
    # 用错误的 100 覆盖掉真值。实机日志：dod_init: 3903, 100, 100, 33
    INIT_ANCHOR = "\tfgauge_initialization();"

    def fix_init_flag(text):
        idx = text.find("s32 battery_meter_initial(void)")
        if idx < 0:
            return text, False
        end = text.find("s32 battery_meter_reset", idx)
        if end < 0:
            return text, False
        body = text[idx:end]
        if "S3PRO: 必须先置位" in body:
            return text, False
        if INIT_ANCHOR not in body:
            return text, False

        # 1) 删掉后面那句单独的 gFG_Is_Init = true;
        body2 = body.replace("\n\tgFG_Is_Init = true;\n", "\n", 1)
        if body2 == body:
            return text, False

        # 2) 在 fgauge_initialization() 前面插入置位语句
        ins = (
            "\t/* S3PRO: 必须先置位，否则 fgauge_algo_run_init() 里\n"
            "\t * battery_meter_get_battery_voltage() 会因 gFG_Is_Init==false\n"
            "\t * 直接返回 0，容量被 fgauge_read_capacity_by_v(0) 钳成 100%。 */\n"
            "\tgFG_Is_Init = true;\n\n"
        )
        body2 = body2.replace(INIT_ANCHOR, ins + INIT_ANCHOR, 1)
        changes.append("battery_meter_initial(Is_Init 顺序)")
        return text[:idx] + body2 + text[end:], True

    out, ok = fix_init_flag(out)
    if not ok:
        changes.append("battery_meter_initial 未匹配")

    return out, changes


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    tree = sys.argv[1]
    dry = "--dry-run" in sys.argv

    path = os.path.join(tree, TARGET)
    if not os.path.isfile(path):
        raise SystemExit("找不到 %s" % path)

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        src = f.read()

    if "S3PRO: GET_HW_FG_CAR 返回的是状态码" in src:
        print("[ok]  已打过补丁，跳过")
        return

    new, changes = patch_text(src)
    bad = [x for x in changes if "未找到" in x or "未匹配" in x]
    if len(changes) != 7 or bad:
        raise SystemExit(
            "只匹配到 %d/7 处（%s），源码结构和预期不符，中止"
            % (len(changes), ", ".join(changes)))

    if dry:
        print("[dry] 会修改 7 处：%s" % ", ".join(changes))
        return

    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    print("[ok]  电量补丁完成：%s" % ", ".join(changes))


if __name__ == "__main__":
    main()
