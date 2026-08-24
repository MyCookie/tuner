"""`tuner train` (docs/03-components/trainer.md)."""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import accelerate
import click
import mlflow
import peft
import torch
import transformers
from peft import LoraConfig, get_peft_model
from pydantic import ValidationError
from safetensors.torch import load_file as st_load_file
from transformers import Trainer, TrainerCallback, TrainingArguments, default_data_collator

from tuner.core.config import DEFAULT_CONFIG_PATH, ConfigError, load_config, merge_hyperparameters
from tuner.core.schemas import IndexMap, RegistryEval, RegistryManifest
from tuner.core.storage import StorageClient
from tuner.models.base import HFAuthError, ModelAdapter
from tuner.models.registry import get_adapter

ARTIFACTS_BUCKET = "tuner-artifacts"
REGISTRY_BUCKET = "tuner-registry"
STAGE = "trainer"
SEED = 42


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _TensorDataset(torch.utils.data.Dataset):
    """Wraps the SafeTensors-loaded tensors directly -- input_ids/attention_mask/labels
    are already final (padded per split, int64), so no collator logic beyond stacking
    is needed (tokenizer.md step 4 / trainer.md core logic 4)."""

    def __init__(self, tensors: dict[str, torch.Tensor]) -> None:
        self._tensors = tensors
        self._length = tensors["input_ids"].shape[0]

    def __len__(self) -> int:
        return self._length

    def __getitem__(self, index: int) -> dict[str, torch.Tensor]:
        return {key: value[index] for key, value in self._tensors.items()}


class _MLflowStepLogger(TrainerCallback):
    """Forwards HF Trainer's own periodic logs (loss every `logging_steps`, eval
    metrics at each `eval_strategy` point) into the already-open MLflow run, rather
    than relying on transformers' own mlflow integration and its separate
    run-management assumptions (core logic 6: "loss logged to MLflow every 10
    steps")."""

    def on_log(
        self, args: Any, state: Any, control: Any, logs: dict | None = None, **kwargs: Any
    ) -> None:
        if not logs:
            return
        metrics = {k: v for k, v in logs.items() if isinstance(v, int | float)}
        if metrics:
            mlflow.log_metrics(metrics, step=state.global_step)


def build_lora_config(hyperparameters: dict[str, Any]) -> LoraConfig:
    """Build the PEFT `LoraConfig` from the merged hyperparameters (adapter defaults <
    config overrides, 01 §6 precedence) -- a pure function, no model touched, so it's
    testable without CUDA/bitsandbytes (TRN-I-002; core logic 3)."""
    return LoraConfig(
        r=hyperparameters["lora_r"],
        lora_alpha=hyperparameters["lora_alpha"],
        lora_dropout=hyperparameters["lora_dropout"],
        target_modules=list(hyperparameters["lora_target_modules"]),
    )


