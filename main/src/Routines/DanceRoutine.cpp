#include "DanceRoutine.h"
#include "States/BBStateMachine.h"
#include <Statics/ApplicationStatics.h>
#include <Util/stdafx.h>
#include <Core/Application.h>
#include <Services/Audio/Audio.h>
#include <Phrases.h>
#include <CtrlData.h>
#include <freertos/FreeRTOS.h>
#include <Util/ServiceLocator.h>
#include "Audio/SpeechAudioGen.h"
#include "Audio/SpeechAudioSource.h"
#include "Audio/SpeechGen.h"
#include "Services/Com.h"

DEFINE_LOG(DanceRoutine)

const DanceRoutine::DanceMove DanceRoutine::DanceMoves[] = {
	{  60,  60,  800 },
	{ -60, -60,  800 },
	{ -80,  80,  600 },
	{  80, -80,  600 },
	{-100, 100,  900 },
	{ 100,-100,  900 },
	{   0,   0,  400 },
	{  70,  35,  500 },
	{  35,  70,  500 },
};

static constexpr AudioFrontend::Phrase StopPhrases[] = {
	{ "stop dancing", "STnP DaNSgl", "STnP", 0.25f },
	// Add more?
};

void DanceRoutine::bindEvents(){
	sm->bindRoutine(moduleService->ModulesEvent, this, &DanceRoutine::onModuleEvent);
	sm->bindRoutine(com->OnCommand, this, &DanceRoutine::onCommand);
	sm->bindRoutine(buttonInput->OnButtonEvent, this, &DanceRoutine::onButtonEvent);
	sm->bindRoutine(baseBoard->onProximityReading, this, &DanceRoutine::onProximityReading);
	sm->bindRoutine(baseBoard->onProximityChange, this, &DanceRoutine::onProximityChange);
	if(ServiceLocator::SettingsInstance && ServiceLocator::SettingsInstance->get().bottomSensor == BottomSensorMode::On){
		sm->bindRoutine(motionService->onMotion, this, &DanceRoutine::onMotion);
	}
}

void DanceRoutine::unbindEvents(){
	sm->unbindRoutine(moduleService->ModulesEvent);
	sm->unbindRoutine(com->OnCommand);
	sm->unbindRoutine(buttonInput->OnButtonEvent);
	sm->unbindRoutine(baseBoard->onProximityReading);
	sm->unbindRoutine(baseBoard->onProximityChange);
	sm->unbindRoutine(motionService->onMotion);
}

void DanceRoutine::pickRandomMove(){
	static constexpr uint8_t pairs[4][2] = {
		{ 0, 1 },
		{ 2, 3 },
		{ 4, 5 },
		{ 7, 8 },
	};

	if(waitingForCounterpart){
		currentMoveIndex = counterpartIndex;
		waitingForCounterpart = false;
	}else{
		uint8_t pairIndex;
		do{
			pairIndex = static_cast<uint8_t>(rand() % 4);
		}while(pairIndex == lastPairIndex);
		lastPairIndex = pairIndex;

		const bool swapped = rand() % 2;
		currentMoveIndex = pairs[pairIndex][swapped ? 1 : 0];
		counterpartIndex = pairs[pairIndex][swapped ? 0 : 1];
		waitingForCounterpart = true;
	}

	const DanceMove& move = DanceMoves[currentMoveIndex];
	baseBoard->setMotors(move.motorLeft, move.motorRight);
	moveEndMs = millis() + move.durationMs;
}

void DanceRoutine::playPhrase(Phrase phrase, Audio* audio){
	const int16_t id = Phrases::get(phrase);
	if(id < 0){
		CMF_LOG(DanceRoutine, LogLevel::Warning, "No phrase output for phrase %d", static_cast<int>(phrase));
		return;
	}
	const std::string text = Phrases::map(phrase, id);
	CMF_LOG(DanceRoutine, LogLevel::Info, "Playing: '%s'", text.c_str());
	audio->play(ServiceLocator::SpeechAudioGenInstance.get(), std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text, text));
}

void DanceRoutine::startListeningForStop(){
	std::vector<AudioFrontend::Phrase> phrases(std::begin(StopPhrases), std::end(StopPhrases));
	audioFrontend->setSpeechPhrases(phrases);
	sm->unbindRoutine(audioFrontend->onPhrase);
	sm->bindRoutine(audioFrontend->onPhrase, this, &DanceRoutine::onStopPhrase);
	audioFrontend->setMode(AudioFrontend::Mode::Speech);
	listeningForStop = true;
}

