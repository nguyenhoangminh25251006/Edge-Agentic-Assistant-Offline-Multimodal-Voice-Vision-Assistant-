"""Bộ chọn công cụ (Tool RAG, theo TinyAgent): một model phân loại nhỏ đọc câu nói và chọn vài công cụ liên quan.

Vì sao cần: prompt chỉ chứa các công cụ được chọn nên ngắn hơn nhiều (ít công cụ hơn = ít token phải đọc).
Cách làm: DeBERTa-v3-small + một lớp tuyến tính 11 đầu ra. Mỗi đầu ra là xác suất "công cụ này liên quan đến câu nói".
Công cụ nào có xác suất vượt ngưỡng được đưa vào prompt (và luôn lấy ít nhất `min_tools` công cụ cao nhất, vì thiếu đúng
công cụ cần thiết thì model phía sau không thể chọn được nữa).

Dùng:  r = ToolRetriever(); r.fit(texts, gold_tools); r.select(texts); r.save(dir); ToolRetriever.load(dir)
"""
import json
import random
from pathlib import Path

import make_train as M

DEFAULT_BASE = "microsoft/deberta-v3-small"


class ToolRetriever:
    def __init__(self, tool_names=M.ALL_TOOLS, base=DEFAULT_BASE):
        self.tools, self.base = list(tool_names), base
        self.tokenizer = self.encoder = self.head = None

    # ------------------------------------------------------------------ khởi tạo
    def _init_modules(self, source):
        import torch
        from transformers import AutoModel, AutoTokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(source)
        self.encoder = AutoModel.from_pretrained(source)
        self.head = torch.nn.Linear(self.encoder.config.hidden_size, len(self.tools))

    def _logits(self, texts, device, max_len=48):
        batch = self.tokenizer(texts, padding=True, truncation=True, max_length=max_len, return_tensors="pt").to(device)
        return self.head(self.encoder(**batch).last_hidden_state[:, 0])           # lấy vector của token đầu tiên

    def to(self, device):
        self.encoder.to(device)
        self.head.to(device)
        return self

    def train(self):
        self.encoder.train()
        self.head.train()
        return self

    def eval(self):
        self.encoder.eval()
        self.head.eval()
        return self

    # ------------------------------------------------------------------ huấn luyện
    def fit(self, texts, gold_tools, epochs=6, lr=5e-5, bs=32, device=None, seed=0, log=print):
        """gold_tools[i] là công cụ đúng của câu i, hoặc None nếu câu ngoài phạm vi (mọi đầu ra đều là 0)."""
        import torch
        random.seed(seed)
        torch.manual_seed(seed)
        device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self._init_modules(self.base)
        self.to(device).train()
        targets = torch.zeros(len(texts), len(self.tools))
        for i, tool in enumerate(gold_tools):
            if tool is not None:
                targets[i, self.tools.index(tool)] = 1.0
        params = list(self.encoder.parameters()) + list(self.head.parameters())
        optimizer = torch.optim.AdamW(params, lr=lr, weight_decay=0.01)
        loss_fn = torch.nn.BCEWithLogitsLoss()
        order, history = list(range(len(texts))), []
        for epoch in range(epochs):
            random.shuffle(order)
            total = 0.0
            for start in range(0, len(order), bs):
                idx = order[start:start + bs]
                loss = loss_fn(self._logits([texts[i] for i in idx], device), targets[idx].to(device))
                loss.backward()
                torch.nn.utils.clip_grad_norm_(params, 1.0)
                optimizer.step()
                optimizer.zero_grad(set_to_none=True)
                total += loss.item() * len(idx)
            history.append(total / len(texts))
            log(f"vòng {epoch + 1}/{epochs} | loss {history[-1]:.4f}")
        self.to("cpu").eval()
        return history

    # ------------------------------------------------------------------ dùng
    def probabilities(self, texts, device="cpu", bs=64):
        """Ma trận xác suất [số câu, số công cụ]."""
        import torch
        self.to(device).eval()
        out = []
        with torch.no_grad():
            for start in range(0, len(texts), bs):
                out.append(torch.sigmoid(self._logits(texts[start:start + bs], device)).cpu())
        return torch.cat(out)

    def select(self, texts, threshold=0.5, min_tools=3, device="cpu"):
        """Với mỗi câu, danh sách tên công cụ được chọn (theo thứ tự cố định để prompt giống nhau giữa các câu)."""
        chosen = []
        for probs in self.probabilities(texts, device):
            keep = {i for i, p in enumerate(probs.tolist()) if p > threshold}
            keep |= set(probs.argsort(descending=True)[:min_tools].tolist())
            chosen.append([self.tools[i] for i in sorted(keep)])
        return chosen

    # ------------------------------------------------------------------ lưu và nạp
    def save(self, directory):
        import torch
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.encoder.save_pretrained(directory / "encoder")
        self.tokenizer.save_pretrained(directory / "encoder")
        torch.save(self.head.state_dict(), directory / "head.pt")
        (directory / "tools.json").write_text(json.dumps({"tools": self.tools, "base": self.base}), encoding="utf-8")

    @classmethod
    def load(cls, directory):
        import torch
        directory = Path(directory)
        meta = json.loads((directory / "tools.json").read_text(encoding="utf-8"))
        retriever = cls(meta["tools"], meta["base"])
        retriever._init_modules(str(directory / "encoder"))
        retriever.head.load_state_dict(torch.load(directory / "head.pt", map_location="cpu"))
        return retriever.to("cpu").eval()


def retrieval_report(selected, cases):
    """Bộ chọn có giữ lại công cụ đúng không?
    recall: tỉ lệ câu mà công cụ đúng nằm trong tập được chọn (câu ngoài phạm vi luôn tính là đạt).
    avg_tools: số công cụ trung bình đưa vào prompt (càng ít càng nhanh)."""
    hit = [case["tool"] is None or case["tool"] in names for case, names in zip(cases, selected)]
    return {"recall": round(sum(hit) / len(hit), 3), "avg_tools": round(sum(map(len, selected)) / len(selected), 2)}
