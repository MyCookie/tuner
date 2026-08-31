"""`tuner smoke` (docs/03-components/smoke-test.md)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import click
import mlflow
import torch
from mlflow.tracking import MlflowClient
from peft import PeftModel
from pydantic import ValidationError
from transformers import AutoModelForCausalLM

from tuner.core.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from tuner.core.schemas import (
    IndexMap,
    SmokeGeneration,
    SmokeSample,
    SmokeTranscript,
    validate_gold,
)
from tuner.core.storage import StorageClient
from tuner.models.base import HFAuthError, ModelAdapter
from tuner.models.registry import get_adapter

ARTIFACTS_BUCKET = "tuner-artifacts"
GOLD_BUCKET = "tuner-gold"
TRAINER_STAGE = "trainer"


def _generate(
    model: Any, tokenizer: Any, prompt_messages: list[dict[str, str]], max_new_tokens: int
) -> str:
    """Greedy-decode one completion for `prompt_messages`, returning only the newly
    generated text (core logic 3-4: `temperature 0`, `max_new_tokens`)."""
    inputs = tokenizer.apply_chat_template(
        prompt_messages,
        tokenize=True,
        add_generation_prompt=True,
        return_tensors="pt",
        return_dict=True,
    )
    # apply_chat_template always returns CPU tensors -- moved onto the model's own
    # device so this also works for a quantized/CUDA-resident model, not just the
    # CPU-only tiny-test lane this suite exercises (PR #12 review round 1 finding 5).
    inputs = {k: v.to(model.device) for k, v in inputs.items()}
    with torch.no_grad():
        out = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
    generated = out[0][inputs["input_ids"].shape[1] :]
    return tokenizer.decode(generated, skip_special_tokens=True)


def smoke(
    run_id: str,
    config_path: str,
    storage: StorageClient | None = None,
) -> int:
    """Run the Smoke-test end to end; returns the process exit code (0/1/2/3)."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        click.echo(f"smoke: {exc}", err=True)
        return 2

    # Checked before any storage/HF/MLflow call, same reasoning as the Trainer's own
    # early MLFLOW_TRACKING_URI check (PR #11 review round 1 finding 5): attaching the
    # transcript to the Trainer's run is the whole point of this stage, so a missing
    # tracking URI must fail before any model download, not after.
    if not os.environ.get("MLFLOW_TRACKING_URI"):
        click.echo("smoke: MLFLOW_TRACKING_URI must be set", err=True)
        return 2

    try:
        adapter: ModelAdapter = get_adapter(config.model.adapter)
    except KeyError as exc:
        click.echo(f"smoke: {exc}", err=True)
        return 2

    # train.method: full writes {run_id}/model/ instead of {run_id}/adapter/ (02
    # §5.1/§5.3) -- the same config key the Trainer itself branches on to decide the
    # output subdir also decides which one Smoke-test reads back.
    out_subdir = "model" if config.train.method == "full" else "adapter"
    weights_prefix = f"{run_id}/{out_subdir}/"
    quantized = config.train.method == "qlora"

    # Checked before any storage/HF call, same reasoning and message as the
    # Trainer's own TRN-I-012 gate: bitsandbytes 4-bit quantization fundamentally
    # cannot run without a CUDA device (PR #12 review round 1 finding 4 -- smoke had
    # no equivalent to this Trainer check at all).
    if quantized and not torch.cuda.is_available():
        click.echo(
            "smoke: method qlora requires a CUDA device (bitsandbytes 4-bit "
            "quantization); none found -- see the host-venv fallback in "
            "05-infrastructure.md §3",
            err=True,
        )
        return 2

    storage = storage or StorageClient()

    with tempfile.TemporaryDirectory() as work_dir_str:
        # Wraps the whole run body, not just the specific exceptions already caught
        # below -- a mid-run failure (storage, MLflow, generation) used to escape as
        # a raw traceback instead of the "smoke: ..." message every other failure
        # mode gets, unlike the Trainer's own equivalent outer try (PR #12 review
        # round 1 finding 4).
        try:
            work_dir = Path(work_dir_str)
            weights_dir = work_dir / out_subdir
            storage.download_dir(ARTIFACTS_BUCKET, weights_prefix, weights_dir)
            # Checked before load_tokenizer/load_base_model below -- "no model
            # download happens" for this case (SMK-I-005), same discrimination fix
            # TRN-I-006 needed (PR #11 review round 1 finding 2).
            if not any(weights_dir.rglob("*")):
                click.echo(
                    f"smoke: trainer has not completed for this run ID "
                    f"(missing s3://{ARTIFACTS_BUCKET}/{weights_prefix})",
                    err=True,
                )
                return 2

            index_map_raw = storage.read_json(ARTIFACTS_BUCKET, f"{run_id}/tokens/index_map.json")
            if index_map_raw is None:
                click.echo(f"smoke: missing tokens/index_map.json for run {run_id}", err=True)
                return 2
            try:
                index_map = IndexMap.model_validate(index_map_raw)
            except ValidationError as exc:
                click.echo(f"smoke: invalid index_map.json: {exc}", err=True)
                return 2

            eval_entries = index_map.splits.eval
            if not eval_entries:
                click.echo(f"smoke: no eval-split records for run {run_id}", err=True)
                return 3

            num_prompts = config.smoke.num_prompts
            if len(eval_entries) < num_prompts:
                click.echo(
                    f"smoke: only {len(eval_entries)} eval record(s) available, requested "
                    f"{num_prompts} -- using all available",
                    err=True,
                )
            selected = eval_entries[:num_prompts]
            wanted_ids = {entry.record_id for entry in selected}

            gold_records = {}
            for raw in storage.read_jsonl(GOLD_BUCKET, f"{run_id}/"):
                if raw.get("id") in wanted_ids:
                    gold_records[raw["id"]] = validate_gold(raw)
            missing = wanted_ids - gold_records.keys()
            if missing:
                click.echo(f"smoke: eval record(s) not found in Gold: {sorted(missing)}", err=True)
                return 1

            try:
                hf_tokenizer = adapter.load_tokenizer()
                base_model = adapter.load_base_model(quantized=quantized)
            except HFAuthError as exc:
                click.echo(f"smoke: {exc}", err=True)
                return 2
            base_model.eval()

            max_new_tokens = config.smoke.max_new_tokens
            prompts: dict[str, tuple[list[dict[str, str]], str, str]] = {}
            for entry in selected:
                record = gold_records[entry.record_id]
                # to_chat_messages, not a hand-rolled re-join of the raw conversation
                # -- prompt_messages is exactly what the model is prompted with (02
                # §5.3).
                messages = adapter.to_chat_messages(record.conversation)
                prompt_messages, reference = messages[:-1], messages[-1]["content"]
                base_output = _generate(base_model, hf_tokenizer, prompt_messages, max_new_tokens)
                prompts[entry.record_id] = (prompt_messages, reference, base_output)

            if config.train.method == "full":
                # No PEFT adapter for full FT -- the saved weights *are* the tuned
                # model (02 §5.3's train.method: full clarification).
                tuned_model = AutoModelForCausalLM.from_pretrained(str(weights_dir))
            else:  # pragma: no cover -- GPU-only (PEFT/QLoRA); manual T15 check (08 smoke.md)
                tuned_model = PeftModel.from_pretrained(base_model, str(weights_dir))
            tuned_model.eval()

            samples = [
                SmokeSample(
                    record_id=entry.record_id,
                    prompt_messages=prompts[entry.record_id][0],
                    reference=prompts[entry.record_id][1],
                    base_output=prompts[entry.record_id][2],
                    tuned_output=_generate(
                        tuned_model, hf_tokenizer, prompts[entry.record_id][0], max_new_tokens
                    ),
                )
                for entry in selected
            ]

            transcript = SmokeTranscript(
                run_id=run_id,
                model_version=f"{adapter.name}-{run_id}",
                generation=SmokeGeneration(max_new_tokens=max_new_tokens, strategy="greedy"),
                samples=samples,
            )
            transcript_dict = transcript.model_dump(mode="json")

            mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])
            runs = mlflow.search_runs(
                search_all_experiments=True,
                filter_string=(
                    f"tags.`tuner.run_id` = '{run_id}' and tags.`tuner.stage` = '{TRAINER_STAGE}'"
                ),
                output_format="list",
            )
            # Exactly one run must match (03-components/smoke-test.md Output; the
            # pair filter, not tuner.run_id alone, per 01 §7) -- checked before
            # anything is written to storage so a bad lookup never leaves a
            # committed transcript dangling without its MLflow attachment.
            if len(runs) != 1:
                click.echo(
                    f"smoke: expected exactly one trainer run for run_id {run_id}, "
                    f"found {len(runs)}",
                    err=True,
                )
                return 2

            storage.delete_prefix(ARTIFACTS_BUCKET, f"{run_id}/smoke/")
            storage.write_json(ARTIFACTS_BUCKET, f"{run_id}/smoke/transcript.json", transcript_dict)

            local_transcript_path = work_dir / "transcript.json"
            local_transcript_path.write_text(json.dumps(transcript_dict))
            MlflowClient().log_artifact(
                runs[0].info.run_id, str(local_transcript_path), artifact_path="smoke"
            )
        except Exception as exc:  # unexpected mid-run failure -> exit 1
            click.echo(f"smoke: {exc}", err=True)
            return 1

    return 0


@click.command(name="smoke")
@click.option("--run-id", required=True, help="Run ID shared across the pipeline.")
@click.option(
    "--config",
    "config_path",
    default=str(DEFAULT_CONFIG_PATH),
    show_default=True,
    help="Pipeline config path.",
)
def smoke_command(run_id: str, config_path: str) -> None:
    """Generate before/after transcripts proving the trained model changed behavior."""
    sys.exit(smoke(run_id, config_path))
