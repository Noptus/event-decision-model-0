# Solace Event Broker → Laya routing bridge

This Go bridge consumes JSON business events from Solace PubSub+, sends each event to one long-lived local Python/Laya worker, and publishes an event-correlated routing decision. It supports two transports:

- **MQTT 3.1.1** in the default, CGO-free binary.
- **Native SMF Guaranteed Messaging** in an optional `smf` build backed by the official `solace.dev/go/messaging` SDK.

The Python checkpoint is loaded once, not once per message. The current model contract is loaded from the selected checkpoint's `route_contract.json`, so archived and current checkpoints remain self-describing.

## Data path

```text
MQTT subscription or preprovisioned SMF durable queue
  -> bounded Go queue
  -> persistent stdin/stdout JSONL worker
  -> native Laya typed choice + probabilities + truncation metadata
  -> MQTT QoS 1 or confirmed persistent SMF publication
  -> source acknowledgement only after output confirmation
```

When Laya returns `review_required=true`, `{route}` renders as `review`. The proposed route and complete probability distribution remain in the envelope, but the original event is not forwarded automatically.

## Build

Go is installed project-locally; no global installation or `sudo` is used:

```bash
cd laya-mac-finetune/solace-go
./scripts/install-go.sh
source scripts/env.sh
```

The installer pins Go 1.27.1 for Darwin ARM64 and verifies the official archive SHA-256. Dependencies are pinned in `go.mod`/`go.sum`:

- Eclipse Paho MQTT Go `v1.5.1`
- Solace Messaging API for Go `v1.10.1`

### Lightweight MQTT binary

```bash
make build
./bin/laya-solace-bridge --transport mqtt
```

`make build` sets `CGO_ENABLED=0`; the native Solace SDK is excluded by the `smf` build tag.

### Native SMF binary

```bash
make build-smf
./bin/laya-solace-bridge-smf --transport smf
```

The official Go API wraps the Solace C API with CGO. On this Apple Silicon machine, `make build-smf` was verified with Apple Clang, the SDK's bundled Darwin ARM64 `libsolclient.a`, and Homebrew OpenSSL 3. `scripts/prepare-smf.sh` builds an ignored, project-local hashed CA directory from the installed CA bundle. Certificate expiry and server-name validation remain enabled; there is no insecure-TLS switch.

On Linux, install a supported C compiler and OpenSSL development/runtime libraries, then set `OPENSSL_PREFIX` if they are outside standard paths. The Makefile accepts `.dylib`, `.so`, or `.a` OpenSSL libraries. Validate the resulting binary on the exact production Linux image before deployment.

## Checkpoint checkout

The complete `outputs/best-model` checkpoint is versioned with Git LFS. Fetch the LFS objects before starting the worker; an LFS pointer is not a usable model:

```bash
git lfs install
git lfs pull
```

The checkpoint includes its Apache-2.0 license, upstream base-model card, route contract, tokenizer, weights, and calibration identity.

## Offline end-to-end demo

This exercises Go → persistent Python worker → real local checkpoint → output envelope without a broker:

```bash
make offline-demo
```

Logs go to stderr. The bridge process writes only machine-readable JSONL to stdout. The captured result is in `../outputs/solace_go_offline_demo.jsonl`. Warm local MPS calls have been approximately 41–43 ms; cold model loading and first-shape Metal compilation are excluded from that figure.

Run the real worker integration test explicitly:

```bash
make real-worker-test
```

## MQTT configuration

Use exact values from the Solace service's **Connect** page. Do not invent a VPN-qualified username.

```bash
cp config.example.env config.env
# Edit config.env and keep it untracked.
set -a
source config.env
set +a
./bin/laya-solace-bridge --transport mqtt
```

Typical ports are 1883 for MQTT and 8883 for MQTT/TLS, but endpoints are configured per Message VPN and the supplied Connect values are authoritative. MQTT filters use `+` and `#`.

