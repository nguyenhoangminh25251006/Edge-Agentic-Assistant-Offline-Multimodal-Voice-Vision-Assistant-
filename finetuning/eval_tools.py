r"""Đo model gọi công cụ: độ chính xác, bộ nhớ, tốc độ.

Cách đo (giống lúc chạy thật):
  * prompt đặt phần cố định lên đầu để llama.cpp dùng lại cache,
  * đầu ra bị ép theo JSON schema (tên công cụ và giá trị tham số chỉ được nằm trong danh sách cho phép),
  * nhãn action_type suy ra bằng code từ tool_call, vì model nhỏ hay ghi sai nhãn này,
  * câu mà model trả JSON hỏng hoặc bị cắt vẫn được tính là sai (không bị bỏ).

Ba bộ câu hỏi:  test156() là bộ gốc của dự án, heldout() là bộ giữ riêng chưa từng thấy khi huấn luyện,
load_real() là câu thật do nhóm ghi lại (nếu có).
"""
import csv
import json
import time
from collections import Counter
from pathlib import Path

import slm_tools
import tool_cases as T
from make_train import messages_cached

NAMES = T.TOOL_SETS[11]


# ---------------------------------------------------------------- Chọn model và bộ câu hỏi
def find_cfg(name_part, quant):
    """Lấy cấu hình model từ catalog, ví dụ find_cfg('Qwen2.5-0.5B', 'F16')."""
    return next(c for c in slm_tools.all_configs() if name_part in c["name"] and c["quant"] == quant)


def ft_cfg(base_cfg, gguf_path, label):
    """Cấu hình cho một file GGUF mới (model đã fine-tune), dựa trên cấu hình của model gốc."""
    cfg = dict(base_cfg)
    cfg["file"], cfg["label"] = gguf_path, label
    return cfg


def test156():
    return T.make_cases()


def heldout(path="heldout_v2.json"):
    return json.load(open(path, encoding="utf-8"))


REAL_HEADER = ["text", "tool", "args"]
REAL_EXAMPLES = [["could you slow the belt down to twenty five rpm", "request_set_speed", '{"target_speed_rpm": 25}'],
                 ["what is the weather going to be like", "", ""],
                 ["why did it stop just now", "get_last_stop_reason", ""]]


def write_real_template(path="real_utterances.csv"):
    """Tạo file mẫu để nhóm điền câu thật. Cột tool để trống nếu câu không liên quan đến băng tải."""
    if not Path(path).exists():
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([REAL_HEADER] + REAL_EXAMPLES)
    print("Mở", path, "và thay các dòng ví dụ bằng câu thật (mỗi dòng: câu nói, tên tool, tham số JSON).")


def load_real(path="real_utterances.csv"):
    """Đọc câu thật. Mỗi dòng: text, tool (rỗng nếu ngoài phạm vi), args (JSON, rỗng nếu không có tham số)."""
    cases = []
    for i, row in enumerate(csv.DictReader(open(path, encoding="utf-8"))):
        tool = row["tool"].strip() or None
        if tool is not None and tool not in NAMES:
            raise ValueError(f"Dòng {i+2}: tool '{tool}' không có trong danh sách 11 công cụ")
        speed, current, temp = T._STATES["RUNNING"]
        cases.append({"id": f"real-{i}", "tool": tool, "args": json.loads(row["args"]) if row["args"].strip() else {},
                      "noisy": True, "core": tool is None,
                      "context": {"fsm_state": "RUNNING",
                                  "telemetry": {"belt_speed_rpm": speed, "motor_current_ma": current, "motor_temp_c": temp},
                                  "user_prompt": row["text"].strip()}})
    return cases


# ---------------------------------------------------------------- Schema ép đầu ra
def schema_v2(names=NAMES):
    """GBNF grammar ép đầu ra: hoặc trả lời trực tiếp (tool_call = null), hoặc gọi đúng một công cụ trong danh sách.
    Nhãn action_type để tự do vì sẽ được sửa bằng code trong normalize()."""
    return T.grammar_for(names)


def normalize(text):
    """Sửa action_type cho khớp với tool_call: có tool_call thì là TOOL_REQUEST, không thì DIRECT_ANSWER."""
    answer = json.loads(text)
    answer["action_type"] = "TOOL_REQUEST" if answer.get("tool_call") else "DIRECT_ANSWER"
    return json.dumps(answer)


def predicted_tool(text):
    try:
        call = json.loads(text).get("tool_call")
        return (call or {}).get("tool_name")
    except Exception:
        return "JSON_HONG"


# ---------------------------------------------------------------- Chạy và tổng hợp
BROKEN = {"valid": False, "tool_ok": False, "args_ok": False, "over_trigger": False, "ok": False}


