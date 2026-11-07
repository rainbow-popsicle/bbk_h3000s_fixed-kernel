import re, zlib, collections

k = open(r"work\real-parts\kernel.gz", "rb").read()
d = zlib.decompressobj(31)
img = d.decompress(k)
img += d.flush()
print("Image: %d 字节" % len(img))

# 所有可打印 ASCII 串
STR_RE = re.compile(rb"[\x20-\x7e]{5,80}")
strings = [m.group(0).decode("latin-1") for m in STR_RE.finditer(img)]
print("可打印字符串: %d 条\n" % len(strings))

GROUPS = {
    "触摸 IC": ("gt9", "GT9", "ft5", "FT5", "nt3", "NT3", "synaptics", "Synaptics",
                "himax", "Himax", "goodix", "Goodix", "chipone", "Chipone",
                "ilitek", "ILITEK", "melfas", "Melfas", "elan", "Elan",
                "tpd", "TPD", "touch"),
    "屏/LCM": ("lcm", "LCM", "dsi_vdo", "dsi_cmd", "video_mode", "cmd_mode",
               "nt36", "jd9", "ili9881", "hx839", "otm", "st7703", "ek79",
               "panel", "Panel", "PANEL", "lcd", "LCD"),
    "传感器": ("accel", "gyro", "alsps", "msensor", "stk3", "bmi", "bma", "lsm"),
}

seen = set()
for group, keys in GROUPS.items():
    hits = collections.Counter()
    for s in strings:
        if any(key in s for key in keys):
            hits[s.strip()] += 1
    print("=" * 72)
    print("%s —— %d 种不同字符串" % (group, len(hits)))
    print("=" * 72)
    for s, n in sorted(hits.items(), key=lambda kv: (-kv[1], kv[0])):
        if s in seen:
            continue
        seen.add(s)
        print("  %4d×  %s" % (n, s))
    print()
