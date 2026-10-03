package main

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"laya.local/solacebridge/internal/bridge"
	"laya.local/solacebridge/internal/protocol"
)

type orderedClient struct {
	mu     sync.Mutex
	events []string
}

func (c *orderedClient) record(event string) {
	c.mu.Lock()
	c.events = append(c.events, event)
	c.mu.Unlock()
}

func (c *orderedClient) Publish(context.Context, string, byte, []byte) error {
	time.Sleep(15 * time.Millisecond)
	c.record("publish")
	return nil
}

func (c *orderedClient) StopIntake() error {
	c.record("stop-intake")
	return nil
}

func (c *orderedClient) Disconnect(time.Duration) { c.record("disconnect") }

type fixedInferencer struct{}

func (fixedInferencer) Infer(context.Context, string, json.RawMessage) (*protocol.Decision, error) {
	time.Sleep(15 * time.Millisecond)
	return &protocol.Decision{
		SelectedRoute: "logistics",
		Probabilities: map[string]float64{"logistics": 1},
		Confidence:    1,
	}, nil
}

func TestShutdownDrainsPublishBeforeDisconnect(t *testing.T) {
	client := &orderedClient{}
	pipeline, err := bridge.New(bridge.Config{
		InputFilter:      "business/events/#",
		OutputTopic:      "ai/routes/{route}",
		QoS:              1,
		QueueCapacity:    2,
		MaxEventBytes:    1024,
		EnqueueTimeout:   time.Second,
		InferenceTimeout: time.Second,
		PublishTimeout:   time.Second,
	}, fixedInferencer{}, client, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	processorContext, cancel := context.WithCancel(context.Background())
	go pipeline.Run(processorContext)
	if err := pipeline.Submit(bridge.Incoming{
		Topic:   "business/events/eu",
		Payload: []byte(`{"event_id":"shutdown-order"}`),
		Ack:     func() error { client.record("ack"); return nil },
	}); err != nil {
		t.Fatal(err)
	}
	if err := drainThenDisconnect(pipeline, client, cancel, time.Second); err != nil {
		t.Fatal(err)
	}
	client.mu.Lock()
	defer client.mu.Unlock()
	want := []string{"stop-intake", "publish", "ack", "disconnect"}
	if len(client.events) != len(want) {
		t.Fatalf("events=%v", client.events)
	}
	for index := range want {
		if client.events[index] != want[index] {
			t.Fatalf("events=%v, want=%v", client.events, want)
		}
	}
}
