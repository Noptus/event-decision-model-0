package worker

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strconv"
	"sync"
	"time"

	"laya.local/solacebridge/internal/protocol"
)

var ErrClosed = errors.New("model worker is closed")

type Inferencer interface {
	Infer(context.Context, string, json.RawMessage) (*protocol.Decision, error)
}

type Config struct {
	Python           string
	Script           string
	Model            string
	Device           string
	WorkingDirectory string
	StartupTimeout   time.Duration
	StopTimeout      time.Duration
	MaxEventBytes    int
	MaxResponseBytes int
}

type processState struct {
	command      *exec.Cmd
	stdin        io.WriteCloser
	responses    chan protocol.WorkerResponse
	scanErr      chan error
	scanStop     chan struct{}
	scanDone     chan struct{}
	scanStopOnce sync.Once
	done         chan struct{}
	waitErr      error
}

func (s *processState) stopScanner() {
	s.scanStopOnce.Do(func() { close(s.scanStop) })
}

type Process struct {
	config Config
	mu     sync.Mutex
	state  *processState
	closed bool
}

func New(config Config) (*Process, error) {
	if config.Python == "" || config.Script == "" || config.Model == "" {
		return nil, errors.New("python, worker script, and model path are required")
	}
	if config.Device == "" {
		config.Device = "auto"
	}
	if config.StartupTimeout <= 0 {
		config.StartupTimeout = 90 * time.Second
	}
	if config.StopTimeout <= 0 {
		config.StopTimeout = 5 * time.Second
	}
	if config.MaxEventBytes <= 0 {
		config.MaxEventBytes = 256 * 1024
	}
	if config.MaxResponseBytes <= 0 {
		config.MaxResponseBytes = 1024 * 1024
	}
	return &Process{config: config}, nil
}

func (p *Process) Start(ctx context.Context) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.startLocked(ctx)
}

func (p *Process) startLocked(ctx context.Context) error {
	if p.closed {
		return ErrClosed
	}
	if p.state != nil {
		select {
		case <-p.state.done:
			p.state.stopScanner()
			_ = p.state.stdin.Close()
			<-p.state.scanDone
			p.state = nil
		default:
			return nil
		}
	}

	command := exec.Command(
		p.config.Python,
		p.config.Script,
		"--model", p.config.Model,
		"--device", p.config.Device,
		"--max-event-bytes", strconv.Itoa(p.config.MaxEventBytes),
	)
	command.Dir = p.config.WorkingDirectory
	command.Env = append(os.Environ(), "PYTHONUNBUFFERED=1")
	command.Stderr = os.Stderr
	stdin, err := command.StdinPipe()
	if err != nil {
		return fmt.Errorf("open model worker stdin: %w", err)
	}
	stdout, err := command.StdoutPipe()
	if err != nil {
		_ = stdin.Close()
		return fmt.Errorf("open model worker stdout: %w", err)
	}
	if err := command.Start(); err != nil {
		_ = stdin.Close()
		_ = stdout.Close()
		return fmt.Errorf("start model worker: %w", err)
	}

	state := &processState{
		command:   command,
		stdin:     stdin,
		responses: make(chan protocol.WorkerResponse, 1),
		scanErr:   make(chan error, 1),
		scanStop:  make(chan struct{}),
		scanDone:  make(chan struct{}),
		done:      make(chan struct{}),
	}
	p.state = state
	go func() {
		state.waitErr = command.Wait()
		close(state.done)
	}()
	go func() {
		defer close(state.scanDone)
		scanResponses(stdout, state.responses, state.scanErr, state.scanStop, p.config.MaxResponseBytes)
	}()

	startupContext, cancel := context.WithTimeout(ctx, p.config.StartupTimeout)
	defer cancel()
	select {
	case response := <-state.responses:
		if response.Type != "ready" || response.ProtocolVersion != protocol.Version {
			p.killLocked()
			return fmt.Errorf("unexpected model worker handshake: type=%q protocol=%d", response.Type, response.ProtocolVersion)
		}
		return nil
	case err := <-state.scanErr:
		waitErr := p.killLocked()
		if errors.Is(err, io.EOF) {
			return processExitError("before handshake", waitErr)
		}
		return fmt.Errorf("model worker handshake: %w", err)
	case <-state.done:
		state.stopScanner()
		<-state.scanDone
		p.state = nil
		return processExitError("before handshake", state.waitErr)
	case <-startupContext.Done():
		p.killLocked()
		return fmt.Errorf("model worker startup: %w", startupContext.Err())
	}
}

