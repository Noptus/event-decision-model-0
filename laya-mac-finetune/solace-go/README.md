# Solace MQTT → Laya routing bridge

A small Go process subscribes to JSON business events over MQTT 3.1.1, sends each event to one long-lived local Python/Laya worker, and publishes an event-correlated decision envelope. It is intentionally a bridge, not a Solace broker client built on the native C API.

The Python checkpoint loads once. Normal requests use the already-resident MPS model; they do **not** launch a Python process per event. Before connecting a real broker, use the cross-team checklist in [Solace requirements](../docs/solace-requirements.md).

## Data path

```text
Solace MQTT subscription (QoS 1, manual ack)
  -> bounded Go queue
  -> persistent stdin/stdout JSONL worker
  -> native Laya typed choice + six probabilities + truncation metadata
  -> configurable MQTT output topic (QoS 1)
  -> acknowledge input only after output publish completes
```

Defaults:

- input filter: `acme/prod/+/events/#`
- output template: `acme/prod/ai/laya-routing/{route}`
- client ID: `laya-event-router`
- QoS: `1`
- clean session: `false`
- queue capacity: 32
- maximum event: 256 KiB
- inference/publish timeouts: 5 seconds each
- checkpoint: `../outputs/best-model`
- device: `auto` (MPS on the reference Mac)

## Build with the project-local Go toolchain

Go is not required globally. The installer downloads the official `go1.27.1.darwin-arm64.tar.gz` archive under `../.cache/tools`, verifies SHA-256 `ee215d57e0ec269c60cc9ceca68e6bda321ba9ee5afe24f4b0988703c2d87d12`, and does not use `sudo` or alter system directories.

```bash
cd laya-mac-finetune/solace-go
./scripts/install-go.sh
source scripts/env.sh
go mod download
go test ./...
go test -race ./...
go build -trimpath -o bin/laya-solace-bridge ./cmd/laya-solace-bridge
```

The MQTT dependency is pinned to the official Eclipse Paho client, `github.com/eclipse/paho.mqtt.golang v1.5.1`, in `go.mod`/`go.sum`.

## Offline end-to-end demo

This exercises the built Go bridge, persistent JSONL subprocess, real saved checkpoint, MPS inference, topic rendering, and output envelopes without claiming broker connectivity:

```bash
make offline-demo
```

Or explicitly:

```bash
./bin/laya-solace-bridge \
  --offline-stdin \
  --model ../outputs/best-model \
  --device mps \
  --offline-input-topic acme/prod/eu/events/demo \
  < testdata/events.jsonl
```

Logs and model diagnostics go to stderr. Stdout contains only machine-readable JSONL publications. The first event includes Metal warm-up; subsequent requests use the resident model. Repeated local runs confirm approximately 41–43 ms for warm requests with zero dropped state tokens. Cold model loading and the first new Metal input shape vary substantially and are excluded from that warm figure. A captured three-event run is saved at `../outputs/solace_go_offline_demo.jsonl`; the exact documented `make offline-demo` target also passed from this space-containing path.

Run the real worker integration test explicitly:

```bash
LAYA_REAL_WORKER_TEST=1 LAYA_PROJECT_ROOT=.. go test ./internal/worker -run TestRealSavedModelWorker -v
```

## Configure Solace PubSub+

Use the exact values from the broker service's **Connect** page for MQTT. Do not synthesize a `user@VPN` username: supply the host, port, username, and password exactly as provisioned for the target Message VPN.

```bash
cp config.example.env config.env
# Edit config.env. Keep credentials out of git.
set -a
source config.env
set +a
./bin/laya-solace-bridge
```

Common Solace endpoints are:

- `tcp://HOST:1883` for MQTT
- `ssl://HOST:8883` for MQTT over TLS
- WebSocket listeners commonly use ports 8000 and 8443 (`ws://` / `wss://`)

Ports are configured per Message VPN and can differ from defaults. The bridge relies on normal Go TLS certificate verification for `ssl://`/`wss://`; there is deliberately no insecure-TLS flag.

Every setting has a CLI flag and an environment equivalent:

| Flag | Environment | Purpose |
|---|---|---|
| `--broker-url` | `SOLACE_BROKER_URL` | MQTT endpoint |
| `--input-filter` | `SOLACE_INPUT_FILTER` | Source topic filter (`+` and final `#` supported) |
| `--output-topic` | `SOLACE_OUTPUT_TOPIC` | Topic/template for decisions |
| `--username` | `SOLACE_USERNAME` | Broker username |
| `--password` | `SOLACE_PASSWORD` | Broker password; env is safer than process arguments |
| `--client-id` | `SOLACE_CLIENT_ID` | Stable MQTT session identity |
| `--qos` | `SOLACE_QOS` | `0` or `1`; default `1` |
| `--clean-session` | `SOLACE_CLEAN_SESSION` | Default `false` |
| `--model` | `LAYA_MODEL` | Local checkpoint path |
| `--device` | `LAYA_DEVICE` | `auto`, `mps`, or `cpu` |
| `--python` | `LAYA_PYTHON` | Project virtualenv Python |
| `--worker-script` | `LAYA_WORKER_SCRIPT` | JSONL worker path |
| `--queue-capacity` | `BRIDGE_QUEUE_CAPACITY` | Bound on accepted in-memory events |
| `--max-event-bytes` | `BRIDGE_MAX_EVENT_BYTES` | Payload bound |
| timeout flags | `BRIDGE_*_TIMEOUT` | Enqueue, inference, publish, startup, shutdown deadlines |

The bridge never logs the username or password. CLI passwords can be visible to local process-inspection tools, so prefer `SOLACE_PASSWORD` supplied by your runtime secret mechanism.

### Output topic templates

`--output-topic` accepts:

