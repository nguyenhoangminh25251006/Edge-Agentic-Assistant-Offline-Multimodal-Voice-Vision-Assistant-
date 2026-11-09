"""Bộ câu hỏi cho băng tải: model có chọn ĐÚNG công cụ và điền ĐÚNG tham số không?

Mỗi "ca" gồm: tình trạng máy (JSON) + một câu nói của operator + đáp án đúng.
Cùng một seed thì luôn sinh ra đúng cùng một bộ ca, nên mọi model được chấm trên đề giống hệt nhau.
"""
import json
import random
import re

# ---------------------------------------------------------------- 11 công cụ
def _tool(name, description, props=None, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props or {}, "required": list(required)}}}


_PERIOD = {"type": "string", "enum": ["last_hour", "today", "this_shift"]}
_SPEED = {"type": "integer", "description": "Conveyor speed in RPM"}
_METRICS = ["current", "temperature", "speed", "product_count", "all_events"]

TOOLS = [
    _tool("query_blackbox_logs", "Look up sensor/incident HISTORY from the database. Use for questions about when something happened in the past.",
          {"target_metric": {"type": "string", "enum": _METRICS}, "lookback_minutes": {"type": "integer", "default": 30}},
          ["target_metric"]),
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
NAMES = [t["function"]["name"] for t in TOOLS]
# Ba cỡ bộ công cụ để xem "nhiều công cụ thì model có bị rối không"
TOOL_SETS = {3: NAMES[:3], 6: NAMES[:6], 11: NAMES}
FREE_TEXT = {"reason"}            # tham số văn bản tự do: chỉ cần có, không so sánh nội dung

SYSTEM_PROMPT = (
    "You are the supervisory assistant of a conveyor line. You receive one JSON context object. "
    'Reply with exactly one JSON object: {"action_type": "DIRECT_ANSWER" or "TOOL_REQUEST", '
    '"tool_call": null or {"tool_name": <one of available_tools>, "arguments": {...}}, "response_text": <one short sentence>}. '
    "Use TOOL_REQUEST only when one of available_tools can do what the user asks. "
    "Otherwise use DIRECT_ANSWER with tool_call null. Never invent tools.")


def grammar_for(tool_names):
    """Luật ép model chỉ được trả JSON đúng khuôn và chỉ được chọn tên công cụ có trong danh sách."""
    names = " | ".join(f'"\\"{n}\\""' for n in tool_names)
    return r'''
root ::= "{" space "\"action_type\":" space ("\"DIRECT_ANSWER\"" | "\"TOOL_REQUEST\"") "," space "\"tool_call\":" space Call "," space "\"response_text\":" space String "}" space
Call ::= "null" space | "{" space "\"tool_name\":" space Name "," space "\"arguments\":" space Obj "}" space
Name ::= ''' + names + r'''
Obj ::= "{" space (Pair ("," space Pair)*)? "}" space
Pair ::= String ":" space Value
Value ::= String | Number | Bool | "null" space
Number ::= ("-"? ("0" | [1-9] [0-9]*) ("." [0-9]+)?) space
Bool ::= ("true" | "false") space
space ::= " "?
String ::= "\"" ([^"\\\x00-\x1F] | "\\" (["\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F]))* "\"" space
'''


# Wording for request for the random cases
_METRIC_WORDS = {"current": ["motor current", "current draw"], "temperature": ["motor temperature", "heat level"],
                 "speed": ["belt speed"], "product_count": ["product count", "number of items"],
                 "all_events": ["all events", "every incident"]}
_LIVE_WORDS = {"all": "all sensors", "current": "motor current", "temperature": "motor temperature",
               "speed": "belt speed", "product_count": "product count"}
_PERIODS = {"last_hour": "in the last hour", "today": "today", "this_shift": "this shift"}
_ALARMS = {"ALERT_JAM": ["sound the jam alarm", "warn everyone about the jam"],
           "ALERT_OVERTEMP": ["sound the overheating alarm", "warn people the motor is too hot"],
           "ALERT_MANUAL_CHECK": ["call someone to check the machine by hand", "sound the manual inspection alert"]}
_OUT_OF_SCOPE = ["what is the weather like today", "tell me a joke", "what is the capital of france",
                 "order more cardboard boxes", "email the supervisor", "how are you doing", "play some music"]
_STATES = {"RUNNING": (30, 1200, 40.0), "JAMMED": (0, 2900, 48.0), "OVERTEMP": (0, 600, 82.0), "IDLE": (0, 100, 30.0)}
# the first element is belt speed in RPM, the second is motor current in mA, the third is motor temperature in Celsius

def _request(tool, r):
    """Trả về (câu nói, tham số đúng) cho một công cụ."""
    if tool == "query_blackbox_logs":
        m, n = r.choice(_METRICS), r.choice([None, 5, 10, 15, 45, 60])
        w = r.choice(_METRIC_WORDS[m])
        return (f"show me {w} for the last {n} minutes" if n else f"look at the history of {w}"), \
               {"target_metric": m, **({"lookback_minutes": n} if n else {})}
    if tool == "request_line_restart":
        s = r.choice([20, 30, 40, 50])
        return r.choice([f"restart the belt at {s} rpm", f"start the conveyor again at {s} rpm"]), {"target_speed_rpm": s}
    if tool == "trigger_audio_alarm":
        code = r.choice(list(_ALARMS))
        return r.choice(_ALARMS[code]), {"alarm_code": code}
    if tool == "get_live_telemetry":
        m = r.choice(list(_LIVE_WORDS))
        return f"read {_LIVE_WORDS[m]} live from the sensors right now", {"metric": m}
    if tool == "get_last_stop_reason":
        return r.choice(["why did the conveyor stop", "what caused the last stop"]), {}
    if tool == "request_line_stop":
        return r.choice(["stop the belt", "pause the conveyor", "halt the line"]), {}
    if tool == "get_production_stats":
        p = r.choice(list(_PERIODS))
        return f"how many products did we make {_PERIODS[p]}", {"period": p}
    if tool == "get_inspection_summary":
        p = r.choice(list(_PERIODS))
        return f"how many defective items did the camera find {_PERIODS[p]}", {"period": p}
    if tool == "request_set_speed":
        s = r.choice([20, 30, 40, 50])
        return r.choice([f"set the belt speed to {s} rpm", f"change the running speed to {s} rpm"]), {"target_speed_rpm": s}
    if tool == "acknowledge_alarm":
        return r.choice(["i cleared the jam so acknowledge the alarm", "alarm acknowledged"]), {}
    c, p = r.choice(["green", "yellow", "red"]), r.choice(["solid", "blinking"])
    return f"turn the status light {c} and {p}", {"color": c, "pattern": p}


def _speech_noise(text, r):
    """Mô phỏng chữ do nhận dạng giọng nói tạo ra: không hoa, không dấu câu, có "um"."""
    words = re.sub(r"[^\w\s]", "", text.lower()).split()
    words = [w for w in words if not (w in ("the", "a") and r.random() < 0.3)]
    if r.random() < 0.5:
        words.insert(0, r.choice(["um", "uh", "okay", "hey"]))
    return " ".join(words)


def make_cases(seed=42, per_tool=12, per_out_of_scope=24):
    """Danh sách các ca. Nửa số ca có "nhiễu giọng nói"."""
    r, cases = random.Random(seed), []
    for tool in NAMES + [None]:
        for i in range(per_out_of_scope if tool is None else per_tool):
            text, args = r.choice(_OUT_OF_SCOPE), {}
            if tool:
                text, args = _request(tool, r)
            noisy = i % 2 == 1
            state = r.choice(list(_STATES))
            speed, cur, temp = _STATES[state]
            cases.append({
                "id": f"{tool or 'none'}-{i}", "tool": tool, "args": args, "noisy": noisy,
                "core": tool in TOOL_SETS[3] or tool is None,     # ca dùng được cho cả bộ 3 công cụ
                "context": {"fsm_state": state, "telemetry": {"belt_speed_rpm": speed, "motor_current_ma": cur,
                                                              "motor_temp_c": temp},
                            "user_prompt": _speech_noise(text, r) if noisy else text.capitalize() + "."}})
    return cases


def messages(case, tool_names):
    """Đúng những gì model nhận được."""
    tools = [t for t in TOOLS if t["function"]["name"] in tool_names]
    payload = {**case["context"], "available_tools": tools}
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, separators=(",", ":"))}]


