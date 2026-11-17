#include "Snapshot.h"
#include <cstdio>
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include <img_converters.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/idf_additions.h>
#include <Core/Application.h>
#include <Statics/ApplicationStatics.h>
#include <Devices/Camera.h>
#include "FaceDet.h"

namespace Snapshot {

namespace {
	constexpr framesize_t NormalRes = FRAMESIZE_128X128; // what face detection and "what is this?" expect
	constexpr framesize_t PhotoRes = FRAMESIZE_QVGA;     // 320x240
	constexpr int JpegQuality = 55;
	constexpr int SettleFrames = 3; // frames thrown away after a resolution change (exposure / white balance)
	// Internal RAM the bigger capture may use: the WiFi upload and audio front end need the rest
	constexpr size_t BigMinFree = 56 * 1024;
	constexpr size_t BigMinBlock = 24 * 1024;

	bool switchTo(Camera* cam, framesize_t res){
		if(cam->getRes() == res && cam->isInited()) return true;
		cam->setRes(res);
		return cam->init() == ESP_OK;
	}
}

static Photo takeHere(){
	Photo p;
	const Application* app = ApplicationStatics::getApplication();
	Camera* cam = app != nullptr ? app->getDevice<Camera>() : nullptr;
	if(cam == nullptr){
		p.why = "no_camera";
		return p;
	}
	if(const FaceDet* fd = app->getService<FaceDet>(); fd != nullptr && fd->isStarted()){
		p.why = "busy";
		return p;
	}

	const int64_t t0 = esp_timer_get_time();
	const size_t freeBefore = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
	const size_t blockBefore = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
	bool big = freeBefore >= BigMinFree && blockBefore >= BigMinBlock;
	if(big && !switchTo(cam, PhotoRes)){
		big = false;
		switchTo(cam, NormalRes);
	}

	camera_fb_t* fb = nullptr;
	for(int i = 0; i <= (big ? SettleFrames : 0); ++i){
		if(fb != nullptr) cam->releaseFrame();
		fb = cam->getFrame();
		if(fb == nullptr) break;
	}
	if(fb != nullptr && fb->buf != nullptr && fb->len > 0){
		p.w = fb->width;
		p.h = fb->height;
		if(!frame2jpg(fb, JpegQuality, &p.jpg, &p.len) || p.jpg == nullptr){
			p.jpg = nullptr;
			p.len = 0;
			p.why = "encode";
		}
	}else{
		p.why = "capture";
	}
	cam->releaseFrame();

	const size_t freeDuring = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
	const bool restored = switchTo(cam, NormalRes);
	printf("[BBAI] event=photo w=%u h=%u bytes=%u big=%d ms=%lld ram_before=%u ram_during=%u ram_after=%u restored=%d%s%s\n",
	       (unsigned) p.w, (unsigned) p.h, (unsigned) p.len, big ? 1 : 0, (esp_timer_get_time() - t0) / 1000,
	       (unsigned) (freeBefore / 1024), (unsigned) (freeDuring / 1024),
	       (unsigned) (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024), restored ? 1 : 0,
	       p.jpg == nullptr ? " why=" : "", p.jpg == nullptr ? p.why : "");
	return p;
}

/*
 * JPEG encoding needs more stack than the AI worker has (its stack is in scarce internal RAM), so the photo is taken
 * in a short-lived helper task with an 8 KB stack in PSRAM (fine: nothing here writes flash), like the Diagnostics
 * camera's worker. The caller waits for it.
 */
Photo take(){
	struct Job {
		Photo photo;
		TaskHandle_t caller;
	} job{ {}, xTaskGetCurrentTaskHandle() };
	TaskHandle_t helper = nullptr;
	const BaseType_t ok = xTaskCreatePinnedToCoreWithCaps([](void* arg){
		auto* j = static_cast<Job*>(arg);
		j->photo = takeHere();
		xTaskNotifyGive(j->caller);
		vTaskSuspend(nullptr); // deleted by the caller (a task with a caps stack can't delete itself)
	}, "bbsnap", 8192, &job, 3, &helper, tskNO_AFFINITY, MALLOC_CAP_SPIRAM);
	if(ok != pdPASS){
		Photo p;
		p.why = "memory";
		return p;
	}
	ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
	// Its stack is freed with it, so wait until it has really stopped (it may still be on the other core)
	while(eTaskGetState(helper) != eSuspended) vTaskDelay(1);
	vTaskDeleteWithCaps(helper);
	return job.photo;
}

}
