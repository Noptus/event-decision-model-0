# Laya × Solace Event-Mesh Routing Lab

A small, fully local Apple Silicon proof of concept that specializes the official multilingual Laya decision model for **bilingual business-event routing**. It models a practical event-mesh gate: route an English or French JSON event to one of six consumer services, while preserving Laya's native typed `choice` distribution and flagging low-confidence decisions for review.

This is not a broker connector and requires no running Solace broker. Its topic shapes and routing concerns are modeled after a Solace event mesh: hierarchical topics, schema evolution, replay noise, legacy topic drift, and topic/payload conflicts.

> **Experimental only.** The 260 events are deterministic synthetic examples, not production data. The resulting model and confidence threshold are not production-ready.

For Solace integration, see the lightweight [Go MQTT bridge](solace-go/README.md). It keeps this checkpoint loaded in one persistent worker and includes a broker-free end-to-end demo. The concrete data, broker, ownership, hardware, and acceptance inputs needed from Solace are in [docs/solace-requirements.md](docs/solace-requirements.md).

## What is pinned

- Laya source: `NandhaKishorM/laya@fa9a2a7070b1789912a49ae24603bbfb1a78b001` (`0.3.24`)
- Base checkpoint: `convaiinnovations/laya-multilingual@e4e9ddf21a7b1903b7acffd8814ad4307bf63a67`
- Base checkpoint size: about 678 MB, 321.9M parameters
- Python: 3.11
- Resolved Python packages: `uv.lock`

The setup installs the pinned local Laya checkout in editable mode. Nothing is installed globally, and no credentials are required.

## System requirements

- Apple Silicon Mac recommended; CPU works as a slower correctness path
- Python 3.11 or 3.12
- `uv`
- At least 10 GiB free; 20–30 GiB free is a comfortable reserve for caches and atomic checkpoints
- Internet for the first source/package/checkpoint download

The reference run uses an Apple M4 Mac mini with 16 GiB unified memory. All caches, the virtual environment, source checkout, and downloaded weights stay beneath this directory.

## Setup

```bash
cd laya-mac-finetune
chmod +x scripts/*.sh scripts/*.py
./scripts/setup.sh
source scripts/env.sh
```

`setup.sh` verifies the source revision, creates `.venv`, resolves `uv.lock`, inspects the Mac, downloads only the pinned public model files, and verifies `torch`, `laya`, and an MPS tensor operation.

## Generate and validate the toy event mesh

```bash
source scripts/env.sh
uv run python scripts/generate_dataset.py
uv run python scripts/validate_dataset.py
uv run pytest -q
```

The generator writes exactly 160 train, 40 validation, and 60 test events. Every split contains all six routes and both languages. Test data includes a topic hierarchy absent from training for every class. Other challenge slices include missing optional fields, noisy replay text, ambiguous cross-domain events, and deliberately wrong legacy topics. The model only sees `state` and the typed question; labels and challenge metadata are never part of its features.

## Base inference

The same six-option question contract is used for base and fine-tuned models:

```bash
uv run python scripts/infer.py --model base --file examples/events.jsonl --verbose
```

## Smoke test

Five optimizer steps over a balanced 12-event subset verify the native forward/backward path, RLCD plus soft cross-entropy loss, atomic checkpoint save, reload, and inference:

```bash
uv run python scripts/train.py --mode smoke
uv run python scripts/infer.py --model outputs/smoke-model --event "$(uv run python -c 'import json; print(json.dumps(json.load(open("examples/event_en.json"))))')"
```

## Conservative experiment

```bash
uv run python scripts/train.py --mode experiment
uv run python scripts/evaluate.py
uv run pytest -q
```

The 16 GiB defaults are deliberately conservative:

