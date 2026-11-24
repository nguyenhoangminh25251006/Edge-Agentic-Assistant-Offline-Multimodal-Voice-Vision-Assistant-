"""AB-native: train lại công thức AB (dữ liệu A + B) nhưng đầu ra là <tool_call> NATIVE của Qwen, bố cục triển khai B.

Khác AB gốc (qwen_AB_hybrid_recipe.ipynb):
  * Đầu ra: <tool_call>{"name","arguments"}</tool_call> (khoảng 22 token) thay JSON tự do + grammar (khoảng 40 token)
    -> phần sinh token trên Pi ngắn đi gần một nửa. Câu ngoài phạm vi: một câu ngắn.
  * Prompt: bố cục B như server_latency / server_fair (system cố định + 11 tool qua chat template,
    trạng thái máy ở đầu tin nhắn user) -> phần cố định dùng lại cache được giữa các lệnh.
  * Thêm câu DỪNG KHẨN CẤP có ngữ cảnh nguy hiểm (người/tay gần băng tải): AB gốc gọi nhầm thành báo động.
  * Dữ liệu teacher (B) lọc lại bằng luật từ khoá đã siết trong teacher_data.is_valid.
  * Dùng make_train.check_no_leak bản mới (so cả mẫu câu che số).
Giữ nguyên: LoRA r=32 alpha=64 lr 1.5e-4, 1 epoch, bs 1 x accum 16; base Qwen2.5-0.5B-Instruct.

Chạy: python abn_native.py [data|train|gguf|eval|all]
"""
import json
import os
import random
import re
import sys
from pathlib import Path

os.environ.setdefault("SLM_DEVICE", "gpu")
ROOT = Path(__file__).parent
sys.path[:0] = [str(ROOT), str(ROOT / "finetuning"), str(ROOT / "results_pi" / "scripts")]
import lora_train  # noqa: E402
import make_train as M  # noqa: E402
import model_catalog as mc  # noqa: E402
import pi_quant_eval as pe  # noqa: E402
import server_latency as sl  # noqa: E402
import slm_tools as st  # noqa: E402
import teacher_data as D  # noqa: E402
import tool_cases as T  # noqa: E402
import tool_cases_ood as ood  # noqa: E402

BASE = "Qwen2.5-0.5B-Instruct"
BASE_DIR = Path("C:/model_cache/hf") / BASE
CONVERT = Path("C:/model_cache/llama_src/convert_hf_to_gguf.py")
LLAMA_DIR = Path("./llama.cpp")
RUN = Path("runs/qwen05_ABn")
TRAIN_FILE = RUN / "train_ABn.jsonl"
MERGED = RUN / "merged"
F16 = RUN / f"{BASE}-ABn.F16.gguf"
IMATRIX = RUN / "imatrix_ABn.gguf"
CALIB = RUN / "calib_ABn.txt"
QUANTS = {"Q4_K_M-o4": ["--output-tensor-type", "q4_0"],
          "Q4_K_M-himx-o4": ["--imatrix", str(IMATRIX), "--output-tensor-type", "q4_0"]}
HELD_FILE = Path("runs/qwen05_A/heldout_v2.json")
TEACHER_RAW = Path("runs/qwen05_B/teacher_queries.jsonl")
LORA_CFG = dict(r=32, alpha=64, dropout=0.05, lr=1.5e-4, epochs=1, bs=1, accum=16)
STATE_KEYS = {"fsm_state": "state"}

# Câu dừng khẩn cấp có ngữ cảnh nguy hiểm. KHÔNG dùng câu/mẫu câu của tool_cases_ood (bộ chấm), chỉ cùng ý.
EMERGENCY = [
    "emergency stop, someone is caught in the conveyor",
    "stop the belt right now, a worker's sleeve is stuck in it",
    "halt the line immediately, there is a person too close to the rollers",
    "e stop the conveyor, somebody got their arm trapped",
    "shut it down fast, someone's fingers are in the belt",
    "stop the conveyor, a guy is leaning right over it",
    "cut the power to the belt, a person fell onto it",
    "freeze the line now, there's a hand inside the machine",
    "stop now, someone is in danger next to the belt",
    "turn the conveyor off right away, a kid is standing next to it",
    "the belt grabbed someone's glove, stop it",
    "halt everything, a technician is still inside the guard",
    "quick, stop the line, his hair is caught",
    "stop the belt, someone is reaching in to grab a box",
]


