#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TOOLS="${PROJECT_ROOT}/.cache/tools"
JRE_VERSION="21.0.12.1"
JRE_ARCHIVE="OpenJDK21U-jre_aarch64_mac_hotspot_21.0.12.1_1.tar.gz"
JRE_SHA256="dec50fc6f9fcd4fe3ae8cabf5a5fa68f6afc48841f7698e468e9aa5d54beed84"
JRE_URL="https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12.1%2B1/${JRE_ARCHIVE}"
JRE_DIR="${TOOLS}/java/temurin-${JRE_VERSION}"
SDKPERF_VERSION="10.30.2"
SDKPERF_ARCHIVE="sdkperf-jcsmp-${SDKPERF_VERSION}.zip"
SDKPERF_SHA256="be88867d3a1c58948eb107749fd53ab4d12eabf9b1554b46c2e7a2dacb2ddb68"
SDKPERF_URL="https://products.solace.com/download/SDKPERF_JAVA"
SDKPERF_PARENT="${TOOLS}/sdkperf"
SDKPERF_DIR="${SDKPERF_PARENT}/sdkperf-jcsmp-${SDKPERF_VERSION}/sdkperf-jcsmp-${SDKPERF_VERSION}"

verify() {
  local file="$1" expected="$2" actual
  actual="$(shasum -a 256 "${file}" | cut -d ' ' -f 1)"
  if [[ "${actual}" != "${expected}" ]]; then
    printf 'Checksum mismatch for %s\nexpected %s\nactual   %s\n' "${file}" "${expected}" "${actual}" >&2
    return 1
  fi
}

mkdir -p "${TOOLS}/java" "${SDKPERF_PARENT}"
if [[ ! -x "${JRE_DIR}/Contents/Home/bin/java" ]]; then
  archive_path="${TOOLS}/java/${JRE_ARCHIVE}"
  curl --fail --location --retry 3 --output "${archive_path}" "${JRE_URL}"
  verify "${archive_path}" "${JRE_SHA256}"
  rm -rf "${JRE_DIR}"
  mkdir -p "${JRE_DIR}"
  tar -xzf "${archive_path}" -C "${JRE_DIR}" --strip-components=1
  rm -f "${archive_path}"
fi

if [[ ! -x "${SDKPERF_DIR}/sdkperf_java.sh" ]]; then
  archive_path="${SDKPERF_PARENT}/${SDKPERF_ARCHIVE}"
  curl --fail --location --retry 3 --output "${archive_path}" "${SDKPERF_URL}"
  verify "${archive_path}" "${SDKPERF_SHA256}"
  rm -rf "${SDKPERF_PARENT}/sdkperf-jcsmp-${SDKPERF_VERSION}"
  unzip -q "${archive_path}" -d "${SDKPERF_PARENT}/sdkperf-jcsmp-${SDKPERF_VERSION}"
  rm -f "${archive_path}"
fi

export JAVA_HOME="${JRE_DIR}/Contents/Home"
export PATH="${JAVA_HOME}/bin:${PATH}"
"${JAVA_HOME}/bin/java" -version
printf 'SDKPerf %s installed at %s\n' "${SDKPERF_VERSION}" "${SDKPERF_DIR}"
printf 'Run help with: %s -hm\n' "${SDKPERF_DIR}/sdkperf_java.sh"
