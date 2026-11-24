"""Chấm model fine-tune AB trên bộ câu viết tay tool_cases_ood (GPU PC, chỉ để xem độ chính xác).

Cùng THƯỚC với pi_quant_eval_ood (tool_select / end2end chỉ tính trên ca cần tool, over_trigger trên ca ngoài phạm vi).
AB chạy đúng định dạng nó được train: JSON tự do + GBNF grammar, prompt messages_cached (11 tool, phần cố định ở đầu).
Khác eval_tools.run: câu lỗi server KHÔNG bị bỏ, mà tính là sai (ghi số lỗi).
Bỏ các câu OOD trùng mẫu câu train_AB (che số, chuẩn hoá như make_train.normalize); bảng so sánh dùng chung tập còn lại.

Chạy: python ab_eval_ood.py   -> results/ab_eval_ood.json, từng ca ở results/raw/ab_ood/<quant>.jsonl
"""
import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("SLM_DEVICE", "gpu")
sys.path.insert(0, str(Path(__file__).parent / "finetuning"))
import eval_tools as E  # noqa: E402
import make_train  # noqa: E402
import slm_tools as st  # noqa: E402
import tool_cases as T  # noqa: E402
import tool_cases_ood as ood  # noqa: E402

RUN_DIR = Path("runs/qwen05_AB")
QUANTS = ["Q4_K_M", "Q4_K_M-o4", "Q4_K_M-himx-o4"]
BASELINES = ["Qwen3-0.6B__Q4_K_M-himx-o4", "Qwen2.5-0.5B-Instruct__Q4_K_M-himx-o4"]   # native, đã chấm trên Pi-quant OOD
OUT = Path("results/ab_eval_ood.json")
RAW = Path("results/raw/ab_ood")


def mask(text):
    return re.sub(r"\d+", "#", make_train.normalize(text))


def overlap_ids(cases):
    train = {mask(json.loads(json.loads(x)["messages"][1]["content"])["user_prompt"])
             for x in open(RUN_DIR / "train_AB.jsonl", encoding="utf-8") if x.strip()}
    return {c["id"] for c in cases if mask(c["context"]["user_prompt"]) in train}


def mean(xs):
    xs = [bool(x) for x in xs]
    return round(sum(xs) / len(xs), 4) if xs else None


def summarize(rows):
    need = [r for r in rows if r["tool"] is not None]
    none = [r for r in rows if r["tool"] is None]
    return {"n": len(rows), "2_tool_select": mean(r["tool_ok"] for r in need), "3_end2end": mean(r["ok"] for r in need),
            "1_over_trigger": mean(r["called"] for r in none), "1_missed_call": mean(not r["called"] for r in need),
            "clean_end2end": mean(r["ok"] for r in need if not r["noisy"]),
            "noisy_end2end": mean(r["ok"] for r in need if r["noisy"])}


def eval_ab(srv, case):
    r = {"id": case["id"], "tool": case["tool"], "noisy": case["noisy"], "error": None}
    try:
        out = srv.ask(make_train.messages_cached(case, E.NAMES), grammar=E.schema_v2(), cache_prompt=True)
        r["text"], r["tokens"] = out["text"], out.get("tokens")
        s = T.score(case, E.normalize(out["text"]))
        pred = E.predicted_tool(out["text"])
    except Exception as ex:  # noqa: BLE001  lỗi server hoặc JSON hỏng: tính là sai
        r["error"], s, pred = f"{type(ex).__name__}: {ex}"[:200], E.BROKEN, None
    r.update(pred=pred, called=pred not in (None, "JSON_HONG"), tool_ok=bool(s["tool_ok"]), ok=bool(s["ok"]))
    return r


def main():
    cases = ood.make_cases()
    skip = overlap_ids(cases)
    keep = [c for c in cases if c["id"] not in skip]
    print(f"OOD: {len(cases)} ca, bỏ {len(skip)} ca trùng mẫu train_AB -> {len(keep)} ca", flush=True)
    RAW.mkdir(parents=True, exist_ok=True)
    base_cfg = E.find_cfg("Qwen2.5-0.5B", "F16")
    res = {"excluded": sorted(skip), "n_kept": len(keep), "table": {}}
    for q in QUANTS:
        cfg = E.ft_cfg(base_cfg, RUN_DIR / f"Qwen2.5-0.5B-Instruct-AB.{q}.gguf", f"AB {q}")
        with st.server(cfg, gpu_layers=st.GPU_LAYERS) as srv:
            srv.ask(make_train.messages_cached(keep[0], E.NAMES), grammar=E.schema_v2())   # làm nóng cache
            rows = [eval_ab(srv, c) for c in cases]
        with open(RAW / f"{q}.jsonl", "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        kept = [r for r in rows if r["id"] not in skip]
        tok = [r["tokens"] for r in kept if r.get("tokens")]
        res["table"][f"AB {q}"] = {**summarize(kept), "errors": sum(r["error"] is not None for r in kept),
                                   "mean_out_tokens": round(sum(tok) / len(tok), 1) if tok else None,
                                   "all104": summarize(rows)}
        print(f"AB {q}", json.dumps(res["table"][f"AB {q}"], ensure_ascii=False), flush=True)
    for b in BASELINES:   # cùng tập ca còn lại, chấm lại từ file thô native
        p = Path("results/raw/pi_quant_ood") / f"{b}.jsonl"
        if p.exists():
            rows = [json.loads(x) for x in open(p, encoding="utf-8")]
            res["table"][f"{b} (native)"] = summarize([r for r in rows if r["id"] not in skip])
    OUT.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    for k, v in res["table"].items():
        print(f"{k:45s} tool_select {v['2_tool_select']}  end2end {v['3_end2end']}  over {v['1_over_trigger']}")


if __name__ == "__main__":
    main()
