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
		return errors.New("MQTT publisher is not ready")
	}
	return publisher.Publish(ctx, topic, qos, payload)
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
	return bridge.Config{
		InputFilter:      cfg.InputFilter,
		OutputTopic:      cfg.OutputTopic,
		QoS:              cfg.QoS,
		QueueCapacity:    cfg.QueueCapacity,
		MaxEventBytes:    cfg.MaxEventBytes,
		EnqueueTimeout:   cfg.EnqueueTimeout,
		InferenceTimeout: cfg.InferenceTimeout,
		PublishTimeout:   cfg.PublishTimeout,
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
			QoS:     cfg.QoS,
			Ack:     func() {},
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

type disconnecter interface {
	Disconnect(time.Duration)
}

func drainThenDisconnect(
	pipeline *bridge.Bridge,
	client disconnecter,
	cancelProcessing context.CancelFunc,
	timeout time.Duration,
) error {
	// Stop accepting locally first. The MQTT subscription is intentionally retained: with
	// CleanSession=false the broker keeps it and any unacknowledged QoS 1 messages for restart.
	pipeline.Close()
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case <-pipeline.Done():
		cancelProcessing()
		client.Disconnect(500 * time.Millisecond)
		return nil
	case <-timer.C:
		// Cancel active inference/publication, let the bounded pipeline abandon remaining items,
		// and only then disconnect so no accepted item publishes against a closed client.
		cancelProcessing()
		abortTimer := time.NewTimer(timeout)
		defer abortTimer.Stop()
		select {
		case <-pipeline.Done():
			client.Disconnect(500 * time.Millisecond)
			return errors.New("graceful drain timed out; remaining inputs were left unacknowledged")
		case <-abortTimer.C:
			client.Disconnect(0)
			return errors.New("pipeline did not stop after cancellation")
		}
	}
}

func runMQTT(ctx context.Context, cfg config.Config, modelWorker *worker.Process, logger *slog.Logger) error {
	publisher := &deferredPublisher{}
	pipeline, err := bridge.New(bridgeConfig(cfg), modelWorker, publisher, logger)
	if err != nil {
		return err
	}
	client, err := mqttclient.New(cfg, pipeline.Submit, logger)
	if err != nil {
		return err
	}
	publisher.Set(client)
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
			logger.Warn("cleanup after MQTT connect failure", "error", shutdownErr)
		}
		return err
	}
	logger.Info(
		"bridge ready",
		"client_id", cfg.ClientID,
		"input_filter", cfg.InputFilter,
		"output_topic", cfg.OutputTopic,
		"qos", cfg.QoS,
		"clean_session", cfg.CleanSession,
	)
	<-ctx.Done()
	logger.Info("shutdown requested; stopping local intake and draining accepted events before disconnect")
	drainErr := drainThenDisconnect(pipeline, client, cancelProcessing, cfg.ShutdownTimeout)
	failures := <-errorDone
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
	return runMQTT(ctx, cfg, modelWorker, logger)
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		slog.Error("bridge stopped", "error", err)
		os.Exit(1)
	}
}
