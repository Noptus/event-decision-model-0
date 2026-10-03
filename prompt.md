Act as a senior ML engineer working directly in my terminal. Build and run a minimal, local, end-to-end Laya fine-tuning experiment on my Apple Silicon Mac mini.

Do not only explain what to do: inspect the machine, create the project, install dependencies, generate a tiny dataset, fine-tune the model, evaluate it, and run inference. Keep the experiment small enough to complete locally. Ask me only before performing destructive actions or if a required decision cannot be inferred safely.

OBJECTIVE

Create a small proof of concept that:

1. Downloads the official Laya code and checkpoint.
2. Fine-tunes Laya locally on a tiny synthetic event-routing dataset.
3. Uses Apple Metal/MPS when available, with CPU fallback.
4. Compares the base and fine-tuned checkpoints on a held-out test set.
5. Runs inference on new JSON business events.
6. Returns typed choices and probability distributions using Laya’s native decision format.
7. Saves everything in a clean, reproducible local project.

Use this base checkpoint unless repository compatibility requires another official checkpoint:

    convaiinnovations/laya-multilingual

This is the multilingual mmBERT-based Laya checkpoint. The experiment should specialize it for event routing similar to a Solace event mesh.

OFFICIAL SOURCES

Use the current implementations from these sources rather than inventing Laya APIs:

- https://github.com/NandhaKishorM/laya
- https://huggingface.co/convaiinnovations/laya-multilingual
- Official Apple Silicon trainer:
  notebooks/laya_finetune_typed_decisions_mps.py
- Reference CUDA notebook:
  notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb

First inspect the current repository, current model configuration, official Apple Silicon script, dataset construction logic, checkpoint format, training loss, and inference API. APIs may have changed, so verify all names against the checked-out source. Do not guess private classes or dataset schemas.

PROJECT DIRECTORY

Create:

    laya-mac-finetune/

Use this approximate structure, adapting it if the official Laya implementation requires something different:

    laya-mac-finetune/
    ├── README.md
    ├── pyproject.toml
    ├── .gitignore
    ├── scripts/
    │   ├── setup.sh
    │   ├── inspect_system.py
    │   ├── generate_dataset.py
    │   ├── train.py
    │   ├── evaluate.py
    │   └── infer.py
    ├── data/
    │   ├── train.jsonl
    │   ├── validation.jsonl
    │   └── test.jsonl
    ├── configs/
    │   └── experiment.yaml
    ├── outputs/
    └── tests/

SYSTEM INSPECTION

Before installing or training:

1. Detect:
   - Apple chip generation.
   - Total unified memory.
   - macOS version.
   - Architecture.
   - Python versions.
   - Free disk space.
   - Whether PyTorch MPS is available.
2. Print the findings.
3. Select conservative settings based on available memory.
4. Refuse to start training if free disk space is clearly insufficient.
5. Never install anything globally; use a local virtual environment.
6. Prefer Python 3.11 or 3.12 if compatible with the current Laya repository.
7. Record package versions, Git commit SHA and model revision in outputs/run_manifest.json.

INSTALLATION

Prefer uv if it is installed; otherwise use python -m venv and pip.

Clone or otherwise obtain the official Laya repository at a pinned commit. Install the local checkout in editable mode with the dependencies needed for training and inference.

Verify:

    import torch
    import laya
    print(torch.backends.mps.is_available())
    print(laya.__version__)

Set this where appropriate:

    PYTORCH_ENABLE_MPS_FALLBACK=1
    USE_TF=0

Do not use CUDA, bitsandbytes, FlashAttention, NCCL, DeepSpeed or CUDA-specific fused kernels on the Mac.

APPLE SILICON REQUIREMENTS

The training implementation must:

