<#
.SYNOPSIS
    boot.img 体检脚本（Windows 原生，不需要安装 Python）

.DESCRIPTION
    读原厂 boot.img，打印决定后续路线的几项信息：
      - 内核版本 release（决定选哪棵源码树）
      - 内核格式（gzip / arm64-image ...，决定新内核要不要压缩）
      - page_size、内核段偏移、是否带 MTK 私有头
      - 内嵌 .config 是否存在
    与 scripts/bootimg.py 的 info 输出一一对应，两边结果已做过交叉核对。

    兼容 Windows PowerShell 5.1 与 PowerShell 7。

.PARAMETER Image
    原厂 boot.img 路径。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\check-bootimg.ps1 inputs\boot.img
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Image
)

$ErrorActionPreference = 'Stop'

function Read-U32 {
    param([byte[]]$Buffer, [int]$Offset)
    return [BitConverter]::ToUInt32($Buffer, $Offset)
}

function Get-Ascii([byte[]]$Buffer, [int]$Offset, [int]$Length) {
    if ($Offset + $Length -gt $Buffer.Length) { $Length = $Buffer.Length - $Offset }
    return [System.Text.Encoding]::ASCII.GetString($Buffer, $Offset, $Length)
}

function Get-CString([byte[]]$Buffer, [int]$Offset, [int]$Length) {
    $raw = Get-Ascii $Buffer $Offset $Length
    $nul = $raw.IndexOf([char]0)
    if ($nul -ge 0) { $raw = $raw.Substring(0, $nul) }
    return $raw.Trim()
}

function Get-KernelKind {
    param([byte[]]$Buffer, [int]$Offset)
    if ($Buffer.Length -lt $Offset + 64) { return $null }
    if ($Buffer[$Offset] -eq 0x1f -and $Buffer[$Offset + 1] -eq 0x8b) { return 'gzip' }
    if ($Buffer[$Offset] -eq 0x04 -and $Buffer[$Offset + 1] -eq 0x22 -and
        $Buffer[$Offset + 2] -eq 0x4d -and $Buffer[$Offset + 3] -eq 0x18) { return 'lz4' }
    if ($Buffer[$Offset] -eq 0x28 -and $Buffer[$Offset + 1] -eq 0xb5 -and
        $Buffer[$Offset + 2] -eq 0x2f -and $Buffer[$Offset + 3] -eq 0xfd) { return 'zstd' }
    if ((Get-Ascii $Buffer ($Offset + 56) 4) -eq 'ARMd') { return 'arm64-image' }
    if ((Read-U32 $Buffer ($Offset + 36)) -eq 0x016F2818) { return 'arm-zimage' }
    return $null
}

function Expand-GzipBuffer {
    param([byte[]]$Buffer, [int]$Offset)
    $len = $Buffer.Length - $Offset
    if ($len -le 0) { return $null }
    $slice = New-Object byte[] $len
    [Array]::Copy($Buffer, $Offset, $slice, 0, $len)
    $ms = New-Object System.IO.MemoryStream (,$slice)
    try {
        $gz = New-Object System.IO.Compression.GZipStream($ms, [System.IO.Compression.CompressionMode]::Decompress)
    } catch {
        return $null
    }
    $out = New-Object System.IO.MemoryStream
    try {
        $gz.CopyTo($out)
    } catch {
        $gz.Dispose()
        return $null
    }
    $gz.Dispose()
    return $out.ToArray()
}

function Expand-RawDeflate {
    param([byte[]]$Buffer, [int]$Offset, [int]$Length)
    if ($Length -le 0) { return $null }
    $slice = New-Object byte[] $Length
    [Array]::Copy($Buffer, $Offset, $slice, 0, $Length)
    $ms = New-Object System.IO.MemoryStream (,$slice)
    $ds = New-Object System.IO.Compression.DeflateStream($ms, [System.IO.Compression.CompressionMode]::Decompress)
    $out = New-Object System.IO.MemoryStream
    try {
        $ds.CopyTo($out)
    } catch {
        $ds.Dispose()
        return $null
    }
    $ds.Dispose()
    return $out.ToArray()
}

# --------------------------------------------------------------------------- #
$resolved = (Resolve-Path -LiteralPath $Image).Path
$data = [System.IO.File]::ReadAllBytes($resolved)
Write-Output ("文件            : {0} ({1} 字节)" -f $resolved, $data.Length)

if ($data.Length -lt 1632) { throw "文件太小，不像 boot.img" }
if ((Get-Ascii $data 0 8) -ne 'ANDROID!') { throw "没有 ANDROID! magic，前 8 字节是 $(Get-Ascii $data 0 8)" }

$kernelSize   = Read-U32 $data 8
$ramdiskSize  = Read-U32 $data 16
$pageSize     = Read-U32 $data 36
$headerVer    = Read-U32 $data 40
$name         = Get-CString $data 48 16
$cmdline      = Get-CString $data 64 512

if ($headerVer -gt 3) { $headerVer = 0 }
if ($pageSize -lt 512 -or $pageSize -gt 65536 -or ($pageSize -band ($pageSize - 1)) -ne 0) {
    $pageSize = 2048
}