The bridge defaults to QoS 1, `CleanSession=false`, a stable client ID, Paho file-backed protocol state, automatic reconnect/resubscribe, and manual acknowledgement after output publication. With `CleanSession=true`, broker session state is discarded on disconnect.

## Native SMF configuration

Use the native SMF host from the Connect page, normally `tcp://HOST:55555` or `tcps://HOST:55443`, plus a separate VPN name:

```bash
export SOLACE_TRANSPORT=smf
export SOLACE_BROKER_URL='tcps://your-service.messaging.solace.cloud:55443'
export SOLACE_SMF_VPN='your-vpn'
export SOLACE_SMF_QUEUE='edm0-routing-pilot'
export SOLACE_INPUT_FILTER='edm0/pilot/events/>'
export SOLACE_OUTPUT_TOPIC='edm0/pilot/results/{route}'
export SOLACE_USERNAME='...'
export SOLACE_PASSWORD='...'
./bin/laya-solace-bridge-smf --transport smf
```

SMF topic subscriptions use `*` for one level, support a suffix wildcard such as `order*` within one level, and use terminal `>` for one or more levels. MQTT wildcards are rejected in SMF mode and vice versa. Native topics are checked against the 250-byte and 128-level limits before subscribing or publishing.

The queue is expected to be durable and preprovisioned. The normal bridge uses `PersistentReceiverDoNotCreateMissingResources` and never deletes queues or subscriptions. If the platform owner has authorized creation of the dedicated pilot queue, run once:

```bash
./bin/solace-smf-probe \
  --transport smf \
  --smf-provision \
  --smf-add-subscription
```

Provisioning uses only the configured queue and filter, treats an identical existing queue as success, and never deprovisions anything. `--smf-add-subscription` can also be used without provisioning when the client username is explicitly allowed to add the configured subscription. Otherwise, the platform owner must attach that subscription administratively.

### Native delivery semantics

- Input is received from a durable exclusive queue by default; set `--smf-queue-exclusive=false` only for an approved competing-consumer design.
- Automatic acknowledgement is disabled. The message remains owned by the callback until processing completes and is explicitly disposed afterward.
- Output uses `PublishAwaitAcknowledgement` with persistent delivery. Input is accepted only after the broker confirms the output was persisted.
- On graceful shutdown, the receiver is paused, already accepted local work drains while the publisher remains connected, and then receiver/publisher/service are terminated. The durable queue and subscription are not removed.
- A crash after confirmed output publish but before input acknowledgement can create a duplicate. Consumers must deduplicate by `correlation_id` and, where needed, `source.payload_sha256`. This is at-least-once processing, not exactly once.

### Singleton and bounded accounting

Before loading the model, broker mode takes a nonblocking OS file lock keyed by transport, broker, VPN, client ID, and state directory. The filename contains only a hash and the file contains only the PID. A second bridge with the same identity fails fast instead of creating an overlapping consumer; unrelated identities can run concurrently. The lock does not alter broker resources.

`--max-messages=N` enables an in-memory unique-correlation set and stops after `N` acknowledged events. This is intended for finite audits such as the 10,000-event run. With the production default `--max-messages=0`, correlation tracking is disabled so the service cannot grow that set without bound. Final stats explicitly report `correlations_tracked`, `unique_correlations`, successful decisions, error envelopes, publishes, acknowledgements, and failures.

## Configuration

Every setting has a flag and environment equivalent. Credentials are never logged; environment injection is preferred because a password flag is visible to process inspection.

