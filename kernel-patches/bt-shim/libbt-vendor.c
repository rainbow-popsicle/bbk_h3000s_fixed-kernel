/*
 * libbt-vendor.c —— 64 位蓝牙厂商库封装层（简洁正确版 v2）
 *
 * 原理：上层 bluetooth.default.so 需要 libbt-vendor.so 导出
 * BLUETOOTH_VENDOR_LIB_INTERFACE；真正的 MTK STP 逻辑在
 * /system/vendor/lib64/libbluetooth_mtk.so（设备自带，64 位）。
 * 本库只做薄转发。
 *
 * === v2 相对旧版的关键修正 ===
 * 旧版基于一个【错误假设】写了大量绕圈逻辑：
 *   ✗ "内核 BT_open 只允许 open 一次，第二次 ENODEV"
 *     实测：/dev/stpbt 连续 open/close 4 次全部成功
 *           （WMT turn on BT OK / STP is ready 每次都出现）
 *   ✗ 因此旧版扫描 /proc/self/fd 找 fd、fd 复用、fw_cfg 预热两次、
 *     fw_cfg 后重扫 fd 并改写 HAL 指针 —— 这些全是多余的，且引入 bug。
 *
 * v2 的做法：
 *   1) init_uart() 反汇编确认：open("/dev/stpbt") 后把 fd 存到库内
 *      .data+0x8（虚拟地址 0x8008），返回值是 -1（不是 fd）。
 *      用 dlopen 具体库 + dlsym("bt_vnd_cbacks", 位于 0x8068)，
 *      则 fd 地址 = &bt_vnd_cbacks - (0x8068 - 0x8008)
 *                 = &bt_vnd_cbacks - 0x60，直接读，不扫 /proc。
 *   2) mtk_fw_cfg() 只调一次。
 *
 * 编译（CI，gcc-aarch64-linux-gnu）：
 *   aarch64-linux-gnu-gcc -fPIC -shared -nostdlib -Os -o libbt-vendor.so libbt-vendor.c
 *
 * 不依赖任何 Android 头文件（CI 交叉环境没有 bionic 头），
 * 外部符号全部手动声明。
 */

/* ---------------- 基础类型 ---------------- */
typedef unsigned long size_t_u;
typedef unsigned char uint8_t_u;
typedef unsigned int uint32_t_u;
typedef int int32_t_u;

/* ---------------- AOSP bt_vendor 操作码 ---------------- */
enum {
	BT_VND_OP_POWER_CTRL = 0,
	BT_VND_OP_FW_CFG,
	BT_VND_OP_SCO_CFG,
	BT_VND_OP_USERIAL_OPEN,
	BT_VND_OP_USERIAL_CLOSE,
	BT_VND_OP_GET_LPM_IDLE_TIMEOUT,
	BT_VND_OP_LPM_SET_MODE,
	BT_VND_OP_LPM_WAKE_SET_STATE,
	BT_VND_OP_EPILOG,
	BT_VND_OP_SET_FW_ASSERT,
	BT_VND_OP_SET_PSM_CONTROL,
};

enum {
	BT_VND_PWR_OFF = 0,
	BT_VND_PWR_ON = 1,
	BT_VND_PWR_FULL = 2,
};

enum {
	BT_VND_OP_RESULT_SUCCESS = 0,
	BT_VND_OP_RESULT_FAIL = 1,
};

struct bt_vendor_callbacks_t;

/* ---------------- libbluetooth_mtk.so 导出函数 ---------------- */
extern int init_uart(void *arg);
extern int close_uart(void *arg);
extern int mtk_fw_cfg(void *arg);
extern int mtk_sco_cfg(void *arg);
extern int mtk_prepare_off(void *arg);
extern void set_callbacks(void *cbacks);
extern void clean_callbacks(void);
extern void clean_resource(void);
extern int BT_InitDevice(void *arg);
extern int BT_DeinitDevice(void *arg);
extern int BT_Cleanup(void *arg);
extern int mtk_set_fw_assert(void *arg);
extern int mtk_set_psm_control(void *arg);

/* dlopen/dlsym */
extern void *dlopen(const char *filename, int flag);
extern void *dlsym(void *handle, const char *symbol);
extern char *dlerror(void);
#define RTLD_NOW 2

