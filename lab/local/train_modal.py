"""LoRA-дообучение открытой модели-судьи на метках учителя — на Modal (A100-80GB из кредитов).

На выходе — LoRA-адаптер в формате GGUF для llama.cpp (подключается к той же Q4-модели на маке флагом --lora)
и он же в формате PEFT (safetensors). Базовые веса запекаются в образ, чтобы не качать их на каждом запуске.

  .venv/bin/modal run lab/local/train_modal.py --data lab/local/data/train_qwen35.jsonl --out lab/local/adapters/qwen35-r1
  .venv/bin/modal run lab/local/train_modal.py --data ... --out ... --smoke      # 16 примеров, 4 шага — проверка пути

Траты: A100-80GB $2,50/ч из кредитов Modal; лимит трат аккаунта $0 — кончатся кредиты, обучение остановится.
Приложение эфемерное (modal run): контейнер гасится сразу после возврата результата, volumes не создаём.
"""
from __future__ import annotations

import json
import pathlib
import time

import modal

BASE_REPO = "Qwen/Qwen3.5-9B"
LLAMA_TAG = "b11669"  # та же сборка llama.cpp, что стоит на маке (llama-server --version)

app = modal.App("cqa-lora")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("git")
    .pip_install("torch", "transformers", "peft", "accelerate", "safetensors", "sentencepiece", "numpy",
                 "huggingface_hub[hf_transfer]", "flash-linear-attention")
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
    .run_commands(f"huggingface-cli download {BASE_REPO} --local-dir /base || hf download {BASE_REPO} --local-dir /base")
    .run_commands(f"git clone --depth 1 --branch {LLAMA_TAG} https://github.com/ggml-org/llama.cpp /llama.cpp",
                  "pip install -e /llama.cpp/gguf-py")
)


