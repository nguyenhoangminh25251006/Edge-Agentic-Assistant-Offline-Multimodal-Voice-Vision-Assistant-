"""End-to-end bench demo: wake word on the Pico 2 -> command audio -> Vosk STT ->
SLM ABn (llama-server, native tool calls) -> B-03 command -> Pico 2 -> motor.

    # 1. on the Pi: start llama-server with the ABn model (branch slm, results_pi/)
    llama-server -m ABn_Q4_K_M-himx-o4.gguf -t 4 --port 8080 --jinja
    # 2. run this script
    python3 voice_to_belt.py --link /dev/serial0 --audio /dev/ttyACM1 \
        --vosk ~/.cache/vosk/vosk-model-small-en-us-0.15 --roller-mm 50

DEMO ONLY: tool calls go straight to the MCU. In the real system they go through the
orchestrator FSM + Safety Arbiter (branch orchestrator). The MCU still validates every
command on its own (state, limits, heartbeat, E-stop), so this path cannot bypass the
MCU safety rules.

Units: the SLM tools use target_speed_rpm (branch slm, server_latency.py TOOLS); B-03
uses mm/s. mm/s = rpm * pi * roller_diameter_mm / 60. Measure the drive roller.
"""
from __future__ import annotations

import argparse
import json
import math
import time
import urllib.request

import b03
from audio_rx import AudioReceiver, save_wav
from link_test import Link

# Same tool list as the fine-tuned ABn model was trained on (branch slm, results_pi/scripts/server_latency.py)
_SPEED = {"type": "integer", "description": "Conveyor speed in RPM"}
_PERIOD = {"type": "string", "enum": ["last_hour", "today", "this_shift"]}
_METRICS = ["current", "temperature", "speed", "product_count", "all_events"]


def _tool(name, description, props=None, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props or {}, "required": list(required)}}}


TOOLS = [
    _tool("query_blackbox_logs", "Look up sensor/incident HISTORY from the database. Use for questions about when something happened in the past.",
          {"target_metric": {"type": "string", "enum": _METRICS}, "lookback_minutes": {"type": "integer", "default": 30}}, ["target_metric"]),
    _tool("request_line_restart", "Ask to restart the stopped conveyor belt. The Safety Arbiter must approve first.",
          {"target_speed_rpm": _SPEED, "reason": {"type": "string"}}, ["target_speed_rpm", "reason"]),
    _tool("trigger_audio_alarm", "Sound an audible warning on site through the HMI speaker.",
          {"alarm_code": {"type": "string", "enum": ["ALERT_JAM", "ALERT_OVERTEMP", "ALERT_MANUAL_CHECK"]}}, ["alarm_code"]),
    _tool("get_live_telemetry", "Read the sensors RIGHT NOW (fresh values, not history).",
          {"metric": {"type": "string", "enum": ["all", "current", "temperature", "speed", "product_count"]}}, ["metric"]),
    _tool("get_last_stop_reason", "Explain why the conveyor stopped the last time, with the evidence."),
    _tool("request_line_stop", "Ask to stop the conveyor belt.", {"reason": {"type": "string"}}, ["reason"]),
    _tool("get_production_stats", "Count of products made, throughput and jam count for a period.", {"period": _PERIOD}, ["period"]),
    _tool("get_inspection_summary", "Number of defective products found by camera inspection in a period.", {"period": _PERIOD}, ["period"]),
    _tool("request_set_speed", "Ask to change the speed of the running conveyor.", {"target_speed_rpm": _SPEED}, ["target_speed_rpm"]),
    _tool("acknowledge_alarm", "Operator confirms the alarm has been handled (e.g. the jam was cleared)."),
    _tool("set_led_indicator", "Set the status light on the machine.",
          {"color": {"type": "string", "enum": ["green", "yellow", "red"]},
           "pattern": {"type": "string", "enum": ["solid", "blinking"]}}, ["color", "pattern"]),
]
SYSTEM = ("You supervise a conveyor line. Current machine state: {state}. "
          "Call a tool only when one of the tools can do what the user asks; otherwise answer in one short sentence.")


