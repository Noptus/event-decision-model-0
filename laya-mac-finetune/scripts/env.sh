#!/usr/bin/env bash
# Source this file from the project root before direct uv commands. Set
# LAYA_MAC_FINETUNE_ROOT when sourcing it from another directory or shell.
if [[ -n "${LAYA_MAC_FINETUNE_ROOT:-}" ]]; then
  _PROJECT_ROOT="${LAYA_MAC_FINETUNE_ROOT}"
elif [[ -f "$(pwd)/configs/experiment.yaml" ]]; then
  _PROJECT_ROOT="$(pwd)"
elif [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  _PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
else
  echo "Source scripts/env.sh from the laya-mac-finetune project root." >&2
  return 1
fi
export UV_PROJECT_ENVIRONMENT="${_PROJECT_ROOT}/.venv"
export UV_CACHE_DIR="${_PROJECT_ROOT}/.cache/uv"
export PIP_CACHE_DIR="${_PROJECT_ROOT}/.cache/pip"
export XDG_CACHE_HOME="${_PROJECT_ROOT}/.cache"
export HF_HOME="${_PROJECT_ROOT}/.cache/huggingface"
export HF_HUB_CACHE="${HF_HOME}/hub"
export HF_DATASETS_CACHE="${HF_HOME}/datasets"
export TORCH_HOME="${_PROJECT_ROOT}/.cache/torch"
export TMPDIR="${_PROJECT_ROOT}/.tmp"
export PYTORCH_ENABLE_MPS_FALLBACK=1
export USE_TF=0
unset _PROJECT_ROOT
