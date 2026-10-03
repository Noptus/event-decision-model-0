# Laya × Solace Event-Mesh Routing Lab

A local, bilingual fine-tuning experiment for routing Solace-style JSON business
events to six consumer services, with native Laya probability distributions and
a validation-fitted manual-review policy.

All project code, configuration, synthetic data, tests, and measured results are
in [`laya-mac-finetune/`](laya-mac-finetune/). Start with its
[README](laya-mac-finetune/README.md).

```bash
cd laya-mac-finetune
source scripts/env.sh
uv run python scripts/infer.py --model outputs/best-model --interactive --verbose
```

Measured on the local M4 with MPS: 118.8 seconds for the selected head-only
training run and about 38 ms per warm inference. Base and fine-tuned routing
accuracy both reached 83.3% on 60 synthetic test events; uncalibrated fine-tuning
improved negative log loss from 0.526 to 0.474. The first run's results are
preserved in `laya-mac-finetune/outputs/runs/head60/`.

This is an experimental routing prototype; it does not require a Solace broker.

## Solace MQTT bridge

A lightweight Go bridge is included at [`laya-mac-finetune/solace-go/`](laya-mac-finetune/solace-go/). It keeps the Python/Laya model worker resident, subscribes and publishes with MQTT QoS 1/manual acknowledgement, preserves native probabilities and truncation metadata, and includes an offline end-to-end demo that needs no broker. See its [integration guide](laya-mac-finetune/solace-go/README.md).

Before a real pilot, review [what we need from Solace](laya-mac-finetune/docs/solace-requirements.md): accountable owners, representative data, broker setup, hardware guidance, acceptance gates, and current limitations.
