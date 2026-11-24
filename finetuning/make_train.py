"""Sinh dữ liệu huấn luyện (v2) cho model gọi công cụ của băng tải.

Ý tưởng chính
-------------
* Nhãn (tool + tham số) được sinh bằng chương trình, nên luôn đúng. Chỉ cách diễn đạt là do mình viết.
* Có ba loại mẫu câu: "train" (để học), "held" (giữ riêng, chỉ để kiểm tra), và bộ test 156 câu
  có sẵn trong tool_cases.py. Script dừng lại nếu một câu train trùng với test hoặc held.
* Để model không học đường tắt theo từ khoá, dữ liệu có:
    - cặp câu gần giống nhau nhưng khác tool ("stop the belt" / "why did the belt stop"),
    - câu ngoài phạm vi chứa từ khoá của tool ("set a timer", "my car inspection is due"),
    - số viết bằng chữ ("forty rpm") và lỗi chính tả như nhận dạng giọng nói tạo ra.

Dùng:  python make_train.py        ->  train_v2.jsonl, heldout_v2.json
       from make_train import messages_cached, build_dataset
"""
import json
import random
import re
import sys
from collections import Counter

import tool_cases as T

ALL_TOOLS = T.TOOL_SETS[11]


# =====================================================================================
# 1. Prompt đưa vào model
# =====================================================================================
def messages_cached(case, tool_names):
    """Phần cố định (hướng dẫn + mô tả công cụ) nằm ĐẦU prompt để llama.cpp dùng lại được cache.
    Phần thay đổi (trạng thái máy + câu nói) nằm CUỐI."""
    tools = [t for t in T.TOOLS if t["function"]["name"] in tool_names]
    system = T.SYSTEM_PROMPT + "\n\nAVAILABLE TOOLS:\n" + json.dumps(tools, separators=(",", ":"))
    return [{"role": "system", "content": system},
            {"role": "user", "content": json.dumps(case["context"], separators=(",", ":"))}]


