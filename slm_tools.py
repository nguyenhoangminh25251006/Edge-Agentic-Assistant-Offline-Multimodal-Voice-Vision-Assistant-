"""Công cụ dùng chung cho mọi notebook benchmark.

File này chỉ làm 4 việc đơn giản:
  1. Bật / tắt một model (server) và đo RAM của nó.
  2. Gửi một câu hỏi cho model và đo thời gian trả lời.
  3. Chạy cùng một bài đo cho cả 15 cấu hình và lưu kết quả vào thư mục results/.
  4. Vẽ biểu đồ để so sánh các model.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

# Đảm bảo Windows tìm được các DLL CUDA (cublas, cudart...) từ torch cho cả tiến trình hiện tại và subprocess
try:
    import torch
    _torch_lib = os.path.join(os.path.dirname(torch.__file__), "lib")
    if os.path.isdir(_torch_lib):
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(_torch_lib)
        if _torch_lib not in os.environ.get("PATH", ""):
            os.environ["PATH"] = _torch_lib + os.pathsep + os.environ.get("PATH", "")
except Exception:
    pass

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import psutil
import requests
from matplotlib.patches import Patch

import model_catalog

RESULTS_DIR = Path("results")      # mỗi benchmark lưu 1 file JSON ở đây
CHARTS_DIR = Path("charts")        # biểu đồ lưu thành ảnh ở đây
RAM_BUDGET_MB = 1500               # GIẢ ĐỊNH: RAM tối đa cho SLM trên UNO Q (sửa theo giới hạn container)
UNO_Q_RAM_MB = 4096
TTL_S = 2.0                        # thời gian tối đa cho 1 lần trả lời (theo thiết kế của nhóm)
QUANT_SHORT = {"F16": "F16", "Q8_0": "Q8", "Q4_K_M": "Q4"}


# ===================================================================== 1. Danh sách model
def all_configs():
    """15 cấu hình (model + mức nén) lấy từ model_catalog.CONFIGS."""
    cfgs = []
    for e in model_catalog.all_entries():
        e = dict(e)
        e["label"] = f"{e['name']} ({QUANT_SHORT[e['quant']]})"
        e["file"] = model_catalog.gguf_path(e["name"], e["quant"])
        cfgs.append(e)
    return cfgs


def base_configs():
    """Một cấu hình cho mỗi model GỐC (trọng số Hugging Face, chưa nén), dùng cho notebook 01/02/06.

    Các bài lm-eval chạy bằng transformers trên trọng số gốc, KHÔNG chạy trên file GGUF đã nén.
    Vì vậy kết quả được lưu với id "<model>__HF" và nhãn "(HF gốc)" để không bị nhầm thành điểm của Q4/Q8.
    """
    seen = {}
    for c in all_configs():
        if c["name"] not in seen:
            seen[c["name"]] = {**c, "id": f"{c['name']}__HF", "quant": "F16", "bits": 16,
                               "label": f"{c['name']} (HF gốc)"}
    return list(seen.values())


# ===================================================================== 2. Bật model và đo RAM
class _RamMeter:
    """Đo RAM lớn nhất mà tiến trình model (llama-server) đã dùng.

    Chỉ đo đúng tiến trình server (không cộng tiến trình con / Python). Trên Windows lấy thêm
    `peak_wset` do hệ điều hành ghi lại, nên không bỏ sót đỉnh ngắn giữa 2 lần lấy mẫu.
    """

    def __init__(self, pid):
        self.pid, self.peak_mb, self._stop = pid, 0.0, threading.Event()
        try:
            self._proc = psutil.Process(pid)
        except psutil.Error:
            self._proc = None
        self._sample()                 # có ngay số RAM sau khi nạp model
        threading.Thread(target=self._loop, daemon=True).start()

    def _sample(self):
        if self._proc is None:
            return
        try:
            mi = self._proc.memory_info()
            mb = max(mi.rss, getattr(mi, "peak_wset", 0)) / 2**20
            self.peak_mb = max(self.peak_mb, mb)
        except psutil.Error:
            pass

    def _loop(self):
        while not self._stop.is_set():
            self._sample()
            time.sleep(0.1)

    def stop(self):
        self._sample()                 # lấy mẫu lần cuối trước khi server tắt
        self._stop.set()


def ram_suspicious(ram_mb, file_mb, n_ctx=2048):
    """True nếu số RAM đo được vô lý so với kích thước file model (dấu hiệu đo sai)."""
    if not ram_mb or not file_mb:
        return False
    return ram_mb > 2.0 * file_mb + 1024 + n_ctx / 2


class Server:
    """Một model đang chạy. Dùng .ask(...) để hỏi nó."""

    def __init__(self, cfg, url, ram, boot_s):
        self.cfg, self.url, self.ram, self.boot_s = cfg, url, ram, boot_s
        self._http = requests.Session()
        self._http.trust_env = False        # tránh proxy hệ thống chặn kết nối nội bộ

    def ask(self, messages, grammar=None, max_tokens=200, cache_prompt=False, tools=None):
        """Hỏi model 1 câu. Trả về chữ trả lời + thời gian (giây).

        grammar: luật ép model chỉ được viết JSON hợp lệ (GBNF của llama.cpp).
        cache_prompt: True = giữ lại phần prompt giống lần trước (system prompt + danh sách tool),
                      đúng như khi triển khai thật. False = đo trường hợp xấu nhất (prompt nguội).
        tools: danh sách tool theo chuẩn OpenAI. Khi có, tool được đưa vào prompt bằng CHAT TEMPLATE của
               chính model (định dạng model đã được huấn luyện) và kết quả nằm ở "tool_calls".
        """
        messages = [dict(m) for m in messages]
        body = {"messages": messages, "max_tokens": max_tokens, "temperature": 0.0, "stream": True,
                "cache_prompt": cache_prompt}
        if "Qwen3" in self.cfg["name"]:
            # Tắt chế độ "suy nghĩ" của Qwen3 cho MỌI câu (trước đây chỉ tắt khi không có grammar).
            # enable_thinking=False làm chat template chèn sẵn khối <think></think> rỗng vào prompt.
            if isinstance(messages[-1].get("content"), str):
                messages[-1]["content"] += " /no_think"
            body["chat_template_kwargs"] = {"enable_thinking": False}
        if grammar:
            body["grammar"] = grammar
        if tools:
            body["tools"] = tools
        t0, t_first, parts, finish = time.perf_counter(), None, [], None
        calls = {}                                     # index -> {"name", "arguments"(chuỗi ghép dần)}
        with self._http.post(f"{self.url}/v1/chat/completions", json=body, stream=True, timeout=600) as r:
            r.raise_for_status()
            r.encoding = "utf-8"
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:") or line.endswith("[DONE]"):
                    continue
                choice = json.loads(line[5:])["choices"][0]
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if piece:
                    t_first = t_first or time.perf_counter()
                    parts.append(piece)
                for tc in delta.get("tool_calls") or []:
                    t_first = t_first or time.perf_counter()
                    slot = calls.setdefault(tc.get("index", 0), {"name": "", "arguments": ""})
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["arguments"] += fn.get("arguments") or ""
                    parts.append("")                   # đếm như một token đã sinh
                finish = choice.get("finish_reason") or finish
        total = time.perf_counter() - t0
        text = "".join(parts)
        if "</think>" in text:
            text = text.split("</think>")[-1]
        return {"text": text.strip(), "tool_calls": [calls[k] for k in sorted(calls)],
                "first_token_s": (t_first - t0) if t_first else None,
                "total_s": total, "tokens": len(parts), "finish": finish}


@contextmanager
def server(cfg, n_ctx=2048, threads=None, gpu_layers=0, command=None):
    """Bật model, đưa cho notebook dùng, rồi tự tắt.

    gpu_layers=0: chạy bằng CPU giống UNO Q, nên RAM và tốc độ có thể so sánh.
    command: chỉ dùng khi kiểm thử bằng server giả.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cmd = command(port) if command else [
        str(model_catalog.find_llama_bin("llama-server", "./llama.cpp")), "-m", str(cfg["file"]),
        "--host", "127.0.0.1", "--port", str(port), "-c", str(n_ctx),
        "-t", str(threads or os.cpu_count() or 4), "-ngl", str(gpu_layers), "-b", "64", "-ub", "64",
        "--cache-ram", "0", "-np", "1", "--no-repack",
        "--jinja"]                       # cần để chat_template_kwargs (tắt thinking của Qwen3) có tác dụng
    t0 = time.perf_counter()
    env = os.environ.copy()
    if gpu_layers == 0:
        env["CUDA_VISIBLE_DEVICES"] = "-1"     # giấu GPU để CUDA không khởi tạo, giống UNO Q
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                            encoding="utf-8", errors="replace")
    log = []
    threading.Thread(target=lambda: [log.append(l.rstrip()) for l in proc.stdout], daemon=True).start()
    url, http, ram = f"http://127.0.0.1:{port}", requests.Session(), None
    http.trust_env = False
    try:
        while True:                                   # chờ model nạp xong
            if proc.poll() is not None:
                raise RuntimeError("Model không chạy được (có thể thiếu RAM):\n" + "\n".join(log[-8:]))
            if time.perf_counter() - t0 > 300:
                raise RuntimeError("Model nạp quá 300 giây")
            try:
                if http.get(f"{url}/v1/models", timeout=1).status_code == 200:
                    break
            except requests.RequestException:
                time.sleep(0.3)
        ram = _RamMeter(proc.pid)
        try:
            yield Server(cfg, url, ram, time.perf_counter() - t0)
        except Exception as e:
            if proc.poll() not in (None, 0):          # server đã chết
                raise RuntimeError(f"{e}\n--- log server ---\n" + "\n".join(log[-15:])) from e
            raise
    finally:
        if ram:
            ram.stop()
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

