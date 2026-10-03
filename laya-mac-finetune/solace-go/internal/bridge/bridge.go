package bridge

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"laya.local/solacebridge/internal/protocol"
	"laya.local/solacebridge/internal/worker"
)

const producer = "laya-solace-bridge"

var (
	ErrClosed          = errors.New("bridge is closed")
	ErrQueueFull       = errors.New("bridge queue is full")
	ErrPayloadTooLarge = errors.New("event payload exceeds configured maximum")
	ErrUnexpectedTopic = errors.New("message topic does not match configured input filter")
)

type Publisher interface {
	Publish(context.Context, string, byte, []byte) error
}

type Incoming struct {
	Topic     string
	Payload   []byte
	QoS       byte
	Retained  bool
	Duplicate bool
	Ack       func()
}

type Config struct {
	InputFilter      string
	OutputTopic      string
	QoS              byte
	QueueCapacity    int
	MaxEventBytes    int
	EnqueueTimeout   time.Duration
	InferenceTimeout time.Duration
	PublishTimeout   time.Duration
}

type queued struct {
	message     Incoming
	payloadHash string
	correlation string
	rejected    error
}

type Bridge struct {
	config     Config
	inferencer worker.Inferencer
	publisher  Publisher
	logger     *slog.Logger
	queue      chan queued
	errors     chan error
	closed     atomic.Bool
	submitMu   sync.RWMutex
	closeOnce  sync.Once
	done       chan struct{}
}

func New(config Config, inferencer worker.Inferencer, publisher Publisher, logger *slog.Logger) (*Bridge, error) {
	if inferencer == nil || publisher == nil {
		return nil, errors.New("inferencer and publisher are required")
	}
	if logger == nil {
		logger = slog.Default()
	}
	if config.QueueCapacity < 1 || config.MaxEventBytes < 1 {
		return nil, errors.New("queue capacity and maximum event size must be positive")
	}
	if config.EnqueueTimeout <= 0 || config.InferenceTimeout <= 0 || config.PublishTimeout <= 0 {
		return nil, errors.New("bridge timeouts must be positive")
	}
	if err := ValidateTopicFilter(config.InputFilter); err != nil {
		return nil, fmt.Errorf("input filter: %w", err)
	}
	renderedSample, err := RenderTopic(config.OutputTopic, "sample-route", "sample-id", "sample/input")
	if err != nil {
		return nil, fmt.Errorf("output topic: %w", err)
	}
	if !strings.Contains(config.OutputTopic, "{") && TopicMatches(config.InputFilter, renderedSample) {
		return nil, fmt.Errorf("output topic %q matches input filter %q", renderedSample, config.InputFilter)
	}
	return &Bridge{
		config:     config,
		inferencer: inferencer,
		publisher:  publisher,
		logger:     logger,
		queue:      make(chan queued, config.QueueCapacity),
		errors:     make(chan error, config.QueueCapacity),
		done:       make(chan struct{}),
	}, nil
}

func (b *Bridge) Errors() <-chan error  { return b.errors }
func (b *Bridge) Done() <-chan struct{} { return b.done }

func (b *Bridge) Submit(message Incoming) error {
	if b.closed.Load() {
		return ErrClosed
	}
	b.submitMu.RLock()
	defer b.submitMu.RUnlock()
	if b.closed.Load() {
		return ErrClosed
	}
	if !TopicMatches(b.config.InputFilter, message.Topic) {
		return fmt.Errorf("%w: %q", ErrUnexpectedTopic, message.Topic)
	}
	if message.Ack == nil {
		message.Ack = func() {}
	}
	payloadHash := sha256.Sum256(message.Payload)
	item := queued{
		message:     message,
		payloadHash: hex.EncodeToString(payloadHash[:]),
		correlation: CorrelationID(message.Payload, payloadHash),
	}
	if len(message.Payload) > b.config.MaxEventBytes {
		item.rejected = fmt.Errorf("%w: got %d bytes, maximum %d", ErrPayloadTooLarge, len(message.Payload), b.config.MaxEventBytes)
		item.message.Payload = nil
	} else {
		item.message.Payload = append([]byte(nil), message.Payload...)
	}

	timer := time.NewTimer(b.config.EnqueueTimeout)
	defer timer.Stop()
	select {
	case b.queue <- item:
		return nil
	case <-timer.C:
		return ErrQueueFull
	}
}

func (b *Bridge) Close() {
	b.closeOnce.Do(func() {
		b.submitMu.Lock()
		defer b.submitMu.Unlock()
		b.closed.Store(true)
		close(b.queue)
	})
}

func (b *Bridge) Run(ctx context.Context) {
	defer close(b.done)
	defer close(b.errors)
	for item := range b.queue {
		if err := ctx.Err(); err != nil {
			b.reportError(fmt.Errorf("event %s left unacknowledged during forced shutdown: %w", item.correlation, err))
			continue
		}
		if err := b.process(ctx, item); err != nil {
			b.reportError(err)
		}
	}
}

func (b *Bridge) reportError(err error) {
	select {
	case b.errors <- err:
	default:
		b.logger.Error("dropping bridge error because error channel is full", "error", err)
	}
}

