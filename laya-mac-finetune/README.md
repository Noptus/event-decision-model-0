# EDM-0: Laya × Solace Event-Mesh Decision Model

A local, reproducible Apple Silicon experiment that fine-tunes the official multilingual Laya decision model for bilingual enterprise event routing. The current **EDM-0 enterprise v2** model selects one of eight operational teams and returns Laya's native typed-choice probability distribution plus a validation-fitted review signal.

The project also includes a lightweight Go bridge with two optional broker transports:

- MQTT 3.1.1 through Eclipse Paho in the default CGO-free build.
- Native Solace SMF Guaranteed Messaging through the official Go SDK in the `smf` build.

See [solace-go/README.md](solace-go/README.md) for integration commands and [docs/solace-requirements.md](docs/solace-requirements.md) for the data, broker, ownership, hardware, and acceptance requirements for a real pilot.

> **Experimental only.** All 10,000 current events are deterministic synthetic examples, not production data. The metrics and review threshold are not production-readiness evidence.

## Pinned components

- Laya source: `NandhaKishorM/laya@fa9a2a7070b1789912a49ae24603bbfb1a78b001` (`0.3.24`)
- Base checkpoint: `convaiinnovations/laya-multilingual@e4e9ddf21a7b1903b7acffd8814ad4307bf63a67`
- Base checkpoint: 321,908,995 parameters; 643,835,514-byte weight file
- Python 3.11 with dependencies locked in `uv.lock`
- Go 1.27.1 Darwin ARM64 toolchain in the ignored local cache
- Eclipse Paho Go `v1.5.1`; optional Solace Go API `v1.10.1`
- Java SDKPerf `10.30.2` and Temurin JRE `21.0.12.1` in the ignored local cache

Nothing is installed globally and no hosted inference API is used.

## Setup

```bash
cd laya-mac-finetune
chmod +x scripts/*.sh scripts/*.py solace-go/scripts/*.sh solace-go/python_worker.py
./scripts/setup.sh
source scripts/env.sh
```

The setup pins the upstream checkout, creates `.venv`, resolves `uv.lock`, inspects the machine, downloads the public checkpoint, and verifies an MPS tensor operation.

## Dataset

```bash
uv run python scripts/generate_dataset.py
uv run python scripts/validate_dataset.py --tokenization
uv run python scripts/export_sdkperf.py
uv run python scripts/run_sdkperf_replay.py --batch-size 250  # dry run
```

The active dataset has exactly:

- 8,000 training events
- 1,000 validation/calibration events
- 1,000 held-out test events
- 1,250 examples for each of eight routes overall
- 1,250 examples for each source domain: finance, manufacturing, payments, shipping, insurance, IT/OT, SAP, and Salesforce
- near-balanced English/French coverage in every split and both languages for every actual scenario
- 150 counterfactual pairs whose route changes with one declared business field

The audit verifies no exact-state, entity, scenario-family, or normalized semantic-template overlap across splits. It also verifies true topic/payload conflicts, genuinely novel OOD schemas/bodies, route-label isolation, time-separated splits, and all 10,000 native tokenization paths. At `max_length=384` and `head_max_length=128`, zero events lose state tokens and all eight option markers survive. Details are in [data/README.md](data/README.md) and `outputs/dataset_audit.json`.

`data/sdkperf_replay_manifest.jsonl` contains 10,000 validated payload/topic pairs with unique correlation IDs and payload hashes. SDKPerf replayed all 10,000 to the authorized nonproduction broker with 10,000 publish ACKs and zero NACKs. SDKPerf is the traffic generator/replayer; it did not generate semantic content or labels.

## Training

Smoke test:

```bash
uv run python scripts/train.py --mode smoke
uv run python scripts/infer.py --model outputs/smoke-model --file examples/events.jsonl
```

Full bounded experiment:

```bash
uv run python scripts/train.py --mode experiment
uv run python scripts/evaluate.py
uv run pytest -q
```

The M4/16 GiB configuration uses:

- MPS with explicit CPU fallback warning
- FP32 optimization
- frozen mmBERT encoder and native Laya decision head
- label-free float16 frozen-encoder cache, guarded by token-ID and base-weight hashes
- fixed 384-token inputs and a 128-token question budget
- micro-batch 16, gradient accumulation 2, effective batch 32
- 250 optimizer steps, exactly 8,000 event exposures (one complete pass)
- validation every 50 steps
- deterministic seeds, finite-loss/gradient checks, gradient clipping
- atomic best checkpoint and at most one recovery checkpoint

The cache is valid only for a frozen encoder and never contains labels. A parity check against the normal model path must pass before training. Partial caches record progress and can resume after interruption.

### Objective and deviations from upstream

The trainer preserves the official Apple trainer's model construction, `build_sequence` encoding, typed-choice markers, soft targets, four noisy logit samples, proper-scoring-rule RLCD reward (`w_sph=0.75`, `w_rps=1.0`), and full-weight soft cross-entropy. Temperature and abstention thresholds are fitted on validation only.

