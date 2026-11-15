#ifndef BUTTERBOT_FIRMWARE_SPEECHAUDIOGEN_H
#define BUTTERBOT_FIRMWARE_SPEECHAUDIOGEN_H

#include "Services/Audio/AudioGenerator.h"
#include "Reverb.h"
#include "Compressor.h"
#include "VoicePreset.h"
/**
 * Interface between CMF Audio service and locally defined SpeechGen service.
 *
 * Basically just a passthrough of data from AudioSource, which is expected to be a SpeechAudioSource.
 */
class SpeechAudioGen : public AudioGenerator {
	using SampleType = int16_t;

public:
	SpeechAudioGen();

	void open(std::unique_ptr<AudioSource> resource) override;

	void close() override;

	size_t getData(uint8_t* buffer, size_t bytes) override;

private:
	std::unique_ptr<AudioSource> resource;
	int bytesRemaining = 0;
	Compressor compressor;
	Reverb reverb;

	// Fork: voice preset effect (see VoicePreset.h)
	static constexpr int SampleRate = 16000;
	void applyFx(int16_t* samples, size_t count);
	Voice::Fx fx = Voice::Fx::None;
	float ringPhase = 0.0f;
	int crushCount = 0;
	int16_t crushHeld = 0;

	// Fx::Show: two high-passes at 300 Hz and a low-pass at 2.5 kHz (a tiny speaker), then a slow warble
	struct Biquad {
		float b0 = 1, b1 = 0, b2 = 0, a1 = 0, a2 = 0, z1 = 0, z2 = 0;
		void set(bool highPass, float hz, float q);
		float run(float x){
			const float y = b0 * x + z1;
			z1 = b1 * x - a1 * y + z2;
			z2 = b2 * x - a2 * y;
			return y;
		}
	};
	Biquad showHp1, showHp2, showLp;
	float warblePhase = 0.0f;
	float warbleStep = 0.0f;
	float warbleDepth = 0.0f;
	float showGain = 1.0f;
};


#endif //BUTTERBOT_FIRMWARE_SPEECHAUDIOGEN_H