# =====================================================================================
# 2. Ngân hàng câu: mỗi mục có hai phần "train" và "held" (giữ riêng)
# =====================================================================================
# Ký hiệu trong mẫu câu: {s}=tốc độ rpm  {n}=số phút  {m}/{w}=tên số đo  {p}=khoảng thời gian
#                         {col}=màu đèn  {pat}=kiểu nháy của đèn
TEMPLATES = {
    "history": {   # query_blackbox_logs, có số phút
        "train": ["pull up the {m} over the past {n} minutes", "what did the {m} do during the last {n} minutes",
                  "i want the {m} history for the previous {n} minutes", "check the log for {m} going back {n} minutes",
                  "was there anything odd in the {m} earlier, last {n} minutes", "give me the recorded {m} from the last {n} minutes",
                  "review the stored {m} for the last {n} minutes", "how did the {m} look over the last {n} minutes",
                  "show the {m} trend for the past {n} minutes", "look back {n} minutes at the {m}",
                  "i need the {m} readings from the previous {n} minutes"],
        "held": ["scroll back through {n} minutes of {m} data", "dig through the archive of {m} from the past {n} minutes",
                 "can you replay the {m} for the last {n} minutes"]},
    "history_no_n": {   # query_blackbox_logs, không nói số phút
        "train": ["pull up the recorded {m}", "i want to review the past {m}", "dig out the {m} from the log",
                  "what does the log say about {m}", "open the archive for {m}", "show me the historical {m}",
                  "check what the {m} did earlier"],
        "held": ["bring up the logged {m}", "let me see how the {m} behaved before"]},
    "request_line_restart": {
        "train": ["get the belt going again at {s} rpm", "resume the line at {s} rpm", "bring the conveyor back up to {s} rpm",
                  "the area is safe, power the belt on at {s} rpm", "kick the line back into motion at {s} rpm",
                  "please relaunch the belt at {s} rpm", "start the belt up again at {s} rpm",
                  "turn the conveyor back on at {s} rpm"],
        "held": ["fire the line back up at {s} rpm", "relaunch operations at {s} rpm"]},
    "get_live_telemetry": {
        "train": ["what is {w} at the moment", "give me a fresh reading of {w}", "check {w} this instant",
                  "i need the live value of {w}", "how is {w} looking right now", "quick live look at {w}",
                  "tell me {w} currently", "what are we seeing for {w} right now"],
        "held": ["what does {w} say at this very moment", "pull the latest live number for {w}"]},
    "get_last_stop_reason": {
        "train": ["what made the belt stop earlier", "explain the previous stoppage", "why is the line down",
                  "tell me what triggered the last shutdown", "what was the cause of the most recent stop",
                  "what happened before the line halted", "why did we shut down before",
                  "what brought the belt to a standstill", "give me the reason for the last stoppage",
                  "what went wrong the last time the line stopped", "can you tell me what caused the previous halt",
                  "i want to know why it stopped last", "what was behind the last breakdown",
                  "find out why the line stopped back then", "figure out why the conveyor went down last time",
                  "what stopped us last time", "walk me through the last stoppage"],
        "held": ["what was the trigger for the previous shutdown", "help me understand why the line went down",
                 "diagnose the last stop"]},
    "request_line_stop": {
        "train": ["shut down the line", "bring the conveyor to a halt", "turn off the belt", "cut the belt now",
                  "i need the line stopped", "freeze the conveyor", "stop everything on the belt",
                  "i want the belt to stop moving", "shut the conveyor off", "bring the line to a stop please",
                  "end the run now", "kill the conveyor", "take the belt offline", "park the line",
                  "power the belt down", "switch the conveyor off", "make the line stand still",
                  "stop the conveyor immediately please"],
        "held": ["bring the belt to rest", "cease belt movement"]},
    "get_production_stats": {
        "train": ["what is the production count {p}", "how many units have we produced {p}", "throughput numbers {p}",
                  "how many jams did we have {p}", "give me the output figures {p}", "what is our production total {p}",
                  "tell me the number of finished items {p}", "report the production totals {p}",
                  "how much did we produce {p}", "what is the throughput {p}", "jam count {p}"],
        "held": ["what was our output {p}", "i want the production summary {p}"]},
    "get_inspection_summary": {
        "train": ["number of rejects from the camera {p}", "how many bad parts did vision catch {p}", "defect count {p}",
                  "what did the inspection camera flag {p}", "how many faulty pieces were detected {p}",
                  "tally the rejected pieces {p}", "how many items failed inspection {p}",
                  "camera reject count {p}", "what is the defect total {p}"],
        "held": ["how many pieces did the vision system turn down {p}", "give me the quality control numbers {p}"]},
    "request_set_speed": {
        "train": ["speed the belt up or down to {s} rpm", "run the line at {s} rpm instead", "adjust the conveyor to {s} rpm",
                  "make the belt run at {s} rpm", "i want {s} rpm on the running line", "shift the running belt to {s} rpm",
                  "set the conveyor speed to {s} rpm", "change the belt speed to {s} rpm", "set the line to {s} rpm",
                  "switch the running belt to {s} rpm", "put the belt at {s} rpm while it is running",
                  "slow it down to {s} rpm", "bump the speed up to {s} rpm", "take the belt speed to {s} rpm"],
        "held": ["dial the belt speed to {s} rpm", "get the running conveyor to {s} rpm"]},
    "acknowledge_alarm": {
        "train": ["the jam is cleared, dismiss the alarm", "ok i fixed it, clear the alert", "got it, the alarm is handled",
                  "i dealt with the alarm already", "confirm the alarm, the problem is resolved",
                  "problem solved, you can silence the alert now", "the blockage is gone, mark the alarm as handled",
                  "i took care of the fault so reset the warning", "done fixing it, acknowledge",
                  "all clear, the alert can be closed", "i removed the obstruction, acknowledge that alert",
                  "we sorted the issue, dismiss the warning", "the machine problem is fixed, log the alarm as handled",
                  "tell the system i handled the alarm", "noted, the alert has been taken care of",
                  "fault repaired, please acknowledge it", "it is all sorted, close the alarm"],
        "held": ["that fault is dealt with, you can close the alert", "ok it is repaired, mark the warning as done",
                 "consider the alarm resolved"]},
    "set_led_indicator": {
        "train": ["make the indicator {pat} {col}", "set the light to {pat} {col}", "show a {pat} {col} status light",
                  "switch the lamp to {col} and {pat}", "i want the beacon {pat} {col}", "set the signal lamp to {pat} {col}",
                  "light the tower {pat} {col}", "make the warning light {col} and {pat}",
                  "i would like the status lamp {pat} {col}"],
        "held": ["give me a {pat} {col} indicator", "the lamp should be {pat} {col}"]},
}

