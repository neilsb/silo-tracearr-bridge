package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	v1 "github.com/Silo-Server/silo-plugin-sdk/pkg/pluginproto/silo/plugin/v1"
	publicmanifest "github.com/Silo-Server/silo-plugin-sdk/pkg/pluginsdk/manifest"
	sdk "github.com/Silo-Server/silo-plugin-sdk/pkg/pluginsdk/runtime"
	"github.com/hashicorp/go-hclog"
	"github.com/hashicorp/go-plugin"
	"google.golang.org/protobuf/types/known/structpb"
)

func TestManifestPassesHostValidation(t *testing.T) {
	m, err := publicmanifest.LoadWithChecksum(manifest, version)
	if err != nil {
		t.Fatal(err)
	}
	if err := publicmanifest.Validate(m); err != nil {
		t.Fatal(err)
	}
}

func TestPluginProcessLifecycle(t *testing.T) {
	// Use the actual executable and go-plugin handshake, not a mocked Runtime.
	binary := filepath.Join(t.TempDir(), "plugin")
	cmd := exec.Command("go", "build", "-o", binary, ".")
	if out, e := cmd.CombinedOutput(); e != nil {
		t.Fatalf("build: %v %s", e, out)
	}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer upstream" {
			t.Error("wrong native credential")
		}
		switch r.URL.Path {
		case "/api/v2/admin/sessions":
			respond(w, 200, object{"items": []any{}})
		case "/api/v2/admin/sessions/command-capabilities":
			respond(w, 200, object{"actions": []string{"terminate"}, "terminate_revokes_authority": true})
		case "/api/v2/admin/sessions/capabilities":
			respond(w, 200, object{"available": true})
		case "/api/v2/admin/users":
			respond(w, 200, object{"items": []any{}})
		case "/api/v2/user/libraries":
			respond(w, 200, object{"items": []any{}})
		case "/api/v2/admin/catalog/capabilities":
			respond(w, 200, object{"item_listing": true})
		default:
			t.Errorf("unexpected native request %s", r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	defer upstream.Close()
	launch := func() (*plugin.Client, *sdk.Client) {
		host := plugin.NewClient(&plugin.ClientConfig{HandshakeConfig: sdk.HandshakeConfig(), Plugins: sdk.DefaultPluginSet(sdk.CapabilityServers{}), Cmd: exec.Command(binary), AllowedProtocols: []plugin.Protocol{plugin.ProtocolGRPC}, Logger: hclog.NewNullLogger()})
		t.Cleanup(host.Kill)
		rpc, e := host.Client()
		if e != nil {
			t.Fatal(e)
		}
		dispensed, e := rpc.Dispense(sdk.PluginSetName)
		if e != nil {
			t.Fatal(e)
		}
		return host, dispensed.(*sdk.Client)
	}
	host, rpc := launch()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	// Fresh installation must accept incomplete configuration.
	if _, e := rpc.Runtime().Configure(ctx, &v1.ConfigureRequest{}); e != nil {
		t.Fatal(e)
	}
	value, _ := structpb.NewStruct(object{"silo_url": upstream.URL, "silo_api_key": "upstream", "bridge_api_key": "tracearr", "listen_address": "127.0.0.1:0"})
	config := []*v1.ConfigEntry{{Key: "bridge", Value: value}}
	if _, e := rpc.Runtime().Configure(ctx, &v1.ConfigureRequest{Config: config}); e != nil {
		t.Fatal(e)
	}
	check, e := rpc.ConfigurationChecks().TestConfiguration(ctx, &v1.TestConfigurationRequest{Config: config})
	if e != nil || !check.GetOk() {
		t.Fatalf("check: %v %v", check, e)
	}
	task, e := rpc.ScheduledTask().Run(ctx, &v1.RunScheduledTaskRequest{TaskKey: "listener"})
	if e != nil {
		t.Fatal(e)
	}
	status := task.GetOutput().AsMap()
	address := str(status["listen_address"])
	if !yes(status["loopback_authenticated"]) {
		t.Fatalf("status: %v", status)
	}
	req, _ := http.NewRequest("GET", "http://"+address+"/Sessions", nil)
	resp, e := http.DefaultClient.Do(req)
	if e != nil {
		t.Fatal(e)
	}
	resp.Body.Close()
	if resp.StatusCode != 401 {
		t.Fatal("unauthenticated sessions exposed")
	}
	req.Header.Set("Authorization", `MediaBrowser Token="tracearr"`)
	resp, e = http.DefaultClient.Do(req)
	if e != nil {
		t.Fatal(e)
	}
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatal("Tracearr header rejected")
	}
	// A temporary settings checker must not bind/conflict with the live port.
	_, temp := launch()
	value.Fields["listen_address"] = structpb.NewStringValue(address)
	if _, e = temp.Runtime().Configure(ctx, &v1.ConfigureRequest{Config: config}); e != nil {
		t.Fatal(e)
	}
	check, e = temp.ConfigurationChecks().TestConfiguration(ctx, &v1.TestConfigurationRequest{Config: config})
	if e != nil || !check.GetOk() {
		t.Fatalf("temporary check: %v %v", check, e)
	}
	statusResp, e := rpc.HttpRoutes().Handle(ctx, &v1.HandleHTTPRequest{Method: "GET", Path: "/status"})
	if e != nil || statusResp.GetStatusCode() != 200 {
		t.Fatalf("status RPC: %v %v", statusResp, e)
	}
	host.Kill()
	short := &http.Client{Timeout: time.Second}
	if resp, e := short.Get("http://" + address + "/health/live"); e == nil {
		resp.Body.Close()
		t.Fatal("listener survived plugin process stop")
	}
	// A newly launched process can reclaim the address and restore service.
	_, restart := launch()
	if _, e = restart.Runtime().Configure(ctx, &v1.ConfigureRequest{Config: config}); e != nil {
		t.Fatal(e)
	}
	if _, e = restart.ScheduledTask().Run(ctx, &v1.RunScheduledTaskRequest{TaskKey: "listener"}); e != nil {
		t.Fatal(e)
	}
	// Direct binary upload introspection emits valid self-checksummed JSON.
	out, e := exec.Command(binary, "manifest").Output()
	if e != nil {
		t.Fatal(e)
	}
	var m object
	if json.Unmarshal(out, &m) != nil || len(str(m["checksum"])) != 64 {
		t.Fatal("invalid manifest output")
	}
}

func TestExpiredReasonAndUnconfirmedRevocation(t *testing.T) {
	var reason string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "POST" {
			var body object
			json.NewDecoder(r.Body).Decode(&body)
			reason = str(body["reason"])
			respond(w, 200, object{"session_id": "a", "authority_revoked": false, "durable_state": "stopped"})
			return
		}
		respond(w, 200, object{"actions": []string{"terminate"}, "terminate_revokes_authority": true})
	}))
	defer upstream.Close()
	b := newBridge(Config{SiloURL: upstream.URL, SiloKey: "upstream", BridgeKey: "tracearr"})
	b.reasons["a"] = pendingReason{time.Now().Add(-time.Minute), "stale"}
	if _, _, e := b.control(context.Background(), "/Sessions/a/Playing/Stop", nil); e == nil {
		t.Fatal("unconfirmed termination succeeded")
	}
	if reason != "Stream terminated by Tracearr" {
		t.Fatal("stale reason reused")
	}
}
