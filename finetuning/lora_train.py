"""Fine-tune bằng LoRA, rồi gộp LoRA vào model gốc để đổi sang GGUF được.

LoRA là gì (tóm tắt): đóng băng toàn bộ trọng số gốc, chỉ học thêm vài ma trận rất nhỏ gắn vào các lớp
tuyến tính. Rất tiết kiệm bộ nhớ nên dạy được model 0.5B-1.7B trên GPU 12 GB.

Hàm chính:  train_lora(...)  ->  lưu model đã gộp vào out_dir và trả về nhật ký huấn luyện.
"""
import json
import math
import random
import re
import time
from collections import Counter
from pathlib import Path

def build_example(tok, messages, max_len, tools=None):
    """Trả về (input_ids, labels) hoặc None nếu quá dài. labels = -100 ở phần prompt, nên chỉ học phần trả lời.
    tools: danh sách tool (định dạng OpenAI) để chat template tự chèn khối <tools> như llama-server --jinja
    (dùng khi train đầu ra <tool_call> native). Không có tools thì giống hệt bản cũ."""
    prompt = tok.apply_chat_template(messages[:2], tools=tools, add_generation_prompt=True, tokenize=False)
    p_ids = tok(prompt, add_special_tokens=False)["input_ids"]
    a_ids = tok(messages[2]["content"] + tok.eos_token, add_special_tokens=False)["input_ids"]
    if len(p_ids) + len(a_ids) > max_len:
        return None
    return p_ids + a_ids, [-100] * len(p_ids) + a_ids


def collate(batch, pad_id):
    """Ghép nhiều mẫu thành một lô: đệm cho cùng độ dài, phần đệm không tính vào loss."""
    n = max(len(x[0]) for x in batch)
    ids = [x[0] + [pad_id] * (n - len(x[0])) for x in batch]
    lab = [x[1] + [-100] * (n - len(x[1])) for x in batch]
    att = [[1] * len(x[0]) + [0] * (n - len(x[0])) for x in batch]
    return ids, lab, att


def load_examples(tokenizer, data_path, max_len):
    """Đọc train.jsonl thành (input_ids, labels). Bỏ các mẫu dài hơn max_len token.
    Trả thêm `groups`: khoá câu nói của user cho từng mẫu (cùng câu lặp lại với trạng thái máy khác -> cùng khoá),
    để tách val theo câu chứ không theo dòng."""
    examples, groups, skipped = [], [], 0
    for line in open(data_path, encoding="utf-8"):
        if not line.strip():
            continue
        row = json.loads(line)
        messages = row["messages"]
        ex = build_example(tokenizer, messages, max_len, tools=row.get("tools"))
        if ex is None:
            skipped += 1
        else:
            examples.append(ex)
            groups.append(_utterance_key(messages[1]["content"]))
    return examples, groups, skipped


def _utterance_key(user_content):
    """Câu nói của user (bỏ số, chữ thường) làm khoá nhóm; nếu nội dung không phải JSON thì dùng nguyên văn."""
    try:
        text = json.loads(user_content).get("user_prompt", user_content)
    except (ValueError, AttributeError):
        text = user_content.split("\n", 1)[-1] if user_content.startswith("State:") else user_content
    return re.sub(r"\d+", "#", re.sub(r"[^\w\s]", "", str(text).lower())).strip()


def count_parameters(model):
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    return trainable, sum(p.numel() for p in model.parameters())


def cosine_schedule(optimizer, total_steps, warmup_fraction=0.05):
    """Tốc độ học tăng nhanh lúc đầu (warmup) rồi giảm dần theo hình cosine."""
    import torch
    warm = max(1, int(warmup_fraction * total_steps))

    def factor(step):
        if step < warm:
            return (step + 1) / warm
        return 0.5 * (1 + math.cos(math.pi * (step - warm) / max(1, total_steps - warm)))

    return torch.optim.lr_scheduler.LambdaLR(optimizer, factor)