func (b *Bridge) process(ctx context.Context, item queued) error {
	started := time.Now()
	if item.rejected == nil && IsBridgeOutput(item.message.Payload) {
		item.message.Ack()
		b.logger.Warn("acknowledged self-produced message to prevent a routing loop", "topic", item.message.Topic)
		return nil
	}

	var decision *protocol.Decision
	var bridgeError *protocol.BridgeError
	if item.rejected != nil {
		bridgeError = &protocol.BridgeError{
			Code: "payload_too_large", Message: item.rejected.Error(), Retryable: false,
		}
	} else if !validJSONObject(item.message.Payload) {
		bridgeError = &protocol.BridgeError{
			Code: "invalid_event_json", Message: "payload must be one JSON object", Retryable: false,
		}
	} else {
		inferenceContext, cancel := context.WithTimeout(ctx, b.config.InferenceTimeout)
		var err error
		decision, err = b.inferencer.Infer(inferenceContext, item.correlation, item.message.Payload)
		cancel()
		if err != nil {
			bridgeError = &protocol.BridgeError{
				Code: "inference_failed", Message: err.Error(), Retryable: true,
			}
		}
	}

	route := "review"
	if decision != nil && decision.SelectedRoute != "" && !decision.ReviewRequired {
		route = decision.SelectedRoute
	}
	outputTopic, err := RenderTopic(b.config.OutputTopic, route, item.correlation, item.message.Topic)
	if err != nil {
		return fmt.Errorf("render output topic for %s: %w", item.correlation, err)
	}
	if TopicMatches(b.config.InputFilter, outputTopic) {
		return fmt.Errorf(
			"loop prevention refused output topic %q because it matches input filter %q",
			outputTopic,
			b.config.InputFilter,
		)
	}

	envelope := protocol.OutputEnvelope{
		SchemaVersion: "1.0",
		Producer:      producer,
		CorrelationID: item.correlation,
		ProcessedAt:   time.Now().UTC(),
		Source: protocol.Source{
			Topic:         item.message.Topic,
			QoS:           item.message.QoS,
			Retained:      item.message.Retained,
			Duplicate:     item.message.Duplicate,
			PayloadSHA256: item.payloadHash,
		},
		Decision:        decision,
		Error:           bridgeError,
		BridgeLatencyMS: float64(time.Since(started).Microseconds()) / 1000,
	}
	payload, err := json.Marshal(envelope)
	if err != nil {
		return fmt.Errorf("encode result envelope: %w", err)
	}
	publishContext, cancel := context.WithTimeout(ctx, b.config.PublishTimeout)
	err = b.publisher.Publish(publishContext, outputTopic, b.config.QoS, payload)
	cancel()
	if err != nil {
		return fmt.Errorf("publish result for %s: %w", item.correlation, err)
	}
	// For QoS 1, acknowledge input only after the corresponding output publication completes.
	// A crash between those operations can still duplicate output; correlation_id enables dedupe.
	item.message.Ack()
	return nil
}

func validJSONObject(payload []byte) bool {
	var value map[string]any
	return json.Unmarshal(payload, &value) == nil && value != nil
}

func IsBridgeOutput(payload []byte) bool {
	var marker struct {
		Producer string `json:"producer"`
	}
	return json.Unmarshal(payload, &marker) == nil && marker.Producer == producer
}

func CorrelationID(payload []byte, digest [sha256.Size]byte) string {
	var event map[string]any
	if json.Unmarshal(payload, &event) == nil {
		for _, key := range []string{"correlation_id", "event_id", "id"} {
			if value, ok := event[key].(string); ok && strings.TrimSpace(value) != "" {
				return truncate(value, 128)
			}
		}
		if nested, ok := event["payload"].(map[string]any); ok {
			for _, key := range []string{"correlation_id", "event_id", "id"} {
				if value, ok := nested[key].(string); ok && strings.TrimSpace(value) != "" {
					return truncate(value, 128)
				}
			}
		}
	}
	return hex.EncodeToString(digest[:12])
}

func truncate(value string, maximum int) string {
	if len(value) <= maximum {
		return value
	}
	return value[:maximum]
}

func topicValue(value string, allowSlash bool) (string, error) {
	value = strings.TrimSpace(value)
	if value == "" || strings.ContainsRune(value, 0) || strings.ContainsAny(value, "+#") {
		return "", fmt.Errorf("unsafe empty or wildcard topic value %q", value)
	}
	if !allowSlash {
		value = strings.ReplaceAll(value, "/", "_")
	}
	return value, nil
}

func RenderTopic(template, route, correlationID, inputTopic string) (string, error) {
	route, err := topicValue(route, false)
	if err != nil {
		return "", err
	}
	correlationID, err = topicValue(correlationID, false)
	if err != nil {
		return "", err
	}
	inputTopic, err = topicValue(inputTopic, true)
	if err != nil {
		return "", err
	}
	result := strings.NewReplacer(
		"{route}", route,
		"{correlation_id}", correlationID,
		"{input_topic}", inputTopic,
	).Replace(template)
	if strings.ContainsAny(result, "+#\x00") || strings.ContainsAny(result, "{}") {
		return "", fmt.Errorf("rendered publish topic contains a wildcard or unknown placeholder: %q", result)
	}
	if strings.TrimSpace(result) == "" {
		return "", errors.New("rendered publish topic is empty")
	}
	return result, nil
}

func ValidateTopicFilter(filter string) error {
	if filter == "" || strings.ContainsRune(filter, 0) {
		return errors.New("topic filter is empty or contains NUL")
	}
	levels := strings.Split(filter, "/")
	for index, level := range levels {
		if strings.Contains(level, "#") && (level != "#" || index != len(levels)-1) {
			return errors.New("# must occupy the final topic-filter level")
		}
		if strings.Contains(level, "+") && level != "+" {
			return errors.New("+ must occupy an entire topic-filter level")
		}
	}
	return nil
}

func TopicMatches(filter, topic string) bool {
	filterLevels := strings.Split(filter, "/")
	topicLevels := strings.Split(topic, "/")
	for index, level := range filterLevels {
		if level == "#" {
			return index == len(filterLevels)-1
		}
		if index >= len(topicLevels) {
			return false
		}
		if level != "+" && level != topicLevels[index] {
			return false
		}
	}
	return len(filterLevels) == len(topicLevels)
}
