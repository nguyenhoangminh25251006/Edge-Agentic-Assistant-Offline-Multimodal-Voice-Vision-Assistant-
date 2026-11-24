"""Kiểm tra độ chính xác của các bản quant chạy trên Pi: Q4_K_M (đối chứng) vs Q4_0-std vs Q4_0-o4.

5 nhóm tiêu chí, chấm trên 156 ca của tool_cases.make_cases() (11 tool, nửa số ca có nhiễu giọng nói):
  1. intent       : quyết định ĐÚNG có cần gọi tool hay không (ca ngoài phạm vi -> không gọi; ca cần tool -> gọi)
  2. tool_select  : trong các ca cần tool, chọn đúng tên tool
  3. args         : trong các ca đã chọn đúng tool, điền đúng tham số (args_cond); và đúng cả tool + tham số (end2end)
  4. json         : (a) tool-call native: arguments là JSON object hợp lệ VÀ khớp schema (đủ trường bắt buộc,
                        giá trị enum hợp lệ, đúng kiểu); (b) định dạng JSON tự do (SYSTEM_PROMPT cũ, KHÔNG grammar):
                        json.loads được và có đủ khóa action_type / tool_call / response_text
  5. instruction  : (a) ca không cần tool: trả lời đúng "one short sentence" (có chữ, 1 câu, <= 25 từ);
                    (b) không bịa tool ngoài danh sách; (c) định dạng JSON tự do: action_type thuộc 2 giá trị cho phép
                        và không có khóa thừa

Chế độ model: Qwen3 chạy với thinking TẮT (enable_thinking=False + /no_think, như slm_tools.Server.ask),
vì đó là chế độ triển khai trên Pi.

Chạy: python pi_quant_eval.py            (GPU, -ngl 99 như các notebook 03/04/05/08; SLM_DEVICE=cpu để chạy CPU)
Kết quả: results/pi_quant_eval.json, từng ca ở results/raw/pi_quant/<id>.jsonl
"""
import json
import os
import re
import sys
import time
from pathlib import Path

os.environ.setdefault("SLM_DEVICE", "gpu")
import slm_tools  # noqa: E402
import tool_cases as tc  # noqa: E402

MC = Path("C:/model_cache")
FILES = {
    "SmolLM2-135M-Instruct": {"Q4_K_M": MC / "gguf/SmolLM2-135M-Instruct.Q4_K_M.gguf"},
    "Qwen2.5-0.5B-Instruct": {"Q4_K_M": MC / "gguf_pi/Qwen2.5-0.5B-Instruct.Q4_K_M.gguf"},
    "Qwen3-0.6B": {"Q4_K_M": MC / "gguf_pi/Qwen3-0.6B.Q4_K_M.gguf"},
}
for name in FILES:
    for q in ("Q4_0-std", "Q4_0-o4"):
        FILES[name][q] = MC / f"gguf_pi/{name}.{q}.gguf"
# Đợt 2: giữ độ chính xác. Q4_0-imx = Q4_0 + imatrix (wiki.test.raw, 100 chunk x 512);
# Q4_K_M-o4 = Q4_K_M nhưng token_embd/output ép về q4_0. SmolLM2 không gọi được tool nên bỏ qua.
for name in ("Qwen2.5-0.5B-Instruct", "Qwen3-0.6B"):
    for q in ("Q4_0-imx", "Q4_K_M-o4"):
        FILES[name][q] = MC / f"gguf_pi/{name}.{q}.gguf"
# Đợt 3: Q4_0-imx-o4 = Q4_0 + imatrix + token_embd/output q4_0 (chỉ Qwen3)
FILES["Qwen3-0.6B"]["Q4_0-imx-o4"] = MC / "gguf_pi/Qwen3-0.6B.Q4_0-imx-o4.gguf"
# Đợt 4: ứng viên từ notebook 09 (IQ4_NL / IQ4_XS / Q4_K_M + imatrix lai), imatrix "-himx" dựng lại KHÔNG rò bộ test
# (results_pi/scripts/build_calib_heldout.py: seed 2026, bỏ mọi câu trùng nguyên văn với 156 ca seed 42).
for name in ("Qwen2.5-0.5B-Instruct", "Qwen3-0.6B"):
    for q in ("Q4_K_M-himx", "Q4_K_M-himx-o4", "IQ4_NL-himx", "IQ4_XS-himx"):
        FILES[name][q] = MC / f"gguf_pi/{name}.{q}.gguf"

OUT = Path("results/pi_quant_eval.json")
RAW = Path("results/raw/pi_quant")
SCHEMA = {t["function"]["name"]: t["function"]["parameters"] for t in tc.TOOLS}
_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool}


def schema_ok(name, args):
    """arguments là dict, đủ trường bắt buộc, không có trường lạ, đúng kiểu và enum."""
    if name not in SCHEMA or not isinstance(args, dict):
        return False
    props, req = SCHEMA[name]["properties"], SCHEMA[name]["required"]
    if any(k not in args for k in req) or any(k not in props for k in args):
        return False
    for k, v in args.items():
        p = props[k]
        t = _TYPES.get(p.get("type"))
        if t and (not isinstance(v, t) or (p.get("type") == "integer" and isinstance(v, bool))):
            return False
        if "enum" in p and v not in p["enum"]:
            return False
    return True


