# Requirements for the Next EDM-0 Solace Pilot

**Status date:** 2026-10-03
**Purpose:** define what Solace and application teams must provide before this experimental router can move from a synthetic proof of concept to a controlled pilot or production decision.

> Every volume, metric, and hardware figure below is an engineering starting point—not a Solace product requirement, contractual SLO, or promised model result.

## 1. What exists today

| Area | Verified result |
|---|---|
| Dataset | Exactly 10,000 synthetic events: 8,000 train, 1,000 validation/calibration, 1,000 test |
| Balance | 1,250 events per route and per source domain overall; approximately 50/50 English/French |
| Isolation | No exact-state, entity, scenario-family, or normalized semantic-template overlap across splits |
| Hard cases | 150 counterfactual pairs, topic/body conflicts, schema evolution, missing fields, noisy text, and 80 validation/test OOD events |
| Token budget | All 10,000 records fit native Laya encoding at 384 tokens; zero dropped state tokens and zero option-marker failures |
| Training | Apple M4, 16 GiB, MPS, head-only, FP32, exactly 8,000 exposures / 250 optimizer steps |
| Duration | 129.2 seconds for the completed cache stage and 519.9 seconds for optimization/evaluation/checkpointing |
| Known-owner test accuracy | Untouched Laya 0.8031 → tuned Laya 0.8719 |
| Compatibility accuracy | 0.7810 → 0.8560, including provisional labels on 40 OOD events |
| Raw NLL | 0.6729 → 0.5351 |
| Fraud-review recall | 0.360 → 0.416; still the weakest route |
| Calibration | Fitted only on validation; test ECE worsened from 0.1101 raw to 0.1186 calibrated |
| Runtime | Persistent Go/Python worker; roughly 41–43 ms warm single-event observations, not a load-test result |
| SDKPerf | 10,000 distinct payload/topic pairs published persistently; 10,000 ACK events and zero NACKs |
| Broker | Nonproduction TLS connection, dedicated queue provisioning, and 10,000 SDKPerf source ACKs succeeded; the unified 10,000-result SMF audit is pending final verification in `outputs/run_manifest.json` |
| Tests | Python tests plus Go unit, race, vet, default/native builds, and real-worker checks passed before the live audit |

The previous 260-event/six-route experiment remains under `archive/v1-260/`; its selected checkpoint is under ignored `outputs/models/v1-260/`.

## 2. Named owners and required deliverables

| Accountable owner | Deliverable |
|---|---|
| Business application owner | Signed routing scope, rollout mode, and final go/no-go decision |
| Event-mesh architect | Versioned topic/schema contract and topic-versus-body precedence rules |
| Route SMEs | Adjudicated labels and examples for all eight operational destinations |
| Fraud/risk owner | Fraud/payment boundary, false-negative cost, review rules, and minimum fraud recall |
| Data/privacy owner | Approved extraction, pseudonymization, retention, access, and permitted payload fields |
| Solace platform owner | Nonproduction VPN, endpoints, queue/subscriptions, ACLs, spool quotas, test producers/consumers |
| ML owner | Leakage-safe datasets, model versions, evaluation evidence, calibration analysis, and rollback package |
| Security owner | Secret delivery, CA trust, network access, audit/log redaction, and artifact provenance |
| SRE/operations | p95/p99 latency, EPS/burst/backlog SLOs, monitoring, failure tests, runbook, and rollback |

## 3. Business contract that must be approved

Provide a version-controlled contract that answers:

1. Is routing single-target, fan-out, or allowed to reject an event?
2. What are the canonical route IDs and owning consumer applications?
3. Which signal wins when topic, schema, event type, and payload disagree?
4. Which schema versions are accepted, deprecated, or incompatible?
5. What is the policy for unknown event types, unsupported languages, malformed JSON, and truncated inputs?
6. Which decisions require review, who handles the review queue, and what is its capacity/SLA?
7. What are the costs of each error, particularly fraud-as-payment and payment-as-fraud?
8. What p95/p99 latency, sustained EPS, burst duration/rate, outage backlog, and recovery time are required?

**Mandatory safety behavior:** `review_required=true` is an abstention. The bridge maps `{route}` to `review` for such decisions while retaining the proposed route and probabilities in the envelope. No downstream consumer may execute or forward the original event based only on a predicted route or route-shaped topic.

## 4. Real data required

The generated 10,000 examples are useful for software and training-path verification, but they do not replace real data.

### Input fields

Provide representative, approved, anonymized event snapshots containing:

- topic
- schema name and version
- event type
- payload
- event timestamp
- source application/system
- correlation or trace ID
- entity-group keys such as customer, account, order, payment, shipment, claim, device, incident, and replay lineage