# Cách gọi từng số đo / khoảng thời gian / kiểu đèn
WORDS = {
    "history_metric": {
        "train": {"current": ["amperage", "amp draw", "motor current", "electrical load", "power draw"],
                  "temperature": ["motor temp", "how hot the motor got", "thermal readings", "temperature", "heat readings"],
                  "speed": ["rpm", "belt speed", "how fast the belt ran", "rotation speed"],
                  "product_count": ["output count", "items produced", "parts counted", "piece count"],
                  "all_events": ["event log", "incidents", "everything that happened", "incident history"]},
        "held": {"current": ["current consumption", "motor amps"], "temperature": ["motor heat", "thermal data"],
                 "speed": ["conveyor velocity", "belt rpm"], "product_count": ["production tally", "units counted"],
                 "all_events": ["every event", "the full incident record"]}},
    "live_metric": {
        "train": {"all": ["every sensor", "the whole sensor panel", "all readings"],
                  "current": ["amperage", "the motor load", "power draw"],
                  "temperature": ["motor temp", "the temperature", "heat reading"],
                  "speed": ["rpm", "how fast the belt is going", "belt rpm"],
                  "product_count": ["the item counter", "parts counted", "piece counter"]},
        "held": {"all": ["the full sensor set", "everything the sensors report"],
                 "current": ["motor amps", "electrical draw"], "temperature": ["motor heat", "thermal level"],
                 "speed": ["conveyor speed", "the live rpm"], "product_count": ["the unit counter", "the product tally sensor"]}},
    "period": {
        "train": {"last_hour": ["over the past hour", "during the previous hour", "in the last 60 minutes", "within the last hour"],
                  "today": ["so far today", "over the whole day", "since the day began", "for today"],
                  "this_shift": ["during my shift", "in the current shift", "since the shift started", "on this shift"]},
        "held": {"last_hour": ["across the preceding hour", "in the most recent hour"],
                 "today": ["during today's run", "for the current day"],
                 "this_shift": ["throughout this shift", "over the course of the shift"]}},
    "pattern": {"solid": ["solid", "steady"], "blinking": ["blinking", "flashing"]},
    "alarm": {   # cả câu, vì mỗi mã còi có cách nói riêng
        "train": {"ALERT_JAM": ["blast the jam siren", "alert the floor that the belt is blocked", "raise the blockage warning",
                                "make the speaker announce a jam", "hit the jam buzzer", "let the crew hear that the line is jammed",
                                "warn the crew about the blockage", "play the blockage warning over the speaker"],
                  "ALERT_OVERTEMP": ["trigger the overheat siren", "alert the floor that the motor is overheating",
                                     "raise the high temperature warning", "make the speaker announce overheating",
                                     "hit the overtemperature buzzer", "let the crew hear that the motor is too hot",
                                     "sound the over-temperature alert", "play the overheating warning over the speaker"],
                  "ALERT_MANUAL_CHECK": ["page a technician to inspect the line", "raise the hands-on inspection alert",
                                         "request a manual look at the machine", "make the speaker ask for a manual check",
                                         "hit the inspection buzzer", "let the crew hear that someone must check it by hand",
                                         "ask for a physical inspection of the machine", "ring for someone to inspect the machine by hand",
                                         "get a person to physically inspect the line"]},
        "held": {"ALERT_JAM": ["sound off about the jammed belt", "use the buzzer for the blocked line"],
                 "ALERT_OVERTEMP": ["buzz everyone about the motor running too hot", "alert the crew with the overheating tone"],
                 "ALERT_MANUAL_CHECK": ["summon a person to examine the machine in person", "alert maintenance to come look at it by hand"]}},
}

