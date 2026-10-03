# What We Need from Solace to Advance the Laya Routing Pilot

**Status date:** 2026-10-03
**Scope:** Requirements for moving the existing local proof of concept into a measured non-production Solace pilot, then—only if the evidence supports it—toward production.

> The quantities and gates below are proposed engineering targets. They are not Solace product requirements, contractual service levels, or performance promises.

## 1. Current evidence and limits

The repository already proves the local mechanics:

| Item | Measured result |
|---|---|
| Data | 160 train / 40 validation / 60 test synthetic events |
| Languages | English and French for every route/event-type combination |
| Model strategy | Frozen mmBERT encoder; native Laya decision head trained with RLCD + soft cross-entropy |
| Hardware | Apple M4, 16 GiB unified memory, MPS, FP32 training |
| Selected run | Step 50; 118.8 seconds; validation accuracy 0.925 |
| Held-out choice accuracy | Base 0.8333; tuned 0.8333 |
| Held-out raw NLL | Base 0.5260; tuned 0.4736 |
| Fraud-review recall | Base 3/10; tuned 3/10 |
| Calibration | Fitted on validation, but test ECE worsened from 0.1227 raw to 0.1469 calibrated |
| Python verification | 13 tests passed, including real checkpoint load and inference |
| Go verification | Unit tests, race detector, vet, build, real worker test, and offline end-to-end demo passed |
| Warm Go-worker inference | Approximately 41–43 ms/event in the local demo after warm-up |
| Live Solace broker | Not tested; no endpoint or credentials were supplied |

Fine-tuning improved probability quality but did not change any held-out argmax decisions. The data are synthetic and tiny, the test set has already informed exploratory decisions, and fraud/payment separation remains weak. These facts prevent any production-readiness claim.

## 2. Decisions and accountable owners

| Owner | Required decision or deliverable |
|---|---|
| Business application owner | Name the accountable routing owner; approve the route taxonomy and rollout scope. |
| Domain SMEs | Provide and adjudicate labels for orders, payments, shipments, inventory, support, and fraud/security. Resolve disputed examples. |
| Fraud/risk owner | Define the fraud-versus-payment boundary, false-negative cost, escalation policy, and minimum acceptable fraud recall. |
| Event-mesh architect | Provide the versioned topic/schema contract, topic-to-payload precedence rules, source systems, and whether routing is single-target or fan-out. |
| Solace platform owner | Provision the non-production Message VPN, MQTT service, ACLs, session policy, quotas, test publisher, and results consumer. |
| Data/privacy owner | Approve extraction, anonymization, retention, access, and use of payload fields for training/evaluation. |
| ML owner | Build leakage-safe splits, train candidates, publish evaluation evidence, version artifacts, and recommend—not unilaterally set—thresholds. |
| Operations/SRE | Define latency, throughput, burst, backlog, availability, recovery, monitoring, and rollback targets. |
| Security owner | Approve secret delivery, certificate trust, network path, least-privilege ACLs, artifact provenance, and log redaction. |

A named person or team must accept each row before a production gate. “The model team” or “Solace” without an accountable owner is not sufficient.

## 3. Business contract required first

Deliver a version-controlled routing specification containing:

1. The canonical route IDs and owning consumer application for each route.
2. Whether one event has exactly one target, may fan out to multiple targets, or can be rejected.
3. Precedence when topic, schema, event type, and payload disagree.
4. Schema-version compatibility and deprecation rules.
5. Explicit out-of-distribution behavior for unknown event types, tenants, languages, or malformed payloads.
6. The human-review policy: which routes are eligible, staffing/capacity, response SLA, and what happens while review is pending.
7. A cost matrix for mistakes—especially fraud sent to payment operations and payment events incorrectly blocked for fraud review.
8. Required p95/p99 latency, steady-state events/second, burst duration/rate, maximum recoverable backlog, and allowed error rate.

**Safety rule:** `review_required=true` is an abstention, not permission to dispatch. The bridge publishes only a decision envelope and uses `review` for the `{route}` output-topic placeholder when review is required. No downstream service may automatically forward or execute the original event based only on a predicted route or route-shaped topic; it must inspect the review flag and route review cases to the approved human workflow.