/* libbluetooth_mtk.so 内的 fd 槽：
 * init_uart 里 `adrp x8,#0x8000; str w0,[x8,#8]` → fd 存在 0x8008
 * bt_vnd_cbacks 导出符号在 0x8068
 * 所以 fd 槽 = &bt_vnd_cbacks - 0x60
 */
#define MTK_LIB_PATH "/system/vendor/lib64/libbluetooth_mtk.so"
#define CBACKS_LINK_ADDR 0x8068UL
#define FD_SLOT_LINK_ADDR 0x8008UL
#define FD_SLOT_DELTA (CBACKS_LINK_ADDR - FD_SLOT_LINK_ADDR)  /* 0x60 */

/* 日志 */
extern int __android_log_print(int prio, const char *tag, const char *fmt, ...);
#define LOG_TAG "bt_vendor_s3pro"
#define LOGI(...) __android_log_print(4, LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(6, LOG_TAG, __VA_ARGS__)

/* ---------------- 状态 ---------------- */
static const struct bt_vendor_callbacks_t *g_callbacks;
static int g_power_state = BT_VND_PWR_OFF;
static void *g_mtk_handle;
static int *g_fd_slot;       /* 指向 libbluetooth_mtk 内的 fd 存储 */

/* 打开 mtk 库并算出 fd 槽地址（init 时做一次） */
static void init_mtk_handle(void)
{
	if (g_mtk_handle)
		return;
	g_mtk_handle = dlopen(MTK_LIB_PATH, RTLD_NOW);
	if (!g_mtk_handle) {
		LOGE("dlopen %s failed: %s", MTK_LIB_PATH,
		     dlerror() ? dlerror() : "?");
		return;
	}
	void *cbacks = dlsym(g_mtk_handle, "bt_vnd_cbacks");
	if (!cbacks) {
		LOGE("dlsym bt_vnd_cbacks failed: %s",
		     dlerror() ? dlerror() : "?");
		return;
	}
	g_fd_slot = (int *)((unsigned long)cbacks - FD_SLOT_DELTA);
	LOGI("mtk lib loaded, cbacks=%p fd_slot=%p", cbacks, g_fd_slot);
}

/* ---------------- op 实现 ---------------- */

static int op_power_ctrl(void *arg)
{
	int32_t_u *params = (int32_t_u *)arg;
	int32_t_u pwr = params ? params[0] : BT_VND_PWR_OFF;

	LOGI("BT_VND_OP_POWER_CTRL %d", pwr);

	if (pwr == BT_VND_PWR_ON || pwr == BT_VND_PWR_FULL) {
		if (g_power_state == BT_VND_PWR_OFF) {
			BT_InitDevice(0);
			g_power_state = BT_VND_PWR_ON;
		}
	} else {
		if (g_power_state != BT_VND_PWR_OFF) {
			BT_DeinitDevice(0);
			g_power_state = BT_VND_PWR_OFF;
		}
	}
	return BT_VND_OP_RESULT_SUCCESS;
}

/* USERIAL_OPEN：让 init_uart 打开 stpbt，然后从库内 fd 槽读出 fd 交给 HAL */
static int op_userial_open(void *arg)
{
	int *fds = (int *)arg;

	LOGI("BT_VND_OP_USERIAL_OPEN");
	init_mtk_handle();

	/* init_uart 内部 open("/dev/stpbt") 并把 fd 存入 .data+0x8 */
	init_uart(0);

	int fd = -1;
	if (g_fd_slot)
		fd = *g_fd_slot;   /* 直接读，不扫 /proc */

	LOGI("init_uart done, fd from slot = %d", fd);

	if (fd < 0) {
		LOGE("userial open failed: fd=%d", fd);
		return BT_VND_OP_RESULT_FAIL;
	}
	if (fds) {
		fds[0] = fd;
		fds[1] = -1;       /* 只用一路通道 */
	}
	return 1;              /* 返回 fd 数量 */
}

static int op_userial_close(void *arg)
{
	(void)arg;
	LOGI("BT_VND_OP_USERIAL_CLOSE");
	close_uart(0);
	return BT_VND_OP_RESULT_SUCCESS;
}

/* FW_CFG：只调一次（旧版预热两次是基于错误的 combo-id 竞争假设） */
static int op_fw_cfg(void *arg)
{
	LOGI("BT_VND_OP_FW_CFG");
	int ret = mtk_fw_cfg(arg);
	LOGI("fw_cfg result = %d", ret);
	return (ret == 0) ? BT_VND_OP_RESULT_SUCCESS : BT_VND_OP_RESULT_FAIL;
}

