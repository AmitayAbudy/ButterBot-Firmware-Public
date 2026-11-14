#ifndef BUTTERBOT_FIRMWARE_MEMMARK_H
#define BUTTERBOT_FIRMWARE_MEMMARK_H

#include <atomic>
#include <cstdint>
#include <cstdio>
#include <vector>
#include <esp_heap_caps.h>
#include <esp_memory_utils.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <esp_rom_sys.h>

/**
 * Fork: internal-RAM accounting on the serial console. Internal RAM is the robot's scarce resource:
 * when it runs out, Bluetooth / esp_timer allocations abort the whole robot (which then powers off).
 *   [BBMEM] <where> internal free N KB (largest M KB)
 *   [BBTASK] <task> stack=internal|psram unused=N B
 */
namespace MemMark {

/**
 * The real low point of internal RAM. heap_caps_get_minimum_free_size() adds up each heap region's own minimum,
 * reached at different moments, so it can say 0 KB while there was always memory free (measured: three regions
 * each bottomed out at a few bytes, at different times). This samples the actual total instead.
 */
inline std::atomic<uint32_t> lowestFree{ UINT32_MAX };
inline std::atomic<uint32_t> failedAllocs{ 0 }; // allocations that really failed - the number that matters

inline void sample(){
	const uint32_t f = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
	uint32_t cur = lowestFree.load();
	while(f < cur && !lowestFree.compare_exchange_weak(cur, f)){}
}

inline uint32_t lowestKb(){
	sample();
	return lowestFree.load() / 1024;
}

inline void mark(const char* what){
	sample();
	printf("[BBMEM] %-15s internal free %3u KB (largest %3u KB, lowest %3u KB)\n", what,
	       (unsigned) (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024),
	       (unsigned) (heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL) / 1024),
	       (unsigned) lowestKb());
}

/**
 * Every failed allocation, with who asked: "[BBMEM] alloc_failed size=N caps=0x.. by=<function> free=N KB".
 * Runs inside the allocator, so it prints with the ROM printf (no allocation) and stays short.
 */
inline void watchFailedAllocs(){
	heap_caps_register_failed_alloc_callback([](size_t size, uint32_t caps, const char* fn){
		failedAllocs.fetch_add(1);
		esp_rom_printf("[BBMEM] alloc_failed size=%u caps=0x%x by=%s free=%u KB\n", (unsigned) size, (unsigned) caps,
		               fn ? fn : "?", (unsigned) (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024));
	});
}

// Every task whose stack lives in internal RAM, with how much of it has never been touched.
inline void dumpTasks(){
	const UBaseType_t n = uxTaskGetNumberOfTasks();
	std::vector<TaskStatus_t> tasks(n + 4);
	const UBaseType_t got = uxTaskGetSystemState(tasks.data(), tasks.size(), nullptr);
	unsigned internalTasks = 0;
	for(UBaseType_t i = 0; i < got; i++){
		const TaskStatus_t& t = tasks[i];
		const bool internal = esp_ptr_internal(t.pxStackBase);
		if(internal) internalTasks++;
		printf("[BBTASK] %-16s stack=%-8s unused=%5u B\n", t.pcTaskName, internal ? "internal" : "psram",
		       (unsigned) t.usStackHighWaterMark);
	}
	printf("[BBTASK] %u tasks, %u with internal stacks\n", (unsigned) got, internalTasks);
}

}

#endif //BUTTERBOT_FIRMWARE_MEMMARK_H
