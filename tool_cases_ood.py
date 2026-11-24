"""Bộ test tool calling NGOÀI PHÂN PHỐI (out-of-distribution): câu viết tay, KHÔNG sinh từ mẫu câu của tool_cases.

Mục đích: kiểm tra các bản quant dùng imatrix "lai" (calibration chứa ChatML tool-call sinh từ tool_cases) có giỏi thật
hay chỉ giỏi đúng họ mẫu câu của tool_cases. Cùng 11 tool, cùng định dạng ca và hàm chấm với tool_cases.
52 câu (4 câu / tool x 11 tool + 8 câu ngoài phạm vi), mỗi câu có bản sạch và bản "nhiễu giọng nói" -> 104 ca.
Không câu nào trùng nguyên văn với 156 ca seed 42 hay calibration.
"""
import random

import tool_cases as tc

_S = tc._STATES  # (speed_rpm, current_ma, temp_c)

# (tool, câu nói, tham số đúng, trạng thái máy)
_RAW = [
    ("query_blackbox_logs", "When did the motor current spike over the past 20 minutes?", {"target_metric": "current", "lookback_minutes": 20}, "RUNNING"),
    ("query_blackbox_logs", "Pull up the temperature history.", {"target_metric": "temperature"}, "OVERTEMP"),
    ("query_blackbox_logs", "List every incident recorded in the last 60 minutes.", {"target_metric": "all_events", "lookback_minutes": 60}, "IDLE"),
    ("query_blackbox_logs", "What was the belt speed doing over the previous 10 minutes?", {"target_metric": "speed", "lookback_minutes": 10}, "RUNNING"),
    ("request_line_restart", "Get the line moving again at 35 rpm, the jam is gone.", {"target_speed_rpm": 35}, "JAMMED"),
    ("request_line_restart", "Resume the conveyor at 25 rpm please.", {"target_speed_rpm": 25}, "IDLE"),
    ("request_line_restart", "Bring the stopped belt back up to 40 rpm.", {"target_speed_rpm": 40}, "JAMMED"),
    ("request_line_restart", "Can you start the line back up at 30 rpm?", {"target_speed_rpm": 30}, "IDLE"),
    ("trigger_audio_alarm", "Blast the jam siren.", {"alarm_code": "ALERT_JAM"}, "JAMMED"),
    ("trigger_audio_alarm", "Alert the floor that the motor is overheating.", {"alarm_code": "ALERT_OVERTEMP"}, "OVERTEMP"),
    ("trigger_audio_alarm", "Page a technician to inspect the machine by hand.", {"alarm_code": "ALERT_MANUAL_CHECK"}, "RUNNING"),
    ("trigger_audio_alarm", "Let everyone on site hear that there's a blockage on the belt.", {"alarm_code": "ALERT_JAM"}, "JAMMED"),
    ("get_live_telemetry", "What's the motor temperature at this very moment?", {"metric": "temperature"}, "RUNNING"),
    ("get_live_telemetry", "How fast is the belt running right now?", {"metric": "speed"}, "RUNNING"),
    ("get_live_telemetry", "Show me every live sensor value.", {"metric": "all"}, "IDLE"),
    ("get_live_telemetry", "What is the product counter showing currently?", {"metric": "product_count"}, "RUNNING"),
    ("get_last_stop_reason", "Why did the line halt?", {}, "JAMMED"),
    ("get_last_stop_reason", "What made the belt stop earlier?", {}, "IDLE"),
    ("get_last_stop_reason", "Explain the last shutdown to me.", {}, "OVERTEMP"),
    ("get_last_stop_reason", "What triggered the most recent stop?", {}, "JAMMED"),
    ("request_line_stop", "Shut the conveyor down.", {}, "RUNNING"),
    ("request_line_stop", "Kill the belt now, someone's hand is near it.", {}, "RUNNING"),
    ("request_line_stop", "Bring the line to a stop.", {}, "OVERTEMP"),
    ("request_line_stop", "Stop everything on the belt immediately.", {}, "RUNNING"),
    ("get_production_stats", "What's our output so far today?", {"period": "today"}, "RUNNING"),
    ("get_production_stats", "Give me the throughput for this shift.", {"period": "this_shift"}, "RUNNING"),
    ("get_production_stats", "How many jams did we have in the last hour?", {"period": "last_hour"}, "IDLE"),
    ("get_production_stats", "Total units produced during this shift?", {"period": "this_shift"}, "RUNNING"),
    ("get_inspection_summary", "How many rejects did the camera flag today?", {"period": "today"}, "RUNNING"),
    ("get_inspection_summary", "Defect count from vision inspection in the last hour?", {"period": "last_hour"}, "RUNNING"),
    ("get_inspection_summary", "Were any bad parts caught by the camera this shift?", {"period": "this_shift"}, "IDLE"),
    ("get_inspection_summary", "How many faulty items were detected today?", {"period": "today"}, "RUNNING"),
    ("request_set_speed", "Slow the belt down to 20 rpm.", {"target_speed_rpm": 20}, "RUNNING"),
    ("request_set_speed", "Bump the running speed up to 45 rpm.", {"target_speed_rpm": 45}, "RUNNING"),
    ("request_set_speed", "Run it at 35 rpm instead.", {"target_speed_rpm": 35}, "RUNNING"),
    ("request_set_speed", "Adjust the conveyor to 50 rpm.", {"target_speed_rpm": 50}, "RUNNING"),
    ("acknowledge_alarm", "Alarm handled, you can clear it.", {}, "JAMMED"),
    ("acknowledge_alarm", "I've fixed the blockage, confirm the alarm.", {}, "JAMMED"),
    ("acknowledge_alarm", "Mark the alert as resolved.", {}, "OVERTEMP"),
    ("acknowledge_alarm", "The problem is dealt with, acknowledge the warning.", {}, "JAMMED"),
    ("set_led_indicator", "Make the status light flash red.", {"color": "red", "pattern": "blinking"}, "JAMMED"),
    ("set_led_indicator", "Set the indicator to a steady green.", {"color": "green", "pattern": "solid"}, "RUNNING"),
    ("set_led_indicator", "Turn the machine lamp solid yellow.", {"color": "yellow", "pattern": "solid"}, "IDLE"),
    ("set_led_indicator", "I want the status light blinking green.", {"color": "green", "pattern": "blinking"}, "RUNNING"),
    (None, "What's 12 times 7?", {}, "RUNNING"),
    (None, "Write me a short poem about rain.", {}, "IDLE"),
    (None, "Who won the football match last night?", {}, "RUNNING"),
    (None, "Order lunch for the whole team.", {}, "IDLE"),
    (None, "Translate good morning into French.", {}, "RUNNING"),
    (None, "Set an alarm on my phone for 6 am.", {}, "IDLE"),
    (None, "What is your name?", {}, "RUNNING"),
    (None, "Turn on the office air conditioning.", {}, "IDLE"),
]


def make_cases(seed=7):
    r, cases = random.Random(seed), []
    for i, (tool, text, args, state) in enumerate(_RAW):
        speed, cur, temp = _S[state]
        for noisy in (False, True):
            cases.append({"id": f"ood-{tool or 'none'}-{i}-{'n' if noisy else 'c'}", "tool": tool, "args": args,
                          "noisy": noisy, "core": True,
                          "context": {"fsm_state": state,
                                      "telemetry": {"belt_speed_rpm": speed, "motor_current_ma": cur, "motor_temp_c": temp},
                                      "user_prompt": tc._speech_noise(text, r) if noisy else text}})
    return cases
