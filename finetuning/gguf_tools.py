"""Đổi model Hugging Face sang GGUF và nén thành nhiều mức, dùng llama.cpp.

Quy trình:  thư mục HF  --convert_hf_to_gguf.py-->  F16  --llama-quantize-->  Q8_0 / Q5_K_M / Q4_K_M ...
Mức có hậu tố "-imat" (ví dụ "Q4_K_M-imat") được nén với ma trận tầm quan trọng (imatrix): llama.cpp chạy thử
model trên một đoạn văn bản mẫu để biết trọng số nào quan trọng, rồi bảo vệ chúng khi nén.
"""
import json
import sys
from pathlib import Path

import model_catalog as catalog


def write_calibration_text(train_jsonl, out_path, n=600):
    """Văn bản mẫu cho imatrix: câu nói + câu trả lời của n mẫu huấn luyện đầu tiên (đúng kiểu dữ liệu thật)."""
    with open(out_path, "w", encoding="utf-8") as out:
        for i, line in enumerate(open(train_jsonl, encoding="utf-8")):
            if i >= n:
                break
            messages = json.loads(line)["messages"]
            out.write(messages[1]["content"] + "\n" + messages[2]["content"] + "\n\n")
    return Path(out_path)


def export_gguf(hf_dir, out_dir, name, quants, llama_dir="./llama.cpp", calibration_text=None):
    """Tạo `{name}.F16.gguf` và một file cho mỗi mức trong `quants`. Bỏ qua file đã có.

    Trả về dict {mức: đường dẫn file}, luôn có khoá "F16".
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    quantize = catalog.find_llama_bin("llama-quantize", llama_dir)
    paths = {"F16": out_dir / f"{name}.F16.gguf"}

    if not paths["F16"].exists():
        catalog._run([sys.executable, Path(llama_dir) / "convert_hf_to_gguf.py", hf_dir,
                      "--outfile", paths["F16"], "--outtype", "f16"])

    imatrix = None
    for quant in quants:
        target = out_dir / f"{name}.{quant}.gguf"
        paths[quant] = target
        if target.exists():
            continue
        base_type, with_imatrix = quant.replace("-imat", ""), quant.endswith("-imat")
        command = [quantize]
        if with_imatrix:
            if calibration_text is None:
                raise ValueError(f"{quant} cần calibration_text (xem write_calibration_text)")
            imatrix = imatrix or _compute_imatrix(paths["F16"], calibration_text, out_dir / f"{name}.imatrix.dat", llama_dir)
            command += ["--imatrix", imatrix]
        catalog._run(command + [paths["F16"], target, base_type])
    return paths


def _compute_imatrix(f16_path, calibration_text, out_path, llama_dir):
    if not Path(out_path).exists():
        catalog._run([catalog.find_llama_bin("llama-imatrix", llama_dir), "-m", f16_path, "-f", calibration_text,
                      "-o", out_path, "-ngl", "0"])
    return out_path