DanceRoutine::~DanceRoutine(){
	if(!started || finished){
		return;
	}

	const Application* app = ApplicationStatics::getApplication();
	Audio* audio = app->getService<Audio>();

	baseBoard->stopMotors();
	audioFrontend->setMode(AudioFrontend::Mode::Off);
	sm->unbindRoutine(audioFrontend->onPhrase);

	audioFrontend->unloadModel();
	unbindEvents();

	playPhrase(Phrase::DanceInterrupted, audio);
}

Routine::TickingState DanceRoutine::tick(float deltaTime){
	const Application* app = ApplicationStatics::getApplication();
	Audio* audio = app->getService<Audio>();

	if(!audio || !ServiceLocator::SpeechAudioGenInstance || !ServiceLocator::SpeechGenInstance){
		CMF_LOG(DanceRoutine, LogLevel::Error, "Missing required service(s)");
		return TickingState::Done;
	}

	if(phase == Phase::Init){
		baseBoard = app->getService<BaseBoard>();
		audioFrontend = app->getService<AudioFrontend>();
		motionService = app->getService<MotionService>();
		battery = app->getService<Battery>();
		moduleService = app->getService<ModuleService>();
		com = app->getService<Com>();
		buttonInput = app->getService<ISRButtonInput>();

		if(!baseBoard || !audioFrontend || !motionService || !battery || !moduleService || !com || !buttonInput){
			CMF_LOG(DanceRoutine, LogLevel::Error, "Missing required service(s)");
			return TickingState::Done;
		}

		if(baseBoard->getChargingState() != ChargingState::Unplugged){
			CMF_LOG(DanceRoutine, LogLevel::Info, "Charging - skipping");
			com->sendData(BB::State::Idle, BB::Action::Idle::CantMove, CantMoveData{.reason = CantMoveData::Reason::Charging});
			playPhrase(Phrase::CannotPerformPlugged, audio);
			return TickingState::Done;
		}

		if(!motionService->isUprightOnGround()){
			CMF_LOG(DanceRoutine, LogLevel::Info, "Not upright on the ground - skipping");
			com->sendData(BB::State::Idle, BB::Action::Idle::CantMove, CantMoveData{.reason = CantMoveData::Reason::Unstable});
			playPhrase(Phrase::CannotMove, audio);
			return TickingState::Done;
		}

		bindEvents();
		baseBoard->requestProximityState();
		proxDeadlineMs = millis() + ProxReadingTimeoutMs;

		phase = Phase::ProxCheck;
		return TickingState::Continue;
	}

	if(phase == Phase::ProxCheck){
		if(proxUnsafe){
			CMF_LOG(DanceRoutine, LogLevel::Info, "Unsafe proximity state - skipping");
			playPhrase(Phrase::CannotMove, audio);
			return TickingState::Done;
		}
		if(interrupted){
			return TickingState::Done;
		}
		if(!proxReadingReceived && millis() < proxDeadlineMs){
			return TickingState::Continue;
		}

		started = true;

		com->sendData(BB::State::Scenario, BB::Action::Scenario::Dance, DanceData{});

		playPhrase(Phrase::DanceStart, audio);

		const uint32_t duration = DanceMinMs + static_cast<uint32_t>(rand() % (DanceMaxMs - DanceMinMs + 1));
		danceEndMs = millis() + duration;
		plannedMs = duration;
		CMF_LOG(DanceRoutine, LogLevel::Info, "Dance duration: %u ms", duration);

		nextInterludeCheckMs = millis() + 3000;
		pickRandomMove();
		audioFrontend->loadModel();
		// Listening for "stop dancing" starts once the robot has finished talking (the Dancing phase checks):
		// listening while it speaks let its own voice end the dance

		phase = Phase::Dancing;
		return TickingState::Continue;
	}

	if(phase == Phase::Dancing){
		if(interrupted){
			stopReason = StopReason::Interrupted;
			phase = Phase::Cleanup;
			return TickingState::Continue;
		}

		if(stopVoiceDetected){
			CMF_LOG(DanceRoutine, Debug, "stop voiceline");
			stopReason = StopReason::Voice;
			phase = Phase::Cleanup;
			return TickingState::Continue;
		}

		if(millis() > danceEndMs){
			stopReason = StopReason::Natural;
			phase = Phase::Cleanup;
			return TickingState::Continue;
		}

		if(millis() > moveEndMs){
			pickRandomMove();
		}

		if(!listeningForStop && !audio->isPlaying()){
			startListeningForStop();
		}

		if(millis() > nextInterludeCheckMs){
			const uint32_t interval = InterludeCheckIntervalMinMs + static_cast<uint32_t>(rand() % (InterludeCheckIntervalMaxMs - InterludeCheckIntervalMinMs));
			nextInterludeCheckMs = millis() + interval;

			if(static_cast<uint8_t>(rand() % 100) < InterludeChancePercent){
				listeningForStop = false;
				audioFrontend->setMode(AudioFrontend::Mode::Off);
				playPhrase(Phrase::DanceInterlude, audio);
				// listening resumes above once the line has been spoken
			}
		}

		return TickingState::Continue;
	}

	if(phase == Phase::Cleanup){
		baseBoard->stopMotors();
		audioFrontend->setMode(AudioFrontend::Mode::Off);
		sm->unbindRoutine(audioFrontend->onPhrase);
		audioFrontend->unloadModel();
		unbindEvents();

		if(stopReason == StopReason::Voice){
			playPhrase(Phrase::DanceStopped, audio);
			audio->waitEnd(portMAX_DELAY);
		}else if(stopReason == StopReason::Interrupted){
			playPhrase(Phrase::DanceInterrupted, audio);
			audio->waitEnd(portMAX_DELAY);
		}

		finished = true;

		CMF_LOG(DanceRoutine, LogLevel::Info, "Dance ended, reason: %d", static_cast<int>(stopReason));
		{
			static constexpr const char* Reasons[] = { "natural", "voice", "interrupted" };
			const uint32_t ranMs = (uint32_t) (millis() - (danceEndMs - plannedMs));
			printf("[BBDANCE] event=end reason=%s cause=%s ran_ms=%lu planned_ms=%lu\n",
				   Reasons[static_cast<uint8_t>(stopReason)], stopReason == StopReason::Interrupted ? interruptCause.load() : "-",
				   (unsigned long) ranMs, (unsigned long) plannedMs);
		}
		return TickingState::Done;
	}

	return TickingState::Done;
}

