#ifndef BUTTERBOT_FIRMWARE_USERDATA_H
#define BUTTERBOT_FIRMWARE_USERDATA_H

#include <atomic>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <mutex>
#include <string>
#include <vector>
#include <nvs.h>

/**
 * Fork: what the owner builds in Dimension Hopper's Routines tab, kept on the robot (NVS "bbuser"):
 *   routines       - named lists of steps (say a line, drive, wait, LED module, dance), keys "r<i>"
 *   voice commands - a phrase (text + MultiNet phonemes) that runs a routine, key "cmds"
 *   schedules      - days + time of day that run a routine, key "sched"
 *   quiet hours    - a nightly window with no idle rambling / wandering, key "quiet"
 *
 * Routine text: the name on the first line, then one step per line:
 *   S <text>            say it (up to 100 characters)
 *   M <left> <right> <ms>  drive: motor speeds -100..100 for 100..5000 ms
 *   W <ms>              wait 100..10000 ms
 *   L on|off|strobe|breathe   the LED add-on module
 *   D <seconds>         dance (the built-in moves) for 2..30 s
 *
 * Loaded once at start-up; changed over USB (main.cpp). Writes must run on an internal-RAM stack (runFlashJob).
 */
namespace UserData {

inline constexpr size_t MaxRoutines = 12;
inline constexpr size_t MaxRoutineText = 1500;
inline constexpr size_t MaxSteps = 30;
inline constexpr size_t MaxCommands = 12;
inline constexpr size_t MaxPhrase = 40;
inline constexpr size_t MaxPhonemes = 63;
inline constexpr size_t MaxSchedules = 12;

struct Command {
	std::string phrase;    // what the owner types, also the AI brain's action name
	std::string phonemes;  // for the offline recogniser
	uint8_t routine = 0;
};

struct Schedule {
	bool used = false;
	bool on = false;
	uint8_t days = 0;       // bit 0 = Sunday .. bit 6 = Saturday (tm_wday)
	uint16_t minute = 0;    // minutes after midnight
	uint8_t routine = 0;
	int lastDay = -1;       // the day of the year it last ran (RAM only), so it runs once
};

inline std::mutex lock;
inline std::string routines[MaxRoutines];
inline std::vector<Command> commands;
inline Schedule schedules[MaxSchedules];
inline bool quietOn = false;
inline uint16_t quietStart = 22 * 60, quietEnd = 7 * 60;
inline std::atomic<int> runRequest{ -1 };   // a routine to start as soon as the robot is idle (console "rout run")

inline std::string readStr(nvs_handle_t h, const char* key){
	size_t len = 0;
	if(nvs_get_str(h, key, nullptr, &len) != ESP_OK || len == 0) return {};
	std::string s(len, '\0');
	if(nvs_get_str(h, key, s.data(), &len) != ESP_OK) return {};
	s.resize(len > 0 ? len - 1 : 0);
	return s;
}

inline void saveKey(const char* key, const std::string& value){
	nvs_handle_t h;
	if(nvs_open("bbuser", NVS_READWRITE, &h) != ESP_OK) return;
	if(value.empty()) nvs_erase_key(h, key);
	else nvs_set_str(h, key, value.c_str());
	nvs_commit(h);
	nvs_close(h);
}

/** Calls fn(line) for every non-empty line of s. */
template<typename F>
inline void eachLine(const std::string& s, F fn){
	size_t start = 0;
	while(start < s.size()){
		size_t end = s.find('\n', start);
		if(end == std::string::npos) end = s.size();
		if(end > start) fn(s.substr(start, end - start));
		start = end + 1;
	}
}

/** Checks a routine's text. Returns the number of steps, or -1 (and why) if the robot can't run it. */
// "A <name>" steps: built-in actions a routine can run. Only ones that just talk or animate and hook into no events
// (UserRoutine runs them inside itself); the names are what Dimension Hopper writes. Not the dice: they ask "how many?"
// and give up when nobody answers.
inline constexpr const char* BuiltinActions[] = { "time", "joke", "fact", "butter", "portal" };

inline bool isBuiltinAction(const char* name){
	for(const char* a : BuiltinActions){
		if(strcmp(a, name) == 0) return true;
	}
	return false;
}

inline int validate(const std::string& text, const char** why = nullptr){
	auto fail = [&](const char* w){ if(why) *why = w; return -1; };
	if(text.empty() || text.size() > MaxRoutineText) return fail("size");
	int steps = 0;
	bool first = true, bad = false;
	eachLine(text, [&](const std::string& line){
		if(bad) return;
		if(first){ first = false; if(line.size() > 24) bad = true; return; }
		const char kind = line[0];
		const char* args = line.size() > 2 ? line.c_str() + 2 : "";
		if(line.size() >= 2 && line[1] != ' '){ bad = true; return; }
		int a = 0, b = 0, c = 0;
		switch(kind){
			case 'S': bad = line.size() < 3 || line.size() > 102; break;
			case 'M': bad = sscanf(args, "%d %d %d", &a, &b, &c) != 3 || a < -100 || a > 100 || b < -100 || b > 100 || c < 100 || c > 5000; break;
			case 'W': bad = sscanf(args, "%d", &a) != 1 || a < 100 || a > 10000; break;
			case 'L': bad = strcmp(args, "on") != 0 && strcmp(args, "off") != 0 && strcmp(args, "strobe") != 0 && strcmp(args, "breathe") != 0; break;
			case 'D': bad = sscanf(args, "%d", &a) != 1 || a < 2 || a > 30; break;
			case 'A': bad = !isBuiltinAction(args); break;
			default: bad = true;
		}
		++steps;
	});
	if(first) return fail("empty");
	if(bad) return fail("step");
	if(steps == 0 || steps > (int) MaxSteps) return fail("steps");
	return steps;
}

inline std::string routineName(size_t i){
	if(i >= MaxRoutines) return {};
	std::lock_guard<std::mutex> g(lock);
	const std::string& t = routines[i];
	return t.substr(0, t.find('\n'));
}

inline std::string routineText(size_t i){
	if(i >= MaxRoutines) return {};
	std::lock_guard<std::mutex> g(lock);
	return routines[i];
}

inline bool hasMoves(size_t i){
	const std::string t = routineText(i);
	return t.find("\nM ") != std::string::npos || t.find("\nD ") != std::string::npos;
}

inline void setRoutine(size_t i, const std::string& text){
	if(i >= MaxRoutines) return;
	{
		std::lock_guard<std::mutex> g(lock);
		routines[i] = text;
	}
	char key[6];
	snprintf(key, sizeof(key), "r%u", (unsigned) i);
	saveKey(key, text);
}

inline std::string commandsText(){
	std::string s;
	for(const auto& c : commands){
		s += std::to_string(c.routine) + "\t" + c.phrase + "\t" + c.phonemes + "\n";
	}
	return s;
}

inline void saveCommands(){
	std::string s;
	{
		std::lock_guard<std::mutex> g(lock);
		s = commandsText();
	}
	saveKey("cmds", s);
}

/** Adds or replaces command `index` (index == size: a new one). */
inline bool setCommand(size_t index, uint8_t routine, const std::string& phrase, const std::string& phonemes){
	if(routine >= MaxRoutines || phrase.empty() || phrase.size() > MaxPhrase || phonemes.empty() || phonemes.size() > MaxPhonemes) return false;
	for(char c : phonemes){
		if(!isalpha((unsigned char) c) && c != ' ') return false;
	}
	{
		std::lock_guard<std::mutex> g(lock);
		if(index > commands.size() || (index == commands.size() && commands.size() >= MaxCommands)) return false;
		if(index == commands.size()) commands.emplace_back();
		commands[index] = { phrase, phonemes, routine };
	}
	saveCommands();
	return true;
}

inline bool deleteCommand(size_t index){
	{
		std::lock_guard<std::mutex> g(lock);
		if(index >= commands.size()) return false;
		commands.erase(commands.begin() + (long) index);
	}
	saveCommands();
	return true;
}

/** The routine for a command phrase (the AI brain answers with the phrase), or -1. */
inline int routineForPhrase(const std::string& phrase){
	std::lock_guard<std::mutex> g(lock);
	for(const auto& c : commands){
		if(c.phrase == phrase) return c.routine;
	}
	return -1;
}

inline std::string schedulesText(){
	std::string s;
	char buf[40];
	for(const auto& sc : schedules){
		if(!sc.used) continue;
		snprintf(buf, sizeof(buf), "%u %u %u %u\n", (unsigned) sc.days, (unsigned) sc.minute, (unsigned) sc.routine, sc.on ? 1u : 0u);
		s += buf;
	}
	return s;
}

inline void saveSchedules(){
	std::string s;
	char q[24];
	{
		std::lock_guard<std::mutex> g(lock);
		s = schedulesText();
		snprintf(q, sizeof(q), "%u %u %u", quietOn ? 1u : 0u, (unsigned) quietStart, (unsigned) quietEnd);
	}
	saveKey("sched", s);
	saveKey("quiet", q);
}

/** Packs the used schedules to the front (after a delete, so their indexes are 0..n-1). */
inline void packSchedules(){
	size_t o = 0;
	for(size_t i = 0; i < MaxSchedules; ++i){
		if(schedules[i].used) schedules[o++] = schedules[i];
	}
	for(; o < MaxSchedules; ++o) schedules[o] = {};
}

inline bool setSchedule(size_t index, uint8_t days, uint16_t minute, uint8_t routine, bool on){
	if(index >= MaxSchedules || routine >= MaxRoutines || minute >= 24 * 60 || (days & 0x7f) == 0) return false;
	{
		std::lock_guard<std::mutex> g(lock);
		size_t used = 0;
		while(used < MaxSchedules && schedules[used].used) ++used;
		if(index > used) return false;
		schedules[index] = { true, on, (uint8_t) (days & 0x7f), minute, routine, -1 };
	}
	saveSchedules();
	return true;
}

inline bool deleteSchedule(size_t index){
	{
		std::lock_guard<std::mutex> g(lock);
		if(index >= MaxSchedules || !schedules[index].used) return false;
		schedules[index] = {};
		packSchedules();
	}
	saveSchedules();
	return true;
}

inline void setQuiet(bool on, uint16_t start, uint16_t end){
	{
		std::lock_guard<std::mutex> g(lock);
		quietOn = on;
		quietStart = start % (24 * 60);
		quietEnd = end % (24 * 60);
	}
	saveSchedules();
}

/** True inside the quiet-hours window (it may cross midnight). */
inline bool isQuiet(const tm& now){
	std::lock_guard<std::mutex> g(lock);
	if(!quietOn || quietStart == quietEnd) return false;
	const int m = now.tm_hour * 60 + now.tm_min;
	return quietStart < quietEnd ? (m >= quietStart && m < quietEnd) : (m >= quietStart || m < quietEnd);
}

/**
 * The routine a schedule wants to run now, or -1. A schedule fires once a day, within 2 minutes of its time
 * (the robot may be busy at the exact minute).
 */
inline int dueSchedule(const tm& now){
	std::lock_guard<std::mutex> g(lock);
	const int m = now.tm_hour * 60 + now.tm_min;
	for(auto& sc : schedules){
		if(!sc.used || !sc.on || !(sc.days & (1 << now.tm_wday)) || sc.lastDay == now.tm_yday) continue;
		if(m >= sc.minute && m - sc.minute <= 2){
			sc.lastDay = now.tm_yday;
			if(!routines[sc.routine].empty()) return sc.routine;
		}
	}
	return -1;
}

inline void load(){
	nvs_handle_t h;
	if(nvs_open("bbuser", NVS_READONLY, &h) != ESP_OK) return;
	std::lock_guard<std::mutex> g(lock);
	for(size_t i = 0; i < MaxRoutines; ++i){
		char key[6];
		snprintf(key, sizeof(key), "r%u", (unsigned) i);
		routines[i] = readStr(h, key);
	}
	commands.clear();
	eachLine(readStr(h, "cmds"), [](const std::string& line){
		const size_t t1 = line.find('\t'), t2 = line.find('\t', t1 + 1);
		if(t1 == std::string::npos || t2 == std::string::npos || commands.size() >= MaxCommands) return;
		commands.push_back({ line.substr(t1 + 1, t2 - t1 - 1), line.substr(t2 + 1), (uint8_t) atoi(line.c_str()) });
	});
	size_t n = 0;
	eachLine(readStr(h, "sched"), [&n](const std::string& line){
		unsigned d, m, r, on;
		if(n < MaxSchedules && sscanf(line.c_str(), "%u %u %u %u", &d, &m, &r, &on) == 4 && r < MaxRoutines && m < 24 * 60){
			schedules[n++] = { true, on != 0, (uint8_t) (d & 0x7f), (uint16_t) m, (uint8_t) r, -1 };
		}
	});
	unsigned on, s, e;
	const std::string q = readStr(h, "quiet");
	if(sscanf(q.c_str(), "%u %u %u", &on, &s, &e) == 3){
		quietOn = on != 0;
		quietStart = (uint16_t) (s % (24 * 60));
		quietEnd = (uint16_t) (e % (24 * 60));
	}
	nvs_close(h);
}

} // namespace UserData

#endif //BUTTERBOT_FIRMWARE_USERDATA_H
