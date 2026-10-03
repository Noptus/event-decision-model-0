#!/usr/bin/env bash
set -euo pipefail
umask 077

BRIDGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_ROOT="$(cd "${BRIDGE_ROOT}/.." && pwd)"
STATE_DIR="${BRIDGE_ROOT}/.state/live-audit"
BRIDGE_LOG="${STATE_DIR}/bridge.log"
BRIDGE_STDOUT="${STATE_DIR}/bridge.stdout"
AUDIT_FILE="${STATE_DIR}/confirmed-results.jsonl"
REPLAY_REPORT="${PROJECT_ROOT}/outputs/sdkperf_replay.json"
VERIFY_REPORT="${PROJECT_ROOT}/outputs/smf_live_result.json"

cd "${BRIDGE_ROOT}"
if [[ ! -f .env ]]; then
  printf '%s\n' 'Missing owner-only solace-go/.env' >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
. ./.env
set +a
for key in SOLACE_BROKER_URL SOLACE_USERNAME SOLACE_PASSWORD SOLACE_SMF_VPN SOLACE_SMF_QUEUE SOLACE_INPUT_FILTER SOLACE_OUTPUT_TOPIC; do
  eval 'value=${'"${key}"':-}'
  if [[ -z "${value}" ]]; then
    printf 'Missing required variable: %s\n' "${key}" >&2
    exit 1
  fi
done

./scripts/install-sdkperf.sh >/dev/null
make build-smf >/dev/null
mkdir -p "${STATE_DIR}"
rm -f "${BRIDGE_LOG}" "${BRIDGE_STDOUT}" "${AUDIT_FILE}"

bridge_pid=""
cleanup() {
  if [[ -n "${bridge_pid}" ]] && kill -0 "${bridge_pid}" 2>/dev/null; then
    kill -INT "${bridge_pid}" 2>/dev/null || true
    wait "${bridge_pid}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

./bin/laya-solace-bridge-smf \
  --transport smf \
  --model ../outputs/best-model \
  --device mps \
  --queue-capacity 256 \
  --enqueue-timeout 30s \
  --inference-timeout 30s \
  --publish-timeout 30s \
  --shutdown-timeout 60s \
  --max-messages 10000 \
  --audit-file "${AUDIT_FILE}" \
  >"${BRIDGE_STDOUT}" 2>"${BRIDGE_LOG}" &
bridge_pid=$!

for _ in $(seq 1 120); do
  if grep -q 'msg="bridge ready"' "${BRIDGE_LOG}" 2>/dev/null; then
    break
  fi
  if ! kill -0 "${bridge_pid}" 2>/dev/null; then
    printf 'SMF bridge exited before readiness; inspect %s\n' "${BRIDGE_LOG}" >&2
    exit 1
  fi
  sleep 1
done
if ! grep -q 'msg="bridge ready"' "${BRIDGE_LOG}" 2>/dev/null; then
  printf 'SMF bridge did not become ready; inspect %s\n' "${BRIDGE_LOG}" >&2
  exit 1
fi

"${PROJECT_ROOT}/.venv/bin/python" "${PROJECT_ROOT}/scripts/run_sdkperf_replay.py" \
  --execute --limit 10000 --batch-size 250 --rate 8 --report "${REPLAY_REPORT}"

wait "${bridge_pid}"
bridge_pid=""
"${PROJECT_ROOT}/.venv/bin/python" "${PROJECT_ROOT}/scripts/verify_broker_results.py" \
  --audit "${AUDIT_FILE}" --expected 10000 --output-prefix "edm0/pilot/results/" --report "${VERIFY_REPORT}"
printf 'Live SMF audit passed; logs: %s; result: %s\n' "${BRIDGE_LOG}" "${VERIFY_REPORT}"
