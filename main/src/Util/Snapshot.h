#ifndef BUTTERBOT_FIRMWARE_SNAPSHOT_H
#define BUTTERBOT_FIRMWARE_SNAPSHOT_H

#include <cstddef>
#include <cstdint>

/**
 * Fork: one camera photo as a JPEG, for the AI brain's "look" (BrainLink LOOK).
 *
 * The camera normally runs at 128x128 for face detection. For the photo it switches to 320x240 when there's
 * internal RAM to spare (the capture DMA buffers live there), takes a few frames so the exposure settles, then
 * goes back to 128x128. With too little RAM, or if the switch fails, the photo is 128x128.
 */
namespace Snapshot {

struct Photo {
	uint8_t* jpg = nullptr; // malloc'd (free() it), nullptr on failure
	size_t len = 0;
	uint16_t w = 0, h = 0;
	const char* why = "";   // on failure: no_camera, busy, capture, encode, memory
};

/** Takes the photo (in a helper task, so any caller will do) and waits for it. Returns busy while face detection runs. */
Photo take();

}

#endif //BUTTERBOT_FIRMWARE_SNAPSHOT_H