| Flag | Environment | Applies to |
|---|---|---|
| `--transport` | `SOLACE_TRANSPORT` | `mqtt` or `smf` |
| `--broker-url` | `SOLACE_BROKER_URL` | Both |
| `--input-filter` | `SOLACE_INPUT_FILTER` | Both; syntax depends on transport |
| `--output-topic` | `SOLACE_OUTPUT_TOPIC` | Both |
| `--username` | `SOLACE_USERNAME` | Both |
| `--password` | `SOLACE_PASSWORD` | Both |
| `--client-id` | `SOLACE_CLIENT_ID` | MQTT client ID / SMF application ID |
| `--qos` | `SOLACE_QOS` | MQTT only, 0 or 1 |
| `--clean-session` | `SOLACE_CLEAN_SESSION` | MQTT only |
| `--smf-vpn` | `SOLACE_SMF_VPN` | SMF |
| `--smf-queue` | `SOLACE_SMF_QUEUE` | SMF preprovisioned durable queue |
| `--smf-queue-exclusive` | `SOLACE_SMF_QUEUE_EXCLUSIVE` | SMF, default true |
| `--smf-add-subscription` | `SOLACE_SMF_ADD_SUBSCRIPTION` | SMF, explicit queue subscription mutation |
| `--smf-provision` | `SOLACE_SMF_PROVISION` | SMF, explicit creation of only the named queue |
| `--smf-trust-store` | `SOLACE_SMF_TRUST_STORE` | SMF TLS CA directory |
| `--model` | `LAYA_MODEL` | Local checkpoint |
| `--device` | `LAYA_DEVICE` | `auto`, `mps`, or `cpu` |
| `--python` | `LAYA_PYTHON` | Python interpreter for the persistent worker |
| `--worker-script` | `LAYA_WORKER_SCRIPT` | JSONL worker path |
| `--store-dir` | `BRIDGE_STORE_DIR` | MQTT protocol state and singleton-lock parent |
| `--audit-file` | `BRIDGE_AUDIT_FILE` | Optional owner-only JSONL of broker-confirmed outputs |
| `--queue-capacity` | `BRIDGE_QUEUE_CAPACITY` | Bounded in-process queue length |
| `--max-event-bytes` | `BRIDGE_MAX_EVENT_BYTES` | Maximum inbound payload size |
| `--max-messages` | `BRIDGE_MAX_MESSAGES` | Stop after N acknowledged events; 0 disables correlation tracking and runs until signal |
| `--enqueue-timeout` | `BRIDGE_ENQUEUE_TIMEOUT` | Maximum callback enqueue wait |
| `--inference-timeout` | `BRIDGE_INFERENCE_TIMEOUT` | Per-event worker deadline |
| `--publish-timeout` | `BRIDGE_PUBLISH_TIMEOUT` | Per-result broker publish deadline |
| `--startup-timeout` | `BRIDGE_STARTUP_TIMEOUT` | Persistent-worker startup deadline |
| `--shutdown-timeout` | `BRIDGE_SHUTDOWN_TIMEOUT` | Graceful drain deadline |
| `--idle-timeout` | `BRIDGE_IDLE_TIMEOUT` | Stop after inactivity; 0 disables |
| `--offline-stdin` | — | Read JSONL from stdin instead of a broker |
| `--offline-input-topic` | `BRIDGE_OFFLINE_INPUT_TOPIC` | Source topic recorded by offline mode |

## Message contract

Input is a UTF-8 JSON string or byte-array payload. Native SDT map/stream payloads are rejected with a structured `unsupported_payload` result; they are never silently coerced.

The output envelope contains:

- `correlation_id`, taken from native correlation/application message metadata first, then JSON IDs, then a payload hash
- source `transport`, delivery semantics, topic, redelivery flag, and payload SHA-256
- selected route and probability for every route
- confidence plus review status/threshold
- checkpoint identity and actual inference device
- model and bridge latency
- native Laya token usage, including `truncated` and `state_tokens_dropped`

Output templates support `{route}`, `{correlation_id}`, and `{input_topic}`. A review-required or error result uses `review` for `{route}` while retaining the proposed model route in the JSON. Output topics matching the input filter are refused to prevent loops.

## SDKPerf export and replay

SDKPerf is used only as a traffic generator/replayer; it did not create labels or business semantics.

