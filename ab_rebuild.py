"""Dựng lại model fine-tune AB (finetuning/qwen_AB_hybrid_recipe.ipynb, cell 1-3-5-7) thành file GGUF.

Giữ NGUYÊN công thức của notebook: cùng dữ liệu A (make_train, seed 7), dữ liệu B (teacher Phi-4-mini Q8_0),
cùng cách trộn/shuffle (seed 42), cùng LORA_CFG, cùng file calibration và 4 biến thể quant.
Chỉ khác: base đọc từ thư mục HF cục bộ (hub offline), convert_hf_to_gguf.py lấy từ mã nguồn llama.cpp
đúng bản b12223 (C:/model_cache/llama_src). Dữ liệu B do teacher sinh ngẫu nhiên nên model sẽ KHÔNG giống
từng bit với bản của Benedict; ghi rõ trong báo cáo.

Chạy: SLM_DEVICE=gpu python ab_rebuild.py   -> runs/qwen05_AB/Qwen2.5-0.5B-Instruct-AB.<quant>.gguf
"""
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path

os.environ.setdefault("SLM_DEVICE", "gpu")
sys.path.insert(0, str(Path(__file__).parent / "finetuning"))
import eval_tools as E  # noqa: E402
import lora_train  # noqa: E402
import make_train  # noqa: E402
import model_catalog as mc  # noqa: E402
import slm_tools as st  # noqa: E402
import teacher_data as D  # noqa: E402
import tool_cases as T  # noqa: E402

BASE_NAME = "Qwen2.5-0.5B-Instruct"
BASE_DIR = Path("C:/model_cache/hf") / BASE_NAME
CONVERT = Path("C:/model_cache/llama_src/convert_hf_to_gguf.py")
LLAMA_DIR = Path("./llama.cpp")
RUN_DIR = Path("runs/qwen05_AB")
HYBRID_TRAIN_FILE = RUN_DIR / "train_AB.jsonl"
CALIB_FILE = RUN_DIR / "calib_AB.txt"
IMATRIX_FILE = RUN_DIR / "imatrix_AB.gguf"
MERGED_DIR = RUN_DIR / "merged"
HELD_FILE = Path("runs/qwen05_A/heldout_v2.json")
TRAIN_A_FILE = Path("runs/qwen05_A/train_v2.jsonl")
TRAIN_B_FILE = Path("runs/qwen05_B/train_teacher.jsonl")
LORA_CFG = dict(r=32, alpha=64, dropout=0.05, lr=1.5e-4, epochs=1, bs=1, accum=16)
QUANT_SPECS = [
    {"id": "Q4_K_M", "args": []},
    {"id": "Q4_K_M-imx", "args": ["--imatrix", str(IMATRIX_FILE)]},
    {"id": "Q4_K_M-o4", "args": ["--output-tensor-type", "q4_0"]},
    {"id": "Q4_K_M-himx-o4", "args": ["--imatrix", str(IMATRIX_FILE), "--output-tensor-type", "q4_0"]},
]