- device `mps`, with an explicit warning before CPU fallback
- FP32 training for stability
- fixed 256-token batches with a 128-token question budget, micro-batch 1, gradient accumulation 8
- one process, no DataLoader workers, no `device_map`, CUDA stack, `torch.compile`, or LoRA
- frozen mmBERT encoder and trainable native Laya decision head
- at most 60 optimizer steps, validation every 10 steps, patience 3
- deterministic seeds, finite-loss/gradient checks, gradient clipping
- atomic best checkpoint and at most one recovery checkpoint

### Training objective and upstream deviations

The trainer preserves the official Apple script's native model construction, `build_sequence` tokenization, typed-choice markers, soft targets, four noisy logit samples, proper-scoring-rule RLCD reward (`w_sph=0.75`, `w_rps=1.0`), and full-weight soft cross-entropy. Temperature is fitted with Laya's calibration utility on validation only.

For a fast 16 GiB local proof of concept, this project changes the official demo by freezing the encoder, reducing context from 1024 to 256, using batch 1 with accumulation, evaluating/early-stopping by held-out accuracy and loss, and saving atomically. The frozen encoder stays in evaluation mode; no labels enter an encoder cache. The final review threshold uses Laya's native per-option-bucket abstention helper and never changes the returned route or probability distribution.

## Evaluation outputs

```text
outputs/metrics.json
outputs/predictions.jsonl
outputs/confusion_matrix.csv
outputs/run_manifest.json
outputs/calibration.json
outputs/training_history_smoke.json
outputs/training_history_experiment.json
outputs/training_history_refinement.json
outputs/training_attempts.json
outputs/solace_go_offline_demo.jsonl
outputs/best-model/          # ignored: large generated checkpoint; owns calibration.json
outputs/smoke-model/         # ignored: large generated checkpoint
outputs/recovery-model/      # ignored: at most one prior best
```

`metrics.json` reports base, uncalibrated fine-tuned, and calibrated fine-tuned accuracy; per-route/language/challenge accuracy; NLL; hard and soft-target Brier score; 10-bin ECE; mean confidence; review coverage; warm latency; and exact improvement/regression examples.

## Inference

One French event:

```bash
uv run python scripts/infer.py \
  --model outputs/best-model \
  --event '{"topic":"acme/fr/payment/failed","schema_name":"PaymentFailed","schema_version":"1.0","event_type":"payment.failed","payload":{"amount":120,"currency":"EUR","message":"Le paiement a été refusé"}}'
```

File/batched mode:

```bash
uv run python scripts/infer.py --model outputs/best-model --file examples/events.jsonl
```

Interactive mode:

```bash
uv run python scripts/infer.py --model outputs/best-model --interactive --verbose
```

Each result includes the selected route, all six probabilities, calibrated answer confidence, review status/threshold, checkpoint identity, actual device, and latency. `--verbose` includes the unmodified raw Laya answer object. Add `--no-review-gate` to inspect the model without the validation-fitted policy. A review policy is loaded only from the selected checkpoint's own `calibration.json`, and its identity plus question-contract hash must match; smoke and unrelated custom checkpoints never inherit `outputs/calibration.json`.

## Offline reruns

After `setup.sh` completes, training and inference use local paths only:

```bash
source scripts/env.sh
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
uv run python scripts/train.py --mode smoke
uv run python scripts/infer.py --model base --file examples/events.jsonl
```

Do not delete `models/laya-multilingual`, `upstream/laya`, `.venv`, or the local caches before an offline rerun.

## Force CPU

```bash
uv run python scripts/train.py --mode smoke --device cpu
uv run python scripts/evaluate.py --device cpu
uv run python scripts/infer.py --device cpu --model outputs/best-model --file examples/events.jsonl
```

CPU is much slower. If MPS cannot execute its tensor probe, scripts print a warning before selecting CPU; evaluation refuses an unannounced device change by the Laya loader.

## Unified-memory tuning

| Unified memory | Suggested settings |
|---|---|
| 8 GiB | `head_only`, max length 192, 20–30 steps; close other applications |
| 16 GiB | checked-in defaults: `head_only`, max length 256, 60 steps |
| 24 GiB | try `last_1`, max length 256, 80–100 steps |
| 32 GiB+ | try `last_2`, max length 384, up to 150 steps |