# ------------------------------------------------------------------ định dạng native, bố cục B
def native_messages(case):
    ctx = case["context"]
    sp, cur, temp = ctx["telemetry"]["belt_speed_rpm"], ctx["telemetry"]["motor_current_ma"], ctx["telemetry"]["motor_temp_c"]
    state = {"state": ctx["fsm_state"], "speed_rpm": sp, "motor_current_ma": cur, "motor_temp_c": temp}
    return sl.msgs("B", state, ctx["user_prompt"])


def native_target(case):
    if case["tool"] is None:
        return M.OUT_OF_SCOPE_REPLY
    args = dict(case["args"])
    if case["tool"] in ("request_line_restart", "request_line_stop"):
        args["reason"] = "operator request"
    return "<tool_call>\n" + json.dumps({"name": case["tool"], "arguments": args}, ensure_ascii=False) + "\n</tool_call>"


def emergency_cases(seed=11, copies=5):
    r, cases = random.Random(seed), []
    for i, text in enumerate(EMERGENCY):
        for k in range(copies):
            shown, noisy = M.make_utterance(text, r)
            ctx = M.machine_context(r, "request_line_stop")
            ctx["user_prompt"] = shown
            cases.append({"id": f"emg-{i}-{k}", "tool": "request_line_stop", "args": {}, "noisy": noisy, "core": True,
                          "context": ctx})
    return cases


