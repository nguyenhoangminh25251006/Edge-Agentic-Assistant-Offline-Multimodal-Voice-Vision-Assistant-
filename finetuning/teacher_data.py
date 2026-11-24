"""Cách B (theo TinyAgent): nhờ một LLM lớn hơn (teacher) viết câu nói, rồi kiểm tra và ghép thành dữ liệu huấn luyện.

So với cách A (câu do chương trình dựng từ mẫu):
  * Câu nói do teacher viết nên đa dạng hơn nhiều, nhưng có thể sai ý. Vì vậy mỗi câu phải qua bước kiểm tra
    (có đúng số, đúng từ khoá, không lẫn sang công cụ khác) mới được nhận.
  * Nhãn (công cụ + tham số) vẫn do chương trình chọn trước và đưa cho teacher, nên nhãn không bao giờ sai.
  * Prompt huấn luyện chỉ chứa công cụ đúng cộng vài công cụ không liên quan ("mẫu phủ định"), không phải cả 11 công cụ.

Các hàm chính:  make_chat -> generate -> to_cases -> build_training_rows
"""
import json
import random
import re
from collections import Counter
from pathlib import Path

import requests

import make_train as M
import tool_cases as T

ALL_TOOLS = M.ALL_TOOLS


# =====================================================================================
# 1. Nói cho teacher biết ý định bằng lời thường
# =====================================================================================
HISTORY_DESC = {"current": "the motor's electrical current", "temperature": "the motor temperature", "speed": "the belt speed",
                "product_count": "how many products were counted", "all_events": "all events and incidents"}
LIVE_DESC = {"all": "all the sensors at once", **{k: v for k, v in HISTORY_DESC.items() if k != "all_events"}}
PERIOD_DESC = {"last_hour": "in the last hour", "today": "today", "this_shift": "during the current shift"}
ALARM_DESC = {"ALERT_JAM": "the belt is jammed or blocked", "ALERT_OVERTEMP": "the motor is overheating",
              "ALERT_MANUAL_CHECK": "someone must come and check the machine by hand"}


def intent(tool, args):
    """Một câu tiếng Anh mô tả việc operator muốn làm. Teacher sẽ viết nhiều cách nói cho ý này."""
    if tool is None:
        return None
    if tool == "query_blackbox_logs":
        what = f"look up the recorded HISTORY (not the live value) of {HISTORY_DESC[args['target_metric']]}"
        n = args.get("lookback_minutes")
        return what + (f" for the last {n} minutes" if n else " (no time span given)")
    return {
        "request_line_restart": lambda: f"restart the stopped conveyor belt at {args['target_speed_rpm']} rpm",
        "trigger_audio_alarm": lambda: f"sound the audible alarm on site because {ALARM_DESC[args['alarm_code']]}",
        "get_live_telemetry": lambda: f"read the LIVE value right now of {LIVE_DESC[args['metric']]}",
        "get_last_stop_reason": lambda: "ask why the conveyor stopped the last time",
        "request_line_stop": lambda: "stop the conveyor belt",
        "get_production_stats": lambda: f"ask how many products were made (production count, throughput, jams) {PERIOD_DESC[args['period']]}",
        "get_inspection_summary": lambda: f"ask how many defective products the inspection camera found {PERIOD_DESC[args['period']]}",
        "request_set_speed": lambda: f"change the speed of the running belt to {args['target_speed_rpm']} rpm",
        "acknowledge_alarm": lambda: "say that the alarm has been handled, for example the jam was cleared",
        "set_led_indicator": lambda: f"set the machine's status light to {'steady' if args['pattern'] == 'solid' else 'blinking'} {args['color']}",
    }[tool]()


TEACHER_SYSTEM = ("You write realistic short voice commands that a factory operator might say to a voice assistant on a "
                  "conveyor line. Output only the commands, nothing else.")