def train(
    run_id: str,
    config_path: str,
    storage: StorageClient | None = None,
) -> int:
    """Run the Trainer end to end; returns the process exit code (0/1/2)."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        click.echo(f"train: {exc}", err=True)
        return 2

    try:
        adapter: ModelAdapter = get_adapter(config.model.adapter)
    except KeyError as exc:
        click.echo(f"train: {exc}", err=True)
        return 2

    # Checked immediately after resolving the adapter, before any storage or HF call --
    # "no model download happens" for this case (TRN-I-006).
    if config.train.method == "full" and not adapter.supports_full_ft:
        click.echo(
            f"train: {adapter.name} does not support full fine-tuning "
            "(SAS §3.1 sanctioned-models policy) -- use method: qlora",
            err=True,
        )
        return 2

    quantized = config.train.method == "qlora"
    # Also checked this early (not just right before load_base_model): bitsandbytes
    # 4-bit quantization fundamentally cannot run without a CUDA device, so there is
    # no more point touching storage first than there is for the supports_full_ft
    # check above (TRN-I-012).
    if quantized and not torch.cuda.is_available():
        click.echo(
            "train: method qlora requires a CUDA device (bitsandbytes 4-bit "
            "quantization); none found -- see the host-venv fallback in "
            "05-infrastructure.md §3",
            err=True,
        )
        return 2

    try:
        merged = merge_hyperparameters(adapter.training_defaults, config.train.hyperparameters)
    except ConfigError as exc:
        click.echo(f"train: {exc}", err=True)
        return 2

    storage = storage or StorageClient()

    index_map_raw = storage.read_json(ARTIFACTS_BUCKET, f"{run_id}/tokens/index_map.json")
    if index_map_raw is None:
        click.echo(f"train: missing tokens/index_map.json for run {run_id}", err=True)
        return 2
    try:
        index_map = IndexMap.model_validate(index_map_raw)
    except ValidationError as exc:
        click.echo(f"train: invalid index_map.json: {exc}", err=True)
        return 2

    if index_map.adapter != config.model.adapter:
        click.echo(
            f"train: tokens were built for adapter {index_map.adapter!r}, config "
            f"selects {config.model.adapter!r} -- tensors were built for a different "
            "model, re-run tokenize first",
            err=True,
        )
        return 2

    model_version = f"{adapter.name}-{run_id}"

    try:
        hf_tokenizer = adapter.load_tokenizer()
        base_model = adapter.load_base_model(quantized=quantized)
    except HFAuthError as exc:
        click.echo(f"train: {exc}", err=True)
        return 2

    if quantized:  # pragma: no cover -- GPU-only (bitsandbytes); TRN-G-020, T15
        model = get_peft_model(base_model, build_lora_config(merged))
    else:
        model = base_model

    with tempfile.TemporaryDirectory() as work_dir_str:
        work_dir = Path(work_dir_str)
        tokens_dir = work_dir / "tokens"
        storage.download_dir(ARTIFACTS_BUCKET, f"{run_id}/tokens/", tokens_dir)

        train_tensors = st_load_file(str(tokens_dir / "train.safetensors"))
        eval_tensors = st_load_file(str(tokens_dir / "eval.safetensors"))
        train_dataset = _TensorDataset(train_tensors)
        has_eval = eval_tensors["input_ids"].shape[0] > 0
        eval_dataset = _TensorDataset(eval_tensors) if has_eval else None

        mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])
        mlflow.set_experiment(config.train.mlflow_experiment)

        out_subdir = "model" if config.train.method == "full" else "adapter"

        try:
            with mlflow.start_run(run_name=run_id) as run:
                try:
                    mlflow.set_tags(
                        {
                            "tuner.run_id": run_id,
                            "tuner.model_version": model_version,
                            "tuner.adapter": adapter.name,
                            "tuner.stage": STAGE,
                        }
                    )
                    mlflow.log_params(merged)
                    mlflow.log_params(
                        {
                            "method": config.train.method,
                            "gold_manifest_uri": index_map.gold_manifest_uri,
                            "index_map_uri": (
                                f"s3://{ARTIFACTS_BUCKET}/{run_id}/tokens/index_map.json"
                            ),
                        }
                    )
                    mlflow.log_params(
                        {
                            "pkg.torch": torch.__version__,
                            "pkg.transformers": transformers.__version__,
                            "pkg.peft": peft.__version__,
                            "pkg.accelerate": accelerate.__version__,
                        }
                    )

                    training_args = TrainingArguments(
                        output_dir=str(work_dir / "checkpoints"),
                        num_train_epochs=merged["epochs"],
                        per_device_train_batch_size=merged["per_device_batch_size"],
                        per_device_eval_batch_size=merged["per_device_batch_size"],
                        gradient_accumulation_steps=merged["gradient_accumulation_steps"],
                        learning_rate=merged["learning_rate"],
                        warmup_ratio=merged["warmup_ratio"],
                        bf16=True,
                        gradient_checkpointing=True,
                        logging_steps=10,
                        eval_strategy="epoch" if has_eval else "no",
                        save_strategy="no",
                        seed=SEED,
                        report_to=[],
                    )
                    hf_trainer = Trainer(
                        model=model,
                        args=training_args,
                        train_dataset=train_dataset,
                        eval_dataset=eval_dataset,
                        data_collator=default_data_collator,
                        callbacks=[_MLflowStepLogger()],
                    )

                    train_result = hf_trainer.train()
                    final_train_loss = train_result.metrics.get("train_loss", float("nan"))
                    if has_eval:
                        eval_metrics = hf_trainer.evaluate()
                        final_eval_loss = eval_metrics.get("eval_loss", final_train_loss)
                    else:
                        # No eval split -- nothing to report separately; RegistryEval
                        # requires a concrete float, so mirror the train loss rather
                        # than inventing a value that implies eval actually ran.
                        final_eval_loss = final_train_loss

                    final_dir = work_dir / out_subdir
                    model.save_pretrained(str(final_dir), safe_serialization=True)
                    hf_tokenizer.save_pretrained(str(final_dir))

                    storage.delete_prefix(ARTIFACTS_BUCKET, f"{run_id}/{out_subdir}/")
                    storage.upload_dir(ARTIFACTS_BUCKET, f"{run_id}/{out_subdir}/", final_dir)

                    registry_manifest = RegistryManifest(
                        model_version=model_version,
                        run_id=run_id,
                        adapter_name=adapter.name,
                        base_model=adapter.hf_model_id,
                        method=config.train.method,
                        created_at=_utc_now(),
                        gold_manifest_uri=index_map.gold_manifest_uri,
                        index_map_uri=f"s3://{ARTIFACTS_BUCKET}/{run_id}/tokens/index_map.json",
                        weights_uri=f"s3://{ARTIFACTS_BUCKET}/{run_id}/{out_subdir}/",
                        mlflow_run_id=run.info.run_id,
                        hyperparameters=merged,
                        eval=RegistryEval(
                            final_train_loss=final_train_loss, final_eval_loss=final_eval_loss
                        ),
                        status="candidate",
                    )
                    storage.write_json(
                        REGISTRY_BUCKET,
                        f"{model_version}/manifest.json",
                        registry_manifest.model_dump(mode="json"),
                    )
                except Exception:
                    # Traceback logged while the run is still active; re-raising lets
                    # mlflow.start_run's own __exit__ mark the run FAILED (core logic 9).
                    tb_path = work_dir / "traceback.txt"
                    tb_path.write_text(traceback.format_exc())
                    mlflow.log_artifact(str(tb_path))
                    raise
        except Exception as exc:  # unexpected mid-run failure -> exit 1
            click.echo(f"train: {exc}", err=True)
            return 1

    return 0


@click.command(name="train")
@click.option("--run-id", required=True, help="Run ID shared across the pipeline.")
@click.option(
    "--config",
    "config_path",
    default=str(DEFAULT_CONFIG_PATH),
    show_default=True,
    help="Pipeline config path.",
)
def train_command(run_id: str, config_path: str) -> None:
    """Fine-tune the selected adapter's base model on tokenized Gold data."""
    sys.exit(train(run_id, config_path))
