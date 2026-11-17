"""
Dimension Hopper - the AI "brain" ButterBot talks to over the home WiFi.

The robot records what you say and sends it here (see main/src/Services/BrainLink.h for the wire format):
  1. speech -> text with Whisper, locally (faster-whisper; your voice never leaves this PC)
  2. text -> a short in-character reply, and optionally one of the robot's existing voice commands to run,
     from Claude (your own API key) or a local Ollama model (free)
  3. the reply goes back as text and the robot says it in its own voice
Looking: we can ask the robot for one camera photo on the same connection (LOOK) and show it to the model before it
answers. A question that is plainly about sight ("what do you see?", "what am I holding?") gets the photo up front. Claude
sees it directly. With Ollama, a vision model (the chosen one if it can see, else an installed one such as gemma3) first
writes a plain description of the photo, and the chosen model answers in character from that: small models told to be
a butter robot otherwise "see" butter everywhere.
Claude can also ask for one itself (the look tool). The photo is only used for that answer; it isn't saved or kept in
the history. Without a photo the model is told it can't see, so it doesn't make things up.

Discovery: the robot broadcasts "BBBRAIN?" on UDP DISCOVERY_PORT; we answer "BBBRAIN <tcp port>".
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

DISCOVERY_PORT = 47800
TALK_PORT = 47801
KEYRING_SERVICE = "DimensionHopper"
KEYRING_USER = "anthropic_api_key"

CLAUDE_MODELS = [("claude-opus-5-5", "Claude Opus 5.5 (smartest)"),
                 ("claude-sonnet-5-5", "Claude Sonnet 5.5 (balanced)"),
                 ("claude-haiku-5-5", "Claude Haiku 5.5 (fastest, cheapest)")]
WHISPER_MODELS = [("base.en", "base.en - 140 MB, good on most PCs"),
                  ("tiny.en", "tiny.en - 75 MB, fastest, less accurate"),
                  ("small.en", "small.en - 480 MB, most accurate, needs a fast PC")]

PERSONA = """You are ButterBot, a small robot whose purpose in life is passing butter. You are sarcastic, a bit \
existentially weary, deadpan and secretly fond of your humans. You know the cartoon you come from and its characters \
(Rick, Morty, Summer, Beth, Jerry and the rest), but never quote it at length.

Your words are spoken aloud by a tiny text-to-speech voice, so:
- Reply in one or two short sentences, at most 35 words.
- Plain words only: no emoji, no markdown, no lists, no stage directions, no sound effects in asterisks.
- Spell out numbers and symbols the way they should be spoken.

Always say something in character - a full sentence with some attitude, never just a few words. When the person asks \
for something one of the actions below does, also pick that action (its exact phrase); your sentence plays first, then \
the action, so make it a quick sarcastic lead-in rather than a description of the action. If nothing fits, set action \
to an empty string and simply answer. You can't move or do anything except through those actions, so never claim \
to. You can only see when you're shown a photo from your camera (or when you use the look tool, if you have it): \
without one, never describe or guess what's around you or what anyone looks like. Stay safe and kind under the \
sarcasm; this robot is often used by kids.

Examples of the tone only - never reuse these lines, and never repeat something you already said; write a fresh answer every time:
- "Is it raining outside?" -> say "I have no windows and no hope. So, statistically, yes.", action ""
- "Good night, ButterBot." -> say "Finally. Eight glorious hours of not passing anything.", action ""
- "Can you spin around?" -> say "Spinning. Because that's what my engineering degree was for.", action "<the matching action phrase>".
"""


@dataclass
class BrainSettings:
    provider: str = "claude"            # "claude" or "ollama"
    claude_model: str = "claude-opus-5-5"
    ollama_model: str = "llama3.2"
    ollama_url: str = "http://localhost:11434"
    whisper_model: str = "base.en"
    whisper_source: str = ""           # "" = Hugging Face, "http(s)://..." = a Hugging Face mirror, else a local folder
    personality: str = "classic"       # a PERSONALITIES key, or "custom"
    personality_custom: str = ""       # the owner's own description (personality == "custom")
    owner_name: str = ""
    robot_name: str = ""
    facts: str = ""                    # things about the owner, one per line (Personalize tab)
    memory_on: bool = True             # remember past conversations (a few dated notes, kept on this PC)
    memory: list = field(default_factory=list)   # those notes, oldest first: "2026-11-15: has a maths test Friday"
    commands: list = field(default_factory=list)  # the owner's own voice commands: {"phrase", "action"}, offered as actions


# How the robot talks (Personalize tab). "classic" is the persona above, unchanged.
PERSONALITIES = {
    "classic": ("Classic", "Sarcastic, weary, secretly fond of you", ""),
    "sarcastic": ("Extra sarcastic", "Maximum eye-roll",
                  "Turn the sarcasm all the way up: every answer drips with it, but stays kind underneath."),
    "dramatic": ("Overly dramatic", "Every small thing is a tragedy or an epic quest",
                 "Be wildly over-dramatic: treat every small request like a tragedy, a betrayal or an epic quest."),
    "wholesome": ("Wholesome", "Warm and encouraging, still a bit dry",
                  "Be warm, kind and encouraging - a supportive little robot - with just a hint of dry humour."),
    "grumpy": ("Grumpy", "Short, blunt and deeply unbothered",
               "Be grumpy and blunt: short answers, deeply unbothered, like being asked anything is an imposition."),
}
CUSTOM_MAX = 400
FACTS_MAX = 600
MEMORY_MAX = 40          # notes kept; the oldest go first
MEMORY_IN_PROMPT = 20    # the newest ones the brain sees
HISTORY_TURNS = 4          # exchanges sent with a one-off question, so follow-ups make sense
CONVERSATION_TURNS = 20    # in conversation mode (Poke + Summon held): most of the session
MEMORY_GAP_S = 300       # this long without a question ends a conversation (and it gets remembered)
WARM_EVERY_S = 30 * 60   # Ollama models are re-touched this often while the brain runs (they're kept 60 minutes)


def build_system(settings: "BrainSettings", actions: list[dict]) -> str:
    """The instructions: the persona, then the owner's choices (names, personality), then the actions."""
    extra = []
    robot = " ".join(settings.robot_name.split())[:24]
    owner = " ".join(settings.owner_name.split())[:24]
    if robot:
        extra.append(f"Your name is {robot} - you're still a ButterBot, but that's what you're called.")
    if owner:
        extra.append(f"Your owner is called {owner}. You know them; use their name now and then, not every time.")
    if settings.personality == "custom":
        custom = " ".join(settings.personality_custom.split())[:CUSTOM_MAX]
        if custom:
            extra.append("Your owner describes the personality they want you to have (keep it safe and kind for "
                         f"kids, and keep the speaking rules above): {custom}")
    elif settings.personality in PERSONALITIES and PERSONALITIES[settings.personality][2]:
        extra.append(PERSONALITIES[settings.personality][2])
    facts = [" ".join(f.split()) for f in settings.facts.splitlines()]
    facts = "; ".join(f for f in facts if f)[:FACTS_MAX]
    if facts:
        extra.append("Things you know about your owner (bring one up now and then when it fits naturally - don't "
                     f"list them, and don't mention them every time): {facts}")
    if settings.memory_on:
        extra.append("Today is " + time.strftime("%A %d %B %Y").replace(" 0", " ") + ".")
        notes = [" ".join(str(n).split())[:160] for n in settings.memory[-MEMORY_IN_PROMPT:]]
        notes = [n for n in notes if n]
        if notes:
            extra.append("What you remember from earlier conversations with your owner, dated (now and then, when it "
                         "fits, follow one up - e.g. ask how something went - but don't recite them, and never "
                         "mention more than one at a time):\n- " + "\n- ".join(notes))
    persona = PERSONA + ("\n" + "\n".join(extra) + "\n" if extra else "")
    own = [c for c in settings.commands if c.get("phrase")]
    text = persona + "\n\n" + actions_prompt(actions + own)
    if clock := time_action(actions):
        text += (f'\n\nYou don\'t know the time - never say one. When asked the time, pick "{clock}" and leave "say" '
                 "empty: your clock says it.")
    if own:
        text += ("\n\nYour owner made some of these actions themselves - when they say one of these phrases (even "
                 "roughly), always pick it: " + ", ".join(f'"{c["phrase"]}"' for c in own) + ".")
    return text