@app.function(image=image, gpu="A100-80GB", timeout=3 * 3600, max_containers=1)
def train(rows: list[dict], cfg: dict) -> dict:
    import subprocess

    import torch
    import transformers
    from peft import LoraConfig, get_peft_model
    from transformers import AutoTokenizer, Trainer, TrainingArguments

    t0 = time.time()
    log = {"versions": {"torch": torch.__version__, "transformers": transformers.__version__}}
    tok = AutoTokenizer.from_pretrained("/base")
    model = None
    for cls_name in ("AutoModelForCausalLM", "AutoModelForImageTextToText"):
        try:
            cls = getattr(transformers, cls_name)
            model = cls.from_pretrained("/base", dtype=torch.bfloat16, device_map={"": 0})
            log["model_class"] = f"{cls_name} → {type(model).__name__}"
            break
        except Exception as e:  # noqa: BLE001
            log[f"load_{cls_name}"] = str(e)[:300]
    assert model is not None, log

    # обучающие примеры: потери только на ответе учителя
    feats, skipped = [], 0
    for r in rows:
        p = tok(r["prompt"], add_special_tokens=False)["input_ids"]
        c = tok(r["completion"], add_special_tokens=False)["input_ids"]
        if len(p) + len(c) > cfg["max_len"]:
            skipped += 1
            continue
        feats.append({"input_ids": p + c, "labels": [-100] * len(p) + c})
    lens = sorted(len(f["input_ids"]) for f in feats)
    log["data"] = {"n": len(feats), "skipped_long": skipped, "len_median": lens[len(lens) // 2], "len_max": lens[-1],
                   "tokens": sum(lens)}

    lcfg = LoraConfig(r=cfg["r"], lora_alpha=cfg["alpha"], lora_dropout=0.05, bias="none", task_type="CAUSAL_LM",
                      target_modules=r"^(?!.*visual).*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)$")
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    model = get_peft_model(model, lcfg)
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    log["trainable_params"] = trainable

    def collate(batch):
        n = max(len(b["input_ids"]) for b in batch)
        pad = tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id
        ids = torch.full((len(batch), n), pad)
        lab = torch.full((len(batch), n), -100)
        att = torch.zeros((len(batch), n), dtype=torch.long)
        for i, b in enumerate(batch):
            k = len(b["input_ids"])
            ids[i, :k] = torch.tensor(b["input_ids"])
            lab[i, :k] = torch.tensor(b["labels"])
            att[i, :k] = 1
        return {"input_ids": ids, "labels": lab, "attention_mask": att}

    import inspect
    import math
    steps = cfg.get("max_steps") or math.ceil(len(feats) / cfg["grad_accum"] * cfg["epochs"])
    kw = dict(output_dir="/tmp/out", per_device_train_batch_size=1, gradient_accumulation_steps=cfg["grad_accum"],
              num_train_epochs=cfg["epochs"], max_steps=cfg.get("max_steps", -1), learning_rate=cfg["lr"],
              lr_scheduler_type="cosine", warmup_steps=max(1, int(0.05 * steps)), bf16=True, logging_steps=5,
              save_strategy="no", report_to=[], remove_unused_columns=False, dataloader_num_workers=0,
              seed=cfg.get("seed", 1))
    accepted = inspect.signature(TrainingArguments.__init__).parameters
    log["dropped_args"] = [k for k in kw if k not in accepted]
    args = TrainingArguments(**{k: v for k, v in kw.items() if k in accepted})
    trainer = Trainer(model=model, args=args, train_dataset=feats, data_collator=collate)
    res = trainer.train()
    log["train"] = {"loss": res.training_loss, "runtime_s": round(res.metrics.get("train_runtime", 0)),
                    "history": [{k: v for k, v in h.items() if k in ("step", "loss", "learning_rate", "epoch")}
                                for h in trainer.state.log_history if "loss" in h]}

    out = pathlib.Path("/tmp/adapter")
    model.save_pretrained(out)
    conv = subprocess.run(["python", "/llama.cpp/convert_lora_to_gguf.py", "--base", "/base", "--outtype", "f16",
                           "--outfile", "/tmp/adapter.gguf", str(out)], capture_output=True, text=True)
    log["convert"] = {"rc": conv.returncode, "tail": (conv.stdout + conv.stderr)[-3000:]}
    files = {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()}
    if pathlib.Path("/tmp/adapter.gguf").exists():
        files["adapter.gguf"] = pathlib.Path("/tmp/adapter.gguf").read_bytes()
    log["wall_s"] = round(time.time() - t0)
    # только простые типы: локально torch нет, тензор в журнале не распакуется
    log = json.loads(json.dumps(log, default=lambda o: o.item() if hasattr(o, "item") else str(o)))
    return {"log": log, "files": files}


@app.local_entrypoint()
def main(data: str, out: str, smoke: bool = False, epochs: float = 2.0, lr: float = 1e-4, r: int = 16,
         grad_accum: int = 16, max_len: int = 6144):
    rows = [json.loads(x) for x in pathlib.Path(data).read_text().splitlines() if x.strip()]
    cfg = {"epochs": epochs, "lr": lr, "r": r, "alpha": 2 * r, "grad_accum": grad_accum, "max_len": max_len}
    if smoke:
        rows, cfg["max_steps"], cfg["grad_accum"] = rows[:16], 4, 4
    print(f"обучение: {len(rows)} примеров, {cfg}")
    t0 = time.time()
    res = train.remote(rows, cfg)
    dst = pathlib.Path(out)
    dst.mkdir(parents=True, exist_ok=True)
    for name, b in res["files"].items():
        (dst / name).write_bytes(b)
    log = {**res["log"], "cfg": cfg, "data": res["log"].get("data"), "source": data, "wall_local_s": round(time.time() - t0)}
    (dst / "train_log.json").write_text(json.dumps(log, ensure_ascii=False, indent=1))
    print(json.dumps({k: v for k, v in log.items() if k not in ("train",)}, ensure_ascii=False, indent=1)[:4000])
    tr = log.get("train", {})
    print("loss:", tr.get("loss"), "время обучения, с:", tr.get("runtime_s"))
    for h in tr.get("history", [])[-8:]:
        print("  ", h)
    print("файлы:", sorted(p.name for p in dst.iterdir()))