def load_base(benchmark):
    """Kết quả của bài đo trên model GỐC (HF, chưa nén): mỗi model một dòng.

    Lưu ý: đây là điểm của trọng số gốc, KHÔNG phải của bản Q8/Q4. Không được ghép điểm này vào hàng Q4/Q8.
    Kết quả cũ (trước khi sửa) bị lưu dưới id kiểu "Phi-4__Q4_K_M" nhưng thực chất cũng là điểm HF gốc,
    nên vẫn được đọc vào và gắn lại nhãn đúng.
    """
    df = load(benchmark)
    if df.empty:
        return df
    df["_hf"] = df["id"].str.endswith("__HF")
    df = df.sort_values(["_hf", "quant"], ascending=[False, True]).drop_duplicates("model").drop(columns="_hf")
    df["label"], df["quant"] = df["model"] + " (HF gốc)", "F16"      # F16 chỉ để lấy màu xanh dương
    return df

# ===================================================================== 3. Chạy và lưu kết quả
def run_all(benchmark, measure, configs=None, force=False, needs_server=True, require=(), **server_args):
    """Chạy hàm `measure(srv)` cho từng cấu hình và lưu vào results/<benchmark>.json.

    Chạy lại notebook sẽ bỏ qua cấu hình đã xong (đặt force=True để đo lại).
    require: các cột bắt buộc; kết quả cũ thiếu cột nào thì đo lại (dùng khi bài đo vừa thêm chỉ số mới).
    measure(srv) phải trả về dict các con số. Nếu needs_server=False thì measure nhận cfg thay vì srv.
    """
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"{benchmark}.json"
    results = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for cfg in configs or all_configs():
        done = cfg["id"] in results and "error" not in results[cfg["id"]]
        if done and not force and all(k in results[cfg["id"]] for k in require):
            print(f"[đã có] {cfg['label']}")
            continue
        if not Path(cfg["file"]).exists():
            print(f"[thiếu file] {cfg['label']}  (chạy notebook 00 trước)")
            continue
        print(f"[đang chạy] {cfg['label']}")
        t0 = time.time()
        try:
            if needs_server:
                with server(cfg, **server_args) as srv:
                    ram_load = srv.ram.peak_mb
                    metrics = measure(srv)
                    srv.ram.stop()
                    metrics["ram_load_mb"] = round(ram_load, 1)          # RAM ngay sau khi nạp model
                    metrics["ram_peak_mb"] = round(srv.ram.peak_mb, 1)   # RAM lớn nhất trong cả bài đo
                    file_mb = Path(cfg["file"]).stat().st_size / 2**20
                    if ram_suspicious(metrics["ram_peak_mb"], file_mb, server_args.get("n_ctx", 2048)):
                        metrics["ram_suspect"] = True
                        print(f"   CẢNH BÁO: RAM {metrics['ram_peak_mb']:.0f} MB quá lớn so với file "
                              f"{file_mb:.0f} MB (lúc nạp: {ram_load:.0f} MB) -> RAM tăng dần theo số câu hỏi?")
            else:
                metrics = measure(cfg)
        except Exception as e:                        # hết RAM hay lỗi khác cũng là một kết quả
            metrics = {"error": str(e)[:300]}
            print("   lỗi:", str(e)[:150])
        results[cfg["id"]] = metrics
        path.write_text(json.dumps(results, indent=1), encoding="utf-8")
        print(f"   xong sau {time.time() - t0:.0f}s")


