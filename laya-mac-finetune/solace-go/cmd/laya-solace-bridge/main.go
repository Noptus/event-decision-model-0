package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"laya.local/solacebridge/internal/bridge"
	"laya.local/solacebridge/internal/config"
	"laya.local/solacebridge/internal/mqttclient"
	"laya.local/solacebridge/internal/smfclient"
	"laya.local/solacebridge/internal/singleton"
	"laya.local/solacebridge/internal/worker"
)

type deferredPublisher struct {
	mu        sync.RWMutex
	publisher bridge.Publisher
}

func (p *deferredPublisher) Set(publisher bridge.Publisher) {
	p.mu.Lock()
	p.publisher = publisher
	p.mu.Unlock()
}

func (p *deferredPublisher) Publish(ctx context.Context, topic string, qos byte, payload []byte) error {
	p.mu.RLock()
	publisher := p.publisher
	p.mu.RUnlock()
	if publisher == nil {
		return errors.New("broker publisher is not ready")
	}
	return publisher.Publish(ctx, topic, qos, payload)
}

type auditPublisher struct {
	publisher bridge.Publisher
	file      *os.File
	mu        sync.Mutex
}

func newAuditPublisher(publisher bridge.Publisher, path string) (*auditPublisher, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, fmt.Errorf("create audit directory: %w", err)
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		return nil, fmt.Errorf("open result audit: %w", err)
	}
	return &auditPublisher{publisher: publisher, file: file}, nil
}

func (p *auditPublisher) Publish(ctx context.Context, topic string, qos byte, payload []byte) error {
	if err := p.publisher.Publish(ctx, topic, qos, payload); err != nil {
		return err
	}
	line, err := json.Marshal(struct {
		Topic   string          `json:"topic"`
		Payload json.RawMessage `json:"payload"`
	}{Topic: topic, Payload: json.RawMessage(payload)})
	if err != nil {
		return fmt.Errorf("encode result audit record: %w", err)
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if _, err := p.file.Write(append(line, '\n')); err != nil {
		return fmt.Errorf("write result audit record: %w", err)
	}
	return nil
}

func (p *auditPublisher) Close() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.file.Close()
}

type stdoutPublisher struct {
	mu     sync.Mutex
	writer io.Writer
}

func (p *stdoutPublisher) Publish(_ context.Context, topic string, qos byte, payload []byte) error {
	var envelope json.RawMessage
	if !json.Valid(payload) {
		return errors.New("bridge produced invalid JSON")
	}
	envelope = append(envelope, payload...)
	line, err := json.Marshal(struct {
		Topic   string          `json:"topic"`
		QoS     byte            `json:"qos"`
		Payload json.RawMessage `json:"payload"`
	}{Topic: topic, QoS: qos, Payload: envelope})
	if err != nil {
		return err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	_, err = fmt.Fprintf(p.writer, "%s\n", line)
	return err
}

func newWorker(cfg config.Config) (*worker.Process, error) {
	return worker.New(worker.Config{
		Python:           cfg.Python,
		Script:           cfg.WorkerScript,
		Model:            cfg.ModelPath,
		Device:           cfg.Device,
		WorkingDirectory: filepath.Dir(cfg.WorkerScript),
		StartupTimeout:   cfg.StartupTimeout,
		StopTimeout:      cfg.ShutdownTimeout,
		MaxEventBytes:    cfg.MaxEventBytes,
		MaxResponseBytes: 1024 * 1024,
	})
}

func bridgeConfig(cfg config.Config) bridge.Config {
	delivery := fmt.Sprintf("mqtt-qos-%d", cfg.QoS)
	if cfg.Transport == "smf" {
		delivery = "smf-persistent-guaranteed"
	}
	return bridge.Config{
		Transport:         cfg.Transport,
		TopicSyntax:       cfg.Transport,
		DeliverySemantics: delivery,
		InputFilter:       cfg.InputFilter,
		OutputTopic:       cfg.OutputTopic,
		QoS:               cfg.QoS,
		QueueCapacity:     cfg.QueueCapacity,
		MaxEventBytes:     cfg.MaxEventBytes,
		EnqueueTimeout:    cfg.EnqueueTimeout,
		InferenceTimeout:  cfg.InferenceTimeout,
		PublishTimeout:    cfg.PublishTimeout,
		MaxMessages:       uint64(cfg.MaxMessages),
	}
}

func logErrors(logger *slog.Logger, errorsChannel <-chan error, done chan<- int) {
	count := 0
	for err := range errorsChannel {
		count++
		logger.Error("event processing failed; input remains unacknowledged", "error", err)
	}
	done <- count
}

func runOffline(cfg config.Config, modelWorker *worker.Process, logger *slog.Logger) error {
	publisher := &stdoutPublisher{writer: os.Stdout}
	pipelineConfig := bridgeConfig(cfg)
	pipelineConfig.Transport = "offline"
	pipelineConfig.TopicSyntax = "mqtt"
	pipelineConfig.DeliverySemantics = "offline-simulation"
	pipelineConfig.InputFilter = cfg.OfflineInputTopic
	pipeline, err := bridge.New(pipelineConfig, modelWorker, publisher, logger)
	if err != nil {
		return err
	}
	processorContext := context.Background()
	go pipeline.Run(processorContext)
	errorCount := make(chan int, 1)
	go logErrors(logger, pipeline.Errors(), errorCount)

	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 64*1024), cfg.MaxEventBytes+64*1024)
	line := 0
	for scanner.Scan() {
		line++
		if len(scanner.Bytes()) == 0 {
			continue
		}
		payload := append([]byte(nil), scanner.Bytes()...)
		if err := pipeline.Submit(bridge.Incoming{
			Topic:   cfg.OfflineInputTopic,
			Payload: payload,
			Ack:     func() error { return nil },
		}); err != nil {
			pipeline.Close()
			<-pipeline.Done()
			return fmt.Errorf("submit stdin line %d: %w", line, err)
		}
	}
	if err := scanner.Err(); err != nil {
		pipeline.Close()
		<-pipeline.Done()
		return fmt.Errorf("read stdin JSONL: %w", err)
	}
	pipeline.Close()
	<-pipeline.Done()
	if failures := <-errorCount; failures > 0 {
		return fmt.Errorf("%d offline event(s) failed", failures)
	}
	return nil
}

