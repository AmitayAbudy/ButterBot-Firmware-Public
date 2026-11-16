#ifndef BUTTERBOT_FIRMWARE_CONVERSATION_H
#define BUTTERBOT_FIRMWARE_CONVERSATION_H

#include <atomic>
#include <cctype>
#include <cstring>
#include <cstdint>
#include <cstdio>
#include <string>
#include <Util/stdafx.h>

/**
 * Fork: conversation mode. Holding Poke + Summon on the controller sends Ctrl::Command::Converse; from then on the
 * robot goes back to listening after every reply (IdleState sends it straight back to ListenState) until:
 *   - any button is pressed, on the robot or the controller (main.cpp's global handlers, ListenState)
 *   - the owner says goodbye ("bye", "that's all", "stop talking"...)
 *   - MaxQuiet turns in a row heard nothing / nothing understood
 *   - the AI brain fails, or MaxTurns is reached
 * The brain is told (BBTALK conv=1), so it keeps the whole session as context instead of the last few exchanges.
 */
namespace Conversation {

inline constexpr int MaxQuiet = 2;
inline constexpr int MaxTurns = 25; // the WiFi driver loses ~0.6 KB of internal RAM per turn: 25 keeps a safe margin

inline std::atomic<bool> on{ false };
inline std::atomic<int> quiet{ 0 };   // turns in a row with nothing heard / understood
inline std::atomic<int> turns{ 0 };   // listens started in this conversation
inline std::atomic<uint32_t> endedAt{ 0 };

inline bool active(){ return on.load(); }

inline void start(){
	quiet = 0;
	turns = 0;
	if(!on.exchange(true)) printf("[BBCONV] event=start\n");
}

inline void end(const char* why){
	if(on.exchange(false)){
		endedAt = (uint32_t) millis() | 1;
		printf("[BBCONV] event=end why=%s turns=%d\n", why, turns.load());
	}
}

/** True for a moment after end(): the button that ended it shouldn't also do its usual thing. */
inline bool justEnded(){
	const uint32_t t = endedAt.load();
	return t != 0 && (uint32_t) millis() - t < 1500;
}

/** A turn that heard (or understood) nothing. Returns true when that ends the conversation. */
inline bool quietTurn(){
	if(!active()) return false;
	if(++quiet >= MaxQuiet){
		end("quiet");
		return true;
	}
	return false;
}

inline void heardSomething(){ quiet = 0; }

/** "Goodbye", "bye bye", "that's all", "stop talking", "see you later"... - a short utterance that ends the chat. */
inline bool isGoodbye(const std::string& heard){
	std::string s;
	for(char c : heard){
		if(isalpha((unsigned char) c) || c == ' ' || c == '\'') s += (char) tolower((unsigned char) c);
	}
	int words = 0;
	bool inWord = false;
	for(char c : s){
		if(c != ' ' && !inWord) ++words;
		inWord = c != ' ';
	}
	if(words == 0 || words > 6) return false;  // a long sentence that mentions "bye" is still a conversation
	for(const char* p : { "goodbye", "good bye", "bye", "that's all", "thats all", "stop talking", "see you",
	                      "good night", "goodnight", "end conversation", "stop the conversation", "we're done",
	                      "were done", "over and out" }){
		const size_t at = s.find(p);
		if(at == std::string::npos) continue;
		const size_t endAt = at + strlen(p);
		const bool startOk = at == 0 || s[at - 1] == ' ';
		const bool endOk = endAt >= s.size() || s[endAt] == ' ';
		if(startOk && endOk) return true;
	}
	return false;
}

} // namespace Conversation

#endif //BUTTERBOT_FIRMWARE_CONVERSATION_H