Change `configs/experiment.yaml`; always rerun smoke mode first after increasing trainable layers or context.

## MPS notes

- The first forward compiles Metal kernels and is excluded from warm latency.
- Some PyTorch operations can fall back to CPU because `PYTORCH_ENABLE_MPS_FALLBACK=1`; this is not the same as silently moving the whole model to CPU.
- Training stays FP32. Native Laya inference may use MPS FP16 autocast for sufficiently large batches.
- Fixed sequence lengths limit MPS graph-cache growth. `torch.mps.empty_cache()` is called only after evaluation/checkpoint boundaries.
- Peak-memory reporting depends on the installed PyTorch APIs; available MPS allocator values are recorded in training history.
- External-volume checkpoint saves can be slower than internal SSD writes.

## Observed results

Measured locally on the M4/16 GiB machine with PyTorch 2.14.1 and MPS:

| Metric (60 held-out events) | Untouched base | Fine-tuned, raw | Fine-tuned, calibrated |
|---|---:|---:|---:|
| Choice accuracy | 0.8333 | 0.8333 | 0.8333 |
| Negative log loss | 0.5260 | **0.4736** | 0.5002 |
| Soft-target cross-entropy | 1.1748 | **0.8659** | 0.8659 |
| Soft-target Brier | 0.1725 | **0.1423** | 0.1438 |
| 10-bin ECE | **0.0905** | 0.1227 | 0.1469 |
| Mean confidence | 0.8077 | 0.7475 | 0.7231 |
| Warm single-event median | 36.8 ms | — | 37.8 ms |

The selected checkpoint came from optimizer step 50, reached 0.925 validation accuracy, and took 118.8 seconds to train. Smoke training took 23.9 seconds. A separate 120-step-cap refinement attempt early-stopped at step 80 after 51.0 seconds without beating the selected checkpoint, so it was discarded; total measured search time was 169.9 seconds.

Accuracy did **not** improve on the held-out test set: there were zero corrected argmax decisions and zero argmax regressions. Fine-tuning still moved probability mass toward difficult true routes—the largest gain was `mesh-tes-0208`, where the true `order-processing` probability rose from 0.0067 to 0.1049—but did not flip the winner. It also softened some already-correct order predictions; the largest true-route decrease was 0.3119. Fraud-review recall remains weak at 0.30 for both models.

The validation-fitted review policy sends 6.7% of test events to review; accuracy among the remaining 93.3% is 0.8929. It does not catch every confidently wrong topic/payload conflict, so it must not be treated as a production safety boundary.

Temperature fitting illustrates the danger of a tiny calibration set. On the 40 validation events, the fitted `T=1.0811` improved the soft-target objective from 0.8337 to 0.8301, but worsened hard-label NLL (0.4027→0.4314) and ECE (0.1898→0.1973). On test it likewise softened confidence and worsened ECE relative to the uncalibrated fine-tuned checkpoint. Both versions are retained in `outputs/metrics.json`; no calibration benefit is claimed.

These are measured synthetic-data results, not estimates or evidence of production quality. Full per-route, language, challenge, confusion, latency, and exact example data are in `outputs/metrics.json`, `outputs/predictions.jsonl`, and `outputs/confusion_matrix.csv`.

The [Go Solace bridge](solace-go/README.md) was also built and exercised end to end without a broker. Its persistent Python worker loaded the local MPS checkpoint once, then processed the three demo messages in 61.4, 44.3, and 41.7 ms with no input truncation. The exact publish envelopes are in `outputs/solace_go_offline_demo.jsonl`; no live-broker result is claimed.

## Cleanup

Remove only generated/downloaded artifacts while preserving code, data, and metrics:

```bash
rm -rf .venv .cache .tmp models upstream outputs/best-model outputs/smoke-model outputs/recovery-model
```

To delete absolutely everything, leave this directory and remove `laya-mac-finetune` itself. Review the path before running either command.
