#!/Users/chrischo/server/brain/.venv/bin/python3
"""Fine-tune multilingual-e5 with LoRA adapter on Chris's feedback data.

Uses sentence-transformers + peft library. Trains entirely locally on CPU.
Output: logs/training/lora_<version>/ directory containing the adapter.

Must run from the brain venv — sentence-transformers/peft/torch are only
installed there, not in the system Python.

Usage:
  brain_finetune.py --pairs logs/training/pairs_*.jsonl --output logs/training/lora_v1/
  brain_finetune.py --dry-run  # just validate pairs, don't train
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))


BASE_MODEL = "intfloat/multilingual-e5-large-instruct"
LORA_RANK = 8
LEARNING_RATE = 2e-5
EPOCHS = 3
BATCH_SIZE = 16
MIN_PAIRS = 50  # smoke-test threshold; production should be ~100+


def load_pairs(pattern: str) -> list[dict]:
    """Load training pairs from JSONL file(s)."""
    files = sorted(glob.glob(pattern))
    if not files:
        return []
    pairs = []
    for f in files:
        with open(f) as fh:
            for line in fh:
                try:
                    pairs.append(json.loads(line))
                except Exception:
                    continue
    return pairs


def build_training_dataset(pairs: list[dict]):
    """Convert pairs into a HuggingFace Dataset for SentenceTransformerTrainer.

    Schema: {"anchor": query, "positive": positive_doc}. The trainer's
    MultipleNegativesRankingLoss treats other anchors in the same batch as
    in-batch negatives.
    """
    from datasets import Dataset

    anchors: list[str] = []
    positives: list[str] = []
    for pair in pairs:
        if pair.get("label") != "useful":
            continue
        query = (pair.get("query") or "").strip()
        positive = (pair.get("positive") or "").strip() or (pair.get("positive_content") or "").strip()
        if not query or not positive:
            continue
        # E5 asymmetric prefixes must live inside the training text.
        anchors.append(f"query: {query}")
        positives.append(f"passage: {positive}")
    if not anchors:
        return None
    return Dataset.from_dict({"anchor": anchors, "positive": positives})


def train(pairs_pattern: str, output_dir: Path, dry_run: bool = False) -> dict:
    """Main training loop using v3+ SentenceTransformerTrainer with LoRA."""
    pairs = load_pairs(pairs_pattern)
    if not pairs:
        return {"status": "error", "reason": f"no pairs found matching {pairs_pattern}"}

    positive_count = sum(1 for p in pairs if p.get("label") == "useful")
    print(f"Loaded {len(pairs)} pairs ({positive_count} positive)")

    if positive_count < MIN_PAIRS:
        return {
            "status": "insufficient_data",
            "positive_pairs": positive_count,
            "minimum_needed": MIN_PAIRS,
        }

    if dry_run:
        return {
            "status": "dry_run_ok",
            "positive_pairs": positive_count,
            "ready_to_train": True,
        }

    try:
        import torch
        from sentence_transformers import SentenceTransformer
        from sentence_transformers.sentence_transformer import losses as st_losses
        from sentence_transformers.sentence_transformer.trainer import SentenceTransformerTrainer
        from sentence_transformers.sentence_transformer.training_args import SentenceTransformerTrainingArguments
    except ImportError as e:
        return {"status": "error", "reason": f"sentence-transformers v3+ API missing: {e}"}

    try:
        from peft import LoraConfig, TaskType
    except ImportError:
        return {"status": "error", "reason": "peft not installed"}

    print(f"Loading base model: {BASE_MODEL}")
    # fp32 forced — fp16 on MPS underflows in the L2 normalization layer
    # during training and produces NaN embeddings at inference time.
    model = SentenceTransformer(BASE_MODEL, model_kwargs={"torch_dtype": torch.float32})

    # Add LoRA via the native sentence-transformers API. Assigning a
    # PeftModel back to model[0].auto_model would trigger the Transformer
    # module's setter which silently unwraps PeftModel; model.add_adapter()
    # injects the LoRA modules in-place and preserves them through training.
    lora_config = LoraConfig(
        r=LORA_RANK,
        lora_alpha=16,
        target_modules=["query", "key", "value"],
        lora_dropout=0.05,
        bias="none",
        task_type=TaskType.FEATURE_EXTRACTION,
    )
    try:
        model.add_adapter(lora_config)
        trainable = sum(p.numel() for p in model[0].auto_model.parameters() if p.requires_grad)
        total = sum(p.numel() for p in model[0].auto_model.parameters())
        print(f"LoRA applied — trainable params: {trainable:,} / {total:,} ({100*trainable/total:.2f}%)")
    except Exception as e:
        return {"status": "error", "reason": f"add_adapter failed: {e}"}

    train_ds = build_training_dataset(pairs)
    if train_ds is None or len(train_ds) == 0:
        return {"status": "error", "reason": "no valid training examples"}
    print(f"Training on {len(train_ds)} examples for {EPOCHS} epochs")

    loss_fn = st_losses.MultipleNegativesRankingLoss(model)

    # SentenceTransformerTrainingArguments wraps HF TrainingArguments. We disable
    # fp16/bf16 explicitly because the base model must stay in fp32 for stable
    # L2 norm gradients on MPS.
    args = SentenceTransformerTrainingArguments(
        output_dir=str(output_dir / "_checkpoints"),
        num_train_epochs=EPOCHS,
        per_device_train_batch_size=BATCH_SIZE,
        learning_rate=LEARNING_RATE,
        warmup_ratio=0.1,
        fp16=False,
        bf16=False,
        logging_steps=5,
        save_strategy="no",  # we save explicitly at the end
        report_to=[],
    )

    trainer = SentenceTransformerTrainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        loss=loss_fn,
    )
    try:
        trainer.train()
    except Exception as e:
        return {"status": "error", "reason": f"training failed: {e}"}

    # Save the LoRA adapter ONLY (~5MB) by dumping the adapter state dict +
    # writing adapter_config.json alongside. The standard PeftModel.save_pretrained
    # doesn't fire because sentence-transformers mutates add_adapter in-place
    # rather than wrapping, so we serialize manually.
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        import json as _json
        from safetensors.torch import save_file
        adapter_state = model.get_adapter_state_dict()
        save_file(adapter_state, str(output_dir / "adapter_model.safetensors"))
        # LoraConfig.to_dict() leaves target_modules as a set and task_type as
        # an enum. JSON can't serialize either natively, so normalize them
        # before dumping. `default=str` is wrong here — it turns the set into
        # a literal string "{'query', ...}" that LoraConfig parses back as a
        # single module name.
        cfg_dict = lora_config.to_dict()
        if isinstance(cfg_dict.get("target_modules"), (set, frozenset)):
            cfg_dict["target_modules"] = sorted(cfg_dict["target_modules"])
        if cfg_dict.get("task_type") is not None:
            cfg_dict["task_type"] = str(cfg_dict["task_type"]).split(".")[-1]
        with (output_dir / "adapter_config.json").open("w") as f:
            _json.dump(cfg_dict, f, indent=2)
        # Record the base model name so load can reproduce the setup.
        with (output_dir / "base_model.txt").open("w") as f:
            f.write(BASE_MODEL)
        size_mb = (output_dir / "adapter_model.safetensors").stat().st_size / 1024 / 1024
        print(f"LoRA adapter saved to: {output_dir} ({size_mb:.2f} MB)")
    except Exception as e:
        return {"status": "error", "reason": f"save failed: {e}"}

    return {
        "status": "ok",
        "examples_trained": len(train_ds),
        "epochs": EPOCHS,
        "lora_rank": LORA_RANK,
        "output": str(output_dir),
    }


def main():
    parser = argparse.ArgumentParser(description="Fine-tune e5 with LoRA on feedback data")
    parser.add_argument("--pairs", default="/Users/chrischo/server/brain/logs/training/pairs_*.jsonl")
    parser.add_argument("--output", type=Path, default=Path("/Users/chrischo/server/brain/logs/training/lora_v1/"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Bypass BRAIN_FINETUNE_ENABLED check")
    args = parser.parse_args()

    if not args.dry_run and not args.force:
        try:
            from config import BRAIN_FINETUNE_ENABLED
        except ImportError:
            BRAIN_FINETUNE_ENABLED = os.environ.get("BRAIN_FINETUNE_ENABLED", "").lower() in ("1", "true", "yes")
        if not BRAIN_FINETUNE_ENABLED:
            print(json.dumps({
                "status": "disabled",
                "reason": "BRAIN_FINETUNE_ENABLED=false. Set the flag or pass --force.",
            }, indent=2))
            return 2

    result = train(args.pairs, args.output, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") in ("ok", "dry_run_ok") else 1


if __name__ == "__main__":
    sys.exit(main())
