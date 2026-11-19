/*
 * libbt-vendor.c —— 64 位蓝牙厂商库封装层（v3：修正符号加载问题）
 *
 * 原理：上层 bluetooth.default.so 需要 libbt-vendor.so 导出
 * BLUETOOTH_VENDOR_LIB_INTERFACE；真正的 MTK STP 逻辑在
 * /system/vendor/lib64/libbluetooth_mtk.so（设备自带，64 位）。
 * 本库只做薄转发。
 *
 * === 演进 ===
 * v1（自写）：基于"内核只能 open stpbt 一次"的错误假设，扫 /proc 找 fd、
 *            fw_cfg 预热两次、重扫 fd —— 绕圈且引入 bug。
 * v2：去掉绕圈逻辑，改用 dlopen/dlsym 读 libbluetooth_mtk 的 fd 槽。
 *     ✗ 加载失败：dlopen failed: cannot locate symbol "dlerror"
 *     原因：bionic 的 dlopen/dlsym/dlerror 带符号版本，
 *           -nostdlib 交叉编译的未定义符号无版本 → 运行时匹配失败。
 * v3（本版）：
 *   · 完全不用 dlopen/dlsym —— 只用 open/readlink/close（bionic 无版本符号，
 *     这是旧版已验证能加载的方式）。
 *   · init_uart() 打开 /dev/stpbt 后，扫 /proc/self/fd 找那个 fd（一次即可）。
 *   · mtk_fw_cfg() 只调一次，不做预热、不重扫 fd。
 *   · 核心假设已实测纠正：内核 BT_open 可反复 open/close（4 次全成功），
 *     所以不需要任何 fd 复用/保护的绕圈逻辑。
 *
 * 编译（CI，clang -nostdlib）：
 *   clang --target=aarch64-linux-gnu -fPIC -shared -nostdlib -Os \
 *         -fuse-ld=lld -o libbt-vendor.so libbt-vendor.c
 */

typedef unsigned long size_t_u;
typedef unsigned char uint8_t_u;
typedef unsigned int uint32_t_u;
typedef int int32_t_u;

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

/* libbluetooth_mtk.so 导出函数（无版本符号，可解析） */
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

/* bionic 无版本符号（旧版已验证可解析） */
extern long readlink(const char *path, char *buf, unsigned long bufsiz);

extern int __android_log_print(int prio, const char *tag, const char *fmt, ...);
#define LOG_TAG "bt_vendor_s3pro"
#define LOGI(...) __android_log_print(4, LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(6, LOG_TAG, __VA_ARGS__)

static const struct bt_vendor_callbacks_t *g_callbacks;
static int g_power_state = BT_VND_PWR_OFF;

/* 扫 /proc/self/fd 找指向 /dev/stpbt 的 fd（init_uart 打开的那个） */
static int find_stpbt_fd(void)
{
	char path[40];
	char target[128];
	int fd;
	int found = -1;

	for (fd = 3; fd < 256; fd++) {
		long r;
		int n, i;

		/* 手工拼 "/proc/self/fd/N"（避免 sprintf/snprintf 符号） */
		path[0]='/'; path[1]='p'; path[2]='r'; path[3]='o'; path[4]='c';
		path[5]='/'; path[6]='s'; path[7]='e'; path[8]='l'; path[9]='f';
		path[10]='/'; path[11]='f'; path[12]='d'; path[13]='/';
		n = 14;
		if (fd >= 100) {
			path[n++]='0' + (fd/100);
			path[n++]='0' + ((fd/10)%10);
			path[n++]='0' + (fd%10);
		} else if (fd >= 10) {
			path[n++]='0' + (fd/10);
			path[n++]='0' + (fd%10);
		} else {
			path[n++]='0' + fd;
		}
		path[n]=0;

		r = readlink(path, target, sizeof(target)-1);
		if (r <= 0)
			continue;
		target[r] = 0;
		/* 找 "stpbt" */
		for (i = 0; i + 5 <= r; i++) {
			if (target[i]=='s' && target[i+1]=='t' &&
			    target[i+2]=='p' && target[i+3]=='b' &&
			    target[i+4]=='t') {
				found = fd;
				LOGI("found stpbt fd=%d (%s)", fd, target);
				return found;
			}
		}
	}
	LOGE("no stpbt fd found");
	return -1;
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

/* USERIAL_OPEN：init_uart 打开 stpbt，扫 /proc 拿到 fd 交给 HAL */
static int op_userial_open(void *arg)
{
	int *fds = (int *)arg;
	int fd;

	LOGI("BT_VND_OP_USERIAL_OPEN");
	init_uart(0);
	fd = find_stpbt_fd();

	LOGI("userial opened fd=%d", fd);
	if (fd < 0)
		return BT_VND_OP_RESULT_FAIL;
	if (fds) {
		fds[0] = fd;
		fds[1] = -1;
	}
	return 1;
}

static int op_userial_close(void *arg)
{
	(void)arg;
	LOGI("BT_VND_OP_USERIAL_CLOSE");
	close_uart(0);
	return BT_VND_OP_RESULT_SUCCESS;
}

/* FW_CFG：只调一次，不做预热、不重扫 fd（内核可反复 open，无需绕圈） */
static int op_fw_cfg(void *arg)
{
	int ret;
	LOGI("BT_VND_OP_FW_CFG");
	ret = mtk_fw_cfg(arg);
	LOGI("fw_cfg result=%d", ret);
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
	mtk_set_fw_assert(arg);
	return BT_VND_OP_RESULT_SUCCESS;
}

static int op_set_psm_control(void *arg)
{
	mtk_set_psm_control(arg);
	return BT_VND_OP_RESULT_SUCCESS;
}

static int bt_vendor_op(int32_t_u opcode, void *arg)
{
	switch (opcode) {
	case BT_VND_OP_POWER_CTRL:            return op_power_ctrl(arg);
	case BT_VND_OP_FW_CFG:                return op_fw_cfg(arg);
	case BT_VND_OP_SCO_CFG:               return op_sco_cfg(arg);
	case BT_VND_OP_USERIAL_OPEN:          return op_userial_open(arg);
	case BT_VND_OP_USERIAL_CLOSE:         return op_userial_close(arg);
	case BT_VND_OP_GET_LPM_IDLE_TIMEOUT:  return op_lpm_idle_timeout(arg);
	case BT_VND_OP_LPM_SET_MODE:          return op_lpm_set_mode(arg);
	case BT_VND_OP_LPM_WAKE_SET_STATE:    return op_lpm_wake_set_state(arg);
	case BT_VND_OP_EPILOG:                return op_epilog(arg);
	case BT_VND_OP_SET_FW_ASSERT:         return op_set_fw_assert(arg);
	case BT_VND_OP_SET_PSM_CONTROL:       return op_set_psm_control(arg);
	default:
		LOGE("unknown opcode %u", opcode);
		return BT_VND_OP_RESULT_FAIL;
	}
}

static size_t_u bt_vendor_init(const struct bt_vendor_callbacks_t *callbacks,
			       uint8_t_u *local_bdaddr)
{
	(void)local_bdaddr;
	LOGI("bt_vendor_init v3");
	g_callbacks = callbacks;
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