def data():
    RUN.mkdir(parents=True, exist_ok=True)
    train_a, _ = M.build_dataset()                         # cách A, seed 7 như notebook (cùng bộ câu)
    held = json.load(open(HELD_FILE, encoding="utf-8"))
    accepted = D.load_accepted(TEACHER_RAW)
    kept = [a for a in accepted if D.is_valid(a["tool"], a["args"], a["text"])]   # luật đã siết
    train_b = D.to_cases(kept)
    emg = emergency_cases()
    test = T.make_cases()
    key = lambda c: M.mask_numbers(M.normalize(c["context"]["user_prompt"]))     # mức mẫu câu (che số)
    forbidden = {key(c) for c in test + held}
    cases = [c for c in train_a + train_b + emg if key(c) not in forbidden]
    M.check_no_leak(cases, held)                           # nguyên văn + mẫu câu che số, với test156 và held
    ood_keys = {M.mask_numbers(M.normalize(c["context"]["user_prompt"])) for c in ood.make_cases()}
    ood_hit = sum(M.mask_numbers(M.normalize(c["context"]["user_prompt"])) in ood_keys for c in emg)
    assert ood_hit == 0, "câu khẩn cấp trùng mẫu câu với bộ chấm OOD"
    rows = [{"messages": native_messages(c) + [{"role": "assistant", "content": native_target(c)}], "tools": T.TOOLS}
            for c in cases]
    random.Random(42).shuffle(rows)
    with open(TRAIN_FILE, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    print(f"train_ABn: {len(rows)} mẫu | A {len(train_a)} | B {len(kept)}/{len(accepted)} câu teacher qua luật mới "
          f"| khẩn cấp {len(emg)} | bỏ {len(train_a) + len(train_b) + len(emg) - len(cases)} trùng test/held", flush=True)


def train():
    if not (MERGED / "config.json").exists():
        hist = lora_train.train_lora(str(BASE_DIR), TRAIN_FILE, MERGED, **LORA_CFG)
        json.dump(hist, open(RUN / "train_history.json", "w", encoding="utf-8"), indent=2)


def gguf():
    if not F16.exists():
        mc._run([sys.executable, CONVERT, MERGED, "--outfile", F16, "--outtype", "f16"])
    if not CALIB.exists():   # như notebook AB: wiki 100k + 160 mẫu train đầu, render đúng template có tools
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(str(BASE_DIR))
        parts = [Path("wiki.test.raw").read_text(encoding="utf-8")[:100_000]] if Path("wiki.test.raw").exists() else []
        for line in list(open(TRAIN_FILE, encoding="utf-8"))[:160]:
            row = json.loads(line)
            parts.append(tok.apply_chat_template(row["messages"], tools=row["tools"], tokenize=False))
        CALIB.write_text("\n\n".join(parts), encoding="utf-8")
    if not IMATRIX.exists():
        mc._run([mc.find_llama_bin("llama-imatrix", LLAMA_DIR), "-m", str(F16), "-f", str(CALIB), "-o", str(IMATRIX),
                 "--process-output", "--parse-special", "-c", "512", "--chunks", "80", "-ngl", "99"])
    for q, args in QUANTS.items():
        out = RUN / f"{BASE}-ABn.{q}.gguf"
        if not out.exists():
            mc._run([mc.find_llama_bin("llama-quantize", LLAMA_DIR), *args, str(F16), str(out), "Q4_K_M"])
        print(out, round(out.stat().st_size / 2**20, 1), "MiB", flush=True)


# ------------------------------------------------------------------ chấm (GPU): bố cục B native cho mọi model
def ask_native(srv, case):
    try:
        ans = srv.ask(native_messages(case), tools=T.TOOLS, max_tokens=200, cache_prompt=True)
        s, calls, err = T.score_native(case, ans), ans.get("tool_calls") or [], None
        text, finish = ans.get("text", ""), ans.get("finish")
    except Exception as ex:  # noqa: BLE001  lỗi server: tính là sai
        s, calls, err, text, finish = pe_broken(), [], f"{type(ex).__name__}: {ex}"[:200], "", None
    return {"id": case["id"], "tool": case["tool"], "noisy": case["noisy"], "called": bool(calls),
            "tool_ok": bool(s["tool_ok"]), "ok": bool(s["ok"]), "pred": calls[0]["name"] if calls else None,
            "text": text[:160], "finish": finish, "error": err,
            "short_sentence": (not calls) and pe.one_short_sentence(text) if case["tool"] is None else None}


def pe_broken():
    return {"valid": False, "tool_ok": False, "args_ok": False, "over_trigger": False, "ok": False}


def summarize(rows):
    need = [r for r in rows if r["tool"] is not None]
    none = [r for r in rows if r["tool"] is None]
    m = pe.mean
    return {"n": len(rows), "2_tool_select": m(r["tool_ok"] for r in need), "3_end2end": m(r["ok"] for r in need),
            "1_over_trigger": m(r["called"] for r in none), "1_missed_call": m(not r["called"] for r in need),
            "5_short_sentence": m(r["short_sentence"] for r in none), "errors": sum(r["error"] is not None for r in rows),
            "clean_end2end": m(r["ok"] for r in need if not r["noisy"]), "noisy_end2end": m(r["ok"] for r in need if r["noisy"])}


def evaluate():
    train_keys = set()
    for line in open(TRAIN_FILE, encoding="utf-8"):
        u = json.loads(line)["messages"][1]["content"].split("\n", 1)[-1]
        train_keys.add(M.mask_numbers(M.normalize(u)))
    sets = {"ood": ood.make_cases(), "156": T.make_cases(), "held": json.load(open(HELD_FILE, encoding="utf-8"))}
    skip = {c["id"] for c in sets["ood"] if M.mask_numbers(M.normalize(c["context"]["user_prompt"])) in train_keys}
    sets["ood"] = [c for c in sets["ood"] if c["id"] not in skip]
    print(f"OOD: bỏ {len(skip)} ca trùng mẫu câu train_ABn -> {len(sets['ood'])} ca", flush=True)
    ref = next(c for c in st.all_configs() if "Qwen2.5-0.5B" in c["name"] and c["quant"] == "F16")
    models = {f"ABn {q}": dict(ref, file=RUN / f"{BASE}-ABn.{q}.gguf", label=f"ABn {q}") for q in QUANTS}
    for name, q in (("Qwen3-0.6B", "Q4_K_M-himx-o4"), ("Qwen2.5-0.5B-Instruct", "Q4_K_M-himx-o4")):
        models[f"{name} {q} (gốc)"] = {"name": name, "quant": q, "id": f"{name}__{q}", "file": pe.FILES[name][q],
                                        "label": f"{name}__{q}"}
    raw = Path("results/raw/abn"); raw.mkdir(parents=True, exist_ok=True)
    out = {"ood_excluded": sorted(skip), "table": {}}
    for label, cfg in models.items():
        res = {}
        with st.server(cfg, n_ctx=4096, gpu_layers=st.GPU_LAYERS) as srv:
            srv.ask(native_messages(sets["ood"][0]), tools=T.TOOLS, max_tokens=8, cache_prompt=True)   # làm nóng cache
            for sname, cases in sets.items():
                rows = [ask_native(srv, c) for c in cases]
                with open(raw / f"{re.sub(r'[^A-Za-z0-9.-]+', '_', label)}__{sname}.jsonl", "w", encoding="utf-8") as fh:
                    fh.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
                res[sname] = summarize(rows)
        out["table"][label] = res
        print(label, json.dumps({k: (v["2_tool_select"], v["3_end2end"], v["1_over_trigger"]) for k, v in res.items()}),
              flush=True)
    Path("results/abn_eval.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    for name, fn in (("data", data), ("train", train), ("gguf", gguf), ("eval", evaluate)):
        if step in (name, "all"):
            fn()