## 4. Data required

### 4.1 Record fields supplied by the event owner

Provide anonymized, approved event snapshots with these model-input fields:

- `topic`
- `schema_name`
- `schema_version`
- `event_type`
- `payload`
- event timestamp
- source application/system
- correlation or trace ID
- entity-group keys needed for leakage control, such as customer, account, order, payment, shipment, ticket, device, and replay lineage

Identifiers should be consistently pseudonymized while retaining the relationships needed to group related events. Remove secrets, credentials, payment-card data, unrestricted personal data, and fields not approved for model use.

### 4.2 Labels kept separate from model input

For each event, SMEs should provide separate annotation metadata:

- correct target route or approved fan-out set
- short routing rationale
- whether the case is genuinely ambiguous
- plausible secondary route, where applicable
- whether human review is required
- labeler identity/role and adjudication status
- applicable policy/routing-contract version

The target route, rationale, review flag, and labeler fields must never be serialized into the state presented to Laya.

### 4.3 Proposed pilot volume

- **Training:** 2,000–5,000 approved events, with at least 300 per route.
- **Validation/calibration:** a separate few hundred events, large enough to assess each route, language, option shape, and confidence region.
- **Locked test:** approximately 1,000 fresh events held out by entity and time, not merely random rows.
- **Expansion:** consider 10,000–50,000 only after a learning curve shows that more data improves the agreed metrics.

More examples are not automatically better. Label consistency, boundary cases, and leakage prevention matter more than raw volume.

### 4.4 Coverage priorities

The collection must deliberately include:

- real historical misroutes and operational escalations
- fraud/payment boundary cases, including legitimate declines, chargebacks, account takeover, and suspicious authorizations
- representative English and French—not translated duplicates only
- topic/payload and schema/payload conflicts
- new or reordered topic hierarchies
- schema upgrades, deprecated fields, and missing optional fields
- retries, replays, near duplicates, correction events, and out-of-order events
- unknown/OOD event types and unsupported languages
- short, long, malformed, and near-context-limit payloads
- realistic class imbalance and peak-period traffic

### 4.5 Split controls

- Deduplicate exact and near-duplicate payloads before splitting.
- Keep all events for the same business entity or incident in one split.
- Keep replay chains and corrected versions in one split.
- Split by time so the locked test represents future traffic.
- Keep events generated from the same template or transformation in one split.
- Freeze the test set before tuning. The current synthetic test is now exploratory and must not become the future unbiased benchmark.

## 5. Solace non-production pilot requirements

The Solace platform owner should provide:

1. A non-production Message VPN with MQTT enabled and the exact values shown by its **Connect** settings.
2. A TLS MQTT endpoint and trusted CA chain. Typical defaults are port 1883 for MQTT and 8883 for MQTT/TLS, but ports are per VPN and the provisioned values are authoritative.
3. A dedicated client username and secret delivered through the approved secret mechanism. Do not infer or construct a `user@VPN` value.
4. Subscribe ACL limited to the approved source filter.
5. Publish ACL limited to the decision, review, and error topic hierarchy.
6. A stable, unique client ID per bridge replica.
7. Guaranteed Messaging enabled, a client profile that permits QoS 1 persistent sessions, and enough session-queue/spool quota for the measured outage backlog. Source publishers must use Guaranteed/QoS 1 delivery too; requesting a QoS 1 subscription does not upgrade a QoS 0 source message.
8. Explicit source, decision, review, and error topics with owners and retention policies.
9. A controlled test publisher and a results consumer that validates the envelope and deduplicates by correlation ID/payload hash.
10. Network/DNS/firewall access from the runtime and an approved certificate-root update process.

The current bridge uses MQTT 3.1.1 QoS 1, `CleanSession=false`, a stable client ID, Paho file-backed protocol state, automatic reconnect, resubscription, bounded local intake, and manual input acknowledgement after output publication. It deliberately does not unsubscribe on normal shutdown, allowing the broker session subscription to survive. With `CleanSession=true`, that persistence is intentionally lost.

