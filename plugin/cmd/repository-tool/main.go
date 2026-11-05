// repository-tool converts SDK manifests to Silo's encoding/json catalog wire
// format and validates generated indexes with the same protobuf field types.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	v1 "github.com/Silo-Server/silo-plugin-sdk/pkg/pluginproto/silo/plugin/v1"
	"github.com/Silo-Server/silo-plugin-sdk/pkg/pluginsdk/manifest"
	"google.golang.org/protobuf/encoding/protojson"
)

func run() error {
	if len(os.Args) != 2 {
		return fmt.Errorf("usage: repository-tool manifest|repository.json")
	}
	if os.Args[1] == "manifest" {
		raw, err := io.ReadAll(os.Stdin)
		if err != nil {
			return err
		}
		var m v1.PluginManifest
		if err := protojson.Unmarshal(raw, &m); err != nil {
			return err
		}
		if err := manifest.Validate(&m); err != nil {
			return err
		}
		return json.NewEncoder(os.Stdout).Encode(&m)
	}
	raw, err := os.ReadFile(os.Args[1])
	if err != nil {
		return err
	}
	var index struct {
		Plugins []struct {
			Manifest *v1.PluginManifest `json:"manifest"`
			Binaries map[string]struct {
				URL      string `json:"url"`
				Checksum string `json:"checksum"`
			} `json:"binaries"`
		} `json:"plugins"`
	}
	if err := json.Unmarshal(raw, &index); err != nil {
		return err
	}
	if len(index.Plugins) != 1 {
		return fmt.Errorf("expected one plugin")
	}
	for _, p := range index.Plugins {
		if p.Manifest == nil {
			return fmt.Errorf("missing manifest")
		}
		if err := manifest.Validate(p.Manifest); err != nil {
			return err
		}
		for _, platform := range p.Manifest.SupportedPlatforms {
			binary, ok := p.Binaries[platform.Os+"/"+platform.Arch]
			if !ok || binary.URL == "" || binary.Checksum != p.Manifest.Checksum {
				return fmt.Errorf("missing or mismatched platform binary")
			}
		}
	}
	fmt.Println("Silo repository wire format validated")
	return nil
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