static int op_sco_cfg(void *arg)
{
	LOGI("BT_VND_OP_SCO_CFG");
	mtk_sco_cfg(arg);
	return BT_VND_OP_RESULT_SUCCESS;
}

static int op_lpm_idle_timeout(void *arg)
{
	int32_t_u *timeout = (int32_t_u *)arg;
	if (timeout)
		*timeout = 3000;
	LOGI("BT_VND_OP_GET_LPM_IDLE_TIMEOUT -> 3000ms");
	return BT_VND_OP_RESULT_SUCCESS;
}

static int op_lpm_set_mode(void *arg)
{
	(void)arg;
	return BT_VND_OP_RESULT_SUCCESS;
}

static int op_lpm_wake_set_state(void *arg)
{
	(void)arg;
	return BT_VND_OP_RESULT_SUCCESS;
}

static int op_epilog(void *arg)
{
	(void)arg;
	LOGI("BT_VND_OP_EPILOG");
	mtk_prepare_off(0);
	return BT_VND_OP_RESULT_SUCCESS;
}

static int op_set_fw_assert(void *arg)
{
	LOGI("BT_VND_OP_SET_FW_ASSERT");
	mtk_set_fw_assert(arg);
	return BT_VND_OP_RESULT_SUCCESS;
}

static int op_set_psm_control(void *arg)
{
	LOGI("BT_VND_OP_SET_PSM_CONTROL");
	mtk_set_psm_control(arg);
	return BT_VND_OP_RESULT_SUCCESS;
}

/* ---------------- op 总入口 ---------------- */
static int bt_vendor_op(int32_t_u opcode, void *arg)
{
	switch (opcode) {
	case BT_VND_OP_POWER_CTRL:      return op_power_ctrl(arg);
	case BT_VND_OP_FW_CFG:          return op_fw_cfg(arg);
	case BT_VND_OP_SCO_CFG:         return op_sco_cfg(arg);
	case BT_VND_OP_USERIAL_OPEN:    return op_userial_open(arg);
	case BT_VND_OP_USERIAL_CLOSE:   return op_userial_close(arg);
	case BT_VND_OP_GET_LPM_IDLE_TIMEOUT: return op_lpm_idle_timeout(arg);
	case BT_VND_OP_LPM_SET_MODE:    return op_lpm_set_mode(arg);
	case BT_VND_OP_LPM_WAKE_SET_STATE: return op_lpm_wake_set_state(arg);
	case BT_VND_OP_EPILOG:          return op_epilog(arg);
	case BT_VND_OP_SET_FW_ASSERT:   return op_set_fw_assert(arg);
	case BT_VND_OP_SET_PSM_CONTROL: return op_set_psm_control(arg);
	default:
		LOGE("unknown opcode %u", opcode);
		return BT_VND_OP_RESULT_FAIL;
	}
}

/* ---------------- init / cleanup ---------------- */
static size_t_u bt_vendor_init(const struct bt_vendor_callbacks_t *callbacks,
			       uint8_t_u *local_bdaddr)
{
	(void)local_bdaddr;
	LOGI("bt_vendor_init v2");
	g_callbacks = callbacks;
	init_mtk_handle();
	/* 不在 init 里调 BT_InitDevice —— POWER_CTRL 里会调 */
	set_callbacks((void *)callbacks);
	return 0;
}

static void bt_vendor_cleanup(void)
{
	LOGI("bt_vendor_cleanup");
	close_uart(0);
	if (g_power_state != BT_VND_PWR_OFF) {
		BT_DeinitDevice(0);
		g_power_state = BT_VND_PWR_OFF;
	}
	BT_Cleanup(0);
	clean_resource();
	clean_callbacks();
	g_callbacks = 0;
}

/* ---------------- 导出接口表（MTK 扩展：首字段 0x20）---------------- */
struct bt_vendor_interface {
	size_t_u magic_size;
	size_t_u (*init)(const struct bt_vendor_callbacks_t *callbacks,
			 uint8_t_u *local_bdaddr);
	int (*op)(int32_t_u opcode, void *arg);
	void (*cleanup)(void);
};

__attribute__((visibility("default")))
struct bt_vendor_interface BLUETOOTH_VENDOR_LIB_INTERFACE = {
	.magic_size = 0x20,
	.init = bt_vendor_init,
	.op = bt_vendor_op,
	.cleanup = bt_vendor_cleanup,
};
