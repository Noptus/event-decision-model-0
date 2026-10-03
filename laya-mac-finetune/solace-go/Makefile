PROJECT_ROOT := $(abspath ..)
GO := $(PROJECT_ROOT)/.cache/tools/go1.27.1/bin/go
GO_ENV := GOPATH="$(PROJECT_ROOT)/.cache/go" GOMODCACHE="$(PROJECT_ROOT)/.cache/go/pkg/mod" GOCACHE="$(PROJECT_ROOT)/.cache/go/build" GOTMPDIR="$(PROJECT_ROOT)/.tmp"

.PHONY: toolchain tidy fmt test race build real-worker-test worker-smoke offline-demo

toolchain:
	./scripts/install-go.sh

tidy: toolchain
	$(GO_ENV) "$(GO)" mod tidy

fmt: toolchain
	$(GO_ENV) "$(GO)" fmt ./...

test: toolchain
	$(GO_ENV) "$(GO)" test ./...

race: toolchain
	$(GO_ENV) CGO_ENABLED=1 "$(GO)" test -race ./...

build: toolchain
	mkdir -p bin
	$(GO_ENV) "$(GO)" build -trimpath -o bin/laya-solace-bridge ./cmd/laya-solace-bridge

real-worker-test: toolchain
	$(GO_ENV) LAYA_REAL_WORKER_TEST=1 LAYA_PROJECT_ROOT="$(PROJECT_ROOT)" "$(GO)" test ./internal/worker -run TestRealSavedModelWorker -v

worker-smoke: build
	$(GO_ENV) LAYA_SOLACE_GO_ROOT="$(CURDIR)" ./bin/laya-solace-bridge --offline-stdin --model ../outputs/best-model --device mps < testdata/events.jsonl

offline-demo: worker-smoke
