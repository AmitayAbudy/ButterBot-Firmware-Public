#include "UserRoutine.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <Statics/ApplicationStatics.h>
#include <Core/Application.h>
#include <Services/Audio/Audio.h>
#include <BBData.h>
#include <Phrases.h>
#include <Util/ServiceLocator.h>
#include <Util/stdafx.h>
#include "States/BBStateMachine.h"
#include "Audio/SpeechAudioGen.h"
#include "Audio/SpeechAudioSource.h"
#include "Audio/SpeechGen.h"
#include "Services/LEDModuleControl.h"
#include "Services/UserData.h"
#include "States/ScenarioRoutineMappings.h"
#include <esp_heap_caps.h>

DEFINE_LOG(UserRoutine)

namespace {
	struct Move { int8_t left, right; uint16_t ms; };
	// The built-in dance's moves (DanceRoutine), picked at random for a D step
	constexpr Move DanceMoves[] = {
		{ 60, 60, 800 }, { -60, -60, 800 }, { -80, 80, 600 }, { 80, -80, 600 }, { -100, 100, 900 },
		{ 100, -100, 900 }, { 0, 0, 400 }, { 70, 35, 500 }, { 35, 70, 500 },
	};

	void playPhrase(Phrase phrase){
		Audio* audio = ApplicationStatics::getApplication()->getService<Audio>();
		const int16_t id = Phrases::get(phrase);
		if(audio == nullptr || id < 0 || !ServiceLocator::SpeechAudioGenInstance) return;
		audio->play(ServiceLocator::SpeechAudioGenInstance.get(),
		            std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text, Phrases::map(phrase, id)));
		audio->waitEnd(portMAX_DELAY);
	}
}

UserRoutine::~UserRoutine(){
	endAction();
	if(actionBuf != nullptr){
		heap_caps_free(actionBuf);
	}
	if(baseBoard != nullptr){
		baseBoard->stopMotors();
	}
	if(bound){
		sm->unbindRoutine(com->OnCommand);
		sm->unbindRoutine(buttonInput->OnButtonEvent);
		if(moves){
			sm->unbindRoutine(baseBoard->onProximityChange);
			sm->unbindRoutine(motionService->onMotion);
		}
	}
	if(phase == Phase::Running){
		printf("[BBROUT] event=end i=%d how=stopped\n", index);
	}
}

void UserRoutine::finish(const char* how){
	endAction();
	if(baseBoard != nullptr){
		baseBoard->stopMotors();
	}
	printf("[BBROUT] event=end i=%d how=%s steps=%d\n", index, how, stepNo);
	phase = Phase::Done;
}

Routine::TickingState UserRoutine::tick(float deltaTime){
	if(phase == Phase::Done){
		return TickingState::Done;
	}

	if(phase == Phase::Init){
		const Application* app = ApplicationStatics::getApplication();
		baseBoard = app->getService<BaseBoard>();
		motionService = app->getService<MotionService>();
		com = app->getService<Com>();
		buttonInput = app->getService<ISRButtonInput>();
		index = pending.exchange(-1);
		if(index < 0 || !baseBoard || !motionService || !com || !buttonInput){
			CMF_LOG(UserRoutine, LogLevel::Error, "No routine or missing service(s)");
			return TickingState::Done;
		}
		text = UserData::routineText((size_t) index);
		if(UserData::validate(text) < 0){
			printf("[BBROUT] event=end i=%d how=missing steps=0\n", index);
			return TickingState::Done;
		}
		pos = text.find('\n');
		name = text.substr(0, pos);
		pos = pos == std::string::npos ? text.size() : pos + 1;
		moves = UserData::hasMoves((size_t) index);

		if(moves){
			if(baseBoard->getChargingState() != ChargingState::Unplugged){
				com->sendData(BB::State::Idle, BB::Action::Idle::CantMove, CantMoveData{.reason = CantMoveData::Reason::Charging});
				playPhrase(Phrase::CannotPerformPlugged);
				printf("[BBROUT] event=end i=%d how=charging steps=0\n", index);
				return TickingState::Done;
			}
			if(!motionService->isUprightOnGround()){
				com->sendData(BB::State::Idle, BB::Action::Idle::CantMove, CantMoveData{.reason = CantMoveData::Reason::Unstable});
				playPhrase(Phrase::CannotMove);
				printf("[BBROUT] event=end i=%d how=not_upright steps=0\n", index);
				return TickingState::Done;
			}
		}

		sm->bindRoutine(com->OnCommand, this, &UserRoutine::onCommand);
		sm->bindRoutine(buttonInput->OnButtonEvent, this, &UserRoutine::onButtonEvent);
		if(moves){
			sm->bindRoutine(baseBoard->onProximityChange, this, &UserRoutine::onProximityChange);
			sm->bindRoutine(motionService->onMotion, this, &UserRoutine::onMotion);
		}
		bound = true;
		phase = Phase::Running;
		printf("[BBROUT] event=start i=%d\n", index);
		CMF_LOG(UserRoutine, LogLevel::Info, "Running routine %d '%s'", index, name.c_str());
	}

	if(interrupted){
		printf("[BBROUT] event=interrupted i=%d cause=%s\n", index, cause.load());
		finish("interrupted");
		return TickingState::Done;
	}

	if(action != nullptr){
		if(action->tick(deltaTime) != TickingState::Done){
			return TickingState::Continue;
		}
		endAction();
	}

	if(stepEndMs != 0){
		const uint64_t now = millis();
		if(danceEndMs != 0 && now < danceEndMs){
			if(now >= moveEndMs) danceMove();
			return TickingState::Continue;
		}
		if(now < stepEndMs){
			return TickingState::Continue;
		}
		stepEndMs = danceEndMs = 0;
		baseBoard->stopMotors();
	}

	if(!nextStep()){
		finish("done");
		return TickingState::Done;
	}
	return TickingState::Continue;
}