def teacher_messages(tool, args, k):
    """Yêu cầu gửi cho teacher: viết k cách nói khác nhau cho cùng một ý."""
    if tool is None:
        task = (f"Write {k} different things a person might say to a voice assistant that have NOTHING to do with controlling or "
                "monitoring a conveyor belt: small talk, general questions, requests for other services, everyday tasks. "
                "Some may use words like set, stop, restart, speed, alarm, light, history or count in a different meaning.")
    else:
        task = (f"Write {k} different ways an operator could say this: {intent(tool, args)}. Vary the wording a lot "
                "(formal, casual, very short, indirect). Do not mention tool names or JSON. Keep any numbers exactly as given.")
    return [{"role": "system", "content": TEACHER_SYSTEM},
            {"role": "user", "content": task + "\nOne command per line, no numbering, no quotes."}]


def make_chat(base_url, model="teacher", max_tokens=250, timeout=300):
    """Hàm gọi teacher qua llama-server: chat(messages, temperature, seed) -> văn bản."""
    session = requests.Session()
    session.trust_env = False                     # không đi qua proxy hệ thống

    def chat(messages, temperature=0.9, seed=0):
        body = {"model": model, "messages": messages, "temperature": temperature, "top_p": 0.95, "seed": seed,
                "max_tokens": max_tokens}
        reply = session.post(f"{base_url}/v1/chat/completions", json=body, timeout=timeout)
        reply.raise_for_status()
        return reply.json()["choices"][0]["message"]["content"]

    return chat


def parse_lines(reply):
    """Tách câu trả lời của teacher thành từng câu, bỏ đánh số, gạch đầu dòng và dấu ngoặc kép."""
    lines = []
    for raw in reply.splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", raw).strip().strip("\"'`“”")
        if line:
            lines.append(line)
    return lines


# =====================================================================================
# 2. Kiểm tra từng câu: có đúng ý đã yêu cầu không
# =====================================================================================
def has(text, words):
    """Có từ nào bắt đầu bằng một trong `words` không (so khớp tiền tố, ví dụ 'temp' khớp 'temperature')."""
    return re.search(r"\b(?:" + "|".join(words) + ")", text.lower()) is not None


def mentions_number(text, n):
    """Câu có nói số n, bằng chữ số hoặc bằng chữ ('forty five')."""
    return re.search(rf"\b{n}\b", text) is not None or re.search(rf"\b{M.spell(n)}\b", text.lower()) is not None


METRIC_KW = {"current": ["current", "amp", "load", "power", "electric"], "temperature": ["temp", "hot", "heat", "thermal", "warm"],
             "speed": ["speed", "rpm", "fast", "velocity", "rotat"], "product_count": ["count", "product", "item", "part", "piece", "output", "unit"],
             "all_events": ["event", "incident", "everything", "happen", "log", "record", "alarm"],
             "all": ["all", "every", "whole", "full", "panel", "sensor", "reading"]}
PERIOD_KW = {"last_hour": ["hour", "60", "sixty"], "today": ["today", "day", "morning"], "this_shift": ["shift"]}
HISTORY_WORDS = ["history", "past", "last", "previous", "earlier", "log", "record", "trend", "ago", "back", "archive", "before", "histor"]
LIVE_WORDS = ["now", "live", "current", "right", "moment", "instant", "fresh", "latest", "this"]
PATTERN_KW = {"solid": ["solid", "steady", "constant", "still", "stable", "continuous", "fixed", "not blink"],
              "blinking": ["blink", "flash", "puls", "flicker"]}
CODE_KW = {"ALERT_JAM": ["jam", "block", "stuck", "clog", "obstruct"], "ALERT_OVERTEMP": ["hot", "heat", "temp", "overheat", "burn"],
           "ALERT_MANUAL_CHECK": ["check", "inspect", "look", "technician", "maintenance", "manual", "someone", "person", "hand", "examine"]}
SOUND_KW = ["alarm", "siren", "buzz", "alert", "warn", "speaker", "announce", "horn", "beep", "sound", "tell", "page", "call", "ring", "notify"]
STOP_KW = ["stop", "halt", "pause", "shut", "off", "kill", "freeze", "park", "end", "cut", "down", "standstill", "cease", "rest"]
WHY_KW = ["why", "reason", "cause", "happen", "went wrong", "trigger", "behind", "explain", "what"]


