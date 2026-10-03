---
license: apache-2.0
base_model: convaiinnovations/laya-multilingual
language: [en, fr]
tags: [solace, event-routing, fine-tuned, synthetic-data]
---

# EDM-0 release checkpoint

Head-only fine-tune of Laya Multilingual by ConvAI Innovations, base revision
`e4e9ddf21a7b1903b7acffd8814ad4307bf63a67`. The encoder remains frozen.
The original model card is preserved in `BASE_MODEL_CARD.md`; Apache 2.0
terms are included in `LICENSE`. This directory contains modified model weights.

Trained locally on 8,000 synthetic enterprise events, calibrated on 1,000,
and evaluated on a separate 1,000. The full 10,000-record dataset is in
`../../data/`. The saved step-250 checkpoint completed one training epoch.
Test accuracy is 85.6%, compared with 78.1% for the base model under the
same canonical route ordering. These are synthetic benchmark results.

Eight candidate owner routes and their exact descriptions are defined in
`route_contract.json`. Use the repository inference script and calibration
file together; preserve the canonical alphabetical option order.
Fraud recall is 41.6%, hard-boundary accuracy is 33%, and unfamiliar-event
review recall is 7.5%. Human review and representative real-data evaluation
are required before using these decisions for consequential automation.

The weight file and tokenizer JSON use Git LFS. Run `git lfs install` and
`git lfs pull` after cloning. Run inference from `laya-mac-finetune`:

```sh
uv run python scripts/infer.py --model outputs/best-model --file examples/events.jsonl
```
