"""Đo độ trễ thực tế của llama-server trên Pi (chạy trên Pi, chỉ dùng thư viện chuẩn).

Dùng đúng 11 tool + định dạng native của tool_cases.py. Mỗi request lấy `timings` của server:
prompt_n (số token prompt phải xử lý mới), cache_n (token lấy lại từ cache), prompt_ms (~TTFT),
predicted_n / predicted_ms (sinh token).

Các kịch bản:
  A = bố cục hiện tại của tool_cases.NATIVE_SYSTEM: trạng thái máy nằm trong system prompt
      -> mỗi khi trạng thái đổi, prefix đổi, cache mất.
  B = bố cục đề xuất: system prompt cố định, trạng thái máy chuyển xuống tin nhắn user.
  think_on / think_off = Qwen3 bật / tắt chế độ thinking (chat_template_kwargs.enable_thinking).

Cách dùng: python3 server_latency.py <đường dẫn model> <nhãn> <có_thinking 0|1>
(script tự bật llama-server ở 127.0.0.1:8080, tự bật lại nếu server crash)
"""
import json
import os
import sys
import time
import urllib.request

URL = "http://127.0.0.1:8080/v1/chat/completions"


def _tool(name, description, props=None, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props or {}, "required": list(required)}}}


_PERIOD = {"type": "string", "enum": ["last_hour", "today", "this_shift"]}
_SPEED = {"type": "integer", "description": "Conveyor speed in RPM"}
_METRICS = ["current", "temperature", "speed", "product_count", "all_events"]
TOOLS = [  # chép nguyên từ tool_cases.TOOLS
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
NATIVE_SYSTEM = ("You supervise a conveyor line. Current machine state: {state}. "
                 "Call a tool only when one of the tools can do what the user asks; otherwise answer in one short sentence.")
STATIC_SYSTEM = ("You supervise a conveyor line. The current machine state is given at the start of each user message. "
                 "Call a tool only when one of the tools can do what the user asks; otherwise answer in one short sentence.")
STATES = [
    {"state": "RUNNING", "speed_rpm": 30, "motor_current_ma": 1200, "motor_temp_c": 40.0},
    {"state": "JAMMED", "speed_rpm": 0, "motor_current_ma": 2900, "motor_temp_c": 48.0},
    {"state": "OVERTEMP", "speed_rpm": 0, "motor_current_ma": 600, "motor_temp_c": 82.0},
]
QUESTIONS = ["What is the motor temperature right now?", "Tell me a joke.", "Sound the jam alarm."]


def msgs(layout, state, q):
    s = json.dumps(state, separators=(",", ":"))
    if layout == "A":
        return [{"role": "system", "content": NATIVE_SYSTEM.format(state=s)}, {"role": "user", "content": q}]
    return [{"role": "system", "content": STATIC_SYSTEM}, {"role": "user", "content": f"State: {s}\n{q}"}]


def ask(messages, think=None, max_tokens=128):
    body = {"messages": messages, "tools": TOOLS, "temperature": 0, "max_tokens": max_tokens, "cache_prompt": True}
    if think is not None:
        body["chat_template_kwargs"] = {"enable_thinking": think}
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=1800) as r:
        d = json.loads(r.read())
    wall = time.time() - t0
    t = d.get("timings", {})
    m = d["choices"][0]["message"]
    calls = [c["function"]["name"] for c in (m.get("tool_calls") or [])]
    return {"prompt_n": t.get("prompt_n"), "cache_n": t.get("cache_n"), "prompt_ms": round(t.get("prompt_ms", 0)),
            "prompt_tps": round(t.get("prompt_per_second", 0), 2), "pred_n": t.get("predicted_n"),
            "pred_ms": round(t.get("predicted_ms", 0)), "pred_tps": round(t.get("predicted_per_second", 0), 2),
            "wall_s": round(wall, 1), "tool_calls": calls, "think_chars": len(m.get("reasoning_content") or ""),
            "text": (m.get("content") or "")[:80]}


def scenario(has_think):
    """Danh sách (tên ca, tham số ask). Thứ tự quan trọng vì trạng thái cache nối tiếp nhau."""
    off = False if has_think else None  # tắt thinking cho Qwen3 ở các phép đo bố cục
    steps = []
    # Bố cục A: mỗi lượt trạng thái đổi -> đo xem phải xử lý lại bao nhiêu token
    for i, (st, q) in enumerate(zip(STATES, QUESTIONS)):
        steps.append((f"A_turn{i + 1}" + ("_cold" if i == 0 else ""), dict(messages=msgs("A", st, q), think=off)))
    # Bố cục B: lượt đầu dựng cache cho prefix tĩnh, các lượt sau chỉ xử lý phần user
    for i, (st, q) in enumerate(zip(STATES, QUESTIONS)):
        steps.append((f"B_turn{i + 1}", dict(messages=msgs("B", st, q), think=off)))
    if has_think:  # chế độ thinking: cùng câu hỏi, bố cục B (prefix đã có trong cache)
        for i in (0, 1):
            m = msgs("B", STATES[i], QUESTIONS[i])
            steps.append((f"B_think_off_q{i + 1}", dict(messages=m, think=False)))
            steps.append((f"B_think_on_q{i + 1}", dict(messages=m, think=True, max_tokens=512)))
    return steps


def _rss_mib(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            return next(int(l.split()[1]) // 1024 for l in f if l.startswith("VmRSS"))
    except (OSError, StopIteration):
        return None


def managed(model, label, has_think, server_args, tries=4):
    """Tự bật llama-server. Server crash giữa chừng (Pi ép xung 1400 MHz đôi khi segfault / illegal
    instruction khi tải nặng lâu) -> bật lại và chạy LẠI TỪ ĐẦU cả kịch bản, vì số đo cache phụ thuộc
    các request trước. Chỉ in kết quả của lần chạy trọn vẹn, cùng số lần crash."""
    import subprocess
    crashes = []
    for attempt in range(1, tries + 1):
        log = open(f"/home/hminh/server_{label}.log", "w")
        p = subprocess.Popen([os.path.expanduser("~/llama/build/bin/llama-server"), "-m", model, *server_args,
                              "--host", "127.0.0.1", "--port", "8080"], stdout=log, stderr=subprocess.STDOUT)
        for _ in range(300):
            try:
                urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=2)
                break
            except OSError:
                if p.poll() is not None:
                    break
                time.sleep(1)
        rows, peak, failed = [], 0, None
        for case, kw in scenario(has_think):
            try:
                rows.append({"tag": label, "case": case, "attempt": attempt, **ask(**kw)})
                peak = max(peak, _rss_mib(p.pid) or 0)
            except Exception as e:  # noqa: BLE001 - server chết thì mọi lỗi kết nối đều về đây
                time.sleep(1)
                failed = f"{case}: {type(e).__name__}, server exit={p.poll()}"
                break
        p.kill()
        p.wait()
        log.close()
        if failed is None:
            for r in rows:
                print(json.dumps(r, ensure_ascii=False), flush=True)
            print(json.dumps({"tag": label, "summary": True, "attempts": attempt, "crashes": crashes,
                              "peak_rss_mib": peak}), flush=True)
            return
        crashes.append(failed)
        print(f"# {label} attempt {attempt} CRASH at {failed}", flush=True)
        time.sleep(20)
    print(json.dumps({"tag": label, "summary": True, "attempts": tries, "crashes": crashes, "FAILED": True}), flush=True)


if __name__ == "__main__":
    model, label, has_think = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
    managed(model, label, has_think, ["-t", "4", "-c", "2048", "-cram", "64", "-fa", "on", "--jinja"])
    print("DONE", label, flush=True)