def _blackbox(a, t):
    n = a.get("lookback_minutes")
    number_ok = n is None or mentions_number(t, n) or (n == 60 and has(t, ["hour"]))
    return number_ok and has(t, METRIC_KW[a["target_metric"]]) and has(t, HISTORY_WORDS)


def _alarm(a, t):
    return has(t, SOUND_KW) and has(t, CODE_KW[a["alarm_code"]])


def _led(a, t):
    others = {"green", "yellow", "red"} - {a["color"]}
    return a["color"] in t.lower() and not any(c in t.lower() for c in others) and has(t, PATTERN_KW[a["pattern"]]) \
        and has(t, ["light", "lamp", "led", "indicator", "beacon", "tower", "signal", "status"])


RULES = {
    "query_blackbox_logs": _blackbox,
    "request_line_restart": lambda a, t: mentions_number(t, a["target_speed_rpm"]) and has(t, ["restart", "resume", "start", "again", "going", "back", "on", "run", "relaunch", "power", "fire", "bring"]) and not has(t, ["why"]),
    "trigger_audio_alarm": _alarm,
    "get_live_telemetry": lambda a, t: has(t, METRIC_KW[a["metric"]]) and has(t, LIVE_WORDS) and not has(t, ["history", "past", "earlier", "ago", "log", "previous"]),
    "get_last_stop_reason": lambda a, t: has(t, WHY_KW) and has(t, ["stop", "halt", "shut", "down", "breakdown", "standstill", "stall"]),
    "request_line_stop": lambda a, t: has(t, STOP_KW) and not has(t, ["why", "reason", "cause", "happen", "last"]),
    "get_production_stats": lambda a, t: has(t, PERIOD_KW[a["period"]]) and has(t, ["produc", "made", "make", "unit", "item", "count", "throughput", "jam", "output", "finish", "total", "manufactur"]) and not has(t, ["defect", "reject", "camera", "faulty"]),
    "get_inspection_summary": lambda a, t: has(t, PERIOD_KW[a["period"]]) and has(t, ["defect", "reject", "bad", "faulty", "fail", "camera", "inspect", "vision", "flag", "quality", "damaged", "scrap"]),
    "request_set_speed": lambda a, t: mentions_number(t, a["target_speed_rpm"]) and has(t, ["speed", "rpm", "faster", "slower", "adjust", "change", "set", "run", "make", "slow", "bump", "dial", "put"]) and not has(t, ["restart", "resume", "again", "relaunch"]),
    "acknowledge_alarm": lambda a, t: has(t, ["acknowledg", "clear", "fix", "handl", "dismiss", "resolv", "dealt", "sort", "silenc", "confirm", "reset", "clos", "done", "remov", "took care", "repair", "taken care"]),
    "set_led_indicator": _led,
}


def is_valid(tool, args, text):
    """Câu nói hợp lệ: độ dài hợp lý, tiếng Anh thuần, không lộ tên công cụ, và qua luật riêng của công cụ."""
    if not (2 <= len(text.split()) <= 25) or len(text) > 160 or not text.isascii():
        return False
    if "_" in text or "{" in text or "tool" in text.lower():
        return False
    return True if tool is None else RULES[tool](args, text)


# =====================================================================================
# 3. Vòng sinh câu (có thể chạy tiếp nếu bị dừng giữa chừng)
# =====================================================================================
def load_accepted(path):
    path = Path(path)
    return [json.loads(line) for line in open(path, encoding="utf-8")] if path.exists() else []


