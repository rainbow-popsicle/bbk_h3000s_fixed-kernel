import io

PATH = "scripts/check-bootimg.ps1"

old = (
    'Write-Output ("kernel          : off={0} size={1} 格式={2}{3}" -f $kernelOff, $kernelSize, $kernelKind, $mtkNote)\r\n'
    'Write-Output ("ramdisk         : off={0} size={1}" -f ([math]::Ceiling(($kernelOff + $kernelSize) / $pageSize) * $pageSize), $ramdiskSize)'
)
new = (
    'Write-Output ("kernel          : off={0} size={1} 格式={2}{3}" -f $kernelOff, $kernelSize, $kernelKind, $mtkNote)\r\n'
    '\r\n'
    '# ramdisk 位置 = 内核之后第一个页边界；头部格式能反证布局推断对不对\r\n'
    '$ramdiskOff = [math]::Ceiling(($kernelOff + $kernelSize) / $pageSize) * $pageSize\r\n'
    "$ramdiskKind = '未知'\r\n"
    'if ($ramdiskSize -gt 0 -and ($ramdiskOff + 6) -le $data.Length) {\r\n'
    '    $head6 = Get-Ascii $data $ramdiskOff 6\r\n'
    "    if ($head6 -eq '070701' -or $head6 -eq '070702') { $ramdiskKind = 'cpio' }\r\n"
    '    elseif ($data[$ramdiskOff] -eq 0x1f -and $data[$ramdiskOff + 1] -eq 0x8b) { $ramdiskKind = 'gzip' }\r\n'
    '}\r\n'
    'Write-Output ("ramdisk         : off={0} size={1} 格式={2}" -f $ramdiskOff, $ramdiskSize, $ramdiskKind)'
)

with open(PATH, "r", encoding="utf-8-sig", newline="") as f:
    text = f.read()

if old not in text:
    # 允许文件用成了 \n 换行
    old = old.replace("\r\n", "\n")
    new = new.replace("\r\n", "\n")
    if old not in text:
        raise SystemExit("找不到目标片段，未修改")

text = text.replace(old, new, 1)

with open(PATH, "w", encoding="utf-8-sig", newline="") as f:
    f.write(text)

raw = open(PATH, "rb").read()
print("写入完成，前 3 字节:", " ".join("%02X" % b for b in raw[:3]))