Identifiers should be consistently pseudonymized so related records can be grouped without retaining secrets, credentials, card data, or unapproved personal data.

### Labels kept outside model input

For each event, SMEs must separately provide:

- correct route or approved fan-out set
- concise rationale
- ambiguity marker and plausible secondary route
- review-needed decision
- labeler role and adjudication state
- routing-policy version

Labels, rationales, split names, and review decisions must never be inserted into the model-visible state.

### Proposed engineering volume

- Pilot training: **2,000–5,000 real labeled events**, at least **300 per route**.
- Validation/calibration: a separate **few hundred** events with route/language/confidence coverage.
- Locked test: about **1,000 fresh events**, grouped by entity and held out by time.
- Scale to **10,000–50,000 real events only if a learning curve demonstrates value**.

Required coverage includes real misroutes; fraud/payment boundaries; English and French authored independently; SAP and Salesforce workflows; IT and OT incidents; topic/body conflicts; unseen topic shapes; schema changes; retries/replays; malformed and near-context-limit payloads; and genuine unknown/OOD events.

Deduplicate exact and near duplicates. Keep each entity, replay chain, incident, source template, and derived/corrected event in one split. Freeze the new test set before tuning. The current synthetic test has already informed engineering choices and must be treated as exploratory.

## 5. Solace platform requirements

### Shared prerequisites

- A nonproduction Message VPN and exact Connect-page endpoints.
- Dedicated client credentials delivered through the approved secret mechanism.
- TLS trust chain and network/DNS/firewall access from the runtime.
- Subscribe ACL restricted to `edm0/pilot/events/...`.
- Publish ACL restricted to `edm0/pilot/results/...`.
- Explicit source, accepted-result, review, and error topic ownership.
- A test publisher and a result consumer that validates envelopes and deduplicates by correlation ID plus payload hash.
- Measured spool sizing for the agreed outage/backlog window.

### Native SMF path

- Native SMF host, normally `tcp://HOST:55555` or `tcps://HOST:55443`.
- Message VPN name supplied separately from username/password.
- Preprovisioned durable queue `edm0-routing-pilot`, with exclusive/non-exclusive mode chosen by the platform owner.
- Queue subscription `edm0/pilot/events/>` and permission to consume/ack.
- Permission to publish persistent results to `edm0/pilot/results/>`.
- If the queue does not exist, the explicit pilot-only provisioning command may create exactly that queue and add exactly that subscription. Normal bridge startup remains `DO_NOT_CREATE`, and no code deprovisions a queue.
- Topic limits: at most 250 UTF-8 bytes and 128 levels. Supported matching is exact levels, `*`, suffix wildcards such as `order*`, and terminal `>` with one-or-more-level semantics.

The native adapter uses client acknowledgement and calls `Ack` only after `PublishAwaitAcknowledgement` confirms the persistent output. It disposes the SDK-owned inbound message after processing. On shutdown it pauses intake, drains accepted work while the publisher remains connected, then terminates receiver/publisher/service without deleting queue subscriptions.

### MQTT path

- MQTT listener for the selected VPN: typically 1883 plaintext or 8883 TLS, but use the actual Connect-page values.
- Stable unique client ID and `CleanSession=false` for broker-held QoS 1 session state.
- MQTT topic syntax (`+`, `#`) rather than SMF syntax (`*`, `>`).
- QoS 1 on source and result publication. Shared MQTT subscriptions on PubSub+ are QoS 0 and do not satisfy this flow.

Both transports are at-least-once, not exactly-once. A crash after output confirmation but before source acknowledgement can duplicate output; consumers must deduplicate.

## 6. Pilot versus production

### Pilot must-haves

- Named owners and approved routing/review contract.
- Approved real dataset and locked leakage-safe evaluation set.
- Nonproduction VPN, dedicated queue, topic subscriptions, least-privilege ACLs, TLS roots, and secret delivery.
- Agreed error costs and acceptance thresholds.
- Shadow mode only: record decisions without automatic business actions.
- Failure tests for disconnect/reconnect, replay, duplicate delivery, worker timeout/restart, output failure, queue saturation, and graceful shutdown.
- Load tests measuring p50/p95/p99 latency, EPS, burst tolerance, spool growth, and backlog recovery.
- A functioning human-review sink whose capacity exceeds measured review volume.

### Production must-haves