def data():
    """Cell 3: dữ liệu A, B và tập lai."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    test_cases = T.make_cases()
    if not TRAIN_A_FILE.exists() or not HELD_FILE.exists():
        train_a, held_a = make_train.build_dataset()
        TRAIN_A_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TRAIN_A_FILE, "w", encoding="utf-8") as f:
            for row in make_train.to_chat_rows(train_a):
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        json.dump(held_a, open(HELD_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    held_cases = E.heldout(HELD_FILE)
    if not TRAIN_B_FILE.exists():
        raw = TRAIN_B_FILE.parent / "teacher_queries.jsonl"
        TRAIN_B_FILE.parent.mkdir(parents=True, exist_ok=True)
        blocked = {make_train.normalize(c["context"]["user_prompt"]) for c in (test_cases + held_cases)}
        have = Counter(a["tool"] for a in D.load_accepted(raw))
        if any(have[t] < (260 if t is None else 130) for t in D.ALL_TOOLS + [None]):
            teacher_cfg = E.find_cfg("Phi-4-mini", "Q8_0")
            with st.server(teacher_cfg, n_ctx=2048, gpu_layers=st.GPU_LAYERS) as t_srv:
                D.generate(D.make_chat(t_srv.url), raw, 130, 260, blocked)
        with open(TRAIN_B_FILE, "w", encoding="utf-8") as f:
            for row in D.build_training_rows(D.to_cases(D.load_accepted(raw))):
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    forbidden = {c["context"]["user_prompt"].strip().lower() for c in (test_cases + held_cases)}
    rows_a = [json.loads(x) for x in open(TRAIN_A_FILE, encoding="utf-8") if x.strip()]
    rows_b = [json.loads(x) for x in open(TRAIN_B_FILE, encoding="utf-8") if x.strip()]
    sys11 = rows_a[0]["messages"][0]["content"]
    hybrid, ka, kb = [], 0, 0
    for r in rows_a:
        if json.loads(r["messages"][1]["content"])["user_prompt"].strip().lower() not in forbidden:
            hybrid.append({"messages": r["messages"]})
            ka += 1
    for r in rows_b:
        if json.loads(r["messages"][1]["content"])["user_prompt"].strip().lower() not in forbidden:
            hybrid.append({"messages": [{"role": "system", "content": sys11}, r["messages"][1], r["messages"][2]]})
            kb += 1
    random.Random(42).shuffle(hybrid)
    with open(HYBRID_TRAIN_FILE, "w", encoding="utf-8") as f:
        for r in hybrid:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"train_AB: {len(hybrid)} mẫu (A {ka}, B {kb})", flush=True)
    return hybrid


def train():
    """Cell 5."""
    if not (MERGED_DIR / "config.json").exists():
        hist = lora_train.train_lora(str(BASE_DIR), HYBRID_TRAIN_FILE, MERGED_DIR, **LORA_CFG)
        json.dump(hist, open(RUN_DIR / "train_history.json", "w", encoding="utf-8"), indent=2)


def to_gguf(hybrid):
    """Cell 7: F16 -> imatrix (calib = wiki 100k + 160 mẫu train đầu) -> 4 biến thể quant."""
    f16 = RUN_DIR / f"{BASE_NAME}-AB.F16.gguf"
    if not f16.exists():
        mc._run([sys.executable, CONVERT, MERGED_DIR, "--outfile", f16, "--outtype", "f16"])
    if not CALIB_FILE.exists():
        parts = [Path("wiki.test.raw").read_text(encoding="utf-8")[:100_000]] if Path("wiki.test.raw").exists() else []
        for r in hybrid[:160]:
            m = r["messages"]
            parts.append(f"<|im_start|>system\n{m[0]['content']}<|im_end|>\n<|im_start|>user\n{m[1]['content']}<|im_end|>\n"
                         f"<|im_start|>assistant\n{m[2]['content']}<|im_end|>\n")
        CALIB_FILE.write_text("\n\n".join(parts), encoding="utf-8")
    if not IMATRIX_FILE.exists():
        mc._run([mc.find_llama_bin("llama-imatrix", LLAMA_DIR), "-m", str(f16), "-f", str(CALIB_FILE), "-o", str(IMATRIX_FILE),
                 "--process-output", "--parse-special", "-c", "512", "--chunks", "80", "-ngl", "99"])
    for s in QUANT_SPECS:
        out = RUN_DIR / f"{BASE_NAME}-AB.{s['id']}.gguf"
        if not out.exists():
            mc._run([mc.find_llama_bin("llama-quantize", LLAMA_DIR), *s["args"], str(f16), str(out), "Q4_K_M"])
        print(out, round(out.stat().st_size / 2**20, 1), "MiB", flush=True)


if __name__ == "__main__":
    h = data()
    train()
    to_gguf(h)
