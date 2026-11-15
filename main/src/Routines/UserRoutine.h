#ifndef BUTTERBOT_FIRMWARE_USERROUTINE_H
#define BUTTERBOT_FIRMWARE_USERROUTINE_H

#include <atomic>
#include <cstdint>
#include <string>
#include "Routine.h"
#include "Services/BaseBoard.h"
#include "Services/MotionService.h"
#include "Services/Com.h"
#include "Services/ISRButtonInput.h"
#include "CtrlData.h"

/**
 * Fork: runs one of the owner's routines (Services/UserData.h) step by step - started by a custom voice command, a
 * schedule or Dimension Hopper. Routines are made from argument-less factories, so the routine number is handed over
 * in `pending` just before. Driving follows the dance's safety rules: not while charging or not upright, and it stops
 * for an obstacle, an edge (bottom sensor on), being picked up, a button press or a poke.
 */
class UserRoutine : public Routine {
public:
	using Routine::Routine;
	~UserRoutine() override;
	TickingState tick(float deltaTime) override;

	static inline std::atomic<int> pending{ -1 };

private:
	void onCommand(Ctrl::Command command);
	void onButtonEvent(int button, ISRButtonInput::Action action);
	void onProximityChange(ProxSensor sensor, bool state);
	void onMotion(MotionType type);
	void interrupt(const char* cause);
	bool nextStep();          // starts the next step; false at the end
	void say(const std::string& text);
	void danceMove();
	void finish(const char* how);
	void startAction(const char* name);   // an A step: a built-in action, run inside this routine
	void endAction();

	enum class Phase : uint8_t { Init, Running, Done };
	Phase phase = Phase::Init;
	int index = -1;
	std::string name;
	std::string text;          // the routine (PSRAM when long); steps are read from it one at a time
	size_t pos = 0;            // where the next step starts in `text`
	int stepNo = 0;
	bool moves = false;
	bool bound = false;
	uint64_t stepEndMs = 0;    // M / W / D: when the step ends (0 = no timed step running)
	uint64_t danceEndMs = 0;
	uint64_t moveEndMs = 0;
	Routine* action = nullptr;  // the A step's built-in routine while it runs
	void* actionBuf = nullptr;  // its buffer (PSRAM), kept for the routine's life

	std::atomic<bool> interrupted{ false };
	std::atomic<const char*> cause{ "" };

	BaseBoard* baseBoard = nullptr;
	MotionService* motionService = nullptr;
	Com* com = nullptr;
	ISRButtonInput* buttonInput = nullptr;
};

#endif //BUTTERBOT_FIRMWARE_USERROUTINE_H