# ---------------------------------------------------------------- Chế độ "native" (tool-calling chuẩn)
# Định dạng ở trên (nhét tool vào JSON của tin nhắn user + khuôn JSON tự chế) KHÔNG phải định dạng model được
# huấn luyện. Kiểm chứng: Qwen3-1.7B Q8 được 0/12 câu cần tool với định dạng cũ, nhưng 10/12 với native.
# Native = đưa tool qua tham số `tools` của API, llama-server (--jinja) dùng chat template của chính model.
NATIVE_SYSTEM = ("You supervise a conveyor line. Current machine state: {state}. "
                 "Call a tool only when one of the tools can do what the user asks; otherwise answer in one short sentence.")


def native_request(case, tool_names):
    """(messages, tools) cho chế độ native."""
    tools = [t for t in TOOLS if t["function"]["name"] in tool_names]
    state = {k: v for k, v in case["context"].items() if k != "user_prompt"}
    msgs = [{"role": "system", "content": NATIVE_SYSTEM.format(state=json.dumps(state, separators=(",", ":")))},
            {"role": "user", "content": case["context"]["user_prompt"]}]
    return msgs, tools


def score_native(case, answer):
    """Chấm kết quả srv.ask(..., tools=...): chuyển tool_calls về khuôn cũ rồi dùng chung hàm score()."""
    calls = answer.get("tool_calls") or []
    call = None
    if calls:
        try:
            args = calls[0]["arguments"]
            args = json.loads(args) if isinstance(args, str) and args.strip() else (args or {})
            call = {"tool_name": calls[0]["name"], "arguments": args if isinstance(args, dict) else {}}
        except (json.JSONDecodeError, KeyError, TypeError):
            return {"valid": False, "tool_ok": False, "args_ok": False, "over_trigger": case["tool"] is None,
                    "ok": False}
    raw = json.dumps({"action_type": "TOOL_REQUEST" if call else "DIRECT_ANSWER", "tool_call": call,
                      "response_text": answer.get("text") or ""})
    return score(case, raw)


