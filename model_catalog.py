"""Catalog of the 15 benchmark configurations (base model x quantisation).

Naming used by the team  ->  GGUF type
    no label / "quant 16" -> F16  (no compression; the original precision)
    "quant 8"  -> Q8_0
    "quant 4"  -> Q4_K_M
Small models are deliberately left uncompressed.

Every GGUF is built from the SAME Hugging Face checkpoint with llama.cpp's
convert_hf_to_gguf.py (-> F16) and llama-quantize (-> Q8_0 / Q4_K_M).  Using
one source avoids the unfair comparison you get when each quant is downloaded
from a different uploader (different imatrix / llama.cpp version).

If you prefer to download ready-made files, fill PREBUILT below.
"""
from pathlib import Path
import shutil
import subprocess
import sys

CACHE_DIR = Path("./model_cache")

QUANT_TYPES = {"F16": 16, "Q8_0": 8, "Q4_K_M": 4}   # name -> nominal bits

BASE_MODELS = {
    "SmolLM2-135M-Instruct": dict(hf_repo="HuggingFaceTB/SmolLM2-135M-Instruct", params_b=0.135),
    "Qwen2.5-0.5B-Instruct": dict(hf_repo="Qwen/Qwen2.5-0.5B-Instruct", params_b=0.5),
    "Qwen3-0.6B": dict(hf_repo="Qwen/Qwen3-0.6B", params_b=0.6),
    "Gemma-3-1B-it": dict(hf_repo="google/gemma-3-1b-it", params_b=1.0, gated=True, full="bf16"),
    "Arch-Agent-1.5B": dict(hf_repo="katanemo/Arch-Agent-1.5B", params_b=1.5),
    "xLAM-2-1B-fc-r": dict(hf_repo="Salesforce/xLAM-2-1b-fc-r", params_b=1.0),
    "Qwen3-1.7B": dict(hf_repo="Qwen/Qwen3-1.7B", params_b=1.7),
    "Arch-Agent-3B": dict(hf_repo="katanemo/Arch-Agent-3B", params_b=3.0),
    "Phi-4-mini-instruct": dict(hf_repo="microsoft/Phi-4-mini-instruct", params_b=3.8),
}

# (base model, quant) in the order the team listed them.  EDIT HERE to add/remove runs.
CONFIGS = [
    # không nén (F16 = giữ nguyên độ chính xác gốc)
    ("SmolLM2-135M-Instruct", "F16"),
    ("Qwen2.5-0.5B-Instruct", "F16"),
    ("Qwen3-0.6B", "F16"),
    ("Gemma-3-1B-it", "F16"),            # thực chất BF16, xem ghi chú ở trên
    ("Arch-Agent-1.5B", "F16"),
    ("xLAM-2-1B-fc-r", "F16"),
    ("Qwen3-1.7B", "F16"),
    ("Arch-Agent-3B", "F16"),
    # nén nhẹ
    ("Arch-Agent-1.5B", "Q8_0"),
    ("xLAM-2-1B-fc-r", "Q8_0"),
    ("Qwen3-1.7B", "Q8_0"),
    ("Arch-Agent-3B", "Q8_0"),
    ("Phi-4-mini-instruct", "Q8_0"),
    # nén mạnh
    ("Arch-Agent-3B", "Q4_K_M"),
    ("Phi-4-mini-instruct", "Q4_K_M"),
]

# Optional: {(base, quant): (hf_repo, filename)} to download instead of building.
PREBUILT = {}


def config_id(base, quant):
    return f"{base}__{quant}"


def all_entries():
    """One dict per configuration, with the fields the benchmark code uses."""
    entries = []
    for base, quant in CONFIGS:
        meta = BASE_MODELS[base]
        entries.append({
            "id": config_id(base, quant), "name": base, "quant": quant,
            "bits": QUANT_TYPES[quant], **meta,
        })
    return entries


def gguf_path(base, quant, cache_dir=CACHE_DIR):
    return Path(cache_dir) / "gguf" / f"{base}.{quant}.gguf"


# --------------------------------------------------------------------------
# Building GGUF files (needs a llama.cpp checkout + its binaries)
# --------------------------------------------------------------------------
def find_llama_bin(name, llama_cpp_dir):
    """Locate llama.cpp binary `name` (e.g. 'llama-quantize') on Linux/Mac/Windows."""
    llama_cpp_dir = Path(llama_cpp_dir)
    candidates = [
        llama_cpp_dir / "build" / "bin" / name,
        llama_cpp_dir / "build" / "bin" / "Release" / f"{name}.exe",
        llama_cpp_dir / "build" / "bin" / f"{name}.exe",
        llama_cpp_dir / f"{name}.exe",
        llama_cpp_dir / name,
    ]
    for c in candidates:
        if c.is_file():
            return c
    found = shutil.which(name)
    if found:
        return Path(found)
    raise FileNotFoundError(f"{name} not found under {llama_cpp_dir} (build llama.cpp or download a release zip)")


def _run(cmd):
    """Chạy một lệnh; nếu lỗi thì hiện RÕ lý do ngay trong notebook (Jupyter không hiện chữ của chương trình con)."""
    print("[run]", " ".join(str(c) for c in cmd))
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        tail = "\n".join((p.stdout + "\n" + p.stderr).strip().splitlines()[-25:])
        raise RuntimeError(f"Lệnh thất bại (mã {p.returncode}). 25 dòng cuối:\n{tail}")


def ensure_gguf(base, quant, llama_cpp_dir="./llama.cpp", cache_dir=CACHE_DIR):
    """Return the path of the GGUF for (base, quant); build/download it if missing."""
    out = gguf_path(base, quant, cache_dir)
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)

    if (base, quant) in PREBUILT:
        from huggingface_hub import hf_hub_download
        repo, fname = PREBUILT[(base, quant)]
        p = Path(hf_hub_download(repo_id=repo, filename=fname, local_dir=str(out.parent)))
        p.replace(out)
        return out

    f16 = gguf_path(base, "F16", cache_dir)
    if not f16.is_file():
        from huggingface_hub import snapshot_download
        hf_dir = Path(cache_dir) / "hf" / base
        snapshot_download(
            repo_id=BASE_MODELS[base]["hf_repo"], local_dir=str(hf_dir),
            allow_patterns=["*.json", "*.safetensors", "*.model", "*.txt", "*.tiktoken", "*.jinja"],
        )
        convert = Path(llama_cpp_dir) / "convert_hf_to_gguf.py"
        _run([sys.executable, convert, hf_dir, "--outfile", f16, "--outtype", BASE_MODELS[base].get("full", "f16")])
    if quant == "F16":
        return f16
    _run([find_llama_bin("llama-quantize", llama_cpp_dir), f16, out, quant])
    return out


def gguf_size_mb(path):
    return round(Path(path).stat().st_size / (1024 * 1024), 1)
