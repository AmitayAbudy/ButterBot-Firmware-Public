#ifndef BUTTERBOT_FIRMWARE_PERSONAL_H
#define BUTTERBOT_FIRMWARE_PERSONAL_H

#include <mutex>
#include <string>
#include <vector>
#include <cstdio>
#include <nvs.h>
#include <esp_random.h>

/**
 * Fork: personal touches set from Dimension Hopper's Personalize tab, kept on the robot (NVS "bbpers"):
 *   the owner's name   - added to the greeting when it recognises the owner's face
 *   the robot's name   - for the PC's AI brain (read back with "names")
 *   the owner's own ramble lines - mixed into the idle rambles
 *   a start-up line    - said when it switches on, instead of the stock one ("{name}" = the owner's name)
 *   chat replies       - the owner's own answer to a chat voice line, by line index (keys "c<index>")
 * Loaded once at start-up; changed over USB ("names <hex> <hex>", "ramble add <hex>", "ramble clear",
 * "startline <hex|->", "chat set <index> <hex|->").
 * Writes must run on an internal-RAM stack (runFlashJob in main.cpp).
 */
namespace Personal {

inline constexpr size_t MaxName = 24;
inline constexpr size_t MaxLine = 100;
inline constexpr size_t MaxLines = 30;
inline constexpr size_t MaxChat = 64;   // chat line indexes we keep replies for (Scenarios.h has 48)

inline std::mutex lock;
inline std::string ownerName;
inline std::string robotName;
inline std::vector<std::string> lines;
inline std::string startLine;
inline std::string chatReplies[MaxChat];

inline std::string readStr(nvs_handle_t h, const char* key){
	size_t len = 0;
	if(nvs_get_str(h, key, nullptr, &len) != ESP_OK || len == 0) return {};
	std::string s(len, '\0');
	if(nvs_get_str(h, key, s.data(), &len) != ESP_OK) return {};
	s.resize(len > 0 ? len - 1 : 0);
	return s;
}

inline void load(){
	nvs_handle_t h;
	if(nvs_open("bbpers", NVS_READONLY, &h) != ESP_OK) return;
	std::lock_guard<std::mutex> g(lock);
	ownerName = readStr(h, "owner");
	robotName = readStr(h, "robot");
	lines.clear();
	const std::string all = readStr(h, "lines");
	size_t start = 0;
	while(start < all.size()){
		size_t end = all.find('\n', start);
		if(end == std::string::npos) end = all.size();
		if(end > start) lines.emplace_back(all.substr(start, end - start));
		start = end + 1;
	}
	startLine = readStr(h, "start");
	for(size_t i = 0; i < MaxChat; ++i){
		char key[8];
		snprintf(key, sizeof(key), "c%u", (unsigned) i);
		chatReplies[i] = readStr(h, key);
	}
	nvs_close(h);
}

/** Saves one string key (empty = erased). */
inline void saveKey(const char* key, const std::string& value){
	nvs_handle_t h;
	if(nvs_open("bbpers", NVS_READWRITE, &h) != ESP_OK) return;
	if(value.empty()) nvs_erase_key(h, key);
	else nvs_set_str(h, key, value.c_str());
	nvs_commit(h);
	nvs_close(h);
}

inline void setStartLine(const std::string& line){
	std::string v = line.substr(0, MaxLine);
	{
		std::lock_guard<std::mutex> g(lock);
		startLine = v;
	}
	saveKey("start", v);
}

/** The start-up line with "{name}" filled in, or "" for the stock one. */
inline std::string startupText(){
	std::lock_guard<std::mutex> g(lock);
	std::string s = startLine;
	for(size_t at; (at = s.find("{name}")) != std::string::npos;){
		s.replace(at, 6, ownerName.empty() ? std::string("human") : ownerName);
	}
	return s;
}

inline bool setChat(size_t index, const std::string& reply){
	if(index >= MaxChat) return false;
	std::string v = reply.substr(0, MaxLine);
	{
		std::lock_guard<std::mutex> g(lock);
		chatReplies[index] = v;
	}
	char key[8];
	snprintf(key, sizeof(key), "c%u", (unsigned) index);
	saveKey(key, v);
	return true;
}

/** The owner's reply to chat line `index`, or "" for the stock one. */
inline std::string chatReply(size_t index){
	if(index >= MaxChat) return {};
	std::lock_guard<std::mutex> g(lock);
	return chatReplies[index];
}

inline void save(){
	nvs_handle_t h;
	if(nvs_open("bbpers", NVS_READWRITE, &h) != ESP_OK) return;
	std::string all;
	{
		std::lock_guard<std::mutex> g(lock);
		nvs_set_str(h, "owner", ownerName.c_str());
		nvs_set_str(h, "robot", robotName.c_str());
		for(const auto& l : lines){
			all += l;
			all += '\n';
		}
	}
	nvs_set_str(h, "lines", all.c_str());
	nvs_commit(h);
	nvs_close(h);
}

inline void setNames(const std::string& owner, const std::string& robot){
	{
		std::lock_guard<std::mutex> g(lock);
		ownerName = owner.substr(0, MaxName);
		robotName = robot.substr(0, MaxName);
	}
	save();
}

inline bool addLine(const std::string& line){
	{
		std::lock_guard<std::mutex> g(lock);
		if(line.empty() || lines.size() >= MaxLines) return false;
		lines.emplace_back(line.substr(0, MaxLine));
	}
	save();
	return true;
}

inline void clearLines(){
	{
		std::lock_guard<std::mutex> g(lock);
		lines.clear();
	}
	save();
}

inline std::string owner(){
	std::lock_guard<std::mutex> g(lock);
	return ownerName;
}

/** One of the owner's lines, or "" when there are none. */
inline std::string randomLine(){
	std::lock_guard<std::mutex> g(lock);
	if(lines.empty()) return {};
	return lines[esp_random() % lines.size()];
}

inline size_t lineCount(){
	std::lock_guard<std::mutex> g(lock);
	return lines.size();
}

}

#endif //BUTTERBOT_FIRMWARE_PERSONAL_H