# 找内核偏移：AOSP 布局 / MTK 布局（页边界后额外 512 字节头）
$kernelOff = -1
$kernelKind = $null
$mtkHeader = $false
$candidates = @(
    @($pageSize, 0), @($pageSize, 512),
    @(512, 0), @(512, 512),
    @(1024, 0), @(1024, 512),
    @(4096, 0), @(4096, 512)
)
foreach ($cand in $candidates) {
    $base = $cand[0]
    $extra = $cand[1]
    $off = $base + $extra
    if ($off + 64 -gt $data.Length) { continue }
    if ($extra -gt 0) {
        if ((Get-Ascii $data $base 16) -ne 'MTK_BLOADER_INFO') { continue }
    } else {
        if ((Get-Ascii $data $base 16) -eq 'MTK_BLOADER_INFO') { continue }
    }
    $kind = Get-KernelKind $data $off
    if ($kind) {
        $kernelOff = $off
        $kernelKind = $kind
        $mtkHeader = ($extra -gt 0)
        break
    }
}

Write-Output ("header 版本     : v{0}" -f $headerVer)
Write-Output ("page_size       : {0}" -f $pageSize)
Write-Output ("name            : '{0}'" -f $name)
Write-Output ("cmdline         : '{0}'" -f $cmdline)

if ($kernelOff -lt 0) {
    Write-Output "错误            : 按 AOSP / MTK 两种布局都校验不出内核 magic，无法继续"
    exit 2
}

$mtkNote = ''
if ($mtkHeader) { $mtkNote = '  [前置 MTK 头]' }
Write-Output ("kernel          : off={0} size={1} 格式={2}{3}" -f $kernelOff, $kernelSize, $kernelKind, $mtkNote)
Write-Output ("ramdisk         : off={0} size={1}" -f ([math]::Ceiling(($kernelOff + $kernelSize) / $pageSize) * $pageSize), $ramdiskSize)

# 版本字符串 / 内嵌 .config
$kernelLen = [math]::Min($kernelSize, $data.Length - $kernelOff)
$kblob = New-Object byte[] $kernelLen
[Array]::Copy($data, $kernelOff, $kblob, 0, $kernelLen)

$searchBuf = $kblob
if ($kernelKind -eq 'gzip') {
    $unpacked = Expand-GzipBuffer $kblob 0
    if ($unpacked) { $searchBuf = $unpacked }
}

$latin1 = [System.Text.Encoding]::GetEncoding(28591)
$text = $latin1.GetString($searchBuf)

$versionText = '（没找到，可能被压缩或已剥离）'
$idx = $text.IndexOf('Linux version ')
if ($idx -ge 0) {
    $end = $text.IndexOf([char]0, $idx)
    if ($end -lt 0) { $end = [math]::Min($idx + 200, $text.Length) }
    $versionText = $text.Substring($idx, $end - $idx)
}
Write-Output ("内核版本字符串  : {0}" -f $versionText)

$cfgState = '没有（CONFIG_IKCONFIG 未开启）'
$cfgIdx = $text.IndexOf('IKCFG_ST')
if ($cfgIdx -ge 0) {
    $payloadStart = $cfgIdx + 8
    $searchTo = [math]::Min($text.Length, $payloadStart + (4 * 1024 * 1024))
    $edIdx = $text.IndexOf('IKCFG_ED', $payloadStart)
    if ($edIdx -lt 0) { $edIdx = $searchTo }
    $payloadLen = $edIdx - $payloadStart
    $cfgBytes = $null
    $z = Expand-RawDeflate $searchBuf ($payloadStart + 2) ($payloadLen - 2)
    if ($z -and ([System.Text.Encoding]::ASCII.GetString($z, 0, [math]::Min(64, $z.Length))).Contains('CONFIG')) {
        $cfgBytes = $z
    } else {
        $z = Expand-RawDeflate $searchBuf $payloadStart $payloadLen
        if ($z -and ([System.Text.Encoding]::ASCII.GetString($z, 0, [math]::Min(64, $z.Length))).Contains('CONFIG')) {
            $cfgBytes = $z
        }
    }
    if ($cfgBytes) {
        $cfgText = $latin1.GetString($cfgBytes)
        $lines = ($cfgText -split "`n" | Where-Object { $_ -match '^CONFIG_' }).Count
        $cfgState = ("有，{0} 行 CONFIG_*" -f $lines)
    } else {
        $cfgState = '有 IKCFG 标记但解压失败（格式待确认）'
    }
}
Write-Output ("内嵌 .config    : {0}" -f $cfgState)

Write-Output ''
Write-Output '结论提示：'
if ($versionText -match 'Linux version (\d+\.\d+)') {
    $majorMinor = $Matches[1]
    if ($majorMinor -eq '3.18') {
        Write-Output '  公开的 MT8176 源码树（cappu 3.18.123 / mt8176_common 3.18.35）版本段对得上，可以试着自编。'
    } elseif ($majorMinor -eq '4.4') {
        Write-Output '  MT8176 没有公开的 4.4 源码树，自编需要跨平台移植，不建议作为第一步。'
    } else {
        Write-Output ("  内核 {0} 不在已知 MT8176 源码范围内，需要另找树。" -f $majorMinor)
    }
} else {
    Write-Output '  没读到内核版本，先确认这个文件确实是 boot 分区镜像。'
}