- `{route}` — selected route when accepted; `review` when `review_required=true` or for an error envelope
- `{correlation_id}` — `correlation_id`, `event_id`, or `id` from the event/payload; otherwise a stable payload hash prefix
- `{input_topic}` — original MQTT topic hierarchy

Example:

```bash
export SOLACE_OUTPUT_TOPIC='acme/prod/routing/{route}/{correlation_id}'
```

Publishing is refused if the rendered output matches the configured input filter. Output envelopes also carry `"producer":"laya-solace-bridge"`; if one is received anyway, it is acknowledged and discarded without inference. Wildcards and NUL bytes are rejected in publish topics.

## Message contract

Input payloads are JSON objects matching the model contract:

```json
{
  "topic": "acme/prod/eu/payments/failed/v1",
  "schema_name": "PaymentFailed",
  "schema_version": "1.0",
  "event_type": "payment.failed",
  "payload": {
    "event_id": "evt-42",
    "amount": 120,
    "currency": "EUR",
    "message": "Le paiement a été refusé"
  }
}
```

Published payload:

```json
{
  "schema_version": "1.0",
  "producer": "laya-solace-bridge",
  "correlation_id": "evt-42",
  "processed_at": "2026-10-03T14:16:34Z",
  "source": {
    "topic": "acme/prod/eu/events/payments",
    "qos": 1,
    "retained": false,
    "duplicate": false,
    "payload_sha256": "..."
  },
  "decision": {
    "selected_route": "payment-operations",
    "probabilities": {
      "order-processing": 0.0165,
      "payment-operations": 0.6092,
      "logistics": 0.0149,
      "inventory-management": 0.0154,
      "customer-support": 0.0959,
      "fraud-review": 0.2481
    },
    "confidence": 0.6092,
    "review_required": false,
    "review_status": "passed",
    "review_threshold": 0.1862,
    "model": {
      "path": "../outputs/best-model",
      "name": "laya-solace-event-mesh-router",
      "fine_tuned": true
    },
    "device": "mps",
    "latency_ms": 41.5,
    "usage": {
      "input_tokens": 219,
      "output_tokens": 0,
      "state_tokens": 91,
      "state_tokens_dropped": 0,
      "truncated": false,
      "truncated_questions": []
    }
  },
  "bridge_latency_ms": 41.7
}
```

The native `usage` object is intentionally preserved. An integration can reject or send for review any result where `truncated` is true or `state_tokens_dropped` is non-zero rather than silently treating a partial event as complete. When `review_required` is true, `{route}` renders as `review`; the model's proposed `selected_route` and probabilities remain unchanged inside the envelope for a reviewer.

Invalid JSON, oversized input, and inference failures become structured error envelopes when the output can be published. If output publication fails, the input is not acknowledged.

## Delivery and shutdown semantics

- Solace documents MQTT QoS 0 as at-most-once and QoS 1 as at-least-once. This bridge defaults to QoS 1 for both subscription and publication and rejects QoS 2 because PubSub+ downgrades it to QoS 1.
- Paho automatic acknowledgements are disabled. A received message is acknowledged only after its result/error envelope has been published successfully.
- A crash after output publication but before input acknowledgement can produce a duplicate output. Consumers should deduplicate by `correlation_id` and, where needed, `source.payload_sha256`. This is **at-least-once, not exactly-once** processing.
- `CleanSession=false` and a stable client ID request a persistent MQTT 3.1.1 session. On normal shutdown the bridge does **not** unsubscribe: it first rejects new local submissions without acknowledging them, drains already accepted events while the connection is live, then disconnects. PubSub+ can retain the QoS 1 subscription and undelivered messages for reconnection.
- With `CleanSession=true`, the broker discards session state on disconnect; offline intake is not retained.
- The in-process queue is bounded. If it remains full past the enqueue timeout, the callback returns without acknowledging that event. Redelivery may require reconnection, depending on broker/client state.
- Paho uses asynchronous handlers (`OrderMatters=false`) so model work and result publication do not block the network reader. Reconnect is automatic, and the `OnConnect` handler re-subscribes after every connection.
- Paho's file store under `.state/mqtt` persists its QoS protocol state. It is not an application database or an idempotent transaction log.
- A Solace MQTT persistent session uses a broker-managed session queue, but it does not expose the full provisioning, access, replay, selector, or operational semantics of a native Solace Guaranteed Messaging queue. Shared MQTT subscriptions are also downgraded to QoS 0 by PubSub+, so do not use `$share/...` when this QoS 1 flow is required.
- Oversized or rejected events are never retained in the Go heap beyond their small metadata. Worker timeout kills and reaps the stuck child; the next event can start a replacement worker.

## Tests

```bash
make test              # mocked worker/publisher, config, child lifecycle
make race              # includes concurrent Submit/Close regression
make real-worker-test  # loads the real saved model and performs one MPS inference
make offline-demo      # three real events through Go -> Python -> Laya -> JSON envelope
```

No live broker test is claimed because no broker endpoint or credentials were supplied.

## Official references

- [Solace: Using MQTT](https://docs.solace.com/API/MQTT/Using-MQTT.htm)
- [Solace: MQTT 3.1.1 conformance](https://docs.solace.com/API/MQTT-311-Prtl-Conformance-Spec/MQTT_311_Prtl_Conformance_Spec.htm)
- [Solace: Managing MQTT Sessions](https://docs.solace.com/Configuring-and-Managing/Managing-MQTT-Sessions.htm)
- [Solace: software broker configuration defaults](https://docs.solace.com/Software-Broker/SW-Broker-Configuration-Defaults.htm)
- [Eclipse Paho Go client v1.5.1](https://pkg.go.dev/github.com/eclipse/paho.mqtt.golang@v1.5.1)
