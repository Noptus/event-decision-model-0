package config

import (
	"bytes"
	"errors"
	"flag"
	"strconv"
	"strings"
	"testing"
)

func environment(values map[string]string) func(string) string {
	return func(key string) string { return values[key] }
}

func TestEnvironmentAndFlagPrecedence(t *testing.T) {
	cfg, err := Parse([]string{"--client-id", "flag-client", "--qos", "0"}, environment(map[string]string{
		"SOLACE_CLIENT_ID": "env-client",
		"SOLACE_USERNAME":  "event-user",
		"SOLACE_PASSWORD":  "secret-value",
		"SOLACE_QOS":       "1",
	}), &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ClientID != "flag-client" || cfg.QoS != 0 || cfg.Username != "event-user" || cfg.Password != "secret-value" {
		t.Fatalf("unexpected config: %#v", cfg)
	}
}

func TestPasswordIsNotRenderedInHelp(t *testing.T) {
	var output bytes.Buffer
	_, err := Parse([]string{"-h"}, environment(map[string]string{"SOLACE_PASSWORD": "do-not-print-me"}), &output)
	if !errors.Is(err, flag.ErrHelp) {
		t.Fatalf("expected flag.ErrHelp, got %v", err)
	}
	if strings.Contains(output.String(), "do-not-print-me") {
		t.Fatal("password leaked into help output")
	}
}

func TestRejectsQoSTwo(t *testing.T) {
	_, err := Parse([]string{"--qos", "2"}, environment(nil), &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "downgrades") {
		t.Fatalf("expected QoS 2 validation error, got %v", err)
	}
}

func TestQoSDoesNotWrapBeforeValidation(t *testing.T) {
	for _, value := range []int{-1, 256, 257} {
		text := strconv.Itoa(value)
		t.Run(text, func(t *testing.T) {
			if _, err := Parse([]string{"--qos", text}, environment(nil), &bytes.Buffer{}); err == nil {
				t.Fatal("invalid flag QoS was accepted")
			}
			if _, err := Parse(nil, environment(map[string]string{"SOLACE_QOS": text}), &bytes.Buffer{}); err == nil {
				t.Fatal("invalid environment QoS was accepted")
			}
		})
	}
}
