# EDM-0 enterprise synthetic dataset

`train.jsonl`, `validation.jsonl`, and `test.jsonl` contain exactly 10,000 deterministic synthetic events. They are test fixtures and research data, not real Solace customer traffic.

## Splits

| Split | Events | Per route | Per source domain | Time window |
|---|---:|---:|---:|---|
| train | 8,000 | 1,000 | 1,000 | January 2026 |
| validation | 1,000 | 125 | 125 | July 2026 |
| test | 1,000 | 125 | 125 | September 2026 |

The eight source domains are finance, manufacturing, payments, shipping, insurance, IT/OT, SAP, and Salesforce. They are crossed with eight operational destinations so source platform/domain is not a direct label shortcut.

The routes are `finance-accounting`, `manufacturing-operations`, `payment-operations`, `shipping-logistics`, `insurance-operations`, `it-ot-operations`, `customer-crm`, and `fraud-review`.

## Record boundaries

Only `state` and `questions` are model inputs. `gold`, `annotation`, `scenario_family`, `template_id`, `entity_group`, `split`, and `challenge_tags` are evaluation metadata and must never be inserted into the state.

Each state includes the topic, schema name/version, event type, timestamp, source system, correlation ID, business entity, and payload. Labels include a probability distribution rather than only a hard class.

The generated challenges include:

- topic/body conflicts whose topic points toward the declared secondary route
- ambiguous soft-label cases marked for review
- genuinely novel OOD schema/event/body families in validation and test
- schema evolution, missing optional fields, noisy replay notes, and unseen test topic layouts
- counterfactual pairs where all semantic inputs except one declared payload field are equal; identifiers and timestamps differ only where required for event identity

## Leakage and context audit

Run:

```bash
uv run python scripts/generate_dataset.py
uv run python scripts/validate_dataset.py --tokenization
```

The audit verifies exact counts and balance, unique states and correlations, no entity overlap, chronological split separation, no scenario-family or normalized semantic-template overlap, valid counterfactual pairs, real topic conflicts, bilingual scenario/version coverage, and absence of route/annotation fields from model input.

With the checked-in 384-token context and 128-token question budget, all 10,000 records fit the native Laya encoder with zero dropped state tokens and zero option-marker failures.

## SDKPerf replay

`data/sdkperf_replay_manifest.jsonl` maps every record to its exact payload file, topic, correlation ID, byte length, and SHA-256. Payload files are regenerated under the ignored `.cache/sdkperf-replay/` directory:

```bash
uv run python scripts/export_sdkperf.py
uv run python scripts/run_sdkperf_replay.py --batch-size 250
```

SDKPerf only replays these records; it does not generate their semantics or labels.