def slm_state(t: dict, roller_mm: float) -> dict:
    """Map MCU telemetry to the machine-state names the SLM was trained with."""
    st, fault = t.get("state", 1), t.get("fault", 0)
    name = {1: "IDLE", 2: "RUNNING"}.get(st, "SAFETY_FAULT")
    if st == 3 and fault == 0x02:
        name = "JAMMED"
    elif st == 3 and fault == 0x04:
        name = "OVERTEMP"
    rpm = t.get("speed_mms", 0) * 60 / (math.pi * roller_mm)
    temp = t.get("temp_c10", 0)
    return {"state": name, "speed_rpm": round(rpm), "motor_current_ma": t.get("current_ma", 0),
            "motor_temp_c": None if temp in (0x7FFF, 0x7FFE) else temp / 10}


def ask_slm(url: str, text: str, state: dict) -> list[dict]:
    body = {"messages": [{"role": "system", "content": SYSTEM.format(state=json.dumps(state))},
                         {"role": "user", "content": text}],
            "tools": TOOLS, "max_tokens": 120, "temperature": 0, "cache_prompt": True}
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        msg = json.load(r)["choices"][0]["message"]
    if msg.get("content"):
        print(f"   SLM says: {msg['content']}")
    return [{"name": c["function"]["name"], "args": json.loads(c["function"].get("arguments") or "{}")}
            for c in (msg.get("tool_calls") or [])]


def execute(link: Link, call: dict, roller_mm: float) -> None:
    name, args = call["name"], call["args"]
    to_mms = lambda rpm: int(round(float(rpm) * math.pi * roller_mm / 60))  # noqa: E731
    if name == "request_line_stop":
        link.send(b03.STOP, b03.p_stop(link.next_id(), 500, 0))
    elif name == "request_line_restart":
        link.send(b03.RUN, b03.p_run(link.next_id(), 500, 1, to_mms(args.get("target_speed_rpm", 30))))
    elif name == "request_set_speed":
        link.send(b03.SET_SPEED, b03.p_set_speed(link.next_id(), 500, to_mms(args.get("target_speed_rpm", 30))))
    elif name == "acknowledge_alarm":
        link.send(b03.CLEAR_FAULT, b03.p_cmd(link.next_id(), 500))
    elif name == "get_live_telemetry":
        print(f"   live: {link.latest}")
    else:
        print(f"   ({name} is handled by the orchestrator / HMI / database, not by the MCU)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--link", default="/dev/serial0")
    ap.add_argument("--audio", default="/dev/ttyACM1")
    ap.add_argument("--vosk", required=True, help="Vosk model folder")
    ap.add_argument("--slm", default="http://127.0.0.1:8080/v1/chat/completions")
    ap.add_argument("--roller-mm", type=float, default=50.0, help="drive roller diameter (measure it)")
    ap.add_argument("--save", default="", help="folder to keep the WAV files")
    a = ap.parse_args()

    import vosk  # pip install vosk
    model = vosk.Model(a.vosk)
    link = Link(a.link, show_telem=False)
    link.start()

    def on_utterance(pcm: bytes, rate: int) -> None:
        t0 = time.monotonic()
        if a.save:
            save_wav(time.strftime(f"{a.save}/cmd_%H%M%S.wav"), pcm, rate)
        rec = vosk.KaldiRecognizer(model, rate)
        rec.AcceptWaveform(pcm)
        text = json.loads(rec.FinalResult()).get("text", "")
        t1 = time.monotonic()
        print(f"\n[STT {t1 - t0:.1f} s] \"{text}\"")
        if not text:
            return
        calls = ask_slm(a.slm, text, slm_state(link.telem, a.roller_mm))
        t2 = time.monotonic()
        print(f"[SLM {t2 - t1:.1f} s] {calls or 'no tool call'}")
        for c in calls:
            execute(link, c, a.roller_mm)

    rx = AudioReceiver(a.audio, on_utterance)
    print("ready: say the wake word (or press push-to-talk), then the command")
    while True:
        rx.poll()


if __name__ == "__main__":
    main()