void DanceRoutine::interrupt(const char* cause){
	if(!interrupted.exchange(true)){
		interruptCause = cause;
	}
}

void DanceRoutine::onModuleEvent(uint8_t bus, Modules::Type type, ModuleService::Action action){
	interrupt("module");
}

void DanceRoutine::onCommand(Ctrl::Command command){
	if(command == Ctrl::Command::Poke || command == Ctrl::Command::Listen){
		interrupt(command == Ctrl::Command::Poke ? "poke" : "listen");
	}
}

void DanceRoutine::onButtonEvent(int button, ISRButtonInput::Action action){
	if(action == ISRButtonInput::Action::Press){
		interrupt("button");
	}
}

void DanceRoutine::onProximityReading(ProxState front, ProxState bottom){
	proxReadingReceived = true;
	if(ServiceLocator::SettingsInstance && ServiceLocator::SettingsInstance->get().bottomSensor == BottomSensorMode::On){
		proxUnsafe = (bottom == ProxState::Uncovered) || (front == ProxState::Covered);
	} else{
		proxUnsafe = (front == ProxState::Covered);
	}
}

void DanceRoutine::onProximityChange(ProxSensor sensor, bool state){
	if(ServiceLocator::SettingsInstance && ServiceLocator::SettingsInstance->get().bottomSensor == BottomSensorMode::On){
		if(sensor == ProxSensor::Bottom && !state){
			interrupt("bottom_uncovered");
		}else if(sensor == ProxSensor::Front && state){
			interrupt("front_obstacle");
		}
	} else{
		if(sensor == ProxSensor::Front && state){
			interrupt("front_obstacle");
		}
	}
}

void DanceRoutine::onMotion(MotionType type){
	static constexpr const char* Names[] = { "motion_fall", "motion_upside_down", "motion_pickup", "motion_shake" };
	interrupt(static_cast<uint8_t>(type) < 4 ? Names[static_cast<uint8_t>(type)] : "motion");
}

void DanceRoutine::onStopPhrase(bool recognized, int index, const std::string& transcript, float confidence){
	if(recognized && (index == 0 || index == -1)){
		printf("[BBDANCE] event=heard_stop index=%d conf=%.2f transcript=%s\n", index, confidence, transcript.c_str());
		stopVoiceDetected = true;
	}
	listeningForStop = false;
}
