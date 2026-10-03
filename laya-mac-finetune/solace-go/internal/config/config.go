package config

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	BrokerURL         string
	InputFilter       string
	OutputTopic       string
	Username          string
	Password          string
	ClientID          string
	QoS               byte
	CleanSession      bool
	ModelPath         string
	Device            string
	Python            string
	WorkerScript      string
	StoreDirectory    string
	QueueCapacity     int
	MaxEventBytes     int
	EnqueueTimeout    time.Duration
	InferenceTimeout  time.Duration
	PublishTimeout    time.Duration
	StartupTimeout    time.Duration
	ShutdownTimeout   time.Duration
	OfflineStdin      bool
	OfflineInputTopic string
}

func envString(getenv func(string) string, key, fallback string) string {
	if value := getenv(key); value != "" {
		return value
	}
	return fallback
}

func envInt(getenv func(string) string, key string, fallback int) (int, error) {
	value := getenv(key)
	if value == "" {
		return fallback, nil
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return 0, fmt.Errorf("%s must be an integer: %w", key, err)
	}
	return parsed, nil
}

func envBool(getenv func(string) string, key string, fallback bool) (bool, error) {
	value := getenv(key)
	if value == "" {
		return fallback, nil
	}
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return false, fmt.Errorf("%s must be true or false: %w", key, err)
	}
	return parsed, nil
}

func envDuration(getenv func(string) string, key string, fallback time.Duration) (time.Duration, error) {
	value := getenv(key)
	if value == "" {
		return fallback, nil
	}
	parsed, err := time.ParseDuration(value)
	if err != nil {
		return 0, fmt.Errorf("%s must be a Go duration: %w", key, err)
	}
	return parsed, nil
}

func bridgeRoot(getenv func(string) string) (string, error) {
	if root := getenv("LAYA_SOLACE_GO_ROOT"); root != "" {
		return filepath.Abs(root)
	}
	cwd, err := os.Getwd()
	if err != nil {
		return "", err
	}
	if _, err := os.Stat(filepath.Join(cwd, "python_worker.py")); err == nil {
		return cwd, nil
	}
	candidate := filepath.Join(cwd, "solace-go")
	if _, err := os.Stat(filepath.Join(candidate, "python_worker.py")); err == nil {
		return candidate, nil
	}
	return cwd, nil
}