def load(benchmark):
    """Đọc kết quả thành bảng (mỗi dòng một cấu hình)."""
    path = RESULTS_DIR / f"{benchmark}.json"
    results = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    rows = []
    for cfg in all_configs() + base_configs():
        if cfg["id"] in results:
            rows.append({"id": cfg["id"], "model": cfg["name"], "quant": cfg["quant"], "label": cfg["label"],
                         "params_b": cfg["params_b"], **results[cfg["id"]]})
    return pd.DataFrame(rows)


# ===================================================================== 4. Đo bằng lm-evaluation-harness
_hf_score_cache = {}

def lm_eval_scores_hf(target, limits=None, batch_size=8):
    """Chấm accuracy qua backend 'hf' (transformers) trực tiếp trên GPU — có batching song song.
    Nhanh gấp hàng trăm lần so với gửi từng câu tuần tự qua HTTP tới server GGUF.
    target có thể là Server (srv) hoặc dict cấu hình (cfg).

    CHÚ Ý: hàm này tải trọng số GỐC từ Hugging Face (hf_repo), KHÔNG dùng file GGUF đã nén.
    Điểm trả về là điểm của model gốc, phải chạy với st.base_configs() và đọc bằng st.load_base().
    Muốn chấm đúng bản Q8/Q4 thì dùng lm_eval_scores(srv, ..., fast=False) qua server GGUF.
    """
    import gc
    import torch
    from lm_eval import simple_evaluate

    cfg = getattr(target, "cfg", target)
    hf_repo = cfg.get("hf_repo") or cfg.get("name")

    cache_key = (hf_repo, tuple(sorted(limits.items())) if isinstance(limits, dict) else tuple(limits or []))
    if cache_key in _hf_score_cache:
        print(f"   [tái sử dụng kết quả hf của {cfg.get('name')}]")
        return dict(_hf_score_cache[cache_key])

    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    dtype = ("bfloat16" if torch.cuda.is_bf16_supported() else "float16") if torch.cuda.is_available() else "float32"

    prefer = ["acc_norm,none", "acc,none", "exact_match,strict-match", "prompt_level_strict_acc,none"]
    scores = {}

    tasks = list(limits.keys()) if isinstance(limits, dict) else list(limits)
    for task in tasks:
        limit = limits.get(task) if isinstance(limits, dict) else None
        try:
            res = simple_evaluate(
                model="hf",
                model_args=f"pretrained={hf_repo},dtype={dtype},device={device}",
                tasks=[task],
                limit=limit,
                batch_size=batch_size,
                bootstrap_iters=0,
            )["results"][task]
            scores[task] = next(res[k] for k in prefer if k in res)
        except Exception as e:
            print(f"   lỗi bài {task}: {e}")
            scores[task] = None

    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    _hf_score_cache[cache_key] = scores
    return scores