# Câu ngoài phạm vi. Nhiều câu chứa từ khoá của công cụ để model không học đường tắt.
OUT_OF_SCOPE = {
    "train": ["what time is it", "good morning", "thank you very much", "tell me about yourself", "who won the football match",
              "translate hello into french", "book a meeting for tomorrow", "calculate twelve times eight", "reset my password",
              "what is the tallest mountain", "recommend a good movie", "how does a conveyor belt work", "what does rpm stand for",
              "can you open the factory door", "write me a poem", "how many days until friday", "what is the price of steel today",
              "send a message to the manager", "order new safety gloves", "where is the nearest coffee shop", "what is your name",
              "set an alarm clock for seven", "turn up the music", "how do i get to the train station", "explain machine learning",
              "what is the meaning of life", "lets chat for a bit", "how was your weekend", "tell me a fun fact", "who is the president",
              "can you cook dinner", "schedule maintenance for next month", "print the quarterly report",
              "what is the exchange rate for dollars", "how tall is the eiffel tower", "do you like pizza",
              "remind me to call my mother", "what should i have for lunch", "change the factory wifi password",
              "lock the office door", "turn off the office lights", "what is two plus two", "give me a motivational quote",
              "who built this machine", "when was the factory founded", "what is a good name for a dog", "can you hear me",
              "say something funny", "what is the speed of light", "how do planes fly",
              # từ khoá của tool nhưng không liên quan đến băng tải
              "set a timer for ten minutes", "set my phone to silent", "turn the volume up", "what is the speed limit on the highway",
              "how fast can a cheetah run", "my alarm clock is broken", "my car inspection is due next week",
              "how many products does the store sell", "i need to stop by the shop later", "light a candle for me",
              "the meeting will start in thirty minutes", "show me the history of the roman empire",
              "what is the current time in tokyo", "read me the news live", "how hot is it outside", "pause the music",
              "restart my computer", "restart the meeting", "change the channel", "set the table",
              "what is the capital of japan", "play my favorite song", "tell me a riddle", "email the warehouse team",
              "how much does a forklift cost", "are you a robot", "whats the news today", "order lunch for the crew"],
    "held": ["what year did the war end", "play something relaxing", "restart the router in the office", "set a reminder for my dentist",
             "how fast is a bullet train", "the weather looks hot today", "email me the recipe", "stop the music",
             "what is the population of canada", "how do i bake bread", "turn the heating up in the break room",
             "i need a break in ten minutes"],
}


# =====================================================================================
# 3. Chọn tham số đúng, rồi diễn đạt thành câu
# =====================================================================================
_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = {20: "twenty", 30: "thirty", 40: "forty", 50: "fifty", 60: "sixty", 70: "seventy", 80: "eighty", 90: "ninety"}