type transportLifecycle interface {
	StopIntake() error
	Disconnect(time.Duration)
}

type brokerClient interface {
	bridge.Publisher
	transportLifecycle
	Connect(context.Context) error
}

func drainThenDisconnect(
	pipeline *bridge.Bridge,
	client transportLifecycle,
	cancelProcessing context.CancelFunc,
	timeout time.Duration,
) error {
	// Stop accepting locally before pausing the transport. Accepted events drain while the
	// publisher remains connected; neither transport removes its durable subscription.
	pipeline.Close()
	stopIntakeErr := client.StopIntake()
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case <-pipeline.Done():
		cancelProcessing()
		client.Disconnect(500 * time.Millisecond)
		return stopIntakeErr
	case <-timer.C:
		// Cancel active inference/publication, let the bounded pipeline abandon remaining items,
		// and only then disconnect so no accepted item publishes against a closed client.
		cancelProcessing()
		abortTimer := time.NewTimer(timeout)
		defer abortTimer.Stop()
		select {
		case <-pipeline.Done():
			client.Disconnect(500 * time.Millisecond)
			return errors.Join(stopIntakeErr, errors.New("graceful drain timed out; remaining inputs were left unacknowledged"))
		case <-abortTimer.C:
			client.Disconnect(0)
			return errors.Join(stopIntakeErr, errors.New("pipeline did not stop after cancellation"))
		}
	}
}

func waitForStop(ctx context.Context, pipeline *bridge.Bridge, idleTimeout time.Duration) string {
	if idleTimeout <= 0 {
		select {
		case <-ctx.Done():
			return "signal"
		case <-pipeline.LimitReached():
			return "message-limit"
		}
	}
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	stats := pipeline.Stats()
	lastCount := stats.Submitted + stats.Acknowledged
	lastActivity := time.Now()
	for {
		select {
		case <-ctx.Done():
			return "signal"
		case <-pipeline.LimitReached():
			return "message-limit"
		case <-ticker.C:
			stats = pipeline.Stats()
			count := stats.Submitted + stats.Acknowledged
			if count != lastCount {
				lastCount = count
				lastActivity = time.Now()
			} else if time.Since(lastActivity) >= idleTimeout {
				return "idle-timeout"
			}
		}
	}
}

func newBrokerClient(
	cfg config.Config, submit func(bridge.Incoming) error, logger *slog.Logger,
) (brokerClient, error) {
	if cfg.Transport == "smf" {
		return smfclient.New(cfg, submit, logger)
	}
	return mqttclient.New(cfg, submit, logger)
}