- Prefer device="mps" when available.
- Fall back to CPU with a clear warning.
- Avoid device_map="auto" on MPS.
- Use a single process.
- Start with micro-batch size 1.
- Use gradient accumulation to obtain a larger effective batch.
- Use fixed-length padded batches or a small set of fixed length buckets to avoid excessive MPS graph-cache growth.
- Start with a short context, ideally 256 tokens, unless the official trainer requires a different minimum.
- Enable gradient checkpointing where supported.
- Disable DataLoader multiprocessing initially with num_workers=0.
- Use deterministic seeds where practical.
- Detect NaN or infinite loss and stop with a useful error.
- Save checkpoints atomically.
- Keep only the best checkpoint and, at most, one recovery checkpoint.
- Call torch.mps.empty_cache() only at sensible boundaries, not every training step.
- Prefer stable precision over speed. If FP16 or BF16 is unsupported or unstable on this machine, use FP32.
- Avoid torch.compile unless the repository explicitly demonstrates that it is stable on MPS.
- Log peak memory if the available PyTorch APIs support it.

Use the official Apple Silicon fine-tuning script as the primary reference. Reuse it directly if practical. If it cannot support this small custom dataset cleanly, create the smallest possible adaptation while preserving the official model construction, tokenization, loss and checkpoint format. Document every deviation.

DATASET

Create a deterministic, clearly labelled toy dataset for event-routing experimentation. It is not production data.

Generate approximately:

- 160 training events.
- 40 validation events.
- 60 test events.

Include English and French examples. Use realistic but fictional JSON events and topic hierarchies from these domains:

- Orders.
- Payments.
- Shipments.
- Inventory.
- Customer support.
- Fraud/security alerts.

Each state should contain fields resembling:

- topic
- schema_name
- schema_version
- event_type
- payload

Use routing labels such as:

- order-processing
- payment-operations
- logistics
- inventory-management
- customer-support
- fraud-review

Include:

- Straightforward examples.
- Near-duplicate routes.
- Missing optional fields.
- Noisy free text.
- French descriptions.
- A few ambiguous examples.
- At least one unseen topic pattern per class in the held-out test set.

Represent routing as a native Laya typed `choice` decision. Derive the exact fine-tuning record structure from the official Apple trainer and reference notebook. Do not invent an incompatible schema.

Prevent exact duplicate states from crossing train, validation and test splits. Add a validation script or test that verifies split isolation and class counts.

TRAINING STRATEGY

This is a local smoke test, not an attempt to reproduce the full Laya training recipe.

Implement two modes:

1. smoke:
   - Very small subset.
   - 5–10 optimizer steps.
   - Confirms forward pass, backward pass, checkpoint saving and loading.

2. experiment:
   - Full tiny dataset.
   - Conservative maximum of roughly 50–200 optimizer steps, selected according to machine memory and measured speed.
   - Early stopping on validation loss or validation accuracy.
   - Evaluation every small number of steps.
   - Runtime target of minutes to a few hours, not days.

Start by running smoke mode. Only proceed to experiment mode if smoke mode succeeds.

Prefer this adaptation order when memory or runtime is excessive:

1. Reduce sequence length.
2. Keep micro-batch size at 1 and increase gradient accumulation.
3. Freeze most of the encoder and train the decision head.
4. Unfreeze only the final 1–2 encoder blocks plus the decision head.
5. Use CPU as a last-resort correctness path.

Do not introduce LoRA unless the official Laya architecture and current PEFT stack support the custom decision head cleanly. A correct head-only or last-block fine-tune is preferable to a fragile LoRA implementation.

Expose the strategy in configs/experiment.yaml, including:

- base_model
- model_revision
- seed
- device
- max_length
- micro_batch_size
- gradient_accumulation_steps
- learning_rate
- weight_decay
- warmup_steps
- max_steps
- evaluation_interval
- patience
- freeze_strategy
- output_directory

Choose conservative defaults after inspecting the machine. Explain the final choices in README.md.

LOSS AND CALIBRATION

Use the official Laya supervised/RLCD-compatible training logic available in the repository. For this tiny local experiment, do not attempt a large RLCD reproduction unless the official script already makes it straightforward.

If the official trainer combines supervised soft cross-entropy and proper-scoring-rule/RLCD updates:

- Preserve that implementation.
- Use a simplified supervised phase if RLCD needs multiple expensive samples or assumptions unsuitable for this toy dataset.
- Clearly state which objective was actually run.

