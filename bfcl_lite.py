"""BFCL rút gọn: dùng đúng dữ liệu và đáp án của Berkeley Function-Calling Leaderboard (v4).

Bốn nhóm câu hỏi:
  simple      - một hàm, gọi một lần
  multiple    - nhiều hàm để chọn, gọi một hàm
  parallel    - phải gọi cùng lúc nhiều lần
  irrelevance - KHÔNG có hàm nào phù hợp, model phải không gọi gì

Đây là bản chấm đơn giản hóa (không dùng bộ chấm AST chính thức), nên điểm KHÔNG so sánh trực tiếp
với bảng xếp hạng BFCL; dùng để so sánh các model của nhóm với nhau trên cùng một đề.
"""
import json
import random
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/ShishirPatil/gorilla/main/berkeley-function-call-leaderboard/bfcl_eval/data/"
FILES = {"simple": "BFCL_v4_simple_python.json", "multiple": "BFCL_v4_multiple.json",
         "parallel": "BFCL_v4_parallel.json", "irrelevance": "BFCL_v4_irrelevance.json"}

GRAMMAR = r'''
root ::= "{" space "\"tool_calls\":" space "[" space (Call ("," space Call)*)? "]" space "}" space
Call ::= "{" space "\"name\":" space String "," space "\"arguments\":" space Obj "}" space
Obj ::= "{" space (Pair ("," space Pair)*)? "}" space
Pair ::= String ":" space Value
Value ::= String | Number | Bool | "null" space | Arr | Obj
Arr ::= "[" space (Value ("," space Value)*)? "]" space
Number ::= ("-"? ("0" | [1-9] [0-9]*) ("." [0-9]+)? ([eE] [-+]? [0-9]+)?) space
Bool ::= ("true" | "false") space
space ::= " "?
String ::= "\"" ([^"\\\x00-\x1F] | "\\" (["\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F]))* "\"" space
'''

SYSTEM = ('You can call the functions listed below. Reply with exactly one JSON object: '
          '{"tool_calls": [{"name": <function name>, "arguments": {...}}, ...]}. '
          'Use one entry per call. If no function fits the request, reply {"tool_calls": []}.\n'
          'Functions:\n')


def _download(name, folder):
    path = Path(folder) / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(BASE + name, path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_items(category, n=60, seed=42, folder="bfcl_data"):
    """Tải dữ liệu (lần đầu) và lấy ngẫu nhiên n câu với seed cố định."""
    items = _download(FILES[category], folder)
    if category != "irrelevance":
        answers = {a["id"]: a["ground_truth"] for a in _download("possible_answer/" + FILES[category], folder)}
        for it in items:
            it["truth"] = answers[it["id"]]
    return random.Random(seed).sample(items, min(n, len(items)))


def messages(item):
    return [{"role": "system", "content": SYSTEM + json.dumps(item["function"])},
            {"role": "user", "content": item["question"][0][0]["content"]}]


def _same(a, b):
    """So sánh giá trị, bỏ qua chênh lệch int/float và hoa/thường."""
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    if num(a) and num(b):
        return float(a) == float(b)
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return a == b


def _call_matches(call, truth, schema_params):
    """truth = {"tên_hàm": {"tham_số": [các giá trị chấp nhận được]}}; "" trong danh sách = tham số tùy chọn."""
    (fname, params), = truth.items()
    args = call.get("arguments", {})
    if call.get("name") != fname or not isinstance(args, dict):
        return False
    for p, allowed in params.items():
        if p in args:
            if not any(_same(args[p], v) for v in allowed):
                return False
        elif "" not in allowed:
            return False                                     # thiếu tham số bắt buộc
    return all(k in schema_params for k in args)             # không được bịa tham số


def score(item, category, raw):
    """True nếu model làm đúng."""
    try:
        calls = json.loads(raw)["tool_calls"]
        assert isinstance(calls, list)
    except Exception:
        return False
    if category == "irrelevance":
        return calls == []
    props = {k for f in item["function"] for k in f["parameters"]["properties"]}
    truths = item["truth"]
    if len(calls) != len(truths):
        return False
    unused = list(calls)
    for t in truths:                                         # mỗi lời gọi đúng phải khớp một lời gọi của model
        hit = next((c for c in unused if isinstance(c, dict) and _call_matches(c, t, props)), None)
        if hit is None:
            return False
        unused.remove(hit)
    return True
