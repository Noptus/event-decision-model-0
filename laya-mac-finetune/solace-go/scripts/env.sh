#!/usr/bin/env bash
# Source from solace-go/, or set LAYA_SOLACE_GO_ROOT explicitly.
if [[ -n "${LAYA_SOLACE_GO_ROOT:-}" ]]; then
  _BRIDGE_ROOT="${LAYA_SOLACE_GO_ROOT}"
elif [[ -f "$(pwd)/go.mod" ]]; then
  _BRIDGE_ROOT="$(pwd)"
elif [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  _BRIDGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
else
  echo "Source scripts/env.sh from the solace-go directory." >&2
  return 1
fi
_PROJECT_ROOT="$(cd "${_BRIDGE_ROOT}/.." && pwd)"
export PATH="${_PROJECT_ROOT}/.cache/tools/go1.27.1/bin:${PATH}"
export GOPATH="${_PROJECT_ROOT}/.cache/go"
export GOMODCACHE="${GOPATH}/pkg/mod"
export GOCACHE="${GOPATH}/build"
export GOTMPDIR="${_PROJECT_ROOT}/.tmp"
export PYTORCH_ENABLE_MPS_FALLBACK=1
export USE_TF=0
export HF_HOME="${_PROJECT_ROOT}/.cache/huggingface"
export HF_HUB_CACHE="${HF_HOME}/hub"
unset _BRIDGE_ROOT _PROJECT_ROOT