This provides at-least-once behavior, not exactly-once processing. A crash after publishing the result and before acknowledging the source can create a duplicate output. An MQTT persistent session queue is broker-managed through the MQTT session and does not provide every native Solace Guaranteed Messaging queue feature. Shared MQTT subscriptions are QoS 0 on PubSub+ and are unsuitable for this QoS 1 flow.

Producer payloads must be UTF-8 JSON objects containing the model's topic, schema, event-type, and payload fields. The current bridge records the transport topic in the result but does not inject it into an event that omits `topic`. Agree on a normalization envelope for existing producers. In particular, MQTT 3.1.1 does not carry arbitrary SMF user properties or Solace structured-data maps into this JSON contract; required metadata must be included explicitly in the payload. Validate this conversion with real producer traffic. See [Solace's payload-conversion rules](https://docs.solace.com/API/MQTT/Using-MQTT.htm).

## 6. Hardware and implementation path

### Pilot

The current 16 GiB M4 is sufficient for this head-only proof of concept:

- training selected a checkpoint in 118.8 seconds
- warm single-event inference is about 38 ms in the Python evaluation and approximately 41–43 ms through the Go worker
- fixed 256-token inputs fit without truncation in the recorded Go demo

The arithmetic reciprocal of 40 ms is roughly 25 events/second for one serial worker. That is **not** a sustainable throughput claim: it excludes load spikes, new-shape Metal compilation, broker round trips, publication acknowledgement, retries, and safety headroom. Measure actual EPS, p95/p99 latency, and backlog recovery on representative payloads.

### Optional acceleration after data quality is proven

A borrowed or rented single NVIDIA L4 with 24 GB GPU memory is a reasonable starting budget to profile final-block or full-encoder experiments and batched inference. It is not a guarantee that every sequence length, optimizer, or full fine-tune fits. Plan approximately 32–64 GB host RAM and 50 GB free disk for datasets, environments, checkpoints, optimizer state, and atomic-save headroom.

Before any GPU purchase or rental is treated as usable, port and test the current MPS-specific trainer/inference wrappers on Linux/CUDA. Profile memory because attention cost grows approximately quadratically with sequence length. Do not add multiple GPUs until a measured single-GPU bottleneck and learning curve justify them. GPU capacity cannot correct ambiguous requirements or bad labels.

## 7. Current limitations

- All model evidence comes from synthetic data; no production distribution or operational error cost has been measured.
- The current test set has informed exploratory tuning and is no longer an unbiased future benchmark.
- Head-only tuning improved probability quality but not held-out choice accuracy; fraud-review recall is only 3/10.
- The fitted temperature worsened held-out ECE. The present confidence and review threshold are experimental and miss some confidently wrong events.
- The model accepts one route choice, not a validated fan-out contract or OOD class.
- The model context is fixed at 256 tokens. Truncation is exposed in every envelope but remains possible for larger production events.
- One serial Python worker is implemented. Its arithmetic ceiling is not a broker-load capacity result, and it is not highly available.
- Only macOS arm64/MPS has been exercised. The bundled Go installer is darwin-arm64; Linux/CUDA training and inference are not implemented or tested.
- No live broker, ACL, TLS, reconnect, backlog, or failover test has been run.
- MQTT QoS 1 permits duplicates and does not provide exactly-once processing or the full operational controls of a native Solace queue.

## 8. Staged acceptance plan

### Stage A — contract and data readiness

Must pass before model comparison:

- route contract, precedence, fan-out, review semantics, and error costs approved by named owners
- data/privacy approval recorded
- minimum pilot dataset and per-route coverage met
- group/time/template/replay leakage checks pass
- label audit samples reach an SME-agreed agreement level
- locked test hashes and cutoff date recorded

### Stage B — offline model acceptance

Compare three frozen candidates on the same locked real test:

1. deterministic topic/schema rules baseline
2. untouched multilingual Laya
3. tuned Laya

Business owners must set numeric gates before results are opened. At minimum assess:

- overall and per-route recall/precision, with a specific critical fraud-recall gate
- cost-weighted routing error under the approved error matrix
- accepted precision and review coverage at the chosen abstention policy
- English/French parity and topic/schema-version slices
- NLL, Brier score, ECE, reliability plots, and confidence under shift
- OOD detection/review behavior
- truncation rate, requiring explicit rejection/review rather than silent acceptance
- p50/p95/p99 latency, memory, and sustained/burst throughput

The current 83.33% synthetic accuracy, 3/10 fraud recall, and degraded calibrated ECE do not satisfy an unstated production threshold.

### Stage C — non-production Solace shadow test

Run without automatic business action. Validate:

- TLS connection and least-privilege ACLs
- reconnect and persistent-session resume
- broker restart, bridge restart, worker timeout/restart, and network interruption
- duplicate delivery and consumer deduplication
- replay and out-of-order events
- queue saturation, enqueue timeout, backpressure, and spool growth
- output publish failure before source acknowledgement
- graceful shutdown order: stop intake, drain/publish, then disconnect without unsubscribe
- certificate and secret rotation
- review queue/UI integration and reviewer capacity
- measured p95/p99 latency, sustained EPS, burst EPS, and backlog recovery time

### Stage D — controlled production decision

Proceed only with signed business, risk, security, data, and operations approval. Start in shadow mode, then a small canary with an immediate rollback path. Keep model/config/calibration versions in every result, monitor per-route drift and review volume, and retain human override plus feedback capture. Automatic event forwarding remains disabled for `review_required=true`.

Production work also needs readiness/health checks and metrics for queue depth, inference and publish latency, reconnects, worker failures, outstanding acknowledgements, and broker spool usage. The current result identifies the model by path/name; an immutable checkpoint hash and policy/config version in each published envelope remain production requirements. The run manifest already records source/model revisions, but it is not a replacement for per-result provenance.

## 9. Must-haves versus optional spend

### Must-have before a pilot

- named owners and approved routing/review contract
- representative anonymized labeled data and leakage-safe split
- non-production Message VPN and least-privilege MQTT credentials
- TLS/network path and certificate roots
- measurable latency/EPS/backlog/error-cost targets
- test publisher, output consumer, and human-review sink
- duplicate handling and failure/reconnect test plan

### Optional spend after evidence

- 24 GB L4 trial for Linux/CUDA profiling
- larger 10k–50k dataset if the learning curve supports it
- multiple workers or GPUs if measured throughput requires them
- native Solace API implementation if MQTT session semantics do not meet operational needs

Do not buy GPU capacity or provision production infrastructure as a substitute for the route contract, labels, and locked evaluation set.

## 10. Immediate request to Solace stakeholders

Provide one package containing:

1. Named application owner, route SMEs, fraud/risk approver, Solace platform owner, and operations owner.
2. Versioned route/precedence/review contract and error-cost matrix.
3. An approved 2,000–5,000-event pilot training export plus separate validation/calibration and locked test exports with grouping metadata.
4. Non-production MQTT Connect details, ACLs, client profile/session quota, stable client ID, and secret/certificate delivery method.
5. Written p95/p99 latency, EPS, burst, backlog, review-capacity, and recovery objectives.
6. A scheduled shadow-mode acceptance exercise covering the Stage C failure cases.

## Primary references

- [Solace: Using MQTT](https://docs.solace.com/API/MQTT/Using-MQTT.htm)
- [Solace: Managing MQTT Sessions](https://docs.solace.com/Configuring-and-Managing/Managing-MQTT-Sessions.htm)
- [Solace: MQTT 3.1.1 protocol conformance](https://docs.solace.com/API/MQTT-311-Prtl-Conformance-Spec/MQTT_311_Prtl_Conformance_Spec.htm)
- [NVIDIA L4 Tensor Core GPU](https://www.nvidia.com/en-gb/data-center/l4/)

The model evidence is recorded in `outputs/metrics.json`; environment, revisions, training timings, and bridge verification are recorded in `outputs/run_manifest.json`.