For local speed and bounded memory, the encoder is frozen, its deterministic outputs are cached, context is 384 rather than 1024, and only the native decision head is optimized. No LoRA, CUDA-only package, distributed runtime, `device_map="auto"`, or `torch.compile` is used.

## Measured enterprise-v2 results

Measured on an Apple M4 with 16 GiB unified memory and MPS:

| Metric (1,000 held-out events) | Untouched base | Tuned, raw | Tuned, calibrated |
|---|---:|---:|---:|
| Known-owner accuracy (excludes 40 OOD cases) | 0.8031 | **0.8719** | **0.8719** |
| Compatibility accuracy (includes provisional OOD labels) | 0.7810 | **0.8560** | **0.8560** |
| Negative log loss | 0.6729 | **0.5351** | 0.5506 |
| Soft-target cross-entropy | 1.6980 | 0.9019 | **0.9000** |
| Soft-target Brier | 0.2564 | **0.1666** | 0.1682 |
| 10-bin ECE | **0.0645** | 0.1101 | 0.1186 |
| Warm single-event median (5 observations) | 51.5 ms | — | 51.7 ms |

The selected checkpoint is optimizer step 250. The optimization/evaluation phase took 519.9 seconds after 129.2 seconds in the final cache-building process; all 8,000 training examples were exposed exactly once. Fine-tuning corrected 81 base-model choices and regressed 6. Fraud-review recall improved from 0.360 to 0.416, but remains the weakest route.

Calibration improved validation soft-target cross-entropy slightly but worsened hard-label NLL/ECE. On test it also worsened ECE relative to the raw tuned model. The fitted values are retained for analysis, not claimed as a calibration win. The review policy reviewed 2.3% of test events and caught only 7.5% of synthetic OOD cases, so it is not a production safety boundary.

Full metrics and examples are in `outputs/metrics.json`, `outputs/predictions.jsonl`, and `outputs/confusion_matrix.csv`. Revisions, hardware, packages, training exposure, SDKPerf, and broker verification belong in `outputs/run_manifest.json`.

The complete release checkpoint is included in Git LFS. Run `git lfs install`
and `git lfs pull` from the repository root after cloning.

## Inference

Base checkpoint:

```bash
uv run python scripts/infer.py --model base --file examples/events.jsonl --verbose
```

Fine-tuned checkpoint:

```bash
uv run python scripts/infer.py --model outputs/best-model --file examples/events.jsonl --verbose
```

Interactive mode:

```bash
uv run python scripts/infer.py --model outputs/best-model --interactive --verbose
```

The checkpoint owns `route_contract.json` and its matching calibration metadata. The worker therefore follows the selected model's option set rather than a stale process-global taxonomy.

## Solace bridge and SDKPerf

```bash
cd solace-go
make test race build
make test-smf race-smf build-smf
make real-worker-test
make offline-demo
```

For an authorized broker replay, keep credentials in ignored `solace-go/.env`, then:

```bash
cd solace-go
set -a
source .env
set +a
make sdkperf-replay
```

See [solace-go/README.md](solace-go/README.md) for MQTT/SMF details. The live run uses only `edm0/pilot/events/>`, `edm0/pilot/results/...`, and the dedicated durable queue.

## Outputs and archive

```text
configs/route_contract.json
configs/experiment.yaml
data/{train,validation,test}.jsonl
data/sdkperf_replay_manifest.jsonl
outputs/dataset_audit.json
outputs/sdkperf_replay.json
outputs/metrics.json
outputs/predictions.jsonl
outputs/confusion_matrix.csv
outputs/run_manifest.json
outputs/best-model/                  # committed release; weights/tokenizer use Git LFS
outputs/encoder-cache/               # ignored, label-free frozen features
archive/v1-260/                      # original six-route code/data/results
outputs/models/v1-260/               # ignored original selected checkpoint
```

## Offline and CPU modes

After setup/download:

```bash
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
uv run python scripts/infer.py --model outputs/best-model --file examples/events.jsonl
```

Force CPU explicitly:

```bash
uv run python scripts/infer.py --device cpu --model outputs/best-model --file examples/events.jsonl
```

CPU is a correctness fallback and is substantially slower.

## Memory guidance

| Unified memory | Suggested starting point |
|---|---|
| 8 GiB | head-only, shorter context, micro-batch 1–2 |
| 16 GiB | checked-in head-only cache, 384 tokens, micro-batch 16 |
| 24 GiB | profile final-block tuning and larger batches |
| 32 GiB+ | profile final 1–2 blocks or longer context before committing |

Attention memory grows rapidly with context length. Run the token audit and smoke mode after every context or freeze-strategy change.

## Cleanup

Remove generated/downloaded artifacts while preserving source, data, and metrics:

```bash
rm -rf .venv .cache .tmp models outputs/best-model outputs/smoke-model outputs/recovery-model outputs/models
```

To remove the entire experiment, leave this directory and delete `laya-mac-finetune` after reviewing the path.
