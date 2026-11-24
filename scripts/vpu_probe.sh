#!/system/bin/sh
# vpu_probe.sh —— MT8173 硬解（VPU）运行时诊断
#
# 目的：区分"VPU 没时钟/没上电"和"VPU 起来了但 MVA 映射失败"这两类根因。
#       之前的 enable_count=0 是空闲时测的，不能作数——必须【播放中】采样。
#
# 用法（设备上，root）：
#   adb push vpu_probe.sh /sdcard/ && adb shell sh /sdcard/vpu_probe.sh watch
#
# 流程：
#   1. 先看空闲基线
#   2. 提示你用 VLC 开始播放（或你自己 am start）
#   3. 连续采样 20 秒：VDEC 时钟使能计数、VDEC 中断计数、M4U 占用
#   4. 抓 logcat 里 OMX/MVA/ACodec 的关键行
#   5. 结果打印到屏幕并存到 /sdcard/vpu_probe_<时间>.txt

OUT="/sdcard/vpu_probe_$(date +%H%M%S).txt"

log() { echo "$@" | tee -a "$OUT"; }

read_clk() {
    # $1=时钟名  $2=字段
    for base in /sys/kernel/debug/clk; do
        [ -d "$base/$1" ] || continue
        case "$2" in
            en)   cat "$base/$1/clk_enable_count" 2>/dev/null ;;
            rate) cat "$base/$1/clk_rate" 2>/dev/null ;;
        esac
    done
}

read_irq() {
    # VDEC 中断：MT8173 SPI 204；名字可能是 Vcodec / vdec / 260 等
    grep -iE "vdec|vcodec|mtk_vcodec" /proc/interrupts 2>/dev/null | head -3
}

m4u_state() {
    # M4U/IOMMU 的 MVA 分配情况
    for d in /sys/kernel/debug/m4u /sys/kernel/debug/mtk_iommu /sys/kernel/debug/iommu; do
        [ -e "$d" ] || continue
        echo "[$d]"
        ls "$d" 2>/dev/null | head -8
    done
    # m4u 有时挂在 proc 下
    for f in /proc/m4u /proc/mtk_m4u; do
        [ -e "$f" ] && { echo "[$f]"; head -30 "$f" 2>/dev/null; }
    done
}

log "=============================================="
log " MT8173 VPU 硬解诊断  $(date)"
log "=============================================="
log ""
log "--- 设备信息 ---"
log "build: $(getprop ro.build.display.id 2>/dev/null)"
log "hw   : $(getprop ro.hardware 2>/dev/null) / $(getprop ro.board.platform 2>/dev/null)"
log "gralloc: $(ls /system/vendor/lib/hw/gralloc* 2>/dev/null | tr '\n' ' ')"
log ""

log "--- VDEC 时钟树（空闲基线）---"
for c in vcodecpll vcodecpll_ck vdec_sel vdec_cken vdec_larb_cken vcodecpll_370p5; do
    log "  $c: enable=$(read_clk $c en) rate=$(read_clk $c rate)"
done
log ""
log "--- VDEC 中断（空闲基线）---"
log "$(read_irq)"
log ""

# 清日志便于只看播放期间的
logcat -c 2>/dev/null

log "=========================================================="
log ">>> 现在请在 VLC 里开始播放那个 4K 视频（完全加速打开）<<<"
log ">>> 脚本会连续采样 20 秒，请保持播放                    <<<"
log "=========================================================="
log ""

i=0
while [ $i -lt 40 ]; do
    ts=$(date +%H:%M:%S)
    ck=$(read_clk vdec_cken en)
    cs=$(read_clk vdec_sel en)
    cp=$(read_clk vcodecpll en)
    irq=$(grep -icE "vdec|vcodec" /proc/interrupts 2>/dev/null)
    irqline=$(grep -iE "vdec|vcodec" /proc/interrupts 2>/dev/null | head -1 | tr -s ' ' ' ')
    log "$ts  cken=$ck sel=$cs pll=$cp  irq_lines=$irq  $irqline"
    i=$((i + 1))
    sleep 0.5
done

log ""
log "--- 播放期间 M4U / IOMMU 状态 ---"
m4u_state | tee -a "$OUT"
log ""

log "--- ION 占用（播放中）---"
for h in /sys/kernel/debug/ion/heaps/*; do
    [ -f "$h" ] || continue
    n=$(basename "$h")
    case "$n" in
        *shrink*|*total_in_pool*) continue ;;
    esac
    log "[$n]"
    head -25 "$h" 2>/dev/null | tee -a "$OUT"
done
log ""

log "--- 播放期间关键日志 ---"
logcat -d 2>/dev/null | grep -iE \
    "MtkOmx|MtkOmxMVAMgr|MtkOmxVdec|MtkOmxCore|ACodec|MediaCodec|NdkMediaCodec|OMXNodeInstance|VLC" \
    | tail -80 | tee -a "$OUT"

log ""
log "--- 内核侧 vcodec/m4u 日志 ---"
dmesg 2>/dev/null | grep -iE "vcodec|m4u|vpu" | tail -40 | tee -a "$OUT"

log ""
log "=============================================="
log " 结果已保存：$OUT"
log "=============================================="
log ""
log "【怎么读这份结果】"
log " A) 如果 cken/sel/pll 在播放时变成 >=1、irq 计数持续增长"
log "    → 时钟和 VPU 都没问题，问题在 MVA 映射/输出缓冲（找 M4U + gralloc_extra）"
log " B) 如果 cken/sel/pll 全程 =0"
log "    → 确实是时钟没使能（vdec_power_on 没被调用或 SMI larb 失败）"
log " C) 如果 irq 涨了几下就停、同时日志出现 Can not find MVA from VA"
log "    → 解码器中途死在 MVA 映射，是 7.0 解码器 + 8.1 框架的 buffer 语义不匹配"
