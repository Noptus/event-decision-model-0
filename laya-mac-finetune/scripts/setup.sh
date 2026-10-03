#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAYA_DIR="${ROOT}/upstream/laya"
LAYA_REVISION="fa9a2a7070b1789912a49ae24603bbfb1a78b001"
PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/bin/python3.11}"

source "${ROOT}/scripts/env.sh"
mkdir -p "${UV_CACHE_DIR}" "${PIP_CACHE_DIR}" "${HF_HUB_CACHE}" \
  "${HF_DATASETS_CACHE}" "${TORCH_HOME}" "${TMPDIR}" "${ROOT}/models" "${ROOT}/outputs"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it outside this script or set PATH to an existing uv binary." >&2
  exit 1
fi
if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Compatible Python 3.11 not found at ${PYTHON_BIN}. Set PYTHON_BIN explicitly." >&2
  exit 1
fi

if [[ ! -d "${LAYA_DIR}/.git" ]]; then
  mkdir -p "${ROOT}/upstream"
  git clone --filter=blob:none https://github.com/NandhaKishorM/laya.git "${LAYA_DIR}"
fi
actual_revision="$(git -C "${LAYA_DIR}" rev-parse HEAD)"
if [[ "${actual_revision}" != "${LAYA_REVISION}" ]]; then
  if [[ -n "$(git -C "${LAYA_DIR}" status --porcelain)" ]]; then
    echo "Refusing to replace a modified upstream checkout at ${LAYA_DIR}." >&2
    exit 1
  fi
  git -C "${LAYA_DIR}" fetch origin "${LAYA_REVISION}"
  git -C "${LAYA_DIR}" checkout --detach "${LAYA_REVISION}"
fi

cd "${ROOT}"
uv sync --python "${PYTHON_BIN}" --dev
uv run python scripts/inspect_system.py
uv run python scripts/download_model.py
uv run python - <<'PY'
import laya
import torch
print("torch", torch.__version__)
print("laya", laya.__version__)
print("mps_available", torch.backends.mps.is_available())
if torch.backends.mps.is_available():
    print("mps_probe", (torch.ones(1, device="mps") + 1).item())
PY
