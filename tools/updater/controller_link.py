"""
Dimension Hopper - the emulated controller's link to the robot: the message format the real controller speaks, and
two ways to carry it.

  * USB: the robot's console. "emu on" makes the robot copy every message it sends its controller to the console
    ([BBCOM] d=<hex>); "ctrl <hex>" hands it a controller message. Works next to a connected hardware controller.
  * Bluetooth: this PC connects to the robot like the hardware controller does (bleak). The robot takes one
    controller at a time, so the hardware controller must be off (or out of range) while this one is connected.

Wire format (ButterBot-Common, little-endian, 32-bit enums):
  controller -> robot   int32 Ctrl::Command, then the command's payload (DriveData, ScenarioData, ...)
  robot -> controller   int32 BB::State, int32 action (enum of that state), uint32 size, then size bytes of data
"""
from __future__ import annotations

import asyncio
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable

import hopper_core as core

# --- enums, in declaration order (append-only in the firmware) ------------------------------------------------
CTRL = ["EnterRC", "ExitRC", "Listen", "Summon", "ShutUp", "Poke", "Drive", "RCSound", "Scenario", "Settings", "Status"]
CMD = {name: i for i, name in enumerate(CTRL)}

STATES = ["Idle", "Listen", "Scenario", "RC"]
IDLE = ["None", "Wander", "Ramble", "Person", "Poke", "Summon", "Fall", "UpsideDown", "PickUp", "Shake", "ModuleChange",
        "Gas", "Intruder", "BatteryLow", "Charging", "ChargingFull", "PhoneNotif", "PhoneConnect", "Observe",
        "GasConfigureStart", "GasConfigureEnd", "BatteryLevel", "ShutUp", "CantMove", "SettingsChange", "SettingsSync",
        "RobotStatus"]
SCENARIO = ["ReadNotifs", "EightBall", "Fact", "Joke", "PassTheButter", "Profanity", "YouPassButter", "DiceRoll",
            "CurrentTime", "VoiceControl", "Dance", "FaceDetect", "FaceDetectForget", "IR_train", "IR_forget",
            "IR_forgetAll", "IR_list", "IR_action", "LEDModule", "LEDTurnOn", "LEDTurnOff", "LEDStrobe", "LEDBreathe",
            "LEDFaster", "LEDSlower", "GasModule", "TempHumModule", "TempHumScaleCelsius", "TempHumScaleFahrenheit",
            "TempHumScaleKelvin", "IntruderDetectionOn", "IntruderDetectionOff", "Shutdown", "PhoneNotConnected",
            "PhoneListNotifs", "PhoneWhatsPlaying", "PhoneNextSong", "PhonePrevSong", "PhonePlayMusic",
            "PhoneStopMusic", "WhatsThis", "SettingsActivity", "Chat", "OpenPortal", "Subtitle"]
SC = {name: i for i, name in enumerate(SCENARIO)}