func Parse(args []string, getenv func(string) string, stderr io.Writer) (Config, error) {
	root, err := bridgeRoot(getenv)
	if err != nil {
		return Config{}, fmt.Errorf("resolve bridge root: %w", err)
	}
	queueCapacity, err := envInt(getenv, "BRIDGE_QUEUE_CAPACITY", 32)
	if err != nil {
		return Config{}, err
	}
	maxEventBytes, err := envInt(getenv, "BRIDGE_MAX_EVENT_BYTES", 256*1024)
	if err != nil {
		return Config{}, err
	}
	qosValue, err := envInt(getenv, "SOLACE_QOS", 1)
	if err != nil {
		return Config{}, err
	}
	cleanSession, err := envBool(getenv, "SOLACE_CLEAN_SESSION", false)
	if err != nil {
		return Config{}, err
	}
	enqueueTimeout, err := envDuration(getenv, "BRIDGE_ENQUEUE_TIMEOUT", 250*time.Millisecond)
	if err != nil {
		return Config{}, err
	}
	inferenceTimeout, err := envDuration(getenv, "BRIDGE_INFERENCE_TIMEOUT", 5*time.Second)
	if err != nil {
		return Config{}, err
	}
	publishTimeout, err := envDuration(getenv, "BRIDGE_PUBLISH_TIMEOUT", 5*time.Second)
	if err != nil {
		return Config{}, err
	}
	startupTimeout, err := envDuration(getenv, "BRIDGE_STARTUP_TIMEOUT", 90*time.Second)
	if err != nil {
		return Config{}, err
	}
	shutdownTimeout, err := envDuration(getenv, "BRIDGE_SHUTDOWN_TIMEOUT", 15*time.Second)
	if err != nil {
		return Config{}, err
	}

	defaults := Config{
		BrokerURL:         envString(getenv, "SOLACE_BROKER_URL", "tcp://localhost:1883"),
		InputFilter:       envString(getenv, "SOLACE_INPUT_FILTER", "acme/prod/+/events/#"),
		OutputTopic:       envString(getenv, "SOLACE_OUTPUT_TOPIC", "acme/prod/ai/laya-routing/{route}"),
		Username:          getenv("SOLACE_USERNAME"),
		Password:          getenv("SOLACE_PASSWORD"),
		ClientID:          envString(getenv, "SOLACE_CLIENT_ID", "laya-event-router"),
		QoS:               byte(qosValue),
		CleanSession:      cleanSession,
		ModelPath:         envString(getenv, "LAYA_MODEL", filepath.Join(root, "..", "outputs", "best-model")),
		Device:            envString(getenv, "LAYA_DEVICE", "auto"),
		Python:            envString(getenv, "LAYA_PYTHON", filepath.Join(root, "..", ".venv", "bin", "python")),
		WorkerScript:      envString(getenv, "LAYA_WORKER_SCRIPT", filepath.Join(root, "python_worker.py")),
		StoreDirectory:    envString(getenv, "BRIDGE_STORE_DIR", filepath.Join(root, ".state", "mqtt")),
		QueueCapacity:     queueCapacity,
		MaxEventBytes:     maxEventBytes,
		EnqueueTimeout:    enqueueTimeout,
		InferenceTimeout:  inferenceTimeout,
		PublishTimeout:    publishTimeout,
		StartupTimeout:    startupTimeout,
		ShutdownTimeout:   shutdownTimeout,
		OfflineInputTopic: envString(getenv, "BRIDGE_OFFLINE_INPUT_TOPIC", "offline/events"),
	}

	cfg := defaults
	qos := qosValue
	var brokerFlag, usernameFlag, passwordFlag string
	set := flag.NewFlagSet("laya-solace-bridge", flag.ContinueOnError)
	set.SetOutput(stderr)
	set.StringVar(&brokerFlag, "broker-url", "", "MQTT broker URL override (or SOLACE_BROKER_URL)")
	set.StringVar(&cfg.InputFilter, "input-filter", cfg.InputFilter, "MQTT source topic filter")
	set.StringVar(&cfg.OutputTopic, "output-topic", cfg.OutputTopic, "output topic or template with {route}, {correlation_id}, {input_topic}")
	set.StringVar(&usernameFlag, "username", "", "MQTT username override (prefer SOLACE_USERNAME)")
	set.StringVar(&passwordFlag, "password", "", "MQTT password override (prefer SOLACE_PASSWORD; never logged)")
	set.StringVar(&cfg.ClientID, "client-id", cfg.ClientID, "stable MQTT client ID")
	set.IntVar(&qos, "qos", qos, "MQTT input/output QoS: 0 or 1")
	set.BoolVar(&cfg.CleanSession, "clean-session", cfg.CleanSession, "discard broker session state on disconnect")
	set.StringVar(&cfg.ModelPath, "model", cfg.ModelPath, "local Laya checkpoint directory")
	set.StringVar(&cfg.Device, "device", cfg.Device, "model device: auto, mps, or cpu")
	set.StringVar(&cfg.Python, "python", cfg.Python, "Python interpreter for the persistent worker")
	set.StringVar(&cfg.WorkerScript, "worker-script", cfg.WorkerScript, "JSONL Python worker path")
	set.StringVar(&cfg.StoreDirectory, "store-dir", cfg.StoreDirectory, "Paho local QoS state directory")
	set.IntVar(&cfg.QueueCapacity, "queue-capacity", cfg.QueueCapacity, "bounded in-process event queue")
	set.IntVar(&cfg.MaxEventBytes, "max-event-bytes", cfg.MaxEventBytes, "maximum inbound payload size")
	set.DurationVar(&cfg.EnqueueTimeout, "enqueue-timeout", cfg.EnqueueTimeout, "maximum callback enqueue wait")
	set.DurationVar(&cfg.InferenceTimeout, "inference-timeout", cfg.InferenceTimeout, "per-event model deadline")
	set.DurationVar(&cfg.PublishTimeout, "publish-timeout", cfg.PublishTimeout, "per-result MQTT publish deadline")
	set.DurationVar(&cfg.StartupTimeout, "startup-timeout", cfg.StartupTimeout, "model-worker startup deadline")
	set.DurationVar(&cfg.ShutdownTimeout, "shutdown-timeout", cfg.ShutdownTimeout, "graceful drain deadline")
	set.BoolVar(&cfg.OfflineStdin, "offline-stdin", false, "read JSONL from stdin and write result envelopes to stdout")
	set.StringVar(&cfg.OfflineInputTopic, "offline-input-topic", cfg.OfflineInputTopic, "source topic recorded in offline mode")
	if err := set.Parse(args); err != nil {
		return Config{}, err
	}
	if brokerFlag != "" {
		cfg.BrokerURL = brokerFlag
	}
	if usernameFlag != "" {
		cfg.Username = usernameFlag
	}
	if passwordFlag != "" {
		cfg.Password = passwordFlag
	}
	if qos < 0 || qos > 1 {
		return Config{}, errors.New("qos must be 0 or 1; Solace downgrades MQTT QoS 2 to QoS 1")
	}
	cfg.QoS = byte(qos)

	for field, value := range map[string]string{
		"broker URL":    cfg.BrokerURL,
		"input filter":  cfg.InputFilter,
		"output topic":  cfg.OutputTopic,
		"client ID":     cfg.ClientID,
		"model":         cfg.ModelPath,
		"python":        cfg.Python,
		"worker script": cfg.WorkerScript,
	} {
		if strings.TrimSpace(value) == "" {
			return Config{}, fmt.Errorf("%s must not be empty", field)
		}
	}
	if len(cfg.ClientID) > 128 || strings.ContainsRune(cfg.ClientID, 0) {
		return Config{}, errors.New("client-id must be at most 128 bytes and contain no NUL")
	}
	if cfg.QoS > 1 {
		return Config{}, errors.New("qos must be 0 or 1; Solace downgrades MQTT QoS 2 to QoS 1")
	}
	if cfg.QueueCapacity < 1 || cfg.MaxEventBytes < 1024 {
		return Config{}, errors.New("queue-capacity must be positive and max-event-bytes at least 1024")
	}
	if cfg.EnqueueTimeout <= 0 || cfg.InferenceTimeout <= 0 || cfg.PublishTimeout <= 0 || cfg.StartupTimeout <= 0 || cfg.ShutdownTimeout <= 0 {
		return Config{}, errors.New("all timeout values must be positive")
	}
	if cfg.Device != "auto" && cfg.Device != "mps" && cfg.Device != "cpu" {
		return Config{}, fmt.Errorf("device must be auto, mps, or cpu; got %q", cfg.Device)
	}
	if !cfg.OfflineStdin {
		parsed, err := url.Parse(cfg.BrokerURL)
		if err != nil || parsed.Host == "" {
			return Config{}, errors.New("invalid broker URL")
		}
		if parsed.User != nil {
			return Config{}, errors.New("broker URL must not contain credentials; use SOLACE_USERNAME and SOLACE_PASSWORD")
		}
		switch parsed.Scheme {
		case "tcp", "ssl", "ws", "wss":
		default:
			return Config{}, fmt.Errorf("unsupported broker URL scheme %q", parsed.Scheme)
		}
	}
	for _, path := range []*string{&cfg.ModelPath, &cfg.Python, &cfg.WorkerScript, &cfg.StoreDirectory} {
		if !filepath.IsAbs(*path) {
			*path = filepath.Join(root, *path)
		}
		*path = filepath.Clean(*path)
	}
	return cfg, nil
}