```bash
./scripts/install-sdkperf.sh
../.venv/bin/python ../scripts/export_sdkperf.py
../.venv/bin/python ../scripts/run_sdkperf_replay.py --batch-size 250   # dry run

# Authorized nonproduction replay; reads secrets only from ignored .env.
set -a
source .env
set +a
../.venv/bin/python ../scripts/run_sdkperf_replay.py \
  --execute --limit 10000 --batch-size 250 --rate 500
```

The exporter creates 10,000 distinct payload files in the ignored cache and a tracked manifest pairing each payload with its own `edm0/pilot/events/...` topic. SDKPerf uses `-pal` and an equal-length `-ptl` list per shard, `-mt=persistent`, and its documented `-cpf` password-file option. The temporary owner-only password file is deleted in `finally`.

The executed replay reported 10,000 messages transmitted, 10,000 publish ACK events, and zero NACK events. This proves broker acceptance of 10,000 distinct payload/topic pairs; it is not by itself proof that every downstream result was consumed.

## Bounded native-SMF audit

For an authorized, supervised nonproduction run, `./scripts/run-live-smf-audit.sh` starts exactly one singleton-protected native consumer, replays 10,000 persistent source messages, waits for 10,000 acknowledged results, and then runs `verify_broker_results.py`. Do not launch a second consumer with the same identity.

The optional audit file is mode `0600` and is appended only after the result publisher confirms success and before the source acknowledgement. It contains decision envelopes and source hashes/topics, so treat it as event data even though it contains no broker credentials. The generated files are:

- `.state/live-audit/confirmed-results.jsonl`: local confirmed-publication evidence
- `.state/live-audit/bridge.log`: redacted progress and final counters
- `../outputs/smf_live_result.json`: exact correlation/hash/topic, probability, review-topic, transport, latency, and zero-truncation checks

The verifier requires all expected correlations, valid successful decisions, finite normalized eight-route probabilities, and zero truncated inputs. Its latency summary reports the first cold decision separately, then warm p50/p95/p99 and warm/overall maxima for native model `latency_ms` and bridge processing `bridge_latency_ms`. Bridge processing starts after dequeue and ends when the envelope is created; it excludes queue wait, broker publish confirmation and source acknowledgement. Duplicate outputs are reported explicitly but are not by themselves a failure under at-least-once delivery. The unified audit was stopped at the user’s request before final verification. Its partial results are recorded in the run manifest and are not claimed as complete 10,000-event inference evidence. Earlier overlapping-consumer activity and residual queue cleanup are excluded as evidence.

## Testing

```bash
make test          # CGO-free MQTT/stub build
make race
make test-smf      # native Solace SDK build
make race-smf
make vet
make vet-smf
make build
make build-smf
make real-worker-test
make offline-demo
```

The tests cover mocked publisher/worker behavior, output-before-input-ack ordering, failed publication, graceful drain-before-disconnect, concurrent submit/close, bounded versus unbounded correlation accounting, singleton locks, child write timeout/reaping, MQTT and SMF wildcard differences, native topic limits, UTF-8 native payload extraction, configuration validation, and the real saved model worker.

No live-broker result should be inferred from unit tests. See the run manifest for separately recorded live checks.

## Official references

- [Solace Messaging API for Go supported environments](https://docs.solace.com/API/API-Developer-Guide-Go/Go-API-supported-Environments.htm)
- [Solace Go API module](https://pkg.go.dev/solace.dev/go/messaging@v1.10.1)
- [Solace: Using MQTT](https://docs.solace.com/API/MQTT/Using-MQTT.htm)
- [Solace: Managing MQTT Sessions](https://docs.solace.com/Configuring-and-Managing/Managing-MQTT-Sessions.htm)
- [Solace SDKPerf](https://docs.solace.com/API/SDKPerf/SDKPerf.htm)
- [SDKPerf command-line options](https://docs.solace.com/API/SDKPerf/Command-Line-Options.htm)
- [Eclipse Paho Go v1.5.1](https://pkg.go.dev/github.com/eclipse/paho.mqtt.golang@v1.5.1)