def one_short_sentence(text):
    t = (text or "").strip()
    if not t or len(t.split()) > 25:
        return False
    return len([s for s in re.split(r"(?<=[.!?])\s+", t) if s.strip()]) <= 1


def eval_native(srv, case):
    msgs, tools = tc.native_request(case, tc.NAMES)
    ans = srv.ask(msgs, tools=tools, max_tokens=200, cache_prompt=True)
    s = tc.score_native(case, ans)
    calls = ans.get("tool_calls") or []
    called = bool(calls)
    r = {"intent_ok": called == (case["tool"] is not None), "called": called,
         "tool_ok": s["tool_ok"], "args_ok": s["args_ok"], "ok": s["ok"],
         "invented_tool": any(c["name"] not in tc.NAMES for c in calls), "finish": ans.get("finish"),
         "text": ans.get("text", "")[:200], "calls": calls}
    if called:
        try:
            a = calls[0]["arguments"]
            a = json.loads(a) if isinstance(a, str) and a.strip() else (a or {})
            r["json_valid"] = isinstance(a, dict)
            r["schema_ok"] = schema_ok(calls[0]["name"], a)
        except (ValueError, TypeError):
            r["json_valid"] = r["schema_ok"] = False
    if case["tool"] is None:
        r["short_sentence"] = (not called) and one_short_sentence(ans.get("text"))
    return r


def eval_freeform(srv, case):
    """Định dạng JSON tự do (SYSTEM_PROMPT), KHÔNG grammar: model phải tự viết JSON đúng khuôn."""
    ans = srv.ask(tc.messages(case, tc.NAMES), max_tokens=200, cache_prompt=True)
    raw = ans.get("text", "").strip()
    m = re.search(r"\{.*\}", raw, re.S)          # chấp nhận bọc trong ```json
    out = {"ff_parse": False, "ff_keys": False, "ff_format": False, "ff_ok": False, "ff_raw": raw[:300]}
    try:
        obj = json.loads(m.group(0) if m else raw)
    except (ValueError, TypeError):
        return out
    out["ff_parse"] = isinstance(obj, dict)
    if not out["ff_parse"]:
        return out
    out["ff_keys"] = {"action_type", "tool_call", "response_text"} <= set(obj)
    out["ff_format"] = set(obj) == {"action_type", "tool_call", "response_text"} and \
        obj.get("action_type") in ("DIRECT_ANSWER", "TOOL_REQUEST")
    out["ff_ok"] = tc.score(case, json.dumps(obj))["ok"]
    return out


def mean(xs):
    xs = [bool(x) for x in xs]
    return round(sum(xs) / len(xs), 4) if xs else None


def summarize(rows):
    need = [r for r in rows if r["tool"] is not None]
    none = [r for r in rows if r["tool"] is None]
    picked = [r for r in need if r["tool_ok"]]
    called = [r for r in rows if r["called"]]
    return {
        "n": len(rows),
        "1_intent": mean(r["intent_ok"] for r in rows),
        "1_over_trigger": mean(r["called"] for r in none),
        "1_missed_call": mean(not r["called"] for r in need),
        "2_tool_select": mean(r["tool_ok"] for r in need),
        "3_args_cond": mean(r["args_ok"] for r in picked),
        "3_end2end": mean(r["ok"] for r in need),
        "4_json_native_valid": mean(r.get("json_valid") for r in called),
        "4_json_native_schema": mean(r.get("schema_ok") for r in called),
        "4_json_free_parse": mean(r["ff_parse"] for r in rows),
        "4_json_free_keys": mean(r["ff_keys"] for r in rows),
        "5_short_sentence": mean(r.get("short_sentence") for r in none),
        "5_no_invented_tool": mean(not r["invented_tool"] for r in rows),
        "5_free_format": mean(r["ff_format"] for r in rows),
        "free_end2end": mean(r["ff_ok"] for r in rows),
        "clean_end2end": mean(r["ok"] for r in need if not r["noisy"]),
        "noisy_end2end": mean(r["ok"] for r in need if r["noisy"]),
    }


def main(only=None):
    cases = tc.make_cases()
    RAW.mkdir(parents=True, exist_ok=True)
    results = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    for name, quants in FILES.items():
        for quant, f in quants.items():
            cid = f"{name}__{quant}"
            if only and only not in cid:
                continue
            if not f.exists():
                print("MISSING", f)
                continue
            cfg = {"name": name, "quant": quant, "id": cid, "file": f, "label": cid}
            t0, rows = time.time(), []
            with slm_tools.server(cfg, n_ctx=4096) as srv:
                for i, case in enumerate(cases):
                    r = {"id": case["id"], "tool": case["tool"], "noisy": case["noisy"], **eval_native(srv, case)}
                    rows.append(r)
                    if i % 40 == 39:
                        print(f"  {cid}: {i + 1}/{len(cases)}", flush=True)
                for r, case in zip(rows, cases):
                    r.update(eval_freeform(srv, case))
                hangs = srv.hangs
            with open(RAW / f"{cid}.jsonl", "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            results[cid] = {**summarize(rows), "file_mib": round(f.stat().st_size / 2**20, 1), "hangs": hangs,
                            "minutes": round((time.time() - t0) / 60, 1)}
            OUT.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
            print(cid, json.dumps(results[cid]), flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