def generate(chat, out_path, n_per_tool, n_oos, blocked, seed=0, lines_per_call=6, max_calls=3000, log_every=50):
    """Hỏi teacher nhiều lần cho đến khi mỗi công cụ đủ `n_per_tool` câu hợp lệ (câu ngoài phạm vi: `n_oos`).

    chat      hàm do make_chat tạo ra
    out_path  file .jsonl lưu từng câu được nhận (mỗi dòng: tool, args, text). Chạy lại sẽ đọc tiếp file này.
    blocked   tập câu đã chuẩn hoá không được dùng (bộ test và bộ giữ riêng), để không rò rỉ
    Trả về bảng thống kê: số câu teacher viết và số câu được nhận cho từng công cụ.
    """
    rng = random.Random(seed)
    accepted = load_accepted(out_path)
    quota = lambda tool: n_oos if tool is None else n_per_tool
    counts = Counter(item["tool"] for item in accepted)
    seen = {(item["tool"], M.normalize(item["text"])) for item in accepted}
    stats = Counter()
    for calls in range(max_calls):
        todo = [t for t in ALL_TOOLS + [None] if counts[t] < quota(t)]
        if not todo:
            break
        tool = min(todo, key=lambda t: counts[t] / quota(t))            # luôn bổ sung cho công cụ còn thiếu nhiều nhất
        args = M.sample_args(tool, rng)
        for text in parse_lines(chat(teacher_messages(tool, args, lines_per_call), seed=rng.randrange(10**6))):
            stats[(tool, "viết")] += 1
            key = (tool, M.normalize(text))
            if not is_valid(tool, args, text) or key in seen or key[1] in blocked or counts[tool] >= quota(tool):
                continue
            seen.add(key)
            counts[tool] += 1
            stats[(tool, "nhận")] += 1
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"tool": tool, "args": args, "text": text}, ensure_ascii=False) + "\n")
        if (calls + 1) % log_every == 0:
            print(f"{calls + 1} lượt hỏi | đã nhận {sum(counts.values())}/{sum(quota(t) for t in ALL_TOOLS + [None])} câu")
    return stats


# =====================================================================================
# 4. Từ câu đã nhận thành dữ liệu huấn luyện
# =====================================================================================
def to_cases(accepted, seed=0):
    """Thêm trạng thái máy và (một nửa số câu) nhiễu giọng nói, giống cách A để hai cách so sánh công bằng."""
    rng, cases = random.Random(seed), []
    for i, item in enumerate(accepted):
        shown, noisy = M.make_utterance(item["text"].strip().rstrip(".!?"), rng)
        context = M.machine_context(rng, item["tool"] or "none")
        context["user_prompt"] = shown
        case = {"id": f"teacher-{i}", "tool": item["tool"], "args": item["args"], "noisy": noisy,
                "core": item["tool"] in T.TOOL_SETS[3] or item["tool"] is None, "context": context}
        assert T.score(case, json.dumps(M.target_json(case)))["ok"], f"nhãn sai: {case['id']}"
        cases.append(case)
    return cases


def tool_subset(case, rng, full_share=0.3):
    """Chọn tập công cụ đưa vào prompt huấn luyện (mẫu phủ định): công cụ đúng + vài công cụ không liên quan.
    Một phần (`full_share`) giữ đủ 11 công cụ để model vẫn dùng tốt khi không có bộ chọn công cụ."""
    if rng.random() < full_share:
        return list(ALL_TOOLS)
    gold = [case["tool"]] if case["tool"] else []
    size = rng.randint(2, 6) if gold else rng.randint(3, 7)
    distractors = rng.sample([t for t in ALL_TOOLS if t not in gold], size - len(gold))
    return [t for t in ALL_TOOLS if t in set(gold) | set(distractors)]          # giữ thứ tự cố định


def build_training_rows(cases, seed=0, full_share=0.3):
    """Mỗi ca thành một mẫu huấn luyện [system, user, assistant] với tập công cụ riêng."""
    rng, rows = random.Random(seed), []
    for case in cases:
        names = tool_subset(case, rng, full_share)
        rows.append({"n_tools": len(names), "messages": M.messages_cached(case, names)
                     + [{"role": "assistant", "content": json.dumps(M.target_json(case))}]})
    return rows
