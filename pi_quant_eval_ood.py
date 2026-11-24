"""Chấm các bản quant trên bộ test viết tay tool_cases_ood (ngoài phân phối mẫu câu của tool_cases).

Cùng server, cùng định dạng native, cùng hàm chấm eval_native/summarize của pi_quant_eval (chỉ phần native,
không có phần JSON tự viết). Chạy: python pi_quant_eval_ood.py [lọc_id]
Kết quả: results/pi_quant_eval_ood.json, từng ca ở results/raw/pi_quant_ood/<id>.jsonl
"""
import json
import sys
import time
from pathlib import Path

import pi_quant_eval as pe
import slm_tools
import tool_cases_ood as ood

OUT = Path("results/pi_quant_eval_ood.json")
RAW = Path("results/raw/pi_quant_ood")
TARGETS = {
    "Qwen3-0.6B": ["Q4_K_M", "Q4_K_M-o4", "Q4_K_M-himx", "Q4_K_M-himx-o4", "IQ4_XS-himx", "IQ4_NL-himx", "Q4_0-imx-o4"],
    "Qwen2.5-0.5B-Instruct": ["Q4_K_M", "Q4_K_M-o4", "Q4_K_M-himx", "Q4_K_M-himx-o4", "IQ4_NL-himx", "IQ4_XS-himx"],
}


def summarize(rows):
    need = [r for r in rows if r["tool"] is not None]
    none = [r for r in rows if r["tool"] is None]
    picked = [r for r in need if r["tool_ok"]]
    m = pe.mean
    return {"n": len(rows), "1_intent": m(r["intent_ok"] for r in rows), "1_over_trigger": m(r["called"] for r in none),
            "1_missed_call": m(not r["called"] for r in need), "2_tool_select": m(r["tool_ok"] for r in need),
            "3_args_cond": m(r["args_ok"] for r in picked), "3_end2end": m(r["ok"] for r in need),
            "4_json_native_schema": m(r.get("schema_ok") for r in rows if r["called"]),
            "clean_end2end": m(r["ok"] for r in need if not r["noisy"]), "noisy_end2end": m(r["ok"] for r in need if r["noisy"])}


def main(only=None):
    cases = ood.make_cases()
    RAW.mkdir(parents=True, exist_ok=True)
    res = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    for name, quants in TARGETS.items():
        for q in quants:
            cid = f"{name}__{q}"
            if only and only not in cid:
                continue
            f = pe.FILES[name][q]
            cfg = {"name": name, "quant": q, "id": cid, "file": f, "label": cid}
            t0 = time.time()
            with slm_tools.server(cfg, n_ctx=4096) as srv:
                rows = [{"id": c["id"], "tool": c["tool"], "noisy": c["noisy"], **pe.eval_native(srv, c)} for c in cases]
            with open(RAW / f"{cid}.jsonl", "w", encoding="utf-8") as fh:
                fh.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
            res[cid] = {**summarize(rows), "minutes": round((time.time() - t0) / 60, 1)}
            OUT.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
            print(cid, json.dumps(res[cid]), flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
