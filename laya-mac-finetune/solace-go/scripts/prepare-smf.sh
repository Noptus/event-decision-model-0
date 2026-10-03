#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TRUST_DIR="${PROJECT_ROOT}/.cache/solace-trust"
OPENSSL_PREFIX="${OPENSSL_PREFIX:-/opt/homebrew/opt/openssl}"

if [[ "$(uname -s)" == "Darwin" ]]; then
  test -d "${OPENSSL_PREFIX}/include" || { printf 'OpenSSL headers not found under %s\n' "${OPENSSL_PREFIX}" >&2; exit 1; }
  ls "${OPENSSL_PREFIX}"/lib/libssl.* >/dev/null 2>&1 || { printf 'OpenSSL library not found under %s\n' "${OPENSSL_PREFIX}" >&2; exit 1; }
  bundle="${OPENSSL_PREFIX}/etc/openssl@3/cert.pem"
  [[ -f "${bundle}" ]] || bundle="/opt/homebrew/etc/openssl@3/cert.pem"
  test -f "${bundle}" || { printf 'CA bundle not found; set SOLACE_SMF_TRUST_STORE to a hashed CA directory\n' >&2; exit 1; }
  mkdir -p "${TRUST_DIR}"
  rm -f "${TRUST_DIR}"/cert-*.pem "${TRUST_DIR}"/*.0 "${TRUST_DIR}"/*.1 "${TRUST_DIR}"/*.2
  python3 - "${bundle}" "${TRUST_DIR}" <<'PY'
from pathlib import Path
import sys
source = Path(sys.argv[1]).read_text(encoding="utf-8")
target = Path(sys.argv[2])
count = 0
for part in source.split("-----END CERTIFICATE-----"):
    if "-----BEGIN CERTIFICATE-----" not in part:
        continue
    (target / f"cert-{count:03d}.pem").write_text(
        part.strip() + "\n-----END CERTIFICATE-----\n", encoding="utf-8"
    )
    count += 1
if not count:
    raise SystemExit("CA bundle contained no certificates")
PY
  "${OPENSSL_PREFIX}/bin/openssl" rehash "${TRUST_DIR}" >/dev/null
  printf 'Prepared project-local SMF trust store with %s CA links\n' "$(find "${TRUST_DIR}" -maxdepth 1 -type l | wc -l | tr -d ' ')"
else
  test -d "${OPENSSL_PREFIX}/include" || { printf 'OpenSSL headers not found under %s\n' "${OPENSSL_PREFIX}" >&2; exit 1; }
  printf 'Using system OpenSSL prefix %s; set SOLACE_SMF_TRUST_STORE to the platform CA directory.\n' "${OPENSSL_PREFIX}"
fi
