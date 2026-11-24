"""File calibration "lai" cho llama-imatrix, cùng công thức với notebook 09 (WikiText + ChatML tool-call + log nhà máy),
NHƯNG không rò dữ liệu test:
  - Notebook 09 dùng tool_cases.make_cases() (seed=42) kèm ĐÁP ÁN ĐÚNG -> trùng 100% với 156 ca dùng để chấm.
  - Ở đây dùng seed khác (CALIB_SEED) VÀ bỏ mọi ca có câu user trùng nguyên văn với bộ test (đổi seed vẫn trùng ~1/2
    vì mẫu câu hẹp). Mẫu câu (template) vẫn cùng họ, nên chưa hoàn toàn độc lập; ghi rõ điều này trong báo cáo.
  - Phần log nhà máy (faith_cases) giữ như notebook 09 (không ảnh hưởng bài chấm tool calling).
Chat template: Qwen2.5 và Qwen3 đều dùng ChatML + <tool_call>, nên một file dùng chung cho cả 2 model.

Chạy: python results_pi/scripts/build_calib_heldout.py <file_ra>
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import faith_cases as F  # noqa: E402
import tool_cases as T  # noqa: E402

CALIB_SEED = 2026          # KHÁC seed 42 của bộ test
TEST_SEED = 42


def main(out_path):
    test_prompts = {c["context"]["user_prompt"] for c in T.make_cases(seed=TEST_SEED)}
    cases = T.make_cases(seed=CALIB_SEED)
    sections = [(ROOT / "wiki.test.raw").read_text(encoding="utf-8")[:120_000]]
    chatml, overlap = [], 0
    for idx, c in enumerate(cases):
        if c["context"]["user_prompt"] in test_prompts:   # đổi seed vẫn trùng ~1/2 vì mẫu câu hẹp -> bỏ hẳn
            overlap += 1
            continue
        n_tools = [3, 6, 11][idx % 3] if c["core"] else 11
        msgs, tools = T.native_request(c, T.TOOL_SETS[n_tools])
        tools_json = "\n".join(json.dumps(t, ensure_ascii=False) for t in tools)
        sys_prompt = (f"{msgs[0]['content']}\n\n# Tools\n\nYou may call one or more functions to assist with the user query.\n"
                      f"You are provided with function signatures within <tools></tools> XML tags:\n<tools>\n{tools_json}\n</tools>\n\n"
                      "For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:\n"
                      "<tool_call>\n{\"name\": <function-name>, \"arguments\": <args-json-object>}\n</tool_call>")
        if c["tool"] is not None:
            args = dict(c["args"])
            if c["tool"] in ("request_line_restart", "request_line_stop") and "reason" not in args:
                args["reason"] = "operator requested via voice command"
            reply = "<tool_call>\n" + json.dumps({"name": c["tool"], "arguments": args}, ensure_ascii=False) + "\n</tool_call>"
        else:
            reply = "I cannot fulfill that request because it is outside the scope of conveyor line supervision."
        chatml.append(f"<|im_start|>system\n{sys_prompt}<|im_end|>\n<|im_start|>user\n{msgs[1]['content']}<|im_end|>\n"
                      f"<|im_start|>assistant\n{reply}<|im_end|>\n")
    sections.append("\n".join(chatml))
    faith = []
    for fc in F.make_cases():
        ans = f"According to the log, the value is {fc['truth']}." if fc["truth"] is not None else "NOT IN LOG"
        faith.append(f"<|im_start|>system\n{fc['messages'][0]['content']}<|im_end|>\n"
                     f"<|im_start|>user\n{fc['messages'][1]['content']}<|im_end|>\n<|im_start|>assistant\n{ans}<|im_end|>\n")
    sections.append("\n".join(faith))
    Path(out_path).write_text("\n\n".join(sections), encoding="utf-8")
    print(f"{out_path}: {Path(out_path).stat().st_size / 1024:.0f} KB, giữ {len(chatml)}/{len(cases)} ca tool (seed {CALIB_SEED}), "
          f"đã bỏ {overlap} ca có câu user trùng nguyên văn với bộ test seed {TEST_SEED}")


if __name__ == "__main__":
    main(sys.argv[1])