def lm_eval_scores(srv, limits, fast=True, batch_size=8):
    """Chạy các bài kiểm tra chuẩn của lm-evaluation-harness trên model.

    fast=True (mặc định): Đo siêu nhanh trực tiếp trên GPU qua Transformers với Batching song song
                          (tương tự như cách file edge_slm_benchmark.ipynb đã chạy).
    fast=False: Gửi từng câu tuần tự qua HTTP tới llama.cpp server (chậm hơn rất nhiều).
    """
    if fast:
        return lm_eval_scores_hf(srv, limits, batch_size=batch_size)

    from lm_eval import simple_evaluate
    from lm_eval.models.gguf import GGUFLM

    class GGUFWithLongAnswers(GGUFLM):
        """Bản gốc không đặt độ dài câu trả lời nên bài toán (GSM8K) bị cắt ở 16 token; ở đây sửa lại."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._http = requests.Session()
            self._http.trust_env = False

        def gguf_completion(self, context, continuation=None, stop=None, retries=3, delay=5, **kw):
            body = {"prompt": context, "temperature": 0.0}
            if continuation:       # chấm điểm lựa chọn: hỏi xác suất của đáp án
                body.update(prompt=context + continuation, max_tokens=1, echo=True, logprobs=10)
            else:                  # để model tự viết đáp án
                body["max_tokens"] = 256
            if stop:
                body["stop"] = stop
            for _ in range(retries):
                try:
                    r = self._http.post(f"{self.base_url}/v1/completions", json=body, timeout=600)
                    r.raise_for_status()
                    return r.json()
                except requests.RequestException:
                    time.sleep(delay)
            raise RuntimeError("model không trả lời")

    lm = GGUFWithLongAnswers(base_url=srv.url)
    prefer = ["acc_norm,none", "acc,none", "exact_match,strict-match", "prompt_level_strict_acc,none"]
    scores = {}
    for task, limit in limits.items():
        res = simple_evaluate(model=lm, tasks=[task], limit=limit, bootstrap_iters=0)["results"][task]
        scores[task] = next(res[k] for k in prefer if k in res)
    return scores


# ===================================================================== 5. Đo perplexity (độ "bối rối" của model)
def perplexity(cfg, text_file, llama_cpp_dir="./llama.cpp", chunks=40):
    """Càng thấp càng tốt. Chỉ so sánh giữa các mức nén của CÙNG một model."""
    import re
    exe = model_catalog.find_llama_bin("llama-perplexity", llama_cpp_dir)
    p = subprocess.run([str(exe), "-m", str(cfg["file"]), "-f", str(text_file), "-c", "512",
                        "--chunks", str(chunks), "-ngl", "0"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    m = re.search(r"Final estimate: PPL = ([\d.]+)", p.stdout + p.stderr)
    if not m:
        raise RuntimeError("Không đọc được perplexity:\n" + (p.stdout + p.stderr)[-400:])
    return float(m.group(1))


# ===================================================================== 6. Biểu đồ
COLORS = {"F16": "#0072B2", "Q8_0": "#E69F00", "Q4_K_M": "#009E73"}   # màu an toàn cho người mù màu
MARKERS = {"F16": "o", "Q8_0": "s", "Q4_K_M": "^"}


def _finish(fig, ax, title, subtitle, name):
    ax.set_title(title, loc="left", fontsize=13, fontweight="bold", pad=26)
    ax.annotate(subtitle, (0, 1), xycoords="axes fraction", xytext=(0, 7), textcoords="offset points",
                fontsize=9, color="#555")
    CHARTS_DIR.mkdir(exist_ok=True)
    fig.savefig(CHARTS_DIR / f"{name}.png", dpi=150, bbox_inches="tight")
    plt.show()


def _vline(ax, x, text):
    """Đường mốc dọc (ví dụ giới hạn RAM) kèm chú thích xoay dọc, không đè lên thanh."""
    ax.axvline(x, color="#C0392B", ls="--", lw=1.2, zorder=4)
    ax.text(x, 0.98, text + " ", transform=ax.get_xaxis_transform(), rotation=90, ha="right", va="top",
            fontsize=8, color="#C0392B", bbox={"fc": "white", "ec": "none", "alpha": 0.85, "pad": 1}, zorder=5)


def _quant_legend(ax, quants, loc="below", dy=-0.07):
    place = {"loc": "upper center", "bbox_to_anchor": (0.5, dy), "ncol": 3} if loc == "below" else {"loc": loc}
    ax.legend(**place, handles=[Patch(color=COLORS[q], label={"F16": "F16 (no compression)", "Q8_0": "Q8 (light compression)",
                                                     "Q4_K_M": "Q4 (strong compression)"}[q])
                       for q in COLORS if q in set(quants)], fontsize=8, frameon=False)


def bar_chart(df, col, title, subtitle="Higher is better", percent=True, lines=(), name=None, lower_is_better=False):
    """Thanh ngang: mỗi model một thanh, đã xếp từ tốt đến kém; màu = mức nén.

    lines: [(giá trị, "chú thích")] vẽ đường mốc, ví dụ giới hạn RAM.
    """
    if col not in df.columns:
        raise KeyError(f"Cột '{col}' không có trong kết quả đo. Các model có thể chưa hoàn thành hoặc gặp lỗi khi chạy. Vui lòng kiểm tra lại hàm run_all.")
    d = df.dropna(subset=[col]).sort_values(col, ascending=lower_is_better)
    k = 100 if percent else 1
    fig, ax = plt.subplots(figsize=(9, max(3, 0.4 * len(d) + 1.8)))
    bars = ax.barh(d["label"], d[col] * k, color=[COLORS[q] for q in d["quant"]])
    for b in bars:
        v = b.get_width()
        ax.text(v, b.get_y() + b.get_height() / 2, f" {v:.0f}%" if percent else f" {v:.2f}" if v < 10 else f" {v:.0f}",
                va="center", fontsize=8)
    for x, text in lines:
        _vline(ax, x, text)
    ax.invert_yaxis()
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="x", alpha=0.2)
    _quant_legend(ax, d["quant"])
    _finish(fig, ax, title, subtitle, name or col)


def heatmap(df, cols, title, subtitle="Darker = better", name="heatmap"):
    """Bảng màu: hàng = model, cột = bài kiểm tra, mỗi ô là điểm % (đậm hơn = tốt hơn)."""
    d = df.set_index("label")[list(cols)].astype(float) * 100
    d = d.loc[d.mean(axis=1).sort_values(ascending=False).index]
    fig, ax = plt.subplots(figsize=(1.3 * len(cols) + 3, 0.38 * len(d) + 1.8))
    ax.imshow(d.values, cmap="Blues", vmin=0, vmax=100, aspect="auto")
    ax.set_xticks(range(len(cols)), labels=[cols[c] for c in cols], fontsize=9)
    ax.set_yticks(range(len(d)), labels=d.index, fontsize=8)
    for i in range(len(d)):
        for j in range(len(cols)):
            v = d.values[i, j]
            if v == v:
                ax.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=8, color="white" if v > 55 else "#222")
    ax.spines[:].set_visible(False)
    _finish(fig, ax, title, subtitle, name)


def grouped_bars(df, cols, title, subtitle="Higher is better", percent=True, name="grouped", lower_is_better=False):
    """Mỗi model có vài thanh (ví dụ 3 / 6 / 11 công cụ) để thấy điểm thay đổi thế nào."""
    d = df.dropna(subset=list(cols)).sort_values(list(cols)[0], ascending=not lower_is_better)
    n, shades = len(cols), plt.cm.Blues(np.linspace(0.35, 0.9, len(cols)))
    fig, ax = plt.subplots(figsize=(9, max(3, 0.55 * len(d) + 1.8)))
    h = 0.8 / n
    for i, (c, text) in enumerate(cols.items()):
        ax.barh(np.arange(len(d)) + i * h, d[c] * (100 if percent else 1), height=h, color=shades[i], label=text)
    ax.set_yticks(np.arange(len(d)) + h * (n - 1) / 2, labels=d["label"], fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="x", alpha=0.2)
    ax.legend(fontsize=8, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.04), ncol=n)
    _finish(fig, ax, title, subtitle, name)


def quant_lines(df, col, title, subtitle="Each line is one model", percent=True, name=None):
    """Mỗi đường là một model: điểm thay đổi thế nào khi nén mạnh hơn (F16 -> Q8 -> Q4)."""
    x = {"F16": 0, "Q8_0": 1, "Q4_K_M": 2}
    fig, ax = plt.subplots(figsize=(8, 5))
    for m, g in df.dropna(subset=[col]).groupby("model"):
        if len(g) < 2:
            continue
        g = g.assign(x=g["quant"].map(x)).sort_values("x")
        ax.plot(g["x"], g[col] * (100 if percent else 1), marker="o", lw=1.8, label=m)
        ax.text(g["x"].iloc[-1] + 0.04, g[col].iloc[-1] * (100 if percent else 1), m, fontsize=8, va="center")
    ax.set_xticks([0, 1, 2], labels=["F16\n(no compression)", "Q8\n(light)", "Q4\n(strong)"])
    ax.set_xlim(-0.2, 3.2)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(alpha=0.2)
    _finish(fig, ax, title, subtitle, name or f"quant_{col}")


def scatter_chart(df, x, y, title, xlabel, ylabel, subtitle="", lines=(), name="scatter", frontier=False, logx=False):
    """Mỗi chấm là một model. Màu và hình = mức nén."""
    d = df.dropna(subset=[x, y])
    fig, ax = plt.subplots(figsize=(9, 5.5))
    for q in COLORS:
        s = d[d.quant == q]
        ax.scatter(s[x], s[y] * 100, s=80, c=COLORS[q], marker=MARKERS[q], edgecolor="white", label=q, zorder=3)
    for _, r in d.iterrows():
        ax.annotate(r["label"], (r[x], r[y] * 100), xytext=(5, 4), textcoords="offset points", fontsize=7)
    if logx:
        from matplotlib.ticker import NullFormatter
        ax.set_xscale("log")
        ax.xaxis.set_minor_formatter(NullFormatter())
        ax.set_xticks([0.1, 0.2, 0.5, 1, 2, 4], labels=["0.1B", "0.2B", "0.5B", "1B", "2B", "4B"])
    for v, text in lines:
        _vline(ax, v, text)
    if frontier:      # đường nối các model "không bị model nhỏ hơn nào vượt mặt"
        best, pts = -1, []
        for _, r in d.sort_values(x).iterrows():
            if r[y] > best:
                best = r[y]
                pts.append((r[x], r[y] * 100))
        ax.step(*zip(*pts), where="post", color="#555", ls=":", lw=1.5, zorder=2, label="best quality for that memory")
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(alpha=0.2)
    _quant_legend(ax, d["quant"], dy=-0.14)
    _finish(fig, ax, title, subtitle, name)


def latency_boxplot(df, ttl_s=TTL_S, name="latency_boxplot"):
    """Mỗi hộp là phân bố thời gian trả lời của một model (hộp hẹp = ổn định; chấm = lần chậm bất thường)."""
    d = df[df["latencies"].notna()].copy()
    d["median"] = d["latencies"].apply(np.median)
    d = d.sort_values("median", ascending=False)
    fig, ax = plt.subplots(figsize=(9, max(3, 0.4 * len(d) + 1.8)))
    box = ax.boxplot(list(d["latencies"]), vert=False, patch_artist=True, widths=0.6,
                     flierprops={"markersize": 3}, medianprops={"color": "black"})
    for patch, q in zip(box["boxes"], d["quant"]):
        patch.set_facecolor(COLORS[q])
    ax.set_yticks(range(1, len(d) + 1), labels=d["label"], fontsize=8)
    _vline(ax, ttl_s, f"{ttl_s:.0f}s limit")
    ax.set_xlabel("Seconds per answer")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="x", alpha=0.2)
    _quant_legend(ax, d["quant"])
    _finish(fig, ax, "How long does each model take to answer?", "Lower and narrower is better. Dashed line = time limit in our design", name)