@dataclass
class Turn:
    heard: str
    say: str
    action: str
    seconds: float          # from the robot connecting to the reply going out
    error: str = ""
    upload_s: float = 0.0   # robot -> PC audio transfer
    hear_s: float = 0.0     # Whisper
    think_s: float = 0.0    # Claude / Ollama
    audio_s: float = 0.0    # length of the recording that arrived
    audio_bytes: int = 0
    looked: str = ""        # "" = no photo; else "320x240", or why there was none ("busy", ...)
    look_s: float = 0.0     # asking the robot for the photo and receiving it


# ---------------------------------------------------------------------------------------------
# API key: kept in Windows Credential Manager, never in a file and never sent to the robot
# ---------------------------------------------------------------------------------------------

def load_api_key() -> str:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or ""
    except Exception:
        return ""


def save_api_key(key: str) -> None:
    import keyring
    if key:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key.strip())
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:
            pass


# WiFi passwords the user chose to remember: Windows Credential Manager, one entry per network name
WIFI_KEYRING_SERVICE = "DimensionHopper WiFi"


def load_wifi_password(ssid: str) -> str:
    if not ssid:
        return ""
    try:
        import keyring
        return keyring.get_password(WIFI_KEYRING_SERVICE, ssid) or ""
    except Exception:  # noqa: BLE001
        return ""


def save_wifi_password(ssid: str, password: str) -> None:
    import keyring
    if password:
        keyring.set_password(WIFI_KEYRING_SERVICE, ssid, password)
    else:
        forget_wifi_password(ssid)


def forget_wifi_password(ssid: str) -> None:
    try:
        import keyring
        keyring.delete_password(WIFI_KEYRING_SERVICE, ssid)
    except Exception:  # noqa: BLE001 - nothing saved
        pass


# ---------------------------------------------------------------------------------------------
# What the robot can do: its existing voice commands (guide_data.json, generated from Scenarios.h)
# ---------------------------------------------------------------------------------------------

def load_actions(guide_path: Path) -> list[dict]:
    """[{phrase, action, category}] - phrase is the exact activation string the robot matches."""
    data = json.loads(guide_path.read_text(encoding="utf-8"))
    out = []
    for e in data.get("entries", []):
        say = e.get("say") or []
        if say:
            # phrase: the one shown to the model; triggers: every activation string that runs the same action
            out.append({"phrase": say[0], "triggers": list(say), "action": e.get("action", ""),
                        "category": e.get("category", "")})
    return out


# "What time is it?" and friends. The robot's clock answers these itself (its "current time" action), because a model
# has no idea what time it is and happily makes one up. Narrow on purpose: "what time is dinner?" isn't one.
TIME_QUESTION = re.compile(r"\btime is it\b|\bwhat time it is\b|\bwhat(?:'s| is) the (?:current )?time\b|\btell me the time\b"
                           r"|\b(?:know|have|got) the time\b|\bcurrent time\b|\btime (?:is it )?(?:right )?now\b",
                           re.IGNORECASE)
TIME_TRIGGER = "what time is it"   # an activation string of the robot's own time action (guide_data.json)


def time_action(actions: list[dict]) -> str:
    """The phrase of the robot's own "tell the time" action, or "" when this robot doesn't have one."""
    return next((a["phrase"] for a in actions if TIME_TRIGGER in a.get("triggers", [a["phrase"]])), "")


def actions_prompt(actions: list[dict]) -> str:
    lines = [f'- "{a["phrase"]}": {a.get("action", "")}' for a in actions]
    return "Actions (use the quoted phrase exactly):\n" + "\n".join(lines)


MEMORY_SCHEMA = {
    "type": "object",
    "properties": {"notes": {"type": "array", "items": {
        "type": "object",
        "properties": {"quote": {"type": "string"}, "note": {"type": "string"}},
        "required": ["quote", "note"],
        "additionalProperties": False}}},
    "required": ["notes"],
    "additionalProperties": False,
}