func scanResponses(
	reader io.Reader,
	responses chan<- protocol.WorkerResponse,
	scanErr chan<- error,
	stop <-chan struct{},
	maxBytes int,
) {
	sendError := func(err error) {
		select {
		case scanErr <- err:
		case <-stop:
		}
	}
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 64*1024), maxBytes)
	for scanner.Scan() {
		var response protocol.WorkerResponse
		if err := json.Unmarshal(scanner.Bytes(), &response); err != nil {
			sendError(fmt.Errorf("invalid JSONL response: %w", err))
			return
		}
		select {
		case responses <- response:
		case <-stop:
			return
		}
	}
	if err := scanner.Err(); err != nil {
		sendError(fmt.Errorf("read model worker stdout: %w", err))
		return
	}
	sendError(io.EOF)
}

func writeFull(writer io.Writer, payload []byte) error {
	for len(payload) > 0 {
		written, err := writer.Write(payload)
		if err != nil {
			return err
		}
		if written == 0 {
			return io.ErrShortWrite
		}
		payload = payload[written:]
	}
	return nil
}

func processExitError(phase string, err error) error {
	if err == nil {
		return fmt.Errorf("model worker exited %s", phase)
	}
	return fmt.Errorf("model worker exited %s: %w", phase, err)
}

func (p *Process) Infer(ctx context.Context, id string, event json.RawMessage) (*protocol.Decision, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(event) > p.config.MaxEventBytes {
		return nil, fmt.Errorf("event is %d bytes; maximum is %d", len(event), p.config.MaxEventBytes)
	}
	if err := p.startLocked(ctx); err != nil {
		return nil, err
	}
	request, err := json.Marshal(protocol.WorkerRequest{ID: id, Event: event})
	if err != nil {
		return nil, fmt.Errorf("encode model request: %w", err)
	}
	request = append(request, '\n')
	state := p.state
	writeDone := make(chan error, 1)
	go func() { writeDone <- writeFull(state.stdin, request) }()
	select {
	case err := <-writeDone:
		if err != nil {
			p.killLocked()
			return nil, fmt.Errorf("write model request: %w", err)
		}
	case <-ctx.Done():
		p.killLocked()
		<-writeDone // Closing stdin and reaping the child guarantees the blocked write returns.
		return nil, fmt.Errorf("model worker request write: %w", ctx.Err())
	case <-state.done:
		state.stopScanner()
		p.state = nil
		<-writeDone
		<-state.scanDone
		return nil, processExitError("during request write", state.waitErr)
	}

	select {
	case response := <-state.responses:
		if response.ID != id {
			p.killLocked()
			return nil, fmt.Errorf("model worker response ID %q does not match request %q", response.ID, id)
		}
		if !response.OK {
			if response.Error == nil {
				return nil, errors.New("model worker returned an unspecified error")
			}
			return nil, fmt.Errorf("model worker %s: %s", response.Error.Code, response.Error.Message)
		}
		if response.Decision == nil {
			return nil, errors.New("model worker returned no decision")
		}
		return response.Decision, nil
	case err := <-state.scanErr:
		waitErr := p.killLocked()
		if errors.Is(err, io.EOF) {
			return nil, processExitError("before response", waitErr)
		}
		return nil, fmt.Errorf("model worker response: %w", err)
	case <-state.done:
		state.stopScanner()
		<-state.scanDone
		p.state = nil
		return nil, processExitError("before response", state.waitErr)
	case <-ctx.Done():
		p.killLocked()
		return nil, fmt.Errorf("model worker inference: %w", ctx.Err())
	}
}

func (p *Process) killLocked() error {
	state := p.state
	if state == nil {
		return nil
	}
	p.state = nil
	state.stopScanner()
	_ = state.stdin.Close()
	if state.command.Process != nil {
		_ = state.command.Process.Kill()
	}
	<-state.done
	<-state.scanDone
	return state.waitErr
}

func (p *Process) Close(ctx context.Context) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.closed = true
	return p.stopLocked(ctx)
}

func (p *Process) stopLocked(ctx context.Context) error {
	state := p.state
	if state == nil {
		return nil
	}
	p.state = nil
	state.stopScanner()
	_ = state.stdin.Close()
	timer := time.NewTimer(p.config.StopTimeout)
	defer timer.Stop()
	var result error
	select {
	case <-state.done:
		result = state.waitErr
	case <-ctx.Done():
		if state.command.Process != nil {
			_ = state.command.Process.Kill()
		}
		<-state.done
		result = ctx.Err()
	case <-timer.C:
		if state.command.Process != nil {
			_ = state.command.Process.Kill()
		}
		<-state.done
		result = errors.New("model worker did not exit before stop timeout")
	}
	<-state.scanDone
	return result
}
