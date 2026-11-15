#include "SpeechAudioGen.h"
#include "Core/Application.h"
#include "VoicePreset.h"
#include <algorithm>
#include <cmath>

DEFINE_LOG(SpeechAudioGen)

SpeechAudioGen::SpeechAudioGen(){
	reverb.setRoomSize(0.2f); // 0 = metalan/sjajan, 1 = taman/prigusen
	reverb.setDamping(0.8f); // duzina odjeka
	reverb.setWet(0.05f); // Koliko reverba cujes
}

void SpeechAudioGen::open(std::unique_ptr<AudioSource> resource){
	resource->open();
	this->resource = std::move(resource);
	compressor.reset();

	// Fork: voice preset effects, fixed for the whole utterance
	const Voice::Preset& preset = Voice::current();
	fx = Voice::fx();
	reverb.setRoomSize(preset.reverbRoom);
	reverb.setWet(preset.reverbWet);
	ringPhase = 0.0f;
	crushCount = 0;
	crushHeld = 0;
	const Voice::ShowFx show = Voice::showFx();
	showHp1.set(true, show.hpHz, 0.707f);
	showHp2.set(true, show.hpHz, 0.707f);
	showLp.set(false, show.lpHz, 0.707f);
	warblePhase = 0.0f;
	warbleStep = 2.0f * (float) M_PI * show.warbleHz / (float) SampleRate;
	warbleDepth = show.depth;
	showGain = show.gain;
}

// RBJ cookbook high/low-pass
void SpeechAudioGen::Biquad::set(bool highPass, float hz, float q){
	const float w = 2.0f * (float) M_PI * hz / (float) SampleRate;
	const float alpha = std::sin(w) / (2.0f * q), c = std::cos(w);
	const float a0 = 1.0f + alpha;
	if(highPass){
		b0 = (1.0f + c) / 2.0f / a0;
		b1 = -(1.0f + c) / a0;
	}else{
		b0 = (1.0f - c) / 2.0f / a0;
		b1 = (1.0f - c) / a0;
	}
	b2 = b0;
	a1 = -2.0f * c / a0;
	a2 = (1.0f - alpha) / a0;
	z1 = z2 = 0.0f;
}

void SpeechAudioGen::close(){
	resource->close();
	resource.reset();
}

size_t SpeechAudioGen::getData(uint8_t* buffer, size_t bytes){
	if(resource == nullptr){
		CMF_LOG(SpeechAudioGen, LogLevel::Debug, "early return, no resource");
		return 0;
	}

	size_t bytesTransferred = 0;
	while(bytesTransferred < bytes && (bool)*resource){
		auto i = resource->getData(buffer + bytesTransferred, bytes - bytesTransferred);
		if(i == 0) break;
		bytesTransferred += i;
	}
	const size_t sampleCount = bytesTransferred / sizeof(int16_t);
	auto* samples = reinterpret_cast<int16_t*>(buffer);
	compressor.process(samples, sampleCount);
	applyFx(samples, sampleCount);
	reverb.process(samples, sampleCount);
	return bytesTransferred;
}

void SpeechAudioGen::applyFx(int16_t* samples, size_t count){
	switch(fx){
		case Voice::Fx::None:
			return;

		case Voice::Fx::Ring: {
			// Ring modulator: multiplying by a low sine gives the classic metallic "robot" voice. Part dry so the
			// words stay clear.
			static constexpr float CarrierHz = 55.0f;
			static constexpr float Mix = 0.7f;
			static constexpr float Step = 2.0f * (float) M_PI * CarrierHz / (float) SampleRate;
			for(size_t i = 0; i < count; ++i){
				const float x = samples[i];
				const float y = x * (1.0f - Mix) + x * std::sin(ringPhase) * Mix;
				samples[i] = static_cast<int16_t>(y);
				ringPhase += Step;
				if(ringPhase > 2.0f * (float) M_PI) ringPhase -= 2.0f * (float) M_PI;
			}
			return;
		}

		case Voice::Fx::Crush: {
			// Bitcrusher: hold every sample for 3 (16 kHz -> ~5.3 kHz) and keep 6 bits - an old game console voice.
			static constexpr int Hold = 3;
			static constexpr int DropBits = 10;
			for(size_t i = 0; i < count; ++i){
				if(crushCount == 0){
					crushHeld = static_cast<int16_t>((samples[i] >> DropBits) << DropBits);
				}
				samples[i] = crushHeld;
				crushCount = (crushCount + 1) % Hold;
			}
			return;
		}

		case Voice::Fx::Show: {
			// The show's robot: no bass and little top (Voice::ShowSound: ~500 Hz - 4 kHz, like a tiny speaker), and a
			// ~13 Hz warble - the clip's harmonics come in pairs ~13 Hz apart. Gain makes up for the bass taken out.
			for(size_t i = 0; i < count; ++i){
				float y = showLp.run(showHp2.run(showHp1.run((float) samples[i])));
				y *= showGain * (1.0f - warbleDepth * 0.5f + warbleDepth * 0.5f * std::sin(warblePhase));
				samples[i] = static_cast<int16_t>(std::clamp(y, -32767.0f, 32767.0f));
				warblePhase += warbleStep;
				if(warblePhase > 2.0f * (float) M_PI) warblePhase -= 2.0f * (float) M_PI;
			}
			return;
		}
	}
}