# The controller's action list (ScenarioMapping.h): folder -> [(label, scenario, data byte)]. Chat is filled from the
# guide data (its order is the chat line index the robot expects).
MENU: list[tuple[str, list[tuple[str, str, int]]]] = [
    ("Fun", [("Fact", "Fact", 0), ("Joke", "Joke", 0), ("Pass The Butter", "PassTheButter", 0),
             ("You Pass The Butter", "YouPassButter", 0), ("Eight Ball", "EightBall", 0), ("Dance", "Dance", 0),
             ("Open a Portal", "OpenPortal", 0), ("What is This?", "WhatsThis", 0),
             ("Current Time", "CurrentTime", 0)]),
    ("Chat", []),
    ("Dice", [("Dice Roll", "DiceRoll", 0), ("Roll a D4", "DiceRoll", 1), ("Roll a D6", "DiceRoll", 2),
              ("Roll a D8", "DiceRoll", 3), ("Roll a D10", "DiceRoll", 4), ("Roll a D12", "DiceRoll", 5),
              ("Roll a D20", "DiceRoll", 6), ("Roll a D100", "DiceRoll", 7)]),
    ("Movement", [("Go Forward", "VoiceControl", 0), ("Go Backward", "VoiceControl", 1),
                  ("Turn Left", "VoiceControl", 2), ("Turn Right", "VoiceControl", 3),
                  ("Turn Around", "VoiceControl", 4)]),
    ("Faces", [("Who is this?", "FaceDetect", 0), ("Remember this face", "FaceDetect", 1),
               ("Forget Owner", "FaceDetectForget", 2)]),
    ("Phone", [("List Notifications", "PhoneListNotifs", 0), ("What is Playing?", "PhoneWhatsPlaying", 0),
               ("Next Song", "PhoneNextSong", 0), ("Previous Song", "PhonePrevSong", 0),
               ("Play Music", "PhonePlayMusic", 0), ("Stop Music", "PhoneStopMusic", 0)]),
    ("IR Remote", [("IR train", "IR_train", 0), ("IR list", "IR_list", 0), ("IR forget", "IR_forget", 0),
                   ("IR forget all", "IR_forgetAll", 0)]),
    ("LED Module", [("LED Turn On", "LEDTurnOn", 0), ("LED Turn Off", "LEDTurnOff", 0),
                    ("LED Strobe", "LEDStrobe", 0), ("LED Breathe", "LEDBreathe", 0),
                    ("LED Faster", "LEDFaster", 0), ("LED Slower", "LEDSlower", 0)]),
    ("Climate Module", [("Climate Module", "TempHumModule", 0), ("Scale Celsius", "TempHumScaleCelsius", 0),
                        ("Scale Fahrenheit", "TempHumScaleFahrenheit", 0), ("Scale Kelvin", "TempHumScaleKelvin", 0)]),
    ("Sensors", [("Gas Module", "GasModule", 0), ("Intruder Detection ON", "IntruderDetectionOn", 0),
                 ("Intruder Detection OFF", "IntruderDetectionOff", 0)]),
    ("Robot", [("Activity Mode", "SettingsActivity", 0), ("Activity Off", "SettingsActivity", 1),
               ("Activity Chill", "SettingsActivity", 2), ("Activity Active", "SettingsActivity", 3),
               ("Activity Hyper", "SettingsActivity", 4), ("Shutdown", "Shutdown", 0)]),
]

# Bluetooth: the robot's controller service (Com.h; ESP-IDF stores 128-bit UUIDs little-endian)
SERVICE_UUID = "3685cae7-8251-9891-4440-bd80bc790819"
RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # controller writes here
TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   # robot notifies here


def command(name: str, payload: bytes = b"") -> bytes:
    return struct.pack("<i", CMD[name]) + payload


def scenario(name: str, data: int = 0) -> bytes:
    return command("Scenario", struct.pack("<iB", SC[name], data & 0xFF))


def drive(x: int, y: int) -> bytes:
    return command("Drive", struct.pack("<bb", max(-100, min(100, x)), max(-100, min(100, y))))


@dataclass
class Message:
    """One message from the robot, decoded as far as the emulator needs."""
    state: str
    action: str
    data: bytes

    @staticmethod
    def parse(buf: bytes) -> "Message | None":
        if len(buf) < 12:
            return None
        st, act, size = struct.unpack_from("<iiI", buf)
        state = STATES[st] if 0 <= st < len(STATES) else f"state{st}"
        names = {"Idle": IDLE, "Scenario": SCENARIO}.get(state, [])
        action = names[act] if 0 <= act < len(names) else str(act)
        return Message(state, action, bytes(buf[12:12 + size]))

    def strings(self) -> list[str]:
        """Length-prefixed strings (ComSerial), for the variable-length messages."""
        out, i, d = [], 0, self.data
        while i < len(d):
            n = d[i]
            out.append(d[i + 1:i + 1 + n].decode("utf-8", "replace"))
            i += 1 + n
        return out


class Link:
    """Base: on_message(Message) for everything the robot sends, on_state(text, ok) for connection changes."""

    def __init__(self, on_message: Callable[[Message], None], on_state: Callable[[str, bool], None]):
        self.on_message, self.on_state = on_message, on_state
        self.connected = False

    def send(self, data: bytes):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


class UsbLink(Link):
    """The robot's USB console: "emu on" + "ctrl <hex>" / [BBCOM] lines."""

    def __init__(self, port: str, on_message, on_state):
        super().__init__(on_message, on_state)
        self.port = port
        self.session = core.DiagSession(port, self._line, self._end, diag=False)
        self._keep = threading.Event()
        self.on_cam: Callable[[dict], None] | None = None   # [BBCAM] replies (the camera view), if wanted

    def start(self):
        self.session.start()
        self.session.send("emu on")
        threading.Thread(target=self._keepalive, daemon=True).start()

    def _keepalive(self):
        # "emu on" again now and then: a robot that rebooted (or was switched off and on) starts with it off
        while not self._keep.wait(10):
            self.session.send("emu on")

    def _line(self, kind: str, fields: dict, raw: str):
        if kind == "BBEMU" and "mirror" in fields:
            if not self.connected and fields["mirror"] == "1":
                self.connected = True
                self.on_state(f"Connected over USB ({self.port})", True)
        elif kind == "BBCAM" and self.on_cam is not None:
            self.on_cam(fields)
        elif kind == "BBCOM" and "d" in fields:
            try:
                m = Message.parse(bytes.fromhex(fields["d"]))
            except ValueError:
                m = None
            if m:
                self.on_message(m)

    def _end(self, reason: str):
        self._keep.set()
        self.connected = False
        self.on_state(reason, False)

    def send(self, data: bytes):
        self.session.send("ctrl " + data.hex())

    def close(self):
        self._keep.set()
        self.session.send("emu off")
        self.session.close()