- All pilot gates passed on the locked real-data test set.
- Immutable model/config/calibration identifiers in every output and a tested rollback procedure.
- High availability with unique client IDs or an approved non-exclusive queue design.
- Health/readiness endpoints and monitoring for queue depth, unacknowledged messages, reconnects, worker restarts, inference/publish latency, OOD, truncation, and review backlog.
- Capacity tests on the exact deployment OS/hardware.
- Security review, credential/certificate rotation test, data-retention controls, and operational runbook.
- Canary rollout before any automatic routing action.

## 7. Acceptance plan

Compare three frozen candidates on the same locked real test set:

1. deterministic topic/schema rules
2. untouched multilingual Laya
3. tuned Laya

Business owners must choose numeric gates before seeing final results. At minimum measure:

- overall and per-route precision/recall, with a critical fraud-recall gate
- cost-weighted error under the approved mistake matrix
- accepted precision and review coverage
- OOD review recall and false-review rate
- English/French parity
- topic-conflict, schema-version, source-system, and unseen-topic slices
- NLL, Brier score, ECE, and reliability curves
- truncation rate, with explicit reject/review behavior
- p50/p95/p99 end-to-end latency and sustained/burst throughput
- reconnect, replay, duplicate, acknowledgement, backpressure, and backlog-recovery behavior

## 8. Hardware guidance

The current 16 GiB M4 is sufficient for the head-only prototype. The complete 8,000-example pass ran locally; more GPU is not the first priority.

For final-block/full-encoder trials or higher inference concurrency, a borrowed or rented **single NVIDIA L4 with 24 GB GPU memory** is a reasonable starting budget to profile, not a guaranteed fit. Plan approximately **32–64 GB host RAM and 50 GB free disk** for datasets, caches, optimizer state, and atomic checkpoints. Port and test the current MPS-specific training/inference wrapper on Linux/CUDA before treating that GPU recommendation as usable. Do not add multiple GPUs until measured scaling and a learning curve justify them.

Attention memory rises quickly with context length. The current 384-token limit was selected only after all 10,000 synthetic states were audited with zero truncation.

## 9. Current limitations

- All labels and events are synthetic; no claim about real Solace traffic quality is valid yet.
- Overall accuracy includes provisional labels for 40 intentionally unknown events; use known-owner accuracy as the operational headline.
- Fraud-review recall is 0.416 and remains insufficient without an owner-approved threshold.
- Calibration worsened held-out ECE; the current review policy caught only 7.5% of OOD examples.
- One serial worker is implemented. A 51.7 ms warm median from five observations implies an arithmetic ceiling around 19 events/s, not sustainable measured throughput.
- Cold load, new MPS tensor shapes, broker round trips, retries, and bursts are not represented by the warm number.
- The model supports one selected route, not an approved fan-out policy.
- Only macOS ARM/MPS training and inference are verified. Linux/CUDA requires an explicit port and test.
- The default Go build is pure-Go MQTT. Native SMF requires CGO, the bundled Solace C library, and OpenSSL.
- Live source publication was verified with SDKPerf. The unified 10,000-result SMF audit is still pending final verifier output; end-to-end broker counts must come from `outputs/smf_live_result.json` and the updated run manifest, never from unit tests or earlier cleanup runs.

## 10. Immediate Solace deliverables

1. Name the application, route, fraud/risk, data, platform, security, and SRE owners.
2. Approve the route/fan-out/precedence/review contract and error-cost matrix.
3. Provide the first 2,000–5,000 anonymized real labeled events plus independent validation and locked test sets.
4. Confirm the dedicated VPN, queue mode, subscription, ACLs, spool quota, TLS roots, and secret-delivery mechanism.
5. State p95/p99 latency, EPS, burst, backlog, review-capacity, and recovery objectives.
6. Schedule a shadow-mode acceptance exercise covering all failure cases above.

## Primary references

- [Solace Messaging API for Go supported environments](https://docs.solace.com/API/API-Developer-Guide-Go/Go-API-supported-Environments.htm)
- [Solace Go API v1.10.1](https://pkg.go.dev/solace.dev/go/messaging@v1.10.1)
- [Solace: Using MQTT](https://docs.solace.com/API/MQTT/Using-MQTT.htm)
- [Solace: Managing MQTT Sessions](https://docs.solace.com/Configuring-and-Managing/Managing-MQTT-Sessions.htm)
- [Solace SDKPerf](https://docs.solace.com/API/SDKPerf/SDKPerf.htm)
- [SDKPerf command-line options](https://docs.solace.com/API/SDKPerf/Command-Line-Options.htm)
- [NVIDIA L4 Tensor Core GPU](https://www.nvidia.com/en-gb/data-center/l4/)

Detailed metrics are in `outputs/metrics.json`; revisions, hardware, training exposure, SDKPerf, and live transport evidence are in `outputs/run_manifest.json`.