func runBroker(ctx context.Context, cfg config.Config, modelWorker *worker.Process, logger *slog.Logger) error {
	publisher := &deferredPublisher{}
	pipeline, err := bridge.New(bridgeConfig(cfg), modelWorker, publisher, logger)
	if err != nil {
		return err
	}
	client, err := newBrokerClient(cfg, pipeline.Submit, logger)
	if err != nil {
		return err
	}
	var resultPublisher bridge.Publisher = client
	if cfg.AuditFile != "" {
		audit, err := newAuditPublisher(client, cfg.AuditFile)
		if err != nil {
			return err
		}
		defer func() {
			if err := audit.Close(); err != nil {
				logger.Warn("close result audit", "error", err)
			}
		}()
		resultPublisher = audit
	}
	publisher.Set(resultPublisher)
	processorContext, cancelProcessing := context.WithCancel(context.Background())
	defer cancelProcessing()
	go pipeline.Run(processorContext)
	errorDone := make(chan int, 1)
	go logErrors(logger, pipeline.Errors(), errorDone)

	connectContext, cancel := context.WithTimeout(ctx, cfg.StartupTimeout)
	err = client.Connect(connectContext)
	cancel()
	if err != nil {
		shutdownErr := drainThenDisconnect(pipeline, client, cancelProcessing, cfg.ShutdownTimeout)
		<-errorDone
		if shutdownErr != nil {
			logger.Warn("cleanup after broker connect failure", "transport", cfg.Transport, "error", shutdownErr)
		}
		return err
	}
	logger.Info(
		"bridge ready",
		"transport", cfg.Transport,
		"client_id", cfg.ClientID,
		"input_filter", cfg.InputFilter,
		"output_topic", cfg.OutputTopic,
	)
	stopReason := waitForStop(ctx, pipeline, cfg.IdleTimeout)
	logger.Info(
		"stopping local intake and draining accepted events before disconnect",
		"reason", stopReason,
		"max_messages", cfg.MaxMessages,
	)
	drainErr := drainThenDisconnect(pipeline, client, cancelProcessing, cfg.ShutdownTimeout)
	failures := <-errorDone
	stats := pipeline.Stats()
	logger.Info(
		"bridge stopped",
		"transport", cfg.Transport,
		"submitted", stats.Submitted,
		"published", stats.Published,
		"successful_decisions", stats.SuccessfulDecisions,
		"error_envelopes", stats.ErrorEnvelopes,
		"acknowledged", stats.Acknowledged,
		"correlations_tracked", stats.CorrelationsTracked,
		"unique_correlations", stats.UniqueCorrelations,
		"failed", stats.Failed,
		"looped", stats.Looped,
	)
	if failures > 0 {
		logger.Warn("bridge stopped with event-processing errors", "count", failures)
	}
	return drainErr
}

func run(args []string) error {
	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
	cfg, err := config.Parse(args, os.Getenv, os.Stderr)
	if err != nil {
		return err
	}
	if cfg.Transport == "smf" && !smfclient.Available() {
		return errors.New("SMF transport requires the optional native build; run 'make build-smf'")
	}
	if !cfg.OfflineStdin {
		lockDirectory := filepath.Join(filepath.Dir(cfg.StoreDirectory), "locks")
		instanceLock, err := singleton.Acquire(
			lockDirectory,
			cfg.Transport,
			cfg.BrokerURL,
			cfg.SMFVPN,
			cfg.ClientID,
			cfg.StoreDirectory,
		)
		if err != nil {
			return fmt.Errorf("acquire bridge singleton: %w", err)
		}
		defer func() {
			if err := instanceLock.Close(); err != nil {
				logger.Warn("release bridge singleton", "error", err)
			}
		}()
	}
	modelWorker, err := newWorker(cfg)
	if err != nil {
		return err
	}
	startupContext, startupCancel := context.WithTimeout(context.Background(), cfg.StartupTimeout)
	err = modelWorker.Start(startupContext)
	startupCancel()
	if err != nil {
		return err
	}
	defer func() {
		closeContext, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
		defer cancel()
		if err := modelWorker.Close(closeContext); err != nil && !errors.Is(err, context.Canceled) {
			logger.Warn("model worker shutdown", "error", err)
		}
	}()

	if cfg.OfflineStdin {
		logger.Info("offline stdin mode ready", "model", cfg.ModelPath, "device", cfg.Device)
		return runOffline(cfg, modelWorker, logger)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	return runBroker(ctx, cfg, modelWorker, logger)
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		slog.Error("bridge stopped", "error", err)
		os.Exit(1)
	}
}