def run(srv, cases, cache=True, tools_for=None):
    """Hỏi model từng câu, trả về danh sách (ca, đầu ra thô, điểm).
    tools_for: hàm ca -> danh sách tên công cụ đưa vào prompt (mặc định: cả 11, không dùng bộ chọn công cụ)."""
    rows, broken = [], 0
    for i, case in enumerate(cases):
        names = tools_for(case) if tools_for else NAMES
        try:
            out = srv.ask(messages_cached(case, names), grammar=schema_v2(names), cache_prompt=cache)
        except Exception as error:                      # lỗi server hoặc mạng: bỏ qua câu này
            print(f"LỖI server ở câu {i} ({case['tool']}): {error}")
            continue
        try:
            score = T.score(case, normalize(out["text"]))
        except Exception:                               # JSON hỏng hoặc bị cắt: tính là sai
            broken += 1
            score = BROKEN
        rows.append((case, out, score))
    if broken:
        print(f"{broken} câu model trả JSON hỏng hoặc bị cắt (đã tính là sai)")
    return rows


def summary(rows):
    """Các con số tóm tắt: độ chính xác, gọi thừa tool, số token, thời gian."""
    n = len(rows)
    tool_ok = sum(s["tool_ok"] for _, _, s in rows)
    args_ok = sum(s["args_ok"] for _, _, s in rows if s["tool_ok"])
    out_of_scope = [s for c, _, s in rows if c["tool"] is None]
    seconds = sorted(o["total_s"] for _, o, _ in rows)
    return {"n": n, "ok": round(sum(s["ok"] for _, _, s in rows) / n, 3), "tool_ok": round(tool_ok / n, 3),
            "args_ok|tool_ok": round(args_ok / max(tool_ok, 1), 3),
            "over_trigger": round(sum(s["over_trigger"] for s in out_of_scope) / max(len(out_of_scope), 1), 3),
            "tok_tb": round(sum(o["tokens"] for _, o, _ in rows) / n, 1),
            "p50_s": round(seconds[n // 2], 2), "p95_s": round(seconds[int(n * .95)], 2)}


def wrong_cases(rows, limit=15):
    """Bảng các câu sai: câu nói, đáp án đúng, model chọn gì."""
    import pandas as pd
    wrong = [{"câu": c["context"]["user_prompt"], "đúng": c["tool"], "model": predicted_tool(o["text"]),
              "tham số đúng": c["args"]} for c, o, s in rows if not s["ok"]]
    return pd.DataFrame(wrong[:limit])


def evaluate(cfg, cases, label="", tools_for=None, **server_args):
    """Bật server, đo một bộ câu, in tóm tắt và các cặp nhầm lẫn. Trả về rows."""
    start = time.time()
    with slm_tools.server(cfg, **server_args) as srv:
        srv.ask(messages_cached(cases[0], NAMES), grammar=schema_v2())          # làm nóng cache
        rows = run(srv, cases, tools_for=tools_for)
    print(f"\n== {label or cfg['label']} ({time.time() - start:.0f}s)")
    print(summary(rows))
    confusion = Counter((c["tool"], predicted_tool(o["text"])) for c, o, s in rows if not s["tool_ok"])
    if confusion:
        print("nhầm tool (đúng -> model):", ", ".join(f"{n}x {a}->{b}" for (a, b), n in confusion.most_common(8)))
    return rows


def measure_point(cfg, label="", sets=None, tools_for=None, **server_args):
    """Một điểm trên đồ thị độ chính xác - bộ nhớ: dung lượng file, RAM đỉnh, điểm và thời gian trên từng bộ câu.
    sets: dict tên -> danh sách ca (mặc định: bộ 156 câu và bộ giữ riêng). tools_for: xem hàm run.
    server_args ví dụ gpu_layers=0."""
    sets = sets or {"156": test156(), "lạ": heldout()}
    point = {"model": label or cfg["label"], "file_mb": round(Path(cfg["file"]).stat().st_size / 2**20, 1)}
    first_case = next(iter(sets.values()))[0]
    with slm_tools.server(cfg, **server_args) as srv:
        srv.ask(messages_cached(first_case, NAMES), grammar=schema_v2())        # làm nóng cache
        for name, cases in sets.items():
            s = summary(run(srv, cases, tools_for=tools_for))
            point.update({f"ok_{name}": s["ok"], f"over_{name}": s["over_trigger"],
                          f"p50_{name}": s["p50_s"], f"p95_{name}": s["p95_s"]})
        point["ram_mb"] = round(srv.ram.peak_mb, 1)
    return point


def choose_smallest(results, prefix, metric="ok_lạ", tolerance=0.02, size_col="ram_mb"):
    """Trong các dòng có tên bắt đầu bằng `prefix`, chọn dòng nhỏ nhất (theo size_col) mà điểm `metric`
    không thấp hơn điểm tốt nhất quá `tolerance`."""
    rows = results[results["model"].str.startswith(prefix)]
    good_enough = rows[rows[metric] >= rows[metric].max() - tolerance]
    return good_enough.sort_values(size_col).iloc[0]


def sweep(items, sets=None, tools_for=None, **server_args):
    """items: danh sách (nhãn, cfg) hoặc (nhãn, cfg, tools_for) nếu mỗi mục dùng cách chọn công cụ riêng.
    Đo từng mục và trả về bảng so sánh."""
    import pandas as pd
    rows = []
    for label, cfg, *own_tools in items:
        print("đang đo:", label)
        rows.append(measure_point(cfg, label, sets, own_tools[0] if own_tools else tools_for, **server_args))
    return pd.DataFrame(rows)
