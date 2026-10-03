#!/usr/bin/env bash
set -euo pipefail

GO_VERSION="1.27.1"
ARCHIVE="go${GO_VERSION}.darwin-arm64.tar.gz"
EXPECTED_SHA256="ee215d57e0ec269c60cc9ceca68e6bda321ba9ee5afe24f4b0988703c2d87d12"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TOOLS_DIR="${PROJECT_ROOT}/.cache/tools"
TARGET="${TOOLS_DIR}/go${GO_VERSION}"
ARCHIVE_PATH="${TOOLS_DIR}/${ARCHIVE}"
TEMP_DIR="${TOOLS_DIR}/.go${GO_VERSION}-install"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "This pinned installer supports only darwin-arm64; install Go >=1.24 another way." >&2
  exit 1
fi
if [[ -x "${TARGET}/bin/go" ]]; then
  "${TARGET}/bin/go" version
  exit 0
fi

mkdir -p "${TOOLS_DIR}"
curl --fail --location --retry 3 --output "${ARCHIVE_PATH}" \
  "https://go.dev/dl/${ARCHIVE}"
actual_sha256="$(shasum -a 256 "${ARCHIVE_PATH}" | cut -d ' ' -f 1)"
if [[ "${actual_sha256}" != "${EXPECTED_SHA256}" ]]; then
  echo "Go archive checksum mismatch: expected ${EXPECTED_SHA256}, got ${actual_sha256}" >&2
  rm -f "${ARCHIVE_PATH}"
  exit 1
fi

rm -rf "${TEMP_DIR}"
mkdir -p "${TEMP_DIR}"
tar -xzf "${ARCHIVE_PATH}" -C "${TEMP_DIR}"
mv "${TEMP_DIR}/go" "${TARGET}"
rmdir "${TEMP_DIR}"
rm -f "${ARCHIVE_PATH}"
"${TARGET}/bin/go" version
