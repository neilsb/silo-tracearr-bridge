package main

import (
	"bytes"
	"context"
	_ "embed"
	"html/template"

	v1 "github.com/Silo-Server/silo-plugin-sdk/pkg/pluginproto/silo/plugin/v1"
)

//go:embed diagnostics.html
var diagnosticsHTML string

var diagnosticsTemplate = template.Must(template.New("diagnostics").Parse(diagnosticsHTML))

func (p *pluginServer) diagnostics(ctx context.Context) (*v1.HandleHTTPResponse, error) {
	data, _ := p.status(ctx)
	var body bytes.Buffer
	if err := diagnosticsTemplate.Execute(&body, data); err != nil {
		return nil, err
	}
	// A failed upstream check is displayed within the page, not replaced by a
	// host error page. The machine-readable /status endpoint still returns 503.
	return &v1.HandleHTTPResponse{StatusCode: 200, Headers: map[string]string{"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}, Body: body.Bytes()}, nil
}
