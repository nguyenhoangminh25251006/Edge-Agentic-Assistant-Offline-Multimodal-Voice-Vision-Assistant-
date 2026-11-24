"""Độ trễ mỗi lệnh trên Pi (4 nhân A53, -t 4): fine-tune AB (JSON tự do + grammar) vs Qwen3 gốc (tool-call native).

Hai bước:
  PC : python ab_pi_latency.py make payloads.json   -> dựng sẵn body request đúng định dạng mỗi model
       (AB: messages_cached + GBNF grammar như lúc chấm; Qwen3: bố cục B, 11 tool native, thinking tắt)
  Pi : python3 ab_pi_latency.py run payloads.json   -> chỉ dùng thư viện chuẩn

Công bằng: cùng cờ server (-c 2048 -np 1 -cram 0 -fa on --jinja -t 4), cùng 6 câu, cùng trạng thái máy,
temperature 0. Mỗi lượt: server mới -> 1 câu làm nóng cache phần cố định -> 6 câu đo (prompt cache bật,
như khi triển khai server chạy liên tục). 2 vòng, đổi thứ tự model mỗi vòng. Chờ throttle sạch + nghỉ 20 s.
Crash được ghi lại và chạy lại lượt đó (tối đa 3 lần).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

QUESTIONS = [  # lấy từ tool_cases_ood (câu sạch), trạng thái máy theo câu
    ("What's the motor temperature at this very moment?", "RUNNING"),
    ("Blast the jam siren.", "JAMMED"),
    ("Slow the belt down to 20 rpm.", "RUNNING"),
    ("Why did the line halt?", "JAMMED"),
    ("Make the status light flash red.", "JAMMED"),
    ("Write me a short poem about rain.", "IDLE"),
]
WARM = ("How fast is the belt running right now?", "RUNNING")
MODELS = {"AB": "Qwen2.5-0.5B-Instruct-AB.Q4_K_M-himx-o4", "Qwen3": "Qwen3-0.6B.Q4_K_M-himx-o4",
          "ABn": "Qwen2.5-0.5B-Instruct-ABn.Q4_K_M-himx-o4"}   # ABn = AB train lại, đầu ra <tool_call> native, bố cục B


def make(path):
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(root), str(root / "finetuning")]
    import eval_tools as E
    import make_train
    import tool_cases as T
    sys.path.insert(0, str(Path(__file__).parent))
    import server_latency as sl

    def ctx(q, state):
        sp, cur, temp = T._STATES[state]
        return {"context": {"fsm_state": state, "telemetry": {"belt_speed_rpm": sp, "motor_current_ma": cur, "motor_temp_c": temp},
                            "user_prompt": q}}

    def ab(q, state):
        return {"messages": make_train.messages_cached(ctx(q, state), E.NAMES), "grammar": E.schema_v2(),
                "temperature": 0, "max_tokens": 200, "cache_prompt": True}

    def qwen3(q, state):
        sp, cur, temp = T._STATES[state]
        st = {"state": state, "speed_rpm": sp, "motor_current_ma": cur, "motor_temp_c": temp}
        return {"messages": sl.msgs("B", st, q), "tools": T.TOOLS, "temperature": 0, "max_tokens": 200,
                "cache_prompt": True, "chat_template_kwargs": {"enable_thinking": False}}

    def abn(q, state):   # cùng bố cục B như Qwen3, không có cờ thinking (Qwen2.5 không có chế độ này)
        body = qwen3(q, state)
        del body["chat_template_kwargs"]
        return body

    out = {m: {"warm": f(*WARM), "questions": [f(q, s) for q, s in QUESTIONS]}
           for m, f in (("AB", ab), ("Qwen3", qwen3), ("ABn", abn))}
    Path(path).write_text(json.dumps(out), encoding="utf-8")
    print("wrote", path)


HOME = os.path.expanduser("~")
BIN = f"{HOME}/llama/build/bin/llama-server"
BASE = "http://127.0.0.1:8080"
COMMON = ["-c", "2048", "-np", "1", "-cram", "0", "-fa", "on", "--jinja", "-t", "4", "--host", "127.0.0.1", "--port", "8080"]
ROUNDS, TRIES = 2, 3


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def env():
    return {"temp": sh("vcgencmd measure_temp").split("=")[-1], "clk_mhz": int(sh("vcgencmd measure_clock arm").split("=")[-1]) // 10**6,
            "throttled": sh("vcgencmd get_throttled").split("=")[-1]}


def wait_clean():
    for _ in range(120):
        if int(sh("vcgencmd get_throttled").split("=")[-1], 16) & 0x7 == 0:
            break
        time.sleep(1)
    time.sleep(20)


def post(body):
    req = urllib.request.Request(BASE + "/v1/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=1800) as r:
        d = json.loads(r.read())
    t, m = d.get("timings", {}), d["choices"][0]["message"]
    return {"wall_s": round(time.time() - t0, 2), "prompt_n": t.get("prompt_n"), "cache_n": t.get("cache_n"),
            "prompt_ms": round(t.get("prompt_ms", 0)), "pred_n": t.get("predicted_n"), "pred_ms": round(t.get("predicted_ms", 0)),
            "pred_tps": round(t.get("predicted_per_second", 0), 2),
            "out": (m.get("content") or "")[:160], "calls": [c["function"]["name"] for c in (m.get("tool_calls") or [])]}


def start(model, label):
    log = open(f"{HOME}/abpi_{label}.log", "w")
    p = subprocess.Popen([BIN, "-m", f"{HOME}/models/{model}.gguf", *COMMON], stdout=log, stderr=subprocess.STDOUT)
    t0 = time.time()
    while time.time() - t0 < 300:
        try:
            urllib.request.urlopen(BASE + "/health", timeout=2)
            return p
        except OSError:
            if p.poll() is not None:
                raise RuntimeError(f"server exit {p.poll()} khi khởi động")
            time.sleep(0.5)
    raise RuntimeError("server khởi động quá 300 s")


def stop(p):
    p.kill()
    p.wait()


def out(**kw):
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def trial(name, pay, rnd):
    crashes = []
    for attempt in range(1, TRIES + 1):
        wait_clean()
        e = env()
        p = start(MODELS[name], name)       # chỉ dừng đúng tiến trình mình bật (p.kill), không pkill
        rows = []
        try:
            warm = post(pay["warm"])
            for i, body in enumerate(pay["questions"]):
                rows.append({"q": i + 1, **post(body)})
            stop(p)
            out(kind="warm", model=name, round=rnd, attempt=attempt, crashes=crashes, env=e, env_end=env(), **warm)
            for r in rows:
                out(kind="q", model=name, round=rnd, attempt=attempt, **r)
            return
        except Exception as ex:  # noqa: BLE001
            time.sleep(1)
            crashes.append(f"q{len(rows) + 1}: {type(ex).__name__} exit={p.poll()}")
            stop(p)
    out(kind="q", model=name, round=rnd, FAILED=True, crashes=crashes)


def run(path, names=None):
    pay = json.load(open(path))
    names = names or ["AB", "Qwen3"]
    out(kind="meta", models={n: MODELS[n] for n in names}, flags=COMMON, rounds=ROUNDS, env=env(), date=sh("date -Iseconds"))
    for rnd in range(1, ROUNDS + 1):
        for name in (names if rnd % 2 else names[::-1]):
            trial(name, pay[name], rnd)
    out(kind="done")


if __name__ == "__main__":
    if sys.argv[1] == "make":
        make(sys.argv[2])
    else:
        run(sys.argv[2], sys.argv[3:] or None)   # vd: run payloads.json ABn AB