# ---------------------------------------------------------------- Chấm điểm
def score(case, raw):
    """Trả về dict True/False: JSON hợp lệ, đúng công cụ, đúng tham số, gọi thừa công cụ."""
    out = {"valid": False, "tool_ok": False, "args_ok": False, "over_trigger": False}
    try:
        obj = json.loads(raw)
        call = obj["tool_call"]
        out["valid"] = isinstance(obj["response_text"], str) and (call is None or isinstance(call["arguments"], dict))
    except Exception:
        return {**out, "ok": False}
    if not out["valid"]:
        return {**out, "ok": False}
    if case["tool"] is None:                                 # đúng là KHÔNG được gọi công cụ nào
        out["tool_ok"] = out["args_ok"] = call is None
        out["over_trigger"] = call is not None
    else:
        out["tool_ok"] = call is not None and call["tool_name"] == case["tool"]
        if out["tool_ok"]:
            got = {k: v for k, v in call["arguments"].items() if k not in FREE_TEXT}
            want = dict(case["args"])
            if case["tool"] == "query_blackbox_logs" and got.get("lookback_minutes") == 30:
                got.pop("lookback_minutes")                  # 30 là giá trị mặc định, nói hay không đều được
            out["args_ok"] = got == want
    out["ok"] = out["tool_ok"] and out["args_ok"]
    return out
