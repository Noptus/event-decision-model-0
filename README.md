# EDM-0 : Event Decision Model

A local bilingual decision model for Solace projects. EDM-0 reads a JSON event,
selects an operational owner and returns eight route probabilities plus a review
signal in one forward pass. A lightweight Go consumer keeps the model resident,
subscribes over MQTT or native Solace SMF, and publishes decision envelopes to
customizable topics.

The repository includes **10,000 synthetic enterprise events** spanning finance,
manufacturing, payments, shipping, insurance, IT/OT, SAP and Salesforce; the
training code; SDKPerf replay tools; and the complete trained checkpoint.
Weights and the tokenizer are stored with **Git LFS**.

```bash
git lfs install
git clone https://github.com/Noptus/event-decision-model-0.git
cd event-decision-model-0
git lfs pull
cd laya-mac-finetune
./scripts/setup.sh
source scripts/env.sh
uv run python scripts/infer.py --model outputs/best-model --file examples/events.jsonl
```

Trained on an M4 with 16 GiB memory: one pass over 8,000 training events,
1,000 calibration events, and 1,000 independent test events. Synthetic test
accuracy improved from **78.1% to 85.6%**. The head optimization and validation
phase took 519.9 seconds; encoder-cache preparation is additional.

Fraud recall and unfamiliar-event review remain weak. This is an integration
prototype: representative real events and a better review policy are the next
priority. [Solace requirements](laya-mac-finetune/docs/solace-requirements.md)
cover data, broker setup, hardware and acceptance criteria.

- [Model, dataset, training and terminal guide](laya-mac-finetune/README.md)
- [MQTT / SMF Go integration guide](laya-mac-finetune/solace-go/README.md)
- [Trained model card](laya-mac-finetune/outputs/best-model/README.md)
- [Dataset description](laya-mac-finetune/data/README.md)
- [Measured results](laya-mac-finetune/outputs/metrics.json)

The original 260-event experiment is preserved in
[laya-mac-finetune/archive/v1-260](laya-mac-finetune/archive/v1-260/README.md).
