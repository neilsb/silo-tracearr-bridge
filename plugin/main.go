package main

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"sync"
	"time"

	v1 "github.com/Silo-Server/silo-plugin-sdk/pkg/pluginproto/silo/plugin/v1"
	sdk "github.com/Silo-Server/silo-plugin-sdk/pkg/pluginsdk/runtime"
	"google.golang.org/protobuf/types/known/structpb"
)

//go:embed manifest.json
var manifest []byte

const version = "0.2.4"

type pluginServer struct {
	v1.UnimplementedHttpRoutesServer
	v1.UnimplementedScheduledTaskServer
	v1.UnimplementedConfigurationChecksServer
	mu        sync.Mutex
	config    Config
	server    *http.Server
	address   string
	started   time.Time
	lastError string
	bridge    *Bridge
	starts    int
}

func configEntries(entries []*v1.ConfigEntry) (Config, error) {
	c := defaults()
	for _, entry := range entries {
		if entry.GetKey() == "bridge" {
			data, err := json.Marshal(entry.GetValue().AsMap())
			if err != nil {
				return c, errors.New("invalid bridge settings")
			}
			if err := json.Unmarshal(data, &c); err != nil {
				return c, errors.New("invalid bridge settings")
			}
		}
	}
	return c, nil
}

func (p *pluginServer) configure(_ context.Context, entries []*v1.ConfigEntry) error {
	c, err := configEntries(entries)
	if err != nil {
		return err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.server != nil {
		_ = p.server.Close()
		p.server = nil
	}
	p.config = c
	return nil
}

// Configure only stores settings: connection tests use temporary plugin
// processes and must not compete with the installed listener for its port.
func (p *pluginServer) start() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.server != nil {
		return nil
	}
	if err := p.config.validate(); err != nil {
		return err
	}
	listener, err := net.Listen("tcp", p.config.Listen)
	if err != nil {
		p.lastError = "cannot bind configured listener address"
		return errors.New(p.lastError)
	}
	srv := newHTTPServer(p.config)
	p.bridge = srv.Handler.(*Bridge)
	p.starts++
	p.server, p.address, p.started, p.lastError = srv, listener.Addr().String(), time.Now().UTC(), ""
	go func() {
		err := srv.Serve(listener)
		p.mu.Lock()
		defer p.mu.Unlock()
		if p.server == srv {
			p.server = nil
			if err != nil && !errors.Is(err, http.ErrServerClosed) {
				p.lastError = "listener exited"
			}
		}
	}()
	return nil
}

func (p *pluginServer) status(ctx context.Context) (data map[string]any, err error) {
	defer func() {
		p.mu.Lock()
		defer p.mu.Unlock()
		if data == nil {
			data = map[string]any{}
		}
		data["running"] = p.server != nil
		data["version"] = version
		data["listener_starts"] = p.starts
		data["last_listener_error"] = p.lastError
		if p.server != nil {
			data["listen_address"] = p.address
			data["uptime_seconds"] = time.Since(p.started).Seconds()
		}
		if err != nil {
			data["error"] = err.Error()
		}
		if b := p.bridge; b != nil {
			b.mu.Lock()
			defer b.mu.Unlock()
			data["upstream"] = map[string]any{
				"last_session_poll_at":              timestamp(b.lastPoll),
				"last_successful_session_poll_at":   timestamp(b.lastSuccess),
				"session_poll_error":                b.pollError,
				"consecutive_session_poll_failures": b.pollFailures,
				"session_poll_healthy":              !b.lastPoll.IsZero() && b.pollError == "",
			}
			catalogError := ""
			if b.catalogError != nil {
				catalogError = b.catalogError.Error()
			}
			data["catalog"] = map[string]any{"scan_running": b.catalogFlight != nil, "last_success_at": timestamp(b.catalogUpdated), "last_error": catalogError, "cached_items": len(b.rows["/admin/catalog/items"].rows)}
		}
	}()
	if err := p.start(); err != nil {
		return nil, err
	}
	p.mu.Lock()
	address, started, cfg := p.address, p.started, p.config
	p.mu.Unlock()
	_, port, _ := net.SplitHostPort(address)
	host, _, _ := net.SplitHostPort(address)
	if host == "" || host == "0.0.0.0" {
		host = "127.0.0.1"
	}
	if host == "::" {
		host = "::1"
	}
	target := "http://" + net.JoinHostPort(host, port)
	client := &http.Client{Timeout: 3 * time.Second}
	req, _ := http.NewRequestWithContext(ctx, "GET", target+"/health/live", nil)
	resp, err := client.Do(req)
	if err != nil {
		return nil, errors.New("listener loopback health check failed")
	}
	resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, errors.New("listener loopback health check failed")
	}
	// Exercise the same authenticated route Tracearr uses, not just a TCP bind.
	req, _ = http.NewRequestWithContext(ctx, "GET", target+"/Sessions", nil)
	req.Header.Set("Authorization", `MediaBrowser Token="`+cfg.BridgeKey+`"`)
	resp, err = (&http.Client{Timeout: 12 * time.Second}).Do(req)
	if err != nil {
		return nil, errors.New("authenticated loopback session check failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("authenticated loopback session check returned HTTP %d", resp.StatusCode)
	}
	var sessions []map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&sessions); err != nil {
		return nil, errors.New("invalid loopback session response")
	}
	return map[string]any{"running": true, "listen_address": address, "started_at": started.Format(time.RFC3339), "pid": os.Getpid(), "version": version, "loopback_authenticated": true, "active_sessions": len(sessions)}, nil
}

func (p *pluginServer) Handle(ctx context.Context, req *v1.HandleHTTPRequest) (*v1.HandleHTTPResponse, error) {
	if req.GetMethod() != "GET" || req.GetPath() != "/status" {
		return &v1.HandleHTTPResponse{StatusCode: 404}, nil
	}
	data, err := p.status(ctx)
	status := int32(200)
	if err != nil {
		status = 503
	}
	body, _ := json.Marshal(data)
	return &v1.HandleHTTPResponse{StatusCode: status, Headers: map[string]string{"Content-Type": "application/json"}, Body: body}, nil
}

func timestamp(t time.Time) any {
	if t.IsZero() {
		return nil
	}
	return t.Format(time.RFC3339Nano)
}

func (p *pluginServer) Run(ctx context.Context, _ *v1.RunScheduledTaskRequest) (*v1.RunScheduledTaskResponse, error) {
	data, err := p.status(ctx)
	if err != nil {
		return nil, err
	}
	output, _ := structpb.NewStruct(data)
	return &v1.RunScheduledTaskResponse{Output: output}, nil
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "serve" {
		c := defaults()
		for key, dst := range map[string]*string{"SILO_URL": &c.SiloURL, "SILO_API_KEY": &c.SiloKey, "BRIDGE_API_KEY": &c.BridgeKey, "BRIDGE_LISTEN": &c.Listen, "BRIDGE_NAME": &c.Name} {
			if v := os.Getenv(key); v != "" {
				*dst = v
			}
		}
		if err := c.validate(); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		if err := newHTTPServer(c).ListenAndServe(); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}
	p := &pluginServer{config: defaults()}
	sdk.ServeManifestWithOptions(manifest, version, sdk.CapabilityServers{HttpRoutes: p, ScheduledTask: p}, sdk.WithConfigure(p.configure), sdk.WithConfigurationChecks(p))
}