def train_lora(base, data_path, out_dir, *, r=64, alpha=128, dropout=0.05, lr=2e-4, epochs=3, bs=1, accum=16,
               max_len=1800, seed=0, log_every=10, grad_checkpointing=False,
               val_ratio=0.0, eval_every=30, patience=3, min_delta=1e-4):
    """Dạy model `base` (tên trên Hugging Face hoặc thư mục) bằng LoRA, gộp rồi lưu vào out_dir.

    r, alpha        kích thước LoRA (r lớn = học được nhiều hơn, tốn hơn). alpha/r thường là 2.
    bs, accum       batch thật = bs x accum. Nếu hết bộ nhớ GPU thì giữ bs=1 và bật grad_checkpointing.
    Trả về dict nhật ký: bước, loss, thời gian, số tham số học.
    """
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer

    random.seed(seed)
    torch.manual_seed(seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cpu":
        print("CẢNH BÁO: không có GPU, sẽ rất chậm.")

    tokenizer = AutoTokenizer.from_pretrained(base)
    pad_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id
    model = AutoModelForCausalLM.from_pretrained(base, dtype=torch.float32).to(device)

    # Gắn LoRA vào mọi lớp tuyến tính (trừ lớp đầu ra), đúng kiểu thường dùng cho model nhỏ.
    model = get_peft_model(model, LoraConfig(r=r, lora_alpha=alpha, lora_dropout=dropout,
                                              target_modules="all-linear", task_type="CAUSAL_LM"))
    if grad_checkpointing:
        model.gradient_checkpointing_enable()
        model.enable_input_require_grads()
    trainable, total = count_parameters(model)
    print(f"Tham số học được: {trainable/1e6:.1f}M trên tổng {total/1e6:.0f}M ({100*trainable/total:.1f}%)")

    examples, groups, skipped = load_examples(tokenizer, data_path, max_len)
    val_examples = []
    if not (val_ratio > 0 and len(examples) >= 20):
        random.shuffle(examples)          # giữ y hệt thứ tự ngẫu nhiên cũ khi không tách val (kết quả train cũ tái lập được)
        print(f"{len(examples)} mẫu (bỏ {skipped} mẫu quá dài)")
    else:
        # Tách theo CÂU NÓI: mọi bản lặp của một câu (khác trạng thái máy / khác số) nằm cùng phía,
        # để val loss đo trên câu model chưa thấy, không lạc quan.
        size = Counter(groups)
        keys = sorted(size)
        random.shuffle(keys)
        val_keys, n_val = set(), 0
        for k in keys:
            if n_val >= len(examples) * val_ratio:
                break
            val_keys.add(k)
            n_val += size[k]
        val_examples = [e for e, g in zip(examples, groups) if g in val_keys]
        examples = [e for e, g in zip(examples, groups) if g not in val_keys]
        random.shuffle(examples)
        print(f"{len(examples)} mẫu train | {len(val_examples)} mẫu val, {len(val_keys)} câu không trùng train "
              f"(bỏ {skipped} mẫu quá dài)")

    def eval_val_loss():
        model.eval()
        losses = []
        with torch.no_grad():
            for i in range(0, len(val_examples), bs):
                ids, labels, mask = (torch.tensor(x, device=device) for x in collate(val_examples[i:i + bs], pad_id))
                with torch.autocast(device, dtype=torch.bfloat16, enabled=(device == "cuda")):
                    losses.append(model(input_ids=ids, attention_mask=mask, labels=labels).loss.item())
        model.train()
        return sum(losses) / max(len(losses), 1)

    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=lr, weight_decay=0.0)
    steps_per_epoch = math.ceil(len(examples) / (bs * accum))
    total_steps = steps_per_epoch * epochs
    schedule = cosine_schedule(optimizer, total_steps)

    history = {"step": [], "loss": [], "val_step": [], "val_loss": [], "early_stopped_at": None}
    best_val, best_state, no_improve, stop_early = float("inf"), None, 0, False
    model.train()
    start, step = time.time(), 0
    for epoch in range(epochs):
        random.shuffle(examples)
        batches = [examples[i:i + bs] for i in range(0, len(examples), bs)]
        window = []
        for index, batch in enumerate(batches):
            ids, labels, mask = (torch.tensor(x, device=device) for x in collate(batch, pad_id))
            with torch.autocast(device, dtype=torch.bfloat16, enabled=(device == "cuda")):
                loss = model(input_ids=ids, attention_mask=mask, labels=labels).loss
            (loss / accum).backward()
            window.append(loss.item())
            if (index + 1) % accum == 0 or index == len(batches) - 1:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                schedule.step()
                optimizer.zero_grad(set_to_none=True)
                step += 1
                if step % log_every == 0 or step == total_steps:
                    mean_loss = sum(window) / len(window)
                    history["step"].append(step)
                    history["loss"].append(mean_loss)
                    print(f"vòng {epoch+1}/{epochs} | bước {step}/{total_steps} | loss {mean_loss:.4f} | {time.time()-start:.0f}s")
                    window = []
                if val_examples and (step % eval_every == 0 or step == total_steps):
                    vloss = eval_val_loss()
                    history["val_step"].append(step)
                    history["val_loss"].append(vloss)
                    if vloss < best_val - min_delta:
                        best_val = vloss
                        no_improve = 0
                        best_state = {k: v.detach().cpu().clone() for k, v in model.named_parameters() if v.requires_grad}
                        print(f"  [val] bước {step}: val_loss = {vloss:.4f} (tốt nhất mới)")
                    else:
                        no_improve += 1
                        print(f"  [val] bước {step}: val_loss = {vloss:.4f} (không cải thiện {no_improve}/{patience})")
                        if no_improve >= patience:
                            history["early_stopped_at"] = step
                            print(f"  [Early Stopping] Dừng sớm tại bước {step}, khôi phục trọng số tốt nhất (val_loss={best_val:.4f}).")
                            stop_early = True
                            break
        if stop_early:
            break

    if best_state is not None:
        with torch.no_grad():
            for k, v in model.named_parameters():
                if k in best_state:
                    v.copy_(best_state[k].to(v.device))

    # Gộp LoRA vào trọng số gốc: sau bước này model là một model thường, không cần thư viện peft nữa.
    model = model.merge_and_unload().eval()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(out, safe_serialization=True)
    tokenizer.save_pretrained(out)
    history.update(seconds=round(time.time() - start), trainable_params=trainable, total_params=total,
                   examples=len(examples) + len(val_examples),
                   settings=dict(base=base, r=r, alpha=alpha, lr=lr, epochs=epochs, bs=bs, accum=accum,
                                 val_ratio=val_ratio, eval_every=eval_every, patience=patience))
    (out / "training_log.json").write_text(json.dumps(history, indent=1), encoding="utf-8")
    print("Đã lưu model đã gộp:", out)
    return history