def spell(n):
    """Viết số bằng chữ như nhận dạng giọng nói hay làm: 45 -> 'forty five', 120 -> 'one hundred twenty'."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = (n // 10) * 10, n % 10
        return _TENS[tens] + (" " + _ONES[ones] if ones else "")
    rest = n % 100
    return "one hundred" + (" " + spell(rest) if rest else "")


def _number(n, r):
    """75% viết bằng chữ số, 25% bằng chữ."""
    return str(n) if r.random() < 0.75 else spell(n)


def sample_args(tool, r):
    """Chọn ngẫu nhiên tham số đúng cho một công cụ. Số 30 phút bị loại vì scorer coi 30 là giá trị mặc định."""
    if tool == "query_blackbox_logs":
        args = {"target_metric": r.choice(T._METRICS)}
        if r.random() < 0.7:
            args["lookback_minutes"] = r.choice([5, 10, 15, 20, 25, 45, 60, 90, 120])
        return args
    if tool in ("request_line_restart", "request_set_speed"):
        return {"target_speed_rpm": r.choice([20, 25, 30, 35, 40, 45, 50])}
    if tool == "trigger_audio_alarm":
        return {"alarm_code": r.choice(list(WORDS["alarm"]["train"]))}
    if tool == "get_live_telemetry":
        return {"metric": r.choice(list(WORDS["live_metric"]["train"]))}
    if tool in ("get_production_stats", "get_inspection_summary"):
        return {"period": r.choice(list(WORDS["period"]["train"]))}
    if tool == "set_led_indicator":
        return {"color": r.choice(["green", "yellow", "red"]), "pattern": r.choice(["solid", "blinking"])}
    return {}


def render(tool, args, split, r):
    """Diễn đạt (tool, tham số) thành một câu nói. split là 'train' hoặc 'held'."""
    pick = lambda key: r.choice(TEMPLATES[key][split])
    if tool is None:
        return r.choice(OUT_OF_SCOPE[split])
    if tool == "query_blackbox_logs":
        m = r.choice(WORDS["history_metric"][split][args["target_metric"]])
        if "lookback_minutes" in args:
            return pick("history").format(m=m, n=_number(args["lookback_minutes"], r))
        return pick("history_no_n").format(m=m)
    if tool == "trigger_audio_alarm":
        return r.choice(WORDS["alarm"][split][args["alarm_code"]])
    if tool == "get_live_telemetry":
        return pick(tool).format(w=r.choice(WORDS["live_metric"][split][args["metric"]]))
    if tool in ("get_production_stats", "get_inspection_summary"):
        return pick(tool).format(p=r.choice(WORDS["period"][split][args["period"]]))
    if tool in ("request_line_restart", "request_set_speed"):
        return pick(tool).format(s=_number(args["target_speed_rpm"], r))
    if tool == "set_led_indicator":
        return pick(tool).format(col=args["color"], pat=r.choice(WORDS["pattern"][args["pattern"]]))
    return pick(tool)                      # công cụ không có tham số


def _typo(text, r):
    """Một lỗi nhỏ như nhận dạng giọng nói: bỏ một chữ, đảo hai chữ cạnh nhau, hoặc đọc 'rpm' thành 'r p m'."""
    if " rpm" in text and r.random() < 0.3:
        return text.replace(" rpm", " r p m")
    words = text.split()
    idx = [i for i, w in enumerate(words) if len(w) >= 5 and w.isalpha()]
    if not idx:
        return text
    i = r.choice(idx)
    w, j = words[i], r.randrange(1, len(words[i]) - 2)
    words[i] = w[:j] + w[j + 1:] if r.random() < 0.5 else w[:j] + w[j + 1] + w[j] + w[j + 2:]
    return " ".join(words)


def make_utterance(text, r):
    """Nửa số câu là giọng nói (không hoa, không dấu câu, có 'um'); một phần trong đó còn có lỗi chính tả."""
    if r.random() < 0.5:
        spoken = T._speech_noise(text, r)
        return (_typo(spoken, r) if r.random() < 0.25 else spoken), True
    return text.capitalize() + ".", False


# =====================================================================================
# 4. Trạng thái máy, đáp án, và ghép thành bộ dữ liệu
# =====================================================================================
def machine_context(r, tool):
    """70% trạng thái hợp lý với lệnh, 30% ngẫu nhiên để model đọc câu nói chứ không đoán theo trạng thái."""
    plausible = {"request_line_restart": ["IDLE", "JAMMED", "OVERTEMP"], "request_set_speed": ["RUNNING"],
                 "request_line_stop": ["RUNNING"], "acknowledge_alarm": ["JAMMED", "OVERTEMP"]}
    state = r.choice(plausible[tool]) if tool in plausible and r.random() < 0.7 else r.choice(list(T._STATES))
    speed, current, temp = T._STATES[state]
    return {"fsm_state": state, "telemetry": {"belt_speed_rpm": speed, "motor_current_ma": current, "motor_temp_c": temp}}


OUT_OF_SCOPE_REPLY = "Sorry, I can only help with the conveyor line."


def target_json(case):
    """Câu trả lời đúng mà model phải sinh ra."""
    if case["tool"] is None:
        return {"action_type": "DIRECT_ANSWER", "tool_call": None, "response_text": OUT_OF_SCOPE_REPLY}
    args = dict(case["args"])
    if case["tool"] in ("request_line_restart", "request_line_stop"):
        args["reason"] = "operator request"          # tham số văn bản tự do, scorer không so nội dung
    return {"action_type": "TOOL_REQUEST", "tool_call": {"tool_name": case["tool"], "arguments": args},
            "response_text": ""}


FILLERS = {"um", "uh", "okay", "hey"}


def normalize(text):
    """Dạng chuẩn để so trùng câu: chữ thường, bỏ dấu câu, bỏ từ đệm."""
    words = re.sub(r"[^\w\s]", "", text.lower()).split()
    return " ".join(w for w in words if w not in FILLERS)


MAX_REPEAT = 4      # cùng một câu tối đa 4 lần, mỗi lần với trạng thái máy khác


def build_split(split, per_tool, per_oos, seed):
    r, cases, seen = random.Random(seed), [], {}
    for tool in ALL_TOOLS + [None]:
        want, got, tries = (per_oos if tool is None else per_tool), 0, 0
        while got < want and tries < want * 40:
            tries += 1
            args = sample_args(tool, r)
            shown, noisy = make_utterance(render(tool, args, split, r), r)
            key = (tool, normalize(shown))
            if seen.get(key, 0) >= MAX_REPEAT:
                continue
            seen[key] = seen.get(key, 0) + 1
            context = machine_context(r, tool) if tool else machine_context(r, "none")
            context["user_prompt"] = shown
            cases.append({"id": f"{split}-{tool or 'none'}-{got}", "tool": tool, "args": args, "noisy": noisy,
                          "core": tool in T.TOOL_SETS[3] or tool is None, "context": context})
            got += 1
    r.shuffle(cases)
    return cases


def check_no_leak(train, held):
    """Không câu train nào được trùng với held hoặc với bộ test 156 câu. Trả về số câu trùng test của held."""
    test = {normalize(c["context"]["user_prompt"]) for c in T.make_cases()}
    tr = {normalize(c["context"]["user_prompt"]) for c in train}
    he = {normalize(c["context"]["user_prompt"]) for c in held}
    bad = (tr & test) | (tr & he)
    if bad:
        raise ValueError(f"Rò rỉ dữ liệu giữa train và test/held: {sorted(bad)[:8]}")
    return len(he & test)


def build_dataset(per_tool=150, per_oos=300, held_per_tool=12, held_oos=24, seed=7):
    """Trả về (train, held) là hai danh sách ca. Mọi nhãn đều được scorer của dự án chấm là đúng."""
    train = build_split("train", per_tool, per_oos, seed)
    held = build_split("held", held_per_tool, held_oos, seed + 1)
    check_no_leak(train, held)
    for case in train + held:
        assert T.score(case, json.dumps(target_json(case)))["ok"], f"nhãn sai: {case['id']}"
    return train, held


def to_chat_rows(cases, tool_names=ALL_TOOLS):
    """Mỗi ca thành một mẫu huấn luyện: [system, user, assistant]."""
    for case in cases:
        yield {"messages": messages_cached(case, tool_names) + [{"role": "assistant", "content": json.dumps(target_json(case))}]}


def main(train_path="train_v2.jsonl", held_path="heldout_v2.json"):
    train, held = build_dataset()
    with open(train_path, "w", encoding="utf-8") as f:
        for row in to_chat_rows(train):
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    json.dump(held, open(held_path, "w", encoding="utf-8"), ensure_ascii=False)
    print("train:", len(train), dict(Counter(c["tool"] for c in train)))
    print("held :", len(held), dict(Counter(c["tool"] for c in held)))
    print("train, held và test 156 câu không trùng nhau: OK")


if __name__ == "__main__":
    sys.exit(main())