def bluetooth_available() -> tuple[bool, str]:
    """(usable, why not). Needs the bleak library and a Bluetooth LE adapter that's switched on."""
    try:
        import bleak  # noqa: F401
    except ImportError:
        return False, "Bluetooth support isn't installed in this copy of Dimension Hopper."
    try:
        from bleak import BleakScanner

        async def probe():
            async with BleakScanner():
                await asyncio.sleep(0.2)
        asyncio.run(probe())
    except Exception as e:  # noqa: BLE001 - any failure means no usable adapter
        msg = str(e) or e.__class__.__name__
        if "adapter" in msg.lower() or "radio" in msg.lower() or "turned off" in msg.lower():
            return False, "No Bluetooth found on this PC (or it's switched off)."
        return False, f"Bluetooth isn't usable: {msg}"
    return True, ""


class BleLink(Link):
    """Connects to the robot like the hardware controller: subscribe to TX notifications, write to RX."""

    SCAN_S = 8.0

    def __init__(self, on_message, on_state):
        super().__init__(on_message, on_state)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client = None
        self.tx = self.rx = None
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        self.loop = asyncio.new_event_loop()
        try:
            self.loop.run_until_complete(self._main())
        except Exception as e:  # noqa: BLE001
            self.connected = False
            self.on_state(f"Bluetooth error: {e}", False)
        finally:
            self.loop.close()

    async def _main(self):
        from bleak import BleakClient, BleakScanner
        self._stop = asyncio.Event()
        self.on_state("Looking for the robot over Bluetooth…", False)
        # The robot advertises its name (GAP.h) and a 16-bit HID UUID, not the controller service
        dev = await BleakScanner.find_device_by_filter(
            lambda d, ad: "butter bot" in (ad.local_name or d.name or "").lower(), timeout=self.SCAN_S)
        if dev is None:
            self.on_state("No robot found over Bluetooth. Is it on, and is the hardware controller off? "
                          "(The robot takes one controller at a time.)", False)
            return

        def gone(_client):
            if self._stop is not None and not self._stop.is_set():
                self.connected = False
                self.on_state("The robot disconnected.", False)
                self.loop.call_soon_threadsafe(self._stop.set)

        self.on_state(f"Connecting to {dev.name or 'the robot'}…", False)
        # Only the controller service: the robot also has a HID service (it advertises 0x1812) that Windows guards,
        # and discovering everything stalled until the session timed out
        async with BleakClient(dev, disconnected_callback=gone, services=[SERVICE_UUID], timeout=20.0) as client:
            self.client = client
            # No explicit pair(): the robot asks for security itself (no PIN) and Windows bonds on that request.
            # Calling pair() on an already-bonded robot made Windows drop the link.
            # The phone (Nordic UART) service reuses the same characteristic UUIDs: take them from the controller service
            svc = client.services.get_service(SERVICE_UUID)
            if svc is None:
                self.on_state("This robot has no controller service - does it run ButterBot firmware?", False)
                return
            self.tx = svc.get_characteristic(TX_UUID)
            self.rx = svc.get_characteristic(RX_UUID)
            await client.start_notify(self.tx, lambda _c, data: self._notified(bytes(data)))
            self.connected = True
            self.on_state("Connected over Bluetooth", True)
            await self._stop.wait()
            try:
                await client.stop_notify(self.tx)
            except Exception:  # noqa: BLE001
                pass
        self.client = None
        self.connected = False
        self.on_state("Disconnected", False)

    def _notified(self, data: bytes):
        m = Message.parse(data)
        if m:
            self.on_message(m)

    def send(self, data: bytes):
        if not (self.loop and self.client and self.connected):
            return
        asyncio.run_coroutine_threadsafe(self.client.write_gatt_char(self.rx, data, response=True), self.loop)

    def close(self):
        if self.loop and self._stop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._stop.set)


def now_ms() -> int:
    return int(time.monotonic() * 1000)
