/*
 * libui_shim.c (v2) —— 为 MTK 相机闭源库补上 Android 7.x 的 GraphicBuffer 构造函数
 *
 * === 问题 ===
 * MTK 相机库（libcam_utils.so 等）按 Android 7.x 编译，引用：
 *     android::GraphicBuffer::GraphicBuffer(uint32_t, uint32_t, int, uint32_t)
 *     → _ZN7android13GraphicBufferC1Ejjij
 * 8.1 的 libui.so 改成带 std::string 的新签名，旧符号不存在：
 *     dlopen failed: cannot locate symbol ... referenced by libcam_utils.so
 * 于是 hw_get_module 返回 -22，相机 HAL 加载失败，相机和闪光灯都不可用。
 *
 * === v2 相对 v1 的改动 ===
 * v1 用 12 字节全零伪造 libc++ 的 std::string —— 布局猜错，导致相机 provider
 * 在构造函数里崩溃。v2 改为：
 *   1. 用 dlsym(RTLD_DEFAULT) 在运行时查找所有需要的符号
 *      （这样本库没有任何硬未定义符号，被 LD_PRELOAD 到 crash_dump32 等
 *        进程里也不会导致链接失败）
 *   2. 用 libc++ 自己的构造函数创建空串、用完再析构
 *
 * === 用法 ===
 * 放 /system/vendor/lib/libui_shim.so，相机 provider 的 rc 里加：
 *     setenv LD_PRELOAD /vendor/lib/libui_shim.so
 *
 * 编译：
 *     clang --target=armv7-linux-gnueabi -fPIC -shared -nostdlib -Os \
 *           -o libui_shim.so libui_shim.c
 */

/* 不包含任何头文件 —— -nostdlib 下没有 sysroot，且我们要保持零依赖 */
#define RTLD_DEFAULT ((void *)0)

extern void *dlsym(void *handle, const char *symbol);

typedef void (*gb_ctor5_fn)(void *self, unsigned int width, unsigned int height,
			    int format, unsigned int usage, const void *name);
typedef void (*str_ctor_fn)(void *self);
typedef void (*str_dtor_fn)(void *self);

#define SYM_GB_CTOR5 \
	"_ZN7android13GraphicBufferC1EjjijNSt3__112basic_string" \
	"IcNS1_11char_traitsIcEENS1_9allocatorIcEEEE"
#define SYM_STR_CTOR \
	"_ZNSt3__112basic_stringIcNS_11char_traitsIcEENS_9allocatorIcEEEC1Ev"
#define SYM_STR_DTOR \
	"_ZNSt3__112basic_stringIcNS_11char_traitsIcEENS_9allocatorIcEEED1Ev"

/* 7 参数的 native_handle 构造函数（8.1 里多了一个 unsigned，见下面的注释） */
#define SYM_GB_CTOR_HANDLE8 \
	"_ZN7android13GraphicBufferC1EjjijjjP13native_handleb"

typedef void (*gb_ctor_handle8_fn)(void *self, unsigned int width,
				   unsigned int height, int format,
				   unsigned int usage, unsigned int stride,
				   unsigned int extra, void *handle, int owns);

static gb_ctor_handle8_fn p_gb_ctor_handle8;

static gb_ctor5_fn p_gb_ctor5;
static str_ctor_fn p_str_ctor;
static str_dtor_fn p_str_dtor;
static int resolved;

/* libc++ 的 std::string 在 32 位下 12 字节；给足 32 字节更安全 */
static unsigned char name_buf[32] __attribute__((aligned(8)));

static void resolve(void)
{
	if (resolved)
		return;
	p_gb_ctor5 = (gb_ctor5_fn)dlsym(RTLD_DEFAULT, SYM_GB_CTOR5);
	p_str_ctor = (str_ctor_fn)dlsym(RTLD_DEFAULT, SYM_STR_CTOR);
	p_str_dtor = (str_dtor_fn)dlsym(RTLD_DEFAULT, SYM_STR_DTOR);
	p_gb_ctor_handle8 =
		(gb_ctor_handle8_fn)dlsym(RTLD_DEFAULT, SYM_GB_CTOR_HANDLE8);
	resolved = 1;
}

/* 导出 Android 7.x 的构造函数符号 */
void _ZN7android13GraphicBufferC1Ejjij(void *self, unsigned int width,
				       unsigned int height, int format,
				       unsigned int usage)
{
	resolve();

	/* 找不到新构造函数就直接返回：宁可构造失败，也不要崩溃 */
	if (!p_gb_ctor5)
		return;

	/* 如果拿不到 string 的构造/析构，就用全零（v1 的做法，最后手段） */
	if (!p_str_ctor || !p_str_dtor) {
		int i;

		for (i = 0; i < (int)sizeof(name_buf); i++)
			name_buf[i] = 0;
	} else {
		p_str_ctor(name_buf);
	}

	p_gb_ctor5(self, width, height, format, usage, name_buf);

	if (p_str_ctor && p_str_dtor)
		p_str_dtor(name_buf);
}

/*
 * Android 7.x 的 native_handle 版本构造函数（MTK 相机库用它包装已有缓冲区）：
 *     GraphicBuffer(uint w, uint h, int format, uint usage,
 *                   uint stride, native_handle_t *handle, bool ownsHandle)
 * 8.1 在 stride 和 handle 之间多插了一个 unsigned（buffer id / producer usage）。
 * 从外部句柄包装的场景下这个值是分配器内部编号，传 0 即可。
 */
void _ZN7android13GraphicBufferC1EjjijjP13native_handleb(
	void *self, unsigned int width, unsigned int height, int format,
	unsigned int usage, unsigned int stride, void *handle, int owns_handle)
{
	resolve();

	if (!p_gb_ctor_handle8)
		return;

	p_gb_ctor_handle8(self, width, height, format, usage, stride,
			  0u /* 8.1 新增的 buffer id */, handle, owns_handle);
}