If supported by the repository, fit a simple temperature on the validation set after training and save the calibration parameters. Report metrics both before and after calibration. Never fit calibration on the test set.

EVALUATION

Evaluate both:

- The untouched base checkpoint.
- The local fine-tuned checkpoint.

Report:

- Overall choice accuracy.
- Accuracy per route.
- Confusion matrix as text or CSV.
- Negative log loss if available.
- Brier score if compatible with the output.
- Expected calibration error if the repository already provides it.
- Mean confidence.
- Inference latency for several warm runs on MPS or CPU.
- Training duration.
- Exact examples where the fine-tuned model improved or regressed.

Save machine-readable results to:

    outputs/metrics.json
    outputs/predictions.jsonl
    outputs/confusion_matrix.csv
    outputs/run_manifest.json

With such a small dataset, explicitly describe results as experimental and do not claim production quality.

INFERENCE

Create scripts/infer.py with both file and interactive modes.

Examples:

    uv run python scripts/infer.py \
      --model outputs/best-model \
      --event '{"topic":"acme/fr/payment/failed","schema_name":"PaymentFailed","schema_version":"1.0","event_type":"payment.failed","payload":{"amount":120,"currency":"EUR","message":"Le paiement a été refusé"}}'

and:

    uv run python scripts/infer.py \
      --model outputs/best-model \
      --file examples/events.jsonl

The output must include:

- Selected route.
- Probability for every route.
- Confidence.
- Model/checkpoint identity.
- Device.
- Inference latency.
- Raw Laya answer object when --verbose is passed.

Use the native Laya Agent or Router API if it can load the fine-tuned local directory. Do not bypass the Laya decision head with a generic Transformers classification pipeline.

Also demonstrate inference against the untouched base model using the exact same question definition.

README

Write a concise but complete README containing:

- What was built.
- System requirements.
- Exact setup commands.
- Exact smoke-test command.
- Exact training command.
- Exact evaluation command.
- Exact inference commands.
- Expected directory outputs.
- How to rerun offline after the first model download.
- How to force CPU mode.
- How to adjust settings for 8 GB, 16 GB, 24 GB, 32 GB or more unified memory.
- Known MPS limitations.
- What training objective and freeze strategy were actually used.
- How to delete all downloaded/project artifacts.
- A clear warning that the synthetic dataset and resulting model are experimental.

TESTS

Add lightweight tests for:

- Dataset schema validity.
- No exact split leakage.
- Every route represented in every intended split.
- MPS/CPU device selection.
- Loading the saved checkpoint.
- Running one inference call.
- Probabilities being finite and approximately summing to 1.
- Base and fine-tuned models accepting the same question contract.

EXECUTION ORDER

Perform the work in this order:

1. Inspect the Mac.
2. Inspect the official Laya repository and training scripts.
3. Present a short implementation plan and estimated disk/memory requirements.
4. Create the project.
5. Create the virtual environment and install pinned dependencies.
6. Generate and validate the dataset.
7. Run unit tests.
8. Run base-model inference.
9. Run smoke training.
10. Load the smoke checkpoint and run inference.
11. If smoke training is stable, run the small experiment.
12. Evaluate base versus fine-tuned checkpoints.
13. Run final inference examples in English and French.
14. Update the README with the actual commands, versions, device and observed results.
15. Print a compact final report with:
    - Files created.
    - Device used.
    - Freeze strategy.
    - Training duration.
    - Base accuracy.
    - Fine-tuned accuracy.
    - Best checkpoint path.
    - One exact inference command I can run next.

IMPORTANT BEHAVIOUR

- Do not fabricate successful runs, metrics or files.
- Show real command output summaries.
- If a command fails, diagnose it and make the smallest robust correction.
- Do not silently switch from MPS to CPU.
- Do not push anything to Hugging Face or GitHub.
- Do not require API keys.
- Do not download unrelated large models.
- Do not overwrite unrelated files outside laya-mac-finetune.
- Keep the implementation simple enough that I can understand and modify it.
- Prefer official Laya utilities over reimplementing model internals.
- If current upstream code differs from these instructions, follow the verified upstream API and document the difference.