bool UserRoutine::nextStep(){
	while(pos < text.size()){
		size_t end = text.find('\n', pos);
		if(end == std::string::npos) end = text.size();
		const std::string line = text.substr(pos, end - pos);
		pos = end + 1;
		if(line.size() < 2){
			continue;
		}
		++stepNo;
		const char* args = line.c_str() + 2;
		int a = 0, b = 0, c = 0;
		switch(line[0]){
			case 'S':
				say(line.substr(2));
				return true; // spoken (blocking); the next tick checks for interruptions first
			case 'M':
				sscanf(args, "%d %d %d", &a, &b, &c);
				baseBoard->setMotors((int8_t) a, (int8_t) b);
				stepEndMs = millis() + (uint32_t) c;
				return true;
			case 'W':
				sscanf(args, "%d", &a);
				stepEndMs = millis() + (uint32_t) a;
				return true;
			case 'D':
				sscanf(args, "%d", &a);
				danceEndMs = stepEndMs = millis() + (uint32_t) a * 1000;
				moveEndMs = 0;
				danceMove();
				return true;
			case 'L':
				if(LEDModuleControl* leds = ApplicationStatics::getApplication()->getService<LEDModuleControl>()){
					if(strcmp(args, "on") == 0) leds->turnLedOn();
					else if(strcmp(args, "off") == 0) leds->turnLedOff();
					else if(strcmp(args, "strobe") == 0) leds->turnLedStrobe();
					else if(strcmp(args, "breathe") == 0) leds->turnLedBreathe();
				}
				continue; // instant: carry on with the next step
			case 'A':
				startAction(args);
				if(action == nullptr) continue; // unknown here (older list): skip it
				return true;
			default:
				continue;
		}
	}
	return false;
}

void UserRoutine::say(const std::string& line){
	Audio* audio = ApplicationStatics::getApplication()->getService<Audio>();
	if(audio == nullptr || !ServiceLocator::SpeechAudioGenInstance){
		return;
	}
	// The controller shows it like a chat reply: the routine's name as "heard", the line as "said"
	SubtitleData subtitle;
	subtitle.heard = name;
	subtitle.say = line;
	com->sendData(BB::State::Scenario, BB::Action::Scenario::Subtitle, subtitle);
	audio->play(ServiceLocator::SpeechAudioGenInstance.get(), std::make_unique<SpeechAudioSource>(SpeechGen::InputType::Text, line));
	audio->waitEnd(portMAX_DELAY);
}

void UserRoutine::startAction(const char* what){
	std::pair<BB::Action::Scenario, ScenarioData> scenario{ BB::Action::Scenario::Joke, {} };
	if(strcmp(what, "time") == 0) scenario = { BB::Action::Scenario::CurrentTime, {} };
	else if(strcmp(what, "joke") == 0) scenario = { BB::Action::Scenario::Joke, {} };
	else if(strcmp(what, "fact") == 0) scenario = { BB::Action::Scenario::Fact, {} };
	else if(strcmp(what, "butter") == 0) scenario = { BB::Action::Scenario::PassTheButter, {} };
	else if(strcmp(what, "portal") == 0) scenario = { BB::Action::Scenario::OpenPortal, {} };
	else return;
	const RoutineFactory factory = routineForScenario(scenario);
	if(factory == nullptr) return;
	if(actionBuf == nullptr){
		actionBuf = heap_caps_malloc(RoutineBufSize, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
		if(actionBuf == nullptr) return;
	}
	action = factory(actionBuf, sm);
	printf("[BBROUT] event=action i=%d what=%s\n", index, what);
}

void UserRoutine::endAction(){
	if(action != nullptr){
		action->~Routine();
		action = nullptr;
	}
}

void UserRoutine::danceMove(){
	const Move& m = DanceMoves[rand() % (sizeof(DanceMoves) / sizeof(DanceMoves[0]))];
	baseBoard->setMotors(m.left, m.right);
	moveEndMs = millis() + m.ms;
}

void UserRoutine::interrupt(const char* why){
	if(!interrupted.exchange(true)){
		cause = why;
	}
}

void UserRoutine::onCommand(Ctrl::Command command){
	if(command == Ctrl::Command::Poke || command == Ctrl::Command::Listen){
		interrupt(command == Ctrl::Command::Poke ? "poke" : "listen");
	}
}

void UserRoutine::onButtonEvent(int button, ISRButtonInput::Action action){
	if(action == ISRButtonInput::Action::Press){
		interrupt("button");
	}
}

void UserRoutine::onProximityChange(ProxSensor sensor, bool state){
	if(sensor == ProxSensor::Front && state){
		interrupt("front_obstacle");
	}else if(sensor == ProxSensor::Bottom && !state && ServiceLocator::SettingsInstance &&
	         ServiceLocator::SettingsInstance->get().bottomSensor == BottomSensorMode::On){
		interrupt("bottom_uncovered");
	}
}

void UserRoutine::onMotion(MotionType type){
	interrupt("motion");
}
