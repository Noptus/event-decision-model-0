PROJECT_ROOT := $(abspath ..)
GO := $(PROJECT_ROOT)/.cache/tools/go1.27.1/bin/go
GO_ENV := GOPATH="$(PROJECT_ROOT)/.cache/go" GOMODCACHE="$(PROJECT_ROOT)/.cache/go/pkg/mod" GOCACHE="$(PROJECT_ROOT)/.cache/go/build" GOTMPDIR="$(PROJECT_ROOT)/.tmp"
PURE_GO_ENV := $(GO_ENV) CGO_ENABLED=0
ifeq ($(shell uname -s),Darwin)
OPENSSL_PREFIX ?= /opt/homebrew/opt/openssl
else
OPENSSL_PREFIX ?= /usr
endif
SMF_ENV := $(GO_ENV) CGO_ENABLED=1 CGO_CFLAGS="-I$(OPENSSL_PREFIX)/include" CGO_LDFLAGS="-L$(OPENSSL_PREFIX)/lib"

.PHONY: toolchain sdkperf-tools tidy fmt test race vet build check-smf-deps test-smf race-smf vet-smf build-smf real-worker-test worker-smoke offline-demo sdkperf-export sdkperf-replay live-smf-audit

toolchain:
	./scripts/install-go.sh

sdkperf-tools:
	./scripts/install-sdkperf.sh

tidy: toolchain
	$(GO_ENV) "$(GO)" mod tidy

fmt: toolchain
	$(GO_ENV) "$(GO)" fmt ./...

test: toolchain
	$(PURE_GO_ENV) "$(GO)" test ./...

race: toolchain
	$(GO_ENV) CGO_ENABLED=1 "$(GO)" test -race ./...

vet: toolchain
	$(PURE_GO_ENV) "$(GO)" vet ./...

build: toolchain
	mkdir -p bin
	$(PURE_GO_ENV) "$(GO)" build -trimpath -o bin/laya-solace-bridge ./cmd/laya-solace-bridge

check-smf-deps:
	@OPENSSL_PREFIX="$(OPENSSL_PREFIX)" ./scripts/prepare-smf.sh

test-smf: toolchain check-smf-deps
	$(SMF_ENV) "$(GO)" test -tags smf ./...

race-smf: toolchain check-smf-deps
	$(SMF_ENV) "$(GO)" test -race -tags smf ./...

vet-smf: toolchain check-smf-deps
	$(SMF_ENV) "$(GO)" vet -tags smf ./...

build-smf: toolchain check-smf-deps
	mkdir -p bin
	$(SMF_ENV) "$(GO)" build -tags smf -trimpath -o bin/laya-solace-bridge-smf ./cmd/laya-solace-bridge
	$(SMF_ENV) "$(GO)" build -tags smf -trimpath -o bin/solace-smf-probe ./cmd/solace-smf-probe

real-worker-test: toolchain
	$(GO_ENV) LAYA_REAL_WORKER_TEST=1 LAYA_PROJECT_ROOT="$(PROJECT_ROOT)" "$(GO)" test ./internal/worker -run TestRealSavedModelWorker -v

worker-smoke: build
	$(GO_ENV) LAYA_SOLACE_GO_ROOT="$(CURDIR)" ./bin/laya-solace-bridge --offline-stdin --model ../outputs/best-model --device mps < testdata/events.jsonl

offline-demo: worker-smoke

sdkperf-export:
	../.venv/bin/python ../scripts/export_sdkperf.py

sdkperf-replay: sdkperf-tools sdkperf-export
	@set -a; . ./.env; set +a; ../.venv/bin/python ../scripts/run_sdkperf_replay.py --execute --batch-size 250 --rate 20

live-smf-audit: sdkperf-tools sdkperf-export build-smf
	./scripts/run-live-smf-audit.sh