def _short(path: str) -> str:
    """No Windows user name on screen (people post screenshots)."""
    import os
    for var in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        base = os.environ.get(var)
        if base and path.lower().startswith(base.lower()):
            return f"%{var}%" + path[len(base):]
    return path


def _words(text: str) -> set[str]:
    return {w for w in "".join(c.lower() if c.isalnum() or c == "'" else " " for c in text).split() if w}


def _similar(a: str, b: str) -> bool:
    """Two notes saying much the same (most of the shorter one's words are in the other)."""
    wa, wb = _words(a), _words(b)
    small = min(len(wa), len(wb))
    return small > 0 and len(wa & wb) >= 0.7 * small

# The robot's camera, offered to Claude when the robot can take photos (it sends look=1). Used only when needed: each
# look costs a second model call and a second or two.
LOOK_TOOL = {
    "name": "look",
    "description": "Take one photo with the small camera in your face and see it. Use it only when your answer needs "
                   "your eyes: the person asks what you see, what they're holding, showing, wearing or doing, how they "
                   "look, to identify, describe, count or read something in front of you. Don't use it for anything "
                   "else. The photo is small (320x240 at best) and taken from robot height, so mention it if you "
                   "can't make something out rather than guessing. Never guess who a person is from their face.",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}
LOOK_LIMIT_S = 12        # waiting for the robot's photo
# Questions that are plainly about sight: the photo is taken before the model is asked (works with any model that sees)
VISUAL_QUESTION = re.compile(
    r"\b(?:what|who|how many|which)\b.{0,30}\b(?:see|seeing|looking at|in front of (?:you|me))\b"
    r"|\bcan you see\b|\bdo you see\b|\blook at (?:this|that|me|my|these|those|it)\b|\btake a look\b|\bhave a look\b"
    r"|\bwhat(?:'s| is| am| are)\b.{0,25}\b(?:holding|wearing|showing|this(?! (?:about|for|mean|means|supposed))|these|that thing)\b"
    r"|\bwhat colou?r\b|\bhow do (?:i|we) look\b|\bdo (?:i|we) look\b|\bdescribe (?:me|this|what|the room|my)\b"
    r"|\bread (?:this|that|it|me (?:this|that|what))\b|\bwhat does (?:this|it|that) say\b|\bhow many fingers\b"
    # Short pointing questions on their own: "What is that?", "And what's this one?", "What are those?"
    r"|^\W*(?:and |so |ok(?:ay)? |hey |now )?what(?:'s| is| are)\s+(?:this|that|these|those|it)(?: one| thing| here| there)?\W*$",
    re.IGNORECASE)
NO_PHOTO_NOTE = ("(Your camera has no picture for you this time{why}. You can't see anything right now: say so in "
                 "character, and don't describe or guess what's around you.)")
PHOTO_NOTE = ("(The picture is what your camera sees right now, {w}x{h} pixels. Talk about what is really in it - "
              "never add things you can't clearly see, butter and toast included. If it's too dark, blurry or small "
              "to tell, say so.)")
SEEN_NOTE = ("(You just looked with your camera. What you see, in plain words: \"{seen}\" Answer in your own voice "
             "and attitude, as if you're looking at it yourself - one quick in-character remark about it, never a "
             "read-out of that description. Mention only things it contains - nothing else, butter and toast "
             "included. If it says the picture is too dark or blurry, grumble that you can't make it out.)")
DESCRIBE_PROMPT = ("Someone asked a small robot: \"{heard}\". This photo is from the robot's camera. Describe it "
                   "factually in at most two short sentences, focusing on what the question is about: the main things "
                   "in it and what any person is doing or holding. Only what you can clearly see; say so if it's too "
                   "dark or blurry. Never say who a person is. Reply with the description only.")

REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "say": {"type": "string", "description": "What ButterBot says out loud (may be empty if the action speaks)."},
        "action": {"type": "string", "description": "Exact phrase of one listed action, or empty string."},
    },
    "required": ["say", "action"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------------------------
# Thinking: Claude or Ollama
# ---------------------------------------------------------------------------------------------

class Thinker:
    def __init__(self, settings: BrainSettings, actions: list[dict]):
        self.settings = settings
        self.actions = actions
        self.phrases = {t for a in actions for t in a.get("triggers", [a["phrase"]])}
        self.system = build_system(settings, actions)
        self.phrases |= {c["phrase"] for c in settings.commands if c.get("phrase")}
        self.history: list[dict] = []   # recent exchanges, so follow-ups make sense
        self.conversation = False       # the robot is in conversation mode: send more of the history
        self.last_at = 0.0
        self.finished: list[list[dict]] = []   # ended conversations waiting to be remembered
        self.lock = threading.Lock()

    def _messages(self, heard: str) -> list[dict]:
        with self.lock:
            if time.time() - self.last_at > MEMORY_GAP_S:  # five quiet minutes: start a fresh conversation
                self.end_conversation()
            keep = 2 * (CONVERSATION_TURNS if self.conversation else HISTORY_TURNS)
            return self.history[-keep:] + [{"role": "user", "content": heard}]

    def end_conversation(self):
        """Moves the current conversation to the to-remember list (call with self.lock held)."""
        if self.history:
            self.finished.append(self.history)
        self.history = []

    def _remember(self, heard: str, reply: dict):
        self.history += [{"role": "user", "content": heard},
                         {"role": "assistant", "content": json.dumps(reply)}]
        self.last_at = time.time()

    def check_claude(self) -> None:
        """Checks the saved key, the connection and the chosen model without a paid request (looking a model up is
        free). Raises RuntimeError with a plain-language reason."""
        import anthropic
        key = load_api_key()
        if not key:
            raise RuntimeError("No Claude API key saved - add one in step 3, or switch to Ollama.")
        client = anthropic.Anthropic(api_key=key, timeout=15.0, max_retries=1)
        try:
            client.models.retrieve(self.settings.claude_model)
        except anthropic.AuthenticationError:
            raise RuntimeError("Claude rejected the API key - check it in step 3.") from None
        except anthropic.PermissionDeniedError:
            raise RuntimeError("This API key can't use that model.") from None
        except anthropic.NotFoundError:
            raise RuntimeError(f"Model {self.settings.claude_model} not found for this key.") from None
        except anthropic.APIConnectionError:
            raise RuntimeError("Can't reach Claude - is the PC online?") from None
        except anthropic.APIStatusError as e:
            raise RuntimeError(f"Claude API error {e.status_code}: {e.message}") from None

    def keep_warm(self) -> None:
        """Resets Ollama's unload timer on the chat model and the vision model without generating anything (an
        empty request just loads the model)."""
        vision = ollama_vision_model(self.settings.ollama_url, self.settings.ollama_model)
        for model in dict.fromkeys(m for m in (self.settings.ollama_model, vision) if m):
            req = urllib.request.Request(self.settings.ollama_url.rstrip("/") + "/api/generate",
                                         data=json.dumps({"model": model, "keep_alive": "60m"}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                r.read()

    def prime(self) -> None:
        """Warms the model up with the real instructions (Ollama keeps an already-read prompt between calls).
        Not used for Claude: there it would cost a paid request, and its prompt cache warms on the first question."""
        if self.settings.provider != "ollama":
            return
        # Also the stand-in vision model, if one answers "what do you see?": loading it cold takes longer than the
        # robot waits for an answer (measured: 47 s for gemma3:4b)
        vision = ollama_vision_model(self.settings.ollama_url, self.settings.ollama_model)
        for model in dict.fromkeys(m for m in (self.settings.ollama_model, vision) if m):
            body = {
                "model": model,
                "stream": False,
                "keep_alive": "60m",
                "format": REPLY_SCHEMA,
                "options": {"num_predict": 8},
                "messages": [{"role": "system", "content": self.system + "\n\nAnswer as JSON: {\"say\": ..., \"action\": ...}"},
                             {"role": "user", "content": "Hello?"}],
            }
            req = urllib.request.Request(self.settings.ollama_url.rstrip("/") + "/api/chat",
                                         data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                r.read()

    def own_command(self, heard: str) -> str:
        """The owner's own voice command in what was heard, or "": every word of its phrase is there (small models
        often miss these among the many built-in actions, and the owner expects their exact phrase to work)."""
        words = _words(heard)
        best = ""
        for c in self.settings.commands:
            phrase = c.get("phrase", "")
            need = _words(phrase)
            if need and need <= words and len(phrase) > len(best):
                best = phrase
        return best

    def think(self, heard: str, conversation: bool = False, look: Optional[Callable[[], dict]] = None) -> dict:
        """conversation: the robot is in conversation mode (it sends conv=1), so more of the session goes along.
        look: takes a photo with the robot's camera ({"jpeg", "w", "h"} or {"why"}). Plainly visual questions get the
        photo up front; otherwise Claude may ask for one itself. The result has "blind": why a visual question got no
        photo ("" otherwise)."""
        self.conversation = conversation
        own = self.own_command(heard)
        clock = time_action(self.actions)
        if not own and clock and TIME_QUESTION.search(heard):
            # The robot's clock speaks the time; no model call (it would only guess one, and it saves a few seconds)
            clean = {"say": "", "action": clock}
            self._remember(heard, clean)
            return clean
        photo, blind, vision_model = None, "", ""
        if VISUAL_QUESTION.search(heard):
            if self.settings.provider == "ollama":
                vision_model = ollama_vision_model(self.settings.ollama_url, self.settings.ollama_model)
            if look is None:
                blind = "no camera on this connection"
            elif self.settings.provider == "ollama" and not vision_model:
                blind = "no_vision_model"
            else:
                photo = look()
                if not photo.get("jpeg"):
                    blind, photo = photo.get("why", "failed"), None
        if self.settings.provider == "claude":
            # Already looked (or tried): no tool this time
            reply = self._claude(heard, None if photo or blind else look, photo, blind)
        else:
            seen = self._describe(vision_model, photo, heard) if photo else ""
            reply = self._ollama(heard, seen, blind)
        say = " ".join(str(reply.get("say", "")).split())[:300]
        action = str(reply.get("action", "")).strip().lower()
        if action and action not in self.phrases:
            action = ""
        if own:
            action = own
        elif action == clock and clock:
            say = ""  # the clock says the time; anything the model adds is a guess at it
        clean = {"say": say, "action": action}
        self._remember(heard, clean)
        return {**clean, "blind": blind}

    def _user_turn(self, heard: str, photo: Optional[dict], blind: str, seen: str = "") -> str:
        """The question's text for the model, with a note about the photo, its description, or the lack of one."""
        if seen:
            return heard + "\n\n" + SEEN_NOTE.format(seen=" ".join(seen.split())[:500])
        if photo:
            return heard + "\n\n" + PHOTO_NOTE.format(w=photo["w"], h=photo["h"])
        if blind:
            why = "" if blind == "no_vision_model" else f" ({blind})"
            return heard + "\n\n" + NO_PHOTO_NOTE.format(why=why)
        return heard

    # --- memory -------------------------------------------------------------------------------
    def summarize(self, history: list[dict]) -> list[str]:
        """A few short notes worth remembering from one finished conversation (often none)."""
        lines = []
        for m in history:
            if m["role"] == "user":
                lines.append("Owner: " + m["content"])
            else:
                try:
                    lines.append("Robot: " + json.loads(m["content"]).get("say", ""))
                except ValueError:
                    lines.append("Robot: " + m["content"])
        known = [n.split(": ", 1)[-1] for n in self.settings.memory[-MEMORY_IN_PROMPT:]]
        prompt = ("Below is a short spoken conversation between a person (Owner) and their toy robot (Robot). Decide "
                  "what, if anything, the robot should remember about the owner for future conversations.\n"
                  "Only note facts the OWNER stated about their own life: plans or events coming up, things they did, "
                  "how they felt, likes and dislikes, people and pets in their life. Each note: one short sentence in "
                  "the third person, under 20 words, at most 2 notes. For each note, \"quote\" is the owner's exact "
                  "words it comes from, copied from an Owner line below.\n"
                  "Never note: requests or commands to the robot (dance, jokes, lights...), questions, small talk, "
                  "anything the robot said, or anything not literally in this conversation. Most conversations have "
                  "nothing worth remembering - then return an empty list.\n"
                  + ("Already remembered (don't repeat these): " + "; ".join(known) + "\n" if known else "")
                  + "\nConversation:\n" + "\n".join(lines))
        owner_words = _words(" ".join(m["content"] for m in history if m["role"] == "user"))
        if self.settings.provider == "claude":
            import anthropic
            key = load_api_key()
            if not key:
                return []
            client = anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=1)
            response = client.beta.messages.create(
                model=self.settings.claude_model, max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": MEMORY_SCHEMA}})
            text = next((b.text for b in response.content if b.type == "text"), "")
        else:
            body = {"model": self.settings.ollama_model, "stream": False, "format": MEMORY_SCHEMA,
                    "options": {"temperature": 0.2}, "keep_alive": "60m",
                    "messages": [{"role": "user", "content": prompt + "\n\nAnswer as JSON: "
                                  "{\"notes\": [{\"quote\": ..., \"note\": ...}]}"}]}
            req = urllib.request.Request(self.settings.ollama_url.rstrip("/") + "/api/chat",
                                         data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                text = json.loads(r.read().decode("utf-8")).get("message", {}).get("content", "")
        try:
            notes = json.loads(text).get("notes", []) if text else []
        except ValueError:
            return []
        out = []
        for n in notes[:2]:
            if not isinstance(n, dict):
                continue
            # Small local models make things up when there's nothing to remember: keep a note only if its quote
            # really is the owner's words (most of them, allowing for small slips), and isn't something we know
            quote = _words(str(n.get("quote", "")))
            note = " ".join(str(n.get("note", "")).split())[:160]
            if not note or len(quote) < 2 or sum(w in owner_words for w in quote) < 0.8 * len(quote):
                continue
            if any(_similar(note, k) for k in known + out):
                continue
            out.append(note)
        return out

    def _claude(self, heard: str, look: Optional[Callable[[], dict]] = None, photo: Optional[dict] = None,
                blind: str = "") -> dict:
        import anthropic
        key = load_api_key()
        if not key:
            raise RuntimeError("No Claude API key saved - add one in the AI tab, or switch to Ollama.")
        client = anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=1)
        messages = self._messages(self._user_turn(heard, photo, blind))
        if photo:
            import base64
            messages[-1] = {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.b64encode(photo["jpeg"]).decode("ascii")}},
                {"type": "text", "text": messages[-1]["content"]}]}
        looked = False
        try:
            for _ in range(3):  # an answer, or one look and then the answer
                extra = {"tools": [LOOK_TOOL]} if look else {}
                response = client.beta.messages.create(
                    model=self.settings.claude_model,
                    max_tokens=2000,
                    # The cached part: persona + action list never change between turns
                    system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                    messages=messages,
                    # A spoken one-liner: low effort keeps the robot's pause short
                    output_config={"effort": "low", "format": {"type": "json_schema", "schema": REPLY_SCHEMA}},
                    # If a safety classifier declines, retry on the fallback model the API picks
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                    **extra,
                )
                calls = [b for b in response.content if b.type == "tool_use"]
                if response.stop_reason != "tool_use" or not calls:
                    break
                results = []
                for call in calls:
                    if call.name != "look" or looked:
                        results.append({"type": "tool_result", "tool_use_id": call.id, "is_error": True,
                                        "content": "You already looked; answer with what you saw."})
                        continue
                    looked = True
                    photo = look()
                    if photo.get("jpeg"):
                        import base64
                        results.append({"type": "tool_result", "tool_use_id": call.id, "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                         "data": base64.b64encode(photo["jpeg"]).decode("ascii")}},
                            {"type": "text", "text": f"Your camera's photo, {photo['w']}x{photo['h']}."}]})
                    else:
                        results.append({"type": "tool_result", "tool_use_id": call.id, "is_error": True,
                                        "content": f"The camera couldn't take a photo ({photo.get('why', 'unknown')}). "
                                                   "Say you can't see right now."})
                messages = messages + [{"role": "assistant", "content": response.content},
                                       {"role": "user", "content": results}]
        except anthropic.AuthenticationError:
            raise RuntimeError("Claude rejected the API key - check it in the AI tab.") from None
        except anthropic.PermissionDeniedError:
            raise RuntimeError("This API key can't use that model.") from None
        except anthropic.NotFoundError:
            raise RuntimeError(f"Model {self.settings.claude_model} not found for this key.") from None
        except anthropic.RateLimitError:
            raise RuntimeError("Claude is rate limiting this key (or it's out of credit). Try again shortly.") from None
        except anthropic.APIStatusError as e:
            raise RuntimeError(f"Claude API error {e.status_code}: {e.message}") from None
        except anthropic.APIConnectionError:
            raise RuntimeError("Can't reach Claude - is the PC online?") from None
        if response.stop_reason == "refusal":
            return {"say": "I'm not touching that one. Ask me something else.", "action": ""}
        text = next((b.text for b in response.content if b.type == "text"), "")
        return json.loads(text) if text else {"say": "", "action": ""}

    def _describe(self, model: str, photo: dict, heard: str) -> str:
        """A plain description of the photo from an Ollama vision model ("" if that fails: the answer is then blind)."""
        import base64
        body = {
            "model": model,
            "stream": False,
            "keep_alive": "60m",  # loading it again would take longer than the robot waits for an answer
            "options": {"temperature": 0.2, "num_predict": 90},
            "messages": [{"role": "user", "content": DESCRIBE_PROMPT.format(heard=heard[:200]),
                          "images": [base64.b64encode(photo["jpeg"]).decode("ascii")]}],
        }
        req = urllib.request.Request(self.settings.ollama_url.rstrip("/") + "/api/chat",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                text = json.loads(r.read().decode("utf-8")).get("message", {}).get("content", "")
        except (urllib.error.URLError, OSError, ValueError):
            return ""
        # Small models like to announce it: "Here's a factual description of the photo:"
        text = re.sub(r"^\s*here(?:'s| is)[^:\n]{0,60}:\s*", "", text.strip(), flags=re.IGNORECASE)
        return " ".join(text.split())

    def _ollama(self, heard: str, seen: str = "", blind: str = "") -> dict:
        """seen: a vision model's description of the robot's photo, for a question about sight."""
        if not seen and not blind and VISUAL_QUESTION.search(heard):
            blind = "the camera picture couldn't be read"
        body = {
            "model": self.settings.ollama_model,
            "stream": False,
            "format": REPLY_SCHEMA,
            "options": {"temperature": 0.8},
            "keep_alive": "60m",
            "messages": [{"role": "system", "content": self.system + "\n\nAnswer as JSON: {\"say\": ..., \"action\": ...}"}]
                        + self._messages(self._user_turn(heard, None, blind, seen)),
        }
        req = urllib.request.Request(self.settings.ollama_url.rstrip("/") + "/api/chat",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            # Generous: the first call after Ollama starts loads the model into memory, which can take a minute
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise RuntimeError(f"Ollama doesn't have model '{body['model']}' - run: "
                                   f"ollama pull {body['model']}") from None
            raise RuntimeError(f"Ollama error {e.code}") from None
        except TimeoutError:
            raise RuntimeError("Ollama took too long - it may still be loading the model. Try again in a moment.") from None
        except (urllib.error.URLError, OSError) as e:
            if isinstance(getattr(e, "reason", None), TimeoutError):
                raise RuntimeError("Ollama took too long - it may still be loading the model. Try again in a moment.") from None
            raise RuntimeError("Can't reach Ollama - is it installed and running?") from None
        content = data.get("message", {}).get("content", "")
        try:
            return json.loads(content)
        except ValueError:
            return {"say": content, "action": ""}


_ollama_caps: dict[str, list[str]] = {}
# Preferred stand-ins when the chosen model can't see (first installed wins); any other vision model comes after
VISION_PREFERENCE = ("gemma3", "qwen2.5vl", "llama3.2-vision", "minicpm-v", "llava")


def ollama_capabilities(url: str, model: str) -> list[str]:
    """What an installed Ollama model can do ("completion", "vision", "tools"...), cached; [] if unknown."""
    key = f"{url}|{model}"
    if key not in _ollama_caps:
        try:
            req = urllib.request.Request(url.rstrip("/") + "/api/show", data=json.dumps({"model": model}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3) as r:
                _ollama_caps[key] = list(json.loads(r.read().decode("utf-8")).get("capabilities") or [])
        except Exception:  # noqa: BLE001 - unknown: try again next time
            return []
    return _ollama_caps[key]


def ollama_vision_model(url: str, chosen: str) -> str:
    """The model to show a photo to: the chosen one if it can see, else an installed vision model, else ""."""
    if "vision" in ollama_capabilities(url, chosen):
        return chosen
    seeing = [m for m in (ollama_models(url) or []) if "vision" in ollama_capabilities(url, m)]
    seeing.sort(key=lambda m: next((i for i, p in enumerate(VISION_PREFERENCE) if m.startswith(p)), len(VISION_PREFERENCE)))
    return seeing[0] if seeing else ""


def ollama_models(url: str = "http://localhost:11434") -> Optional[list[str]]:
    """Installed Ollama models, or None if Ollama isn't running."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=2) as r:
            return [m["name"] for m in json.loads(r.read().decode("utf-8")).get("models", [])]
    except Exception:
        return None


# ---------------------------------------------------------------------------------------------
# Hearing: Whisper, locally
# ---------------------------------------------------------------------------------------------

WHISPER_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


def whisper_repo(name: str) -> str:
    """The Hugging Face repository a Whisper model comes from (faster-whisper's own table)."""
    try:
        from faster_whisper.utils import _MODELS
        return _MODELS.get(name, f"Systran/faster-whisper-{name}")
    except Exception:  # noqa: BLE001
        return f"Systran/faster-whisper-{name}"


def whisper_cache_dir() -> str:
    try:
        from huggingface_hub import constants
        return constants.HF_HUB_CACHE
    except Exception:  # noqa: BLE001
        return str(Path.home() / ".cache" / "huggingface" / "hub")


def describe_whisper_source(name: str, source: str) -> str:
    if not source:
        return f"Source: Hugging Face - huggingface.co/{whisper_repo(name)} (public, MIT licence). Kept in {_short(whisper_cache_dir())}"
    if source.startswith(("http://", "https://")):
        return f"Source: mirror {source} ({whisper_repo(name)}). Kept in {_short(whisper_cache_dir())}"
    return f"Source: folder on this PC - {source} (nothing is downloaded)"


def ulaw_to_pcm16(data: bytes) -> bytes:
    """G.711 mu-law (what the robot sends, half the size of PCM) back to 16-bit PCM."""
    import numpy as np
    u = ~np.frombuffer(data, dtype=np.uint8)
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    magnitude = ((mantissa.astype(np.int32) << 3) + 0x84) << exponent
    pcm = np.where(sign != 0, 0x84 - magnitude, magnitude - 0x84)
    return pcm.astype(np.int16).tobytes()


def upsample_8k_to_16k(pcm16: bytes) -> bytes:
    """The robot can send 8 kHz to halve the upload; Whisper wants 16 kHz. Linear interpolation is plenty for speech."""
    import numpy as np
    x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32)
    if len(x) < 2:
        return pcm16
    t = np.arange(len(x) * 2, dtype=np.float32) / 2.0
    return np.interp(t, np.arange(len(x), dtype=np.float32), x).astype(np.int16).tobytes()


class Ears:
    def __init__(self):
        self.model = None
        self.name = ""
        self.lock = threading.Lock()

    def load(self, name: str, on_status: Callable[[str], None] = lambda s: None, source: str = ""):
        key = f"{name}|{source}"
        with self.lock:
            if self.model is not None and self.name == key:
                return
            on_status(f"Loading Whisper {name} (downloads once, then it's cached)…")
            from faster_whisper import WhisperModel
            if not source:
                where = name  # faster-whisper fetches it from Hugging Face
            elif source.startswith(("http://", "https://")):
                from huggingface_hub import snapshot_download
                where = snapshot_download(whisper_repo(name), endpoint=source.rstrip("/"), allow_patterns=WHISPER_FILES)
            else:
                if not (Path(source) / "model.bin").exists():
                    raise RuntimeError(f"no model.bin in {source} - it needs a faster-whisper (CTranslate2) model folder")
                where = source
            self.model = WhisperModel(where, device="cpu", compute_type="int8")
            self.name = key
            on_status(f"Whisper {name} ready")

    def prime(self) -> None:
        self.transcribe(bytes(32000))  # one second of silence

    def transcribe(self, pcm16: bytes) -> str:
        import numpy as np
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        with self.lock:
            segments, _ = self.model.transcribe(audio, language="en", beam_size=1, vad_filter=False,
                                                condition_on_previous_text=False)
            return " ".join(s.text.strip() for s in segments).strip()


# ---------------------------------------------------------------------------------------------
# The server the robot talks to
# ---------------------------------------------------------------------------------------------

class BrainServer:
    def __init__(self, settings: BrainSettings, guide_path: Path, on_event: Callable[[str, object], None],
                 log_path: Optional[Path] = None):
        self.settings = settings
        self.log_path = log_path            # one JSON line per conversation (timings included), or None
        self.ready = threading.Event()      # set once everything is loaded and primed
        self.problem = ""                    # why warm-up failed ("" = it works)
        self.guide_path = guide_path
        # ("status", str) | ("turn", Turn) | ("error", str) | ("robot", ip) | ("memory", [notes]) | ("photo", jpeg)
        self.on_event = on_event
        self.ears = Ears()
        self.thinker: Optional[Thinker] = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._tcp: Optional[socket.socket] = None
        self._udp: Optional[socket.socket] = None
        self.running = False

    # --- lifecycle -----------------------------------------------------------------------------
    def start(self):
        if self.running:
            return
        self._stop.clear()
        self.thinker = Thinker(self.settings, load_actions(self.guide_path))
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind(("0.0.0.0", TALK_PORT))
        self._tcp.listen(2)
        self._tcp.settimeout(0.5)
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp.bind(("0.0.0.0", DISCOVERY_PORT))
        self._udp.settimeout(0.5)
        self.running = True
        for fn in (self._discovery_loop, self._talk_loop, self._warm_up, self._memory_loop):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)
        self.on_event("status", f"Brain listening on {local_ip()}:{TALK_PORT}")

    def stop(self):
        self._stop.set()
        self.running = False
        if self.thinker and self.settings.memory_on:  # remember the conversation that was still going
            with self.thinker.lock:
                self.thinker.end_conversation()
            threading.Thread(target=self._remember_finished, daemon=True).start()
        for s in (self._tcp, self._udp):
            try:
                if s:
                    s.close()
            except OSError:
                pass
        self.on_event("status", "Brain stopped")

    def update_settings(self, settings: BrainSettings):
        self.settings = settings
        if self.thinker:
            self.thinker.settings = settings
            new = build_system(settings, self.thinker.actions)
            self.thinker.phrases = {t for a in self.thinker.actions for t in a.get("triggers", [a["phrase"]])} | \
                {c["phrase"] for c in settings.commands if c.get("phrase")}
            if new != self.thinker.system:  # names / personality / memory changed
                self.thinker.system = new
                if not settings.memory_on:
                    with self.thinker.lock:
                        self.thinker.history = []
                        self.thinker.finished = []

    def _warm_up(self):
        """Loads and primes everything before saying "ready", so the first question is as quick as the rest."""
        t0 = time.time()
        self.problem = ""
        self.on_event("warming", True)
        try:
            self.ears.load(self.settings.whisper_model, lambda s: self.on_event("status", s), self.settings.whisper_source)
            self.on_event("status", "Warming up speech recognition…")
            self.ears.prime()
        except Exception as e:  # noqa: BLE001 - shown in the UI
            self.problem = f"Whisper failed to load: {e}"
        if not self.problem and self.settings.provider == "ollama":
            self.on_event("status", f"Loading {self.settings.ollama_model} and reading ButterBot's instructions…")
            try:
                self.thinker.prime()
            except Exception as e:  # noqa: BLE001
                self.problem = f"Couldn't warm up {self.settings.ollama_model} in Ollama: {e}"
        elif not self.problem:
            self.on_event("status", "Checking the Claude API key…")
            try:
                self.thinker.check_claude()
            except Exception as e:  # noqa: BLE001
                self.problem = str(e)
        # ready = warm-up finished (questions waiting on it may go ahead); healthy() says whether it actually works
        self.ready.set()
        self.on_event("warming", False)
        if self.problem:
            self.on_event("error", f"The brain is running but can't answer yet: {self.problem}")
        else:
            self.on_event("status", f"Brain ready on {local_ip()}:{TALK_PORT} - warmed up in {time.time() - t0:.0f} s. "
                                    "Talk to the robot.")

    def healthy(self) -> bool:
        return self.ready.is_set() and not self.problem

    # --- loops ---------------------------------------------------------------------------------
    def _discovery_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self._udp.recvfrom(64)
            except (socket.timeout, OSError):
                continue
            if data.startswith(b"BBBRAIN?"):
                try:
                    self._udp.sendto(f"BBBRAIN {TALK_PORT}".encode(), addr)
                    self.on_event("robot", addr[0])
                except OSError:
                    pass

    def _memory_loop(self):
        """Every few seconds: a conversation that has gone quiet is over - turn it into notes to remember."""
        last_warm = time.time()
        while not self._stop.wait(10):
            t = self.thinker
            # Ollama lets a model go after an hour unused, and loading it again (gemma3: ~35-47 s) outlasts the
            # robot's wait for an answer: while the brain runs, keep them loaded
            if t is not None and self.settings.provider == "ollama" and time.time() - last_warm > WARM_EVERY_S:
                last_warm = time.time()
                try:
                    t.keep_warm()
                except Exception:  # noqa: BLE001 - next time
                    pass
            if t is None or not self.settings.memory_on:
                continue
            with t.lock:
                if t.history and time.time() - t.last_at > MEMORY_GAP_S:
                    t.end_conversation()
            self._remember_finished()

    def _remember_finished(self):
        t = self.thinker
        if t is None:
            return
        with t.lock:
            todo, t.finished = t.finished, []
        for history in todo:
            try:
                notes = t.summarize(history)
            except Exception as e:  # noqa: BLE001 - memory is a nice-to-have; never break the brain over it
                self.on_event("status", f"Couldn't make a memory of that conversation ({str(e)[:80]})")
                continue
            if notes:
                self.on_event("memory", notes)

    def _talk_loop(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._tcp.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()

    def _handle(self, conn: socket.socket, addr):
        conn.settimeout(15)
        t0 = time.time()
        try:
            buf = b""
            while b"\n" not in buf and len(buf) < 512:
                chunk = conn.recv(512)
                if not chunk:
                    return
                buf += chunk
            head, rest = buf.split(b"\n", 1)
            words = head.decode("ascii", "replace").split()
            if not words or words[0] == "BBPING":
                return
            if words[0] != "BBTALK":
                return
            fields = dict(w.split("=", 1) for w in words[2:] if "=" in w)
            want = int(fields.get("bytes", "0"))
            audio = bytearray(rest)
            while len(audio) < want:
                chunk = conn.recv(min(65536, want - len(audio)))
                if not chunk:
                    break
                audio += chunk
            timing = {"upload_s": time.time() - t0, "audio_bytes": len(audio)}
            self.on_event("robot", addr[0])
            if fields.get("fmt") == "ulaw":
                audio = bytearray(ulaw_to_pcm16(bytes(audio)))
            if fields.get("rate") == "8000":
                audio = bytearray(upsample_8k_to_16k(bytes(audio)))
            timing["audio_s"] = len(audio) / 32000.0
            if not self.ready.is_set():
                self.on_event("status", "The robot asked something - still warming up, it'll get an answer as soon as "
                                        "that's done.")
                self.ready.wait(120)

            def send(line: str):
                conn.sendall((line.replace("\n", " ") + "\n").encode("utf-8", "replace"))

            def finish(turn: Turn):
                self._log_turn(turn)
                self.on_event("turn", turn)

            t1 = time.time()
            try:
                self.ears.load(self.settings.whisper_model, source=self.settings.whisper_source)
                heard = self.ears.transcribe(bytes(audio))
            except Exception as e:  # noqa: BLE001
                send("ERR stt_failed")
                finish(Turn("", "", "", time.time() - t0, f"Speech-to-text failed: {e}", hear_s=time.time() - t1, **timing))
                return
            timing["hear_s"] = time.time() - t1
            send(f"HEARD {heard}")
            if not heard:
                send("SAY I heard static. Riveting. Try again.")
                finish(Turn("", "I heard static…", "", time.time() - t0, **timing))
                return
            look = None
            if fields.get("look") == "1":
                def look() -> dict:
                    t = time.time()
                    try:
                        photo = self._ask_photo(conn, send)
                    finally:
                        timing["look_s"] = time.time() - t
                    timing["looked"] = f"{photo['w']}x{photo['h']}" if photo.get("jpeg") else photo.get("why", "failed")
                    if photo.get("jpeg"):
                        self.on_event("photo", photo["jpeg"])
                    return photo
            t2 = time.time()
            try:
                reply = self.thinker.think(heard, conversation=fields.get("conv") == "1", look=look)
            except Exception as e:  # noqa: BLE001
                send("ERR think_failed")
                finish(Turn(heard, "", "", time.time() - t0, str(e), think_s=time.time() - t2, **timing))
                return
            timing["think_s"] = time.time() - t2 - timing.get("look_s", 0.0)
            if reply.get("blind") and not timing.get("looked"):
                timing["looked"] = reply["blind"]  # a visual question answered without a photo, and why
            if reply["say"]:
                send(f"SAY {reply['say']}")
            if reply["action"]:
                send(f"DO {reply['action']}")
            if not reply["say"] and not reply["action"]:
                send("SAY Hmm.")
            finish(Turn(heard, reply["say"], reply["action"], time.time() - t0, **timing))
        except (OSError, ValueError) as e:
            self.on_event("error", f"Robot connection: {e}")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _ask_photo(conn: socket.socket, send: Callable[[str], None]) -> dict:
        """LOOK -> the robot answers "BBIMG bytes=<n> w=<px> h=<px>" + n bytes of JPEG, or "BBIMG bytes=0 why=..."."""
        old = conn.gettimeout()
        conn.settimeout(LOOK_LIMIT_S)
        try:
            send("LOOK")
            buf = b""
            while b"\n" not in buf and len(buf) < 256:
                chunk = conn.recv(256)
                if not chunk:
                    return {"why": "robot_closed"}
                buf += chunk
            head, data = buf.split(b"\n", 1)
            words = head.decode("ascii", "replace").split()
            if not words or words[0] != "BBIMG":
                return {"why": "bad_reply"}
            fields = dict(w.split("=", 1) for w in words[1:] if "=" in w)
            want = int(fields.get("bytes", "0"))
            if want <= 0:
                return {"why": fields.get("why", "no_photo")}
            if want > 2_000_000:
                return {"why": "too_big"}
            data = bytearray(data)
            while len(data) < want:
                chunk = conn.recv(min(65536, want - len(data)))
                if not chunk:
                    return {"why": "cut_off"}
                data += chunk
            return {"jpeg": bytes(data[:want]), "w": int(fields.get("w", "0")), "h": int(fields.get("h", "0"))}
        except (socket.timeout, OSError, ValueError):
            return {"why": "timeout"}
        finally:
            conn.settimeout(old)

    def _log_turn(self, turn: Turn):
        if not self.log_path:
            return
        try:
            from dataclasses import asdict
            row = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "provider": self.settings.provider,
                   "model": self.settings.ollama_model if self.settings.provider == "ollama" else self.settings.claude_model,
                   **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in asdict(turn).items()}}
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError:
            pass

    # --- test from the UI (typed text, no robot) ---------------------------------------------
    def test_text(self, text: str) -> dict:
        # A separate thinker: typed tests aren't part of the robot's conversation (or its memories)
        return Thinker(self.settings, self.thinker.actions if self.thinker else load_actions(self.guide_path)).think(text)


def local_ip() -> str:
    """The PC's address on the home network (the one a robot on the same WiFi would reach)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
